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
     independent members) and `zstd` (RFC 8878 concatenated independent frames
     with per-frame 32-bit XXH64 content checksums enabled to match stock `zstd`),
     defaulting to **1 MiB** uncompressed frames (16× finer random-access
     granularity than 16 MiB with <0.05% size overhead) and bounded RAM usage.
   - Uses native zero-subprocess zstd compression in priority order: Python 3.14+
     `compression.zstd` -> `python3-zstandard` -> **`libzstd.so.1` via `ctypes`**
     (thread-local `ZSTD_CCtx` + `ZSTD_compress2` with `ZSTD_c_checksumFlag=1`,
     GIL-free; active out of the box on Ubuntu 24.04 Noble's Python 3.12 +
     `libzstd1`) -> `zstd` CLI fallback.
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
   - Fixes an upstream 3-process pipe deadlock by closing parent copies of
     `p_partclone.stdout` and `p_comp.stdout` immediately after spawning downstream
     consumers, captures compression and `split` stderr via `TemporaryFile`,
     checks all pipeline exit codes, and removes partial `.rzidx`/split artifacts
     using `glob.escape`.
   - Adds cancellable metadata subprocess execution (`_run_cancellable`, session
     group SIGTERM + `/var/lib/os-prober/mount` / detached `grub-mount` cleanup)
     and pre-`Popen` / post-wait cancellation checks so both metadata-stage and
     partition-stage cancellations terminate cleanly with zero orphan processes.
3. **Added multi-disk backup selection (`rescuezilla.glade`, `drive_query.py`, `handler.py`, `backup_manager.py`, `cli/args.py`, `rescuezilla.py`)**:
   - Adds a checkbox column (`Backup`) to the Backup Step 1 drive list (without affecting Restore or Clone drive selection tables), with fallback support for single-row highlight selection and a double-click guard on the checkbox column.
   - Displays all selected drives' partitions in Step 2 (prefixed with `[<drive>]` when multiple drives are selected) and excludes all selected source drives from the Step 3 destination list.
   - Preserves the exact base folder name when 1 drive is selected; when 2+ drives are selected, sequentially backs up each drive into its own standalone, upstream-compatible folder named `<base_name>-<drive>` (e.g., `2026-11-27-2015-img-rescuezilla-nvme0n1`, `...-nvme1n1`), generating independent `clonezilla-img`, `Info-img-id.txt` (omitted on failed/cancelled drives), and `Info-seekidx-*.rzidx` per drive, marking any drive with partition failures as failed, keeping `/mnt/backup` mounted across the batch, and scheduling any post-task shutdown/reboot only once after all drives succeed.
   - Extends `rescuezillapy backup --source` (`action="extend"`, `nargs="+"`) to accept multiple drives (`--source /dev/sda --source /dev/sdb --destination ...` or `--source /dev/sda /dev/sdb --destination ...`), normalizing and deduplicating sources and validating `--partitions` before creating directories.
4. **Added unit & pipeline tests** (`test/test_seekable_compressor.py`, `test/test_multi_drive_backup.py`):
   - Verifies gzip & zstd multi-frame compression, multi-threaded `libzstd.so.1` backend & RFC 8878 XXH64 checksum bit, stock `gzip -d` / `zstd -d` compatibility, O(1) random frame seeking, CRC32 validation, the 3-process `partclone -> seekable_compressor -> split` pipeline, Clonezilla glob non-collision, multi-disk naming/deduplication/partition validation, batch execution/partial failure/cancellation, immediate metadata cancellation, `glob.escape`, and Glade XML schema (with class-level fail-closed host shutdown/umount safety guards).

## Current Verification Status

- **Verified**:
  - Python unit tests inside Ubuntu 24.04 Noble (`make test`: 47 tests passing) and `ruff format --check` / `ruff check --select F` (0 errors).
  - Full Ubuntu 24.04 (`noble`) Debian package + bootable Live ISO builds (both test and clean no-SSH release ISOs; release ISO `filesystem.manifest` verified to contain zero `openssh-server`).
  - Live boot of the unmodified built ISO inside VMware Workstation (`26.0.1`), verifying:
    - Live GTK3 + Glade UI against X11 (`backup_drive_toggled`, non-double-toggle guard on checkbox column, row activation toggle on non-checkbox columns, refresh preservation, Step 2 multi-disk partition prefixes, Back/Next navigation, Step 3 exclusion of all source drives, Step 5 folder naming, Step 7 Pango markup).
    - Live ISO Python 3.12.3 native `libzstd.so.1` backend selection (`455–507 MiB/s` measured on 64 MiB inside a 2-vCPU VM) with RFC 8878 XXH64 checksum verified on every frame.
    - Real live cancellation during `os-prober` (`0.77s`, zero stale `/var/lib/os-prober/mount`, zero orphan `grub-mount`) and during `partclone` + `seekable_compressor` + `split` (zero orphan processes, no `Info-img-id.txt`).
    - Real multi-disk batch with an injected compressor failure on Drive 1 and real `zstd` backup on Drive 2 (zero pipe deadlock, compressor stderr captured in summary, Drive 1 counted as failed without `Info-img-id.txt` and partials removed, Drive 2 succeeded).
    - Real 2-disk `rescuezillapy backup --source <d1> --source <d2> --compression-format zstd --compression-level 3` (`38.63s` across 6 partitions / 1,462 1 MiB frames, all `.zst.*` splits passing `zstd -t` and carrying XXH64 checksums), followed by destructive wipe of both disks, live `rescuezillapy restore` of both disks, clean Debian 12 reboot with automatic `fstab` mounts, and 100% SHA-256 verification of all 12 sentinel files and 3 symlinks across all 5 filesystems.
    - End-to-end O(1) instant loading (`0–4 ms`) and 100% SHA-256 recovery of all live multi-disk `gzip` and `zstd` backup images in `rescuezilla-browser` (40/40 unit tests passing).
- **Not yet verified**: Physical bare-metal USB boot (including UEFI Secure Boot on physical firmware), stock Clonezilla `ocs-sr` restore (only `rescuezillapy restore` and stock `gzip -d`/`zstd -d`/`zstd -t` were tested), and manual mouse-click execution of an end-to-end GTK backup run.
