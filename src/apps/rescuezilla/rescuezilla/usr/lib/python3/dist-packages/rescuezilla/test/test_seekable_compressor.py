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
    compress_stream,
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
        self.assertFalse(fnmatch.fnmatch(idx_name, "nvme0n1p2[.]*-ptcl-img[.][a-zA-Z]*[.][a-zA-Z][a-zA-Z]"))

        with tempfile.TemporaryDirectory() as td:
            idx_path = os.path.join(td, idx_name)
            cmd = Utility.get_seekable_compression_cmd_list("gzip", 3, idx_path, frame_size=65536)
            data = b"hello world " * 20000
            proc = subprocess.run(cmd, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            self.assertTrue(os.path.isfile(idx_path))
            with open(idx_path, "rb") as f:
                meta = parse_seekidx_bytes(f.read())
            self.assertEqual(meta["total_uncompressed"], len(data))
            self.assertEqual(meta["total_compressed"], len(proc.stdout))


if __name__ == "__main__":
    unittest.main()
