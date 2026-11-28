# ----------------------------------------------------------------------
#   Copyright (C) 2026 Rescuezilla Seekable-Index Community Fork Contributors
#   Derived from Rescuezilla (Copyright (C) 2019-2025 Rescuezilla.com)
#   Added 2026-11-28 (Community Fork): SeekZilla Phase 2 differential image
#   metadata (RZDMETA1), manifest and naming helpers (GPL-3.0-or-later).
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
"""On-disk formats of a SeekZilla differential (`rzdiff`) backup.

Per partition a differential backup stores:
- `<part>.<fs>-rzdiff.meta`: self-identifying `RZDMZST1`/`RZDMRAW1` envelope
  around an `RZDMETA1` payload (new Partclone header + bitmap, changed-chunk
  bitmap and offsets) protected by a SHA-256 trailer.
- `<part>.<fs>-rzdiff-img.<comp>.aa..`: concatenated used-block bytes of the
  changed chunks only, as seekable gzip/zstd frames.
- `Info-seekidx-<part>.rzdiff.rzidx`: RZSKIDX1 seek index of the delta stream.
The directory-level `Info-rzdiff-manifest.json` links the backup to its base.
"""

import bisect
import hashlib
import json
import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional, Tuple, Union

try:
    from . import block_hash as _bh  # type: ignore[import-not-found]
    from . import seekable_compressor as _sc  # type: ignore[import-not-found]
except ImportError:
    import block_hash as _bh  # type: ignore[import-not-found,no-redef]
    import seekable_compressor as _sc  # type: ignore[import-not-found,no-redef]

EXIT_FALLBACK_FULL = 42

DIFF_MANIFEST_FILENAME = "Info-rzdiff-manifest.json"
DIFF_README_FILENAME = "README-RZDIFF.txt"
DIFF_MANIFEST_FORMAT = "rzdiff"
DIFF_MANIFEST_VERSION = 1

META_MAGIC = b"RZDMETA1"
META_VERSION = 1
META_ENVELOPE_ZSTD = b"RZDMZST1"
META_ENVELOPE_RAW = b"RZDMRAW1"
META_TRAILER_SIZE = 32
ZERO_SHA256 = b"\x00" * 32

# magic, version, flags, chunk_size, block_size, totalblock, device_size,
# num_chunks, changed_chunk_count, base_bhash_sha256, base_header_sha256,
# raw_header_len, raw_bitmap_len, delta_chunk_bitmap_len, offsets_table_len
_META_HEADER_STRUCT = struct.Struct("<8sIIIIQQQQ32s32sIIIQ")
META_HEADER_SIZE = _META_HEADER_STRUCT.size
_OFFSET_ENTRY_STRUCT = struct.Struct("<QQQ")
OFFSET_ENTRY_SIZE = _OFFSET_ENTRY_STRUCT.size  # 24
_U64 = struct.Struct("<Q")

# Upper bound for a decoded RZDMETA1 payload (zip-bomb guard). 8 TB at 4 KiB
# blocks: 256 MiB bitmap + 192 MiB offsets (100% changed) + headers.
MAX_META_UNCOMPRESSED_BYTES = 512 * 1024 * 1024

SUPPORTED_DELTA_COMPRESSIONS = ("gzip", "zstd", "uncompressed")


class DiffFormatError(ValueError):
    """Corrupt or inconsistent differential metadata/manifest."""


# ----------------------------------------------------------------------
# Path safety (manifest values are untrusted: images live on external media)
# ----------------------------------------------------------------------

_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_safe_identifier(value: str, label: str) -> str:
    """`[A-Za-z0-9._-]+`, never `.`/`..`/containing `..` (partition / fs names)."""
    if (
        not isinstance(value, str)
        or not _SAFE_IDENTIFIER_RE.fullmatch(value)
        or value in (".", "..")
        or ".." in value
    ):
        raise DiffFormatError(f"Unsafe {label} {value!r} in rzdiff manifest")
    return value


