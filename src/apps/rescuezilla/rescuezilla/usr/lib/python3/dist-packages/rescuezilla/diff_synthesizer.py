#!/usr/bin/env python3
# ----------------------------------------------------------------------
#   Copyright (C) 2026 Rescuezilla Seekable-Index Community Fork Contributors
#   Derived from Rescuezilla (Copyright (C) 2019-2025 Rescuezilla.com)
#   Added 2026-11-28 (Community Fork): SeekZilla Phase 2 Partclone v0002
#   stream synthesizer and differential merge/flatten tool (GPL-3.0-or-later).
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
"""Reconstruct a byte-exact Partclone v0002 stream from base + differential.

`stream` writes the synthesized image to stdout (pipe into
`partclone.restore --source -` / `partclone.chkimg --source -`); `merge`
flattens a differential backup and its base into a standalone, stock
Rescuezilla/Clonezilla-compatible full image directory.

Fail-closed: the base `.rzbhash` SHA-256 must match the differential metadata,
the base Partclone header/bitmap CRC32 and geometry must match, and every chunk
read from the base is re-hashed against the base `.rzbhash` before it is
emitted (`BaseCorruptionError` on any bit flip). Exit codes: 0 ok, 1 error,
3 base mismatch, 4 base corruption, 5 corrupt differential, 141 broken pipe.
"""

import argparse
import errno
import hashlib
import os
import shutil
import signal
import stat
import struct
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Callable, List, Optional, Tuple

try:
    from . import block_hash as _bh  # type: ignore[import-not-found]
    from . import diff_format as _df  # type: ignore[import-not-found]
    from . import seekable_compressor as _sc  # type: ignore[import-not-found]
except ImportError:
    import block_hash as _bh  # type: ignore[import-not-found,no-redef]
    import diff_format as _df  # type: ignore[import-not-found,no-redef]
    import seekable_compressor as _sc  # type: ignore[import-not-found,no-redef]

# Self-contained native seekable split reader (.rzidx random access or sequential).
SeekableSplitStream = _sc.SeekableSplitStream

DEFAULT_SPLIT_SIZE = 4 * 1000 * 1000 * 1000  # GNU split --bytes=4GB
SPLIT_SUFFIX_LENGTH = 2

EXIT_BASE_MISMATCH = 3
EXIT_BASE_CORRUPTION = 4
EXIT_DIFF_CORRUPTION = 5

_U32 = struct.Struct("<I")
_MASK32 = 0xFFFFFFFF
_READ_ERRORS = (EOFError, OSError, ValueError, RuntimeError, MemoryError)
# Malformed manifest/meta values (wrong types, bad geometry) -> exit 5.
_MANIFEST_ERRORS = (_df.DiffFormatError, KeyError, TypeError, ValueError)


class BaseCorruptionError(RuntimeError):
    """Base image data does not match its own CRC32s / `.rzbhash` digests."""


class BaseMismatchError(RuntimeError):
    """The base image is not the one this differential backup was taken against."""


class DiffCorruptionError(RuntimeError):
    """The differential metadata or delta stream is corrupt or inconsistent."""


def _write_all(stream, data) -> None:
    mv = memoryview(data).cast("B")
    total = len(mv)
    written = 0
    while written < total:
        n = stream.write(mv[written:])
        if n is None:
            raise BlockingIOError("Non-blocking output stream returned None on write")
        if n == 0:
            raise BrokenPipeError("Short write to output stream")
        written += n


