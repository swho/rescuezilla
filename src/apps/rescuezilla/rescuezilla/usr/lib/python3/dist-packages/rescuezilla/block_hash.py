#!/usr/bin/env python3
# ----------------------------------------------------------------------
#   Copyright (C) 2026 Rescuezilla Seekable-Index Community Fork Contributors
#   Derived from Rescuezilla (Copyright (C) 2019-2025 Rescuezilla.com)
#   Added 2026-11-28 (Community Fork): SeekZilla Phase 1 block-hash index
#   (.rzbhash) for Partclone v0002 images (GPL-3.0-or-later).
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
"""Partclone v0002 parsing and the `.rzbhash` block-hash index.

The `.rzbhash` file stores one SHA-256 digest per fixed-size *logical* chunk of
the partition (default 1 MiB). A chunk digest covers the canonical BM_BIT
bitmap slice of the chunk plus the bytes of its used blocks, so it is
independent of where those blocks land in the Partclone data stream (inserting
a block near the start of the partition does not change later digests).

Usable both as `import block_hash` (package dir on `sys.path`) and as
`from rescuezilla import block_hash` / `python3 -m rescuezilla.block_hash`.
"""

import argparse
import array
import glob
import hashlib
import os
import re
import struct
import sys
import zlib
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple, Union

# ----------------------------------------------------------------------
# Partclone v0002 constants
# ----------------------------------------------------------------------

PARTCLONE_MAGIC = b"partclone-image\x00"
PARTCLONE_VERSION_V2 = b"0002"
PARTCLONE_ENDIANESS = 0xC0DE

CSM_NONE = 0x00
CSM_CRC32 = 0x20

BM_NONE = 0x00
BM_BIT = 0x01
BM_BYTE = 0x08

PARTCLONE_FIXED_HEADER_SIZE = 88
PARTCLONE_MIN_FEATURE_SIZE = 18
PARTCLONE_CRC_SIZE = 4

# image_head_v2 (36) + file_system_info_v2 (52), packed little-endian
_PTCL_HEAD_STRUCT = struct.Struct("<16s14s4sH16sQQQQI")
# image_options_v2 (first 18 bytes of the feature area)
_PTCL_OPTS_STRUCT = struct.Struct("<IHHHHIBB")
_U32 = struct.Struct("<I")

_MASK32 = 0xFFFFFFFF

# ----------------------------------------------------------------------
# .rzbhash constants
# ----------------------------------------------------------------------

DEFAULT_BHASH_CHUNK_SIZE = 1048576
EMPTY_CHUNK_DIGEST = b"\x00" * 32
DIGEST_SIZE = 32

BHASH_MAGIC = b"RZBHASH1"
BHASH_VERSION = 1
BHASH_ALGO_SHA256 = 1
_BHASH_HEADER_STRUCT = struct.Struct("<8sIIIIQQQ16sI")  # 68 bytes
BHASH_HEADER_SIZE = _BHASH_HEADER_STRUCT.size
BHASH_TRAILER_SIZE = 32

CHUNK_DIGEST_DOMAIN = b"RZBH1"
_CHUNK_DIGEST_PREFIX_STRUCT = struct.Struct("<II")

# Clonezilla split-file compression suffixes handled natively
COMPRESSION_SUFFIX_TO_FORMAT = {
    "gz": "gzip",
    "zst": "zstd",
    "uncomp": "uncompressed",
}
FORMAT_TO_COMPRESSION_SUFFIX = {v: k for k, v in COMPRESSION_SUFFIX_TO_FORMAT.items()}

# No path separators anywhere: a (manifest-supplied) prefix can never leave its dir.
_PTCL_IMAGE_RE = re.compile(
    r"^(?P<part>[^/\\]+?)\.(?P<fs>[^./\\]+)-ptcl-img\.(?P<comp>[A-Za-z0-9]+)\.aa$"
)
_SPLIT_SUFFIX_RE = re.compile(r"^[a-z]{2,}$")


class PartcloneStreamError(ValueError):
    """Corrupt or truncated Partclone v0002 stream (CRC32/length mismatch)."""


class PartcloneUnsupportedError(ValueError):
    """Valid-looking Partclone image this module cannot index (version/endianness/mode)."""


class BlockHashFormatError(ValueError):
    """Corrupt or invalid `.rzbhash` file."""


class ChunkGeometryError(ValueError):
    """Requested `.rzbhash` chunk size is incompatible with the image geometry."""


def partclone_crc32(data, prev_crc: Optional[int] = None) -> int:
    """Partclone CSM_CRC32 value (zlib CRC-32 without the final XOR).

    `prev_crc` continues a running Partclone checksum (reseed_checksum=0).
    """
    if prev_crc is None:
        return (~zlib.crc32(data)) & _MASK32
    return (~zlib.crc32(data, (~prev_crc) & _MASK32)) & _MASK32


def bhash_index_filename(short_device_node: str) -> str:
    """`Info-bhash-<short_device_node>.rzbhash` (never matches `*-ptcl-img.*`)."""
    return "Info-bhash-" + short_device_node + ".rzbhash"


def normalize_fs_name(fs) -> bytes:
    """Partclone `fs` char[16] -> 16-byte NUL-terminated, zero-padded form."""
    if isinstance(fs, str):
        fs = fs.encode("utf-8")
    fs = bytes(fs).split(b"\x00", 1)[0]
    if len(fs) > 16:
        raise ValueError(f"Filesystem name too long: {fs!r}")
    return fs.ljust(16, b"\x00")