def validate_safe_basename(value: str, label: str) -> str:
    """A plain file name: non-empty, no `/` or `\\`, not `.`/`..`, no NUL."""
    if (
        not isinstance(value, str)
        or not value
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or value in (".", "..")
        or os.path.basename(value) != value
    ):
        raise DiffFormatError(f"Unsafe {label} {value!r} in rzdiff manifest")
    return value


def assert_path_within(child_path, root_dir, *, allow_equal: bool = False) -> str:
    """Return realpath(child_path); raise DiffFormatError unless it is inside root_dir.

    Symlinks are resolved first, so a link pointing outside `root_dir` is
    rejected. `allow_equal=True` also accepts the root itself (dir checks).
    """
    root = os.path.realpath(os.fspath(root_dir))
    child = os.path.realpath(os.fspath(child_path))
    try:
        common = os.path.commonpath([root, child])
    except ValueError:
        common = ""
    if common != root or (child == root and not allow_equal):
        raise DiffFormatError(f"Path {os.fspath(child_path)!r} escapes {root!r}")
    return child


def validate_partition_entry(short_part, entry) -> dict:
    """Validate one manifest partition entry; returns a copy with defaults filled.

    Every file name must be a plain basename and the partition/fs names safe
    identifiers, so no manifest value can address a path outside its dir.
    """
    validate_safe_identifier(short_part, "partition name")
    if not isinstance(entry, dict):
        raise DiffFormatError(f"Manifest entry for {short_part!r} is not an object")
    out = dict(entry)
    validate_safe_identifier(out.get("fs"), "filesystem name")
    if out.get("compression") not in SUPPORTED_DELTA_COMPRESSIONS:
        raise DiffFormatError(
            f"Unsupported delta compression {out.get('compression')!r} for {short_part}"
        )
    for key in ("meta", "data_prefix", "seekidx"):
        if key not in out:
            raise DiffFormatError(f"Manifest entry for {short_part} lacks {key!r}")
        validate_safe_basename(out[key], key)
    out.setdefault("base_bhash", _bh.bhash_index_filename(short_part))
    out.setdefault("base_seekidx", f"Info-seekidx-{short_part}.rzidx")
    validate_safe_basename(out["base_bhash"], "base_bhash")
    validate_safe_basename(out["base_seekidx"], "base_seekidx")
    prefix = out.get("base_image_prefix")
    if prefix is not None:
        validate_safe_basename(prefix, "base_image_prefix")
        if _bh.parse_ptcl_split_name(prefix + "aa") is None:
            raise DiffFormatError(f"Invalid base_image_prefix {prefix!r}")
    sha_hex = out.get("base_bhash_sha256")
    if sha_hex is not None:
        if not isinstance(sha_hex, str):
            raise DiffFormatError("Manifest base_bhash_sha256 is not a string")
        try:
            if len(bytes.fromhex(sha_hex)) != 32:
                raise ValueError("not 32 bytes")
        except ValueError as exc:
            raise DiffFormatError(f"Manifest base_bhash_sha256 is invalid: {exc}") from exc
    return out


# ----------------------------------------------------------------------
# Naming helpers
# ----------------------------------------------------------------------


def get_diff_meta_filename(short_part: str, fs: str) -> str:
    return f"{short_part}.{fs}-rzdiff.meta"


def get_diff_data_prefix(short_part: str, fs: str, comp_suffix: str) -> str:
    return f"{short_part}.{fs}-rzdiff-img.{comp_suffix}."


def get_diff_seekidx_filename(short_part: str) -> str:
    return f"Info-seekidx-{short_part}.rzdiff.rzidx"


def is_diff_artifact_filename(name: str) -> bool:
    """True for files that only exist in a differential backup directory."""
    return (
        name == DIFF_MANIFEST_FILENAME
        or name == DIFF_README_FILENAME
        or "-rzdiff.meta" in name
        or "-rzdiff-img." in name
        or name.endswith(".rzdiff.rzidx")
    )