class PartcloneV2DataEmitter:
    """Writes used-block bytes + Partclone v0002 data CRC32s byte-for-byte.

    A CRC32 follows every `blocks_per_checksum` blocks and the trailing partial
    group; the running CRC restarts per group iff `reseed_checksum` is set.
    """

    def __init__(self, out_stream, header: _bh.PartcloneHeader, expected_used_blocks: int):
        self._out = out_stream
        self._cs = header.checksum_enabled
        self._reseed = bool(header.reseed_checksum)
        self._group_bytes = (
            header.blocks_per_checksum * header.block_size if self._cs else 0
        )
        self._group_done = 0
        self._z = 0
        self._expected = int(expected_used_blocks) * header.block_size
        self.written = 0

    def write(self, data) -> None:
        mv = memoryview(data).cast("B")
        n = len(mv)
        if self.written + n > self._expected:
            raise DiffCorruptionError("Synthesized data exceeds the new bitmap's used blocks")
        if not self._cs:
            _write_all(self._out, mv)
            self.written += n
            return
        pos = 0
        while pos < n:
            take = min(n - pos, self._group_bytes - self._group_done)
            piece = mv[pos : pos + take]
            self._z = zlib.crc32(piece, self._z)
            _write_all(self._out, piece)
            pos += take
            self._group_done += take
            self.written += take
            if self._group_done == self._group_bytes:
                self._emit_crc()

    def _emit_crc(self) -> None:
        _write_all(self._out, _U32.pack((~self._z) & _MASK32))
        self._group_done = 0
        if self._reseed:
            self._z = 0

    def finish(self) -> None:
        if self.written != self._expected:
            raise DiffCorruptionError(
                f"Synthesized {self.written} data bytes, expected {self._expected}"
            )
        if self._cs and self._group_done:
            self._emit_crc()


def read_partclone_preamble(stream) -> Tuple[_bh.PartcloneHeader, _bh.BitmapHelper]:
    """Read + CRC32-verify header and bitmap from a stream positioned at 0.

    Layout per partclone.c: header + header CRC32 (always), bitmap + bitmap
    CRC32 (iff bitmap_mode != BM_NONE, independent of checksum_mode).
    """
    prefix = stream.read_exact(_bh.PARTCLONE_FIXED_HEADER_SIZE + 12)
    total = _bh.partclone_header_size(prefix)
    raw_header = prefix + stream.read_exact(total - len(prefix))
    header = _bh.parse_partclone_header(raw_header, verify_crc=True)
    raw_bitmap = stream.read_exact(header.bitmap_bytes) if header.bitmap_bytes else b""
    crc = stream.read_exact(header.bitmap_crc_size) if header.bitmap_crc_size else b""
    bitmap = _bh.BitmapHelper(raw_bitmap, header.bitmap_mode, header.totalblock)
    _bh.verify_partclone_bitmap_crc(header, raw_bitmap, crc, bitmap)
    return header, bitmap


def _safe_join(root, name: str, label: str) -> Path:
    """root / basename, asserting the resolved path stays strictly inside root."""
    _df.validate_safe_basename(name, label)
    path = Path(root) / name
    _df.assert_path_within(path, root)
    return path


