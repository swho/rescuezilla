# ----------------------------------------------------------------------
#   Copyright (C) 2026 Rescuezilla Seekable-Index Community Fork Contributors
#   Added 2026-11-28 (Community Fork): SeekZilla Phase 1 (.rzbhash) and
#   Phase 2 (diff writer / synthesizer / merge) tests.
# ----------------------------------------------------------------------
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.
# ----------------------------------------------------------------------
import copy
import fnmatch
import glob
import hashlib
import io
import os
import random
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from unittest import mock

import block_hash as bh
import diff_format as df
import diff_synthesizer as ds
import diff_writer as dw
import seekable_compressor as sc
from utility import Utility

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST_DIR = os.path.dirname(PKG_DIR)
M32 = 0xFFFFFFFF
MiB = 1024 * 1024


# ----------------------------------------------------------------------
# Independent reference implementations (do not use the code under test)
# ----------------------------------------------------------------------


def ref_crc_nofinal(data: bytes, state: int = M32) -> int:
    """Bitwise reflected CRC-32 (poly 0xEDB88320), init 0xFFFFFFFF, no final XOR."""
    crc = state
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 & -(crc & 1))
    return crc & M32


def zcrc(data: bytes) -> int:
    return zlib.crc32(data) ^ M32


def build_ptcl(
    blocks: dict,
    totalblock: int,
    block_size: int,
    *,
    fs: bytes = b"EXTFS",
    bitmap_mode: int = bh.BM_BIT,
    checksum: bool = True,
    bpcs: int = 64,
    reseed: int = 1,
    feature_size: int = 18,
    device_size=None,
    garbage_tail_bits: bool = False,
) -> bytes:
    """Synthetic Partclone v0002 image built from the format rules only.

    Layout as in partclone.c (image_desc_v2 / write_image_bitmap / data loop):
    header + header CRC32 (ALWAYS, also for CSM_NONE), bitmap + bitmap CRC32
    (iff bitmap_mode != BM_NONE, over the bit-packed bitmap even for BM_BYTE),
    data with a CRC32 per `bpcs` blocks + final partial group (iff CSM_CRC32).
    """
    used = sorted(blocks)
    if device_size is None:
        device_size = totalblock * block_size
    head = struct.pack(
        "<16s14s4sH16sQQQQI",
        b"partclone-image\x00",
        b"0.3.27".ljust(14, b"\x00"),
        b"0002",
        0xC0DE,
        fs.ljust(16, b"\x00"),
        device_size,
        totalblock,
        len(used),
        len(used),
        block_size,
    )
    opts = struct.pack(
        "<IHHHHIBB",
        feature_size,
        2,
        64,
        0x20 if checksum else 0x00,
        4 if checksum else 0,
        bpcs if checksum else 0,
        reseed,
        bitmap_mode,
    ) + b"\xab" * (feature_size - 18)
    hdr = head + opts
    hdr += struct.pack("<I", zcrc(hdr))  # header CRC32: always present
    packed_bits = bytearray((totalblock + 7) // 8)
    for b in used:
        packed_bits[b >> 3] |= 1 << (b & 7)
    if bitmap_mode == bh.BM_BIT:
        bm = bytearray(packed_bits)
        if garbage_tail_bits and totalblock % 8:
            bm[-1] |= (0xFF << (totalblock % 8)) & 0xFF
        bm_crc_src = bytes(bm)  # partclone CRCs exactly the bytes it writes
    elif bitmap_mode == bh.BM_BYTE:
        bm = bytearray(totalblock)
        for b in used:
            bm[b] = 1
        bm_crc_src = bytes(packed_bits)  # CRC over the bit-packed bitmap
    else:
        assert len(used) == totalblock
        bm = bytearray()
        bm_crc_src = None  # BM_NONE: neither bitmap nor bitmap CRC32
    out = bytearray(hdr + bm)
    if bm_crc_src is not None:
        out += struct.pack("<I", zcrc(bm_crc_src))
    if checksum:
        z_all = 0
        for gi in range(0, len(used), bpcs):
            group = b"".join(blocks[b] for b in used[gi : gi + bpcs])
            out += group
            if reseed:
                crc = zlib.crc32(group) ^ M32
            else:
                z_all = zlib.crc32(group, z_all)
                crc = z_all ^ M32
            out += struct.pack("<I", crc)
    else:
        for b in used:
            out += blocks[b]
    return bytes(out)


def ref_digests(blocks: dict, totalblock: int, block_size: int, chunk_size: int) -> bytes:
    bpc = max(chunk_size, block_size) // block_size
    num = (totalblock + bpc - 1) // bpc
    out = bytearray()
    for c in range(num):
        s = c * bpc
        e = min(totalblock, s + bpc)
        n = e - s
        bits = bytearray((n + 7) // 8)
        used = []
        for i, b in enumerate(range(s, e)):
            if b in blocks:
                bits[i >> 3] |= 1 << (i & 7)
                used.append(b)
        if not used:
            out += b"\x00" * 32
            continue
        h = hashlib.sha256(b"RZBH1" + struct.pack("<II", n, len(bits)) + bytes(bits))
        for b in used:
            h.update(blocks[b])
        out += h.digest()
    return bytes(out)


def rand_blocks(rng, idxs, block_size, zero_every=0) -> dict:
    out = {}
    for n, i in enumerate(sorted(idxs)):
        if zero_every and n % zero_every == 0:
            out[i] = b"\x00" * block_size
        else:
            out[i] = rng.randbytes(block_size)
    return out


def feed_all(data: bytes, step: int, chunk_size: int):
    feed = bh.StreamingBlockHashFeed(chunk_size=chunk_size)
    for i in range(0, len(data), step):
        feed.feed(data[i : i + step])
    return feed.finish()


def write_splits(prefix: str, data: bytes, split_size: int):
    paths = []
    n = max(1, (len(data) + split_size - 1) // split_size)
    for i in range(n):
        p = prefix + ds.SplitFileWriter.suffix(i)
        with open(p, "wb") as f:
            f.write(data[i * split_size : (i + 1) * split_size])
        paths.append(p)
    return paths


def flip_bit_in_splits(paths, global_offset: int, bit: int = 0) -> None:
    for p in paths:
        size = os.path.getsize(p)
        if global_offset < size:
            with open(p, "r+b") as f:
                f.seek(global_offset)
                b = f.read(1)[0]
                f.seek(global_offset)
                f.write(bytes([b ^ (1 << bit)]))
            return
        global_offset -= size
    raise AssertionError("offset beyond split files")


def run_py(args, **kw):
    kw.setdefault("cwd", tempfile.gettempdir())
    return subprocess.run(
        [sys.executable] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        **kw,
    )


def pkg_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = DIST_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return env


# ----------------------------------------------------------------------
# Phase 1: block_hash
# ----------------------------------------------------------------------


class PartcloneHelpersTest(unittest.TestCase):
    def test_partclone_crc32_matches_bitwise_reference(self):
        rng = random.Random(1)
        for n in (0, 1, 7, 64, 1000):
            data = rng.randbytes(n)
            self.assertEqual(bh.partclone_crc32(data), ref_crc_nofinal(data))
        a, b = rng.randbytes(333), rng.randbytes(777)
        self.assertEqual(
            bh.partclone_crc32(b, bh.partclone_crc32(a)), ref_crc_nofinal(a + b)
        )
        self.assertEqual(bh.partclone_crc32(b""), 0xFFFFFFFF)

    def test_header_parse_dynamic_feature_size_and_crc(self):
        rng = random.Random(2)
        blocks = rand_blocks(rng, range(0, 50, 3), 4096)
        for fsz in (18, 26):
            img = build_ptcl(blocks, 50, 4096, feature_size=fsz, bpcs=5)
            h = bh.parse_partclone_header(img)
            self.assertEqual(h.feature_size, fsz)
            self.assertEqual(h.header_total_size, 88 + fsz + 4)
            self.assertEqual(h.bitmap_bytes, (50 + 7) // 8)
            self.assertEqual(h.data_start, 88 + fsz + 4 + 7 + 4)
            self.assertEqual(h.fs_str, "EXTFS")
            self.assertEqual(h.blocks_per_checksum, 5)
            bad = bytearray(img)
            bad[60] ^= 0x01
            with self.assertRaises(bh.PartcloneStreamError):
                bh.parse_partclone_header(bytes(bad))
            bh.parse_partclone_header(bytes(bad), verify_crc=False)
        # CSM_NONE (-a0): header CRC and bitmap CRC are still present
        img_nocs = build_ptcl(blocks, 50, 4096, checksum=False)
        h = bh.parse_partclone_header(img_nocs)
        self.assertFalse(h.checksum_enabled)
        self.assertEqual((h.header_crc_size, h.bitmap_crc_size, h.data_crc_size), (4, 4, 0))
        self.assertEqual(h.header_total_size, 88 + 18 + 4)
        self.assertEqual(h.data_start, 110 + 7 + 4)
        self.assertEqual(h.header_crc, zcrc(img_nocs[:106]))
        bad = bytearray(img_nocs)
        bad[60] ^= 0x01  # header CRC32 is verified even with CSM_NONE
        with self.assertRaises(bh.PartcloneStreamError):
            bh.parse_partclone_header(bytes(bad))
        # BM_NONE + CSM_CRC32: no bitmap and no bitmap CRC32, data CRCs present
        all_blocks = rand_blocks(rng, range(50), 4096)
        h = bh.parse_partclone_header(build_ptcl(all_blocks, 50, 4096, bitmap_mode=bh.BM_NONE))
        self.assertEqual((h.bitmap_bytes, h.bitmap_crc_size, h.data_crc_size), (0, 0, 4))
        self.assertEqual(h.data_start, 110)
        # BM_BYTE: one byte per block + 4-byte bitmap CRC32
        h = bh.parse_partclone_header(build_ptcl(blocks, 50, 4096, bitmap_mode=bh.BM_BYTE))
        self.assertEqual((h.bitmap_bytes, h.bitmap_crc_size), (50, 4))
        self.assertEqual(h.data_start, 110 + 50 + 4)
        # CSM_CRC32 with blocks_per_checksum 0: layout undefined -> unsupported
        raw = bytearray(build_ptcl(blocks, 50, 4096)[:110])
        struct.pack_into("<I", raw, 88 + 12, 0)  # image_options_v2.blocks_per_checksum
        struct.pack_into("<I", raw, 106, zcrc(bytes(raw[:106])))
        with self.assertRaises(bh.PartcloneUnsupportedError):
            bh.parse_partclone_header(bytes(raw))

    def test_bitmap_helper_masking_rank_and_slices(self):
        # BM_BIT with garbage beyond totalblock=13 is masked
        raw = bytes([0b10110101, 0xFF])
        bm = bh.BitmapHelper(raw, bh.BM_BIT, 13)
        self.assertEqual(bm.bits, bytes([0b10110101, 0b00011111]))
        self.assertEqual(bm.used_count, 5 + 5)
        sl, used, n = bm.canonical_chunk_bitmap_and_count(1, 8)
        self.assertEqual((sl, used, n), (bytes([0b00011111]), 5, 5))
        # Unaligned chunks (blocks_per_chunk = 3)
        all_bits = [(bm.bits[i >> 3] >> (i & 7)) & 1 for i in range(13)]
        for c in range(5):
            sl, used, n = bm.canonical_chunk_bitmap_and_count(c, 3)
            exp_bits = all_bits[c * 3 : min(13, c * 3 + 3)]
            self.assertEqual(n, len(exp_bits))
            self.assertEqual(used, sum(exp_bits))
            exp = sum(b << i for i, b in enumerate(exp_bits))
            self.assertEqual(sl, exp.to_bytes((n + 7) // 8, "little"))
        with self.assertRaises(IndexError):
            bm.canonical_chunk_bitmap_and_count(5, 3)
        # rank() vs brute force across several 4096-bit superblocks
        rng = random.Random(3)
        tb = 20011
        used_set = {i for i in range(tb) if rng.random() < 0.37}
        raw = bytearray((tb + 7) // 8)
        for i in used_set:
            raw[i >> 3] |= 1 << (i & 7)
        bm = bh.BitmapHelper(bytes(raw), bh.BM_BIT, tb)
        prefix = 0
        expected = []
        for i in range(tb + 1):
            expected.append(prefix)
            if i < tb and i in used_set:
                prefix += 1
        for i in list(range(0, 70)) + [4095, 4096, 4097, 8191, 8192, 12345, tb - 1, tb]:
            self.assertEqual(bm.rank(i), expected[i], i)
        for _ in range(500):
            i = rng.randrange(tb + 1)
            self.assertEqual(bm.rank(i), expected[i], i)
        # BM_BYTE / BM_NONE canonicalisation
        # BM_BYTE: any non-zero byte means "used"
        bm_byte = bh.BitmapHelper(
            bytes(7 if i in used_set else 0 for i in range(tb)), bh.BM_BYTE, tb
        )
        self.assertEqual(bm_byte.bits, bm.bits)
        bm_none = bh.BitmapHelper(b"", bh.BM_NONE, 21)
        self.assertEqual(bm_none.bits, b"\xff\xff\x1f")
        self.assertEqual(bm_none.used_count, 21)

    def test_chunk_geometry(self):
        self.assertEqual(bh.compute_chunk_geometry(4096, 1000, MiB), (MiB, 256, 4))
        self.assertEqual(bh.compute_chunk_geometry(16384, 10, 4096), (16384, 1, 10))
        with self.assertRaises(ValueError):
            bh.compute_chunk_geometry(4096, 1000, MiB + 512)
        self.assertEqual(bh.compute_chunk_digest(5, b"\x00", b"", 0), bh.EMPTY_CHUNK_DIGEST)


class StreamingFeedTest(unittest.TestCase):
    def test_feed_matrix_bitmap_modes_crc_modes_block_sizes(self):
        rng = random.Random(10)
        # totalblock % 7 != 0 so BM_NONE also ends with a partial CRC group
        cases = [(512, 1003, 4096), (4096, 302, 65536), (16384, 78, MiB)]
        for block_size, tb, chunk in cases:
            for mode in (bh.BM_BIT, bh.BM_BYTE, bh.BM_NONE):
                for checksum, reseed in ((True, 1), (True, 0), (False, 1)):
                    with self.subTest(bs=block_size, mode=mode, cs=checksum, reseed=reseed):
                        if mode == bh.BM_NONE:
                            idxs = list(range(tb))
                        else:
                            idxs = [i for i in range(tb) if rng.random() < 0.6]
                            if len(idxs) % 7 == 0:
                                idxs = idxs[:-1]
                        blocks = rand_blocks(rng, idxs, block_size, zero_every=5)
                        self.assertNotEqual(len(blocks) % 7, 0)
                        img = build_ptcl(
                            blocks,
                            tb,
                            block_size,
                            bitmap_mode=mode,
                            checksum=checksum,
                            bpcs=7,
                            reseed=reseed,
                        )
                        idx = feed_all(img, 65536, chunk)
                        self.assertIsNotNone(idx)
                        self.assertEqual(idx.digests, ref_digests(blocks, tb, block_size, chunk))
                        self.assertEqual(idx.block_size, block_size)
                        self.assertEqual(idx.totalblock, tb)
                        self.assertEqual(idx.fs_str, "EXTFS")
                        self.assertEqual(idx.raw_header_bytes, bh.parse_partclone_header(img).raw)

    def test_bm_bit_trailing_garbage_and_bm_byte_canonical_equivalence(self):
        rng = random.Random(11)
        tb = 1021  # tb % 8 != 0, and n_blocks % 8 != 0 in the last chunk
        blocks = rand_blocks(rng, [i for i in range(tb) if rng.random() < 0.5], 512)
        ref = ref_digests(blocks, tb, 512, 4096)
        for kw in (
            dict(bitmap_mode=bh.BM_BIT),
            dict(bitmap_mode=bh.BM_BIT, garbage_tail_bits=True),
            dict(bitmap_mode=bh.BM_BYTE),
        ):
            img = build_ptcl(blocks, tb, 512, bpcs=13, **kw)
            self.assertEqual(feed_all(img, 4096, 4096).digests, ref, kw)

    def test_weird_feed_sizes_bit_identical(self):
        rng = random.Random(12)
        small_blocks = rand_blocks(rng, [i for i in range(203) if rng.random() < 0.5], 512)
        small = build_ptcl(small_blocks, 203, 512, bpcs=9, reseed=0)
        big_blocks = rand_blocks(rng, [i for i in range(1500) if rng.random() < 0.8], 4096)
        big = build_ptcl(big_blocks, 1500, 4096, bpcs=64)
        for img, chunk, steps in (
            (small, 4096, (1, 7, 131072, 4 * MiB)),
            (big, MiB, (131072, 1000003, 4 * MiB)),
        ):
            one_shot = feed_all(img, len(img), chunk).serialize()
            for step in steps:
                self.assertEqual(feed_all(img, step, chunk).serialize(), one_shot, step)

    def test_non_partclone_stream_returns_none(self):
        rng = random.Random(13)
        for data in (b"", b"part", b"xyzzy", rng.randbytes(300000)):
            self.assertIsNone(feed_all(data, 4096, MiB), data[:8])
        feed = bh.StreamingBlockHashFeed()
        feed.feed(rng.randbytes(1000))
        self.assertFalse(feed.is_partclone)
        # Unsupported version: skipped (not an error) for the compressor feed
        img = bytearray(build_ptcl({0: b"\x00" * 512}, 8, 512))
        img[30:34] = b"0001"
        feed = bh.StreamingBlockHashFeed()
        feed.feed(bytes(img))
        self.assertIsNone(feed.finish())
        self.assertIn("version", feed.unsupported_reason)

    def test_corruption_and_truncation_raise(self):
        rng = random.Random(14)
        blocks = rand_blocks(rng, [i for i in range(100) if i % 3], 4096)
        img = build_ptcl(blocks, 100, 4096, bpcs=7)
        h = bh.parse_partclone_header(img)
        bitmap_off = h.header_total_size
        data_start = h.data_start
        variants = {
            "header-crc": (60, None),
            "header-crc-field": (h.header_total_size - 2, None),
            "bitmap": (bitmap_off + 1, None),
            "bitmap-crc": (data_start - 1, None),
            "data": (data_start + 10, None),
            "data-crc": (data_start + 7 * 4096 + 1, None),
            "last-partial-crc": (len(img) - 1, None),
        }
        for name, (off, _x) in variants.items():
            bad = bytearray(img)
            bad[off] ^= 0x10
            with self.subTest(name=name), self.assertRaises(bh.PartcloneStreamError):
                feed_all(bytes(bad), 1000, 65536)
        for cut in (50, 100, bitmap_off + 3, data_start + 5, len(img) - 1):
            with self.subTest(cut=cut), self.assertRaises(bh.PartcloneStreamError):
                feed_all(img[:cut], 1000, 65536)
        with self.assertRaises(bh.PartcloneStreamError):
            feed_all(img + b"\x00", 1000, 65536)
        # CSM_NONE / BM_BYTE: header + bitmap CRC32s are still verified
        for kw in (dict(checksum=False), dict(bitmap_mode=bh.BM_BYTE)):
            img2 = build_ptcl(blocks, 100, 4096, bpcs=7, **kw)
            h2 = bh.parse_partclone_header(img2)
            self.assertIsNotNone(feed_all(img2, 1000, 65536))
            # bitmap byte 0: block 0 is free, so the flip also changes BM_BYTE's
            # canonical bits (bitmap CRC32 covers the bit-packed bitmap)
            for off in (60, h2.header_total_size - 1, h2.header_total_size, h2.data_start - 1):
                bad = bytearray(img2)
                bad[off] ^= 0x10
                with self.subTest(kw=kw, off=off), self.assertRaises(bh.PartcloneStreamError):
                    feed_all(bytes(bad), 1000, 65536)

    def test_low1_incompatible_bhash_chunk_size_is_passthrough(self):
        img = build_ptcl({0: b"\x01" * 4096, 5: b"\x02" * 4096}, 64, 4096)
        feed = bh.StreamingBlockHashFeed(chunk_size=MiB + 512)  # not a multiple of 4096
        feed.feed(img)
        self.assertIsNone(feed.finish())
        self.assertIn("multiple", feed.unsupported_reason)
        with tempfile.TemporaryDirectory() as td:
            bhp = os.path.join(td, "b.rzbhash")
            out = io.BytesIO()
            sc.compress_stream(
                io.BytesIO(img), out, fmt="zstd", level=1, frame_size=65536, threads=1,
                bhash_output_path=bhp, bhash_chunk_size=MiB + 512,
            )
            self.assertEqual(sc.decompress_zstd_frame(out.getvalue()), img)
            self.assertFalse(os.path.exists(bhp))
        # A corrupt header is still a hard error (PartcloneStreamError is a
        # ValueError too, but must not be downgraded like the geometry error).
        bad = bytearray(img)
        bad[60] ^= 1
        with self.assertRaises(bh.PartcloneStreamError):
            feed_all(bytes(bad), 4096, MiB + 512)

    def test_m3_memory_ownership(self):
        tb = 64 * 1024
        raw = bytearray(tb // 8)
        for i in range(0, tb, 3):
            raw[i >> 3] |= 1 << (i & 7)
        raw = bytes(raw)
        # Canonical BM_BIT bytes are reused as-is (no copy)
        self.assertIs(bh.BitmapHelper(raw, bh.BM_BIT, tb).bits, raw)
        # Garbage tail bits still force a masked copy
        garbage = raw[:-1] + b"\xff"
        self.assertEqual(bh.BitmapHelper(garbage, bh.BM_BIT, tb - 3).bits[-1], 0x1F)
        blocks = rand_blocks(random.Random(15), [i for i in range(300) if i % 2], 4096)
        img = build_ptcl(blocks, 300, 4096)
        feed = bh.StreamingBlockHashFeed(chunk_size=65536)
        feed.feed(img)
        self.assertEqual(feed.parser.raw_bitmap_bytes, b"")  # BitmapHelper owns it
        idx = feed.finish()
        self.assertIsInstance(idx.digests, bytearray)  # handed over without a copy
        self.assertEqual(idx.digests, ref_digests(blocks, 300, 4096, 65536))
        self.assertEqual(bh.parse_bhash_bytes(idx.serialize()).digests, idx.digests)
        # The plain parser (diff_writer's) keeps the raw bitmap
        parser = bh.PartcloneStreamParser(chunk_size=65536)
        parser.feed(img)
        parser.finish()
        self.assertEqual(parser.raw_bitmap_bytes, parser.bitmap.bits)


class BhashFileTest(unittest.TestCase):
    def _make_idx(self):
        rng = random.Random(20)
        blocks = rand_blocks(rng, [i for i in range(700) if rng.random() < 0.5], 4096)
        return feed_all(build_ptcl(blocks, 700, 4096), MiB, 65536)

    def test_roundtrip_atomic_and_bitflips(self):
        idx = self._make_idx()
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, bh.bhash_index_filename("sda1"))
            sha = bh.write_bhash_file(path, idx)
            self.assertEqual(glob.glob(path + ".tmp*"), [])
            with open(path, "rb") as f:
                raw = f.read()
            self.assertEqual(len(raw), 68 + idx.num_chunks * 32 + 32)
            self.assertEqual(raw[:8], b"RZBHASH1")
            self.assertEqual(sha, hashlib.sha256(raw).digest())
            loaded = bh.load_bhash_file(path)
            self.assertEqual(loaded.file_sha256, sha)
            self.assertEqual(loaded.digests, idx.digests)
            self.assertEqual(loaded.fs_name, idx.fs_name)
            for c in range(idx.num_chunks):
                self.assertEqual(loaded.get_digest(c), idx.digests[c * 32 : c * 32 + 32])
            for off in (3, 20, 40, 66, 68 + 5, 68 + idx.num_chunks * 32 - 1, len(raw) - 1):
                bad = bytearray(raw)
                bad[off] ^= 0x01
                with open(path, "wb") as f:
                    f.write(bad)
                with self.subTest(off=off), self.assertRaises(bh.BlockHashFormatError):
                    bh.load_bhash_file(path)
            with open(path, "wb") as f:
                f.write(raw[:-1])
            with self.assertRaises(bh.BlockHashFormatError):
                bh.load_bhash_file(path)

    def test_atomic_write_unlinks_tmp_on_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "x.bin")
            with mock.patch.object(bh.os, "replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    bh.atomic_write_bytes(path, b"abc")
            self.assertEqual(os.listdir(td), [])

    def test_utility_filename_and_cmd_list(self):
        self.assertEqual(Utility.get_bhash_index_filename("nvme0n1p2"), "Info-bhash-nvme0n1p2.rzbhash")
        self.assertEqual(bh.bhash_index_filename("nvme0n1p2"), Utility.get_bhash_index_filename("nvme0n1p2"))
        self.assertFalse(fnmatch.fnmatch("Info-bhash-sda1.rzbhash", "*-ptcl-img.*"))
        cmd = Utility.get_seekable_compression_cmd_list("zstd", 3, "/x/i.rzidx", bhash_output_path="/x/b.rzbhash")
        self.assertEqual(cmd[-2:], ["--bhash-output", "/x/b.rzbhash"])
        cmd = Utility.get_seekable_compression_cmd_list("gzip", 6, "/x/i.rzidx")
        self.assertNotIn("--bhash-output", cmd)


class NativeDecompressionTest(unittest.TestCase):
    def _payload(self):
        rng = random.Random(30)
        return rng.randbytes(200000) + b"\x00" * 500000 + rng.randbytes(77777)

    def _blob(self, fmt, payload):
        out = io.BytesIO()
        sc.compress_stream(io.BytesIO(payload), out, fmt=fmt, level=1, frame_size=65536, threads=2)
        return out.getvalue()

    def _stream_decode(self, fmt, blob, in_step=1000, max_len=10000):
        dec = sc.StreamDecompressor(fmt)
        out = bytearray()
        for i in range(0, len(blob), in_step):
            out += dec.decompress(blob[i : i + in_step], max_len)
            while not dec.needs_input:
                piece = dec.decompress(b"", max_len)
                if not piece:
                    break
                out += piece
        dec.finish()
        return bytes(out)

    def test_stream_decompressor_all_backends(self):
        payload = self._payload()
        for fmt in ("gzip", "zstd"):
            blob = self._blob(fmt, payload)
            self.assertEqual(self._stream_decode(fmt, blob), payload)
            with self.assertRaises(sc.StreamDecompressError):
                self._stream_decode(fmt, blob[:-3])
        blob = self._blob("zstd", payload)
        if sc._LIBZSTD_D is not None:
            with mock.patch.object(sc, "_py_zstd", None):
                self.assertEqual(self._stream_decode("zstd", blob), payload)
                self.assertEqual(sc.decompress_zstd_frame(blob, len(payload)), payload)
        if sc._py_zstd is not None:
            with mock.patch.object(sc, "_LIBZSTD_D", None):
                self.assertEqual(self._stream_decode("zstd", blob), payload)
                self.assertEqual(sc.decompress_zstd_frame(blob, len(payload)), payload)
        with self.assertRaises(sc.StreamDecompressError):
            sc.decompress_zstd_frame(blob, len(payload) - 1)

    def test_gzip_frame_libdeflate_and_zlib(self):
        data = self._payload()[:300000]
        frame = sc.compress_gzip_frame(data, 6)
        self.assertEqual(sc.decompress_gzip_frame(frame, len(data)), data)
        with mock.patch.object(sc, "_LIBDEFLATE_D", None):
            self.assertEqual(sc.decompress_gzip_frame(frame, len(data)), data)
            with self.assertRaises(sc.StreamDecompressError):
                sc.decompress_gzip_frame(frame + b"\x00", len(data))
        if sc._LIBDEFLATE_D is not None:
            with self.assertRaises(sc.StreamDecompressError):
                sc.decompress_gzip_frame(frame + frame, len(data))
        bad = bytearray(frame)
        bad[-6] ^= 0x01  # CRC32 trailer
        with self.assertRaises(sc.StreamDecompressError):
            sc.decompress_gzip_frame(bytes(bad), len(data))
        empty = sc.compress_gzip_frame(b"", 6)
        self.assertEqual(sc.decompress_gzip_frame(empty, 0), b"")

    def test_seekable_split_stream_random_access(self):
        payload = self._payload()
        rng = random.Random(31)
        for fmt in ("gzip", "zstd"):
            with tempfile.TemporaryDirectory() as td:
                idx = os.path.join(td, "i.rzidx")
                out = io.BytesIO()
                sc.compress_stream(
                    io.BytesIO(payload), out, fmt=fmt, level=1, frame_size=65536,
                    index_output=idx, threads=2,
                )
                paths = write_splits(os.path.join(td, "p.img."), out.getvalue(), 50000)
                for seekidx in (idx, None):
                    with self.subTest(fmt=fmt, idx=bool(seekidx)):
                        s = sc.SeekableSplitStream(paths, fmt, seekidx)
                        self.assertEqual(s.mode, "index" if seekidx else "stream")
                        self.assertEqual(s.read(), payload)
                        for _ in range(40):
                            off = rng.randrange(len(payload))
                            n = rng.randrange(1, 200000)
                            s.seek(off)
                            self.assertEqual(s.read(n), payload[off : off + n])
                        s.seek(len(payload) - 3)
                        with self.assertRaises(EOFError):
                            s.read_exact(4)
                        s.close()
                with open(idx, "r+b") as f:
                    f.seek(10)
                    f.write(b"\xff")
                with self.assertRaises(ValueError):
                    sc.SeekableSplitStream(paths, fmt, idx)


class CompressorBhashIntegrationTest(unittest.TestCase):
    def _image(self):
        rng = random.Random(40)
        tb = 900
        blocks = rand_blocks(rng, [i for i in range(tb) if rng.random() < 0.7], 4096, zero_every=4)
        return build_ptcl(blocks, tb, 4096, bpcs=64, fs=b"NTFS"), blocks, tb

    def test_bhash_output_and_build_from_image_gzip_zstd(self):
        img, blocks, tb = self._image()
        ref = ref_digests(blocks, tb, 4096, 65536)
        script = os.path.join(PKG_DIR, "seekable_compressor.py")
        for fmt, suffix in (("gzip", "gz"), ("zstd", "zst")):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as td:
                idx = os.path.join(td, "Info-seekidx-sda1.rzidx")
                bhp = os.path.join(td, "Info-bhash-sda1.rzbhash")
                proc = run_py(
                    [script, "--format", fmt, "--level", "1", "--frame-size", "65536",
                     "--threads", "2", "--index-output", idx, "--bhash-output", bhp,
                     "--bhash-chunk-size", "65536", "--print-stats"],
                    input=img,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn(b"[seekable_compressor]", proc.stderr)
                write_splits(os.path.join(td, f"sda1.ntfs-ptcl-img.{suffix}."), proc.stdout, 700000)
                with open(idx, "rb") as f:
                    meta = sc.parse_seekidx_bytes(f.read())
                self.assertEqual(meta["total_uncompressed"], len(img))
                loaded = bh.load_bhash_file(bhp)
                self.assertEqual(loaded.digests, ref)
                self.assertEqual(loaded.fs_str, "NTFS")
                with open(bhp, "rb") as f:
                    online = f.read()
                # dd image in the same dir must be skipped
                write_splits(os.path.join(td, f"sda2.dd-ptcl-img.{suffix}."), proc.stdout[:100], 1000)
                os.remove(bhp)
                proc2 = run_py(
                    ["-m", "rescuezilla.block_hash", "build-from-image", "--image-dir", td,
                     "--chunk-size", "65536"],
                    env=pkg_env(), cwd=td,
                )
                self.assertEqual(proc2.returncode, 0, proc2.stderr)
                with open(bhp, "rb") as f:
                    self.assertEqual(f.read(), online)
                self.assertFalse(os.path.exists(os.path.join(td, "Info-bhash-sda2.rzbhash")))
                # Existing file is kept without --force
                proc3 = run_py([os.path.join(PKG_DIR, "block_hash.py"), "build-from-image", "--image-dir", td])
                self.assertEqual(proc3.returncode, 0, proc3.stderr)
                self.assertIn(b"exists", proc3.stdout)
                # Sequential (no .rzidx) offline rebuild with direct script + --force
                os.remove(idx)
                proc4 = run_py(
                    [os.path.join(PKG_DIR, "block_hash.py"), "build-from-image", "--image-dir", td,
                     "--partition", "sda1", "--force", "--chunk-size", "65536"]
                )
                self.assertEqual(proc4.returncode, 0, proc4.stderr)
                with open(bhp, "rb") as f:
                    self.assertEqual(f.read(), online)
                self.assertEqual(
                    bh.build_bhash_from_splits(
                        bh.list_split_files(os.path.join(td, f"sda1.ntfs-ptcl-img.{suffix}.")),
                        fmt, os.path.join(td, "x.rzbhash"), chunk_size=65536,
                    ).digests,
                    ref,
                )

    def test_dd_stream_skips_bhash_and_corrupt_partclone_fails_clean(self):
        img, _b, _t = self._image()
        script = os.path.join(PKG_DIR, "seekable_compressor.py")
        with tempfile.TemporaryDirectory() as td:
            idx = os.path.join(td, "Info-seekidx-sda2.rzidx")
            bhp = os.path.join(td, "Info-bhash-sda2.rzbhash")
            args = [script, "--format", "zstd", "--level", "1", "--frame-size", "65536",
                    "--index-output", idx, "--bhash-output", bhp]
            proc = run_py(args, input=random.Random(41).randbytes(300000))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(os.path.exists(idx))
            self.assertFalse(os.path.exists(bhp))
            os.remove(idx)
            bad = bytearray(img)
            bad[len(bad) // 2] ^= 0x01
            proc = run_py(args, input=bytes(bad))
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn(b"CRC32", proc.stderr)
            self.assertEqual(sorted(os.listdir(td)), [])
            proc = run_py(args, input=img[:-10])
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(sorted(os.listdir(td)), [])


# ----------------------------------------------------------------------
# Phase 2: diff writer / synthesizer / merge
# ----------------------------------------------------------------------

BS = 4096
CHUNK = MiB
BPC = CHUNK // BS
TB = BPC * 12 + 77
PART = "sda1"
CZFS = "ext4"


def make_full_dir(d, img, fmt="zstd", with_idx=True, split_size=400000, chunk=CHUNK):
    os.makedirs(d, exist_ok=True)
    idx = os.path.join(d, f"Info-seekidx-{PART}.rzidx")
    bhp = os.path.join(d, bh.bhash_index_filename(PART))
    prefix = os.path.join(d, f"{PART}.{CZFS}-ptcl-img.{bh.FORMAT_TO_COMPRESSION_SUFFIX[fmt]}.")
    if fmt == "uncompressed":
        paths = write_splits(prefix, img, split_size)
        bh.write_bhash_file(bhp, bh.build_bhash_from_stream(io.BytesIO(img), chunk))
    else:
        out = io.BytesIO()
        sc.compress_stream(
            io.BytesIO(img), out, fmt=fmt, level=1, frame_size=65536,
            index_output=idx if with_idx else None, threads=2,
            bhash_output_path=bhp, bhash_chunk_size=chunk,
        )
        paths = write_splits(prefix, out.getvalue(), split_size)
    with open(os.path.join(d, "parts"), "w") as f:
        f.write(PART + "\n")
    return paths


def make_diff_dir(diff_dir, base_dir, new_img, fmt="zstd", base_header_sha256=None):
    os.makedirs(diff_dir, exist_ok=True)
    suffix = bh.FORMAT_TO_COMPRESSION_SUFFIX[fmt]
    base_bhash = os.path.join(base_dir, bh.bhash_index_filename(PART))
    out = io.BytesIO()
    stats = dw.write_diff_stream(
        io.BytesIO(new_img), out,
        base_bhash_path=base_bhash,
        meta_output=os.path.join(diff_dir, df.get_diff_meta_filename(PART, CZFS)),
        index_output=os.path.join(diff_dir, df.get_diff_seekidx_filename(PART)),
        base_header_sha256=base_header_sha256,
        fmt=fmt, level=1, frame_size=65536, threads=2,
    )
    write_splits(os.path.join(diff_dir, df.get_diff_data_prefix(PART, CZFS, suffix)), out.getvalue(), 300000)
    base_sha = bh.load_bhash_file(base_bhash).file_sha256
    manifest = df.make_manifest(
        base_relative_path=os.path.relpath(base_dir, diff_dir),
        base_suggested_dir_name=os.path.basename(base_dir),
        partitions={PART: df.make_partition_entry(
            short_part=PART, fs=CZFS, compression=fmt, base_bhash_sha256=base_sha)},
    )
    df.write_diff_manifest(diff_dir, manifest)
    df.write_diff_readme(diff_dir, os.path.basename(base_dir))
    for name, text in (("parts", PART + "\n"), ("clonezilla-img", "log\n"), ("Info-img-id.txt", "IMG_ID=x\n")):
        with open(os.path.join(diff_dir, name), "w") as f:
            f.write(text)
    return stats, out.getvalue()


def scenario(seed=50, reseed=1, **build_kw):
    """Base/new block maps exercising shift avalanche, modify, free, fill/empty chunks."""
    rng = random.Random(seed)
    used = {b for b in range(TB) if rng.random() < 0.6}
    used -= set(range(0, 40))  # free space at the start of chunk 0
    used -= set(range(3 * BPC, 4 * BPC))  # chunk 3 empty in base
    base = rand_blocks(rng, used, BS, zero_every=6)
    if len(base) % 64 == 0:  # guarantee a partial trailing CRC32 group
        del base[max(base)]
    new = dict(base)
    for b in range(0, 10):  # allocate at partition start -> every later block shifts
        new[b] = rng.randbytes(BS)
    victim = min(b for b in base if 5 * BPC <= b < 6 * BPC)
    new[victim] = rng.randbytes(BS)  # modify chunk 5
    for b in [b for b in base if 9 * BPC <= b < 10 * BPC][:5]:
        del new[b]  # free some blocks of chunk 9
    for b in [b for b in base if 7 * BPC <= b < 8 * BPC]:
        del new[b]  # chunk 7 becomes empty
    for b in range(3 * BPC, 3 * BPC + 33):
        new[b] = rng.randbytes(BS)  # chunk 3 becomes used
    if len(new) % 64 == 0:  # keep chunk 9 changed, add one block there
        new[min(b for b in range(9 * BPC, 10 * BPC) if b not in new)] = rng.randbytes(BS)
    base_img = build_ptcl(base, TB, BS, bpcs=64, reseed=reseed, **build_kw)
    new_img = build_ptcl(new, TB, BS, bpcs=64, reseed=reseed, **build_kw)
    return base, new, base_img, new_img, {0, 3, 5, 9}


def chunk_used(blocks, c):
    return sum(1 for b in blocks if c * BPC <= b < min(TB, (c + 1) * BPC))


class DiffEngineTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = self._td.name
        self.base_dir = os.path.join(self.root, "2026-11-01-img-base")
        self.diff_dir = os.path.join(self.root, "2026-11-02-img-diff")

    def tearDown(self):
        self._td.cleanup()

    def _synth(self, base_dir=None, **kw):
        out = io.BytesIO()
        ds.synthesize_partition_stream(self.diff_dir, base_dir, PART, out, **kw)
        return out.getvalue()

    def test_roundtrip_bit_exact_shift_avalanche(self):
        for fmt in ("zstd", "gzip"):
            for reseed in (1, 0):
                with self.subTest(fmt=fmt, reseed=reseed):
                    shutil.rmtree(self.base_dir, ignore_errors=True)
                    shutil.rmtree(self.diff_dir, ignore_errors=True)
                    _base, new, base_img, new_img, expected = scenario(reseed=reseed)
                    make_full_dir(self.base_dir, base_img, fmt=fmt)
                    stats, delta = make_diff_dir(self.diff_dir, self.base_dir, new_img, fmt=fmt)
                    self.assertEqual(stats["changed_chunks"], len(expected))
                    non_empty = sum(1 for c in range(13) if chunk_used(new, c))
                    self.assertEqual(stats["unchanged_chunks"], non_empty - len(expected))
                    exp_delta = sum(chunk_used(new, c) for c in expected) * BS
                    self.assertEqual(stats["delta_bytes"], exp_delta)
                    meta = df.load_diff_meta(os.path.join(self.diff_dir, df.get_diff_meta_filename(PART, CZFS)))
                    self.assertEqual({c for c, _o, _n in meta.delta_chunk_offsets}, expected)
                    self.assertEqual(meta.raw_partclone_header, bh.parse_partclone_header(new_img).raw)
                    with open(os.path.join(self.diff_dir, df.get_diff_seekidx_filename(PART)), "rb") as f:
                        self.assertEqual(sc.parse_seekidx_bytes(f.read())["total_uncompressed"], exp_delta)
                    self.assertEqual(self._synth(), new_img)
                    self.assertEqual(self._synth(verify_base_crc=False, verify_base_hashes=False), new_img)

    def test_m1_partclone_layout_modes_roundtrip(self):
        """(a) -a0 CSM_NONE+BM_BIT, (b) CSM_CRC32+BM_NONE, (c) CSM_CRC32+BM_BYTE."""
        cases = (
            ("a-csm-none-bm-bit", dict(bitmap_mode=bh.BM_BIT, checksum=False)),
            ("b-csm-crc32-bm-none", dict(bitmap_mode=bh.BM_NONE, checksum=True)),
            ("c-csm-crc32-bm-byte", dict(bitmap_mode=bh.BM_BYTE, checksum=True)),
        )
        for name, kw in cases:
            with self.subTest(name=name):
                shutil.rmtree(self.base_dir, ignore_errors=True)
                shutil.rmtree(self.diff_dir, ignore_errors=True)
                mode = kw["bitmap_mode"]
                if mode == bh.BM_NONE:
                    rng = random.Random(70)
                    base = rand_blocks(rng, range(TB), BS, zero_every=7)
                    new = dict(base)
                    for b in (2 * BPC + 5, 8 * BPC + 11, TB - 1):
                        new[b] = rng.randbytes(BS)
                    expected = {2, 8, (TB - 1) // BPC}
                    base_img = build_ptcl(base, TB, BS, bpcs=64, **kw)
                    new_img = build_ptcl(new, TB, BS, bpcs=64, **kw)
                else:
                    base, new, base_img, new_img, expected = scenario(seed=71, **kw)

                # Layout exactly as partclone.c writes it
                h = bh.parse_partclone_header(new_img)
                nbm = {bh.BM_BIT: (TB + 7) // 8, bh.BM_BYTE: TB, bh.BM_NONE: 0}[mode]
                bm_crc = 0 if mode == bh.BM_NONE else 4
                self.assertEqual(h.header_crc_size, 4)
                self.assertEqual(h.header_total_size, 88 + 18 + 4)
                self.assertEqual(h.bitmap_crc_size, bm_crc)
                self.assertEqual(h.data_crc_size, 4 if kw["checksum"] else 0)
                self.assertEqual(h.data_start, 110 + nbm + bm_crc)
                groups = (len(new) + 63) // 64 if kw["checksum"] else 0
                self.assertEqual(len(new_img), h.data_start + len(new) * BS + groups * 4)
                self.assertEqual(struct.unpack_from("<I", new_img, 106)[0], zcrc(new_img[:106]))
                if mode == bh.BM_BYTE:
                    packed = bytearray((TB + 7) // 8)
                    for b in new:
                        packed[b >> 3] |= 1 << (b & 7)
                    stored = struct.unpack_from("<I", new_img, 110 + TB)[0]
                    self.assertEqual(stored, zcrc(bytes(packed)))
                    self.assertNotEqual(stored, zcrc(new_img[110 : 110 + TB]))

                # Phase 1: online .rzbhash of the base
                make_full_dir(self.base_dir, base_img)
                loaded = bh.load_bhash_file(os.path.join(self.base_dir, bh.bhash_index_filename(PART)))
                self.assertEqual(loaded.digests, ref_digests(base, TB, BS, CHUNK))
                # Phase 2: diff + synthesize + merge
                stats, _d = make_diff_dir(self.diff_dir, self.base_dir, new_img)
                self.assertEqual(stats["changed_chunks"], len(expected))
                meta = df.load_diff_meta(os.path.join(self.diff_dir, df.get_diff_meta_filename(PART, CZFS)))
                self.assertEqual({c for c, _o, _n in meta.iter_offsets()}, expected)
                self.assertEqual(self._synth(), new_img)
                self.assertEqual(self._synth(verify_base_crc=False), new_img)
                out_dir = os.path.join(self.root, "merged-" + name)
                ds.merge_diff_image(self.diff_dir, out_dir, level=1, frame_size=65536, threads=2)
                with sc.SeekableSplitStream(
                    bh.list_split_files(os.path.join(out_dir, f"{PART}.{CZFS}-ptcl-img.zst.")),
                    "zstd", os.path.join(out_dir, f"Info-seekidx-{PART}.rzidx"),
                ) as s:
                    self.assertEqual(s.read(), new_img)
                merged = bh.load_bhash_file(os.path.join(out_dir, bh.bhash_index_filename(PART)))
                self.assertEqual(merged.digests, ref_digests(new, TB, BS, CHUNK))

    @staticmethod
    def _tree(top):
        out = set()
        for dp, dns, fns in os.walk(top):
            for n in dns + fns:
                out.add(os.path.relpath(os.path.join(dp, n), top))
        return out

    def test_m2_manifest_path_traversal_rejected(self):
        _b, _n, base_img, new_img, _e = scenario(seed=72)
        make_full_dir(self.base_dir, base_img)
        make_diff_dir(self.diff_dir, self.base_dir, new_img)
        good = df.load_diff_manifest(self.diff_dir)
        entry = good["partitions"][PART]
        token = "escape-" + os.urandom(6).hex()
        variants = {
            "short_part": ("key", f"../{token}"),
            "short_part_deep": ("key", f"../../../{token}"),
            "short_part_dot": ("key", ".."),
            "fs": ("fs", f"../{token}"),
            "fs_slash": ("fs", "ext4/x"),
            "meta": ("meta", f"../{token}.meta"),
            "meta_abs": ("meta", "/etc/passwd"),
            "meta_type": ("meta", 5),
            "base_bhash": ("base_bhash", f"../../{token}.rzbhash"),
            "seekidx": ("seekidx", f"../{token}.rzidx"),
            "data_prefix": ("data_prefix", f"../{token}-rzdiff-img.zst."),
            "data_prefix_abs": ("data_prefix", "/tmp/x."),
            "base_seekidx": ("base_seekidx", ".."),
            "base_image_prefix": ("base_image_prefix", f"../{token}.ext4-ptcl-img.zst."),
            "compression": ("compression", "../zstd"),
            "suggested_dir_name": ("suggested", f"../../{token}"),
        }
        before = self._tree(self.root)
        merged = os.path.join(self.root, "merged")
        for name, (key, value) in variants.items():
            with self.subTest(name=name):
                m = copy.deepcopy(good)
                part = PART
                if key == "key":
                    m["partitions"] = {value: dict(entry)}
                    part = value
                elif key == "suggested":
                    m["base_image"]["suggested_dir_name"] = value
                else:
                    m["partitions"][PART][key] = value
                df.write_diff_manifest(self.diff_dir, m)
                with self.assertRaises(df.DiffFormatError):
                    ds.merge_diff_image(self.diff_dir, merged, level=1, frame_size=65536)
                out = io.BytesIO()
                with self.assertRaises(df.DiffFormatError):
                    ds.synthesize_partition_stream(self.diff_dir, None, part, out)
                self.assertEqual(out.getvalue(), b"")
                # ZERO files created anywhere (incl. above the test root)
                self.assertEqual(self._tree(self.root), before)
                for up in (os.path.dirname(self.root), os.path.dirname(os.path.dirname(self.root))):
                    self.assertEqual(glob.glob(os.path.join(glob.escape(up), token + "*")), [])
        # CLI: exit code 5 (corrupt differential), nothing written
        proc = run_py(
            [os.path.join(PKG_DIR, "diff_synthesizer.py"), "merge", "--diff-dir", self.diff_dir,
             "--output-dir", merged, "--level", "1"]
        )
        self.assertEqual(proc.returncode, ds.EXIT_DIFF_CORRUPTION, proc.stderr)
        self.assertEqual(self._tree(self.root), before)
        # A CLI --partition with traversal is rejected as well
        df.write_diff_manifest(self.diff_dir, good)
        proc = run_py(
            [os.path.join(PKG_DIR, "diff_synthesizer.py"), "stream", "--diff-dir", self.diff_dir,
             "--partition", "../" + PART]
        )
        self.assertEqual(proc.returncode, ds.EXIT_DIFF_CORRUPTION, proc.stderr)
        self.assertEqual(proc.stdout, b"")
        self.assertEqual(self._synth(), new_img)  # the valid manifest still works

    def test_m2_symlinks_never_followed(self):
        _b, _n, base_img, new_img, _e = scenario(seed=73)
        make_full_dir(self.base_dir, base_img)
        make_diff_dir(self.diff_dir, self.base_dir, new_img)
        token = b"TOP-SECRET-" + os.urandom(8).hex().encode()
        with tempfile.TemporaryDirectory() as secret_dir:
            secret = os.path.join(secret_dir, "shadow")
            with open(secret, "wb") as f:
                f.write(token)
            os.symlink(secret, os.path.join(self.diff_dir, "Info-secret.txt"))
            os.symlink(secret_dir, os.path.join(self.diff_dir, "linked-dir"))
            sub = os.path.join(self.diff_dir, "efi-nvram")
            os.makedirs(sub)
            with open(os.path.join(sub, "real.txt"), "w") as f:
                f.write("ok\n")
            os.symlink(secret, os.path.join(sub, "inner-link"))
            expected_skipped = [
                os.path.join(self.diff_dir, "Info-secret.txt"),
                os.path.join(self.diff_dir, "linked-dir"),
                os.path.join(sub, "inner-link"),
            ]
            if hasattr(os, "mkfifo"):
                os.mkfifo(os.path.join(self.diff_dir, "a-fifo"))
                expected_skipped.append(os.path.join(self.diff_dir, "a-fifo"))
            out_dir = os.path.join(self.root, "merged")
            result = ds.merge_diff_image(self.diff_dir, out_dir, level=1, frame_size=65536)
            self.assertEqual(sorted(result["skipped"]), sorted(expected_skipped))
            names = os.listdir(out_dir)
            for n in ("Info-secret.txt", "linked-dir", "a-fifo"):
                self.assertNotIn(n, names)
            self.assertEqual(os.listdir(os.path.join(out_dir, "efi-nvram")), ["real.txt"])
            for dp, dns, fns in os.walk(out_dir):
                for n in dns + fns:
                    p = os.path.join(dp, n)
                    self.assertFalse(os.path.islink(p), p)
                    if os.path.isfile(p):
                        with open(p, "rb") as f:
                            self.assertNotIn(token, f.read(), p)
            with sc.SeekableSplitStream(
                bh.list_split_files(os.path.join(out_dir, f"{PART}.{CZFS}-ptcl-img.zst.")),
                "zstd", os.path.join(out_dir, f"Info-seekidx-{PART}.rzidx"),
            ) as s:
                self.assertEqual(s.read(), new_img)
            # A manifest-named file that is a symlink leaving its dir is rejected
            meta_p = os.path.join(self.diff_dir, df.get_diff_meta_filename(PART, CZFS))
            outside = os.path.join(secret_dir, "moved.meta")
            shutil.move(meta_p, outside)
            os.symlink(outside, meta_p)
            with self.assertRaises(df.DiffFormatError):
                self._synth()

    def test_low3_malformed_values_exit_5(self):
        _b, _n, base_img, new_img, _e = scenario(seed=76)
        make_full_dir(self.base_dir, base_img)
        make_diff_dir(self.diff_dir, self.base_dir, new_img)
        script = os.path.join(PKG_DIR, "diff_synthesizer.py")
        args = [script, "stream", "--diff-dir", self.diff_dir, "--partition", PART]
        good = df.load_diff_manifest(self.diff_dir)
        bad = copy.deepcopy(good)
        bad["partitions"][PART]["fs"] = 123
        df.write_diff_manifest(self.diff_dir, bad)
        proc = run_py(args)
        self.assertEqual(proc.returncode, ds.EXIT_DIFF_CORRUPTION, proc.stderr)
        self.assertEqual(proc.stdout, b"")
        df.write_diff_manifest(self.diff_dir, good)
        # Crafted meta with block_size = 0 (valid SHA-256 trailer)
        meta_p = os.path.join(self.diff_dir, df.get_diff_meta_filename(PART, CZFS))
        payload = df.load_diff_meta(meta_p).payload_bytes()
        body = bytearray(payload[:-32])
        struct.pack_into("<I", body, 20, 0)  # RZDMETA1 block_size
        with open(meta_p, "wb") as f:
            f.write(df.META_ENVELOPE_RAW + bytes(body) + hashlib.sha256(body).digest())
        with self.assertRaises(ds.DiffCorruptionError):
            self._synth()
        proc = run_py(args)
        self.assertEqual(proc.returncode, ds.EXIT_DIFF_CORRUPTION, proc.stderr)
        self.assertEqual(proc.stdout, b"")

    def test_low7_sequential_delta_trailing_data_rejected(self):
        _b, _n, base_img, new_img, _e = scenario(seed=74)
        make_full_dir(self.base_dir, base_img)
        delta_prefix = os.path.join(self.diff_dir, df.get_diff_data_prefix(PART, CZFS, "zst"))
        for junk in (sc.compress_zstd_frame(b"trailing-chunk-data", 1), b"\x01\x02garbage"):
            with self.subTest(junk=junk[:4]):
                shutil.rmtree(self.diff_dir, ignore_errors=True)
                make_diff_dir(self.diff_dir, self.base_dir, new_img)
                os.remove(os.path.join(self.diff_dir, df.get_diff_seekidx_filename(PART)))
                self.assertEqual(self._synth(), new_img)  # sequential path is fine
                with open(bh.list_split_files(delta_prefix)[-1], "ab") as f:
                    f.write(junk)
                with self.assertRaises(ds.DiffCorruptionError):
                    self._synth()

    def test_low10_empty_stdin_is_error_not_fallback(self):
        _b, _n, base_img, _new, _e = scenario(seed=75)
        make_full_dir(self.base_dir, base_img)
        os.makedirs(self.diff_dir)
        base_bhash = os.path.join(self.base_dir, bh.bhash_index_filename(PART))
        meta = os.path.join(self.diff_dir, "m.meta")
        idx = os.path.join(self.diff_dir, "m.rzidx")
        args = [os.path.join(PKG_DIR, "diff_writer.py"), "--base-bhash", base_bhash,
                "--meta-output", meta, "--index-output", idx]
        for data in (b"", b"partclone"):  # nothing / EOF inside the magic
            with self.subTest(data=data):
                proc = run_py(args, input=data)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertNotEqual(proc.returncode, df.EXIT_FALLBACK_FULL)
                self.assertEqual(proc.stdout, b"")
                self.assertEqual(os.listdir(self.diff_dir), [])
        with self.assertRaisesRegex(bh.PartcloneStreamError, "empty input"):
            dw.write_diff_stream(
                io.BytesIO(b""), io.BytesIO(), base_bhash_path=base_bhash,
                meta_output=meta, index_output=idx,
            )
        self.assertEqual(os.listdir(self.diff_dir), [])

    def test_synth_without_base_seekidx_and_cli_stream(self):
        _b, _n, base_img, new_img, _e = scenario(seed=51)
        make_full_dir(self.base_dir, base_img, fmt="zstd", with_idx=False)
        make_diff_dir(self.diff_dir, self.base_dir, new_img)
        os.remove(os.path.join(self.diff_dir, df.get_diff_seekidx_filename(PART)))
        self.assertEqual(self._synth(), new_img)
        proc = run_py(
            [os.path.join(PKG_DIR, "diff_synthesizer.py"), "stream", "--diff-dir", self.diff_dir,
             "--partition", PART]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, new_img)
        proc = run_py(
            ["-m", "rescuezilla.diff_synthesizer", "stream", "--diff-dir", self.diff_dir,
             "--partition", PART, "--base-dir", self.base_dir],
            env=pkg_env(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, new_img)

    def test_zero_changed_chunks(self):
        _b, _n, base_img, _new, _e = scenario(seed=52)
        make_full_dir(self.base_dir, base_img)
        stats, delta = make_diff_dir(self.diff_dir, self.base_dir, base_img)
        self.assertEqual(stats["changed_chunks"], 0)
        self.assertEqual(stats["delta_bytes"], 0)
        self.assertGreater(len(delta), 0)  # one valid empty zstd frame
        aa = os.path.join(self.diff_dir, df.get_diff_data_prefix(PART, CZFS, "zst") + "aa")
        self.assertGreater(os.path.getsize(aa), 0)
        with open(os.path.join(self.diff_dir, df.get_diff_seekidx_filename(PART)), "rb") as f:
            info = sc.parse_seekidx_bytes(f.read())
        self.assertEqual((info["frame_count"], info["total_uncompressed"]), (1, 0))
        meta = df.load_diff_meta(os.path.join(self.diff_dir, df.get_diff_meta_filename(PART, CZFS)))
        self.assertEqual(meta.changed_chunk_count, 0)
        self.assertEqual(set(meta.delta_chunk_bitmap), {0})
        self.assertEqual(self._synth(), base_img)

    def test_all_chunks_changed(self):
        base, _n, base_img, _new, _e = scenario(seed=53)
        rng = random.Random(530)
        new = {b: rng.randbytes(BS) for b in base}
        new_img = build_ptcl(new, TB, BS, bpcs=64)
        make_full_dir(self.base_dir, base_img)
        stats, _d = make_diff_dir(self.diff_dir, self.base_dir, new_img)
        non_empty = sum(1 for c in range(13) if chunk_used(new, c))
        self.assertEqual((stats["changed_chunks"], stats["unchanged_chunks"]), (non_empty, 0))
        self.assertEqual(self._synth(), new_img)

    def test_incompatible_stream_exits_fallback_full(self):
        _b, new, base_img, _new_img, _e = scenario(seed=54)
        make_full_dir(self.base_dir, base_img)
        base_bhash = os.path.join(self.base_dir, bh.bhash_index_filename(PART))
        rng = random.Random(540)
        variants = {
            "resize": build_ptcl(new, TB + 1, BS, bpcs=64),
            "fs": build_ptcl(new, TB, BS, bpcs=64, fs=b"NTFS"),
            "device_size": build_ptcl(new, TB, BS, bpcs=64, device_size=TB * BS + 512),
            "block_size": build_ptcl({0: b"\x01" * 512}, TB * 8, 512, bpcs=64),
            "dd": rng.randbytes(200000),
        }
        os.makedirs(self.diff_dir)
        meta = os.path.join(self.diff_dir, "m.meta")
        idx = os.path.join(self.diff_dir, "m.rzidx")
        stats_json = os.path.join(self.diff_dir, "s.json")
        args = [os.path.join(PKG_DIR, "diff_writer.py"), "--base-bhash", base_bhash,
                "--meta-output", meta, "--index-output", idx, "--stats-json-output", stats_json,
                "--frame-size", "65536", "--level", "1"]
        for name, data in variants.items():
            with self.subTest(name=name):
                proc = run_py(args, input=data)
                self.assertEqual(proc.returncode, df.EXIT_FALLBACK_FULL, proc.stderr)
                self.assertEqual(proc.stdout, b"")
                self.assertEqual(os.listdir(self.diff_dir), [])
        # Corrupt base .rzbhash also falls back to a full backup
        with open(base_bhash, "r+b") as f:
            f.seek(100)
            f.write(b"\xee")
        proc = run_py(args, input=_new_img)
        self.assertEqual(proc.returncode, df.EXIT_FALLBACK_FULL, proc.stderr)
        self.assertEqual(os.listdir(self.diff_dir), [])

    def test_writer_cli_success_and_corrupt_input(self):
        _b, _n, base_img, new_img, expected = scenario(seed=55)
        make_full_dir(self.base_dir, base_img)
        os.makedirs(self.diff_dir)
        base_bhash = os.path.join(self.base_dir, bh.bhash_index_filename(PART))
        meta = os.path.join(self.diff_dir, df.get_diff_meta_filename(PART, CZFS))
        idx = os.path.join(self.diff_dir, df.get_diff_seekidx_filename(PART))
        stats_json = os.path.join(self.diff_dir, "stats.json")
        hdr_sha = hashlib.sha256(bh.parse_partclone_header(base_img).raw).hexdigest()
        args = ["-m", "rescuezilla.diff_writer", "--base-bhash", base_bhash, "--meta-output", meta,
                "--index-output", idx, "--frame-size", "65536", "--level", "1",
                "--base-header-sha256", hdr_sha, "--stats-json-output", stats_json, "--print-stats"]
        bad = bytearray(new_img)
        bad[-20] ^= 0x01
        proc = run_py(args, input=bytes(bad), env=pkg_env())
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(os.path.exists(meta) or os.path.exists(idx) or os.path.exists(stats_json))
        proc = run_py(args, input=new_img, env=pkg_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(b"[seekable_diff]", proc.stderr)
        self.assertIn(b"changed_chunks=4", proc.stderr)
        self.assertTrue(os.path.exists(stats_json))
        self.assertEqual(glob.glob(os.path.join(self.diff_dir, "*.tmp*")), [])
        write_splits(os.path.join(self.diff_dir, df.get_diff_data_prefix(PART, CZFS, "zst")), proc.stdout, 10**9)
        base_sha = bh.load_bhash_file(base_bhash).file_sha256
        df.write_diff_manifest(self.diff_dir, df.make_manifest(
            base_relative_path="../" + os.path.basename(self.base_dir),
            base_suggested_dir_name=os.path.basename(self.base_dir),
            partitions={PART: df.make_partition_entry(
                short_part=PART, fs=CZFS, compression="zstd", base_bhash_sha256=base_sha)},
        ))
        self.assertEqual(self._synth(), new_img)
        self.assertEqual(df.load_diff_meta(meta).base_header_sha256.hex(), hdr_sha)

    def test_fail_closed_base_mismatch(self):
        _b, _n, base_img, new_img, _e = scenario(seed=56)
        make_full_dir(self.base_dir, base_img)
        make_diff_dir(self.diff_dir, self.base_dir, new_img)
        other_dir = os.path.join(self.root, "other-base")
        _b2, _n2, other_img, _x, _e2 = scenario(seed=57)
        make_full_dir(other_dir, other_img)
        out = io.BytesIO()
        with self.assertRaises(ds.BaseMismatchError):
            ds.synthesize_partition_stream(self.diff_dir, other_dir, PART, out)
        self.assertEqual(out.getvalue(), b"")
        # Wrong .rzbhash swapped into the real base dir
        real = os.path.join(self.base_dir, bh.bhash_index_filename(PART))
        saved = real + ".saved"
        shutil.copy2(real, saved)
        shutil.copy2(os.path.join(other_dir, bh.bhash_index_filename(PART)), real)
        out = io.BytesIO()
        with self.assertRaises(ds.BaseMismatchError):
            ds.synthesize_partition_stream(self.diff_dir, None, PART, out)
        self.assertEqual(out.getvalue(), b"")
        shutil.copy2(saved, real)
        # Right .rzbhash, but the base image splits were replaced by another image
        for p in bh.list_split_files(os.path.join(self.base_dir, f"{PART}.{CZFS}-ptcl-img.zst.")):
            os.remove(p)
        make_full_dir(os.path.join(self.root, "tmp-other"), other_img)
        for p in bh.list_split_files(os.path.join(self.root, "tmp-other", f"{PART}.{CZFS}-ptcl-img.zst.")):
            shutil.copy2(p, self.base_dir)
        os.remove(os.path.join(self.base_dir, f"Info-seekidx-{PART}.rzidx"))
        out = io.BytesIO()
        with self.assertRaises((ds.BaseMismatchError, ds.BaseCorruptionError)):
            ds.synthesize_partition_stream(self.diff_dir, None, PART, out)
        # Manifest tampering
        shutil.rmtree(self.base_dir)
        make_full_dir(self.base_dir, base_img)
        manifest = df.load_diff_manifest(self.diff_dir)
        manifest["partitions"][PART]["base_bhash_sha256"] = "00" * 32
        df.write_diff_manifest(self.diff_dir, manifest)
        out = io.BytesIO()
        with self.assertRaises(ds.BaseMismatchError):
            ds.synthesize_partition_stream(self.diff_dir, None, PART, out)
        self.assertEqual(out.getvalue(), b"")
        with self.assertRaises(FileNotFoundError):
            ds.synthesize_partition_stream(self.diff_dir, os.path.join(self.root, "nope"), PART, out)

    def test_fail_closed_base_header_sha256(self):
        _b, _n, base_img, new_img, _e = scenario(seed=58)
        make_full_dir(self.base_dir, base_img)
        make_diff_dir(self.diff_dir, self.base_dir, new_img, base_header_sha256=b"\x11" * 32)
        out = io.BytesIO()
        with self.assertRaises(ds.BaseMismatchError):
            ds.synthesize_partition_stream(self.diff_dir, None, PART, out)
        self.assertEqual(out.getvalue(), b"")

    def _unchanged_block_offset(self, base_img, base, chunk):
        h = bh.parse_partclone_header(base_img)
        bm = bh.BitmapHelper(base_img[h.header_total_size : h.header_total_size + h.bitmap_bytes], h.bitmap_mode, h.totalblock)
        b = min(x for x in base if chunk * BPC <= x < (chunk + 1) * BPC)
        k = bm.rank(b)
        return h.data_start + k * BS + (k // h.blocks_per_checksum) * 4 + 100

    def test_fail_closed_base_bit_rot_uncompressed_block(self):
        base, _n, base_img, new_img, expected = scenario(seed=59)
        self.assertNotIn(11, expected)
        paths = make_full_dir(self.base_dir, base_img, fmt="uncompressed", split_size=10**9)
        make_diff_dir(self.diff_dir, self.base_dir, new_img)
        self.assertEqual(self._synth(), new_img)
        flip_bit_in_splits(paths, self._unchanged_block_offset(base_img, base, 11), bit=3)
        with self.assertRaisesRegex(ds.BaseCorruptionError, "CRC32"):
            self._synth()
        with self.assertRaisesRegex(ds.BaseCorruptionError, "SHA-256"):
            self._synth(verify_base_crc=False)

    def test_fail_closed_base_bit_rot_compressed_frame(self):
        for fmt, with_idx in (("zstd", True), ("gzip", True), ("zstd", False)):
            with self.subTest(fmt=fmt, idx=with_idx):
                shutil.rmtree(self.base_dir, ignore_errors=True)
                shutil.rmtree(self.diff_dir, ignore_errors=True)
                base, _n, base_img, new_img, _e = scenario(seed=60)
                paths = make_full_dir(self.base_dir, base_img, fmt=fmt, with_idx=True)
                make_diff_dir(self.diff_dir, self.base_dir, new_img, fmt=fmt)
                idx_path = os.path.join(self.base_dir, f"Info-seekidx-{PART}.rzidx")
                with open(idx_path, "rb") as f:
                    entries = sc.parse_seekidx_bytes(f.read())["entries"]
                u_off = self._unchanged_block_offset(base_img, base, 11)
                i = max(j for j in range(len(entries) - 1) if entries[j][1] <= u_off)
                c0, c1 = entries[i][0], entries[i + 1][0]
                flip_bit_in_splits(paths, c0 + (c1 - c0) // 2, bit=5)
                if not with_idx:
                    os.remove(idx_path)
                with self.assertRaises(ds.BaseCorruptionError):
                    self._synth()

    def test_merge_flatten_e2e(self):
        _b, new, base_img, new_img, _e = scenario(seed=61)
        make_full_dir(self.base_dir, base_img, fmt="gzip")
        make_diff_dir(self.diff_dir, self.base_dir, new_img, fmt="zstd")
        out_dir = os.path.join(self.root, "merged")
        result = ds.merge_diff_image(
            self.diff_dir, out_dir, fmt="zstd", level=1, frame_size=65536, threads=2,
            split_size=1000000,
        )
        names = sorted(os.listdir(out_dir))
        self.assertFalse([n for n in names if df.is_diff_artifact_filename(n) or "rzdiff" in n], names)
        for required in ("parts", "clonezilla-img", "Info-img-id.txt", f"Info-seekidx-{PART}.rzidx",
                         bh.bhash_index_filename(PART), f"{PART}.{CZFS}-ptcl-img.zst.aa"):
            self.assertIn(required, names)
        self.assertEqual(result["partitions"][PART]["split_files"][0], f"{PART}.{CZFS}-ptcl-img.zst.aa")
        self.assertEqual(result["skipped"], [])
        self.assertEqual([n for n in os.listdir(self.root) if "rzmerge" in n], [])
        # mkdtemp() staging is 0700; the published dir must get the umask default
        umask = os.umask(0)
        os.umask(umask)
        self.assertEqual(stat.S_IMODE(os.stat(out_dir).st_mode), 0o777 & ~umask)
        with open(os.path.join(out_dir, "parts"), "rb") as f:
            self.assertEqual(f.read(), (PART + "\n").encode())
        splits = bh.list_split_files(os.path.join(out_dir, f"{PART}.{CZFS}-ptcl-img.zst."))
        self.assertGreater(len(splits), 1)
        parts_data = []
        for p in splits:
            with open(p, "rb") as f:
                parts_data.append(f.read())
        blob = b"".join(parts_data)
        self.assertEqual(sc.decompress_zstd_frame(blob), new_img)
        if shutil.which("zstd"):
            proc = subprocess.run(["zstd", "-d", "-c"], input=blob, stdout=subprocess.PIPE, check=True)
            self.assertEqual(proc.stdout, new_img)
        s = sc.SeekableSplitStream(splits, "zstd", os.path.join(out_dir, f"Info-seekidx-{PART}.rzidx"))
        self.assertEqual(s.mode, "index")
        self.assertEqual(s.read(), new_img)
        s.close()
        merged_bhash = bh.load_bhash_file(os.path.join(out_dir, bh.bhash_index_filename(PART)))
        self.assertEqual(merged_bhash.digests, ref_digests(new, TB, BS, CHUNK))
        # Refuses a non-empty output dir and leaves no staging behind
        with self.assertRaises(FileExistsError):
            ds.merge_diff_image(self.diff_dir, out_dir)
        self.assertEqual([n for n in os.listdir(self.root) if "rzmerge" in n], [])
        # CLI merge via `python3 -m rescuezilla.diff_synthesizer`
        out2 = os.path.join(self.root, "merged-cli")
        proc = run_py(
            ["-m", "rescuezilla.diff_synthesizer", "merge", "--diff-dir", self.diff_dir,
             "--output-dir", out2, "--level", "1"],
            env=pkg_env(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        s = sc.SeekableSplitStream(
            bh.list_split_files(os.path.join(out2, f"{PART}.{CZFS}-ptcl-img.zst.")),
            "zstd", os.path.join(out2, f"Info-seekidx-{PART}.rzidx"),
        )
        self.assertEqual(s.read(), new_img)
        s.close()

    def test_merge_failure_cleans_staging(self):
        _b, _n, base_img, new_img, _e = scenario(seed=62)
        paths = make_full_dir(self.base_dir, base_img, fmt="uncompressed", split_size=10**9)
        make_diff_dir(self.diff_dir, self.base_dir, new_img)
        base = _b
        flip_bit_in_splits(paths, self._unchanged_block_offset(base_img, base, 11))
        out_dir = os.path.join(self.root, "merged")
        with self.assertRaises(ds.BaseCorruptionError):
            ds.merge_diff_image(self.diff_dir, out_dir, level=1, frame_size=65536)
        self.assertFalse(os.path.exists(out_dir))
        self.assertEqual([n for n in os.listdir(self.root) if "rzmerge" in n], [])


class DiffFormatTest(unittest.TestCase):
    def _meta(self):
        return df.DiffMeta(
            chunk_size=MiB, block_size=4096, totalblock=3000, device_size=3000 * 4096,
            num_chunks=12, base_bhash_sha256=b"\x01" * 32, base_header_sha256=df.ZERO_SHA256,
            raw_partclone_header=b"H" * 110, raw_partclone_bitmap=b"B" * 375,
            delta_chunk_bitmap=bytes([0b00100001, 0b00001000]),
            delta_chunk_offsets=[(0, 0, 8192), (5, 8192, 4096), (11, 12288, 40960)],
        )

    def test_meta_roundtrip_envelopes_and_bitflips(self):
        meta = self._meta()
        for compress in (True, False):
            blob = meta.serialize(compress=compress)
            self.assertIn(blob[:8], (df.META_ENVELOPE_ZSTD, df.META_ENVELOPE_RAW))
            if not compress:
                self.assertEqual(blob[:8], df.META_ENVELOPE_RAW)
                self.assertEqual(blob[8:16], df.META_MAGIC)
            parsed = df.parse_diff_meta(blob)
            self.assertEqual(parsed, meta)
            self.assertEqual(parsed.delta_stream_size, 12288 + 40960)
            offsets = {9, 20, 60, 140, len(blob) // 2, len(blob) - 1}
            for off in sorted(o for o in offsets if o < len(blob)):
                bad = bytearray(blob)
                bad[off] ^= 0x04
                with self.subTest(compress=compress, off=off), self.assertRaises(df.DiffFormatError):
                    df.parse_diff_meta(bytes(bad))
        with mock.patch.object(sc, "_LIBZSTD", None), mock.patch.object(sc, "_py_zstd", None), \
                mock.patch.object(sc, "_zstandard_pkg", None):
            self.assertEqual(meta.serialize()[:8], df.META_ENVELOPE_RAW)
        bad = self._meta()
        bad.delta_chunk_offsets = [(0, 0, 8192), (5, 9999, 4096), (11, 12288, 40960)]
        with self.assertRaises(df.DiffFormatError):
            bad.serialize()
        bad = self._meta()
        bad.delta_chunk_bitmap = bytes([0b00100001, 0b00011000])
        with self.assertRaises(df.DiffFormatError):
            bad.serialize()

    def test_m3_packed_offset_table(self):
        entries = [(0, 0, 8192), (5, 8192, 4096), (11, 12288, 40960)]
        t = df.DeltaOffsetTable.from_entries(entries)
        self.assertEqual(len(t), 3)
        self.assertEqual(len(t.packed), 3 * df.OFFSET_ENTRY_SIZE)
        self.assertEqual(list(t), entries)
        self.assertEqual(t, entries)
        self.assertEqual(t[-1], entries[-1])
        with self.assertRaises(IndexError):
            t[3]
        for c, off, ln in entries:
            self.assertEqual(t.find_chunk(c), (off, ln))
        for c in (1, 4, 6, 10, 12, 10**9):
            self.assertIsNone(t.find_chunk(c))
        self.assertIsNone(df.DeltaOffsetTable().find_chunk(0))
        with self.assertRaises(df.DiffFormatError):
            df.DeltaOffsetTable(b"\x00" * 25)
        big = df.DeltaOffsetTable.from_entries((c * 3, c * 4096, 4096) for c in range(5000))
        for c in (0, 3, 2997, 14997):
            self.assertEqual(big.find_chunk(c), ((c // 3) * 4096, 4096))
        self.assertIsNone(big.find_chunk(14998))
        meta = self._meta()
        self.assertIsInstance(meta.delta_chunk_offsets, df.DeltaOffsetTable)
        self.assertIs(meta.offsets, meta.delta_chunk_offsets)
        self.assertEqual(meta.offsets_count(), 3)
        self.assertEqual(meta.get_offset(1), entries[1])
        self.assertEqual(list(meta.iter_offsets()), entries)
        self.assertEqual(meta.find_chunk(11), (12288, 40960))
        self.assertIsNone(meta.find_chunk(1))
        parsed = df.parse_diff_meta(meta.serialize(compress=False))
        self.assertIsInstance(parsed.delta_chunk_offsets, df.DeltaOffsetTable)
        self.assertEqual(bytes(parsed.offsets.packed), bytes(t.packed))

    def test_low3_zero_geometry_is_format_error(self):
        good = self._meta().payload_bytes()
        for name, off in (("chunk_size", 16), ("block_size", 20)):
            with self.subTest(field=name):
                m = self._meta()
                setattr(m, name, 0)
                with self.assertRaises(df.DiffFormatError):
                    m.validate()
                body = bytearray(good[:-32])
                struct.pack_into("<I", body, off, 0)
                blob = df.META_ENVELOPE_RAW + bytes(body) + hashlib.sha256(body).digest()
                with self.assertRaises(df.DiffFormatError):
                    df.parse_diff_meta(blob)

    @unittest.skipUnless(sc.has_native_zstd(), "needs a zstd backend")
    def test_low4_meta_zip_bomb_is_bounded(self):
        bomb = sc.compress_zstd_frame(b"\x00" * (4 * MiB), 3)
        self.assertLess(len(bomb), 4096)
        with self.assertRaises(sc.StreamDecompressError):
            sc.decompress_zstd_frame(bomb, max_output=MiB)
        self.assertEqual(len(sc.decompress_zstd_frame(bomb, max_output=4 * MiB)), 4 * MiB)
        with self.assertRaises(sc.StreamDecompressError):
            sc.decompress_zstd_frame(bomb, 4 * MiB, max_output=MiB)
        self.assertGreater(df.MAX_META_UNCOMPRESSED_BYTES, 4 * MiB)
        with mock.patch.object(df, "MAX_META_UNCOMPRESSED_BYTES", MiB):
            with self.assertRaisesRegex(df.DiffFormatError, "zstd envelope"):
                df.parse_diff_meta(df.META_ENVELOPE_ZSTD + bomb)
            with self.assertRaises(df.DiffFormatError):
                df.parse_diff_meta(df.META_ENVELOPE_RAW + b"\x00" * (MiB + 1))
            with tempfile.TemporaryDirectory() as td:
                p = os.path.join(td, "x.meta")
                with open(p, "wb") as f:
                    f.write(df.META_ENVELOPE_RAW + b"\x00" * (MiB + 100))
                with self.assertRaisesRegex(df.DiffFormatError, "too large"):
                    df.load_diff_meta(p)
        # A normal meta still round-trips through the bounded decoder
        meta = self._meta()
        self.assertEqual(df.parse_diff_meta(meta.serialize(compress=True)), meta)

    def test_m2_validators(self):
        for ok in ("sda1", "nvme0n1p2", "vg-lv_1", "md127", "ext4", "mmcblk0p1"):
            self.assertEqual(df.validate_safe_identifier(ok, "x"), ok)
        for bad in ("", ".", "..", "../x", "a/b", "a..b", "a\\b", "sda1\x00", "sda 1", "x\n", None, 5):
            with self.subTest(identifier=bad), self.assertRaises(df.DiffFormatError):
                df.validate_safe_identifier(bad, "x")
        for ok in ("sda1.ext4-rzdiff.meta", "Info-bhash-sda1.rzbhash", "sda1.ext4-rzdiff-img.zst."):
            self.assertEqual(df.validate_safe_basename(ok, "x"), ok)
        for bad in ("", ".", "..", "../x", "/etc/passwd", "a/b", "a\\b", "x\x00", None, 3):
            with self.subTest(basename=bad), self.assertRaises(df.DiffFormatError):
                df.validate_safe_basename(bad, "x")
        with tempfile.TemporaryDirectory() as td:
            sub = os.path.join(td, "sub")
            os.makedirs(sub)
            inside = os.path.join(sub, "f")
            self.assertEqual(df.assert_path_within(inside, sub), os.path.realpath(inside))
            for bad in (os.path.join(sub, "..", "f"), td, "/etc/passwd", sub + "-sibling/f"):
                with self.subTest(path=bad), self.assertRaises(df.DiffFormatError):
                    df.assert_path_within(bad, sub)
            with self.assertRaises(df.DiffFormatError):
                df.assert_path_within(sub, sub)
            df.assert_path_within(sub, sub, allow_equal=True)
            os.symlink(td, os.path.join(sub, "lnk"))
            with self.assertRaises(df.DiffFormatError):
                df.assert_path_within(os.path.join(sub, "lnk", "f"), sub)
        entry = df.make_partition_entry(
            short_part="sda1", fs="ext4", compression="zstd", base_bhash_sha256=b"\x01" * 32
        )
        self.assertEqual(df.validate_partition_entry("sda1", entry)["meta"], "sda1.ext4-rzdiff.meta")
        for key, value in (("base_bhash_sha256", "zz"), ("base_bhash_sha256", "00"), ("compression", "lz4")):
            bad = dict(entry, **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(df.DiffFormatError):
                df.validate_partition_entry("sda1", bad)

    def test_naming_manifest_and_resolve_base(self):
        self.assertEqual(df.get_diff_meta_filename("sda1", "ntfs"), "sda1.ntfs-rzdiff.meta")
        self.assertEqual(df.get_diff_data_prefix("sda1", "ntfs", "zst"), "sda1.ntfs-rzdiff-img.zst.")
        self.assertEqual(df.get_diff_seekidx_filename("sda1"), "Info-seekidx-sda1.rzdiff.rzidx")
        for name in ("sda1.ntfs-rzdiff.meta", "sda1.ntfs-rzdiff-img.zst.aa", "Info-seekidx-sda1.rzdiff.rzidx"):
            self.assertFalse(fnmatch.fnmatch(name, "*-ptcl-img.*"), name)
            self.assertTrue(df.is_diff_artifact_filename(name))
        self.assertFalse(df.is_diff_artifact_filename("Info-seekidx-sda1.rzidx"))
        with tempfile.TemporaryDirectory() as td:
            base = os.path.join(td, "base-img")
            diff = os.path.join(td, "diff-img")
            os.makedirs(base)
            os.makedirs(diff)
            self.assertIsNone(df.load_diff_manifest(diff))
            m = df.make_manifest(base_relative_path="../missing", base_suggested_dir_name="base-img", partitions={})
            df.write_diff_manifest(diff, m)
            self.assertEqual(df.load_diff_manifest(diff), m)
            self.assertEqual(df.resolve_base_dir(diff, m), df.Path(diff).parent / "base-img")
            m["base_image"]["relative_path"] = "../base-img"
            self.assertEqual(os.path.realpath(df.resolve_base_dir(diff, m)), os.path.realpath(base))
            self.assertEqual(df.resolve_base_dir(diff, m, explicit_base_dir=diff), df.Path(diff))
            with self.assertRaises(FileNotFoundError):
                df.resolve_base_dir(diff, m, explicit_base_dir=os.path.join(td, "nope"))
            m["base_image"] = {"relative_path": "../x", "suggested_dir_name": "y"}
            with self.assertRaises(FileNotFoundError):
                df.resolve_base_dir(diff, m)
            df.write_diff_readme(diff, "base-img")
            with open(os.path.join(diff, df.DIFF_README_FILENAME), encoding="utf-8") as f:
                text = f.read()
            self.assertIn("base-img", text)
            self.assertIn("差異備份", text)
            with open(os.path.join(diff, df.DIFF_MANIFEST_FILENAME), "w") as f:
                f.write("{not json")
            with self.assertRaises(df.DiffFormatError):
                df.load_diff_manifest(diff)


class ImportCompatTest(unittest.TestCase):
    def test_toplevel_module_identity(self):
        self.assertIs(ds._bh, bh)
        self.assertIs(dw._df, df)
        self.assertIs(ds.SeekableSplitStream, sc.SeekableSplitStream)

    def test_package_imports_and_module_cli(self):
        code = (
            "from rescuezilla import block_hash, diff_format, diff_writer, diff_synthesizer, "
            "seekable_compressor\n"
            "assert diff_synthesizer._bh is block_hash\n"
            "assert diff_format._sc is seekable_compressor\n"
            "assert seekable_compressor._import_block_hash() is block_hash\n"
        )
        proc = run_py(["-c", code], env=pkg_env(), cwd=tempfile.gettempdir())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for mod in ("block_hash", "diff_writer", "diff_synthesizer"):
            proc = run_py(["-m", "rescuezilla." + mod, "--help"], env=pkg_env())
            self.assertEqual(proc.returncode, 0, (mod, proc.stderr))

    def test_n_m1_meta_limit_triggers_fallback_42_before_output(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            base_dir = os.path.join(td, "base")
            diff_dir = os.path.join(td, "diff")
            os.makedirs(diff_dir)
            _b, _n, base_img, new_img, _e = scenario(seed=91)
            make_full_dir(base_dir, base_img)
            base_bhash = os.path.join(base_dir, bh.bhash_index_filename(PART))
            meta_p = os.path.join(diff_dir, df.get_diff_meta_filename(PART, CZFS))
            idx_p = os.path.join(diff_dir, df.get_diff_seekidx_filename(PART))
            out = io.BytesIO()
            with patch.object(df, "MAX_META_UNCOMPRESSED_BYTES", 256):
                with self.assertRaises(dw.FallbackFullError):
                    dw.write_diff_stream(
                        io.BytesIO(new_img),
                        out,
                        base_bhash_path=base_bhash,
                        meta_output=meta_p,
                        index_output=idx_p,
                    )
            self.assertEqual(out.getvalue(), b"")
            self.assertEqual(os.listdir(diff_dir), [])
            # Defense-in-depth inside DiffMeta.payload_bytes() as well
            hdr = bh.parse_partclone_header(new_img)
            _cs, _bpc, n_chunks = bh.compute_chunk_geometry(hdr.block_size, hdr.totalblock, CHUNK)
            with patch.object(df, "MAX_META_UNCOMPRESSED_BYTES", 256):
                meta = df.DiffMeta(
                    chunk_size=CHUNK,
                    block_size=hdr.block_size,
                    totalblock=hdr.totalblock,
                    device_size=hdr.device_size,
                    num_chunks=n_chunks,
                    base_bhash_sha256=b"\x01" * 32,
                    base_header_sha256=b"\x00" * 32,
                    raw_partclone_header=hdr.raw,
                    raw_partclone_bitmap=b"\x00" * hdr.bitmap_bytes,
                    delta_chunk_bitmap=b"\x00" * ((n_chunks + 7) // 8),
                    delta_chunk_offsets=df.DeltaOffsetTable(),
                )
                with self.assertRaises(df.DiffFormatError):
                    meta.payload_bytes()


if __name__ == "__main__":
    unittest.main()
