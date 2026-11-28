#!/usr/bin/env python3
# ----------------------------------------------------------------------
#   Copyright (C) 2026 Rescuezilla Seekable-Index Community Fork Contributors
#   Derived from Rescuezilla (Copyright (C) 2019-2025 Rescuezilla.com)
#   Added 2026-11-28 (Community Fork): SeekZilla Phase 2 streaming
#   differential writer (GPL-3.0-or-later).
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
"""Streaming differential writer: `partclone.<fs> -c -o - | diff_writer | split`.

Reads an uncompressed Partclone v0002 stream from stdin, compares every
logical chunk digest with the base `.rzbhash`, and writes only the used-block
bytes of changed chunks as seekable gzip/zstd frames to stdout, plus atomic
`RZDMETA1` metadata and `RZSKIDX1` seek index files.

Exit codes: 0 success, 42 (EXIT_FALLBACK_FULL) incompatible base / not a
Partclone v0002 stream (nothing written; caller should do a full backup),
1 error (corrupt input, empty stdin because partclone died, etc.), 141 broken
pipe.
"""

import argparse
import json
import signal
import sys
import time
from typing import List, Optional

try:
    from . import block_hash as _bh  # type: ignore[import-not-found]
    from . import diff_format as _df  # type: ignore[import-not-found]
    from . import seekable_compressor as _sc  # type: ignore[import-not-found]
except ImportError:
    import block_hash as _bh  # type: ignore[import-not-found,no-redef]
    import diff_format as _df  # type: ignore[import-not-found,no-redef]
    import seekable_compressor as _sc  # type: ignore[import-not-found,no-redef]

STATS_LINE_PREFIX = "[seekable_diff]"
_READ_SIZE = 4 * 1024 * 1024


class FallbackFullError(Exception):
    """The new stream cannot be diffed against the base; do a full backup."""


class _DiffSink(_bh.ChunkSink):
    def __init__(self, base: _bh.BlockHashIndex, frame_writer_factory):
        self.base = base
        self._frame_writer_factory = frame_writer_factory
        self.frame_writer: Optional[_sc.SeekableFrameWriter] = None
        self.delta_bitmap = bytearray()
        # Packed <QQQ entries (24 B each) instead of a list of tuples.
        self.offsets = _df.DeltaOffsetTable()
        self.delta_offset = 0
        self.changed_chunks = 0
        self.unchanged_chunks = 0
        self.used_bytes_total = 0
        self._hasher = None
        self._chunk_buf = bytearray()
        self._chunk_meta = None

    def on_header(self, header: _bh.PartcloneHeader) -> None:
        base = self.base
        mismatches = []
        if header.fs != base.fs_name:
            mismatches.append(f"fs {header.fs_str!r} != base {base.fs_str!r}")
        if header.block_size != base.block_size:
            mismatches.append(f"block_size {header.block_size} != base {base.block_size}")
        if header.totalblock != base.totalblock:
            mismatches.append(f"totalblock {header.totalblock} != base {base.totalblock}")
        if header.device_size != base.device_size:
            mismatches.append(
                f"device_size {header.device_size} != base {base.device_size}"
            )
        if mismatches:
            raise FallbackFullError("; ".join(mismatches))

    def on_bitmap(self, parser: _bh.PartcloneStreamParser) -> None:
        if parser.chunk_size != self.base.chunk_size or parser.num_chunks != self.base.num_chunks:
            raise FallbackFullError(
                f"chunk geometry {parser.chunk_size}/{parser.num_chunks} != base "
                f"{self.base.chunk_size}/{self.base.num_chunks}"
            )
        delta_bitmap_len = (parser.num_chunks + 7) // 8
        worst_meta_bytes = (
            _df.META_HEADER_SIZE
            + len(parser.raw_header_bytes)
            + len(parser.raw_bitmap_bytes)
            + delta_bitmap_len
            + parser.num_chunks * _df.OFFSET_ENTRY_SIZE
            + _df.META_TRAILER_SIZE
        )
        if worst_meta_bytes > _df.MAX_META_UNCOMPRESSED_BYTES:
            raise FallbackFullError(
                f"worst-case RZDMETA1 payload ({worst_meta_bytes} bytes) exceeds "
                f"limit ({_df.MAX_META_UNCOMPRESSED_BYTES} bytes)"
            )
        self.delta_bitmap = bytearray(delta_bitmap_len)
        # Only now (compatibility proven) may stdout output begin.
        self.frame_writer = self._frame_writer_factory()

    def on_chunk_start(self, chunk_idx, bm_slice, used_count, n_blocks) -> None:
        self._hasher = _bh.new_chunk_hasher(n_blocks, bm_slice)
        self._chunk_buf = bytearray()

    def on_chunk_data(self, data) -> None:
        self._hasher.update(data)
        self._chunk_buf += data

    def on_chunk_end(self, chunk_idx) -> None:
        digest = self._hasher.digest()
        self._hasher = None
        used = self._chunk_buf
        self._chunk_buf = bytearray()
        self.used_bytes_total += len(used)
        if digest == self.base.get_digest(chunk_idx):
            self.unchanged_chunks += 1
            return
        self.changed_chunks += 1
        _df.set_chunk_bit(self.delta_bitmap, chunk_idx)
        self.offsets.append(chunk_idx, self.delta_offset, len(used))
        self.delta_offset += len(used)
        self.frame_writer.write(used)


