#!/usr/bin/env python3
# ----------------------------------------------------------------------
#   Copyright (C) 2026 Rescuezilla Seekable-Index Community Fork Contributors
#   Derived from Rescuezilla (Copyright (C) 2019-2025 Rescuezilla.com)
#   Modified 2026-11-28 (Community Fork): RAM-aware multi-core worker count,
#   native libdeflate gzip backend, format-specific default frame size, and
#   --print-stats telemetry (GPL-3.0-or-later).
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
(default 1 MiB for gzip, 4 MiB for zstd), compresses each chunk in parallel
(all usable CPUs, RAM-capped) as an independent,
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
import ctypes
import ctypes.util
import os
import signal
import struct
import subprocess
import sys
import threading
import time
import zlib
from typing import List, Optional, Tuple

try:
    from compression import zstd as _py_zstd  # Python 3.14+ stdlib
except ImportError:
    _py_zstd = None

try:
    import zstandard as _zstandard_pkg  # python3-zstandard if installed
except ImportError:
    _zstandard_pkg = None


_ZSTD_C_COMPRESSION_LEVEL = 100  # ZSTD_c_compressionLevel from zstd.h
_ZSTD_C_CHECKSUM_FLAG = (
    201  # ZSTD_c_checksumFlag from zstd.h (XXH64 per-frame checksum)
)


def _load_libzstd():
    """Load system libzstd.so.1 via ctypes (present on Ubuntu 24.04 Noble ISO)."""
    for libname in ("libzstd.so.1", "libzstd.so"):
        try:
            lib = ctypes.CDLL(libname)
            lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
            lib.ZSTD_compressBound.restype = ctypes.c_size_t
            lib.ZSTD_createCCtx.argtypes = []
            lib.ZSTD_createCCtx.restype = ctypes.c_void_p
            lib.ZSTD_freeCCtx.argtypes = [ctypes.c_void_p]
            lib.ZSTD_freeCCtx.restype = ctypes.c_size_t
            lib.ZSTD_CCtx_setParameter.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
            ]
            lib.ZSTD_CCtx_setParameter.restype = ctypes.c_size_t
            lib.ZSTD_compress2.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_size_t,
            ]
            lib.ZSTD_compress2.restype = ctypes.c_size_t
            lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
            lib.ZSTD_isError.restype = ctypes.c_uint
            lib.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
            lib.ZSTD_getErrorName.restype = ctypes.c_char_p
            return lib
        except (OSError, AttributeError):
            continue
    return None


_LIBZSTD = _load_libzstd()
_ZSTD_THREAD_LOCAL = threading.local()
_ZSTD_SUBPROC_WARNED = False


class _LibZstdThreadCtx:
    """Thread-local ZSTD_CCtx + reusable output buffer (GIL released during C call).

    Enables ZSTD_c_checksumFlag=1 (32-bit XXH64 content checksum per frame) to
    match stock `zstd` CLI integrity guarantees.
    """

    def __init__(self, lib):
        self._lib = lib
        self._cctx = lib.ZSTD_createCCtx()
        if not self._cctx:
            raise MemoryError("ZSTD_createCCtx() returned NULL")
        self._buf = None
        self._buf_cap = 0
        self._configured_level = None
        rc = int(lib.ZSTD_CCtx_setParameter(self._cctx, _ZSTD_C_CHECKSUM_FLAG, 1))
        if lib.ZSTD_isError(rc):
            lib.ZSTD_freeCCtx(self._cctx)
            self._cctx = None
            raise RuntimeError("libzstd failed to enable ZSTD_c_checksumFlag")

    def compress(self, data: bytes, level: int) -> bytes:
        level_int = int(level)
        if self._configured_level != level_int:
            rc_lvl = int(
                self._lib.ZSTD_CCtx_setParameter(
                    self._cctx, _ZSTD_C_COMPRESSION_LEVEL, level_int
                )
            )
            if self._lib.ZSTD_isError(rc_lvl):
                err_name = self._lib.ZSTD_getErrorName(rc_lvl)
                msg = (
                    err_name.decode("utf-8", errors="replace")
                    if err_name
                    else f"code={rc_lvl}"
                )
                raise RuntimeError(f"libzstd failed to set compression level: {msg}")
            self._configured_level = level_int

        src_len = len(data)
        bound = int(self._lib.ZSTD_compressBound(src_len))
        if self._buf is None or self._buf_cap < bound:
            self._buf = ctypes.create_string_buffer(bound)
            self._buf_cap = bound
        # Pass empty buffer safely when src_len == 0
        src_arg = data if src_len > 0 else b"\x00"
        rc = int(
            self._lib.ZSTD_compress2(self._cctx, self._buf, bound, src_arg, src_len)
        )
        if self._lib.ZSTD_isError(rc):
            err_name = self._lib.ZSTD_getErrorName(rc)
            msg = (
                err_name.decode("utf-8", errors="replace") if err_name else f"code={rc}"
            )
            raise RuntimeError(f"libzstd ZSTD_compress2 failed: {msg}")
        return ctypes.string_at(self._buf, rc)

    def __del__(self):
        cctx = getattr(self, "_cctx", None)
        lib = getattr(self, "_lib", None)
        if cctx and lib is not None:
            try:
                lib.ZSTD_freeCCtx(cctx)
            except Exception:
                pass
            self._cctx = None


