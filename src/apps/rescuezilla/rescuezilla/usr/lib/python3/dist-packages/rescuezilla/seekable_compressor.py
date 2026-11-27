#!/usr/bin/env python3
# ----------------------------------------------------------------------
#   Copyright (C) 2026 Rescuezilla Seekable-Index Community Fork Contributors
#   Derived from Rescuezilla (Copyright (C) 2019-2025 Rescuezilla.com)
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
"""Seekable multi-frame stream compressor for Rescuezilla backups.

Reads an uncompressed Partclone stream from stdin in fixed-size chunks
(default 1 MiB), compresses each chunk in parallel as an independent,
RFC-compliant gzip (RFC 1952) or zstd (RFC 8878) frame, writes the
concatenated frames strictly in order to stdout (piped to `split`), and
atomically writes a compact binary seek index (`Info-seekidx-<part>.rzidx`).

Backward compatibility guarantee:
- Concatenated independent gzip members are 100% standard RFC 1952 gzip
  streams (decompressible by stock `gzip -d`, `pigz -d`, Rescuezilla, and
  Clonezilla without any modification).
- Concatenated independent zstd frames are 100% standard RFC 8878 zstd
  streams (decompressible by stock `zstd -d`, Rescuezilla, and Clonezilla).
- Index size is ~16 MiB per 1 TiB uncompressed (16 bytes per 1 MiB frame),
  giving 16x finer random-access granularity than 16 MiB frames with <0.05%
  compressed size overhead.
"""

import argparse
import collections
from concurrent.futures import ThreadPoolExecutor
import os
import signal
import struct
import subprocess
import sys
import zlib
from typing import List, Optional, Tuple

try:
    from compression import zstd as _py_zstd  # Python 3.14+ stdlib
except ImportError:
    _py_zstd = None

SEEKIDX_MAGIC = b"RZSKIDX1"
SEEKIDX_VERSION = 1
CODEC_GZIP = 1
CODEC_ZSTD = 2

_HEADER_STRUCT = struct.Struct("<8sIIIIQQ")  # 40 bytes
_ENTRY_STRUCT = struct.Struct("<QQ")  # 16 bytes
_CRC_STRUCT = struct.Struct("<I")  # 4 bytes

DEFAULT_FRAME_SIZE = (
    1 * 1024 * 1024
)  # 1 MiB (fine random-access granularity, <0.05% overhead)


