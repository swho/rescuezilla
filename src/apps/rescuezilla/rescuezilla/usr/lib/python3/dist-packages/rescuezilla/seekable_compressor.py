#!/usr/bin/env python3
# ----------------------------------------------------------------------
#   Copyright (C) 2026 Rescuezilla Seekable-Index Community Fork Contributors
#   Derived from Rescuezilla (Copyright (C) 2019-2025 Rescuezilla.com)
#   Modified 2026-11-28 (Community Fork): RAM-aware multi-core worker count,
#   native libdeflate gzip backend, format-specific default frame size, and
#   --print-stats telemetry (GPL-3.0-or-later).
#   Modified 2026-11-28 (Community Fork): native libzstd/libdeflate/zlib
#   decompression helpers, reusable SeekableFrameWriter/SeekableSplitStream,
#   and optional --bhash-output block-hash index (.rzbhash) generation.
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
import bisect
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


def has_native_zstd() -> bool:
    """True when an in-process zstd compressor (stdlib/zstandard/libzstd) is available."""
    return (
        _py_zstd is not None or _zstandard_pkg is not None or _LIBZSTD is not None
    )


# ----------------------------------------------------------------------
# Native decompression helpers (libzstd.so.1 / libdeflate.so.0 / zlib /
# Python 3.14+ compression.zstd) -- no external pip packages or subprocesses.
# ----------------------------------------------------------------------


class StreamDecompressError(ValueError):
    """Raised on corrupt, truncated, or trailing-garbage compressed input."""


class _ZstdInBuffer(ctypes.Structure):
    _fields_ = [
        ("src", ctypes.c_void_p),
        ("size", ctypes.c_size_t),
        ("pos", ctypes.c_size_t),
    ]


class _ZstdOutBuffer(ctypes.Structure):
    _fields_ = [
        ("dst", ctypes.c_void_p),
        ("size", ctypes.c_size_t),
        ("pos", ctypes.c_size_t),
    ]


def _load_libzstd_decompress():
    """Load libzstd.so.1 decompression symbols via ctypes."""
    for libname in ("libzstd.so.1", "libzstd.so"):
        try:
            lib = ctypes.CDLL(libname)
            lib.ZSTD_decompress.argtypes = [
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_size_t,
            ]
            lib.ZSTD_decompress.restype = ctypes.c_size_t
            lib.ZSTD_createDCtx.argtypes = []
            lib.ZSTD_createDCtx.restype = ctypes.c_void_p
            lib.ZSTD_freeDCtx.argtypes = [ctypes.c_void_p]
            lib.ZSTD_freeDCtx.restype = ctypes.c_size_t
            lib.ZSTD_decompressStream.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_ZstdOutBuffer),
                ctypes.POINTER(_ZstdInBuffer),
            ]
            lib.ZSTD_decompressStream.restype = ctypes.c_size_t
            lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
            lib.ZSTD_isError.restype = ctypes.c_uint
            lib.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
            lib.ZSTD_getErrorName.restype = ctypes.c_char_p
            return lib
        except (OSError, AttributeError):
            continue
    return None


def _load_libdeflate_decompress():
    """Load libdeflate.so.0 gzip decompression symbols via ctypes."""
    candidates = ["libdeflate.so.0"]
    found = ctypes.util.find_library("deflate")
    if found and found not in candidates:
        candidates.append(found)
    for libname in candidates:
        try:
            lib = ctypes.CDLL(libname)
            lib.libdeflate_alloc_decompressor.argtypes = []
            lib.libdeflate_alloc_decompressor.restype = ctypes.c_void_p
            lib.libdeflate_free_decompressor.argtypes = [ctypes.c_void_p]
            lib.libdeflate_free_decompressor.restype = None
            lib.libdeflate_gzip_decompress_ex.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.POINTER(ctypes.c_size_t),
                ctypes.POINTER(ctypes.c_size_t),
            ]
            lib.libdeflate_gzip_decompress_ex.restype = ctypes.c_int
            return lib
        except (OSError, AttributeError):
            continue
    return None


