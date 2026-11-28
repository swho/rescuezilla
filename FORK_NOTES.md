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

7. **v2.0 SeekZilla branding & Hybrid Block-Level Differential Backup (2026-11-28, tag `2.7.0-seekzilla-v2.0`)**:
   - **Rebranding (`SeekZilla` / 瞬尋救援龍)**:
     - Full Midnight Navy (`#0F172A`) + Electric Cyan (`#38BDF8`) + Emerald (`#34D399`) vector identity across GRUB boot menu (`boot_menu_background.png`, `theme.txt`, `grub.cfg`, `EFI/ubuntu/grub.cfg`, `zh_Hant.po`, `zh.po`), Plymouth boot splash (`rescuezilla_logo.png`, 16-color indexed `rescuezilla_logo16.png`, `rescuezilla-logo.script`), Live Desktop wallpaper (`rescuezilla_blue.png`), GTK3 app banner (`banner.png`, `#banner` CSS `#0F172A`), icons (`icon.png`, `rescuezilla.svg`), `.desktop`, Glade (`SeekZilla (Based on Rescuezilla)`, `Welcome to SeekZilla`), Openbox auto-maximize (`title="SeekZilla*"`), ISO Volume ID (`SeekZilla`), and `autorun.inf`. Full GPLv3 attribution (`Based on Rescuezilla (GPLv3)`) preserved throughout.
   - **1 MiB Block-Hash Sidecar (`block_hash.py`, `.rzbhash`)**:
     - Full Base backups remain **100% stock Rescuezilla / Clonezilla compatible** while atomically emitting `Info-bhash-<part>.rzbhash` (`RZBHASH1` 68-byte header + per-chunk SHA-256 digests + 32-byte file SHA-256 trailer; ~32 MiB per 1 TiB) in parallel with `seekable_compressor.py` (`--bhash-output`) in a single streaming pass.
     - Includes offline retroactive sidecar generator (`block_hash.py build-from-image`) so existing full backups (e.g., v1.0 / v1.1 images) can be indexed read-only and used as a differential Base without re-backing up.
   - **Depth-1 Hybrid Differential Backup (`diff_format.py`, `diff_writer.py`, `diff_synthesizer.py`)**:
     - Compares incoming Partclone v0002 chunks against the Base `.rzbhash` in a single pass and writes ONLY changed 1 MiB LBA chunks (`<part>.<fs>-rzdiff-img.<zst|gz>.aa..`, `<part>.<fs>-rzdiff.meta` (`RZDMETA1`, zstd-compressed with 512 MiB bounded decode & write guard), `Info-seekidx-<part>.rzdiff.rzidx`, `Info-rzdiff-manifest.json`, `README-RZDIFF.txt`).
     - Base folders are strictly read-only and never modified. Incompatible geometry (partition resize/reformat, `partclone.dd`, missing `.rzbhash`, or worst-case metadata > 512 MiB) triggers clean exit `42` (`FallbackFullError`) BEFORE writing stdout, automatically falling back to a full partition backup.
     - Direct restore & verify (`restore_manager.py`, `verify_manager.py`, `clonezilla_image.py`) stream `diff_synthesizer.py stream` directly into `partclone.<fs>` / `partclone.chkimg`, verifying Base `.rzbhash`, Partclone header SHA-256, and every Base chunk's SHA-256 fail-closed (exit `3` mismatch / `4` base corruption / `5` diff corruption).
     - `diff_synthesizer.py merge` flattens Base + Diff atomically into a standalone stock-compatible full image (skipping symlinks/FIFOs via `lstat`+`O_NOFOLLOW`, fsyncing files & parent dir).
     - GTK3 Backup Step 7 (`handler.py`) discovers compatible Base images (including multi-drive sibling sets, skipping diff/partial-diff folders) and offers a clean Differential Backup toggle with `-diff-seekzilla` default naming.

8. **v2.1 4K / HiDPI Live USB Display Readability (2026-11-28, tag `2.7.0-seekzilla-v2.1`)**:
   - **Live Desktop Auto-Resolution (`src/livecd/chroot/usr/local/bin/seekzilla-set-display-mode`, `.xprofile`, `/etc/X11/Xsession.d/35seekzilla-hidpi`)**:
     - Runs synchronously at the top of `~/.xprofile` (`>/dev/null 2>&1 || true`) before desktop/panel/SeekZilla launch, leaving `<= 1920×1200` (1080p, 720p, VMware 1280×800) and ultrawide 1080p-height (`2560×1080`, `3840×1080`) displays untouched.
     - Automatically caps 4K (`3840×2160`), 4K+ (`3840×2400`), 5K, and >1080p displays with a 3-tier fail-safe strategy:
       - **Tier 1 (Hardware mode)**: aspect-aware selection (`1920×1080` for 16:9, `1920×1200` for 16:10, `2560×1080` for 21:9, `3840×1080` for 32:9, or `1280×720` when overridden via `seekzilla_display=720p`), preserving mirrored/cloned outputs and non-overlapping multi-monitor `--pos`.
       - **Tier 2 (Single-mode 4K eDP panel fallback)**: `xrandr --scale 0.5x0.5` (2× integer magnification to `1920×1080` logical screen).
       - **Tier 3 (Fixed framebuffer / `efifb` / `vesa` fallback)**: `xrdb -merge` `Xft.dpi: 192` (`>= 3000px` wide) or `144` (`>= 2400px`) plus a per-user `0600` state file re-applied by `/etc/X11/Xsession.d/35seekzilla-hidpi` after `lightdm-session` merges `~/.Xresources` (immune to squashfs root-owned `~/.Xresources` races).
     - Override via kernel cmdline `seekzilla_display=native|720p|1080p` or env `SEEKZILLA_DISPLAY_MODE`; always exits `0` in runtime mode so login can never break.
   - **GRUB 4K Readability (`grub.cfg`, `boot_menu_background.png`)**:
     - Updated `set gfxmode=1280x720,1024x768,1920x1080,800x600,auto` and re-rendered `boot_menu_background.png` at native `1280×720` (16:9) with a centered 4:3 crop-safe hero zone.
   - **Companion `SeekZilla Browser`**: added GUI retroactive `Info-bhash-<part>.rzbhash` builder (`bhash_builder.py`, `BuildBhashWorker`, `BhashBuildDialog`, bit-for-bit verified against `block_hash.py` and real VM backups; **217 / 217 tests** passing).

