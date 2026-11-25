# Fork Notice & GPL-3.0-or-later Compliance

This repository is an **unofficial derivative fork** of **Rescuezilla**
(<https://github.com/rescuezilla/rescuezilla>), originally created and maintained
by Shasheen Ediriweera / Rescuezilla.com and contributors, derived from Redo Backup
and Recovery and Clonezilla.

This fork is **not affiliated with, endorsed by, or supported by** the official
Rescuezilla project.

## License

In full compliance with the upstream **GNU General Public License v3.0 or later
(`GPL-3.0-or-later`)**:

- The complete upstream `LICENSE` file and all original copyright headers in
  source files and `debian/copyright` are preserved verbatim.
- All modifications and newly added files in this fork are released under the
  same **GNU General Public License v3.0 or later**.
- Every modified file carries a prominent modification notice with date per
  GPLv3 Section 5(a).
- If you build or distribute modified binary packages (`.deb`) or Live ISOs
  from this fork, GPLv3 requires making the corresponding complete source code
  available under GPLv3 (keeping your GitHub fork public satisfies this).

## Summary of Modifications (`feature/seekable-index`)

1. **Added `seekable_compressor.py`** (`src/apps/rescuezilla/rescuezilla/usr/lib/python3/dist-packages/rescuezilla/seekable_compressor.py`):
   - Parallel multi-frame compressor supporting `gzip` (RFC 1952 concatenated
     independent members) and `zstd` (RFC 8878 concatenated independent frames),
     defaulting to **1 MiB** uncompressed frames (16× finer random-access
     granularity than 16 MiB with <0.05% size overhead) and bounded RAM usage.
   - Atomically generates `Info-seekidx-<partition>.rzidx` (binary format
     `RZSKIDX1` with trailing IEEE CRC32; ~16 MiB per 1 TiB uncompressed).
   - Output streams remain 100% standard `.gz` / `.zst` files fully compatible
     with stock Rescuezilla, Clonezilla, `gzip -d`, `pigz -d`, and `zstd -d`.
2. **Updated `utility.py` and `backup_manager.py`**:
   - Generates `Info-seekidx-<part>.rzidx` alongside split partition images
     during `gzip` and `zstd` backups (enabled by default; disable via
     `RESCUEZILLA_SEEKABLE_INDEX=0` or `compression_dict["seekable_index"] = False`).
   - Uses the `Info-*` naming convention so upstream Rescuezilla / Clonezilla
     split globs (`*-ptcl-img.*`) ignore the index file during restore.
   - Waits cleanly for compression and split pipeline exit codes and removes
     any `.rzidx` / `.tmp` artifacts if a partition backup fails.
3. **Added unit & 3-process pipeline tests** (`test/test_seekable_compressor.py`):
   - Verifies gzip & zstd multi-frame compression, stock `gzip -d` / `zstd -d`
     decompression compatibility, O(1) random frame seeking, CRC32 validation,
     the full 3-process `partclone -> seekable_compressor -> split` `Popen`
     pipeline, and Clonezilla glob non-collision.

## Current Verification Status

- **Verified**: Python unit tests, 3-process `Popen` + GNU `split` pipeline
  tests, stock `gzip -d` / `zstd -d` decompression compatibility, and
  end-to-end random-access reading with `rescuezilla-browser`.
- **Not yet verified**: Full Debian package / bootable Live ISO build and
  live bare-metal `BackupManager` run inside a Rescuezilla Live environment.