_LIBZSTD_D = _load_libzstd_decompress()
_LIBDEFLATE_D = _load_libdeflate_decompress()
_DECOMP_THREAD_LOCAL = threading.local()


def _zstd_error_name(lib, rc: int) -> str:
    name = lib.ZSTD_getErrorName(rc)
    return name.decode("utf-8", errors="replace") if name else f"code={rc}"


def _thread_out_buffer(attr: str, size: int):
    """Return a reusable thread-local ctypes buffer of at least `size` bytes."""
    buf = getattr(_DECOMP_THREAD_LOCAL, attr, None)
    if buf is None or len(buf) < size:
        buf = ctypes.create_string_buffer(max(1, size))
        setattr(_DECOMP_THREAD_LOCAL, attr, buf)
    return buf


class _LibDeflateDecompressor:
    """RAII wrapper around one `libdeflate_decompressor*` (one per thread)."""

    def __init__(self, lib):
        self._free = lib.libdeflate_free_decompressor
        self._fn = lib.libdeflate_gzip_decompress_ex
        self._handle = lib.libdeflate_alloc_decompressor()
        if not self._handle:
            raise MemoryError("libdeflate_alloc_decompressor() returned NULL")

    def decompress(self, data: bytes, size: int) -> bytes:
        out = _thread_out_buffer("libdeflate_out", size)
        actual_in = ctypes.c_size_t(0)
        actual_out = ctypes.c_size_t(0)
        src = data if len(data) > 0 else b"\x00"
        rc = int(
            self._fn(
                self._handle,
                src,
                len(data),
                out,
                size,
                ctypes.byref(actual_in),
                ctypes.byref(actual_out),
            )
        )
        if rc != 0:
            raise StreamDecompressError(
                f"libdeflate gzip decompression failed (result={rc})"
            )
        if actual_in.value != len(data):
            raise StreamDecompressError(
                f"gzip frame has {len(data) - actual_in.value} trailing bytes"
            )
        if actual_out.value != size:
            raise StreamDecompressError(
                f"gzip frame size mismatch: {actual_out.value} != {size}"
            )
        return ctypes.string_at(out, size)

    def __del__(self):
        handle = getattr(self, "_handle", None)
        free_fn = getattr(self, "_free", None)
        if handle and free_fn is not None:
            try:
                free_fn(handle)
            except Exception:
                pass
            self._handle = None


_DECOMPRESS_ALL_STEP = 64 * 1024 * 1024


def _decompress_all(fmt: str, data: bytes, max_output: Optional[int] = None) -> bytes:
    """Decode a whole multi-frame stream; with `max_output`, never buffer more
    than `max_output + 1` bytes and raise StreamDecompressError when exceeded."""
    dec = StreamDecompressor(fmt)
    out = bytearray()

    def _cap() -> int:
        if max_output is None:
            return _DECOMPRESS_ALL_STEP
        return max(1, min(_DECOMPRESS_ALL_STEP, max_output - len(out) + 1))

    def _append(piece: bytes) -> None:
        out.extend(piece)
        if max_output is not None and len(out) > max_output:
            raise StreamDecompressError(
                f"{fmt} stream decompresses to more than {max_output} bytes"
            )

    _append(dec.decompress(data, _cap()))
    while not dec.needs_input:
        piece = dec.decompress(b"", _cap())
        if not piece:
            break
        _append(piece)
    dec.finish()
    return bytes(out)