class BasePartcloneReader:
    """Random access to used blocks of a base Partclone v0002 image stream."""

    def __init__(
        self,
        stream,
        header: _bh.PartcloneHeader,
        bitmap: _bh.BitmapHelper,
        verify_crc: bool = True,
    ):
        self.stream = stream
        self.header = header
        self.bs = header.block_size
        self.data_start = header.data_start
        self.used = bitmap.used_count
        self.cs = header.checksum_enabled
        self.bpcs = header.blocks_per_checksum if self.cs else 0
        self.reseed = bool(header.reseed_checksum)
        self.verify = bool(verify_crc) and self.cs
        self._cache_g = -1
        self._cache = b""
        self._seq_next = 0
        self._z = 0

    def read_used_blocks(self, first: int, count: int) -> bytes:
        if count <= 0:
            return b""
        if first < 0 or first + count > self.used:
            raise BaseCorruptionError("Base used-block range out of bounds")
        bs = self.bs
        try:
            if not self.cs:
                self.stream.seek(self.data_start + first * bs)
                return self.stream.read_exact(count * bs)
            bpcs = self.bpcs
            g0 = first // bpcs
            g1 = (first + count - 1) // bpcs
            parts = []
            for g in range(g0, g1 + 1):
                data = self._group(g)
                gs = g * bpcs
                lo = max(first, gs) - gs
                hi = min(first + count, gs + len(data) // bs) - gs
                parts.append(memoryview(data)[lo * bs : hi * bs])
            return b"".join(parts)
        except BaseCorruptionError:
            raise
        except _READ_ERRORS as exc:
            raise BaseCorruptionError(f"Base image read failed: {exc}") from exc

    def _group(self, g: int) -> bytes:
        if g == self._cache_g:
            return self._cache
        if self.verify and not self.reseed:
            # reseed_checksum=0: CRC runs over all prior data, so verify sequentially
            if g < self._seq_next:
                raise BaseCorruptionError("Non-monotonic base read with reseed_checksum=0")
            while self._seq_next < g:
                self._read_group(self._seq_next)
        data = self._read_group(g)
        self._cache_g = g
        self._cache = data
        return data

    def _read_group(self, g: int) -> bytes:
        bpcs = self.bpcs
        nblocks = min(bpcs, self.used - g * bpcs)
        size = nblocks * self.bs
        self.stream.seek(self.data_start + g * (bpcs * self.bs + _bh.PARTCLONE_CRC_SIZE))
        raw = self.stream.read_exact(size + _bh.PARTCLONE_CRC_SIZE)
        data = raw[:size]
        if self.verify:
            (stored,) = _U32.unpack_from(raw, size)
            if self.reseed:
                calc = _bh.partclone_crc32(data)
            else:
                self._z = zlib.crc32(data, self._z)
                calc = (~self._z) & _MASK32
                self._seq_next = g + 1
            if calc != stored:
                raise BaseCorruptionError(
                    f"Base Partclone data CRC32 mismatch in group {g}: "
                    f"stored 0x{stored:08x}, computed 0x{calc:08x}"
                )
        return data


def find_partition_splits(
    image_dir, short_part: str, prefix_name: Optional[str] = None
) -> Tuple[List[str], str]:
    """Return (ordered split paths, compression format) of a partition image."""
    image_dir = Path(image_dir)
    if prefix_name:
        _df.validate_safe_basename(prefix_name, "base_image_prefix")
        parsed = _bh.parse_ptcl_split_name(prefix_name + "aa")
        if parsed is None:
            raise BaseMismatchError(f"Invalid base image prefix {prefix_name!r}")
        comp = parsed[2]
        prefix = str(image_dir / prefix_name)
    else:
        cands = [i for i in _bh.find_ptcl_images(image_dir) if i.short_part == short_part]
        if len(cands) != 1:
            raise BaseMismatchError(
                f"Expected exactly 1 Partclone image for {short_part} in {image_dir}, "
                f"found {len(cands)}"
            )
        comp = cands[0].comp_suffix
        prefix = cands[0].prefix
    fmt = _bh.COMPRESSION_SUFFIX_TO_FORMAT.get(comp)
    if fmt is None:
        raise BaseMismatchError(f"Unsupported image compression suffix {comp!r}")
    paths = _bh.list_split_files(prefix)
    if not paths:
        raise BaseMismatchError(f"No split files found for {prefix}*")
    for p in paths:  # also rejects split "files" that are symlinks leaving the dir
        _df.assert_path_within(p, image_dir)
    return paths, fmt


def _open_split_stream(paths, fmt, seekidx_path) -> "_sc.SeekableSplitStream":
    if seekidx_path is not None and os.path.isfile(seekidx_path):
        try:
            return SeekableSplitStream(paths, fmt, str(seekidx_path))
        except ValueError as exc:
            sys.stderr.write(
                f"diff_synthesizer warning: ignoring unusable seek index {seekidx_path}: {exc}\n"
            )
    return SeekableSplitStream(paths, fmt, None)


def synthesize_partition_stream(
    diff_dir,
    base_dir,
    short_part: str,
    out_stream,
    *,
    verify_base_hashes: bool = True,
    verify_base_crc: bool = True,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    meta: Optional[_df.DiffMeta] = None,
) -> dict:
    """Write the byte-exact new Partclone v0002 stream of `short_part` to out_stream.

    All identity/consistency checks run before the first output byte. `meta`
    may be passed when the caller already loaded this partition's metadata
    (avoids holding two copies of a large bitmap/offset table).
    """
    diff_dir = Path(diff_dir)
    manifest = _df.load_diff_manifest(diff_dir)  # validates every entry's names
    if manifest is None:
        raise FileNotFoundError(f"{diff_dir} has no {_df.DIFF_MANIFEST_FILENAME}")
    _df.validate_safe_identifier(short_part, "partition name")
    raw_entry = manifest["partitions"].get(short_part)
    if not isinstance(raw_entry, dict):
        raise DiffCorruptionError(f"Partition {short_part} is not in the rzdiff manifest")
    entry = _df.validate_partition_entry(short_part, raw_entry)
    meta_path = _safe_join(diff_dir, entry["meta"], "meta")
    if meta is None:
        try:
            meta = _df.load_diff_meta(meta_path)
        except _MANIFEST_ERRORS as exc:
            raise DiffCorruptionError(f"Differential metadata unusable: {exc}") from exc
    base_dir = _df.resolve_base_dir(diff_dir, manifest, base_dir)

    # --- New header / bitmap from metadata --------------------------------
    try:
        new_header = _bh.parse_partclone_header(meta.raw_partclone_header, verify_crc=True)
        if len(meta.raw_partclone_header) != new_header.header_total_size:
            raise _bh.PartcloneStreamError("raw header length mismatch")
        new_bitmap = _bh.BitmapHelper(
            meta.raw_partclone_bitmap, new_header.bitmap_mode, new_header.totalblock
        )
        geom = _bh.compute_chunk_geometry(
            new_header.block_size, new_header.totalblock, meta.chunk_size
        )
    except (ValueError, TypeError) as exc:  # incl. PartcloneStreamError
        raise DiffCorruptionError(f"Differential header/bitmap invalid: {exc}") from exc
    if (
        (new_header.block_size, new_header.totalblock, new_header.device_size)
        != (meta.block_size, meta.totalblock, meta.device_size)
        or geom[0] != meta.chunk_size
        or geom[2] != meta.num_chunks
    ):
        raise DiffCorruptionError("Differential metadata geometry is inconsistent")
    bpc = geom[1]
    bs = new_header.block_size
    for chunk_idx, _off, ln in meta.iter_offsets():
        _sl, used, _n = new_bitmap.canonical_chunk_bitmap_and_count(chunk_idx, bpc)
        if ln != used * bs:
            raise DiffCorruptionError(
                f"Delta chunk {chunk_idx} length {ln} != {used} used blocks * {bs}"
            )

    # --- Fail-closed base identity ----------------------------------------
    base_bhash_path = _safe_join(base_dir, entry["base_bhash"], "base_bhash")
    try:
        base_bhash = _bh.load_bhash_file(base_bhash_path)
    except FileNotFoundError as exc:
        raise BaseMismatchError(f"Base block-hash index not found: {base_bhash_path}") from exc
    except (_bh.BlockHashFormatError, OSError) as exc:
        raise BaseCorruptionError(f"Base block-hash index corrupt: {exc}") from exc
    if base_bhash.file_sha256 != meta.base_bhash_sha256:
        raise BaseMismatchError(
            f"Base .rzbhash SHA-256 {base_bhash.file_sha256.hex()} != differential's "
            f"recorded base {meta.base_bhash_sha256.hex()}"
        )
    manifest_hex = entry.get("base_bhash_sha256")
    if manifest_hex is not None:
        manifest_sha = bytes.fromhex(manifest_hex)  # validated by validate_partition_entry
        if manifest_sha != meta.base_bhash_sha256:
            raise BaseMismatchError("Manifest base_bhash_sha256 != metadata base_bhash_sha256")
    if (
        base_bhash.chunk_size,
        base_bhash.block_size,
        base_bhash.totalblock,
        base_bhash.device_size,
        base_bhash.num_chunks,
    ) != (meta.chunk_size, meta.block_size, meta.totalblock, meta.device_size, meta.num_chunks):
        raise BaseMismatchError("Base .rzbhash geometry != differential geometry")
    if base_bhash.fs_name != new_header.fs:
        raise BaseMismatchError("Base .rzbhash filesystem != differential filesystem")

    base_paths, base_fmt = find_partition_splits(
        base_dir, short_part, entry.get("base_image_prefix")
    )
    base_seekidx = _safe_join(base_dir, entry["base_seekidx"], "base_seekidx")
    # Validate every delta path before opening anything.
    data_prefix = _safe_join(diff_dir, entry["data_prefix"], "data_prefix")
    delta_seekidx = _safe_join(diff_dir, entry["seekidx"], "seekidx")
    delta_paths = _bh.list_split_files(data_prefix)
    if not delta_paths:
        raise DiffCorruptionError(f"No delta split files for {entry['data_prefix']}*")
    for p in delta_paths:
        _df.assert_path_within(p, diff_dir)

    base_stream = _open_split_stream(base_paths, base_fmt, base_seekidx)
    delta_stream = None
    try:
        try:
            base_header, base_bitmap = read_partclone_preamble(base_stream)
        except _bh.PartcloneUnsupportedError as exc:
            raise BaseMismatchError(f"Base image is not Partclone v0002: {exc}") from exc
        except _READ_ERRORS as exc:
            raise BaseCorruptionError(f"Base Partclone header/bitmap invalid: {exc}") from exc
        if (
            base_header.fs,
            base_header.device_size,
            base_header.block_size,
            base_header.totalblock,
        ) != (new_header.fs, new_header.device_size, new_header.block_size, new_header.totalblock):
            raise BaseMismatchError("Base Partclone header geometry/fs != new image")
        if meta.base_header_sha256 != _df.ZERO_SHA256:
            if hashlib.sha256(base_header.raw).digest() != meta.base_header_sha256:
                raise BaseMismatchError("Base Partclone header SHA-256 mismatch")
        base_reader = BasePartcloneReader(
            base_stream, base_header, base_bitmap, verify_crc=verify_base_crc
        )

        # --- Delta stream -------------------------------------------------
        delta_stream = _open_split_stream(delta_paths, entry["compression"], delta_seekidx)
        if delta_stream.size is not None and delta_stream.size != meta.delta_stream_size:
            raise DiffCorruptionError(
                f"Delta stream size {delta_stream.size} != metadata {meta.delta_stream_size}"
            )

        # --- Emit -----------------------------------------------------------
        _write_all(out_stream, meta.raw_partclone_header)
        _write_all(out_stream, meta.raw_partclone_bitmap)
        if new_header.bitmap_crc_size:
            # Present iff bitmap_mode != BM_NONE (independent of checksum_mode);
            # for BM_BYTE it covers the bit-packed bitmap (partclone.c).
            _write_all(
                out_stream,
                _U32.pack(
                    _bh.partclone_bitmap_crc32(
                        new_header, meta.raw_partclone_bitmap, new_bitmap
                    )
                ),
            )
        emitter = PartcloneV2DataEmitter(out_stream, new_header, new_bitmap.used_count)

        n_offsets = meta.offsets_count()
        k = 0
        changed = unchanged = 0
        for c in range(meta.num_chunks):
            bm_slice, used, n_blocks = new_bitmap.canonical_chunk_bitmap_and_count(c, bpc)
            if used:
                if meta.is_changed(c):
                    if k >= n_offsets:
                        raise DiffCorruptionError(f"No delta offset entry for chunk {c}")
                    chunk_idx, off, ln = meta.get_offset(k)
                    k += 1
                    if chunk_idx != c or ln != used * bs:
                        raise DiffCorruptionError(f"Delta offset entry mismatch at chunk {c}")
                    try:
                        delta_stream.seek(off)
                        data = delta_stream.read_exact(ln)
                    except _READ_ERRORS as exc:
                        raise DiffCorruptionError(
                            f"Delta stream read failed at chunk {c}: {exc}"
                        ) from exc
                    changed += 1
                else:
                    base_slice, base_used, _bn = base_bitmap.canonical_chunk_bitmap_and_count(
                        c, bpc
                    )
                    if base_slice != bm_slice:
                        raise BaseMismatchError(
                            f"Unchanged chunk {c} has a different bitmap in the base image"
                        )
                    first = base_bitmap.rank(c * bpc)
                    data = base_reader.read_used_blocks(first, base_used)
                    if verify_base_hashes:
                        digest = _bh.compute_chunk_digest(n_blocks, bm_slice, data, used)
                        if digest != base_bhash.get_digest(c):
                            raise BaseCorruptionError(
                                f"Base chunk {c} SHA-256 mismatch (bit rot or tampering)"
                            )
                    unchanged += 1
                emitter.write(data)
            elif meta.is_changed(c):
                raise DiffCorruptionError(f"Empty chunk {c} is marked as changed")
            if progress_cb is not None and (c & 255 == 255 or c + 1 == meta.num_chunks):
                progress_cb(c + 1, meta.num_chunks)
        if k != n_offsets:
            raise DiffCorruptionError(f"{n_offsets - k} delta chunks were not consumed")
        if delta_stream.size is None:
            # Sequential (no .rzidx): the size was never checked up front, so
            # assert the delta ends exactly after the last changed chunk.
            # Done before the final CRC32 so a consumer sees a truncated image.
            expected = meta.delta_stream_size
            try:
                delta_stream.seek(expected)
                tail = delta_stream.read(1)
            except _READ_ERRORS as exc:
                raise DiffCorruptionError(f"Delta stream is corrupt after its data: {exc}") from exc
            if tail or delta_stream.tell() != expected:
                raise DiffCorruptionError(
                    f"Delta stream has trailing data beyond {expected} bytes"
                )
        emitter.finish()
        out_stream.flush()
    finally:
        base_stream.close()
        if delta_stream is not None:
            delta_stream.close()
    return {
        "short_part": short_part,
        "changed_chunks": changed,
        "unchanged_chunks": unchanged,
        "num_chunks": meta.num_chunks,
        "data_bytes": emitter.written,
    }


# ----------------------------------------------------------------------
# merge (flatten diff + base into a standalone full image)
# ----------------------------------------------------------------------


class SplitFileWriter:
    """Writes `<prefix>aa`, `<prefix>ab`, ... like `split --suffix-length=2 --bytes=N`."""

    def __init__(self, prefix, split_size: int = DEFAULT_SPLIT_SIZE):
        self.prefix = os.fspath(prefix)
        self.split_size = int(split_size)
        if self.split_size <= 0:
            raise ValueError("split_size must be > 0")
        self.paths: List[str] = []
        self._fh = None
        self._cur = 0
        self.closed = False

    @staticmethod
    def suffix(i: int) -> str:
        if i >= 26**SPLIT_SUFFIX_LENGTH:
            raise OSError("split suffixes exhausted (output too large for 2-letter suffix)")
        return chr(97 + i // 26) + chr(97 + i % 26)

    def _roll(self) -> None:
        self._close_current()
        path = self.prefix + self.suffix(len(self.paths))
        self._fh = open(path, "wb")
        self.paths.append(path)
        self._cur = 0

    def _close_current(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
            self._fh = None

    def write(self, b) -> int:
        if self.closed:
            raise ValueError("write() on closed SplitFileWriter")
        mv = memoryview(b).cast("B")
        n = len(mv)
        pos = 0
        while pos < n:
            if self._fh is None or self._cur >= self.split_size:
                self._roll()
            take = min(n - pos, self.split_size - self._cur)
            _write_all(self._fh, mv[pos : pos + take])
            self._cur += take
            pos += take
        return n

    def flush(self) -> None:
        if self._fh is not None:
            self._fh.flush()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._close_current()


class _TeeWriter:
    """Buffered sink forwarding every byte to a frame writer and a block-hash feed."""

    def __init__(self, frame_writer, feed, buffer_size: int = 4 * 1024 * 1024):
        self._fw = frame_writer
        self._feed = feed
        self._size = int(buffer_size)
        self._buf = bytearray()

    def write(self, b) -> int:
        mv = memoryview(b).cast("B")
        if self._buf and len(self._buf) + len(mv) > self._size:
            self.flush()
        if len(mv) >= self._size:
            self._forward(mv)
        else:
            self._buf += mv
        return len(mv)

    def _forward(self, data) -> None:
        self._feed.feed(data)
        self._fw.write(data)

    def flush(self) -> None:
        if self._buf:
            data = bytes(self._buf)
            self._buf.clear()
            self._forward(data)


def merge_diff_image(
    diff_dir,
    output_dir,
    *,
    base_dir=None,
    fmt: str = "zstd",
    level: int = 3,
    frame_size: Optional[int] = None,
    threads: Optional[int] = None,
    split_size: int = DEFAULT_SPLIT_SIZE,
    verify_base_hashes: bool = True,
    verify_base_crc: bool = True,
) -> dict:
    """Flatten a differential backup + base into a standalone full image dir.

    Output is staged in a hidden sibling directory and atomically renamed to
    `output_dir` only after every partition was synthesized and verified.
    """
    diff_dir = Path(diff_dir)
    output_dir = Path(output_dir)
    manifest = _df.load_diff_manifest(diff_dir)
    if manifest is None:
        raise FileNotFoundError(f"{diff_dir} has no {_df.DIFF_MANIFEST_FILENAME}")
    resolved_base = _df.resolve_base_dir(diff_dir, manifest, base_dir)
    if output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir())):
        raise FileExistsError(f"Output directory {output_dir} exists and is not empty")
    comp_suffix = _bh.FORMAT_TO_COMPRESSION_SUFFIX.get(fmt)
    if fmt not in ("gzip", "zstd") or comp_suffix is None:
        raise ValueError(f"Unsupported merge output format: {fmt}")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    # Unique (no PID reuse collisions), created 0700; widened to the umask
    # default below so the final image dir gets normal permissions.
    staging = Path(tempfile.mkdtemp(prefix=".rzmerge-", dir=output_dir.parent))
    results = {}
    try:
        os.chmod(staging, _default_dir_mode())
        skipped = _copy_metadata_tree(diff_dir, staging, staging, top_level=True)

        for short_part in sorted(manifest["partitions"]):
            entry = _df.validate_partition_entry(short_part, manifest["partitions"][short_part])
            meta_path = _safe_join(diff_dir, entry["meta"], "meta")
            try:
                meta = _df.load_diff_meta(meta_path)
            except _MANIFEST_ERRORS as exc:
                raise DiffCorruptionError(f"Differential metadata unusable: {exc}") from exc
            prefix = staging / f"{short_part}.{entry['fs']}-ptcl-img.{comp_suffix}."
            seekidx_out = staging / f"Info-seekidx-{short_part}.rzidx"
            bhash_out = staging / _bh.bhash_index_filename(short_part)
            for out_path in (prefix, seekidx_out, bhash_out):
                _df.assert_path_within(out_path, staging)
            feed = _bh.StreamingBlockHashFeed(chunk_size=meta.chunk_size)
            splitter = SplitFileWriter(prefix, split_size)
            try:
                frame_writer = _sc.SeekableFrameWriter(
                    splitter, fmt=fmt, level=level, frame_size=frame_size, threads=threads
                )
                with frame_writer:
                    tee = _TeeWriter(frame_writer, feed)
                    stats = synthesize_partition_stream(
                        diff_dir,
                        resolved_base,
                        short_part,
                        tee,
                        verify_base_hashes=verify_base_hashes,
                        verify_base_crc=verify_base_crc,
                        meta=meta,
                    )
                    del meta  # only one copy of the (large) metadata at a time
                    tee.flush()
                    frame_writer.close()
            finally:
                splitter.close()
            for p in splitter.paths:
                _df.assert_path_within(p, staging)
            bhash = feed.finish()
            if bhash is None:
                raise DiffCorruptionError(f"Synthesized {short_part} is not Partclone v0002")
            _bh.atomic_write_bytes(seekidx_out, frame_writer.serialize_index())
            _bh.write_bhash_file(bhash_out, bhash)
            stats["split_files"] = [os.path.basename(p) for p in splitter.paths]
            results[short_part] = stats

        _fsync_path(staging, directory=True)
        if output_dir.exists():
            output_dir.rmdir()
        os.replace(staging, output_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _fsync_path(output_dir.parent, directory=True)  # persist the rename
    return {
        "output_dir": str(output_dir),
        "base_dir": str(resolved_base),
        "partitions": results,
        "skipped": skipped,
    }


def _default_dir_mode() -> int:
    """0o777 minus the process umask (what a plain mkdir() would create)."""
    mask = os.umask(0)
    os.umask(mask)
    return 0o777 & ~mask


def _fsync_path(path, *, directory: bool = False) -> None:
    flags = os.O_RDONLY | (getattr(os, "O_DIRECTORY", 0) if directory else 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _copy_regular_file_nofollow(src: Path, dst: Path) -> bool:
    """Copy a regular file without ever following a symlink; fsync the copy.

    Returns False (nothing written) when `src` is not a regular file, including
    when it was swapped for a symlink/FIFO after the caller's lstat().
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(src, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENXIO):
            return False
        raise
    with os.fdopen(fd, "rb") as fin:
        st = os.fstat(fin.fileno())
        if not stat.S_ISREG(st.st_mode):
            return False
        with open(dst, "xb") as fout:
            shutil.copyfileobj(fin, fout, 1024 * 1024)
            fout.flush()
            os.fsync(fout.fileno())
        os.chmod(dst, stat.S_IMODE(st.st_mode) & 0o777)  # never copy setuid/setgid
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    return True


def _copy_metadata_tree(src_dir: Path, dst_dir: Path, staging: Path, *, top_level: bool) -> List[str]:
    """Copy non-diff metadata into staging; symlinks and special files are skipped.

    Returns the skipped source paths. Every destination is asserted to resolve
    strictly inside `staging`.
    """
    skipped: List[str] = []
    for name in sorted(os.listdir(src_dir)):
        if top_level and _df.is_diff_artifact_filename(name):
            continue
        src = Path(src_dir) / name
        dst = Path(dst_dir) / name
        _df.assert_path_within(dst, staging)
        st = os.lstat(src)
        if stat.S_ISDIR(st.st_mode):
            os.mkdir(dst, stat.S_IMODE(st.st_mode) | stat.S_IRWXU)
            skipped += _copy_metadata_tree(src, dst, staging, top_level=False)
            _fsync_path(dst, directory=True)
        elif not stat.S_ISREG(st.st_mode) or not _copy_regular_file_nofollow(src, dst):
            skipped.append(str(src))  # symlink (never followed), FIFO, device, ...
    return skipped


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="SeekZilla differential image synthesizer / merge tool"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_stream = sub.add_parser(
        "stream", help="Write the synthesized Partclone v0002 stream of one partition to stdout"
    )
    p_stream.add_argument("--diff-dir", required=True)
    p_stream.add_argument("--partition", required=True)
    p_stream.add_argument("--base-dir", default=None)
    p_merge = sub.add_parser(
        "merge", help="Flatten a differential backup + base into a standalone full image"
    )
    p_merge.add_argument("--diff-dir", required=True)
    p_merge.add_argument("--output-dir", required=True)
    p_merge.add_argument("--base-dir", default=None)
    p_merge.add_argument("--format", default="zstd", choices=["zstd", "gzip"])
    p_merge.add_argument("--level", type=int, default=3)
    p_merge.add_argument("--frame-size", type=int, default=None)
    p_merge.add_argument("--threads", type=int, default=None)
    args = parser.parse_args(argv)

    def _on_signal(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        if args.command == "stream":
            synthesize_partition_stream(
                args.diff_dir, args.base_dir, args.partition, sys.stdout.buffer
            )
        else:
            result = merge_diff_image(
                args.diff_dir,
                args.output_dir,
                base_dir=args.base_dir,
                fmt=args.format,
                level=args.level,
                frame_size=args.frame_size,
                threads=args.threads,
            )
            for path in result.get("skipped", []):
                sys.stderr.write(
                    f"diff_synthesizer warning: skipped non-regular file/symlink {path}\n"
                )
            for part, st in sorted(result["partitions"].items()):
                print(
                    f"merged {part}: changed={st['changed_chunks']} "
                    f"unchanged={st['unchanged_chunks']} files={','.join(st['split_files'])}"
                )
            print(f"output: {result['output_dir']}")
        return 0
    except BrokenPipeError:
        return 141
    except BaseMismatchError as exc:
        sys.stderr.write(f"diff_synthesizer base mismatch: {exc}\n")
        return EXIT_BASE_MISMATCH
    except BaseCorruptionError as exc:
        sys.stderr.write(f"diff_synthesizer base corruption: {exc}\n")
        return EXIT_BASE_CORRUPTION
    except (DiffCorruptionError, _df.DiffFormatError) as exc:
        sys.stderr.write(f"diff_synthesizer differential corrupt: {exc}\n")
        return EXIT_DIFF_CORRUPTION
    except Exception as exc:
        sys.stderr.write(f"diff_synthesizer error: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