# ----------------------------------------------------------------------
# Chunk bitmap helpers
# ----------------------------------------------------------------------


def chunk_bit_is_set(bitmap: bytes, chunk_idx: int) -> bool:
    return bool((bitmap[chunk_idx >> 3] >> (chunk_idx & 7)) & 1)


def set_chunk_bit(bitmap: bytearray, chunk_idx: int) -> None:
    bitmap[chunk_idx >> 3] |= 1 << (chunk_idx & 7)


# ----------------------------------------------------------------------
# Delta offset table (packed `<QQQ`, 24 bytes/entry)
# ----------------------------------------------------------------------

OffsetEntry = Tuple[int, int, int]  # (chunk_idx, delta_offset, length)


class _ChunkKeyView:
    """Read-only sequence of the chunk_idx column of a packed offset buffer."""

    __slots__ = ("_buf", "_n")

    def __init__(self, buf, n: int):
        self._buf = buf
        self._n = n

    def __len__(self) -> int:
        return self._n

    def __getitem__(self, i: int) -> int:
        return _U64.unpack_from(self._buf, i * OFFSET_ENTRY_SIZE)[0]


class DeltaOffsetTable:
    """Compact `(chunk_idx, delta_offset, length)` table stored as packed `<QQQ`.

    24 bytes per changed chunk instead of a ~150-byte Python tuple (about 6x
    less RAM: 24 MiB vs 148 MiB for 1 TiB fully changed at 1 MiB chunks). It
    behaves like a read-only sequence of tuples (`len`, indexing, iteration)
    and also offers `find_chunk()` (binary search on the packed bytes).
    """

    __slots__ = ("_buf",)

    def __init__(self, packed: Union[bytes, bytearray] = b""):
        if not isinstance(packed, (bytes, bytearray)):
            packed = bytes(packed)
        if len(packed) % OFFSET_ENTRY_SIZE:
            raise DiffFormatError(
                f"Packed offset table length {len(packed)} is not a multiple of "
                f"{OFFSET_ENTRY_SIZE}"
            )
        self._buf: Union[bytes, bytearray] = packed

    @classmethod
    def from_entries(cls, entries: Iterable[OffsetEntry]) -> "DeltaOffsetTable":
        table = cls(bytearray())
        for chunk_idx, off, ln in entries:
            table.append(chunk_idx, off, ln)
        return table

    @property
    def packed(self) -> Union[bytes, bytearray]:
        """The underlying packed buffer (not copied)."""
        return self._buf

    def append(self, chunk_idx: int, offset: int, length: int) -> None:
        if not isinstance(self._buf, bytearray):
            self._buf = bytearray(self._buf)
        try:
            self._buf += _OFFSET_ENTRY_STRUCT.pack(chunk_idx, offset, length)
        except struct.error as exc:
            raise DiffFormatError(f"Invalid delta offset entry: {exc}") from exc

    def __len__(self) -> int:
        return len(self._buf) // OFFSET_ENTRY_SIZE

    def __getitem__(self, i: int) -> OffsetEntry:
        if not isinstance(i, int):
            raise TypeError("DeltaOffsetTable indices must be integers")
        n = len(self)
        if i < 0:
            i += n
        if not 0 <= i < n:
            raise IndexError("delta offset index out of range")
        return _OFFSET_ENTRY_STRUCT.unpack_from(self._buf, i * OFFSET_ENTRY_SIZE)

    def __iter__(self) -> Iterator[OffsetEntry]:
        # Zero-copy; the buffer must not be appended to while iterating.
        return _OFFSET_ENTRY_STRUCT.iter_unpack(self._buf)

    def find_chunk(self, chunk_idx: int) -> Optional[Tuple[int, int]]:
        """(delta_offset, length) of `chunk_idx`, or None; O(log n), needs sorted entries."""
        n = len(self)
        i = bisect.bisect_left(_ChunkKeyView(self._buf, n), chunk_idx)
        if i < n:
            c, off, ln = self[i]
            if c == chunk_idx:
                return off, ln
        return None

    def __eq__(self, other) -> bool:
        if isinstance(other, DeltaOffsetTable):
            return self._buf == other._buf
        if isinstance(other, (list, tuple)):
            return list(self) == [tuple(e) for e in other]
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"DeltaOffsetTable(<{len(self)} entries>)"