def decompress_zstd_frame(
    data: bytes,
    uncompressed_size: Optional[int] = None,
    max_output: Optional[int] = None,
) -> bytes:
    """Decompress independent RFC 8878 zstd frame(s), verifying XXH64 checksums.

    When `uncompressed_size` is given (e.g. from a `.rzidx` entry) the output
    length must match exactly. Otherwise `max_output` (if set) bounds the
    decoded size: larger output raises StreamDecompressError while decoding
    (zip-bomb guard). Backend: libzstd.so.1 (GIL-free) -> Python 3.14+ stdlib
    compression.zstd.
    """
    data = bytes(data)
    if uncompressed_size is None:
        return _decompress_all("zstd", data, max_output)
    if max_output is not None and int(uncompressed_size) > max_output:
        raise StreamDecompressError(
            f"zstd frame size {uncompressed_size} exceeds limit {max_output}"
        )
    size = int(uncompressed_size)
    if _LIBZSTD_D is not None:
        lib = _LIBZSTD_D
        out = _thread_out_buffer("zstd_out", size)
        src = data if len(data) > 0 else b"\x00"
        rc = int(lib.ZSTD_decompress(out, size, src, len(data)))
        if lib.ZSTD_isError(rc):
            raise StreamDecompressError(
                f"libzstd ZSTD_decompress failed: {_zstd_error_name(lib, rc)}"
            )
        if rc != size:
            raise StreamDecompressError(f"zstd frame size mismatch: {rc} != {size}")
        return ctypes.string_at(out, size)
    if _py_zstd is not None:
        try:
            out_b = _py_zstd.decompress(data)
        except _py_zstd.ZstdError as exc:
            raise StreamDecompressError(f"zstd decompression failed: {exc}") from exc
        if len(out_b) != size:
            raise StreamDecompressError(
                f"zstd frame size mismatch: {len(out_b)} != {size}"
            )
        return out_b
    raise RuntimeError("No native zstd decompression backend available")


def decompress_gzip_frame(data: bytes, uncompressed_size: Optional[int] = None) -> bytes:
    """Decompress exactly one RFC 1952 gzip member (CRC32 + ISIZE verified).

    Uses native libdeflate.so.0 when the size is known, otherwise Python zlib.
    """
    data = bytes(data)
    if uncompressed_size is not None and _LIBDEFLATE_D is not None:
        dec = getattr(_DECOMP_THREAD_LOCAL, "libdeflate_dec", None)
        if dec is None:
            dec = _LibDeflateDecompressor(_LIBDEFLATE_D)
            _DECOMP_THREAD_LOCAL.libdeflate_dec = dec
        return dec.decompress(data, int(uncompressed_size))
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(data)
    except zlib.error as exc:
        raise StreamDecompressError(f"gzip decompression failed: {exc}") from exc
    if not d.eof:
        raise StreamDecompressError("gzip frame is truncated")
    if d.unused_data:
        raise StreamDecompressError(
            f"gzip frame has {len(d.unused_data)} trailing bytes"
        )
    if uncompressed_size is not None and len(out) != int(uncompressed_size):
        raise StreamDecompressError(
            f"gzip frame size mismatch: {len(out)} != {uncompressed_size}"
        )
    return out


