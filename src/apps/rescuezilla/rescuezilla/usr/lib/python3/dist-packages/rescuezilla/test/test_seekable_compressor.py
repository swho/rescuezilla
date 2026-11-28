# ----------------------------------------------------------------------
#   Copyright (C) 2026 Rescuezilla Seekable-Index Community Fork Contributors
#   Modified 2026-11-28 (Community Fork): libdeflate backend, RAM-aware
#   worker count, default frame size and --print-stats tests.
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
import fnmatch
import gzip
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib

from seekable_compressor import (
    CODEC_GZIP,
    CODEC_ZSTD,
    DEFAULT_FRAME_SIZE,
    _LIBZSTD,
    _compress_zstd_frame_libzstd,
    _write_all,
    compress_stream,
    get_zstd_backend_name,
    parse_seekidx_bytes,
)
from utility import Utility


class SeekableCompressorTest(unittest.TestCase):
    def _verify_roundtrip(self, fmt: str, expected_codec: int, decomp_cmd: list):
        # 1.5 MiB payload with 256 KiB frames -> 6 independent frames
        payload = (
            b"Rescuezilla seekable test block! " * 12000
            + os.urandom(512 * 1024)
            + b"\x00" * (256 * 1024)
            + os.urandom(300 * 1024)
        )
        frame_size = 256 * 1024
        with tempfile.TemporaryDirectory() as td:
            idx_path = os.path.join(td, "Info-seekidx-nvme0n1p2.rzidx")
            out_buf = io.BytesIO()
            fc, total_c, total_u = compress_stream(
                io.BytesIO(payload),
                out_buf,
                fmt=fmt,
                level=3,
                frame_size=frame_size,
                index_output=idx_path,
                threads=4,
            )
            comp_blob = out_buf.getvalue()
            self.assertEqual(total_c, len(comp_blob))
            self.assertEqual(total_u, len(payload))
            self.assertGreaterEqual(fc, 5)

            # 1. Verify .rzidx header, CRC32, and entry count
            with open(idx_path, "rb") as f:
                idx_raw = f.read()
            meta = parse_seekidx_bytes(idx_raw)
            self.assertEqual(meta["codec"], expected_codec)
            self.assertEqual(meta["frame_count"], fc)
            self.assertEqual(meta["total_compressed"], len(comp_blob))
            self.assertEqual(meta["total_uncompressed"], len(payload))
            self.assertEqual(len(meta["entries"]), fc + 1)

            # 2. Verify stock upstream decompressor (gzip -d / zstd -d) reads 100% identically!
            if shutil.which(decomp_cmd[0]):
                proc = subprocess.run(
                    decomp_cmd,
                    input=comp_blob,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )
                self.assertEqual(proc.stdout, payload)

            # 3. Verify O(1) independent single-frame random access (last frame without reading prior!)
            last_i = fc - 1
            c_start, u_start = meta["entries"][last_i]
            c_end, u_end = meta["entries"][last_i + 1]
            frame_bytes = comp_blob[c_start:c_end]
            if fmt == "gzip":
                frame_out = zlib.decompress(frame_bytes, 16 + zlib.MAX_WBITS)
            else:
                proc = subprocess.run(
                    decomp_cmd,
                    input=frame_bytes,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )
                frame_out = proc.stdout
            self.assertEqual(frame_out, payload[u_start:u_end])

            # 4. Verify corrupted CRC32 is rejected
            bad = bytearray(idx_raw)
            bad[-1] ^= 0xFF
            with self.assertRaises(ValueError):
                parse_seekidx_bytes(bytes(bad))

    def test_gzip_seekable_roundtrip_and_stock_compat(self):
        self._verify_roundtrip("gzip", CODEC_GZIP, ["gzip", "-d", "-c"])

    def test_zstd_seekable_roundtrip_and_stock_compat(self):
        if not shutil.which("zstd"):
            self.skipTest("zstd not installed")
        self._verify_roundtrip("zstd", CODEC_ZSTD, ["zstd", "-d", "-c"])

    def test_cli_invocation_and_clonezilla_glob_non_collision(self):
        idx_name = Utility.get_seekable_index_filename("nvme0n1p2")
        self.assertEqual(idx_name, "Info-seekidx-nvme0n1p2.rzidx")
        # Must NOT match Rescuezilla / Clonezilla split image globs!
        self.assertFalse(fnmatch.fnmatch(idx_name, "nvme0n1p2.*-ptcl-img.*"))
        self.assertFalse(
            fnmatch.fnmatch(
                idx_name, "nvme0n1p2[.]*-ptcl-img[.][a-zA-Z]*[.][a-zA-Z][a-zA-Z]"
            )
        )

        with tempfile.TemporaryDirectory() as td:
            idx_path = os.path.join(td, idx_name)
            # Verify default frame size is 1 MiB (fine-grained random access)
            self.assertEqual(DEFAULT_FRAME_SIZE, 1024 * 1024)
            default_cmd = Utility.get_seekable_compression_cmd_list("gzip", 3, idx_path)
            self.assertIn("1048576", default_cmd)

            cmd = Utility.get_seekable_compression_cmd_list(
                "gzip", 3, idx_path, frame_size=65536
            )
            data = b"hello world " * 20000
            proc = subprocess.run(
                cmd,
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            self.assertTrue(os.path.isfile(idx_path))
            with open(idx_path, "rb") as f:
                meta = parse_seekidx_bytes(f.read())
            self.assertEqual(meta["total_uncompressed"], len(data))
            self.assertEqual(meta["total_compressed"], len(proc.stdout))

    def test_three_process_backup_manager_pipeline_with_gnu_split(self):
        """End-to-end test of the exact 3-process pipeline wired in backup_manager.py:
        producer (mock partclone) -> seekable_compressor.py -> split --suffix-length=2 --bytes=...
        """
        if not shutil.which("split"):
            self.skipTest("GNU split not installed")

        payload = (
            b"Partclone simulated payload block! " * 35000
            + os.urandom(1500 * 1024)
            + b"\x00" * (512 * 1024)
            + os.urandom(800 * 1024)
        )
        for fmt, decomp_cmd in [
            ("gzip", ["gzip", "-d", "-c"]),
            ("zstd", ["zstd", "-d", "-c"]),
        ]:
            if not shutil.which(decomp_cmd[0]):
                continue
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as td:
                idx_path = os.path.join(
                    td, Utility.get_seekable_index_filename("nvme0n1p1")
                )
                split_prefix = os.path.join(td, f"nvme0n1p1.ntfs-ptcl-img.{fmt[:2]}.")
                comp_cmd = Utility.get_seekable_compression_cmd_list(
                    fmt, 3, idx_path, frame_size=1024 * 1024
                )
                split_cmd = [
                    "split",
                    "--suffix-length=2",
                    "--bytes=1048576",
                    "-",
                    split_prefix,
                ]

                # Mirror BackupManager: 3 chained Popen processes
                p_prod = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                p_comp = subprocess.Popen(
                    comp_cmd,
                    stdin=p_prod.stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                p_prod.stdout.close()
                p_split = subprocess.Popen(
                    split_cmd,
                    stdin=p_comp.stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                p_comp.stdout.close()

                _, prod_err = p_prod.communicate(input=payload)
                _, comp_err = p_comp.communicate()
                _, split_err = p_split.communicate()

                self.assertEqual(
                    p_prod.returncode, 0, prod_err.decode(errors="replace")
                )
                self.assertEqual(
                    p_comp.returncode, 0, comp_err.decode(errors="replace")
                )
                self.assertEqual(
                    p_split.returncode, 0, split_err.decode(errors="replace")
                )

                # Verify multiple split chunks produced (.aa, .ab, ...)
                chunks = sorted(
                    os.path.join(td, fn)
                    for fn in os.listdir(td)
                    if fn.startswith(os.path.basename(split_prefix))
                )
                self.assertGreaterEqual(len(chunks), 2)
                parts_data = []
                for c in chunks:
                    with open(c, "rb") as fh:
                        parts_data.append(fh.read())
                combined = b"".join(parts_data)

                # Verify .rzidx matches exact split stream totals
                self.assertTrue(os.path.isfile(idx_path))
                with open(idx_path, "rb") as fh:
                    meta = parse_seekidx_bytes(fh.read())
                self.assertEqual(meta["total_uncompressed"], len(payload))
                self.assertEqual(meta["total_compressed"], len(combined))
                self.assertEqual(meta["frame_size"], 1024 * 1024)

                # Verify stock decompressor recovers byte-identical payload across concatenated splits
                dec = subprocess.run(
                    decomp_cmd,
                    input=combined,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )
                self.assertEqual(dec.stdout, payload)

    def test_libzstd_ctypes_backend_nonempty_and_empty_and_write_all_none(self):
        # F1 / M2 / M5: Directly verify native libzstd.so.1 ctypes path (used on Ubuntu 24.04 Python 3.12 ISO),
        # including multi-threaded compress_stream and per-frame RFC 8878 content checksum flag (bit 2 = 0x04).
        from unittest.mock import patch
        import seekable_compressor as sc

        self.assertIn(
            get_zstd_backend_name(), ("stdlib", "zstandard", "libzstd", "subprocess")
        )
        if _LIBZSTD is not None and shutil.which("zstd"):
            payload = (b"libzstd native frame test 0123456789 " * 30000)[: 1024 * 1024]
            for sample in (payload, b""):
                frame = _compress_zstd_frame_libzstd(sample, 3)
                self.assertGreater(len(frame), 5)
                # RFC 8878 Frame_Header_Descriptor bit 2 == Content_Checksum_flag
                self.assertTrue(bool(frame[4] & 0x04))
                dec = subprocess.run(
                    ["zstd", "-d", "-c"],
                    input=frame,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )
                self.assertEqual(dec.stdout, sample)

            # M5: Force multi-threaded compress_stream through the exact libzstd path
            big_payload = b"multithreaded libzstd payload block! " * 40000 + os.urandom(
                512 * 1024
            )
            with (
                patch.object(sc, "_py_zstd", None),
                patch.object(sc, "_zstandard_pkg", None),
            ):
                self.assertEqual(sc.get_zstd_backend_name(), "libzstd")
                out_buf = io.BytesIO()
                fc, tc, tu = compress_stream(
                    io.BytesIO(big_payload),
                    out_buf,
                    fmt="zstd",
                    level=3,
                    frame_size=256 * 1024,
                    threads=4,
                )
                self.assertGreaterEqual(fc, 6)
                self.assertEqual(tu, len(big_payload))
                blob = out_buf.getvalue()
                self.assertEqual(tc, len(blob))
                self.assertTrue(bool(blob[4] & 0x04))
                dec = subprocess.run(
                    ["zstd", "-d", "-c"],
                    input=blob,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )
                self.assertEqual(dec.stdout, big_payload)

        # M2: Also verify active compress_zstd_frame (stdlib on 3.14) sets checksum flag
        if shutil.which("zstd"):
            std_frame = sc.compress_zstd_frame(b"checksum check " * 50, 3)
            self.assertTrue(bool(std_frame[4] & 0x04))

        # F13: _write_all must raise BlockingIOError if write() returns None
        class _NoneStream:
            def write(self, _data):
                return None

        with self.assertRaises(BlockingIOError):
            _write_all(_NoneStream(), b"abc")


class LibDeflateBackendTest(unittest.TestCase):
    """P1a: native libdeflate.so.0 gzip backend + clean zlib fallback."""

    def _assert_gzip_ok(self, blob: bytes, expected: bytes):
        self.assertEqual(blob[:2], b"\x1f\x8b")
        self.assertEqual(gzip.decompress(blob), expected)
        if shutil.which("gzip"):
            dec = subprocess.run(
                ["gzip", "-d", "-c"],
                input=blob,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            self.assertEqual(dec.stdout, expected)

    def test_libdeflate_levels_empty_and_multithreaded_stream(self):
        import seekable_compressor as sc

        if sc._LIBDEFLATE is None:
            self.assertEqual(sc.get_gzip_backend_name(), "zlib")
            self.skipTest("libdeflate.so.0 not available on this system")
        self.assertEqual(sc.get_gzip_backend_name(), "libdeflate")

        payload = b"libdeflate seekable frame 0123456789 " * 30000 + os.urandom(
            64 * 1024
        )
        for level in (0, 1, 6, 9, 12, -5, 99):
            with self.subTest(level=level):
                frame = sc._compress_gzip_frame_libdeflate(payload, level)
                self._assert_gzip_ok(frame, payload)
                self._assert_gzip_ok(sc.compress_gzip_frame(payload, level), payload)
        # Empty input -> valid RFC 1952 empty gzip member
        empty = sc._compress_gzip_frame_libdeflate(b"", 6)
        self._assert_gzip_ok(empty, b"")
        self._assert_gzip_ok(sc.compress_gzip_frame(b""), b"")

        # Multi-threaded compress_stream through libdeflate, then stock gzip -d
        big = b"multithreaded libdeflate block! " * 60000 + os.urandom(700 * 1024)
        out_buf = io.BytesIO()
        stats = {}
        fc, tc, tu = sc.compress_stream(
            io.BytesIO(big),
            out_buf,
            fmt="gzip",
            level=6,
            frame_size=128 * 1024,
            threads=8,
            stats=stats,
        )
        self.assertGreaterEqual(fc, 10)
        self.assertEqual(tu, len(big))
        self.assertEqual(tc, len(out_buf.getvalue()))
        self.assertEqual(stats["backend"], "libdeflate")
        self.assertEqual(stats["workers"], 8)
        self._assert_gzip_ok(out_buf.getvalue(), big)

        # Thread-local cache: one compressor per (thread, level)
        sc.compress_gzip_frame(b"abc", 3)
        sc.compress_gzip_frame(b"abc", 3)
        cache = sc._GZIP_THREAD_LOCAL.libdeflate
        self.assertIn(3, cache)
        self.assertIsInstance(cache[3], sc._LibDeflateCompressor)

    def test_zlib_fallback_when_libdeflate_missing_or_failing(self):
        from unittest.mock import patch
        import seekable_compressor as sc

        payload = b"fallback payload " * 5000
        with patch.object(sc, "_LIBDEFLATE", None):
            self.assertEqual(sc.get_gzip_backend_name(), "zlib")
            for level in (0, 1, 6, 9, 12):
                self._assert_gzip_ok(sc.compress_gzip_frame(payload, level), payload)
            self._assert_gzip_ok(sc.compress_gzip_frame(b""), b"")
            with self.assertRaises(RuntimeError):
                sc._compress_gzip_frame_libdeflate(payload, 6)
            out_buf = io.BytesIO()
            stats = {}
            sc.compress_stream(
                io.BytesIO(payload),
                out_buf,
                fmt="gzip",
                level=6,
                frame_size=65536,
                threads=2,
                stats=stats,
            )
            self.assertEqual(stats["backend"], "zlib")
            self._assert_gzip_ok(out_buf.getvalue(), payload)

        # libdeflate present but failing at runtime -> clean zlib fallback
        with (
            patch.object(sc, "_LIBDEFLATE", object()),
            patch.object(
                sc,
                "_compress_gzip_frame_libdeflate",
                side_effect=RuntimeError("simulated failure"),
            ),
        ):
            self._assert_gzip_ok(sc.compress_gzip_frame(payload, 6), payload)

        # RAII wrapper: NULL alloc and n == 0 both raise RuntimeError; __del__ frees
        freed = []

        class _FakeFn:
            def __init__(self, fn):
                self._fn = fn

            def __call__(self, *a):
                return self._fn(*a)

        class _FakeLib:
            def __init__(self, alloc_ret, compress_ret):
                self.libdeflate_alloc_compressor = _FakeFn(lambda lvl: alloc_ret)
                self.libdeflate_free_compressor = _FakeFn(lambda h: freed.append(h))
                self.libdeflate_gzip_compress_bound = _FakeFn(lambda h, n: n + 64)
                self.libdeflate_gzip_compress = _FakeFn(
                    lambda h, src, n, dst, cap: compress_ret
                )

        with self.assertRaises(RuntimeError):
            sc._LibDeflateCompressor(_FakeLib(0, 10), 6)
        comp = sc._LibDeflateCompressor(_FakeLib(1234, 0), 6)
        with self.assertRaises(RuntimeError):
            comp.compress(b"data")
        del comp
        self.assertEqual(freed, [1234])

        with (
            patch.object(sc, "_LIBDEFLATE", _FakeLib(5678, 0)),
            patch.object(sc, "_GZIP_THREAD_LOCAL", sc.threading.local()),
        ):
            # Fake libdeflate returns n == 0 -> falls back to zlib transparently
            self._assert_gzip_ok(sc.compress_gzip_frame(payload, 6), payload)


class WorkerCountAndFrameSizeTest(unittest.TestCase):
    """P0 + P1b: RAM-aware worker count and format-specific frame sizes."""

    MiB = 1024 * 1024
    GiB = 1024 * 1024 * 1024

    def _with(self, cpus, mem):
        from unittest.mock import patch
        import seekable_compressor as sc

        return (
            patch.object(sc, "_get_available_cpu_count", return_value=cpus),
            patch.object(sc, "_get_available_memory_bytes", return_value=mem),
        )

    def test_default_worker_count_unlocks_and_caps(self):
        import seekable_compressor as sc

        cases = [
            # (cpus, mem, frame_size, fmt, level, expected_workers)
            (32, 64 * self.GiB, 4 * self.MiB, "gzip", 6, 32),  # high-RAM: all 32 CPUs
            (16, 16 * self.GiB, 1 * self.MiB, "gzip", 6, 16),
            (
                32,
                2 * self.GiB,
                4 * self.MiB,
                "gzip",
                6,
                32,
            ),  # 2 GiB / 32 MiB = 64 >= 32
            (32, 512 * self.MiB, 4 * self.MiB, "gzip", 6, 16),  # 512 / 32 = 16
            (32, 128 * self.MiB, 4 * self.MiB, "gzip", 6, 4),
            (4, 10 * self.MiB, 4 * self.MiB, "gzip", 6, 1),  # never below 1
            (24, None, 4 * self.MiB, "zstd", 19, 24),  # unknown RAM -> CPU count
            # High-level zstd CCtx RAM accounting (Finding #2):
            # lvl 3: budget = max(32, 16+32) = 48 MiB -> 2 GiB / 48 = 42 >= 32
            (32, 2 * self.GiB, 4 * self.MiB, "zstd", 3, 32),
            # lvl 15/19: budget = max(32, 16+192) = 208 MiB -> 2 GiB / 208 = 9
            (32, 2 * self.GiB, 4 * self.MiB, "zstd", 15, 9),
            (32, 2 * self.GiB, 4 * self.MiB, "zstd", 19, 9),
            # 64 GiB machine still unlocks all 32 CPUs even at zstd -19
            (32, 64 * self.GiB, 4 * self.MiB, "zstd", 19, 32),
        ]
        for cpus, mem, fs, fmt, lvl, expected in cases:
            p1, p2 = self._with(cpus, mem)
            with self.subTest(cpus=cpus, mem=mem, fs=fs, fmt=fmt, lvl=lvl), p1, p2:
                workers = sc.default_worker_count(fs, fmt=fmt, level=lvl)
                self.assertEqual(workers, expected)
                mif = sc.default_max_in_flight(workers, fs)
                self.assertGreaterEqual(mif, 2)
                self.assertLessEqual(mif, max(workers * 2, 2))
                self.assertLessEqual(mif, max(workers + 16, 2))
                if mem is not None and mif > 2:
                    self.assertLessEqual(mif * fs * 2, mem // 2)

        # Explicit oversized --threads must NOT bypass the <= 1/2 MemAvailable in-flight cap
        p1, p2 = self._with(32, 128 * self.MiB)
        with p1, p2:
            # 128 MiB / 2 = 64 MiB; each 4 MiB frame ~= 8 MiB -> cap = 8 (not 101!)
            self.assertEqual(sc.default_max_in_flight(100, 4 * self.MiB), 8)

        # Head-of-line: 32 workers on huge RAM -> min(64, 48) = 48 in flight
        p1, p2 = self._with(32, 64 * self.GiB)
        with p1, p2:
            self.assertEqual(sc.default_max_in_flight(32, 4 * self.MiB), 48)
            self.assertEqual(sc.default_max_in_flight(4, 4 * self.MiB), 8)

        # compress_stream(threads=None) uses default_worker_count(frame_size)
        p1, p2 = self._with(12, 64 * self.GiB)
        with p1, p2:
            stats = {}
            sc.compress_stream(
                io.BytesIO(b"x" * 300000),
                io.BytesIO(),
                fmt="gzip",
                level=1,
                frame_size=65536,
                threads=None,
                stats=stats,
            )
            self.assertEqual(stats["workers"], 12)
            self.assertEqual(stats["frame_size"], 65536)

    def test_cpu_and_meminfo_probes(self):
        from unittest.mock import mock_open, patch
        import seekable_compressor as sc

        self.assertGreaterEqual(sc._get_available_cpu_count(), 1)
        with patch.object(sc.os, "sched_getaffinity", side_effect=OSError("x")):
            self.assertEqual(sc._get_available_cpu_count(), os.cpu_count() or 1)

        real = sc._get_available_memory_bytes()
        if os.path.exists("/proc/meminfo"):
            self.assertIsNotNone(real)
            self.assertGreater(real, 0)

        meminfo = (
            "MemTotal:       16000000 kB\n"
            "MemFree:               1 kB\n"
            "MemAvailable:    8000000 kB\n"
        )
        with patch("builtins.open", mock_open(read_data=meminfo)):
            self.assertEqual(sc._get_available_memory_bytes(), 8000000 * 1024)
        with patch("builtins.open", mock_open(read_data="MemTotal: 2048 kB\n")):
            self.assertEqual(sc._get_available_memory_bytes(), 2048 * 1024)
        with patch("builtins.open", mock_open(read_data="garbage\nMemTotal: x kB\n")):
            self.assertIsNone(sc._get_available_memory_bytes())
        with patch("builtins.open", side_effect=OSError("no procfs")):
            self.assertIsNone(sc._get_available_memory_bytes())

    def test_default_frame_size_and_utility_cmd_list(self):
        import seekable_compressor as sc

        self.assertEqual(sc.DEFAULT_FRAME_SIZE, 1 * self.MiB)
        self.assertEqual(sc.DEFAULT_ZSTD_FRAME_SIZE, 4 * self.MiB)
        self.assertEqual(sc.default_frame_size_for_format("zstd"), 4 * self.MiB)
        self.assertEqual(sc.default_frame_size_for_format(" ZSTD "), 4 * self.MiB)
        self.assertEqual(sc.default_frame_size_for_format("gzip"), 1 * self.MiB)

        def _frame_arg(cmd):
            return cmd[cmd.index("--frame-size") + 1]

        z = Utility.get_seekable_compression_cmd_list("zstd", 3, "/tmp/x.rzidx")
        g = Utility.get_seekable_compression_cmd_list("gzip", 6, "/tmp/x.rzidx")
        self.assertEqual(_frame_arg(z), str(4 * self.MiB))
        self.assertEqual(_frame_arg(g), str(1 * self.MiB))
        self.assertNotIn("--print-stats", z)
        self.assertNotIn("--print-stats", g)
        zs = Utility.get_seekable_compression_cmd_list(
            "zstd", 3, "/tmp/x.rzidx", print_stats=True
        )
        self.assertEqual(zs[-1], "--print-stats")
        custom = Utility.get_seekable_compression_cmd_list(
            "zstd", 3, "/tmp/x.rzidx", frame_size=65536
        )
        self.assertEqual(_frame_arg(custom), "65536")
        # Non-seekable formats unchanged
        self.assertEqual(
            Utility.get_seekable_compression_cmd_list("bzip2", 9, "/tmp/x.rzidx"),
            Utility.get_compression_cmd_list("bzip2", 9),
        )

        # compress_stream(frame_size=None) picks the format default
        stats = {}
        sc.compress_stream(
            io.BytesIO(b"y" * 1000),
            io.BytesIO(),
            fmt="gzip",
            level=1,
            frame_size=None,
            threads=1,
            stats=stats,
        )
        self.assertEqual(stats["frame_size"], 1 * self.MiB)


class PrintStatsCliTest(unittest.TestCase):
    """P2b: --print-stats emits exactly one structured telemetry line."""

    _LINE_RE = (
        r"^\[seekable_compressor\] fmt=(gzip|zstd) level=-?\d+ backend=\S+ "
        r"workers=\d+ frame_size=\d+ frames=\d+ in_bytes=\d+ out_bytes=\d+ "
        r"ratio=\d+\.\d{2}% elapsed=\d+\.\d{3}s in_MBps=\d+\.\d{2} out_MBps=\d+\.\d{2}$"
    )

    def _run(self, fmt, extra_args, data, idx_path):
        script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "seekable_compressor.py"
        )
        return subprocess.run(
            [sys.executable, script, "--format", fmt, "--level", "3"]
            + ["--index-output", idx_path]
            + extra_args,
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def test_print_stats_line_and_silent_default(self):
        import re

        data = b"print stats payload " * 200000  # ~3.8 MiB
        # The last-resort zstd subprocess backend emits a one-time warning on stderr
        zstd_ok = shutil.which("zstd") and get_zstd_backend_name() != "subprocess"
        formats = ["gzip"] + (["zstd"] if zstd_ok else [])
        with tempfile.TemporaryDirectory() as td:
            for fmt in formats:
                with self.subTest(fmt=fmt):
                    idx = os.path.join(td, f"{fmt}.rzidx")
                    proc = self._run(fmt, ["--print-stats"], data, idx)
                    lines = proc.stderr.decode().splitlines()
                    self.assertEqual(len(lines), 1, lines)
                    self.assertRegex(lines[0], self._LINE_RE)
                    with open(idx, "rb") as f:
                        meta = parse_seekidx_bytes(f.read())
                    # --frame-size omitted -> format-specific default
                    expected_fs = 4 * 1024 * 1024 if fmt == "zstd" else 1024 * 1024
                    self.assertEqual(meta["frame_size"], expected_fs)
                    fields = dict(
                        kv.split("=", 1) for kv in lines[0].split()[1:] if "=" in kv
                    )
                    self.assertEqual(fields["fmt"], fmt)
                    self.assertEqual(fields["level"], "3")
                    self.assertEqual(int(fields["frame_size"]), expected_fs)
                    self.assertEqual(int(fields["frames"]), meta["frame_count"])
                    self.assertEqual(int(fields["in_bytes"]), len(data))
                    self.assertEqual(int(fields["out_bytes"]), len(proc.stdout))
                    self.assertTrue(re.match(r"^\d+$", fields["workers"]))

                    # Default (no --print-stats): stderr stays completely silent
                    quiet = self._run(fmt, [], data, idx)
                    self.assertEqual(quiet.stderr, b"")


if __name__ == "__main__":
    unittest.main()
