# ----------------------------------------------------------------------
#   Copyright (C) 2026 Rescuezilla Seekable-Index Community Fork Contributors
#   Added 2026-11-28 (Community Fork): SeekZilla Phase 3 integration tests
#   (BackupManager full/differential, ClonezillaImage rzdiff parsing,
#   RestoreManager/VerifyManager rzdiff pipelines, base image discovery).
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
import ast
import collections
import hashlib
import io
import json
import os
import random
import shutil
import stat
import struct
import sys
import tempfile
import threading
import types
import unittest
import zlib
from unittest import mock

import backup_manager
import block_hash as bh
import diff_format as df
import diff_synthesizer as ds
import seekable_compressor as sc
from backup_manager import BackupManager, check_diff_eligibility
from utility import Utility

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
M32 = 0xFFFFFFFF
BS = 4096
BPC = (1024 * 1024) // BS  # blocks per 1 MiB chunk (default .rzbhash chunk size)
TB = BPC * 5 + 13


# ----------------------------------------------------------------------
# Synthetic Partclone v0002 images (format rules only, see test_diff_engine)
# ----------------------------------------------------------------------


def _zcrc(data: bytes) -> int:
    return zlib.crc32(data) ^ M32


def build_ptcl(blocks: dict, totalblock: int, block_size: int = BS, bpcs: int = 64) -> bytes:
    used = sorted(blocks)
    head = struct.pack(
        "<16s14s4sH16sQQQQI",
        b"partclone-image\x00",
        b"0.3.27".ljust(14, b"\x00"),
        b"0002",
        0xC0DE,
        b"EXTFS".ljust(16, b"\x00"),
        totalblock * block_size,
        totalblock,
        len(used),
        len(used),
        block_size,
    )
    opts = struct.pack("<IHHHHIBB", 18, 2, 64, 0x20, 4, bpcs, 1, bh.BM_BIT)
    hdr = head + opts
    hdr += struct.pack("<I", _zcrc(hdr))
    bitmap = bytearray((totalblock + 7) // 8)
    for b in used:
        bitmap[b >> 3] |= 1 << (b & 7)
    out = bytearray(hdr + bytes(bitmap))
    out += struct.pack("<I", _zcrc(bytes(bitmap)))
    for gi in range(0, len(used), bpcs):
        group = b"".join(blocks[b] for b in used[gi : gi + bpcs])
        out += group
        out += struct.pack("<I", zlib.crc32(group) ^ M32)
    return bytes(out)


def make_images(seed=7):
    """(base_img, new_img, resized_img): new changes chunks 1 and 3 of base."""
    rng = random.Random(seed)
    used = sorted(b for b in range(TB) if rng.random() < 0.6)
    base = {b: rng.randbytes(BS) for b in used}
    new = dict(base)
    victim = min(b for b in base if BPC <= b < 2 * BPC)
    new[victim] = rng.randbytes(BS)  # modify chunk 1
    free3 = [b for b in range(3 * BPC, 4 * BPC) if b not in base][:5]
    for b in free3:
        new[b] = rng.randbytes(BS)  # allocate blocks in chunk 3
    resized = dict(base)
    return build_ptcl(base, TB), build_ptcl(new, TB), build_ptcl(resized, TB + BPC)


def write_executable(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


FAKE_PARTCLONE_CLONE = """#!{python}
import sys
args = sys.argv[1:]
src = args[args.index("--source") + 1]
with open(src, "rb") as fh:
    data = fh.read()
sys.stdout.buffer.write(data)
sys.stdout.buffer.flush()
"""

# Consumer for restore/verify: stores stdin + argv, fails on an empty stream
# (like partclone does when its source produced no image header).
FAKE_PARTCLONE_CONSUMER = """#!{python}
import os, sys
data = sys.stdin.buffer.read()
out = os.environ.get("RZ_FAKE_CONSUMER_OUT")
if out:
    with open(out, "wb") as fh:
        fh.write(data)
    with open(out + ".argv", "w") as fh:
        fh.write("\\n".join(sys.argv[1:]))
if not data:
    sys.stderr.write("fake partclone: empty input stream\\n")
    sys.exit(1)
sys.exit(0)
"""


def snapshot_tree(root: str) -> dict:
    """{relative path: (mtime_ns, size, mode, sha256)} for every entry below root."""
    snap = {}
    st = os.lstat(root)
    snap["."] = (st.st_mtime_ns, st.st_mode)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames):
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            snap[os.path.relpath(p, root) + "/"] = (st.st_mtime_ns, st.st_mode)
        for name in sorted(filenames):
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            with open(p, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            snap[os.path.relpath(p, root)] = (st.st_mtime_ns, st.st_size, st.st_mode, digest)
    return snap


class _RecordingUi:
    def __init__(self):
        self.errors = []
        self.completed = []

    def set_logger(self, logger):
        pass

    def update_progress_bar(self, fraction):
        pass

    def update_progress_status(self, message):
        pass

    def update_main_statusbar(self, message):
        pass

    def display_status(self, msg1, msg2):
        pass

    def remove_all_main_statusbar(self, context_id):
        pass

    def get_post_task_action(self):
        return "DO_NOTHING"

    def display_error_message(self, summary_message, heading=""):
        self.errors.append(summary_message)

    def completed_operation(self, callable_fn, succeeded, message):
        self.completed.append((succeeded, message))
        return succeeded, message

    def escape_text(self, input):
        return input

    def display_summary_text(self, text_to_display):
        pass


class _ListLogger:
    def __init__(self):
        self.lines = []

    def write(self, message, end="\n"):
        self.lines.append(str(message))

    def close(self):
        pass

    @property
    def text(self):
        return "\n".join(self.lines)


class _FakeBinMixin:
    """Temp dir with fake partclone binaries prepended to PATH."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = self._td.name
        self.bindir = os.path.join(self.root, "bin")
        os.makedirs(self.bindir)
        clone = FAKE_PARTCLONE_CLONE.format(python=sys.executable)
        consumer = FAKE_PARTCLONE_CONSUMER.format(python=sys.executable)
        write_executable(os.path.join(self.bindir, "partclone.ext4"), clone)
        write_executable(os.path.join(self.bindir, "partclone.dd"), clone)
        write_executable(os.path.join(self.bindir, "partclone.chkimg"), consumer)
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "PATH": self.bindir + os.pathsep + os.environ.get("PATH", ""),
                "RESCUEZILLA_SEEKABLE_INDEX": "1",
            },
        )
        self._env_patch.start()
        self.devdir = os.path.join(self.root, "dev")
        os.makedirs(self.devdir)
        self.images_root = os.path.join(self.root, "images")
        os.makedirs(self.images_root)

    def tearDown(self):
        self._env_patch.stop()
        self._td.cleanup()

    def use_restore_consumer(self):
        """partclone.ext4 acts as the restore consumer (reads the stream from stdin)."""
        write_executable(
            os.path.join(self.bindir, "partclone.ext4"),
            FAKE_PARTCLONE_CONSUMER.format(python=sys.executable),
        )

    def write_dev(self, name: str, data: bytes) -> str:
        path = os.path.join(self.devdir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def make_bm(self, dest_dir, diff_base_dir=None, fmt="zstd"):
        os.makedirs(dest_dir, exist_ok=True)
        bm = BackupManager(_RecordingUi(), "test")
        bm.logger = _ListLogger()
        bm.dest_dir = dest_dir
        bm.compression_dict = {"format": fmt, "level": 1}
        bm.is_cloning = False
        bm.is_rescue = False
        bm.requested_stop = False
        bm.summary_message_lock = threading.Lock()
        bm.summary_message = ""
        bm.at_least_one_non_fatal_error = False
        bm.failed_partition_count = 0
        bm.succeeded_partition_count = 0
        bm.partitions_to_backup = {}
        bm.diff_base_dir = diff_base_dir
        bm._reset_rzdiff_state()
        return bm

    def backup_partition(self, bm, dev_path, short, fs):
        bm.partitions_to_backup[dev_path] = {"size": 1, "cumulative_bytes": 0}
        result = bm._backup_partition_image(
            partition_key=dev_path,
            short_device_node=short,
            filesystem=fs,
            env=Utility.get_env_C_locale(),
            total_size=1,
            partition_number=1,
        )
        self.assertIsNone(result)

    def make_full_base(self, base_img, name="2026-11-27-1606-img-seekzilla"):
        base_dir = os.path.join(self.images_root, name)
        bm = self.make_bm(base_dir)
        self.backup_partition(bm, self.write_dev("sda1-base", base_img), "sda1", "ext4")
        self.assertEqual(bm.failed_partition_count, 0, bm.ui_manager.errors)
        for fname, text in (
            ("parts", "sda1\n"),
            ("disk", "sda\n"),
            ("clonezilla-img", "log\n"),
            ("Info-img-id.txt", "# id\nIMG_ID=abc123\n"),
        ):
            with open(os.path.join(base_dir, fname), "w", encoding="utf-8") as fh:
                fh.write(text)
        return base_dir


# ----------------------------------------------------------------------
# 1. Full backup: --bhash-output, .rzbhash + .rzidx (not for dd)
# ----------------------------------------------------------------------


class FullBackupBhashTest(_FakeBinMixin, unittest.TestCase):
    def test_full_backup_writes_rzbhash_and_rzidx_but_not_for_dd(self):
        base_img, _new, _resized = make_images()
        dest = os.path.join(self.images_root, "full")
        bm = self.make_bm(dest)
        recorded = []
        real_spawn = backup_manager.spawn_backup_pipeline

        def recording_spawn(pc, comp, split, *args, **kwargs):
            recorded.append((list(pc), list(comp), list(split)))
            return real_spawn(pc, comp, split, *args, **kwargs)

        with mock.patch("backup_manager.spawn_backup_pipeline", side_effect=recording_spawn):
            self.backup_partition(bm, self.write_dev("sda1", base_img), "sda1", "ext4")
            dd_data = random.Random(3).randbytes(300000)
            self.backup_partition(bm, self.write_dev("sda2", dd_data), "sda2", "fakefs")

        self.assertEqual(bm.failed_partition_count, 0, bm.ui_manager.errors)
        self.assertEqual(bm.succeeded_partition_count, 2)
        ext4_comp = recorded[0][1]
        dd_comp = recorded[1][1]
        self.assertEqual(os.path.basename(recorded[0][0][0]), "partclone.ext4")
        self.assertEqual(os.path.basename(recorded[1][0][0]), "partclone.dd")
        self.assertIn("--bhash-output", ext4_comp)
        self.assertEqual(
            ext4_comp[ext4_comp.index("--bhash-output") + 1],
            os.path.join(dest, "Info-bhash-sda1.rzbhash"),
        )
        self.assertNotIn("--bhash-output", dd_comp)
        self.assertEqual(ext4_comp[1], os.path.join(PKG_DIR, "seekable_compressor.py"))

        names = set(os.listdir(dest))
        self.assertIn("sda1.ext4-ptcl-img.zst.aa", names)
        self.assertIn("Info-seekidx-sda1.rzidx", names)
        self.assertIn("Info-bhash-sda1.rzbhash", names)
        self.assertIn("sda2.dd-ptcl-img.zst.aa", names)
        self.assertIn("Info-seekidx-sda2.rzidx", names)
        self.assertNotIn("Info-bhash-sda2.rzbhash", names)

        # The online .rzbhash matches an offline rebuild of the written image
        online = bh.load_bhash_file(os.path.join(dest, "Info-bhash-sda1.rzbhash"))
        offline = bh.build_bhash_from_stream(io.BytesIO(base_img))
        self.assertEqual(online.digests, offline.digests)
        self.assertIn("[seekable_compressor]", bm.logger.text)
        # Full mode never writes differential artifacts
        self.assertFalse(any(df.is_diff_artifact_filename(n) for n in names))

    def test_remove_partition_output_files_also_removes_rzbhash(self):
        dest = os.path.join(self.images_root, "cleanup")
        bm = self.make_bm(dest)
        prefix = os.path.join(dest, "sda1.ext4-ptcl-img.zst.")
        idx = os.path.join(dest, "Info-seekidx-sda1.rzidx")
        bhp = os.path.join(dest, "Info-bhash-sda1.rzbhash")
        keep = os.path.join(dest, "Info-bhash-sda10.rzbhash.keep")
        for p in (prefix + "aa", prefix + "ab", idx, bhp, bhp + ".tmp", keep):
            with open(p, "wb") as fh:
                fh.write(b"x")
        bm._remove_partition_output_files(prefix, idx, bhp)
        self.assertEqual(os.listdir(dest), [os.path.basename(keep)])

    def test_failed_pipeline_removes_rzbhash(self):
        dest = os.path.join(self.images_root, "failed")
        bm = self.make_bm(dest)
        # Corrupt Partclone stream: seekable_compressor --bhash-output fails (CRC32)
        base_img, _n, _r = make_images()
        bad = bytearray(base_img)
        bad[len(bad) // 2] ^= 0x01
        self.backup_partition(bm, self.write_dev("sda1", bytes(bad)), "sda1", "ext4")
        self.assertEqual(bm.failed_partition_count, 1)
        self.assertEqual(os.listdir(dest), [])

    def test_source_wiring(self):
        with open(backup_manager.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("bhash_output_path=bhash_path", src)
        self.assertIn('_sibling_script_path("diff_writer.py")', src)
        self.assertIn("sys.executable", src)


# ----------------------------------------------------------------------
# 2. Differential backup (+ fallback to full), Base untouched
# ----------------------------------------------------------------------


class DifferentialBackupTest(_FakeBinMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.base_img, self.new_img, self.resized_img = make_images()
        self.base_dir = self.make_full_base(self.base_img)
        self.base_snapshot = snapshot_tree(self.base_dir)

    def assertBaseUntouched(self):
        self.assertEqual(snapshot_tree(self.base_dir), self.base_snapshot)

    def test_diff_backup_produces_rzdiff_files_and_manifest(self):
        diff_dir = os.path.join(self.images_root, "2026-11-28-0900-diff-seekzilla")
        bm = self.make_bm(diff_dir, diff_base_dir=self.base_dir)
        self.backup_partition(bm, self.write_dev("sda1", self.new_img), "sda1", "ext4")
        dd_data = random.Random(5).randbytes(200000)
        self.backup_partition(bm, self.write_dev("sda2", dd_data), "sda2", "fakefs")
        ok, msg = bm._finalize_rzdiff_manifest()
        self.assertTrue(ok, msg)
        self.assertEqual(bm.failed_partition_count, 0, bm.ui_manager.errors)
        self.assertEqual(bm.succeeded_partition_count, 2)

        names = set(os.listdir(diff_dir))
        for expected in (
            "Info-rzdiff-manifest.json",
            "README-RZDIFF.txt",
            "sda1.ext4-rzdiff.meta",
            "sda1.ext4-rzdiff-img.zst.aa",
            "Info-seekidx-sda1.rzdiff.rzidx",
            # dd partition: automatic full image (+ .rzidx, never .rzbhash)
            "sda2.dd-ptcl-img.zst.aa",
            "Info-seekidx-sda2.rzidx",
        ):
            self.assertIn(expected, names)
        self.assertNotIn("sda1.ext4-ptcl-img.zst.aa", names)
        self.assertNotIn("Info-bhash-sda2.rzbhash", names)
        self.assertFalse(any(n.startswith("rzdiff-stats-") for n in names))

        manifest = df.load_diff_manifest(diff_dir)
        self.assertEqual(manifest["base_image"]["relative_path"], os.path.join("..", os.path.basename(self.base_dir)))
        self.assertEqual(manifest["base_image"]["suggested_dir_name"], os.path.basename(self.base_dir))
        self.assertEqual(manifest["base_image"]["img_id"], "abc123")
        self.assertEqual(manifest["base_image"]["disk"], "sda")
        entry = manifest["partitions"]["sda1"]
        self.assertEqual(entry["mode"], "diff")
        self.assertEqual(entry["base_image_prefix"], "sda1.ext4-ptcl-img.zst.")
        base_bhash = bh.load_bhash_file(os.path.join(self.base_dir, "Info-bhash-sda1.rzbhash"))
        self.assertEqual(entry["base_bhash_sha256"], base_bhash.file_sha256.hex())
        self.assertGreater(entry["stats"]["changed_chunks"], 0)
        self.assertGreater(entry["stats"]["unchanged_chunks"], 0)
        self.assertEqual(manifest["full_partitions"]["sda2"]["reason"], "non_partclone_dd")
        self.assertIn("[seekable_diff]", bm.logger.text)

        # Byte-exact reconstruction of the new Partclone stream from base + diff
        out = io.BytesIO()
        ds.synthesize_partition_stream(diff_dir, None, "sda1", out)
        self.assertEqual(out.getvalue(), self.new_img)
        self.assertBaseUntouched()

    def test_fallback_to_full_on_exit_42_incompatible_geometry(self):
        diff_dir = os.path.join(self.images_root, "2026-11-28-1000-diff-seekzilla")
        bm = self.make_bm(diff_dir, diff_base_dir=self.base_dir)
        self.backup_partition(bm, self.write_dev("sda1", self.resized_img), "sda1", "ext4")
        ok, msg = bm._finalize_rzdiff_manifest()
        self.assertTrue(ok, msg)
        self.assertEqual(bm.failed_partition_count, 0, bm.ui_manager.errors)
        self.assertEqual(bm.succeeded_partition_count, 1)
        self.assertEqual(bm.rzdiff_full_partitions["sda1"]["reason"], "fallback_incompatible_base")
        names = set(os.listdir(diff_dir))
        self.assertIn("sda1.ext4-ptcl-img.zst.aa", names)
        self.assertIn("Info-seekidx-sda1.rzidx", names)
        self.assertIn("Info-bhash-sda1.rzbhash", names)
        # No partial differential leftovers, and (no diff partition) no manifest
        self.assertFalse(any(df.is_diff_artifact_filename(n) for n in names), names)
        self.assertIn("fallback", bm.summary_message.lower())
        self.assertBaseUntouched()

    def test_fallback_reasons_without_matching_base_bhash(self):
        diff_dir = os.path.join(self.images_root, "2026-11-28-1100-diff-seekzilla")
        bm = self.make_bm(diff_dir, diff_base_dir=self.base_dir)
        self.backup_partition(bm, self.write_dev("sdb1", self.new_img), "sdb1", "ext4")
        self.assertEqual(bm.rzdiff_full_partitions["sdb1"]["reason"], "missing_base_bhash")
        self.assertIn("Info-bhash-sdb1.rzbhash", os.listdir(diff_dir))
        self.assertBaseUntouched()

    def test_check_diff_eligibility_reasons(self):
        ok = check_diff_eligibility(self.base_dir, "sda1", "ext4", "zstd")
        self.assertTrue(ok["eligible"], ok)
        self.assertEqual(ok["base_split_format"], "zstd")
        self.assertEqual(len(ok["base_header_sha256_hex"]), 64)
        cases = {
            "non_partclone_dd": dict(uses_partclone_fs=False),
            "cloning": dict(is_cloning=True),
        }
        for reason, kwargs in cases.items():
            res = check_diff_eligibility(self.base_dir, "sda1", "ext4", "zstd", **kwargs)
            self.assertEqual(res["reason"], reason)
        self.assertEqual(
            check_diff_eligibility(self.base_dir, "sda1", "ext4", "bzip2")["reason"],
            "unsupported_compression",
        )
        self.assertEqual(
            check_diff_eligibility(os.path.join(self.root, "nope"), "sda1", "ext4", "zstd")["reason"],
            "missing_base_dir",
        )
        self.assertEqual(
            check_diff_eligibility(self.base_dir, "../x", "ext4", "zstd")["reason"],
            "unsafe_partition_name",
        )
        self.assertBaseUntouched()

    def test_unreadable_base_splits_fall_back_cleanly(self):
        broken = os.path.join(self.images_root, "broken-base")
        shutil.copytree(self.base_dir, broken)
        for name in os.listdir(broken):
            if "-ptcl-img." in name:
                with open(os.path.join(broken, name), "wb") as fh:
                    fh.write(b"\x00" * 64)
        res = check_diff_eligibility(broken, "sda1", "ext4", "zstd")
        self.assertFalse(res["eligible"])
        self.assertEqual(res["reason"], "unreadable_base_image")

    def test_destination_inside_base_is_refused(self):
        inside = os.path.join(self.base_dir, "nested")
        bm = BackupManager(_RecordingUi(), "test")
        bm.dest_dir = inside
        bm.diff_base_dir = self.base_dir
        bm._reset_rzdiff_state()
        self.assertIsNone(bm.diff_base_dir)

    def test_multi_drive_mapping_lookup(self):
        mapping = {"/dev/sda": "/a", "nvme0n1": "/b"}
        self.assertEqual(backup_manager._lookup_drive_mapping(mapping, "sda"), "/a")
        self.assertEqual(backup_manager._lookup_drive_mapping(mapping, "/dev/nvme0n1"), "/b")
        self.assertIsNone(backup_manager._lookup_drive_mapping(mapping, "/dev/sdc"))
        self.assertIsNone(backup_manager._lookup_drive_mapping(None, "/dev/sda"))


# ----------------------------------------------------------------------
# Shared differential fixture (base full image + rzdiff image)
# ----------------------------------------------------------------------


class _DiffFixtureMixin(_FakeBinMixin):
    def setUp(self):
        super().setUp()
        self.base_img, self.new_img, _r = make_images(seed=11)
        self.base_dir = self.make_full_base(self.base_img)
        self.diff_dir = os.path.join(self.images_root, "2026-11-28-1200-diff-seekzilla")
        bm = self.make_bm(self.diff_dir, diff_base_dir=self.base_dir)
        self.backup_partition(bm, self.write_dev("sda1", self.new_img), "sda1", "ext4")
        ok, msg = bm._finalize_rzdiff_manifest()
        self.assertTrue(ok, msg)
        self.assertIn("sda1", bm.rzdiff_partitions)
        for fname, text in (("parts", "sda1\n"), ("clonezilla-img", "log\n")):
            with open(os.path.join(self.diff_dir, fname), "w", encoding="utf-8") as fh:
                fh.write(text)


# ----------------------------------------------------------------------
# 3. ClonezillaImage parsing of differential images
# ----------------------------------------------------------------------


class ClonezillaImageRzdiffTest(_DiffFixtureMixin, unittest.TestCase):
    def _parse(self, image_dir):
        from parser.clonezilla_image import ClonezillaImage

        images = ClonezillaImage.get_clonezilla_image_dict(
            os.path.join(image_dir, "clonezilla-img"), os.path.basename(image_dir)
        )
        self.assertEqual(len(images), 1)
        return next(iter(images.values()))

    def test_scan_rzdiff_image(self):
        from parser.clonezilla_image import ClonezillaImage

        fmt = ClonezillaImage.scan_backup_image(self.diff_dir, "sda1", False)
        self.assertTrue(fmt["is_rzdiff"])
        self.assertEqual(fmt["type"], "partclone")
        self.assertEqual(fmt["filesystem"], "ext4")
        self.assertEqual(fmt["binary"], "partclone.ext4")
        self.assertEqual(fmt["compression"], "zstd")
        self.assertEqual(fmt["diff_dir"], self.diff_dir)
        self.assertEqual(fmt["rzdiff_meta"], os.path.join(self.diff_dir, "sda1.ext4-rzdiff.meta"))
        self.assertEqual(
            fmt["absolute_filename_glob_list"][0],
            os.path.join(self.diff_dir, "sda1.ext4-rzdiff-img.zst.aa"),
        )
        self.assertFalse(fmt["is_lvm_logical_volume"])
        # The base (full) image directory has no differential partition
        self.assertEqual(ClonezillaImage.scan_rzdiff_image(self.base_dir, "sda1"), {})
        # Without its .meta file a delta split is ignored
        os.remove(os.path.join(self.diff_dir, "sda1.ext4-rzdiff.meta"))
        self.assertEqual(ClonezillaImage.scan_rzdiff_image(self.diff_dir, "sda1"), {})

    def test_clonezilla_image_flags_diff_image(self):
        image = self._parse(self.diff_dir)
        self.assertTrue(image.is_rzdiff_image)
        self.assertIsInstance(image.rzdiff_manifest, dict)
        self.assertIn("sda1", image.rzdiff_manifest["partitions"])
        self.assertTrue(image.enduser_filename.endswith(" [Diff]"))
        self.assertTrue(image.image_format_dict_dict["sda1"]["is_rzdiff"])
        self.assertNotIn("rzdiff_manifest", image.warning_dict)

    def test_corrupt_manifest_is_a_warning_not_a_crash(self):
        with open(os.path.join(self.diff_dir, df.DIFF_MANIFEST_FILENAME), "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        image = self._parse(self.diff_dir)
        self.assertIsNone(image.rzdiff_manifest)
        self.assertIn("rzdiff_manifest", image.warning_dict)
        # The delta splits still identify it as a differential image
        self.assertTrue(image.is_rzdiff_image)
        self.assertTrue(image.enduser_filename.endswith(" [Diff]"))

    def test_unsafe_manifest_is_rejected_gracefully(self):
        path = os.path.join(self.diff_dir, df.DIFF_MANIFEST_FILENAME)
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        manifest["partitions"]["sda1"]["meta"] = "../../etc/passwd"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        image = self._parse(self.diff_dir)
        self.assertIsNone(image.rzdiff_manifest)
        self.assertIn("Unsafe", image.warning_dict["rzdiff_manifest"])


# ----------------------------------------------------------------------
# 4. RestoreManager / VerifyManager rzdiff pipelines
# ----------------------------------------------------------------------


class RestoreVerifyRzdiffTest(_DiffFixtureMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.consumer_out = os.path.join(self.root, "consumer.out")
        os.environ["RZ_FAKE_CONSUMER_OUT"] = self.consumer_out
        from parser.clonezilla_image import ClonezillaImage

        self.fmt = ClonezillaImage.scan_backup_image(self.diff_dir, "sda1", False)

    def _image(self, diff_dir=None):
        diff_dir = diff_dir or self.diff_dir
        fmt = dict(self.fmt)
        fmt["diff_dir"] = diff_dir
        return types.SimpleNamespace(
            absolute_path=os.path.join(diff_dir, "clonezilla-img"),
            image_format_dict_dict={"sda1": fmt},
        )

    def _verify(self, image):
        from verify_manager import VerifyManager

        vm = VerifyManager(_RecordingUi())
        vm.logger = _ListLogger()
        vm.summary_message_lock = threading.Lock()
        vm.summary_message = ""
        vm.requested_stop = False
        vm.rzdiff_failed_partitions = []
        outcome = vm._verify_rzdiff_partition(
            image=image,
            partition_key="sda1",
            env=Utility.get_env_C_locale(),
            filesystem_verify_message="verify sda1",
            partition_estimated_size_bytes=1,
            cumulative_bytes=0,
            all_images_total_size_estimate=1,
            total_partition_number=1,
            all_images_num_partitions=1,
        )
        return vm, outcome

    def _restore(self, image):
        from restore_manager import RestoreManager

        self.use_restore_consumer()
        rm = RestoreManager(_RecordingUi())
        rm.logger = _ListLogger()
        rm.image = image
        rm.is_cloning = False
        rm.is_rescue = False
        rm.requested_stop = False
        rm.summary_message_lock = threading.Lock()
        rm.summary_message = ""
        rm.rzdiff_failed_partitions = []
        rm.restore_mapping_dict = {
            "sda1": {"dest_key": "/dev/fake1", "estimated_size_bytes": 1, "cumulative_bytes": 0}
        }
        outcome = rm._restore_rzdiff_partition(
            image_key="sda1",
            dest_part={"dest_key": "/dev/fake1"},
            env=Utility.get_env_C_locale(),
            total_size_estimate=1,
            image_number=1,
            filesystem_restore_message="restore sda1",
        )
        return rm, outcome

    def _read_consumer(self):
        with open(self.consumer_out, "rb") as fh:
            data = fh.read()
        with open(self.consumer_out + ".argv", encoding="utf-8") as fh:
            argv = fh.read().split("\n")
        return data, argv

    def test_verify_pipes_synthesized_stream_into_chkimg(self):
        vm, outcome = self._verify(self._image())
        self.assertEqual(outcome, "ok", vm.ui_manager.errors)
        data, argv = self._read_consumer()
        self.assertEqual(data, self.new_img)
        self.assertEqual(argv, ["--source", "-"])
        self.assertIn("successfully verified", vm.summary_message)
        synth_keys = [k for k in vm.proc if k.startswith("rzdiff_synth_")]
        self.assertEqual(len(synth_keys), 1)
        synth = vm.proc[synth_keys[0]]
        self.assertEqual(synth.returncode, 0)
        self.assertEqual(synth.args[1], os.path.join(PKG_DIR, "diff_synthesizer.py"))
        self.assertEqual(synth.args[0], sys.executable)

    def test_restore_pipes_synthesized_stream_into_partclone_restore(self):
        rm, outcome = self._restore(self._image())
        self.assertEqual(outcome, "ok", rm.ui_manager.errors)
        data, argv = self._read_consumer()
        self.assertEqual(data, self.new_img)
        self.assertIn("--restore", argv)
        self.assertEqual(argv[argv.index("--source") + 1], "-")
        self.assertEqual(argv[argv.index("--overwrite") + 1], "/dev/fake1")
        self.assertIn("Successfully restored", rm.summary_message)
        self.assertEqual(rm.rzdiff_failed_partitions, [])

    def _tampered_copy(self, mutate):
        """Copy base + diff into a fresh parent, apply mutate(base_copy, diff_copy)."""
        parent = tempfile.mkdtemp(dir=self.root)
        base_copy = os.path.join(parent, os.path.basename(self.base_dir))
        diff_copy = os.path.join(parent, os.path.basename(self.diff_dir))
        shutil.copytree(self.base_dir, base_copy)
        shutil.copytree(self.diff_dir, diff_copy)
        mutate(base_copy, diff_copy)
        return diff_copy

    @staticmethod
    def _flip_byte(path, offset):
        with open(path, "r+b") as fh:
            fh.seek(offset)
            b = fh.read(1)[0]
            fh.seek(offset)
            fh.write(bytes([b ^ 0x40]))

    def _failure_cases(self):
        new_bhash = bh.build_bhash_from_stream(io.BytesIO(self.new_img))

        def mismatch(base, _diff):  # valid but different .rzbhash -> exit 3
            bh.write_bhash_file(os.path.join(base, "Info-bhash-sda1.rzbhash"), new_bhash)

        def base_corrupt(base, _diff):  # bit flip in base .rzbhash -> exit 4
            p = os.path.join(base, "Info-bhash-sda1.rzbhash")
            self._flip_byte(p, os.path.getsize(p) // 2)

        def diff_corrupt(_base, diff):  # bit flip in differential metadata -> exit 5
            p = os.path.join(diff, "sda1.ext4-rzdiff.meta")
            self._flip_byte(p, os.path.getsize(p) - 5)

        return {3: mismatch, 4: base_corrupt, 5: diff_corrupt}

    def test_verify_and_restore_fail_loudly_on_synthesizer_errors(self):
        for rc, mutate in self._failure_cases().items():
            for mode in ("verify", "restore"):
                with self.subTest(rc=rc, mode=mode):
                    image = self._image(self._tampered_copy(mutate))
                    if mode == "verify":
                        mgr, outcome = self._verify(image)
                    else:
                        mgr, outcome = self._restore(image)
                    self.assertEqual(outcome, "failed")
                    self.assertEqual(mgr.rzdiff_failed_partitions, ["sda1"])
                    self.assertEqual(len(mgr.ui_manager.errors), 1)
                    err = mgr.ui_manager.errors[0]
                    self.assertIn("diff_synthesizer exit code {}".format(rc), err)
                    self.assertIn("diff_synthesizer", mgr.summary_message)
                    synth = [p for k, p in mgr.proc.items() if k.startswith("rzdiff_synth_")]
                    self.assertEqual(synth[0].returncode, rc)

    def test_missing_base_dir_fails_partition_cleanly(self):
        def remove_base(base, _diff):
            shutil.rmtree(base)

        for mode in ("verify", "restore"):
            with self.subTest(mode=mode):
                image = self._image(self._tampered_copy(remove_base))
                if mode == "verify":
                    mgr, outcome = self._verify(image)
                else:
                    mgr, outcome = self._restore(image)
                self.assertEqual(outcome, "failed")
                err = mgr.ui_manager.errors[0]
                self.assertIn("Required Base image folder: " + os.path.basename(self.base_dir), err)
                self.assertEqual(
                    [k for k in mgr.proc if k.startswith("rzdiff_synth_")], []
                )

    def test_cancel_terminates_synthesizer(self):
        from verify_manager import VerifyManager

        vm = VerifyManager(_RecordingUi())
        vm.logger = _ListLogger()
        vm.summary_message_lock = threading.Lock()
        vm.summary_message = ""
        vm.requested_stop = True
        vm.rzdiff_failed_partitions = []
        outcome = vm._verify_rzdiff_partition(
            image=self._image(),
            partition_key="sda1",
            env=Utility.get_env_C_locale(),
            filesystem_verify_message="verify sda1",
            partition_estimated_size_bytes=1,
            cumulative_bytes=0,
            all_images_total_size_estimate=1,
            total_partition_number=1,
            all_images_num_partitions=1,
        )
        self.assertEqual(outcome, "cancelled")
        for proc in vm.proc.values():
            self.assertIsNotNone(proc.poll())

    def test_spawn_rzdiff_pipeline_closes_parent_pipe_and_uses_devnull(self):
        import subprocess

        import verify_manager

        calls = []

        class _FakePipe:
            def __init__(self):
                self.closed = False

            def close(self):
                calls.append("close_synth_stdout")
                self.closed = True

        class _FakeProc:
            def __init__(self, cmd, **kwargs):
                calls.append(("popen", cmd[0], kwargs.get("stdin")))
                self.stdout = _FakePipe()
                self.kwargs = kwargs

        with mock.patch("verify_manager.subprocess.Popen", side_effect=_FakeProc):
            synth, consumer = verify_manager.spawn_rzdiff_pipeline(
                ["synth"], ["partclone.chkimg"], {}, None
            )
        self.assertEqual(synth.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(synth.kwargs["stdout"], subprocess.PIPE)
        self.assertIs(consumer.kwargs["stdin"], synth.stdout)
        self.assertEqual(
            calls,
            [
                ("popen", "synth", subprocess.DEVNULL),
                ("popen", "partclone.chkimg", synth.stdout),
                "close_synth_stdout",
            ],
        )

    def test_synth_cmd_list_is_cwd_independent(self):
        import verify_manager

        cmd = verify_manager.build_rzdiff_synth_cmd_list("/d", "/b", "sda1")
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(os.path.isabs(cmd[1]))
        self.assertEqual(os.path.basename(cmd[1]), "diff_synthesizer.py")
        self.assertEqual(
            cmd[2:], ["stream", "--diff-dir", "/d", "--base-dir", "/b", "--partition", "sda1"]
        )


# ----------------------------------------------------------------------
# 5. discover_compatible_base_images (handler.py, no GTK needed)
# ----------------------------------------------------------------------


def load_handler_function(name):
    """A top-level handler.py function exec'd on its own (must be self-contained)."""
    path = os.path.join(PKG_DIR, "handler.py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), path, "exec"), ns)
    return ns[name]


def load_discover_compatible_base_images():
    return load_handler_function("discover_compatible_base_images")


class DiscoverBaseImagesTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.parent = self._td.name
        self.discover = load_discover_compatible_base_images()

    def tearDown(self):
        self._td.cleanup()

    def _mk(self, name, disk=None, bhash_parts=(), diff=False, mtime=None):
        d = os.path.join(self.parent, name)
        os.makedirs(d)
        if disk is not None:
            with open(os.path.join(d, "disk"), "w", encoding="utf-8") as fh:
                fh.write(disk + "\n")
        for part in bhash_parts:
            open(os.path.join(d, "Info-bhash-" + part + ".rzbhash"), "wb").close()
        if diff:
            open(os.path.join(d, "Info-rzdiff-manifest.json"), "w").close()
        if mtime is not None:
            os.utime(d, (mtime, mtime))
        return d

    def test_single_drive(self):
        newer = self._mk("2026-11-27-1606-img-seekzilla", "sda", ["sda1", "sda2"], mtime=2000)
        older = self._mk("2026-11-20-0900-img-seekzilla", "sda", ["sda1"], mtime=1000)
        self._mk("2026-11-28-1000-diff-seekzilla", "sda", ["sda1"], diff=True)
        self._mk("2026-11-19-0900-img-rescuezilla", "sda", [])  # no .rzbhash
        self._mk("2026-11-26-0800-img-seekzilla", "sdb", ["sdb1"])  # other drive
        nodisk = self._mk("legacy-saveparts", None, ["sda3"], mtime=500)
        open(os.path.join(self.parent, "not-a-dir.txt"), "w").close()
        state = {"/dev/sda": {"partitions": {"/dev/sda1": {}, "/dev/sda2": {}, "/dev/sda3": {}}}}
        found = self.discover(self.parent, ["/dev/sda"], state)
        self.assertEqual([c["base_dir"] for c in found], [newer, older, nodisk])
        self.assertEqual(found[0]["base_dirs_by_drive"], {"/dev/sda": newer})
        self.assertEqual(found[0]["matched_partitions"]["/dev/sda"], ["sda1", "sda2"])
        self.assertFalse(found[0]["is_multi_drive"])
        self.assertEqual(found[0]["missing_drives"], [])

    def test_multi_drive_siblings_grouped(self):
        a = self._mk("2026-11-27-1606-img-rescuezilla-nvme0n1", "nvme0n1", ["nvme0n1p1"], mtime=3000)
        b = self._mk("2026-11-27-1606-img-rescuezilla-nvme1n1", "nvme1n1", ["nvme1n1p1"], mtime=3000)
        self._mk("2026-11-28-0800-diff-seekzilla-nvme0n1", "nvme0n1", ["nvme0n1p1"], diff=True)
        self._mk("2026-11-28-0900-img-seekzilla-nvme1n1", "nvme1n1", [], mtime=4000)
        partial = self._mk("2026-11-26-1000-img-seekzilla-nvme0n1", "nvme0n1", ["nvme0n1p2"], mtime=3500)
        ren_a = self._mk("2026-11-25-1200-mybackup-a", "nvme0n1", ["nvme0n1p1"], mtime=2000)
        ren_b = self._mk("2026-11-25-1200-mybackup-b", "nvme1n1", ["nvme1n1p1"], mtime=2000)
        keys = ["/dev/nvme0n1", "/dev/nvme1n1"]
        found = self.discover(self.parent, keys, {})
        by_name = {c["name"]: c for c in found}
        grouped = by_name["2026-11-27-1606-img-rescuezilla"]
        self.assertTrue(grouped["is_multi_drive"])
        self.assertEqual(grouped["base_dirs_by_drive"], {"/dev/nvme0n1": a, "/dev/nvme1n1": b})
        self.assertEqual(grouped["missing_drives"], [])
        self.assertIn("(2/2 drives)", grouped["label"])
        renamed = by_name["2026-11-25-1200"]
        self.assertEqual(renamed["base_dirs_by_drive"], {"/dev/nvme0n1": ren_a, "/dev/nvme1n1": ren_b})
        half = by_name["2026-11-26-1000-img-seekzilla"]
        self.assertEqual(half["base_dirs_by_drive"], {"/dev/nvme0n1": partial})
        self.assertEqual(half["missing_drives"], ["/dev/nvme1n1"])
        # complete groups first (newest first), partial matches last
        self.assertEqual(
            [c["name"] for c in found],
            ["2026-11-27-1606-img-rescuezilla", "2026-11-25-1200", "2026-11-26-1000-img-seekzilla"],
        )
        for cand in found:
            for path in cand["base_dirs_by_drive"].values():
                self.assertNotIn("diff", os.path.basename(path))

    def test_missing_parent_and_no_drives(self):
        self.assertEqual(self.discover(os.path.join(self.parent, "nope"), ["/dev/sda"], {}), [])
        self.assertEqual(self.discover(self.parent, [], {}), [])
        self.assertEqual(self.discover(self.parent, "/dev/sda", None), [])

    def test_handler_wiring(self):
        with open(os.path.join(PKG_DIR, "handler.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('SEEKZILLA_FULL_FOLDER_SUFFIX = "-img-seekzilla"', src)
        self.assertIn('SEEKZILLA_DIFF_FOLDER_SUFFIX = "-diff-seekzilla"', src)
        self.assertNotIn('"-img-rescuezilla"', src)
        # v2.2: the Backup Mode frame lives on Step 4; Step 6 only gets a warning label
        self.assertIn('_get_builder_object("backup_step4_contentvbox")', src)
        self.assertIn('_get_builder_object("backup_step7_content_vbox2")', src)
        self.assertNotIn('Gtk.Frame(label=_("Differential Backup (Optional)"))', src)
        self.assertIn("diff_base_dirs_by_drive=diff_base_dirs_by_drive", src)
        self.assertIn("diff_base_dir=diff_base_dir", src)
        self.assertIn("self.refresh_differential_base_images()", src)
        self.assertIn("set_on_scan_complete_callback", src)
        self.assertIn("threading.Thread(", src)
        self.assertIn("GLib.idle_add(self._bhash_build_progress_ui", src)
        self.assertIn("GLib.idle_add(self._on_bhash_build_finished", src)
        with open(os.path.join(PKG_DIR, "image_folder_query.py"), encoding="utf-8") as fh:
            ifq = fh.read()
        self.assertIn("def set_on_scan_complete_callback(self, callback):", ifq)
        populate = ifq[ifq.index("def _populate_image_list_table"):ifq.index("def scan_file")]
        self.assertLess(
            populate.index("self.please_wait_popup.destroy()"),
            populate.index("callback()"),
        )


# ----------------------------------------------------------------------
# 6. SeekZilla v2.2: Needs-.rzbhash discovery + per-partition preview
# ----------------------------------------------------------------------


def _write_valid_bhash(path, fs=b"ntfs"):
    bh.write_bhash_file(
        path,
        bh.BlockHashIndex(
            chunk_size=1048576, block_size=4096, totalblock=256, device_size=256 * 4096,
            num_chunks=1, fs_name=fs, digests=b"\x11" * 32,
        ),
    )


def _blkdev_list_text(kname, size, model):
    head = "{:<10} {:<12} {:>7} {:<4} {:<6} {:<10} {}".format(
        "KNAME", "NAME", "SIZE", "TYPE", "FSTYPE", "MOUNTPOINT", "MODEL"
    )
    row = "{:<10} {:<12} {:>7} {:<4} {:<6} {:<10} {}".format(kname, kname, size, "disk", "", "", model)
    part = "{:<10} {:<12} {:>7} {:<4} {:<6}".format(kname + "p1", "`-" + kname + "p1", "512M", "part", "vfat")
    return head + "\n" + row + "\n" + part + "\n"


class NeedsBhashDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.parent = self._td.name
        self.discover = load_discover_compatible_base_images()

    def tearDown(self):
        self._td.cleanup()

    def _mk(self, name, disk=None, parts=(), indexed=(), fs="ntfs", comp="zst", mtime=None,
            corrupt=(), diff=False, blkdev=None, dd_parts=()):
        d = os.path.join(self.parent, name)
        os.makedirs(d)
        if disk is not None:
            with open(os.path.join(d, "disk"), "w", encoding="utf-8") as fh:
                fh.write(disk + "\n")
        for part in parts:
            with open(os.path.join(d, f"{part}.{fs}-ptcl-img.{comp}.aa"), "wb") as fh:
                fh.write(b"z" * 1000)
            with open(os.path.join(d, f"{part}.{fs}-ptcl-img.{comp}.ab"), "wb") as fh:
                fh.write(b"z" * 234)
        for part in dd_parts:
            with open(os.path.join(d, f"{part}.dd-ptcl-img.{comp}.aa"), "wb") as fh:
                fh.write(b"d" * 10)
        for part in indexed:
            _write_valid_bhash(os.path.join(d, bh.bhash_index_filename(part)))
        for part in corrupt:
            open(os.path.join(d, bh.bhash_index_filename(part)), "wb").close()
        if diff:
            open(os.path.join(d, "sda1.ntfs-rzdiff.meta"), "w").close()
        if blkdev is not None:
            with open(os.path.join(d, "blkdev.list"), "w", encoding="utf-8") as fh:
                fh.write(blkdev)
        if mtime is not None:
            os.utime(d, (mtime, mtime))
        return d

    def test_ready_and_needs_bhash_single_drive(self):
        ready = self._mk("2026-11-01-0900-img-seekzilla", "sda", ["sda1", "sda2"], ["sda1", "sda2"], mtime=1000)
        legacy = self._mk("2026-11-20-0900-img-rescuezilla", "sda", ["sda1", "sda2"], [], mtime=3000)
        partial = self._mk("2026-11-10-0900-img-seekzilla", "sda", ["sda1", "sda2"], ["sda1"], mtime=2000)
        corrupt = self._mk("2026-11-05-0900-img-seekzilla", "sda", ["sda1"], [], corrupt=["sda1"], mtime=1500)
        self._mk("2026-11-28-1000-diff-seekzilla", "sda", ["sda1"], ["sda1"], diff=True, mtime=9000)
        self._mk("2026-11-27-1000-dd-only", "sda", [], [], dd_parts=["sda1"], mtime=8000)
        state = {"/dev/sda": {"partitions": {"/dev/sda1": {}, "/dev/sda2": {}}}}

        # Default mode: exact legacy behaviour (only folders with .rzbhash files, newest first).
        legacy_found = self.discover(self.parent, ["/dev/sda"], state)
        self.assertEqual([c["base_dir"] for c in legacy_found], [partial, corrupt, ready])
        self.assertEqual([c["label"] for c in legacy_found], [os.path.basename(c["base_dir"]) for c in legacy_found])
        self.assertNotIn("ready", legacy_found[0])

        found = self.discover(self.parent, ["/dev/sda"], state, include_needs_bhash=True)
        self.assertEqual([c["base_dir"] for c in found], [ready, legacy, partial, corrupt])
        by_dir = {c["base_dir"]: c for c in found}
        self.assertEqual(by_dir[ready]["label"], "2026-11-01-0900-img-seekzilla  [Ready]")
        self.assertTrue(by_dir[ready]["ready"])
        self.assertFalse(by_dir[ready]["needs_bhash"])
        self.assertTrue(by_dir[ready]["fully_indexed"])
        self.assertEqual(by_dir[legacy]["label"], "2026-11-20-0900-img-rescuezilla  [Needs .rzbhash index]")
        self.assertFalse(by_dir[legacy]["has_any_bhash"])
        self.assertEqual(by_dir[legacy]["missing_bhash_count"], 2)
        self.assertEqual(by_dir[legacy]["missing_bhash_bytes"], 2 * 1234)
        (base_dir, images), = by_dir[legacy]["missing_bhash_by_drive"]["/dev/sda"]
        self.assertEqual(base_dir, legacy)
        self.assertEqual([i.short_part for i in images], ["sda1", "sda2"])
        self.assertEqual(len(images[0].split_paths), 2)
        self.assertTrue(by_dir[partial]["has_any_bhash"])
        self.assertTrue(by_dir[partial]["needs_bhash"])
        self.assertFalse(by_dir[partial]["ready"])
        self.assertEqual(by_dir[partial]["missing_bhash_count"], 1)
        self.assertTrue(by_dir[corrupt]["needs_bhash"])  # empty .rzbhash counts as missing
        self.assertFalse(by_dir[corrupt]["has_any_bhash"])
        for cand in found:
            self.assertNotIn("diff", os.path.basename(cand["base_dir"]))
            self.assertNotIn("dd-only", os.path.basename(cand["base_dir"]))

    def test_multi_drive_ready_and_needs_groups(self):
        keys = ["/dev/nvme0n1", "/dev/nvme1n1"]
        r0 = self._mk("2026-11-01-0800-img-seekzilla-nvme0n1", "nvme0n1", ["nvme0n1p1"], ["nvme0n1p1"], mtime=1000)
        r1 = self._mk("2026-11-01-0800-img-seekzilla-nvme1n1", "nvme1n1", ["nvme1n1p1"], ["nvme1n1p1"], mtime=1000)
        n0 = self._mk("2026-11-25-0800-img-rescuezilla-nvme0n1", "nvme0n1", ["nvme0n1p1"], [], mtime=5000)
        n1 = self._mk("2026-11-25-0800-img-rescuezilla-nvme1n1", "nvme1n1", ["nvme1n1p1", "nvme1n1p2"], [], mtime=5000)
        self._mk("2026-11-26-0800-diff-seekzilla-nvme0n1", "nvme0n1", ["nvme0n1p1"], ["nvme0n1p1"], diff=True)
        found = self.discover(self.parent, keys, {}, include_needs_bhash=True)
        self.assertEqual(
            [c["label"] for c in found],
            [
                "2026-11-01-0800-img-seekzilla  (2/2 drives · Ready)",
                "2026-11-25-0800-img-rescuezilla  (2/2 drives · Needs .rzbhash index)",
            ],
        )
        self.assertEqual(found[0]["base_dirs_by_drive"], {keys[0]: r0, keys[1]: r1})
        needs = found[1]
        self.assertEqual(needs["base_dirs_by_drive"], {keys[0]: n0, keys[1]: n1})
        self.assertEqual(sorted(needs["missing_bhash_by_drive"]), keys)
        self.assertEqual(needs["missing_bhash_count"], 3)
        self.assertEqual(needs["missing_bhash_by_drive"][keys[1]][0][0], n1)
        # default mode: the unindexed group is not offered, label format unchanged
        legacy = self.discover(self.parent, keys, {})
        self.assertEqual([c["label"] for c in legacy], ["2026-11-01-0800-img-seekzilla (2/2 drives)"])

    def test_blkdev_list_model_and_size_match_when_device_names_drift(self):
        model = "Samsung SSD 980 PRO 1TB"
        cap = 1000204886016  # lsblk: 931.5G
        base = self._mk(
            "2026-11-20-0900-img-rescuezilla", "nvme1n1", ["nvme1n1p1", "nvme1n1p2"], ["nvme1n1p1"],
            blkdev=_blkdev_list_text("nvme1n1", "931.5G", model), mtime=1000,
        )
        state = {
            "/dev/nvme0n1": {"model": model, "capacity": cap, "partitions": {"/dev/nvme0n1p1": {}}},
            "/dev/sda": {"model": "Other Disk", "capacity": 500107862016, "partitions": {}},
        }
        self.assertEqual(self.discover(self.parent, ["/dev/nvme0n1"], state), [])  # default: no drift matching
        found = self.discover(self.parent, ["/dev/nvme0n1"], state, include_needs_bhash=True)
        self.assertEqual([c["base_dir"] for c in found], [base])
        self.assertTrue(found[0]["device_name_drifted"])
        self.assertTrue(found[0]["all_drifted"])
        self.assertFalse(found[0]["ready"])
        self.assertFalse(found[0]["needs_bhash"])
        self.assertEqual(found[0]["missing_bhash_by_drive"], {})
        self.assertIn("Device name changed: nvme1n1 -> nvme0n1", found[0]["label"])
        self.assertEqual(found[0]["drifted_drives"], {"/dev/nvme0n1": "nvme1n1"})
        # multi-drive: the drifted folder still joins a group for the right drive, not marked ready
        state["/dev/sda"]["partitions"] = {"/dev/sda1": {}}
        multi = self.discover(self.parent, ["/dev/nvme0n1", "/dev/sda"], state, include_needs_bhash=True)
        self.assertEqual(multi[0]["base_dirs_by_drive"], {"/dev/nvme0n1": base})
        self.assertEqual(multi[0]["missing_drives"], ["/dev/sda"])
        self.assertTrue(multi[0]["device_name_drifted"])
        self.assertTrue(multi[0]["all_drifted"])
        self.assertFalse(multi[0]["ready"])
        self.assertFalse(multi[0]["needs_bhash"])
        # nvme0n1 <-> nvme1n1 swap: blkdev.list model+size vetoes wrong suffix match
        other_model = "WD_BLACK SN850X 2000GB"
        other_cap = 2000398934016  # lsblk: 1.8T
        swap_base_0 = self._mk(
            "2026-11-22-0900-img-seekzilla-nvme0n1", "nvme0n1", ["nvme0n1p1"], ["nvme0n1p1"],
            blkdev=_blkdev_list_text("nvme0n1", "1.8T", other_model), mtime=2000,
        )
        swap_base_1 = self._mk(
            "2026-11-22-0900-img-seekzilla-nvme1n1", "nvme1n1", ["nvme1n1p1"], ["nvme1n1p1"],
            blkdev=_blkdev_list_text("nvme1n1", "931.5G", model), mtime=2000,
        )
        swapped_state = {
            "/dev/nvme0n1": {"model": model, "capacity": cap, "partitions": {"/dev/nvme0n1p1": {}}},
            "/dev/nvme1n1": {"model": other_model, "capacity": other_cap, "partitions": {"/dev/nvme1n1p1": {}}},
        }
        swapped = self.discover(
            self.parent, ["/dev/nvme0n1", "/dev/nvme1n1"], swapped_state, include_needs_bhash=True
        )
        swap_group = next(c for c in swapped if swap_base_0 in c["base_dirs_by_drive"].values())
        self.assertEqual(
            swap_group["base_dirs_by_drive"],
            {"/dev/nvme0n1": swap_base_1, "/dev/nvme1n1": swap_base_0},
        )
        self.assertTrue(swap_group["all_drifted"])
        self.assertFalse(swap_group["ready"])
        # ambiguous: two identical drives -> never guess
        state["/dev/nvme2n1"] = {"model": model, "capacity": cap, "partitions": {}}
        self.assertEqual(self.discover(self.parent, ["/dev/nvme0n1"], state, include_needs_bhash=True), [])
        # size mismatch -> no match
        del state["/dev/nvme2n1"]
        state["/dev/nvme0n1"]["capacity"] = 2 * cap
        self.assertEqual(self.discover(self.parent, ["/dev/nvme0n1"], state, include_needs_bhash=True), [])


class DifferentialPartitionPreviewTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.preview = load_handler_function("build_differential_partition_preview")

    def tearDown(self):
        self._td.cleanup()

    def _base(self, name, parts, indexed, fs="ntfs"):
        d = os.path.join(self._td.name, name)
        os.makedirs(d)
        for p in parts:
            open(os.path.join(d, f"{p}.{fs}-ptcl-img.zst.aa"), "wb").close()
        for p in indexed:
            open(os.path.join(d, bh.bhash_index_filename(p)), "wb").close()
        return d

    @staticmethod
    def _parts(*names, fs="ntfs"):
        return collections.OrderedDict(("/dev/" + n, {"filesystem": fs}) for n in names)

    def _modes(self, result, dk):
        drive = next(d for d in result["drives"] if d["drive"] == dk)
        return {p["short_part"]: (p["mode"], p["reason"]) for p in drive["partitions"]}

    def test_base_has_3_user_selects_4(self):
        base = self._base("b", ["sda1", "sda2", "sda3"], ["sda1", "sda2", "sda3"])
        res = self.preview(["/dev/sda"], {"/dev/sda": self._parts("sda1", "sda2", "sda3", "sda4")},
                           {"base_dirs_by_drive": {"/dev/sda": base}})
        self.assertEqual((res["diff_count"], res["full_count"], res["pending_bhash_count"]), (3, 1, 0))
        modes = self._modes(res, "/dev/sda")
        self.assertEqual(modes["sda4"][0], "full")
        self.assertIn("not in base", modes["sda4"][1])
        self.assertEqual(res["omitted_base_parts"], {"/dev/sda": []})
        self.assertIn("sda4", res["summary_text"])
        self.assertIn("3 differential", res["summary_text"])

    def test_base_has_4_user_selects_3(self):
        base = self._base("b", ["sda1", "sda2", "sda3", "sda4"], ["sda1", "sda2", "sda3", "sda4"])
        res = self.preview(["/dev/sda"], {"/dev/sda": self._parts("sda1", "sda2", "sda3")},
                           {"base_dirs_by_drive": {"/dev/sda": base}})
        self.assertEqual((res["diff_count"], res["full_count"]), (3, 0))
        self.assertEqual(res["omitted_base_parts"], {"/dev/sda": ["sda4"]})
        self.assertIn("not selected", res["summary_text"])
        self.assertIn("sda4", res["summary_text"])

    def test_dd_partition_is_full(self):
        base = self._base("b", ["sda1", "sda2"], ["sda1", "sda2"])
        parts = self._parts("sda1")
        parts["/dev/sda2"] = {"filesystem": ""}  # no partclone filesystem -> partclone.dd
        res = self.preview(["/dev/sda"], {"/dev/sda": parts}, {"base_dirs_by_drive": {"/dev/sda": base}})
        modes = self._modes(res, "/dev/sda")
        self.assertEqual(modes["sda1"][0], "diff")
        self.assertEqual(modes["sda2"][0], "full")
        self.assertIn("partclone.dd", modes["sda2"][1])

    def test_filesystem_changed_and_drifted_drive_preview_as_full(self):
        base = self._base("b", ["sda1", "sda2"], ["sda1", "sda2"], fs="ext4")
        parts = collections.OrderedDict(
            [("/dev/sda1", {"filesystem": "ext4"}), ("/dev/sda2", {"filesystem": "btrfs"})]
        )
        res = self.preview(["/dev/sda"], {"/dev/sda": parts}, {"base_dirs_by_drive": {"/dev/sda": base}})
        modes = self._modes(res, "/dev/sda")
        self.assertEqual(modes["sda1"][0], "diff")
        self.assertEqual(modes["sda2"][0], "full")
        self.assertIn("filesystem changed", modes["sda2"][1])
        # Drifted drive must preview every partition as full, never diff/pending
        drifted_res = self.preview(
            ["/dev/sda"],
            {"/dev/sda": parts},
            {"base_dirs_by_drive": {"/dev/sda": base}, "drifted_drives": {"/dev/sda": "sdb"}},
        )
        self.assertEqual((drifted_res["diff_count"], drifted_res["full_count"], drifted_res["pending_bhash_count"]), (0, 2, 0))
        dmodes = self._modes(drifted_res, "/dev/sda")
        self.assertIn("drive name changed", dmodes["sda1"][1])

    def test_unindexed_base_is_pending_bhash(self):
        base = self._base("b", ["sda1", "sda2"], [])
        res = self.preview(["/dev/sda"], {"/dev/sda": self._parts("sda1", "sda2")},
                           {"base_dirs_by_drive": {"/dev/sda": base}})
        self.assertEqual(res["pending_bhash_count"], 2)
        self.assertEqual(self._modes(res, "/dev/sda")["sda1"][0], "pending_bhash")
        self.assertIn("Build Base Index", res["summary_text"])

    def test_multi_drive_with_one_drive_missing(self):
        base = self._base("b-nvme0n1", ["nvme0n1p1"], ["nvme0n1p1"])
        keys = ["/dev/nvme0n1", "/dev/nvme1n1"]
        res = self.preview(
            keys,
            {keys[0]: self._parts("nvme0n1p1"), keys[1]: self._parts("nvme1n1p1")},
            {"base_dirs_by_drive": {keys[0]: base}, "missing_drives": [keys[1]]},
        )
        self.assertEqual(self._modes(res, keys[0])["nvme0n1p1"][0], "diff")
        mode, reason = self._modes(res, keys[1])["nvme1n1p1"]
        self.assertEqual(mode, "full")
        self.assertIn("no compatible base", reason)
        self.assertEqual((res["diff_count"], res["full_count"]), (1, 1))

    def test_no_candidate_means_all_full(self):
        res = self.preview(["/dev/sda"], {"/dev/sda": self._parts("sda1")}, None)
        self.assertEqual((res["diff_count"], res["full_count"]), (0, 1))


# ----------------------------------------------------------------------
# 7. SeekZilla v2.2: Step 4 Handler workflow with fake (headless) GTK
# ----------------------------------------------------------------------


class _FakeWidget:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.children = []
        self.parent = None
        self.visible = False
        self.no_show_all = False
        self.sensitive = True
        self.text = kwargs.get("label", "")
        self.markup = None
        self.handlers = collections.defaultdict(list)
        self.padding = (0, 20)
        self.destroyed = False

    def __getattr__(self, name):
        if name.startswith(("set_", "add_mark", "clear_")):
            return lambda *a, **k: None
        raise AttributeError(name)

    def connect(self, signal, cb, *extra):
        self.handlers[signal].append((cb, extra))

    def emit(self, signal, *args):
        result = None
        for cb, extra in list(self.handlers[signal]):
            result = cb(self, *(args + extra))
        return result

    # containers
    def pack_start(self, child, *_a):
        self.children.append(child)
        child.parent = self

    def add(self, child):
        self.pack_start(child)

    def get_children(self):
        return list(self.children)

    def reorder_child(self, child, pos):
        self.children.remove(child)
        self.children.insert(pos, child)

    def get_parent(self):
        return self.parent

    def get_child(self):
        return None

    def get_content_area(self):
        content = self.__dict__.get("_content")
        if content is None:
            content = self.__dict__["_content"] = _FakeWidget()
            self.pack_start(content)
        return content

    # visibility
    def set_no_show_all(self, value):
        self.no_show_all = bool(value)

    def set_visible(self, value):
        self.visible = bool(value)

    def get_visible(self):
        return self.visible

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False

    def show_all(self):
        if self.no_show_all:
            return
        self.visible = True
        for child in self.children:
            child.show_all()

    def set_sensitive(self, value):
        self.sensitive = bool(value)

    def get_sensitive(self):
        return self.sensitive

    def destroy(self):
        self.destroyed = True

    # labels / entries
    def set_text(self, text):
        self.text = text

    def get_text(self):
        return self.text

    def set_markup(self, markup):
        self.markup = markup
        self.text = markup

    def get_padding(self):
        return self.padding

    def set_padding(self, x, y):
        self.padding = (x, y)

    def get_cells(self):
        return []

    def add_button(self, *_a):
        button = _FakeWidget()
        self.pack_start(button)
        return button


class _FakeRadio(_FakeWidget):
    def __init__(self, group_member, label):
        super().__init__(label=label)
        self.group = group_member.group if group_member is not None else []
        self.group.append(self)
        self.active = group_member is None

    @classmethod
    def new_with_label_from_widget(cls, group_member, label):
        return cls(group_member, label)

    def get_active(self):
        return self.active

    def set_active(self, value):
        if not value or self.active:
            return  # GTK: a radio button cannot be de-activated directly
        changed = [r for r in self.group if r.active]
        for r in changed:
            r.active = False
        self.active = True
        for r in changed + [self]:
            r.emit("toggled")


class _FakeCombo(_FakeWidget):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.items = []
        self.active = -1

    def append_text(self, text):
        self.items.append(text)

    def remove_all(self):
        self.items = []
        if self.active != -1:
            self.active = -1
            self.emit("changed")

    def set_active(self, i):
        if i != self.active:
            self.active = i
            self.emit("changed")

    def get_active(self):
        return self.active


class _FakeCompressionCombo(_FakeWidget):
    def __init__(self, fmt):
        super().__init__()
        self.fmt = fmt

    def get_active_iter(self):
        return "it"

    def get_model(self):
        return {"it": [self.fmt]}


class _FakeMessageDialog(_FakeWidget):
    response = None
    instances = []

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.secondary = ""
        _FakeMessageDialog.instances.append(self)

    def format_secondary_text(self, text):
        self.secondary = text

    def run(self):
        return _FakeMessageDialog.response


def _make_fake_gtk():
    enum = types.SimpleNamespace
    return types.SimpleNamespace(
        Frame=_FakeWidget, Box=_FakeWidget, Label=_FakeWidget, Button=_FakeWidget,
        Viewport=_FakeWidget, ScrolledWindow=_FakeWidget, ProgressBar=_FakeWidget,
        Dialog=_FakeWidget, ComboBoxText=_FakeCombo, RadioButton=_FakeRadio,
        MessageDialog=_FakeMessageDialog,
        Orientation=enum(VERTICAL="V", HORIZONTAL="H"),
        PolicyType=enum(AUTOMATIC="auto", NEVER="never"),
        ShadowType=enum(IN="in", NONE="none"),
        MessageType=enum(QUESTION="q"),
        ButtonsType=enum(OK_CANCEL="okc"),
        ResponseType=enum(OK=-5, CANCEL=-6),
        STOCK_CANCEL="gtk-cancel",
    )


class _DeferredThread:
    """threading.Thread stand-in: start() only records; the test calls run()."""

    instances = []

    def __init__(self, target=None, args=(), kwargs=None, name=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}
        self.name = name
        self.daemon = daemon
        self.started = False
        _DeferredThread.instances.append(self)

    def start(self):
        self.started = True

    def run(self):
        self._target(*self._args, **self._kwargs)


def load_handler_class(extra_ns):
    """handler.Handler (+ module-level helpers) exec'd against fake GTK globals."""
    path = os.path.join(PKG_DIR, "handler.py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    body = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) or (
            isinstance(node, ast.ClassDef) and node.name == "Handler"
        ):
            body.append(node)
        elif isinstance(node, ast.Assign) and all(
            isinstance(t, ast.Name) and t.id.startswith("SEEKZILLA_") for t in node.targets
        ):
            body.append(node)
    import datetime as _dt
    import time as _time
    import traceback as _traceback
    import wizard_state

    ns = {
        "os": os, "collections": collections, "time": _time, "traceback": _traceback,
        "datetime": _dt.datetime, "block_hash": bh, "Utility": Utility,
        "Mode": wizard_state.Mode, "Page": wizard_state.Page, "_": lambda s: s,
        "compute_backup_dest_dirs": backup_manager.compute_backup_dest_dirs,
    }
    ns.update(extra_ns)
    exec(compile(ast.Module(body=body, type_ignores=[]), path, "exec"), ns)
    return ns["Handler"], ns


class Step4HandlerWorkflowTest(unittest.TestCase):
    PART = "sda1"

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.parent = os.path.join(self._td.name, "dest")
        os.makedirs(self.parent)
        self.errors = []
        self.idle = []
        _DeferredThread.instances = []
        _FakeMessageDialog.instances = []
        _FakeMessageDialog.response = -5  # OK
        test = self

        class _Error:
            def __init__(self, builder, message, error_heading=""):
                test.errors.append(message)

        self.gtk = _make_fake_gtk()
        self.Handler, self.ns = load_handler_class(
            {
                "Gtk": self.gtk,
                "GObject": types.SimpleNamespace(markup_escape_text=lambda s: s),
                "GLib": types.SimpleNamespace(
                    idle_add=lambda fn, *args: self.idle.append((fn, args))
                ),
                "Pango": types.SimpleNamespace(
                    EllipsizeMode=types.SimpleNamespace(END="end", MIDDLE="middle"),
                    WrapMode=types.SimpleNamespace(WORD_CHAR="wc"),
                ),
                "threading": types.SimpleNamespace(Event=threading.Event, Thread=_DeferredThread),
                "ErrorMessageModalPopup": _Error,
            }
        )
        # Unindexed full base (official Rescuezilla style): zstd splits + .rzidx, no .rzbhash
        self.base = os.path.join(self.parent, "2026-11-20-0900-img-rescuezilla")
        os.makedirs(self.base)
        rng = random.Random(404)
        blocks = {b: rng.randbytes(BS) for b in range(TB) if rng.random() < 0.5}
        self.img = build_ptcl(blocks, TB)
        out = io.BytesIO()
        self.rzidx = os.path.join(self.base, f"Info-seekidx-{self.PART}.rzidx")
        sc.compress_stream(io.BytesIO(self.img), out, fmt="zstd", level=1, frame_size=65536,
                           index_output=self.rzidx, threads=2)
        data = out.getvalue()
        self.splits = []
        split_size = max(1, len(data) // 3 + 1)  # always 3 split files (.aa .ab .ac)
        for i, off in enumerate(range(0, len(data), split_size)):
            p = os.path.join(self.base, f"{self.PART}.ntfs-ptcl-img.zst.a" + "abc"[i])
            with open(p, "wb") as fh:
                fh.write(data[off:off + split_size])
            self.splits.append(p)
        with open(os.path.join(self.base, "disk"), "w") as fh:
            fh.write("sda\n")

    def tearDown(self):
        self._td.cleanup()

    def _builder(self, strict=False):
        objects = {}
        if not strict:
            step4 = _FakeWidget()
            for wid in ("backup_step4_text", "backup_step4_thickness_box", "backup_step4_text2",
                        "backup_step4_tree_scrolled", "backup_step4_when_ready_label"):
                objects[wid] = _FakeWidget()
                step4.pack_start(objects[wid])
            objects["backup_step4_contentvbox"] = step4
            step7 = _FakeWidget()
            objects["backup_step6_when_ready_label"] = _FakeWidget()
            step7.pack_start(objects["backup_step6_when_ready_label"])
            objects["backup_step7_content_vbox2"] = step7
            objects["backup_step6_compression_format_combobox"] = _FakeCompressionCombo("zstd")
            objects["backup_folder_label"] = _FakeWidget(label=self.parent)

        class _Builder:
            def get_object(self_inner, name):
                if strict:
                    return objects.get(name)
                return objects.setdefault(name, _FakeWidget())

        return _Builder(), objects

    def _handler(self, strict=False):
        builder, objects = self._builder(strict)
        h = self.Handler.__new__(self.Handler)
        h.builder = builder
        h.mode = self.ns["Mode"].BACKUP
        h.current_page = self.ns["Page"].BACKUP_DESTINATION_FOLDER
        h.selected_drive_key = "/dev/sda"
        h.active_backup_drive_keys = ["/dev/sda"]
        h.partitions_to_backup_by_drive = collections.OrderedDict(
            [("/dev/sda", collections.OrderedDict(
                [("/dev/sda1", {"filesystem": "ntfs"}), ("/dev/sda2", {"filesystem": "ext4"})]))]
        )
        h.drive_query = types.SimpleNamespace(
            drive_state={"/dev/sda": {"partitions": {"/dev/sda1": {}, "/dev/sda2": {}}}}
        )
        h._init_differential_backup_ui()
        return h, objects

    def _drain_idle(self):
        while self.idle:
            fn, args = self.idle.pop(0)
            fn(*args)

    def _run_build(self, h, cancel_first=False):
        h._on_build_base_index_clicked(None)
        self.assertEqual(len(_DeferredThread.instances), 1)
        thread = _DeferredThread.instances[0]
        self.assertTrue(thread.started)
        self.assertTrue(thread.daemon)
        # GTK main thread returned immediately: nothing built yet, dialog shown
        self.assertFalse(os.path.exists(os.path.join(self.base, bh.bhash_index_filename(self.PART))))
        state = h._diff_bhash_build_state
        self.assertTrue(state["dialog"].visible)
        self.assertTrue(h.diff_bhash_build_in_progress)
        self.assertFalse(h.diff_build_bhash_button.get_sensitive())
        if cancel_first:
            state["dialog"].emit("response", self.gtk.ResponseType.CANCEL)
            self.assertTrue(state["cancel_event"].is_set())
        thread.run()
        self._drain_idle()
        self.assertTrue(state["dialog"].destroyed)
        self.assertFalse(h.diff_bhash_build_in_progress)
        return state

    def test_frame_is_injected_into_step4_not_step6(self):
        h, objects = self._handler()
        step4 = objects["backup_step4_contentvbox"].get_children()
        frame = step4[-2]
        self.assertIs(step4[-1], objects["backup_step4_when_ready_label"])
        self.assertEqual(frame.kwargs.get("label"), "Backup Mode & Differential Base")
        step7 = objects["backup_step7_content_vbox2"].get_children()
        self.assertEqual(len(step7), 2)  # only the compression warning label + when-ready label
        self.assertIs(step7[-1], objects["backup_step6_when_ready_label"])
        self.assertFalse(step7[0].visible)
        self.assertIsInstance(h.diff_checkbutton, _FakeRadio)
        self.assertTrue(h.diff_full_radiobutton.get_active())
        self.assertFalse(h.diff_checkbutton.get_active())
        for attr in ("diff_base_combobox", "diff_browse_button", "diff_status_label",
                     "diff_build_bhash_button", "diff_preview_label"):
            self.assertIsNotNone(getattr(h, attr), attr)
        self.assertFalse(h.diff_preview_scrolled.visible)  # Full mode: no preview
        self.assertFalse(h.diff_build_bhash_button.visible)
        self.assertEqual(objects["backup_step4_text"].get_padding(), (0, 2))  # compacted for 720p
        # minimal builder: graceful skip
        h2, _o = self._handler(strict=True)
        self.assertIsNone(h2.diff_checkbutton)
        self.assertEqual(h2.get_differential_backup_selection(), (None, None))

    def test_full_mode_next_proposes_img_seekzilla(self):
        h, objects = self._handler()
        h.next_tab(None)
        self.assertEqual(self.errors, [])
        self.assertTrue(objects["backup_name"].get_text().endswith("-img-seekzilla"))
        self.assertEqual(h.current_page, self.ns["Page"].BACKUP_IMAGE_NAME_SELECTION)
        self.assertIsNone(h.diff_selection)

    def test_unindexed_base_blocks_then_live_build_unblocks(self):
        h, objects = self._handler()
        h._on_image_folder_scan_complete()
        h.diff_checkbutton.set_active(True)
        self.assertFalse(h.diff_full_radiobutton.get_active())
        self.assertEqual(h.diff_base_combobox.items, ["2026-11-20-0900-img-rescuezilla  [Needs .rzbhash index]"])
        self.assertTrue(h.diff_preview_scrolled.visible)
        self.assertIn("NEEDS INDEX", h.diff_preview_label.text)
        self.assertTrue(h.diff_build_bhash_button.visible)
        self.assertTrue(h.diff_build_bhash_button.get_sensitive())
        # Next is blocked with an actionable message
        h.next_tab(None)
        self.assertEqual(h.current_page, self.ns["Page"].BACKUP_DESTINATION_FOLDER)
        self.assertEqual(len(self.errors), 1)
        self.assertIn("Build Base Index (.rzbhash)", self.errors[0])
        self.assertIn("Info-bhash-*.rzbhash", self.errors[0])
        # Live build (confirm -> background worker -> idle updates)
        before = snapshot_tree(self.base)
        state = self._run_build(h)
        self.assertIn("read-only", _FakeMessageDialog.instances[0].secondary)
        self.assertIn("Info-bhash-<part>.rzbhash", _FakeMessageDialog.instances[0].secondary)
        self.assertEqual(state["built"], [self.PART])
        self.assertEqual(state["errors"], [])
        after = snapshot_tree(self.base)
        new_files = sorted(set(after) - set(before))
        self.assertEqual(new_files, [bh.bhash_index_filename(self.PART)])
        for rel, meta in before.items():
            if rel != ".":
                self.assertEqual(after[rel], meta, rel)  # original backup files untouched
        built = bh.load_bhash_file(os.path.join(self.base, bh.bhash_index_filename(self.PART)))
        offline = bh.build_bhash_from_stream(io.BytesIO(self.img))
        self.assertEqual(built.digests, offline.digests)
        # candidate is now Ready, still selected, preview updated
        self.assertEqual(h.diff_base_combobox.items, ["2026-11-20-0900-img-rescuezilla  [Ready]"])
        self.assertEqual(h.diff_base_combobox.get_active(), 0)
        self.assertFalse(h.diff_build_bhash_button.visible)
        self.assertIn("DIFF", h.diff_preview_label.text)
        self.assertIn("1 differential", h.diff_preview_label.text)
        # Next now proceeds and proposes -diff-seekzilla directly
        h.next_tab(None)
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(h.current_page, self.ns["Page"].BACKUP_IMAGE_NAME_SELECTION)
        self.assertTrue(objects["backup_name"].get_text().endswith("-diff-seekzilla"))
        self.assertEqual(h.diff_selection["base_dirs_by_drive"], {"/dev/sda": self.base})
        # Step 6 warning only for a differential backup with a non zstd/gzip format
        h._update_diff_compression_warning()
        warning = h.diff_compression_warning_label
        self.assertFalse(warning.visible)
        objects["backup_step6_compression_format_combobox"].fmt = "uncompressed"
        h._update_diff_compression_warning()
        self.assertTrue(warning.visible)
        # Going back to Step 4 keeps the radio + base choice
        h.prev_tab(None)
        self.assertEqual(h.current_page, self.ns["Page"].BACKUP_DESTINATION_FOLDER)
        self.assertTrue(h.diff_checkbutton.get_active())
        self.assertEqual(h.get_differential_backup_selection()[0]["base_dir"], self.base)

    def test_cancelled_build_leaves_no_files_and_stays_blocked(self):
        h, _objects = self._handler()
        h._on_image_folder_scan_complete()
        h.diff_checkbutton.set_active(True)
        before = sorted(os.listdir(self.base))
        state = self._run_build(h, cancel_first=True)
        self.assertTrue(state["cancelled"])
        self.assertEqual(state["built"], [])
        self.assertEqual(sorted(os.listdir(self.base)), before)
        self.assertIn("[Needs .rzbhash index]", h.diff_base_combobox.items[0])
        h.next_tab(None)
        self.assertIn("Build Base Index (.rzbhash)", self.errors[-1])

    def test_read_only_base_folder_is_refused(self):
        h, _objects = self._handler()
        h._on_image_folder_scan_complete()
        h.diff_checkbutton.set_active(True)
        with mock.patch.object(os, "access", return_value=False):
            h._on_build_base_index_clicked(None)
        self.assertEqual(_DeferredThread.instances, [])
        self.assertIn("read-only", self.errors[-1])

    def test_partition_change_refreshes_preview(self):
        h, _objects = self._handler()
        h._on_image_folder_scan_complete()
        h.diff_checkbutton.set_active(True)
        self.assertIn("sda2", h.diff_preview_label.text)
        h.partitions_to_backup_by_drive["/dev/sda"] = collections.OrderedDict(
            [("/dev/sda1", {"filesystem": "ntfs"})]
        )
        h._on_image_folder_scan_complete()  # re-scan after returning to Step 4
        self.assertNotIn("sda2", h.diff_preview_label.text)
        self.assertTrue(h.diff_checkbutton.get_active())

    def test_drifted_base_blocks_next_and_hides_build_button(self):
        h, objects = self._handler()
        model = "Samsung SSD 980 PRO 1TB"
        cap = 1000204886016
        # Replace disk + add blkdev.list so base was recorded as sdb while active drive is sda
        with open(os.path.join(self.base, "disk"), "w") as fh:
            fh.write("sdb\n")
        with open(os.path.join(self.base, "blkdev.list"), "w") as fh:
            fh.write(_blkdev_list_text("sdb", "931.5G", model))
        h.drive_query.drive_state = {
            "/dev/sda": {"model": model, "capacity": cap, "partitions": {"/dev/sda1": {}}}
        }
        h._on_image_folder_scan_complete()
        h.diff_checkbutton.set_active(True)
        self.assertIn("Device name changed: sdb -> sda", h.diff_base_combobox.items[0])
        self.assertFalse(h.diff_build_bhash_button.visible)
        h.next_tab(None)
        self.assertEqual(h.current_page, self.ns["Page"].BACKUP_DESTINATION_FOLDER)
        self.assertIn("different device name", self.errors[-1])

    def test_switching_destination_folder_resets_diff_to_full_when_base_disappears(self):
        h, objects = self._handler()
        h._on_image_folder_scan_complete()
        h.diff_checkbutton.set_active(True)
        self.assertTrue(h.diff_checkbutton.get_active())
        other_parent = os.path.join(self._td.name, "other-folder")
        os.makedirs(other_parent)
        objects["backup_folder_label"].set_text(other_parent)
        h._on_image_folder_scan_complete()
        self.assertFalse(h.diff_checkbutton.get_active())
        self.assertTrue(h.diff_full_radiobutton.get_active())


if __name__ == "__main__":
    unittest.main()