class StreamDecompressor:
    """Incremental multi-frame decompressor with bounded output.

    API mirrors `lzma.LZMADecompressor`: `decompress(data, max_length)` returns at
    most `max_length` bytes and retains unconsumed input internally;
    `needs_input` is False while buffered input/output remains. `finish()` raises
    `StreamDecompressError` if the stream ended inside a frame.
    Supports concatenated gzip members (zlib) and zstd frames (libzstd.so.1 or
    Python 3.14+ compression.zstd).
    """

    def __init__(self, fmt: str):
        fmt_norm = str(fmt).strip().lower()
        if fmt_norm not in ("gzip", "zstd"):
            raise ValueError(f"Unsupported stream decompression format: {fmt}")
        self._fmt = fmt_norm
        self._in = b""
        self._in_frame = False
        self._maybe_more = False
        self._obj = None
        self._dctx = None
        self._outbuf = None
        self._outcap = 0
        if fmt_norm == "zstd":
            if _LIBZSTD_D is not None:
                self._dctx = _LIBZSTD_D.ZSTD_createDCtx()
                if not self._dctx:
                    raise MemoryError("ZSTD_createDCtx() returned NULL")
            elif _py_zstd is None:
                raise RuntimeError("No native zstd decompression backend available")

    @property
    def needs_input(self) -> bool:
        return not self._in and not self._maybe_more

    def decompress(self, data: bytes, max_length: int) -> bytes:
        if data:
            self._in = self._in + bytes(data) if self._in else bytes(data)
        max_length = max(1, int(max_length))
        try:
            if self._fmt == "gzip":
                out = self._decompress_gzip(max_length)
            elif self._dctx is not None:
                out = self._decompress_libzstd(max_length)
            else:
                out = self._decompress_pyzstd(max_length)
        except zlib.error as exc:
            raise StreamDecompressError(f"gzip decompression failed: {exc}") from exc
        self._maybe_more = len(out) >= max_length
        return out

    def _decompress_gzip(self, max_length: int) -> bytes:
        out = bytearray()
        while len(out) < max_length:
            if self._obj is None:
                if not self._in:
                    break
                self._obj = zlib.decompressobj(16 + zlib.MAX_WBITS)
                self._in_frame = True
            src = self._in
            self._in = b""
            piece = self._obj.decompress(src, max_length - len(out))
            out += piece
            if self._obj.eof:
                self._in = self._obj.unused_data
                self._obj = None
                self._in_frame = False
            else:
                self._in = self._obj.unconsumed_tail
                if not piece and not self._in:
                    break
        return bytes(out)

    def _decompress_libzstd(self, max_length: int) -> bytes:
        lib = _LIBZSTD_D
        if self._outbuf is None or self._outcap < max_length:
            self._outbuf = ctypes.create_string_buffer(max_length)
            self._outcap = max_length
        src = self._in  # keeps the bytes object (and its buffer) alive below
        src_ptr = ctypes.cast(ctypes.c_char_p(src), ctypes.c_void_p).value
        inb = _ZstdInBuffer(src_ptr, len(src), 0)
        outb = _ZstdOutBuffer(ctypes.addressof(self._outbuf), max_length, 0)
        while outb.pos < outb.size:
            prev_in, prev_out = inb.pos, outb.pos
            if inb.pos == inb.size and not self._in_frame:
                break
            rc = int(
                lib.ZSTD_decompressStream(
                    self._dctx, ctypes.byref(outb), ctypes.byref(inb)
                )
            )
            if lib.ZSTD_isError(rc):
                raise StreamDecompressError(
                    f"libzstd ZSTD_decompressStream failed: {_zstd_error_name(lib, rc)}"
                )
            self._in_frame = rc != 0
            if inb.pos == prev_in and outb.pos == prev_out:
                break
        self._in = src[inb.pos :]
        return ctypes.string_at(self._outbuf, outb.pos)

    def _decompress_pyzstd(self, max_length: int) -> bytes:
        out = bytearray()
        while len(out) < max_length:
            if self._obj is None:
                if not self._in:
                    break
                self._obj = _py_zstd.ZstdDecompressor()
                self._in_frame = True
            src = self._in
            self._in = b""
            try:
                piece = self._obj.decompress(src, max_length - len(out))
            except _py_zstd.ZstdError as exc:
                raise StreamDecompressError(
                    f"zstd decompression failed: {exc}"
                ) from exc
            out += piece
            if self._obj.eof:
                self._in = self._obj.unused_data
                self._obj = None
                self._in_frame = False
            elif self._obj.needs_input:
                if not piece:
                    break
            else:
                # Output limit reached; decompressor retains its own input.
                break
        return bytes(out)

    def finish(self) -> None:
        """Assert the stream ended cleanly on a frame boundary."""
        if self._in:
            raise StreamDecompressError(
                f"{len(self._in)} unconsumed bytes at end of {self._fmt} stream"
            )
        if self._in_frame:
            raise StreamDecompressError(f"Truncated {self._fmt} stream (incomplete frame)")

    def __del__(self):
        dctx = getattr(self, "_dctx", None)
        if dctx and _LIBZSTD_D is not None:
            try:
                _LIBZSTD_D.ZSTD_freeDCtx(dctx)
            except Exception:
                pass
            self._dctx = None


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