## Current Verification Status

- **Verified (`v2.1`, tag `2.7.0-seekzilla-v2.1`)**:
  - **Automated unit & integration suites**:
    - Ubuntu 24.04 Noble Docker (`Python 3.12`): **194 / 194 tests passing** (incl. 57 `test_display_mode.py` tests covering 4K/4K+/21:9/32:9/single-mode eDP/Tier 3 `Xsession.d` drop-in/cloned/multi-monitor/error isolation, plus all diff & upstream tests), `ruff check --select F` 0 errors, `sh -n` 0 errors, `msgfmt -c` 0 errors, `xmllint` & `desktop-file-validate` clean.
    - Companion `SeekZilla Browser` (`rescuezilla-browser`): **217 / 217 tests passing** (incl. retroactive `.rzbhash` builder bit-for-bit cross-repo tests against `block_hash.py` & real VM `SeekZilla` Base backups, `ForkInteropTest`, and 16-thread concurrent `LayeredPartitionDevice` stress test), `ruff check --select F` 0 errors.
  - **Real VMware Workstation Live ISO v2.0 E2E (`Ubuntu 24.04 Noble`, real `partclone.vfat` / `partclone.btrfs` / `partclone.ntfs` / `partclone.restore` / `partclone.chkimg` / GTK3 / Openbox)**:
    - Live X11 boot verified auto-maximized `SeekZilla (Based on Rescuezilla)` (`1280×800`), Midnight Navy banner, icons, and GTK3 differential backup controls; GRUB boot screen verified via live QEMU framebuffer capture.
    - Disk 2 (`vfat` + `btrfs`) Full Base -> live V2 add/modify/delete mutations -> Differential backup (`494,511 B` = **`8.8%`** of `5,636,384 B` Base; Base folder verified 100% unmodified file-by-file mtime + SHA-256).
    - Real `VerifyManager` (`diff_synthesizer.py stream | partclone.chkimg`) PASSED on both partitions; negative 1-bit Base bit-rot test failed closed with exit `4` (`BaseCorruptionError`).
    - Real `diff_synthesizer.py stream | partclone.restore` onto fresh loopback devices passed `btrfs check --readonly` (`no error found`) and matched **100% file-by-file SHA-256** against V2 state (`sdb1` VFAT: 6 files; `sdb2` Btrfs: 7 files).
    - Real `diff_synthesizer.py merge` produced a standalone stock-compatible full image passing `cat | zstd -d | partclone.chkimg` on both partitions.
    - Real `partclone.ntfs` + `ntfs-3g` loopback E2E (Full Base -> V2 mutation -> Diff -> `partclone.chkimg` -> `partclone.restore`) matched **100% file-by-file SHA-256** against V2 state.
    - Host-side `SeekZilla Browser` opened real VM-produced Full Base (`0.002s–0.004s`), Differential (`0.002s–0.003s`, `[差異]` badge), and Merged (`0.001s–0.003s`) backups and recovered all files with **100% SHA-256 match** (V1 for Base, V2 for Diff & Merged).
  - **Prior v1.1 / v1.0 verified benchmarks & audits**:
    - Ubuntu 24.04 Noble (6-core Docker, 64 MiB ELF corpus): `libdeflate gzip -6` **727.3 MB/s** (**3.84×** vs `zlib1g`), `libzstd zstd -3` 4 MiB **1,745.6 MB/s** (**9.21×**).
    - Bare-metal 2-disk 2.271 TiB -> 1.153 TiB multi-disk audit: 0 bad frames across 496 sampled frames; all 8 browseable partitions opened in `0.000s–0.543s`; retroactive `block_hash.py build-from-image` tested read-only on real bare-metal NTFS split image (`0.052s`).
    - Clean no-SSH Release ISO verified: `filesystem.manifest` contains `0` `openssh-server`, ISO9660 Volume ID `SeekZilla`, `libdeflate0` & `libzstd1` preinstalled.
- **Not yet verified**: UEFI Secure Boot on physical firmware, stock Clonezilla `ocs-sr` restore (only `rescuezillapy` / `partclone.restore` / `partclone.chkimg` / stock `gzip -d` / `zstd -d` were tested), and manual mouse-click execution of an end-to-end GTK differential backup run.