# ----------------------------------------------------------------------
# RZDMETA1
# ----------------------------------------------------------------------


@dataclass
class DiffMeta:
    chunk_size: int
    block_size: int
    totalblock: int
    device_size: int
    num_chunks: int
    base_bhash_sha256: bytes
    base_header_sha256: bytes
    raw_partclone_header: bytes = field(repr=False)
    raw_partclone_bitmap: bytes = field(repr=False)
    delta_chunk_bitmap: bytes = field(repr=False)
    # Always stored as a DeltaOffsetTable (lists of tuples are packed on assignment).
    delta_chunk_offsets: DeltaOffsetTable = field(repr=False)
    flags: int = 0
    version: int = META_VERSION

    def __setattr__(self, name, value) -> None:
        if name == "delta_chunk_offsets" and not isinstance(value, DeltaOffsetTable):
            value = (
                DeltaOffsetTable(value)
                if isinstance(value, (bytes, bytearray))
                else DeltaOffsetTable.from_entries(value)
            )
        object.__setattr__(self, name, value)

    # --- offset table accessors -------------------------------------------

    @property
    def offsets(self) -> DeltaOffsetTable:
        return self.delta_chunk_offsets

    def offsets_count(self) -> int:
        return len(self.delta_chunk_offsets)

    def get_offset(self, i: int) -> OffsetEntry:
        return self.delta_chunk_offsets[i]

    def iter_offsets(self) -> Iterator[OffsetEntry]:
        return iter(self.delta_chunk_offsets)

    def find_chunk(self, chunk_idx: int) -> Optional[Tuple[int, int]]:
        return self.delta_chunk_offsets.find_chunk(chunk_idx)

    @property
    def changed_chunk_count(self) -> int:
        return len(self.delta_chunk_offsets)

    @property
    def delta_stream_size(self) -> int:
        if not len(self.delta_chunk_offsets):
            return 0
        _c, off, ln = self.delta_chunk_offsets[-1]
        return off + ln

    def is_changed(self, chunk_idx: int) -> bool:
        return chunk_bit_is_set(self.delta_chunk_bitmap, chunk_idx)

    def validate(self) -> None:
        """Structural consistency checks (raises DiffFormatError)."""
        for name in ("chunk_size", "block_size", "totalblock", "device_size", "num_chunks"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise DiffFormatError(f"Invalid RZDMETA1 {name} {value!r}")
        if self.block_size <= 0 or self.chunk_size <= 0:
            raise DiffFormatError(
                f"Invalid RZDMETA1 geometry block_size={self.block_size} "
                f"chunk_size={self.chunk_size}"
            )
        if len(self.base_bhash_sha256) != 32 or len(self.base_header_sha256) != 32:
            raise DiffFormatError("SHA-256 fields must be 32 bytes")
        if len(self.delta_chunk_bitmap) != (self.num_chunks + 7) // 8:
            raise DiffFormatError(
                f"delta_chunk_bitmap length {len(self.delta_chunk_bitmap)} != "
                f"({self.num_chunks} + 7) // 8"
            )
        rem = self.num_chunks % 8
        if rem and self.delta_chunk_bitmap[-1] >> rem:
            raise DiffFormatError("delta_chunk_bitmap has bits set beyond num_chunks")
        popcount = int.from_bytes(self.delta_chunk_bitmap, "little").bit_count()
        if popcount != len(self.delta_chunk_offsets):
            raise DiffFormatError(
                f"delta_chunk_bitmap popcount {popcount} != "
                f"changed_chunk_count {len(self.delta_chunk_offsets)}"
            )
        expected_off = 0
        prev = -1
        for chunk_idx, off, ln in self.delta_chunk_offsets:
            if chunk_idx <= prev or chunk_idx >= self.num_chunks:
                raise DiffFormatError(f"delta offset entry chunk {chunk_idx} out of order")
            if not chunk_bit_is_set(self.delta_chunk_bitmap, chunk_idx):
                raise DiffFormatError(f"chunk {chunk_idx} has offset but bit is 0")
            if off != expected_off:
                raise DiffFormatError(
                    f"delta offset for chunk {chunk_idx} is {off}, expected {expected_off}"
                )
            if ln <= 0 or ln % self.block_size:
                raise DiffFormatError(f"invalid delta length {ln} for chunk {chunk_idx}")
            expected_off += ln
            prev = chunk_idx

    def payload_bytes(self) -> bytes:
        self.validate()
        offsets = self.delta_chunk_offsets.packed
        head = _META_HEADER_STRUCT.pack(
            META_MAGIC,
            self.version,
            self.flags,
            self.chunk_size,
            self.block_size,
            self.totalblock,
            self.device_size,
            self.num_chunks,
            len(self.delta_chunk_offsets),
            self.base_bhash_sha256,
            self.base_header_sha256,
            len(self.raw_partclone_header),
            len(self.raw_partclone_bitmap),
            len(self.delta_chunk_bitmap),
            len(offsets),
        )
        parts = (
            head,
            self.raw_partclone_header,
            self.raw_partclone_bitmap,
            self.delta_chunk_bitmap,
            offsets,
        )
        total_len = sum(len(part) for part in parts) + META_TRAILER_SIZE
        if total_len > MAX_META_UNCOMPRESSED_BYTES:
            raise DiffFormatError(
                f"RZDMETA1 payload ({total_len} bytes) exceeds {MAX_META_UNCOMPRESSED_BYTES} bytes"
            )
        h = hashlib.sha256()
        for part in parts:
            h.update(part)
        # One join (no intermediate body copy) incl. the SHA-256 trailer.
        return b"".join(parts + (h.digest(),))

    def serialize(self, compress: bool = True) -> bytes:
        payload = self.payload_bytes()
        if compress and _sc.has_native_zstd():
            return META_ENVELOPE_ZSTD + _sc.compress_zstd_frame(payload, 9)
        return META_ENVELOPE_RAW + payload


def parse_diff_meta_payload(payload) -> DiffMeta:
    if len(payload) < META_HEADER_SIZE + META_TRAILER_SIZE:
        raise DiffFormatError("RZDMETA1 payload is truncated")
    with memoryview(payload) as whole:
        # memoryview slices: no copy of the (possibly hundreds of MiB) body.
        with whole[:-META_TRAILER_SIZE] as body:
            if hashlib.sha256(body).digest() != bytes(whole[-META_TRAILER_SIZE:]):
                raise DiffFormatError("RZDMETA1 SHA-256 trailer mismatch")
            return _parse_meta_body(body)


def _parse_meta_body(body: memoryview) -> DiffMeta:
    (
        magic,
        version,
        flags,
        chunk_size,
        block_size,
        totalblock,
        device_size,
        num_chunks,
        changed,
        base_bhash_sha256,
        base_header_sha256,
        raw_header_len,
        raw_bitmap_len,
        dbm_len,
        offsets_len,
    ) = _META_HEADER_STRUCT.unpack_from(body, 0)
    if magic != META_MAGIC:
        raise DiffFormatError(f"Invalid RZDMETA1 magic {magic!r}")
    if version != META_VERSION:
        raise DiffFormatError(f"Unsupported RZDMETA1 version {version}")
    if offsets_len != changed * OFFSET_ENTRY_SIZE:
        raise DiffFormatError("RZDMETA1 offsets_table_len != changed_chunk_count * 24")
    expected = META_HEADER_SIZE + raw_header_len + raw_bitmap_len + dbm_len + offsets_len
    if len(body) != expected:
        raise DiffFormatError(f"RZDMETA1 length mismatch: {len(body)} != {expected}")
    pos = META_HEADER_SIZE
    raw_header = bytes(body[pos : pos + raw_header_len])
    pos += raw_header_len
    raw_bitmap = bytes(body[pos : pos + raw_bitmap_len])
    pos += raw_bitmap_len
    dbm = bytes(body[pos : pos + dbm_len])
    pos += dbm_len
    offsets = DeltaOffsetTable(bytes(body[pos : pos + offsets_len]))
    meta = DiffMeta(
        chunk_size=chunk_size,
        block_size=block_size,
        totalblock=totalblock,
        device_size=device_size,
        num_chunks=num_chunks,
        base_bhash_sha256=base_bhash_sha256,
        base_header_sha256=base_header_sha256,
        raw_partclone_header=raw_header,
        raw_partclone_bitmap=raw_bitmap,
        delta_chunk_bitmap=dbm,
        delta_chunk_offsets=offsets,
        flags=flags,
        version=version,
    )
    meta.validate()
    return meta


def parse_diff_meta(blob: bytes) -> DiffMeta:
    envelope = bytes(blob[:8])
    limit = MAX_META_UNCOMPRESSED_BYTES
    if envelope == META_ENVELOPE_RAW:
        if len(blob) - 8 > limit:
            raise DiffFormatError(f"RZDMETA1 payload exceeds {limit} bytes")
        with memoryview(blob) as mv:
            return parse_diff_meta_payload(mv[8:])
    if envelope == META_ENVELOPE_ZSTD:
        try:
            # Bounded streaming decode: a zip-bomb fails fast without OOM.
            payload = _sc.decompress_zstd_frame(blob[8:], max_output=limit)
        except (_sc.StreamDecompressError, RuntimeError) as exc:
            raise DiffFormatError(f"RZDMETA1 zstd envelope is corrupt: {exc}") from exc
        return parse_diff_meta_payload(payload)
    raise DiffFormatError(f"Unknown rzdiff meta envelope {envelope!r}")


def load_diff_meta(path) -> DiffMeta:
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        if size > MAX_META_UNCOMPRESSED_BYTES + 8:
            raise DiffFormatError(f"rzdiff meta file {path} is too large ({size} bytes)")
        return parse_diff_meta(f.read())


def write_diff_meta(path, meta: DiffMeta, compress: bool = True) -> None:
    _bh.atomic_write_bytes(path, meta.serialize(compress=compress))


# ----------------------------------------------------------------------
# Manifest + README
# ----------------------------------------------------------------------


def make_partition_entry(
    *,
    short_part: str,
    fs: str,
    compression: str,
    base_bhash_sha256: bytes,
    base_image_prefix: Optional[str] = None,
) -> dict:
    """Manifest entry for one differential partition (file names relative to dirs)."""
    comp_suffix = _bh.FORMAT_TO_COMPRESSION_SUFFIX[compression]
    entry = {
        "fs": fs,
        "compression": compression,
        "meta": get_diff_meta_filename(short_part, fs),
        "data_prefix": get_diff_data_prefix(short_part, fs, comp_suffix),
        "seekidx": get_diff_seekidx_filename(short_part),
        "base_bhash": _bh.bhash_index_filename(short_part),
        "base_bhash_sha256": bytes(base_bhash_sha256).hex(),
        "base_seekidx": f"Info-seekidx-{short_part}.rzidx",
    }
    if base_image_prefix:
        entry["base_image_prefix"] = base_image_prefix
    return entry


def make_manifest(
    *, base_relative_path: str, base_suggested_dir_name: str, partitions: dict
) -> dict:
    return {
        "format": DIFF_MANIFEST_FORMAT,
        "version": DIFF_MANIFEST_VERSION,
        "base_image": {
            "relative_path": base_relative_path,
            "suggested_dir_name": base_suggested_dir_name,
        },
        "partitions": partitions,
    }


def write_diff_manifest(diff_dir, manifest_dict: dict) -> None:
    blob = (json.dumps(manifest_dict, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _bh.atomic_write_bytes(os.path.join(os.fspath(diff_dir), DIFF_MANIFEST_FILENAME), blob)


def load_diff_manifest(diff_dir) -> Optional[dict]:
    """Return the manifest dict, None if absent; raises DiffFormatError if invalid."""
    path = os.path.join(os.fspath(diff_dir), DIFF_MANIFEST_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError) as exc:
        raise DiffFormatError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != DIFF_MANIFEST_FORMAT:
        raise DiffFormatError(f"{path} is not an rzdiff manifest")
    if manifest.get("version") != DIFF_MANIFEST_VERSION:
        raise DiffFormatError(f"Unsupported rzdiff manifest version {manifest.get('version')}")
    if not isinstance(manifest.get("partitions"), dict) or not isinstance(
        manifest.get("base_image"), dict
    ):
        raise DiffFormatError(f"{path} lacks base_image/partitions")
    # Fail-closed before any caller derives a path from an entry.
    for short_part, entry in manifest["partitions"].items():
        validate_partition_entry(short_part, entry)
    base = manifest["base_image"]
    for key in ("relative_path", "suggested_dir_name"):
        value = base.get(key)
        if value is not None and not isinstance(value, str):
            raise DiffFormatError(f"Manifest base_image.{key} is not a string")
    if base.get("suggested_dir_name"):
        validate_safe_basename(base["suggested_dir_name"], "base_image.suggested_dir_name")
    return manifest


def resolve_base_dir(diff_dir, manifest: dict, explicit_base_dir=None) -> Path:
    """Locate the base image dir: explicit -> relative_path -> sibling suggested name."""
    diff_dir = Path(diff_dir)
    tried = []
    if explicit_base_dir is not None:
        p = Path(explicit_base_dir)
        if p.is_dir():
            return p
        raise FileNotFoundError(f"Explicit base image directory not found: {p}")
    base = manifest.get("base_image") or {}
    rel = base.get("relative_path")
    if rel:
        p = diff_dir / rel
        tried.append(str(p))
        if p.is_dir():
            return p
    name = base.get("suggested_dir_name")
    if name:
        p = diff_dir.parent / name
        tried.append(str(p))
        if p.is_dir():
            return p
    raise FileNotFoundError(
        "Base image directory for differential backup "
        f"{diff_dir} not found (tried: {', '.join(tried) or 'nothing'}); "
        "pass --base-dir explicitly / 找不到差異備份的基礎映像目錄，請以 --base-dir 指定"
    )


def write_diff_readme(diff_dir, base_name: str) -> None:
    text = (
        "SeekZilla differential backup (rzdiff)\n"
        "======================================\n\n"
        "[繁體中文]\n"
        "本目錄是「差異備份」，只儲存相對於基礎映像有變更的資料區塊，\n"
        f"無法單獨還原，必須搭配基礎映像目錄：{base_name}\n"
        "請勿刪除、移動或修改基礎映像，否則本差異備份將無法還原。\n"
        "可用以下指令合併為獨立的完整 Rescuezilla 映像：\n"
        "  python3 -m rescuezilla.diff_synthesizer merge --diff-dir <本目錄> "
        "--output-dir <新目錄>\n\n"
        "[English]\n"
        "This directory is a DIFFERENTIAL backup. It only stores the data chunks\n"
        "that changed relative to its base image and cannot be restored alone.\n"
        f"Required base image directory: {base_name}\n"
        "Do not delete, move, or modify the base image, or this backup becomes\n"
        "unrestorable. To flatten it into a standalone full Rescuezilla image:\n"
        "  python3 -m rescuezilla.diff_synthesizer merge --diff-dir <this dir> "
        "--output-dir <new dir>\n"
    )
    _bh.atomic_write_bytes(
        os.path.join(os.fspath(diff_dir), DIFF_README_FILENAME), text.encode("utf-8")
    )