class SeekableFrameWriter:
    """Ordered parallel writer of independent gzip/zstd frames + `.rzidx` entries.

    Accepts arbitrary-sized `write()` calls, cuts the uncompressed stream into
    exactly `frame_size` frames (last frame may be shorter), compresses them on a
    ThreadPoolExecutor, and writes the compressed frames strictly in order to
    `out_stream`. `close()` emits one valid empty frame if nothing was written,
    so the output is never 0 bytes.
    """

    def __init__(
        self,
        out_stream,
        *,
        fmt: str,
        level: int,
        frame_size: Optional[int] = None,
        threads: Optional[int] = None,
        stats: Optional[dict] = None,
    ):
        fmt_norm = str(fmt).strip().lower()
        if fmt_norm == "gzip":
            self.codec = CODEC_GZIP
            self._compress_fn = compress_gzip_frame
            backend = get_gzip_backend_name()
        elif fmt_norm == "zstd":
            self.codec = CODEC_ZSTD
            self._compress_fn = compress_zstd_frame
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
        self.fmt = fmt_norm
        self.level = int(level)
        self.frame_size = frame_size
        self.workers = workers
        self.max_in_flight = default_max_in_flight(workers, frame_size)
        if stats is not None:
            stats.update(
                fmt=fmt_norm,
                level=int(level),
                backend=backend,
                workers=workers,
                max_in_flight=self.max_in_flight,
                frame_size=frame_size,
            )
        self._out = out_stream
        self._pool: Optional[ThreadPoolExecutor] = ThreadPoolExecutor(
            max_workers=workers
        )
        self._pending: collections.deque = collections.deque()
        self._buf = bytearray()
        self.entries: List[Tuple[int, int]] = []
        self.comp_offset = 0
        self.decomp_offset = 0
        self._closed = False

    def write(self, data) -> None:
        if self._closed:
            raise ValueError("write() on closed SeekableFrameWriter")
        fs = self.frame_size
        if not self._buf and isinstance(data, bytes) and len(data) == fs:
            self._submit(data)
            return
        mv = memoryview(data).cast("B")
        if self._buf:
            take = min(fs - len(self._buf), len(mv))
            self._buf += mv[:take]
            mv = mv[take:]
            if len(self._buf) == fs:
                raw = bytes(self._buf)
                self._buf.clear()
                self._submit(raw)
        while len(mv) >= fs:
            self._submit(bytes(mv[:fs]))
            mv = mv[fs:]
        if len(mv):
            self._buf += mv

    def _submit(self, raw: bytes) -> None:
        fut = self._pool.submit(self._compress_fn, raw, self.level)
        self._pending.append((len(raw), fut))
        while len(self._pending) >= self.max_in_flight:
            self._drain_one()

    def _drain_one(self) -> None:
        raw_len, fut = self._pending.popleft()
        comp_bytes = fut.result()
        _write_all(self._out, comp_bytes)
        self.entries.append((self.comp_offset, self.decomp_offset))
        self.comp_offset += len(comp_bytes)
        self.decomp_offset += raw_len

    def close(self) -> Tuple[int, int, int]:
        """Flush all frames; returns (frame_count, total_compressed, total_uncompressed)."""
        if self._closed:
            return len(self.entries), self.comp_offset, self.decomp_offset
        if self._buf:
            raw = bytes(self._buf)
            self._buf.clear()
            self._submit(raw)
        while self._pending:
            self._drain_one()
        self._shutdown(cancel=False)
        # Emit one empty valid member if input stream was 0 bytes
        if not self.entries:
            empty_frame = self._compress_fn(b"", self.level)
            _write_all(self._out, empty_frame)
            self.entries.append((0, 0))
            self.comp_offset = len(empty_frame)
            self.decomp_offset = 0
        self._out.flush()
        self._closed = True
        return len(self.entries), self.comp_offset, self.decomp_offset

    def serialize_index(self) -> bytes:
        if not self._closed:
            raise ValueError("serialize_index() requires close() first")
        return serialize_seekidx(
            codec=self.codec,
            frame_size=self.frame_size,
            entries=self.entries,
            total_compressed=self.comp_offset,
            total_uncompressed=self.decomp_offset,
        )

    def _shutdown(self, cancel: bool) -> None:
        pool = self._pool
        self._pool = None
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=cancel)

    def abort(self) -> None:
        self._pending.clear()
        self._shutdown(cancel=True)
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.abort()
        return False


