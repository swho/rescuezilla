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
3. **Added multi-disk backup selection (`rescuezilla.glade`, `drive_query.py`, `handler.py`, `backup_manager.py`, `cli/args.py`, `rescuezilla.py`)**:
   - Adds a checkbox column (`Backup`) to the Backup Step 1 drive list (without affecting Restore or Clone drive selection tables), with fallback support for single-row highlight selection.
   - Displays all selected drives' partitions in Step 2 (prefixed with `[<drive>]` when multiple drives are selected) and excludes all selected source drives from the Step 3 destination list.
   - Preserves the exact base folder name when 1 drive is selected; when 2+ drives are selected, sequentially backs up each drive into its own standalone, upstream-compatible folder named `<base_name>-<drive>` (e.g., `2026-11-27-2015-img-rescuezilla-nvme0n1`, `...-nvme1n1`), generating independent `clonezilla-img`, `Info-img-id.txt`, and `Info-seekidx-*.rzidx` per drive, keeping `/mnt/backup` mounted across the batch, and scheduling any post-task shutdown/reboot only once after all drives finish.
   - Extends `rescuezillapy backup --source` to accept multiple drives (`--source /dev/sda /dev/sdb`).
4. **Added unit & pipeline tests** (`test/test_seekable_compressor.py`, `test/test_multi_drive_backup.py`):
   - Verifies gzip & zstd multi-frame compression, stock `gzip -d` / `zstd -d`
     decompression compatibility, O(1) random frame seeking, CRC32 validation,
     the full 3-process `partclone -> seekable_compressor -> split` `Popen`
     pipeline, Clonezilla glob non-collision, multi-disk folder naming, batch
     execution/cancellation, CLI multi-`--source` parsing, and Glade XML schema.

## Current Verification Status

- **Verified**:
  - Python unit tests (`make test`: 43 tests, including `test_seekable_compressor.py` 3-process `Popen` + GNU `split` pipeline tests and `test_multi_drive_backup.py` batch/cancel/CLI/Glade tests) and `ruff format` / pyflakes checks.
  - Full Ubuntu 24.04 (`noble`) Debian package + bootable Live ISO build (both test and clean no-SSH release ISOs) via the upstream `Dockerfile` & `Makefile`.
  - Live boot of the unmodified built ISO inside VMware Workstation (`26.0.1`), including live GTK3 + Glade UI verification against X11 (`backup_drive_toggled`, row double-click toggle, refresh preservation, Step 2 multi-disk partition prefixes, Back/Next navigation, Step 3 exclusion of all source drives, Step 5 folder naming for single- & multi-disk, Step 7 Pango confirmation markup).
  - Real multi-disk `rescuezillapy backup` (both `gzip` and `zstd` with 1 MiB frames) backing up two guest disks (`ext4` + `btrfs` + `vfat` + `bios_grub` on Disk 1; `vfat` + `btrfs` on Disk 2) into standalone `<base>-<drive>` folders, plus single-disk backup confirming no suffix is appended.
  - Full destructive wipe of both guest disks followed by live `rescuezillapy restore` (Disk 1 from `gzip`, Disk 2 from `zstd`), clean guest OS reboot, and 100% SHA-256 verification of all 12 sentinel files and 3 symlinks across both disks.
  - End-to-end O(1) instant loading and 100% SHA-256 recovery of all 4 live multi-disk `gzip` and `zstd` backup images in `rescuezilla-browser`.
- **Not yet verified**: Physical bare-metal USB boot (including UEFI Secure Boot on physical firmware) and manual mouse-click execution of an end-to-end GTK backup run.
