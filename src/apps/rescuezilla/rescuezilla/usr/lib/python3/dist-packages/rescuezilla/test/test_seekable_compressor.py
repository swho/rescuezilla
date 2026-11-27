# ----------------------------------------------------------------------
#   Copyright (C) 2026 Rescuezilla Seekable-Index Community Fork Contributors
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


if __name__ == "__main__":
    unittest.main()
