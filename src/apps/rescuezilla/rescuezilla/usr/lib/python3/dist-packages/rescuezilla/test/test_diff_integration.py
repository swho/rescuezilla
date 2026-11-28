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


def load_discover_compatible_base_images():
    path = os.path.join(PKG_DIR, "handler.py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "discover_compatible_base_images"
    )
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), path, "exec"), ns)
    return ns["discover_compatible_base_images"]


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
        self.assertIn('get_object("backup_step7_content_vbox2")', src)
        self.assertIn("diff_base_dirs_by_drive=diff_base_dirs_by_drive", src)
        self.assertIn("diff_base_dir=diff_base_dir", src)
        self.assertIn("self.refresh_differential_base_images()", src)


if __name__ == "__main__":
    unittest.main()
