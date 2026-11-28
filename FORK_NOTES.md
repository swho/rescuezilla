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
     defaulting to **1 MiB** uncompressed frames for `gzip` and **4 MiB** for
     `zstd` with RAM-aware bounded concurrency.
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
5. **v1.1 performance & UX (2026-11-28)**:
   - **Multi-core**: worker count = all CPUs in `sched_getaffinity`, capped by `MemAvailable` accounting for both in-flight frame buffers (`8 × frame_size`) and per-worker compressor context state (measured Ubuntu 24.04 Noble `libzstd1 1.5.5` RSS across levels 1–19, removing the old hard cap of 8 while preventing high-level zstd OOM on low-RAM Live USB systems); in-flight queue `min(2×workers, workers+16)` bounded to `max(2, ≤ ½ MemAvailable)`.
   - **gzip backend**: native `libdeflate.so.0` via `ctypes` (thread-local RAII compressor, GIL-free, levels clamped to 0–12; explicitly declared in `debian/control` `Depends:`), with clean fallback to Python `zlib` when it is missing or fails.
   - **Frame size**: 4 MiB default for `zstd`, 1 MiB for `gzip` (single source of truth in `default_frame_size_for_format()`). The `.rzidx` format is unchanged and records the frame size in its header.
   - **GUI default**: `zstd` level 3 is now index 0 / default in the compression combobox (`cli/args.py` CLI default unchanged). **Compatibility note**: upstream Rescuezilla's built-in legacy Image Explorer only opens `gzip`, `xz`, and `uncompressed` images; use `rescuezilla-browser` for O(1) instant browsing of `zstd` backups, or select `gzip` in Step 6 if legacy built-in Image Explorer or pre-2020 Clonezilla compatibility is required.
   - **Telemetry**: `seekable_compressor.py --print-stats` writes one `[seekable_compressor] fmt=… backend=… workers=… ratio=…% in_MBps=…` line; `backup_manager.py` logs it into `clonezilla-img` only when the full partition pipeline succeeds, and strips benign `[seekable_compressor]` lines when reporting a `partclone`/`split` failure (`comp_rc == 0`) while still reporting full stderr on `comp_rc != 0`.
   - **os-prober cancel (L1)**: final `pkill -KILL grub-mount` now runs before the final `umount -l`; the final umount also checks `/proc/self/mounts`, because `os.path.ismount()` returns False on a dead FUSE mount (`ENOTCONN`).
   - The 3-process spawn moved into `spawn_backup_pipeline()` so the early-close/no-deadlock behaviour has its own unit tests.

## Current Verification Status

- **Verified**:
  - Python unit tests inside Ubuntu 24.04 Noble (`unittest`: **65 tests passing**, incl. `libdeflate` levels 0–12 / empty / multi-threaded / `zlib` fallback, high-level `zstd` CCtx RAM worker cap, `default_max_in_flight` RAM cap, format-specific frame sizes, `--print-stats` telemetry & `strip_seekable_compressor_stats`, GUI `zstd` default, `L1` `os-prober` cleanup, and `spawn_backup_pipeline` deadlock prevention) and `ruff format --check` / `ruff check --select F` (0 errors).
  - Real Ubuntu 24.04 Noble 64 MiB ELF/lib corpus throughput benchmark (6-core Docker): `libdeflate` `gzip -6` (auto workers, 1 MiB) measured **727.3 MB/s** (`40.91%`) vs old `zlib1g` 8-thread `gzip -6` **189.5 MB/s** (`41.24%`, **3.84× speedup**); `libzstd` `zstd -3` (auto workers, 4 MiB) measured **1,745.6 MB/s** (`39.66%`, **9.21× speedup**). Measured Noble `libzstd1 1.5.5` per-worker `delta_maxrss` across levels 1–19 (`4.3–7.3 MiB` at lv 1–6, `44.5 MiB` at lv 12, `52.9–68.6 MiB` at lv 15–19).
  - Verified `rescuezilla-browser` `SeekableFrameStream` cross-frame random-access reads on both 1 MiB `libdeflate` `gzip` and 4 MiB `libzstd` `zstd` `.rzidx` streams.
  - Full Ubuntu 24.04 (`noble`) Debian package + bootable Live ISO builds (both test and clean no-SSH release ISOs; release ISO `filesystem.manifest` verified to contain zero `openssh-server`).
  - Live boot of the v1.1 built ISO inside VMware Workstation (`26.0.1`, 2 vCPU, 2 GiB RAM) verifying:
    - Live GTK3 + Glade UI against X11: default compression combobox index 0 is `zstd` (`zstandard (default)`, level `3`); switching to index 1 selects `gzip` (level `6`).
    - Live ISO Python 3.12.3 activates both `libdeflate0 1.19` (`gzip=libdeflate`) and `libzstd1 1.5.5` (`zstd=libzstd`) with all vCPUs (`workers=2`); 64 MiB live ISO binary corpus measured `216–244 MB/s` (`39.25%`) for `libdeflate gzip -6` (vs `73 MB/s`, `39.84%` for old `zlib1g`) and `715.9 MB/s` (`33.70%`) for `libzstd zstd -3` 4 MiB.
    - Real 2-disk `zstd -3` (4 MiB frame, `38.73s`) and Disk 2 `gzip -6` (1 MiB `libdeflate`, `16.42s`) backups logging `[seekable_compressor]` telemetry into `clonezilla-img`, passing `Info-img-id.txt` SHA-512, `.rzidx` CRC32, XXH64 checksum bits, and stock `zstd -t` / `gzip -t`.
    - All 6 browseable partitions across all 3 live v1.1 images (4 MiB `zstd` and 1 MiB `libdeflate` `gzip`) opened in `0.001s–0.003s` in `rescuezilla-browser` with 100% SHA-256 match.
  - Prior v1.0 live VMware E2E (multi-disk GTK UI, live `os-prober` & `partclone` cancel, partial failure zero-deadlock, destructive wipe + `rescuezillapy restore` + 12/12 SHA-256 & 3/3 symlinks) and real bare-metal 2-disk 2.271 TiB -> 1.153 TiB backup audit (0 bad frames across 496 sampled frames; all 8 browseable partitions opened in `0.000s–0.543s` in `rescuezilla-browser`).
- **Not yet verified**: UEFI Secure Boot on physical firmware, stock Clonezilla `ocs-sr` restore (only `rescuezillapy restore` and stock `gzip -d`/`zstd -d`/`zstd -t` were tested), and manual mouse-click execution of an end-to-end GTK backup run.