def atomic_write_bytes(path, data) -> None:
    """Atomically write `data`: `<path>.tmp.<pid>` + fsync + os.replace.

    The temp file is unlinked on any error, so `path` is either the old file
    or the complete new file, never a partial one.
    """
    path = os.fspath(path)
    out_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(out_dir, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
        try:
            dir_fd = os.open(out_dir, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ----------------------------------------------------------------------
# Partclone v0002 header
# ----------------------------------------------------------------------


@dataclass
class PartcloneHeader:
    ptc_version: str
    fs: bytes  # normalized 16-byte form
    device_size: int
    totalblock: int
    usedblocks: int
    used_bitmap: int
    block_size: int
    feature_size: int
    image_version: int
    cpu_bits: int
    checksum_mode: int
    checksum_size: int
    blocks_per_checksum: int
    reseed_checksum: int
    bitmap_mode: int
    header_crc: int
    raw: bytes = field(repr=False)

    # Partclone v0002 on-disk layout (partclone.c image_desc_v2 /
    # write_image_bitmap / data loop):
    #   header (88 + feature_size) | header CRC32 (ALWAYS 4)
    #   bitmap (bitmap_bytes)      | bitmap CRC32 (4 iff bitmap_mode != BM_NONE)
    #   data blocks, with a 4-byte CRC32 after every `blocks_per_checksum` used
    #   blocks + the final partial group (only when checksum_mode == CSM_CRC32).
    # The header/bitmap CRCs do NOT depend on checksum_mode.

    @property
    def fs_str(self) -> str:
        return self.fs.split(b"\x00", 1)[0].decode("utf-8", errors="replace")

    @property
    def checksum_enabled(self) -> bool:
        """True when the data area carries per-group CRC32s."""
        return (
            self.checksum_mode == CSM_CRC32
            and self.checksum_size == PARTCLONE_CRC_SIZE
            and self.blocks_per_checksum > 0
        )

    @property
    def header_crc_size(self) -> int:
        return PARTCLONE_CRC_SIZE

    @property
    def bitmap_crc_size(self) -> int:
        return PARTCLONE_CRC_SIZE if self.bitmap_mode != BM_NONE else 0

    @property
    def data_crc_size(self) -> int:
        return PARTCLONE_CRC_SIZE if self.checksum_enabled else 0

    @property
    def header_total_size(self) -> int:
        return PARTCLONE_FIXED_HEADER_SIZE + self.feature_size + self.header_crc_size

    @property
    def bitmap_bytes(self) -> int:
        if self.bitmap_mode == BM_BIT:
            return (self.totalblock + 7) // 8
        if self.bitmap_mode == BM_BYTE:
            return self.totalblock
        return 0

    @property
    def data_start(self) -> int:
        return self.header_total_size + self.bitmap_bytes + self.bitmap_crc_size

    def data_area_size(self, used_blocks: int) -> int:
        size = used_blocks * self.block_size
        if self.checksum_enabled and used_blocks:
            size += (
                (used_blocks + self.blocks_per_checksum - 1) // self.blocks_per_checksum
            ) * self.data_crc_size
        return size


def partclone_header_size(prefix: bytes) -> int:
    """Total header size (incl. the always-present header CRC32) from >= 100 bytes."""
    if len(prefix) < PARTCLONE_FIXED_HEADER_SIZE + 12:
        raise PartcloneStreamError("Partclone header is truncated")
    if prefix[:16] != PARTCLONE_MAGIC:
        raise PartcloneUnsupportedError("Not a Partclone image (bad magic)")
    version = bytes(prefix[30:34])
    if version != PARTCLONE_VERSION_V2:
        raise PartcloneUnsupportedError(
            f"Unsupported Partclone image version {version!r} (need 0002)"
        )
    (endianess,) = struct.unpack_from("<H", prefix, 34)
    if endianess != PARTCLONE_ENDIANESS:
        raise PartcloneUnsupportedError(
            f"Unsupported Partclone endianness marker 0x{endianess:04x}"
        )
    (feature_size,) = _U32.unpack_from(prefix, PARTCLONE_FIXED_HEADER_SIZE)
    if feature_size < PARTCLONE_MIN_FEATURE_SIZE or feature_size > 65536:
        raise PartcloneStreamError(f"Invalid Partclone feature_size {feature_size}")
    # image_desc_v2 always ends with a header CRC32, whatever checksum_mode is.
    return PARTCLONE_FIXED_HEADER_SIZE + feature_size + PARTCLONE_CRC_SIZE


def parse_partclone_header(raw: bytes, verify_crc: bool = True) -> PartcloneHeader:
    """Parse (and CRC32-verify) a Partclone v0002 image header."""
    total = partclone_header_size(raw)
    if len(raw) < total:
        raise PartcloneStreamError("Partclone header is truncated")
    raw = bytes(raw[:total])
    (
        _magic,
        ptc_version,
        _version,
        _endianess,
        fs,
        device_size,
        totalblock,
        usedblocks,
        used_bitmap,
        block_size,
    ) = _PTCL_HEAD_STRUCT.unpack_from(raw, 0)
    (
        feature_size,
        image_version,
        cpu_bits,
        checksum_mode,
        checksum_size,
        blocks_per_checksum,
        reseed_checksum,
        bitmap_mode,
    ) = _PTCL_OPTS_STRUCT.unpack_from(raw, PARTCLONE_FIXED_HEADER_SIZE)

    if checksum_mode not in (CSM_NONE, CSM_CRC32):
        raise PartcloneUnsupportedError(
            f"Unsupported Partclone checksum_mode 0x{checksum_mode:02x}"
        )
    if checksum_mode == CSM_CRC32 and checksum_size != 4:
        raise PartcloneUnsupportedError(
            f"Unsupported Partclone CRC32 checksum_size {checksum_size}"
        )
    if bitmap_mode not in (BM_NONE, BM_BIT, BM_BYTE):
        raise PartcloneUnsupportedError(
            f"Unsupported Partclone bitmap_mode 0x{bitmap_mode:02x}"
        )
    if block_size <= 0:
        raise PartcloneStreamError(f"Invalid Partclone block_size {block_size}")

    # The header CRC32 is always present and always verified (even CSM_NONE).
    crc_off = PARTCLONE_FIXED_HEADER_SIZE + feature_size
    (header_crc,) = _U32.unpack_from(raw, crc_off)
    if verify_crc:
        calc = partclone_crc32(raw[:crc_off])
        if calc != header_crc:
            raise PartcloneStreamError(
                f"Partclone header CRC32 mismatch: stored 0x{header_crc:08x}, "
                f"computed 0x{calc:08x}"
            )
    if checksum_mode == CSM_CRC32 and blocks_per_checksum <= 0:
        # Data CRC grouping undefined: never guess the layout.
        raise PartcloneUnsupportedError(
            "Unsupported Partclone CSM_CRC32 image with blocks_per_checksum 0"
        )

    return PartcloneHeader(
        ptc_version=ptc_version.split(b"\x00", 1)[0].decode("ascii", errors="replace"),
        fs=normalize_fs_name(fs),
        device_size=device_size,
        totalblock=totalblock,
        usedblocks=usedblocks,
        used_bitmap=used_bitmap,
        block_size=block_size,
        feature_size=feature_size,
        image_version=image_version,
        cpu_bits=cpu_bits,
        checksum_mode=checksum_mode,
        checksum_size=checksum_size,
        blocks_per_checksum=blocks_per_checksum,
        reseed_checksum=reseed_checksum,
        bitmap_mode=bitmap_mode,
        header_crc=header_crc,
        raw=raw,
    )


def partclone_bitmap_crc32(
    header: PartcloneHeader, raw_bitmap, bitmap: Optional["BitmapHelper"] = None
) -> int:
    """CRC32 partclone stores after the bitmap (partclone.c write_image_bitmap).

    It always covers the bit-packed bitmap of `(totalblock + 7) // 8` bytes:
    the raw on-disk bytes for BM_BIT, the canonical LSB-first packing of the
    byte map for BM_BYTE (`bitmap.bits` is reused when given).
    """
    if header.bitmap_mode == BM_BIT:
        return partclone_crc32(raw_bitmap)
    if header.bitmap_mode == BM_BYTE:
        if bitmap is not None:
            return partclone_crc32(bitmap.bits)
        return partclone_crc32(_byte_map_to_lsb_bits(raw_bitmap))
    raise PartcloneUnsupportedError("BM_NONE images have no bitmap CRC32")


def verify_partclone_bitmap_crc(
    header: PartcloneHeader,
    raw_bitmap,
    crc_bytes,
    bitmap: Optional["BitmapHelper"] = None,
) -> None:
    """Verify the CRC32 that follows the bitmap (present iff bitmap_mode != BM_NONE)."""
    if header.bitmap_crc_size == 0:
        if len(crc_bytes):
            raise PartcloneStreamError("Unexpected bitmap CRC32 for a BM_NONE image")
        return
    if len(crc_bytes) != header.bitmap_crc_size:
        raise PartcloneStreamError("Partclone bitmap CRC32 is truncated")
    (stored,) = _U32.unpack(bytes(crc_bytes))
    calc = partclone_bitmap_crc32(header, raw_bitmap, bitmap)
    if stored != calc:
        raise PartcloneStreamError(
            f"Partclone bitmap CRC32 mismatch: stored 0x{stored:08x}, "
            f"computed 0x{calc:08x}"
        )


# ----------------------------------------------------------------------
# Bitmap
# ----------------------------------------------------------------------

_SUPERBLOCK_BYTES = 512  # rank table granularity (4096 blocks)
_SUPERBLOCK_SHIFT = 12


def _bytes_to_lsb_bits(flags: bytes) -> bytes:
    """Pack a 0/1 byte-per-block array into LSB-first bits (C-speed, no loops per bit)."""
    n = len(flags)
    pad = (-n) % 8
    if pad:
        flags = flags + b"\x00" * pad
    out_len = len(flags) // 8
    acc = 0
    for i in range(8):
        lane = flags[i::8]
        if any(lane):
            acc |= int.from_bytes(lane, "little") << i
    return acc.to_bytes(out_len, "little")


_NONZERO_TO_ONE = bytes([0] + [1] * 255)


def _byte_map_to_lsb_bits(raw_byte_map) -> bytes:
    """BM_BYTE map (any non-zero byte = used) -> canonical LSB-first bits (tail = 0)."""
    if not isinstance(raw_byte_map, (bytes, bytearray)):
        raw_byte_map = bytes(raw_byte_map)
    return _bytes_to_lsb_bits(raw_byte_map.translate(_NONZERO_TO_ONE))


class BitmapHelper:
    """Canonical BM_BIT (LSB-first) view of a Partclone bitmap with O(1) rank().

    For BM_BIT input whose bits beyond `totalblock` are already 0 (the normal
    case) a passed `bytes` object is reused as `self.bits` without any copy.
    """

    def __init__(self, raw_bitmap, bitmap_mode: int, totalblock: int):
        self.totalblock = int(totalblock)
        nbytes = (self.totalblock + 7) // 8
        rem = self.totalblock % 8
        tail_mask = ((1 << rem) - 1) if rem else 0xFF
        bits: bytes
        if bitmap_mode == BM_BIT:
            if len(raw_bitmap) != nbytes:
                raise PartcloneStreamError(
                    f"BM_BIT bitmap length {len(raw_bitmap)} != {nbytes}"
                )
            if nbytes and raw_bitmap[-1] & ~tail_mask & 0xFF:
                masked = bytearray(raw_bitmap)
                masked[-1] &= tail_mask
                bits = bytes(masked)
                del masked
            elif isinstance(raw_bitmap, bytes):
                bits = raw_bitmap  # zero-copy: already canonical
            else:
                bits = bytes(raw_bitmap)
        elif bitmap_mode == BM_BYTE:
            if len(raw_bitmap) != self.totalblock:
                raise PartcloneStreamError(
                    f"BM_BYTE bitmap length {len(raw_bitmap)} != {self.totalblock}"
                )
            bits = _byte_map_to_lsb_bits(raw_bitmap)
        elif bitmap_mode == BM_NONE:
            bits = (b"\xff" * (nbytes - 1) + bytes([tail_mask])) if nbytes else b""
        else:
            raise PartcloneUnsupportedError(f"Unsupported bitmap_mode {bitmap_mode}")
        self.bits = bits

        n_super = (nbytes + _SUPERBLOCK_BYTES - 1) // _SUPERBLOCK_BYTES
        table_arr = array.array("Q", [0]) * (n_super + 1)
        acc = 0
        for i in range(n_super):
            table_arr[i] = acc
            s = i * _SUPERBLOCK_BYTES
            acc += int.from_bytes(self.bits[s : s + _SUPERBLOCK_BYTES], "little").bit_count()
        table_arr[n_super] = acc
        self._super = table_arr
        self.used_count = acc

    def is_used(self, block_idx: int) -> bool:
        return bool((self.bits[block_idx >> 3] >> (block_idx & 7)) & 1)

    def rank(self, block_idx: int) -> int:
        """Number of used blocks in [0, block_idx) -- O(1) (bounded 512-byte popcount)."""
        if block_idx <= 0:
            return 0
        if block_idx >= self.totalblock:
            return self.used_count
        sb = block_idx >> _SUPERBLOCK_SHIFT
        count = self._super[sb]
        start = sb * _SUPERBLOCK_BYTES
        full = block_idx >> 3
        if full > start:
            count += int.from_bytes(self.bits[start:full], "little").bit_count()
        rem = block_idx & 7
        if rem:
            count += (self.bits[full] & ((1 << rem) - 1)).bit_count()
        return count

    def block_range(self, chunk_idx: int, blocks_per_chunk: int) -> Tuple[int, int]:
        b_start = chunk_idx * blocks_per_chunk
        if chunk_idx < 0 or b_start >= self.totalblock:
            raise IndexError(f"chunk {chunk_idx} out of range")
        return b_start, min(self.totalblock, b_start + blocks_per_chunk)

    def canonical_chunk_bitmap_and_count(
        self, chunk_idx: int, blocks_per_chunk: int
    ) -> Tuple[bytes, int, int]:
        """(bm_slice, used_count, n_blocks) for logical blocks [b_start, b_end).

        `bm_slice` is canonical LSB-first BM_BIT of length (n_blocks + 7) // 8 with
        trailing unused bits masked to 0.
        """
        b_start, b_end = self.block_range(chunk_idx, blocks_per_chunk)
        n_blocks = b_end - b_start
        out_len = (n_blocks + 7) // 8
        if (b_start & 7) == 0:
            sl = self.bits[b_start >> 3 : (b_start >> 3) + out_len]
            rem = n_blocks & 7
            if rem:
                sl = sl[:-1] + bytes([sl[-1] & ((1 << rem) - 1)])
            used = int.from_bytes(sl, "little").bit_count()
            return sl, used, n_blocks
        first = b_start >> 3
        last = (b_end + 7) >> 3
        val = int.from_bytes(self.bits[first:last], "little") >> (b_start & 7)
        val &= (1 << n_blocks) - 1
        return val.to_bytes(out_len, "little"), val.bit_count(), n_blocks


# ----------------------------------------------------------------------
# Chunk geometry & digest
# ----------------------------------------------------------------------


def compute_chunk_geometry(
    block_size: int, totalblock: int, requested_chunk_size: int = DEFAULT_BHASH_CHUNK_SIZE
) -> Tuple[int, int, int]:
    """Return (chunk_size, blocks_per_chunk, num_chunks).

    Raises ChunkGeometryError (a ValueError) for incompatible sizes.
    """
    if block_size <= 0:
        raise ChunkGeometryError(f"Invalid block_size {block_size}")
    if requested_chunk_size <= 0:
        raise ChunkGeometryError(f"Invalid chunk size {requested_chunk_size}")
    chunk_size = max(int(requested_chunk_size), int(block_size))
    if chunk_size % block_size != 0:
        raise ChunkGeometryError(
            f"Chunk size {chunk_size} is not a multiple of block_size {block_size}"
        )
    if chunk_size > _MASK32:
        raise ChunkGeometryError(f"Chunk size {chunk_size} exceeds uint32")
    bpc = chunk_size // block_size
    num_chunks = (int(totalblock) + bpc - 1) // bpc
    return chunk_size, bpc, num_chunks


def new_chunk_hasher(n_blocks: int, bm_slice: bytes):
    """SHA-256 object pre-seeded with the chunk digest prefix (feed used bytes next)."""
    h = hashlib.sha256()
    h.update(CHUNK_DIGEST_DOMAIN)
    h.update(_CHUNK_DIGEST_PREFIX_STRUCT.pack(n_blocks, len(bm_slice)))
    h.update(bm_slice)
    return h


def compute_chunk_digest(
    n_blocks: int, bm_slice: bytes, used_block_bytes, used_count: int
) -> bytes:
    """Canonical chunk digest (EMPTY_CHUNK_DIGEST when the chunk has no used blocks)."""
    if used_count == 0:
        return EMPTY_CHUNK_DIGEST
    h = new_chunk_hasher(n_blocks, bm_slice)
    h.update(used_block_bytes)
    return h.digest()


# ----------------------------------------------------------------------
# .rzbhash container
# ----------------------------------------------------------------------


@dataclass
class BlockHashIndex:
    chunk_size: int
    block_size: int
    totalblock: int
    device_size: int
    num_chunks: int
    fs_name: bytes
    # bytes or bytearray, kept as-is (no copy); read via a memoryview.
    digests: Union[bytes, bytearray] = field(repr=False)
    file_sha256: Optional[bytes] = None
    raw_header_bytes: Optional[bytes] = field(default=None, repr=False)

    def __post_init__(self):
        self.fs_name = normalize_fs_name(self.fs_name)
        if not isinstance(self.digests, (bytes, bytearray)):
            self.digests = bytes(self.digests)
        if len(self.digests) != self.num_chunks * DIGEST_SIZE:
            raise BlockHashFormatError(
                f"digest table length {len(self.digests)} != "
                f"{self.num_chunks} * {DIGEST_SIZE}"
            )
        self._view = memoryview(self.digests)

    @property
    def fs_str(self) -> str:
        return self.fs_name.split(b"\x00", 1)[0].decode("utf-8", errors="replace")

    @property
    def blocks_per_chunk(self) -> int:
        return self.chunk_size // self.block_size

    def get_digest(self, chunk_idx: int) -> bytes:
        if chunk_idx < 0 or chunk_idx >= self.num_chunks:
            raise IndexError(f"chunk {chunk_idx} out of range")
        off = chunk_idx * DIGEST_SIZE
        return bytes(self._view[off : off + DIGEST_SIZE])

    def serialize(self) -> bytes:
        head = bytearray(
            _BHASH_HEADER_STRUCT.pack(
                BHASH_MAGIC,
                BHASH_VERSION,
                BHASH_ALGO_SHA256,
                self.chunk_size,
                self.block_size,
                self.totalblock,
                self.device_size,
                self.num_chunks,
                self.fs_name,
                0,
            )
        )
        _U32.pack_into(head, 64, zlib.crc32(bytes(head[:64])) & _MASK32)
        h = hashlib.sha256()
        h.update(head)
        h.update(self.digests)
        return bytes(head) + self.digests + h.digest()


def parse_bhash_bytes(raw: bytes) -> BlockHashIndex:
    """Parse and fully verify an in-memory `.rzbhash` blob."""
    if len(raw) < BHASH_HEADER_SIZE + BHASH_TRAILER_SIZE:
        raise BlockHashFormatError(".rzbhash file is truncated")
    (
        magic,
        version,
        algo,
        chunk_size,
        block_size,
        totalblock,
        device_size,
        num_chunks,
        fs_name,
        header_crc,
    ) = _BHASH_HEADER_STRUCT.unpack_from(raw, 0)
    if magic != BHASH_MAGIC:
        raise BlockHashFormatError(f"Invalid .rzbhash magic {magic!r}")
    if version != BHASH_VERSION:
        raise BlockHashFormatError(f"Unsupported .rzbhash version {version}")
    if algo != BHASH_ALGO_SHA256:
        raise BlockHashFormatError(f"Unsupported .rzbhash hash_algo {algo}")
    calc_crc = zlib.crc32(raw[:64]) & _MASK32
    if calc_crc != header_crc:
        raise BlockHashFormatError(
            f".rzbhash header CRC32 mismatch: stored 0x{header_crc:08x}, "
            f"computed 0x{calc_crc:08x}"
        )
    expected_len = BHASH_HEADER_SIZE + num_chunks * DIGEST_SIZE + BHASH_TRAILER_SIZE
    if len(raw) != expected_len:
        raise BlockHashFormatError(
            f".rzbhash length mismatch: {len(raw)} != {expected_len}"
        )
    body_end = len(raw) - BHASH_TRAILER_SIZE
    with memoryview(raw) as mv:
        if hashlib.sha256(mv[:body_end]).digest() != bytes(mv[body_end:]):
            raise BlockHashFormatError(".rzbhash SHA-256 trailer mismatch")
        try:
            _cs, _bpc, expect_chunks = compute_chunk_geometry(
                block_size, totalblock, chunk_size
            )
        except ValueError as exc:
            raise BlockHashFormatError(f"Invalid .rzbhash geometry: {exc}") from exc
        if _cs != chunk_size or expect_chunks != num_chunks:
            raise BlockHashFormatError(
                f".rzbhash num_chunks {num_chunks} inconsistent with geometry"
            )
        digests_bytes = bytes(mv[BHASH_HEADER_SIZE:body_end])
        file_sha = hashlib.sha256(mv).digest()
    return BlockHashIndex(
        chunk_size=chunk_size,
        block_size=block_size,
        totalblock=totalblock,
        device_size=device_size,
        num_chunks=num_chunks,
        fs_name=fs_name,
        digests=digests_bytes,
        file_sha256=file_sha,
    )


def load_bhash_file(path) -> BlockHashIndex:
    """Load and fully verify (magic/version/algo/CRC32/length/SHA-256) a `.rzbhash`."""
    with open(path, "rb") as f:
        raw = f.read()
    return parse_bhash_bytes(raw)


def write_bhash_file(path, bhash_index: BlockHashIndex) -> bytes:
    """Atomically write a `.rzbhash`; returns (and stores) the file's SHA-256."""
    blob = bhash_index.serialize()
    atomic_write_bytes(path, blob)
    bhash_index.file_sha256 = hashlib.sha256(blob).digest()
    return bhash_index.file_sha256


# ----------------------------------------------------------------------
# Streaming Partclone v0002 parser
# ----------------------------------------------------------------------


class ChunkSink:
    """Callbacks emitted by PartcloneStreamParser (all optional)."""

    def on_header(self, header: PartcloneHeader) -> None:
        """Called after the header CRC32 is verified, before chunk geometry."""

    def on_bitmap(self, parser: "PartcloneStreamParser") -> None:
        """Called after the bitmap CRC32 is verified, before any data block."""

    def on_chunk_start(
        self, chunk_idx: int, bm_slice: bytes, used_count: int, n_blocks: int
    ) -> None:
        """A non-empty logical chunk begins (its used blocks follow)."""

    def on_chunk_data(self, data: memoryview) -> None:
        """Bytes of used blocks of the current chunk (arbitrary split)."""

    def on_chunk_end(self, chunk_idx: int) -> None:
        """All used blocks of the chunk were delivered."""


_ST_PROBE = 0
_ST_HEADER = 1
_ST_BITMAP = 2
_ST_DATA = 3
_ST_DONE = 4
_ST_PASSTHROUGH = 5


class PartcloneStreamParser:
    """Incremental Partclone v0002 parser verifying header, bitmap and EVERY data CRC32.

    Data CRC rules (CSM_CRC32): after every `blocks_per_checksum` used blocks
    a 4-byte little-endian Partclone CRC32 follows; a trailing partial group
    (used_blocks % blocks_per_checksum != 0) also gets one. With
    `reseed_checksum=1` the running CRC restarts per group; with 0 it runs over
    all data bytes since the start of the data area.
    """

    def __init__(self, sink: Optional[ChunkSink] = None, chunk_size: int = DEFAULT_BHASH_CHUNK_SIZE):
        self.sink = sink if sink is not None else ChunkSink()
        self.requested_chunk_size = int(chunk_size)
        self._state = _ST_PROBE
        self._buf = bytearray()
        self.is_partclone: Optional[bool] = None
        self.header: Optional[PartcloneHeader] = None
        self.raw_header_bytes: Optional[bytes] = None
        self.raw_bitmap_bytes: Optional[bytes] = None
        self.bitmap: Optional[BitmapHelper] = None
        self.chunk_size = 0
        self.blocks_per_chunk = 0
        self.num_chunks = 0
        self.used_blocks = 0
        self.bytes_consumed = 0
        # data state
        self._cs_enabled = False
        self._group_target = 0
        self._group_done = 0
        self._groups_done = 0
        self._z = 0
        self._crc_buf = bytearray()
        self._expect_crc = False
        self._data_total = 0
        self._data_done = 0
        self._chunk = -1
        self._chunk_left = 0

    @property
    def done(self) -> bool:
        return self._state == _ST_DONE

    def set_passthrough(self) -> None:
        self._state = _ST_PASSTHROUGH
        self.is_partclone = False
        self._buf = bytearray()

    def feed(self, data) -> None:
        if self._state == _ST_PASSTHROUGH:
            return
        n = len(data)
        if n == 0:
            return
        self.bytes_consumed += n
        if self._state == _ST_DONE:
            raise PartcloneStreamError(
                f"{n} trailing bytes after end of Partclone data area"
            )
        if self._state in (_ST_PROBE, _ST_HEADER, _ST_BITMAP):
            self._buf += data
            leftover = self._advance_preamble()
            if leftover is None:
                return
            data = leftover
            if not len(data):
                return
            if self._state == _ST_DONE:
                raise PartcloneStreamError(
                    f"{len(data)} trailing bytes after end of Partclone data area"
                )
        self._feed_data(memoryview(data).cast("B"))

    def _advance_preamble(self):
        buf = self._buf
        if self._state == _ST_PROBE:
            if len(buf) < len(PARTCLONE_MAGIC):
                if bytes(buf) != PARTCLONE_MAGIC[: len(buf)]:
                    self.set_passthrough()
                return None
            if bytes(buf[: len(PARTCLONE_MAGIC)]) != PARTCLONE_MAGIC:
                self.set_passthrough()
                return None
            self.is_partclone = True
            self._state = _ST_HEADER
        if self._state == _ST_HEADER:
            if len(buf) < PARTCLONE_FIXED_HEADER_SIZE + 12:
                return None
            total = partclone_header_size(buf)
            if len(buf) < total:
                return None
            header = parse_partclone_header(bytes(buf[:total]), verify_crc=True)
            self.sink.on_header(header)
            self.chunk_size, self.blocks_per_chunk, self.num_chunks = (
                compute_chunk_geometry(
                    header.block_size, header.totalblock, self.requested_chunk_size
                )
            )
            self.header = header
            self.raw_header_bytes = header.raw
            del buf[:total]
            self._state = _ST_BITMAP
        if self._state == _ST_BITMAP:
            header = self.header
            nbm = header.bitmap_bytes
            need = nbm + header.bitmap_crc_size
            if len(buf) < need:
                return None
            raw_bitmap = bytes(buf[:nbm])
            crc_bytes = bytes(buf[nbm:need])
            leftover = bytes(buf[need:])
            # Release the accumulation buffer before building the bitmap view.
            self._buf = bytearray()
            del buf
            bitmap = BitmapHelper(raw_bitmap, header.bitmap_mode, header.totalblock)
            verify_partclone_bitmap_crc(header, raw_bitmap, crc_bytes, bitmap)
            self.raw_bitmap_bytes = raw_bitmap
            self.bitmap = bitmap
            self.used_blocks = bitmap.used_count
            self.sink.on_bitmap(self)
            self._init_data_state()
            return leftover
        return None

    def _init_data_state(self) -> None:
        header = self.header
        self._cs_enabled = header.checksum_enabled
        self._data_total = self.used_blocks * header.block_size
        self._data_done = 0
        self._groups_done = 0
        self._group_done = 0
        self._z = 0
        self._group_target = self._next_group_target()
        if self.used_blocks == 0:
            self._state = _ST_DONE
            return
        self._state = _ST_DATA
        self._advance_chunk()

    def _next_group_target(self) -> int:
        if not self._cs_enabled:
            return 0
        bpcs = self.header.blocks_per_checksum
        remaining = self.used_blocks - self._groups_done * bpcs
        return min(bpcs, max(0, remaining)) * self.header.block_size

    def _advance_chunk(self) -> None:
        c = self._chunk + 1
        bitmap = self.bitmap
        bpc = self.blocks_per_chunk
        while c < self.num_chunks:
            b_start = c * bpc
            b_end = min(bitmap.totalblock, b_start + bpc)
            if bitmap.rank(b_end) - bitmap.rank(b_start):
                bm_slice, used, n_blocks = bitmap.canonical_chunk_bitmap_and_count(c, bpc)
                self._chunk = c
                self._chunk_left = used * self.header.block_size
                self.sink.on_chunk_start(c, bm_slice, used, n_blocks)
                return
            c += 1
        self._chunk = self.num_chunks
        self._chunk_left = 0

    def _feed_data(self, mv: memoryview) -> None:
        n = len(mv)
        pos = 0
        sink = self.sink
        while pos < n:
            if self._state == _ST_DONE:
                raise PartcloneStreamError(
                    f"{n - pos} trailing bytes after end of Partclone data area"
                )
            if self._expect_crc:
                take = min(PARTCLONE_CRC_SIZE - len(self._crc_buf), n - pos)
                self._crc_buf += mv[pos : pos + take]
                pos += take
                if len(self._crc_buf) == PARTCLONE_CRC_SIZE:
                    self._check_group_crc()
                continue
            take = min(n - pos, self._chunk_left)
            if self._cs_enabled:
                take = min(take, self._group_target - self._group_done)
            if take <= 0:
                raise PartcloneStreamError("Partclone data stream state inconsistency")
            piece = mv[pos : pos + take]
            if self._cs_enabled:
                self._z = zlib.crc32(piece, self._z)
                self._group_done += take
            sink.on_chunk_data(piece)
            pos += take
            self._data_done += take
            self._chunk_left -= take
            if self._chunk_left == 0:
                sink.on_chunk_end(self._chunk)
                self._advance_chunk()
            if self._cs_enabled:
                if self._group_done == self._group_target:
                    self._expect_crc = True
            elif self._data_done == self._data_total:
                self._state = _ST_DONE

    def _check_group_crc(self) -> None:
        (stored,) = _U32.unpack(bytes(self._crc_buf))
        calc = (~self._z) & _MASK32
        if stored != calc:
            raise PartcloneStreamError(
                f"Partclone data CRC32 mismatch in checksum group {self._groups_done}: "
                f"stored 0x{stored:08x}, computed 0x{calc:08x}"
            )
        self._groups_done += 1
        self._crc_buf = bytearray()
        self._expect_crc = False
        self._group_done = 0
        if self.header.reseed_checksum:
            self._z = 0
        self._group_target = self._next_group_target()
        if self._data_done == self._data_total:
            self._state = _ST_DONE

    def finish(self) -> None:
        """Assert the stream ended exactly at the end of the Partclone data area."""
        if self._state == _ST_PASSTHROUGH:
            return
        if self._state == _ST_PROBE:
            # Shorter than the magic: not a Partclone image (e.g. tiny dd stream)
            self.set_passthrough()
            return
        if self._state in (_ST_HEADER, _ST_BITMAP):
            raise PartcloneStreamError("Partclone stream truncated inside header/bitmap")
        if self._state != _ST_DONE:
            raise PartcloneStreamError(
                f"Partclone stream truncated: {self._data_done}/{self._data_total} "
                f"data bytes, {self._groups_done} CRC32 groups verified"
            )


class StreamingBlockHashFeed(ChunkSink):
    """Builds a `BlockHashIndex` while uncompressed Partclone bytes stream past.

    Non-Partclone input (e.g. partclone.dd) turns the feed into a cheap no-op and
    `finish()` returns None. Corruption/truncation raises PartcloneStreamError.
    """

    def __init__(self, chunk_size: int = DEFAULT_BHASH_CHUNK_SIZE):
        self._parser = PartcloneStreamParser(self, chunk_size)
        self._digests: Optional[bytearray] = None
        self._hasher = None
        self.unsupported_reason: Optional[str] = None

    @property
    def is_partclone(self) -> Optional[bool]:
        return self._parser.is_partclone

    @property
    def parser(self) -> PartcloneStreamParser:
        return self._parser

    def feed(self, data) -> None:
        try:
            self._parser.feed(data)
        except (PartcloneUnsupportedError, ChunkGeometryError) as exc:
            # The .rzbhash is an optional accelerator: an unsupported image or
            # an incompatible --bhash-chunk-size must never fail the backup.
            self.unsupported_reason = str(exc)
            self._parser.set_passthrough()

    def on_bitmap(self, parser: PartcloneStreamParser) -> None:
        self._digests = bytearray(parser.num_chunks * DIGEST_SIZE)
        # BitmapHelper now owns the bitmap; no sink of ours needs the raw copy.
        parser.raw_bitmap_bytes = b""

    def on_chunk_start(self, chunk_idx, bm_slice, used_count, n_blocks) -> None:
        self._hasher = new_chunk_hasher(n_blocks, bm_slice)

    def on_chunk_data(self, data) -> None:
        self._hasher.update(data)

    def on_chunk_end(self, chunk_idx) -> None:
        off = chunk_idx * DIGEST_SIZE
        self._digests[off : off + DIGEST_SIZE] = self._hasher.digest()
        self._hasher = None

    def finish(self) -> Optional[BlockHashIndex]:
        p = self._parser
        p.finish()
        if not p.is_partclone:
            return None
        h = p.header
        return BlockHashIndex(
            chunk_size=p.chunk_size,
            block_size=h.block_size,
            totalblock=h.totalblock,
            device_size=h.device_size,
            num_chunks=p.num_chunks,
            fs_name=h.fs,
            digests=self._digests,  # handed over without a copy
            raw_header_bytes=p.raw_header_bytes,
        )


def build_bhash_from_stream(
    stream, chunk_size: int = DEFAULT_BHASH_CHUNK_SIZE, read_size: int = 4 * 1024 * 1024
) -> Optional[BlockHashIndex]:
    """Build a BlockHashIndex from any readable uncompressed Partclone stream."""
    feed = StreamingBlockHashFeed(chunk_size=chunk_size)
    while True:
        piece = stream.read(read_size)
        if not piece:
            break
        feed.feed(piece)
    return feed.finish()


# ----------------------------------------------------------------------
# Offline retroactive builder
# ----------------------------------------------------------------------


def _import_seekable_compressor():
    try:
        from . import seekable_compressor  # type: ignore[import-not-found]
    except ImportError:
        import seekable_compressor  # type: ignore[import-not-found,no-redef]
    return seekable_compressor


def parse_ptcl_split_name(name: str) -> Optional[Tuple[str, str, str]]:
    """`sda1.ntfs-ptcl-img.zst.aa` -> ("sda1", "ntfs", "zst"), else None."""
    m = _PTCL_IMAGE_RE.match(name)
    if not m:
        return None
    return m.group("part"), m.group("fs"), m.group("comp")


def list_split_files(prefix_path) -> List[str]:
    """Ordered split files `<prefix>aa`, `<prefix>ab`, ... (GNU split suffixes)."""
    prefix_path = os.fspath(prefix_path)
    out = []
    for p in glob.glob(glob.escape(prefix_path) + "*"):
        suffix = p[len(prefix_path) :]
        if _SPLIT_SUFFIX_RE.match(suffix) and os.path.isfile(p):
            out.append(p)
    out.sort(key=lambda s: (len(s), s))
    return out


@dataclass
class PtclImageSet:
    short_part: str
    fs: str
    comp_suffix: str
    prefix: str  # full path prefix without the split suffix
    split_paths: List[str]

    @property
    def compression(self) -> Optional[str]:
        return COMPRESSION_SUFFIX_TO_FORMAT.get(self.comp_suffix)


def find_ptcl_images(image_dir, include_dd: bool = False) -> List[PtclImageSet]:
    """Scan a Rescuezilla/Clonezilla image dir for `<part>.<fs>-ptcl-img.<comp>.aa..`."""
    image_dir = os.fspath(image_dir)
    found = []
    for name in sorted(os.listdir(image_dir)):
        parsed = parse_ptcl_split_name(name)
        if parsed is None:
            continue
        short_part, fs, comp = parsed
        if fs == "dd" and not include_dd:
            continue
        prefix = os.path.join(image_dir, name[:-2])
        found.append(
            PtclImageSet(
                short_part=short_part,
                fs=fs,
                comp_suffix=comp,
                prefix=prefix,
                split_paths=list_split_files(prefix),
            )
        )
    return found


def build_bhash_from_splits(
    split_paths: Sequence,
    compression: str,
    output_bhash_path,
    chunk_size: int = DEFAULT_BHASH_CHUNK_SIZE,
    seekidx_path=None,
) -> Optional[BlockHashIndex]:
    """Retroactively build `.rzbhash` from existing split image files.

    Decompresses sequentially in-process (libzstd / libdeflate / zlib), verifies
    every Partclone CRC32, and atomically writes `output_bhash_path`. Returns None
    (writing nothing) for non-Partclone images. The split files are only read.
    """
    sc = _import_seekable_compressor()
    if seekidx_path is not None and not os.path.isfile(seekidx_path):
        seekidx_path = None
    try:
        stream = sc.SeekableSplitStream(split_paths, compression, seekidx_path)
    except ValueError as exc:
        if seekidx_path is None:
            raise
        # The .rzidx is only an accelerator: fall back to sequential decoding.
        sys.stderr.write(
            f"block_hash warning: ignoring unusable seek index {seekidx_path}: {exc}\n"
        )
        stream = sc.SeekableSplitStream(split_paths, compression, None)
    try:
        idx = build_bhash_from_stream(stream, chunk_size=chunk_size)
    finally:
        stream.close()
    if idx is None:
        return None
    write_bhash_file(output_bhash_path, idx)
    return idx


def _cmd_build_from_image(args) -> int:
    image_dir = args.image_dir
    if not os.path.isdir(image_dir):
        sys.stderr.write(f"block_hash error: not a directory: {image_dir}\n")
        return 2
    images = find_ptcl_images(image_dir)
    if args.partition:
        images = [i for i in images if i.short_part == args.partition]
        if not images:
            sys.stderr.write(
                f"block_hash error: no Partclone image for partition {args.partition}\n"
            )
            return 2
    rc = 0
    for img in images:
        out_path = os.path.join(image_dir, bhash_index_filename(img.short_part))
        if os.path.exists(out_path) and not args.force:
            print(f"skip {img.short_part}: {out_path} exists (use --force)")
            continue
        if img.compression is None:
            print(f"skip {img.short_part}: unsupported compression '{img.comp_suffix}'")
            continue
        seekidx = os.path.join(image_dir, "Info-seekidx-" + img.short_part + ".rzidx")
        try:
            idx = build_bhash_from_splits(
                img.split_paths,
                img.compression,
                out_path,
                chunk_size=args.chunk_size,
                seekidx_path=seekidx if os.path.isfile(seekidx) else None,
            )
        except (PartcloneStreamError, ValueError, OSError, EOFError) as exc:
            sys.stderr.write(f"block_hash error: {img.short_part}: {exc}\n")
            rc = 1
            continue
        if idx is None:
            print(f"skip {img.short_part}: not a Partclone v0002 image")
        else:
            print(
                f"wrote {out_path} chunks={idx.num_chunks} chunk_size={idx.chunk_size} "
                f"fs={idx.fs_str}"
            )
    return rc


def _cmd_info(args) -> int:
    try:
        idx = load_bhash_file(args.path)
    except (BlockHashFormatError, OSError) as exc:
        sys.stderr.write(f"block_hash error: {exc}\n")
        return 1
    empty = sum(
        1 for c in range(idx.num_chunks) if idx.get_digest(c) == EMPTY_CHUNK_DIGEST
    )
    print(
        f"fs={idx.fs_str} block_size={idx.block_size} totalblock={idx.totalblock} "
        f"device_size={idx.device_size} chunk_size={idx.chunk_size} "
        f"num_chunks={idx.num_chunks} empty_chunks={empty} "
        f"sha256={idx.file_sha256.hex()}"
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rescuezilla Partclone block-hash index (.rzbhash) tool"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_build = sub.add_parser(
        "build-from-image",
        help="Retroactively build Info-bhash-<part>.rzbhash for an existing image dir",
    )
    p_build.add_argument("--image-dir", required=True)
    p_build.add_argument("--partition", default=None)
    p_build.add_argument("--force", action="store_true", default=False)
    p_build.add_argument("--chunk-size", type=int, default=DEFAULT_BHASH_CHUNK_SIZE)
    p_build.set_defaults(func=_cmd_build_from_image)
    p_info = sub.add_parser("info", help="Verify and summarize a .rzbhash file")
    p_info.add_argument("path")
    p_info.set_defaults(func=_cmd_info)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