def get_zstd_backend_name() -> str:
    """Return active zstd compression backend identifier."""
    if _py_zstd is not None:
        return "stdlib"
    if _zstandard_pkg is not None:
        return "zstandard"
    if _LIBZSTD is not None:
        return "libzstd"
    return "subprocess"


def _compress_zstd_frame_libzstd(data: bytes, level: int) -> bytes:
    """Compress one independent RFC 8878 zstd frame via native libzstd.so.1."""
    if _LIBZSTD is None:
        raise RuntimeError("libzstd.so.1 is not available")
    ctx = getattr(_ZSTD_THREAD_LOCAL, "libzstd_ctx", None)
    if ctx is None:
        ctx = _LibZstdThreadCtx(_LIBZSTD)
        _ZSTD_THREAD_LOCAL.libzstd_ctx = ctx
    return ctx.compress(data, level)


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
DEFAULT_ZSTD_FRAME_SIZE = 4 * 1024 * 1024  # 4 MiB (better zstd ratio/throughput)


def default_frame_size_for_format(fmt: str) -> int:
    """Return the default seekable frame size for a compression format."""
    if str(fmt).strip().lower() == "zstd":
        return DEFAULT_ZSTD_FRAME_SIZE
    return DEFAULT_FRAME_SIZE


# ----------------------------------------------------------------------
# Worker sizing (CPU affinity + RAM aware)
# ----------------------------------------------------------------------


def _get_available_cpu_count() -> int:
    """CPUs usable by this process (honours taskset / cgroup cpusets)."""
    try:
        n = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        n = os.cpu_count() or 1
    return max(1, int(n))