def write_diff_stream(
    stdin_bin,
    stdout_bin,
    *,
    base_bhash_path: str,
    meta_output: str,
    index_output: str,
    base_header_sha256: Optional[bytes] = None,
    fmt: str = "zstd",
    level: int = 3,
    frame_size: Optional[int] = None,
    threads: Optional[int] = None,
    stats_json_output: Optional[str] = None,
) -> dict:
    """Run the differential writer; returns a stats dict.

    Raises FallbackFullError before any stdout byte or output file is written
    when the new stream is not compatible with the base `.rzbhash`.
    """
    try:
        base = _bh.load_bhash_file(base_bhash_path)
    except (_bh.BlockHashFormatError, OSError) as exc:
        raise FallbackFullError(f"base .rzbhash unusable: {exc}") from exc
    if base_header_sha256 is None:
        base_header_sha256 = _df.ZERO_SHA256
    if len(base_header_sha256) != 32:
        raise ValueError("base_header_sha256 must be 32 bytes")

    comp_stats: dict = {}

    def factory():
        return _sc.SeekableFrameWriter(
            stdout_bin,
            fmt=fmt,
            level=level,
            frame_size=frame_size,
            threads=threads,
            stats=comp_stats,
        )

    sink = _DiffSink(base, factory)
    parser = _bh.PartcloneStreamParser(sink, chunk_size=base.chunk_size)
    t0 = time.monotonic()
    try:
        while True:
            piece = stdin_bin.read(_READ_SIZE)
            if not piece:
                break
            try:
                parser.feed(piece)
            except (_bh.PartcloneUnsupportedError, _bh.ChunkGeometryError) as exc:
                raise FallbackFullError(str(exc)) from exc
            if parser.is_partclone is False:
                raise FallbackFullError("input is not a Partclone v0002 image stream")
        if parser.is_partclone is None:
            # 0 bytes, or EOF inside the magic: partclone died before/while
            # writing its header. That is an error, not a reason to fall back.
            raise _bh.PartcloneStreamError(
                "empty input stream: partclone produced no image header"
                if parser.bytes_consumed == 0
                else f"input ended after {parser.bytes_consumed} bytes inside the "
                "Partclone magic"
            )
        parser.finish()
        if not parser.is_partclone:
            raise FallbackFullError("input is not a Partclone v0002 image stream")
        frames, comp_total, decomp_total = sink.frame_writer.close()
    except BaseException:
        if sink.frame_writer is not None:
            sink.frame_writer.abort()
        raise

    if decomp_total != sink.delta_offset:
        raise RuntimeError(
            f"delta stream size mismatch: {decomp_total} != {sink.delta_offset}"
        )
    header = parser.header
    meta = _df.DiffMeta(
        chunk_size=parser.chunk_size,
        block_size=header.block_size,
        totalblock=header.totalblock,
        device_size=header.device_size,
        num_chunks=parser.num_chunks,
        base_bhash_sha256=base.file_sha256,
        base_header_sha256=bytes(base_header_sha256),
        raw_partclone_header=parser.raw_header_bytes,
        raw_partclone_bitmap=parser.raw_bitmap_bytes,
        delta_chunk_bitmap=bytes(sink.delta_bitmap),
        delta_chunk_offsets=sink.offsets,
    )
    meta_blob = meta.serialize()
    index_blob = sink.frame_writer.serialize_index()

    non_empty = sink.changed_chunks + sink.unchanged_chunks
    used_total = sink.used_bytes_total
    saved_pct = (100.0 * (1.0 - sink.delta_offset / used_total)) if used_total else 100.0
    stats = {
        "fmt": comp_stats.get("fmt"),
        "level": comp_stats.get("level"),
        "backend": comp_stats.get("backend"),
        "frame_size": comp_stats.get("frame_size"),
        "num_chunks": parser.num_chunks,
        "chunk_size": parser.chunk_size,
        "changed_chunks": sink.changed_chunks,
        "unchanged_chunks": sink.unchanged_chunks,
        "empty_chunks": parser.num_chunks - non_empty,
        "used_bytes": used_total,
        "delta_bytes": sink.delta_offset,
        "compressed_bytes": comp_total,
        "frames": frames,
        "saved_pct": round(saved_pct, 2),
        "elapsed": round(time.monotonic() - t0, 3),
        "base_bhash_sha256": base.file_sha256.hex(),
    }

    _bh.atomic_write_bytes(meta_output, meta_blob)
    _bh.atomic_write_bytes(index_output, index_blob)
    if stats_json_output:
        _bh.atomic_write_bytes(
            stats_json_output,
            (json.dumps(stats, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
    return stats


def format_stats_line(stats: dict) -> str:
    return (
        f"{STATS_LINE_PREFIX} fmt={stats.get('fmt')} level={stats.get('level')} "
        f"backend={stats.get('backend')} chunks={stats.get('num_chunks')} "
        f"changed_chunks={stats.get('changed_chunks')} "
        f"unchanged_chunks={stats.get('unchanged_chunks')} "
        f"empty_chunks={stats.get('empty_chunks')} used_bytes={stats.get('used_bytes')} "
        f"delta_bytes={stats.get('delta_bytes')} out_bytes={stats.get('compressed_bytes')} "
        f"saved_pct={stats.get('saved_pct'):.2f} elapsed={stats.get('elapsed'):.3f}s"
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="SeekZilla streaming differential writer (Partclone v0002 stdin -> delta frames stdout)"
    )
    parser.add_argument("--base-bhash", required=True)
    parser.add_argument("--meta-output", required=True)
    parser.add_argument("--index-output", required=True)
    parser.add_argument("--base-header-sha256", default=None)
    parser.add_argument("--format", default="zstd", choices=["zstd", "gzip"])
    parser.add_argument("--level", type=int, default=3)
    parser.add_argument("--frame-size", type=int, default=None)
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument("--stats-json-output", default=None)
    parser.add_argument("--print-stats", action="store_true", default=False)
    args = parser.parse_args(argv)

    base_header_sha256 = None
    if args.base_header_sha256:
        try:
            base_header_sha256 = bytes.fromhex(args.base_header_sha256)
        except ValueError:
            base_header_sha256 = b""
        if len(base_header_sha256) != 32:
            sys.stderr.write("diff_writer error: --base-header-sha256 must be 64 hex chars\n")
            return 2

    def _on_signal(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        stats = write_diff_stream(
            sys.stdin.buffer,
            sys.stdout.buffer,
            base_bhash_path=args.base_bhash,
            meta_output=args.meta_output,
            index_output=args.index_output,
            base_header_sha256=base_header_sha256,
            fmt=args.format,
            level=args.level,
            frame_size=args.frame_size,
            threads=args.threads,
            stats_json_output=args.stats_json_output,
        )
    except FallbackFullError as exc:
        sys.stderr.write(f"diff_writer fallback-full: {exc}\n")
        return _df.EXIT_FALLBACK_FULL
    except BrokenPipeError:
        return 141
    except Exception as exc:
        sys.stderr.write(f"diff_writer error: {exc}\n")
        return 1
    if args.print_stats:
        sys.stderr.write(format_stats_line(stats) + "\n")
        sys.stderr.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