def _write_file_atomic(path: str, blob: bytes) -> None:
    """tmp + fsync + os.replace; the tmp file is unlinked on any error."""
    out_dir = os.path.dirname(os.path.abspath(path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _import_block_hash():
    """Import block_hash both as `rescuezilla.block_hash` and as a top-level module."""
    try:
        from . import block_hash  # type: ignore[import-not-found]
    except ImportError:
        import block_hash  # type: ignore[import-not-found,no-redef]
    return block_hash


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
    bhash_output_path: Optional[str] = None,
    bhash_chunk_size: Optional[int] = None,
) -> Tuple[int, int, int]:
    """Compress stdin -> stdout in independent frames and write atomic .rzidx index.

    `frame_size=None` selects default_frame_size_for_format(fmt) and
    `threads=None/0` selects default_worker_count(frame_size, fmt=..., level=...). If `stats` is a
    dict it is filled with fmt/level/backend/workers/frame_size.

    When `bhash_output_path` is set, every uncompressed chunk is also fed (on a
    dedicated thread, in order) into `block_hash.StreamingBlockHashFeed`. After
    all frames are written cleanly, a Partclone v0002 stream produces an atomic
    `.rzbhash`; a non-Partclone stream (e.g. partclone.dd) produces none. Any
    Partclone CRC32/truncation error raises and no index file is written.

    Returns (frame_count, total_compressed_bytes, total_uncompressed_bytes).
    """
    writer = SeekableFrameWriter(
        stdout_bin,
        fmt=fmt,
        level=level,
        frame_size=frame_size,
        threads=threads,
        stats=stats,
    )
    frame_size = writer.frame_size

    bh = None
    feed = None
    feed_pool: Optional[ThreadPoolExecutor] = None
    feed_futs: collections.deque = collections.deque()
    if bhash_output_path:
        bh = _import_block_hash()
        feed = bh.StreamingBlockHashFeed(
            chunk_size=int(bhash_chunk_size or bh.DEFAULT_BHASH_CHUNK_SIZE)
        )
        feed_pool = ThreadPoolExecutor(max_workers=1)

    try:
        with writer:
            while True:
                raw_chunk = _read_exact_chunk(stdin_bin, frame_size)
                if not raw_chunk:
                    break
                if feed is not None:
                    feed_futs.append(feed_pool.submit(feed.feed, raw_chunk))
                    while feed_futs and (
                        len(feed_futs) > writer.max_in_flight or feed_futs[0].done()
                    ):
                        feed_futs.popleft().result()
                writer.write(raw_chunk)
            frames, comp_total, decomp_total = writer.close()

        bhash_index = None
        if feed is not None:
            while feed_futs:
                feed_futs.popleft().result()
            bhash_index = feed.finish()
            if stats is not None:
                stats["bhash"] = (
                    "written" if bhash_index is not None else "skipped-non-partclone"
                )

        if index_output:
            _write_file_atomic(index_output, writer.serialize_index())
        if bhash_index is not None:
            bh.write_bhash_file(bhash_output_path, bhash_index)

        return frames, comp_total, decomp_total
    finally:
        if feed_pool is not None:
            feed_pool.shutdown(wait=True, cancel_futures=True)


# ----------------------------------------------------------------------
# Read side: concatenated split files + optional .rzidx random access
# ----------------------------------------------------------------------


class _ConcatFiles:
    """Read-only byte view over ordered split files (`.aa`, `.ab`, ...)."""

    def __init__(self, paths):
        self.paths = [os.fspath(p) for p in paths]
        if not self.paths:
            raise FileNotFoundError("No split files given")
        self.sizes = [os.path.getsize(p) for p in self.paths]
        self.starts: List[int] = []
        total = 0
        for size in self.sizes:
            self.starts.append(total)
            total += size
        self.total = total
        self._fh = None
        self._fh_idx = -1

    def read_at(self, offset: int, n: int) -> bytes:
        parts: List[bytes] = []
        while n > 0 and offset < self.total:
            i = bisect.bisect_right(self.starts, offset) - 1
            if i != self._fh_idx:
                self.close()
                self._fh = open(self.paths[i], "rb")
                self._fh_idx = i
            rel = offset - self.starts[i]
            self._fh.seek(rel)
            data = self._fh.read(min(n, self.sizes[i] - rel))
            if not data:
                raise OSError(f"Short read from split file {self.paths[i]}")
            parts.append(data)
            offset += len(data)
            n -= len(data)
        return parts[0] if len(parts) == 1 else b"".join(parts)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
        self._fh = None
        self._fh_idx = -1


class SeekableSplitStream:
    """Seekable uncompressed view of a split gzip/zstd/uncompressed image.

    - With a valid matching `.rzidx`: O(1) random access by decompressing only
      the frame(s) touched (libzstd / libdeflate / zlib, checksums verified).
    - Without an index: sequential multi-frame streaming decompression; forward
      seeks decompress-and-discard, backward seeks restart from the beginning.
    Raises `ValueError` at construction if the given `.rzidx` is corrupt or does
    not match the split files.
    """

    _STREAM_IN_CHUNK = 1024 * 1024
    _STREAM_OUT_CHUNK = 4 * 1024 * 1024

    def __init__(self, split_paths, fmt: str, seekidx_path: Optional[str] = None):
        fmt_norm = str(fmt).strip().lower()
        if fmt_norm in ("uncomp", "none"):
            fmt_norm = "uncompressed"
        if fmt_norm not in ("gzip", "zstd", "uncompressed"):
            raise ValueError(f"Unsupported split image compression: {fmt}")
        self.fmt = fmt_norm
        self._files = _ConcatFiles(split_paths)
        self.pos = 0
        self.size: Optional[int] = None
        self._entries: Optional[List[Tuple[int, int]]] = None
        self._u_offsets: List[int] = []
        self._frame_idx = -1
        self._frame_data = b""
        self._dec: Optional[StreamDecompressor] = None
        self._comp_pos = 0
        self._buf = b""
        self._buf_start = 0
        self._eof = False
        if fmt_norm == "uncompressed":
            self.size = self._files.total
        elif seekidx_path:
            with open(seekidx_path, "rb") as f:
                info = parse_seekidx_bytes(f.read())
            expected_codec = CODEC_GZIP if fmt_norm == "gzip" else CODEC_ZSTD
            if info["codec"] != expected_codec:
                raise ValueError(
                    f"Seek index codec {info['codec_name']} does not match {fmt_norm}"
                )
            if info["total_compressed"] != self._files.total:
                raise ValueError(
                    "Seek index total_compressed "
                    f"{info['total_compressed']} != split size {self._files.total}"
                )
            entries = info["entries"]
            for (c0, u0), (c1, u1) in zip(entries, entries[1:]):
                if c1 < c0 or u1 < u0:
                    raise ValueError("Seek index entries are not monotonic")
            if entries[0] != (0, 0):
                raise ValueError("Seek index does not start at offset 0")
            self._entries = entries
            self._u_offsets = [u for _c, u in entries]
            self.size = info["total_uncompressed"]

    @property
    def mode(self) -> str:
        if self.fmt == "uncompressed":
            return "raw"
        return "index" if self._entries is not None else "stream"

    @property
    def compressed_size(self) -> int:
        return self._files.total

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            new = int(offset)
        elif whence == os.SEEK_CUR:
            new = self.pos + int(offset)
        elif whence == os.SEEK_END:
            if self.size is None:
                raise OSError("SEEK_END unsupported without a seek index")
            new = self.size + int(offset)
        else:
            raise ValueError(f"Invalid whence: {whence}")
        if new < 0:
            raise ValueError("Negative seek position")
        self.pos = new
        return new

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            parts = []
            while True:
                piece = self.read(self._STREAM_OUT_CHUNK)
                if not piece:
                    break
                parts.append(piece)
            return b"".join(parts)
        if n == 0:
            return b""
        if self.mode == "raw":
            data = self._files.read_at(self.pos, n)
        elif self.mode == "index":
            data = self._read_index(n)
        else:
            data = self._read_stream(n)
        self.pos += len(data)
        return data

    def read_exact(self, n: int) -> bytes:
        data = self.read(n)
        if len(data) != n:
            raise EOFError(
                f"Unexpected end of image stream at offset {self.pos} "
                f"(wanted {n} bytes, got {len(data)})"
            )
        return data

    def _get_frame(self, i: int) -> bytes:
        if i != self._frame_idx:
            c0, u0 = self._entries[i]
            c1, u1 = self._entries[i + 1]
            comp = self._files.read_at(c0, c1 - c0)
            if len(comp) != c1 - c0:
                raise StreamDecompressError(f"Seekable frame {i} is truncated")
            if self.fmt == "gzip":
                data = decompress_gzip_frame(comp, u1 - u0)
            else:
                data = decompress_zstd_frame(comp, u1 - u0)
            self._frame_idx = i
            self._frame_data = data
        return self._frame_data

    def _read_index(self, n: int) -> bytes:
        out = bytearray()
        pos = self.pos
        frame_count = len(self._entries) - 1
        while n > 0 and pos < self.size:
            i = bisect.bisect_right(self._u_offsets, pos) - 1
            if i >= frame_count:
                break
            data = self._get_frame(i)
            off = pos - self._u_offsets[i]
            take = min(n, len(data) - off)
            if take <= 0:
                raise StreamDecompressError(f"Seekable frame {i} is shorter than indexed")
            out += data[off : off + take]
            pos += take
            n -= take
        return bytes(out)

    def _reset_stream(self) -> None:
        self._dec = StreamDecompressor(self.fmt)
        self._comp_pos = 0
        self._buf = b""
        self._buf_start = 0
        self._eof = False

    def _next_piece(self) -> bool:
        while True:
            fed = b""
            if self._dec.needs_input:
                if self._comp_pos >= self._files.total:
                    self._dec.finish()
                    self._eof = True
                    return False
                fed = self._files.read_at(self._comp_pos, self._STREAM_IN_CHUNK)
                self._comp_pos += len(fed)
            out = self._dec.decompress(fed, self._STREAM_OUT_CHUNK)
            if out:
                self._buf_start += len(self._buf)
                self._buf = out
                return True
            if not fed and not self._dec.needs_input:
                raise StreamDecompressError(f"{self.fmt} stream decoder stalled")

    def _read_stream(self, n: int) -> bytes:
        if self._dec is None or self.pos < self._buf_start:
            self._reset_stream()
        out = bytearray()
        pos = self.pos
        while n > 0:
            end = self._buf_start + len(self._buf)
            if pos >= end:
                if self._eof or not self._next_piece():
                    break
                continue
            off = pos - self._buf_start
            take = min(n, len(self._buf) - off)
            out += self._buf[off : off + take]
            pos += take
            n -= take
        return bytes(out)

    def close(self) -> None:
        self._files.close()
        self._frame_data = b""
        self._buf = b""
        self._dec = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


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
    parser.add_argument(
        "--bhash-output",
        type=str,
        default=None,
        help="Also write a Partclone block-hash index (.rzbhash) to this path "
        "(skipped cleanly for non-Partclone streams such as partclone.dd)",
    )
    parser.add_argument(
        "--bhash-chunk-size",
        type=int,
        default=1048576,
        help="Logical chunk size in bytes for --bhash-output (default 1 MiB)",
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
            bhash_output_path=args.bhash_output,
            bhash_chunk_size=args.bhash_chunk_size,
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