def _get_available_memory_bytes() -> Optional[int]:
    """Return MemAvailable (fallback MemTotal) from /proc/meminfo in bytes, or None."""
    try:
        with open("/proc/meminfo", "r", encoding="ascii", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    values = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("MemAvailable:", "MemTotal:"):
            try:
                kib = int(parts[1])
            except ValueError:
                continue
            if kib > 0:
                values[parts[0]] = kib * 1024
    if "MemAvailable:" in values:
        return values["MemAvailable:"]
    return values.get("MemTotal:")


def _estimate_worker_cctx_bytes(fmt: str, level: int) -> int:
    """Estimate per-worker compressor context RSS (based on Noble libzstd/libdeflate measurements)."""
    fmt_norm = str(fmt).strip().lower()
    lvl = int(level)
    if fmt_norm == "zstd":
        # Measured delta_maxrss on Ubuntu 24.04 Noble libzstd1 1.5.5 (4 MiB frame):
        # lvl 1-6: 4-7 MiB, lvl 9: ~15 MiB, lvl 12: ~45 MiB, lvl 15-19: ~53-69 MiB.
        if lvl <= 6:
            return 16 * 1024 * 1024
        if lvl <= 10:
            return 32 * 1024 * 1024
        if lvl <= 14:
            return 64 * 1024 * 1024
        return 96 * 1024 * 1024
    return 4 * 1024 * 1024


def default_worker_count(frame_size: int, fmt: str = "gzip", level: int = 6) -> int:
    """Number of compression threads: all usable CPUs, capped by available RAM.

    Accounts for both in-flight frame buffers (frame_size * 8) and per-worker
    compressor context state (e.g. high-level zstd ZSTD_CCtx hash/chain tables).
    """
    workers = _get_available_cpu_count()
    mem_avail = _get_available_memory_bytes()
    if mem_avail is not None:
        fs = max(1, int(frame_size))
        cctx_bytes = _estimate_worker_cctx_bytes(fmt, level)
        per_worker_budget = max(fs * 8, fs * 4 + cctx_bytes * 2)
        workers = min(workers, max(1, mem_avail // per_worker_budget))
    return max(1, int(workers))


def default_max_in_flight(workers: int, frame_size: int) -> int:
    """Bound on queued frames (raw + compressed ~= 2x frame_size each).

    Enough queue depth to avoid head-of-line worker starvation on 16-32T CPUs,
    while keeping in-flight buffers <= half of available RAM (minimum 2 frames).
    """
    workers = max(1, int(workers))
    desired = min(workers * 2, workers + 16)
    mem_avail = _get_available_memory_bytes()
    if mem_avail is not None:
        mem_cap = (mem_avail // 2) // max(1, int(frame_size) * 2)
        return max(2, min(desired, mem_cap))
    return max(2, desired)


# ----------------------------------------------------------------------
# gzip backends: native libdeflate.so.0 (GIL-free) -> Python zlib fallback
# ----------------------------------------------------------------------


def _load_libdeflate():
    """Load system libdeflate.so.0 via ctypes (libdeflate0 on Ubuntu 24.04 Noble ISO)."""
    candidates = ["libdeflate.so.0"]
    found = ctypes.util.find_library("deflate")
    if found and found not in candidates:
        candidates.append(found)
    for libname in candidates:
        try:
            lib = ctypes.CDLL(libname)
            lib.libdeflate_alloc_compressor.argtypes = [ctypes.c_int]
            lib.libdeflate_alloc_compressor.restype = ctypes.c_void_p
            lib.libdeflate_free_compressor.argtypes = [ctypes.c_void_p]
            lib.libdeflate_free_compressor.restype = None
            lib.libdeflate_gzip_compress_bound.argtypes = [
                ctypes.c_void_p,
                ctypes.c_size_t,
            ]
            lib.libdeflate_gzip_compress_bound.restype = ctypes.c_size_t
            lib.libdeflate_gzip_compress.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_size_t,
            ]
            lib.libdeflate_gzip_compress.restype = ctypes.c_size_t
            return lib
        except (OSError, AttributeError):
            continue
    return None


_LIBDEFLATE = _load_libdeflate()
_GZIP_THREAD_LOCAL = threading.local()


class _LibDeflateCompressor:
    """RAII wrapper around one `libdeflate_compressor*` (one per thread & level).

    Holds a direct reference to libdeflate_free_compressor so __del__ works even
    during interpreter shutdown after module globals have been cleared.
    """

    def __init__(self, lib, level: int):
        self._free = lib.libdeflate_free_compressor
        self._bound_fn = lib.libdeflate_gzip_compress_bound
        self._compress_fn = lib.libdeflate_gzip_compress
        self._handle = lib.libdeflate_alloc_compressor(int(level))
        if not self._handle:
            raise RuntimeError(
                f"libdeflate_alloc_compressor({int(level)}) returned NULL"
            )
        self._buf = None
        self._buf_cap = 0

    def compress(self, data: bytes) -> bytes:
        src_len = len(data)
        bound = int(self._bound_fn(self._handle, src_len))
        if bound <= 0:
            raise RuntimeError("libdeflate_gzip_compress_bound returned 0")
        if self._buf is None or self._buf_cap < bound:
            self._buf = ctypes.create_string_buffer(bound)
            self._buf_cap = bound
        # Pass a valid non-NULL pointer when src_len == 0 (empty gzip member)
        src_arg = data if src_len > 0 else b"\x00"
        n = int(self._compress_fn(self._handle, src_arg, src_len, self._buf, bound))
        if n <= 0 or n > bound:
            raise RuntimeError(f"libdeflate_gzip_compress failed (returned {n})")
        return ctypes.string_at(self._buf, n)

    def __del__(self):
        handle = getattr(self, "_handle", None)
        free_fn = getattr(self, "_free", None)
        if handle and free_fn is not None:
            try:
                free_fn(handle)
            except Exception:
                pass
            self._handle = None


def get_gzip_backend_name() -> str:
    """Return active gzip compression backend identifier."""
    return "libdeflate" if _LIBDEFLATE is not None else "zlib"


def _compress_gzip_frame_libdeflate(data: bytes, level: int) -> bytes:
    """Compress one independent RFC 1952 gzip member via native libdeflate.so.0."""
    lib = _LIBDEFLATE
    if lib is None:
        raise RuntimeError("libdeflate.so.0 is not available")
    lvl = max(0, min(12, int(level)))
    cache = getattr(_GZIP_THREAD_LOCAL, "libdeflate", None)
    if cache is None:
        cache = {}
        _GZIP_THREAD_LOCAL.libdeflate = cache
    comp = cache.get(lvl)
    if comp is None:
        comp = _LibDeflateCompressor(lib, lvl)
        cache[lvl] = comp
    return comp.compress(data)


def _compress_gzip_frame_zlib(data: bytes, level: int) -> bytes:
    lvl = max(0, min(9, int(level)))
    comp = zlib.compressobj(lvl, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    return comp.compress(data) + comp.flush()


def compress_gzip_frame(data: bytes, level: int = 6) -> bytes:
    """Compress a single independent RFC 1952 gzip member.

    Uses native libdeflate (GIL released during the C call) when available,
    falling back cleanly to Python zlib if libdeflate is missing or fails.
    """
    if _LIBDEFLATE is not None:
        try:
            return _compress_gzip_frame_libdeflate(data, level)
        except (RuntimeError, MemoryError):
            pass
    return _compress_gzip_frame_zlib(data, level)


def compress_zstd_frame(data: bytes, level: int) -> bytes:
    """Compress a single independent RFC 8878 zstd frame with content checksum enabled.

    Backend priority (zero subprocess spawn on Ubuntu 24.04 Noble & Python 3.14+):
    1. Python 3.14+ stdlib compression.zstd
    2. python3-zstandard (if installed)
    3. Native libzstd.so.1 via ctypes (thread-local ZSTD_CCtx, GIL-free)
    4. zstd CLI subprocess (last-resort fallback)
    """
    global _ZSTD_SUBPROC_WARNED
    if _py_zstd is not None:
        return _py_zstd.compress(
            data,
            options={
                _py_zstd.CompressionParameter.compression_level: int(level),
                _py_zstd.CompressionParameter.checksum_flag: 1,
            },
        )
    if _zstandard_pkg is not None:
        cache = getattr(_ZSTD_THREAD_LOCAL, "zstandard_cctx", None)
        if cache is None or cache[0] != level:
            cache = (
                level,
                _zstandard_pkg.ZstdCompressor(level=level, write_checksum=True),
            )
            _ZSTD_THREAD_LOCAL.zstandard_cctx = cache
        return cache[1].compress(data)
    if _LIBZSTD is not None:
        return _compress_zstd_frame_libzstd(data, level)

    if not _ZSTD_SUBPROC_WARNED:
        _ZSTD_SUBPROC_WARNED = True
        sys.stderr.write(
            "seekable_compressor warning: no native zstd library found; falling back to per-frame zstd subprocess\n"
        )
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
            raise BlockingIOError("Non-blocking stdout stream returned None on write")
        if n == 0:
            raise BrokenPipeError("Short write to stdout")
        written += n


def compress_stream(
    stdin_bin,
    stdout_bin,
    *,
    fmt: str,
    level: int,
    frame_size: Optional[int] = None,
    index_output: Optional[str] = None,
    threads: Optional[int] = None,
    stats: Optional[dict] = None,
) -> Tuple[int, int, int]:
    """Compress stdin -> stdout in independent frames and write atomic .rzidx index.

    `frame_size=None` selects default_frame_size_for_format(fmt) and
    `threads=None/0` selects default_worker_count(frame_size, fmt=..., level=...). If `stats` is a
    dict it is filled with fmt/level/backend/workers/frame_size.

    Returns (frame_count, total_compressed_bytes, total_uncompressed_bytes).
    """
    fmt_norm = fmt.strip().lower()
    if fmt_norm == "gzip":
        codec = CODEC_GZIP
        compress_fn = compress_gzip_frame
        backend = get_gzip_backend_name()
    elif fmt_norm == "zstd":
        codec = CODEC_ZSTD
        compress_fn = compress_zstd_frame
        backend = get_zstd_backend_name()
    else:
        raise ValueError(f"Unsupported seekable compression format: {fmt}")

    if frame_size is None:
        frame_size = default_frame_size_for_format(fmt_norm)
    frame_size = int(frame_size)
    if frame_size < 65536:
        raise ValueError(f"frame_size too small: {frame_size}")

    # Use all usable CPUs, but keep bounded memory on low-RAM Live USB environments
    workers = (
        max(1, int(threads))
        if threads
        else default_worker_count(frame_size, fmt=fmt_norm, level=level)
    )
    max_in_flight = default_max_in_flight(workers, frame_size)
    if stats is not None:
        stats.update(
            fmt=fmt_norm,
            level=int(level),
            backend=backend,
            workers=workers,
            max_in_flight=max_in_flight,
            frame_size=frame_size,
        )

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


STATS_LINE_PREFIX = "[seekable_compressor]"


def format_stats_line(
    stats: dict, frames: int, in_bytes: int, out_bytes: int, elapsed: float
) -> str:
    """Single-line telemetry summary (MBps = 10^6 bytes per second)."""
    ratio = (100.0 * out_bytes / in_bytes) if in_bytes > 0 else 0.0
    secs = max(float(elapsed), 1e-9)
    return (
        f"{STATS_LINE_PREFIX} fmt={stats.get('fmt')} level={stats.get('level')} "
        f"backend={stats.get('backend')} workers={stats.get('workers')} "
        f"frame_size={stats.get('frame_size')} frames={frames} "
        f"in_bytes={in_bytes} out_bytes={out_bytes} ratio={ratio:.2f}% "
        f"elapsed={float(elapsed):.3f}s in_MBps={in_bytes / 1e6 / secs:.2f} "
        f"out_MBps={out_bytes / 1e6 / secs:.2f}"
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seekable multi-frame gzip/zstd compressor with .rzidx generation for Rescuezilla"
    )
    parser.add_argument("--format", required=True, choices=["gzip", "zstd"])
    parser.add_argument("--level", type=int, default=6)
    parser.add_argument(
        "--frame-size",
        type=int,
        default=None,
        help="Frame size in bytes (default: 4 MiB for zstd, 1 MiB for gzip)",
    )
    parser.add_argument("--index-output", type=str, default=None)
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument(
        "--print-stats",
        action="store_true",
        default=False,
        help="Write one [seekable_compressor] summary line to stderr on success",
    )
    args = parser.parse_args(argv)
    frame_size = (
        args.frame_size
        if args.frame_size is not None
        else default_frame_size_for_format(args.format)
    )

    def _on_signal(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        stats: dict = {}
        t0 = time.monotonic()
        frames, out_bytes, in_bytes = compress_stream(
            sys.stdin.buffer,
            sys.stdout.buffer,
            fmt=args.format,
            level=args.level,
            frame_size=frame_size,
            index_output=args.index_output,
            threads=args.threads,
            stats=stats,
        )
        if args.print_stats:
            elapsed = time.monotonic() - t0
            sys.stderr.write(
                format_stats_line(stats, frames, in_bytes, out_bytes, elapsed) + "\n"
            )
            sys.stderr.flush()
        return 0
    except BrokenPipeError:
        return 141
    except Exception as exc:
        sys.stderr.write(f"seekable_compressor error: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