def compress_gzip_frame(data: bytes, level: int) -> bytes:
    """Compress a single independent RFC 1952 gzip member (releases GIL in zlib C)."""
    comp = zlib.compressobj(level, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    return comp.compress(data) + comp.flush()


def compress_zstd_frame(data: bytes, level: int) -> bytes:
    """Compress a single independent RFC 8878 zstd frame."""
    if _py_zstd is not None:
        return _py_zstd.compress(data, level=level)
    proc = subprocess.run(
        ["zstd", "--stdout", f"-{level}", "--threads=1", "--quiet"],
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"zstd frame compression failed (rc={proc.returncode}): {err}"
        )
    return proc.stdout


def serialize_seekidx(
    codec: int,
    frame_size: int,
    entries: List[Tuple[int, int]],
    total_compressed: int,
    total_uncompressed: int,
) -> bytes:
    """Serialize the RZSKIDX1 binary seek index with trailing IEEE CRC32."""
    buf = bytearray()
    buf.extend(
        _HEADER_STRUCT.pack(
            SEEKIDX_MAGIC,
            SEEKIDX_VERSION,
            codec,
            frame_size,
            len(entries),
            total_compressed,
            total_uncompressed,
        )
    )
    for comp_off, decomp_off in entries:
        buf.extend(_ENTRY_STRUCT.pack(comp_off, decomp_off))
    # Final sentinel entry allows exact length computation for the last frame
    buf.extend(_ENTRY_STRUCT.pack(total_compressed, total_uncompressed))
    crc = zlib.crc32(buf) & 0xFFFFFFFF
    buf.extend(_CRC_STRUCT.pack(crc))
    return bytes(buf)


def parse_seekidx_bytes(raw: bytes) -> dict:
    """Parse and CRC-verify an RZSKIDX1 binary seek index."""
    min_len = _HEADER_STRUCT.size + _ENTRY_STRUCT.size + _CRC_STRUCT.size
    if len(raw) < min_len:
        raise ValueError("Seek index file is truncated")
    body = raw[:-4]
    stored_crc = _CRC_STRUCT.unpack_from(raw, len(raw) - 4)[0]
    calc_crc = zlib.crc32(body) & 0xFFFFFFFF
    if stored_crc != calc_crc:
        raise ValueError(
            f"Seek index CRC32 mismatch: expected 0x{calc_crc:08x}, got 0x{stored_crc:08x}"
        )
    magic, version, codec, frame_size, frame_count, total_comp, total_decomp = (
        _HEADER_STRUCT.unpack_from(body, 0)
    )
    if magic != SEEKIDX_MAGIC:
        raise ValueError(f"Invalid seek index magic: {magic!r}")
    if version != SEEKIDX_VERSION:
        raise ValueError(f"Unsupported seek index version: {version}")
    expected_len = _HEADER_STRUCT.size + (frame_count + 1) * _ENTRY_STRUCT.size
    if len(body) != expected_len:
        raise ValueError(f"Seek index length mismatch: {len(body)} != {expected_len}")

    entries: List[Tuple[int, int]] = []
    pos = _HEADER_STRUCT.size
    for _ in range(frame_count + 1):
        entries.append(_ENTRY_STRUCT.unpack_from(body, pos))
        pos += _ENTRY_STRUCT.size

    return {
        "version": version,
        "codec": codec,
        "codec_name": "gzip"
        if codec == CODEC_GZIP
        else ("zstd" if codec == CODEC_ZSTD else "unknown"),
        "frame_size": frame_size,
        "frame_count": frame_count,
        "total_compressed": total_comp,
        "total_uncompressed": total_decomp,
        "entries": entries,  # length = frame_count + 1 (includes sentinel)
    }


def _read_exact_chunk(stream, size: int) -> bytes:
    """Read up to `size` bytes from a binary stream, coalescing short pipe reads."""
    parts: List[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        parts.append(chunk)
        remaining -= len(chunk)
    if not parts:
        return b""
    if len(parts) == 1:
        return parts[0]
    return b"".join(parts)


def _write_all(stream, data: bytes) -> None:
    mv = memoryview(data)
    written = 0
    total = len(mv)
    while written < total:
        n = stream.write(mv[written:])
        if n is None:
            return
        if n == 0:
            raise BrokenPipeError("Short write to stdout")
        written += n


def compress_stream(
    stdin_bin,
    stdout_bin,
    *,
    fmt: str,
    level: int,
    frame_size: int = DEFAULT_FRAME_SIZE,
    index_output: Optional[str] = None,
    threads: Optional[int] = None,
) -> Tuple[int, int, int]:
    """Compress stdin -> stdout in independent frames and write atomic .rzidx index.

    Returns (frame_count, total_compressed_bytes, total_uncompressed_bytes).
    """
    fmt_norm = fmt.strip().lower()
    if fmt_norm == "gzip":
        codec = CODEC_GZIP
        compress_fn = compress_gzip_frame
    elif fmt_norm == "zstd":
        codec = CODEC_ZSTD
        compress_fn = compress_zstd_frame
    else:
        raise ValueError(f"Unsupported seekable compression format: {fmt}")

    if frame_size < 65536:
        raise ValueError(f"frame_size too small: {frame_size}")

    # Keep bounded memory even on low-RAM Live USB environments
    cpu_cnt = os.cpu_count() or 1
    workers = max(1, int(threads) if threads else min(cpu_cnt, 8))
    max_in_flight = workers + 2

    tmp_index: Optional[str] = None
    if index_output:
        idx_dir = os.path.dirname(os.path.abspath(index_output))
        if idx_dir:
            os.makedirs(idx_dir, exist_ok=True)
        tmp_index = f"{index_output}.{os.getpid()}.tmp"

    entries: List[Tuple[int, int]] = []
    comp_offset = 0
    decomp_offset = 0
    pending: collections.deque = collections.deque()

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while True:
                raw_chunk = _read_exact_chunk(stdin_bin, frame_size)
                if not raw_chunk:
                    break
                fut = pool.submit(compress_fn, raw_chunk, level)
                pending.append((len(raw_chunk), fut))

                while len(pending) >= max_in_flight:
                    raw_len, done_fut = pending.popleft()
                    comp_bytes = done_fut.result()
                    _write_all(stdout_bin, comp_bytes)
                    entries.append((comp_offset, decomp_offset))
                    comp_offset += len(comp_bytes)
                    decomp_offset += raw_len

            while pending:
                raw_len, done_fut = pending.popleft()
                comp_bytes = done_fut.result()
                _write_all(stdout_bin, comp_bytes)
                entries.append((comp_offset, decomp_offset))
                comp_offset += len(comp_bytes)
                decomp_offset += raw_len

        # Emit one empty valid member if input stream was 0 bytes
        if not entries:
            empty_frame = compress_fn(b"", level)
            _write_all(stdout_bin, empty_frame)
            entries.append((0, 0))
            comp_offset = len(empty_frame)
            decomp_offset = 0

        stdout_bin.flush()

        if index_output and tmp_index:
            blob = serialize_seekidx(
                codec=codec,
                frame_size=frame_size,
                entries=entries,
                total_compressed=comp_offset,
                total_uncompressed=decomp_offset,
            )
            with open(tmp_index, "wb") as f:
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_index, index_output)
            tmp_index = None

        return len(entries), comp_offset, decomp_offset
    finally:
        if tmp_index and os.path.exists(tmp_index):
            try:
                os.remove(tmp_index)
            except OSError:
                pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seekable multi-frame gzip/zstd compressor with .rzidx generation for Rescuezilla"
    )
    parser.add_argument("--format", required=True, choices=["gzip", "zstd"])
    parser.add_argument("--level", type=int, default=6)
    parser.add_argument("--frame-size", type=int, default=DEFAULT_FRAME_SIZE)
    parser.add_argument("--index-output", type=str, default=None)
    parser.add_argument("--threads", type=int, default=None)
    args = parser.parse_args(argv)

    def _on_signal(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        compress_stream(
            sys.stdin.buffer,
            sys.stdout.buffer,
            fmt=args.format,
            level=args.level,
            frame_size=args.frame_size,
            index_output=args.index_output,
            threads=args.threads,
        )
        return 0
    except BrokenPipeError:
        return 141
    except Exception as exc:
        sys.stderr.write(f"seekable_compressor error: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
