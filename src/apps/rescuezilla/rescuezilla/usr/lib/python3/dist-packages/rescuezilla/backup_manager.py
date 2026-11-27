# ----------------------------------------------------------------------
#   Copyright (C) 2012 RedoBackup.org
#   Copyright (C) 2003-2025 Steven Shiau <steven _at_ clonezilla org>
#   Copyright (C) 2019-2025 Rescuezilla.com <rescuezilla@gmail.com>
#   Modified 2026-11-25 (Community Fork): Added seekable multi-frame
#   compression and Info-seekidx-<part>.rzidx generation during backup
#   (GPL-3.0-or-later).
#   Modified 2026-11-27 (Community Fork): Added multi-disk sequential batch
#   backup with per-disk destination folder naming (GPL-3.0-or-later).
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
import collections
import glob
import os
import pprint
import shutil
import signal
import subprocess
import tempfile
import threading
import traceback
from datetime import datetime
from time import sleep
import re
from typing import Any, Dict, Tuple

from ui_manager import UiManager
from logger import Logger
from parser.combined_drive_state import CombinedDriveState
from parser.lvm import Lvm
from parser.partclone import Partclone
from parser.parted import Parted
from parser.proc_mdstat import ProcMdstat
from parser.sfdisk import Sfdisk
from parser.swappt import Swappt
from utility import Utility, _


def normalize_drive_key(drive_key: str) -> str:
    """Normalize a Linux block device specifier to canonical '/dev/...' form."""
    raw = str(drive_key).strip()
    if not raw:
        raise ValueError("Drive key must not be empty")
    if not raw.startswith("/"):
        raw = "/dev/" + raw
    return os.path.normpath(raw)


def sanitize_drive_suffix(drive_key: str) -> str:
    """Return a clean filename-safe short device name (e.g. '/dev/nvme0n1' -> 'nvme0n1')."""
    short = re.sub(r"^/dev/", "", str(drive_key).strip())
    short = re.sub(r"[^A-Za-z0-9._-]+", "_", short).strip("._-")
    return short or "disk"


def compute_backup_dest_dirs(
    base_dest_dir: str, selected_drive_keys
) -> collections.OrderedDict:
    """Compute per-drive backup destination folder paths.

    - Single selected drive: keeps exact base_dest_dir (100% identical to upstream).
    - Multiple selected drives: appends '-<short_drive_name>' to base_dest_dir for each drive,
      producing standalone upstream-compatible Rescuezilla/Clonezilla backup folders.
    - Duplicate drives (including unnormalized aliases like '/dev/sda' and 'sda') raise ValueError.
    """
    keys = (
        [selected_drive_keys]
        if isinstance(selected_drive_keys, str)
        else list(selected_drive_keys)
    )
    seen_norm = set()
    for dk in keys:
        norm = normalize_drive_key(dk)
        if norm in seen_norm:
            raise ValueError(f"Duplicate source drive specified: {dk} ({norm})")
        seen_norm.add(norm)

    result = collections.OrderedDict()
    if len(keys) <= 1:
        for dk in keys:
            result[dk] = base_dest_dir
        return result

    clean_base = (
        base_dest_dir.rstrip(os.sep) if base_dest_dir != os.sep else base_dest_dir
    )
    used = set()
    for dk in keys:
        suffix = sanitize_drive_suffix(dk)
        candidate = f"{clean_base}-{suffix}"
        if candidate in used:
            n = 2
            while f"{candidate}-{n}" in used:
                n += 1
            candidate = f"{candidate}-{n}"
        used.add(candidate)
        result[dk] = candidate
    return result


def prepare_cli_backup_jobs(
    drive_state: Dict[str, Any], raw_sources, raw_partitions, destination: str
):
    """Validate and normalize CLI backup sources/partitions into backup_jobs tuples.

    Returns a list of (drive_key, partitions_to_backup, dest_dir) tuples.
    Raises ValueError before any directory is created if sources are duplicated/unknown
    or if explicit --partitions cannot be cleanly mapped to the selected drives.
    """
    src_list = [raw_sources] if isinstance(raw_sources, str) else list(raw_sources)
    if not src_list:
        raise ValueError("At least one source drive must be specified.")
    norm_sources = [normalize_drive_key(dk) for dk in src_list]
    # Validates uniqueness across normalized names and builds per-drive destination paths.
    dest_dirs = compute_backup_dest_dirs(destination, norm_sources)

    for dk in norm_sources:
        if dk not in drive_state:
            raise ValueError(f"Source drive not found in system drive list: {dk}")
        if "partitions" not in drive_state[dk]:
            raise ValueError(f"Source drive {dk} has no partition table or filesystem.")

    is_all = raw_partitions == "all" or (
        isinstance(raw_partitions, list)
        and (len(raw_partitions) == 0 or "all" in raw_partitions)
    )
    if is_all:
        return [(dk, ["all"], dest_dirs[dk]) for dk in norm_sources]

    req_parts = (
        [raw_partitions] if isinstance(raw_partitions, str) else list(raw_partitions)
    )
    norm_req_parts = [normalize_drive_key(p) for p in req_parts]
    matched_req = set()
    jobs = []
    for dk in norm_sources:
        drive_parts = drive_state[dk]["partitions"]
        selected_parts = collections.OrderedDict()
        for pk in norm_req_parts:
            if pk in drive_parts:
                part_entry = dict(drive_parts[pk])
                part_entry["description"] = (
                    CombinedDriveState.flatten_partition_description(
                        drive_state, dk, pk
                    )
                )
                selected_parts[pk] = part_entry
                matched_req.add(pk)
        if not selected_parts:
            raise ValueError(
                f"No requested partitions {norm_req_parts} belong to selected source drive {dk}. "
                "Use --partitions all for multi-drive backups or specify partitions for every selected drive."
            )
        jobs.append((dk, selected_parts, dest_dirs[dk]))

    unmatched = [p for p in norm_req_parts if p not in matched_req]
    if unmatched:
        raise ValueError(
            f"Requested partition(s) not found on selected source drive(s): {', '.join(unmatched)}"
        )
    return jobs


class _BatchDriveUiProxy:
    """Per-drive UiManager proxy used during multi-disk sequential backup.

    Scales progress across all drives, prefixes status messages with '[i/N: drive]',
    and captures per-drive completion/errors synchronously without advancing wizard
    tabs, unmounting /mnt/backup, or scheduling shutdown mid-batch.
    """

    def __init__(
        self, real_ui: UiManager, drive_index: int, total_drives: int, drive_key: str
    ):
        self._real_ui = real_ui
        self._drive_index = drive_index
        self._total_drives = max(1, total_drives)
        self._drive_key = drive_key
        self._short_drive = sanitize_drive_suffix(drive_key)
        self._prefix = f"[{drive_index + 1}/{self._total_drives} {self._short_drive}] "
        self.logger = None
        self.succeeded = None
        self.message = ""
        self.error_messages = []

    def set_logger(self, logger):
        self.logger = logger
        self._real_ui.set_logger(logger)

    def update_progress_bar(self, fraction: float):
        clamped = max(0.0, min(1.0, float(fraction)))
        overall = (self._drive_index + clamped) / self._total_drives
        self._real_ui.update_progress_bar(fraction=overall)

    def update_progress_status(self, message: str):
        prefixed = self._prefix + message if message else ""
        self._real_ui.update_progress_status(message=prefixed)

    def get_post_task_action(self) -> str:
        return "DO_NOTHING"

    def update_main_statusbar(self, message: str):
        prefixed = self._prefix + message if message else ""
        self._real_ui.update_main_statusbar(message=prefixed)

    def display_status(self, msg1: str, msg2: str):
        self._real_ui.display_status(msg1=self._prefix + msg1, msg2=msg2)

    def remove_all_main_statusbar(self, context_id: str):
        # Keep statusbar active until entire batch completes.
        return

    def display_error_message(self, summary_message: str, heading: str = ""):
        text = f"{heading}\n{summary_message}".strip() if heading else summary_message
        self.error_messages.append(text)
        if self.logger is not None:
            self.logger.write(text)
        else:
            print(text)

    def completed_operation(self, callable_fn, succeeded: bool, message: str):
        # Record per-drive completion without triggering GUI tab transition or umount.
        self.succeeded = succeeded
        self.message = message or ""
        return succeeded, self.message

    def _on_operation_completed_callback(self, is_success):
        pass

    def escape_text(self, input: str) -> str:
        return self._real_ui.escape_text(input=input)

    def display_summary_text(self, text_to_display):
        # Deferred to final consolidated multi-drive summary page.
        return


class _BackupCancelledError(Exception):
    """Internal control-flow signal raised when user cancels during a metadata command."""


# Signals should automatically propagate to processes called with subprocess.run().
# TODO: This class uses 'subprocess.call', which provides exit code but not stdout/stderr. It would be
# TODO: VERY useful to display stdout/stderr when the exit code is non-zero, like the RestoreManager does.
class BackupManager:
    def __init__(self, ui_manager: UiManager, human_readable_version: str):
        self.ui_manager = ui_manager
        self.backup_in_progress_lock = threading.Lock()
        self.backup_in_progress = False
        self.human_readable_version = human_readable_version
        # proc dictionary
        self._proc_lock = threading.Lock()
        self.proc = collections.OrderedDict()
        self._pgroup_pids = set()
        self.logger = None

        self.requested_stop = False
        self._is_batch_mode = False
        self._single_backup_completed = False
        self.failed_partition_count = 0
        self.succeeded_partition_count = 0

    def _log(self, message: str):
        logger = self.logger
        if logger is not None:
            try:
                logger.write(message)
            except Exception:
                print(message)

    def _run_cancellable(
        self,
        short_description,
        cmd_list,
        use_c_locale,
        output_filepath=None,
        logger=None,
    ):
        """Drop-in cancellable replacement for Utility.run() during backup metadata collection.

        Registers the active subprocess in self.proc['_active_cmd'] (with its own session/pgid)
        so cancel_backup() terminates slow commands (smartctl, os-prober, lshw, etc.) immediately.
        """
        if self.requested_stop:
            raise _BackupCancelledError()
        if use_c_locale:
            env = Utility.get_env_C_locale()
        else:
            env = os.environ.copy()
        flat_command_string = Utility.print_cli_friendly(short_description, [cmd_list])
        proc = subprocess.Popen(
            cmd_list,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        registered = False
        already_cancelled = False
        try:
            with self._proc_lock:
                if self.requested_stop:
                    already_cancelled = True
                else:
                    self.proc["_active_cmd"] = proc
                    self._pgroup_pids.add(proc.pid)
                    registered = True

            if already_cancelled:
                # Terminate and reap OUTSIDE self._proc_lock so GUI cancel_backup() never blocks.
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except Exception:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                try:
                    proc.communicate(timeout=3.0)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    try:
                        proc.communicate(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        pass
                raise _BackupCancelledError()

            stdout_data, stderr_data = proc.communicate()
        finally:
            if registered:
                with self._proc_lock:
                    if self.proc.get("_active_cmd") is proc:
                        del self.proc["_active_cmd"]
                    self._pgroup_pids.discard(proc.pid)
            if (
                self.requested_stop
                and cmd_list
                and cmd_list[0] in ("os-prober", "linux-boot-prober")
            ):
                # M-A: os-prober spawns FUSE `grub-mount <dev> /var/lib/os-prober/mount` which
                # daemonizes out of os-prober's process group. Unmount synchronously first (so
                # FUSE gets DESTROY and exits cleanly), then terminate any detached grub-mount
                # targeting /var/lib/os-prober/mount so zero stale mounts or FUSE daemons remain.
                os_prober_mnt = "/var/lib/os-prober/mount"
                for _attempt in range(4):
                    if os.path.exists(os_prober_mnt):
                        try:
                            res_u = subprocess.run(
                                ["umount", os_prober_mnt],
                                check=False,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=5.0,
                            )
                            if res_u.returncode != 0 and os.path.ismount(os_prober_mnt):
                                subprocess.run(
                                    ["umount", "-l", os_prober_mnt],
                                    check=False,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    timeout=5.0,
                                )
                        except Exception:
                            pass
                    try:
                        res_pk = subprocess.run(
                            [
                                "pkill",
                                "-TERM",
                                "-f",
                                "grub-mount.*/var/lib/os-prober/mount",
                            ],
                            check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=3.0,
                        )
                    except Exception:
                        break
                    if res_pk.returncode != 0 and not os.path.ismount(os_prober_mnt):
                        break
                    sleep(0.2)
                if os.path.exists(os_prober_mnt) and os.path.ismount(os_prober_mnt):
                    try:
                        subprocess.run(
                            ["umount", "-l", os_prober_mnt],
                            check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=5.0,
                        )
                    except Exception:
                        pass
                try:
                    subprocess.run(
                        [
                            "pkill",
                            "-KILL",
                            "-f",
                            "grub-mount.*/var/lib/os-prober/mount",
                        ],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=3.0,
                    )
                except Exception:
                    pass

        if self.requested_stop:
            raise _BackupCancelledError()

        completed = subprocess.CompletedProcess(
            cmd_list, proc.returncode, stdout_data, stderr_data
        )
        logging_output = (
            short_description
            + ": "
            + flat_command_string
            + " returned "
            + str(completed.returncode)
            + ": "
            + completed.stdout
            + " "
            + completed.stderr
            + "\n"
        )
        if logger is None:
            print(logging_output)
        else:
            logger.write(logging_output)

        if output_filepath is not None:
            with open(output_filepath, "a+") as filehandle:
                filehandle.write("%s" % completed.stdout)
                filehandle.flush()

        fail_description = (
            _("Failed to run command: ")
            + flat_command_string
            + "\n\n"
            + completed.stdout
            + "\n"
            + completed.stderr
            + "\n\n"
        )
        return completed, flat_command_string, fail_description

    def _terminate_and_wait_partition_procs(self, partition_key: str):
        """Terminate and reap partclone/compression/split procs for one partition."""
        proc_names = [
            "partclone_backup_" + partition_key,
            "compression_" + partition_key,
            "split_" + partition_key,
        ]
        procs = []
        with self._proc_lock:
            for name in proc_names:
                p = self.proc.get(name)
                if p is not None:
                    procs.append(p)
        for p in procs:
            try:
                if p.poll() is None:
                    p.terminate()
            except Exception:
                pass
        for p in procs:
            try:
                if p.poll() is None:
                    p.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                try:
                    p.kill()
                    p.wait(timeout=2.0)
                except Exception:
                    pass
            except Exception:
                pass
            for pipe in (p.stdout, p.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except Exception:
                        pass

    def _remove_partition_output_files(self, filepath: str, seekidx_path=None):
        """Delete partial or failed partition image splits and .rzidx using glob.escape."""
        failed_paths = glob.glob(glob.escape(filepath) + "*")
        if seekidx_path is not None:
            failed_paths.extend(glob.glob(glob.escape(seekidx_path) + "*"))
        for file_path in failed_paths:
            try:
                os.remove(file_path)
            except OSError:
                self._log("Unable to remove: " + file_path)
                with self.summary_message_lock:
                    self.summary_message += "Unable to remove: " + file_path + "\n"

    def is_backup_in_progress(self):
        with self.backup_in_progress_lock:
            return self.backup_in_progress

    def start_backup(
        self,
        selected_drive_key,
        partitions_to_backup,
        drive_state,
        dest_dir,
        backup_notes,
        compression_dict,
        is_rescue,
        completed_backup_callback,
        metadata_only_image_to_annotate=None,
        on_separate_thread=True,
    ):
        self.backup_timestart = datetime.now()
        self.completed_backup_callback = completed_backup_callback
        self.selected_drive_key = selected_drive_key
        self.dest_dir = dest_dir
        self.backup_notes = backup_notes
        self.compression_dict = compression_dict
        self.is_rescue = is_rescue
        self.partitions_to_backup = partitions_to_backup
        # Entire machine's drive state
        # TODO: This is a crutch that ideally will be removed. It's very bad from an abstraction perspective, and
        # TODO: clear abstractions is important for ensuring correctness of the backup/restore operation
        self.drive_state = drive_state
        self.ui_manager.update_progress_bar(fraction=0)
        self.requested_stop = False
        with self.backup_in_progress_lock:
            self._is_batch_mode = False
            self._single_backup_completed = False
            self.backup_in_progress = True
        self.metadata_only_image_to_annotate = metadata_only_image_to_annotate
        self.is_cloning = False
        if self.metadata_only_image_to_annotate is not None:
            self.is_cloning = True
        else:
            self.is_cloning = False
        if on_separate_thread:
            thread = threading.Thread(target=self.do_backup_wrapper)
            thread.daemon = True
            thread.start()
        else:
            return self.do_backup()

    def start_multi_drive_backup(
        self,
        backup_jobs,
        drive_state,
        backup_notes,
        compression_dict,
        is_rescue,
        completed_backup_callback,
        on_separate_thread=True,
    ):
        """Sequentially back up multiple drives, each into its own standalone backup folder.

        Args:
            backup_jobs: List of (drive_key, partitions_to_backup, dest_dir) tuples.
        """
        jobs = list(backup_jobs)
        if len(jobs) == 0:
            raise ValueError("backup_jobs must not be empty")
        if len(jobs) == 1:
            dk, parts, dest = jobs[0]
            return self.start_backup(
                selected_drive_key=dk,
                partitions_to_backup=parts,
                drive_state=drive_state,
                dest_dir=dest,
                backup_notes=backup_notes,
                compression_dict=compression_dict,
                is_rescue=is_rescue,
                completed_backup_callback=completed_backup_callback,
                metadata_only_image_to_annotate=None,
                on_separate_thread=on_separate_thread,
            )

        self.backup_timestart = datetime.now()
        self.completed_backup_callback = completed_backup_callback
        self.backup_jobs = jobs
        self.drive_state = drive_state
        self.backup_notes = backup_notes
        self.compression_dict = compression_dict
        self.is_rescue = is_rescue
        self.metadata_only_image_to_annotate = None
        self.is_cloning = False
        self.requested_stop = False
        self.summary_message_lock = threading.Lock()
        self.summary_message = ""
        self.ui_manager.update_progress_bar(fraction=0)
        with self.backup_in_progress_lock:
            self._is_batch_mode = True
            self.backup_in_progress = True

        if on_separate_thread:
            thread = threading.Thread(target=self._do_multi_drive_backup_wrapper)
            thread.daemon = True
            thread.start()
        else:
            return self._do_multi_drive_backup()

    def _do_multi_drive_backup_wrapper(self):
        try:
            self._do_multi_drive_backup()
        except Exception:
            tb = traceback.format_exc()
            traceback.print_exc()
            self.ui_manager.completed_operation(
                callable_fn=self._completed_multi_drive_backup,
                succeeded=False,
                message=_("Error creating backup: ") + tb,
            )
            return

    def _do_multi_drive_backup(self):
        real_ui = self.ui_manager
        total_drives = len(self.backup_jobs)
        batch_sections = []
        succeeded_count = 0
        failed_count = 0
        had_any_non_fatal = False
        cancelled = False

        for idx, (drive_key, partitions_to_backup, dest_dir) in enumerate(
            self.backup_jobs
        ):
            if self.requested_stop:
                cancelled = True
                batch_sections.append(
                    f"[{idx + 1}/{total_drives}] {drive_key} -> {dest_dir}\n"
                    f"  " + _("Operation cancelled by user.")
                )
                continue

            drive_start = datetime.now()
            proxy = _BatchDriveUiProxy(
                real_ui=real_ui,
                drive_index=idx,
                total_drives=total_drives,
                drive_key=drive_key,
            )
            self.ui_manager = proxy
            self.selected_drive_key = drive_key
            self.partitions_to_backup = partitions_to_backup
            self.dest_dir = dest_dir
            self.logger = None
            self.failed_partition_count = 0
            self.succeeded_partition_count = 0
            drive_succeeded = False
            drive_err_msg = ""

            try:
                ret = self.do_backup()
                if proxy.succeeded is not None:
                    drive_succeeded = bool(proxy.succeeded)
                    drive_err_msg = proxy.message
                elif isinstance(ret, tuple) and len(ret) >= 2:
                    drive_succeeded = bool(ret[0])
                    drive_err_msg = str(ret[1] or "")
                else:
                    drive_succeeded = True
                # Never treat a drive with failed partitions as succeeded in batch summary.
                if self.failed_partition_count > 0:
                    drive_succeeded = False
            except Exception:
                tb = traceback.format_exc()
                traceback.print_exc()
                drive_succeeded = False
                drive_err_msg = _("Error creating backup: ") + tb
            finally:
                try:
                    self._finalize_clonezilla_img_and_id(
                        dest_dir=dest_dir,
                        write_img_id=(drive_succeeded and not self.requested_stop),
                    )
                except Exception:
                    traceback.print_exc()
                self.ui_manager = real_ui
                real_ui.set_logger(None)

            drive_elapsed = Utility.get_human_readable_minutes_seconds(
                (datetime.now() - drive_start).total_seconds()
            )
            drive_summary = (self.summary_message or "").strip()
            if self.at_least_one_non_fatal_error:
                had_any_non_fatal = True

            if self.requested_stop:
                cancelled = True
                failed_count += 1
                status_line = _("Operation cancelled by user.")
            elif drive_succeeded:
                succeeded_count += 1
                status_line = (
                    _("Backup succeeded with some errors:")
                    if self.at_least_one_non_fatal_error
                    else _("Backup saved successfully.")
                )
            else:
                failed_count += 1
                status_line = _("Backup operation failed:")

            sec_lines = [
                f"=== [{idx + 1}/{total_drives}] {drive_key} -> {dest_dir} ({drive_elapsed} min) ===",
                status_line,
            ]
            if drive_summary:
                sec_lines.append(drive_summary)
            if drive_err_msg:
                sec_lines.append(drive_err_msg)
            if proxy.error_messages:
                sec_lines.extend(proxy.error_messages)
            batch_sections.append("\n".join(sec_lines))

        overall_succeeded = (
            (failed_count == 0)
            and (not cancelled)
            and (succeeded_count == total_drives)
        )
        self.at_least_one_non_fatal_error = had_any_non_fatal
        self.summary_message_lock = threading.Lock()
        header = _("Multi-drive backup: {succeeded}/{total} drive(s) succeeded").format(
            succeeded=succeeded_count, total=total_drives
        )
        if failed_count > 0:
            header += ", " + _("{failed} failed").format(failed=failed_count)
        if cancelled:
            header += " (" + _("cancelled") + ")"
        self.summary_message = header + "\n\n" + "\n\n".join(batch_sections) + "\n"
        if overall_succeeded:
            real_ui.update_progress_bar(fraction=1.0)
        err_msg = _("Operation cancelled by user.") if cancelled else ""
        return real_ui.completed_operation(
            callable_fn=self._completed_multi_drive_backup,
            succeeded=overall_succeeded,
            message=err_msg,
        )

    def _completed_multi_drive_backup(self, succeeded, message):
        callback_succeeded = succeeded
        try:
            self.ui_manager.update_progress_status(message="")
            self.ui_manager.remove_all_main_statusbar(context_id="backup")
            backup_timeend = datetime.now()
            duration_minutes = Utility.get_human_readable_minutes_seconds(
                (backup_timeend - self.backup_timestart).total_seconds()
            )
            duration_message = _("Operation took {num_minutes} minutes.").format(
                num_minutes=duration_minutes
            )

            is_unmounted, umount_message = Utility.umount_warn_on_busy(
                "/mnt/backup", is_lazy_umount=True
            )
            if not is_unmounted:
                print(umount_message)

            with self.summary_message_lock:
                if succeeded:
                    if not self.at_least_one_non_fatal_error:
                        self.summary_message = (
                            _("Backup saved successfully.")
                            + "\n\n"
                            + self.summary_message
                            + "\n"
                        )
                    else:
                        self.summary_message = (
                            _("Backup succeeded with some errors:")
                            + "\n\n"
                            + self.summary_message
                            + "\n"
                        )
                else:
                    heading = _("Backup operation failed:")
                    extra = ("\n\n" + message) if message else ""
                    self.summary_message = (
                        heading + "\n\n" + self.summary_message + extra + "\n"
                    )
                    self.ui_manager.display_error_message(
                        summary_message=self.summary_message, heading=heading
                    )

                if not is_unmounted and umount_message:
                    self.summary_message += umount_message + "\n"

                self.summary_message += duration_message + "\n"
                post_task_action: str = self.ui_manager.get_post_task_action()
                if post_task_action != "DO_NOTHING":
                    if succeeded:
                        has_scheduled, msg = Utility.schedule_shutdown_reboot(
                            post_task_action
                        )
                        self.summary_message += "\n" + msg
                    else:
                        self.summary_message += "\n" + _(
                            "Shutdown/Reboot cancelled due to errors."
                        )

            self.populate_summary_page()
        except Exception:
            callback_succeeded = False
            raise
        finally:
            with self.backup_in_progress_lock:
                self._is_batch_mode = False
                self.backup_in_progress = False
            self.completed_backup_callback(callback_succeeded)

    # Intended to be called via event thread
    def cancel_backup(self):
        self.requested_stop = True
        try:
            self.ui_manager.update_progress_status(
                message=_("Stopping backup operation...")
            )
        except Exception:
            pass
        with self._proc_lock:
            active_procs = list(self.proc.items())
            pgroup_pids = set(self._pgroup_pids)
        if len(active_procs) == 0:
            self._log("Nothing to cancel")
        else:
            self._log(
                "Will send cancel signal to " + str(len(active_procs)) + " processes."
            )
            for _key, process in active_procs:
                try:
                    self._log("Sending SIGTERM to " + str(process))
                    pid = getattr(process, "pid", None)
                    if pid is not None and pid in pgroup_pids:
                        try:
                            os.killpg(pid, signal.SIGTERM)
                            continue
                        except Exception:
                            pass
                    process.terminate()
                except Exception:
                    self._log("Error killing process. (Maybe already dead?)")
        # Both batch and single-drive worker threads observe self.requested_stop,
        # terminate/reap active subprocesses, clean up partial files, unmount
        # /mnt/backup once, and invoke the completion callback exactly once.
        return

    def do_backup_wrapper(self):
        try:
            self.do_backup()
        except Exception:
            tb = traceback.format_exc()
            traceback.print_exc()
            self.ui_manager.completed_operation(
                callable_fn=self.completed_backup,
                succeeded=False,
                message=_("Error creating backup: ") + tb,
            )
            return

    def _get_all_partitions(self) -> Dict[str, Any]:
        # CLI version of Drive query's populate_partition_selection_table that's then extracted
        # by handler.py based on user-selection, with data flowing through the Gtk ListStore table.
        # This is confusing, and needs refactoring.
        to_return = collections.OrderedDict()
        partitions = self.drive_state[self.selected_drive_key]["partitions"]
        for partition_key in partitions:
            to_return[partition_key] = partitions[partition_key]
            to_return[partition_key]["description"] = (
                CombinedDriveState.flatten_partition_description(
                    self.drive_state, self.selected_drive_key, partition_key
                )
            )
        return to_return

    def do_backup(self):
        try:
            return self._do_backup_inner()
        except _BackupCancelledError:
            self.ui_manager.completed_operation(
                callable_fn=self.completed_backup,
                succeeded=False,
                message=_("User requested operation to stop."),
            )
            return False, _("User requested operation to stop.")

    def _do_backup_inner(self):
        pp = pprint.PrettyPrinter(indent=4)
        pp.pprint(self.partitions_to_backup)
        self.at_least_one_non_fatal_error = False
        self.failed_partition_count = 0
        self.succeeded_partition_count = 0
        # Clear proc dictionary under lock
        with self._proc_lock:
            self.proc.clear()
            self._pgroup_pids.clear()
        self.summary_message_lock = threading.Lock()
        self.summary_message = ""
        if self.requested_stop:
            raise _BackupCancelledError()

        # partitions_to_backup is normally dictionary, except when it's set by the CLI argument then it's a list.
        # TODO: this is a silly quick-fix and the CLI argument should be refactored.
        if (
            len(self.partitions_to_backup) == 1
            and isinstance(self.partitions_to_backup, list)
            and self.partitions_to_backup[0] == "all"
        ):
            self.partitions_to_backup = self._get_all_partitions()

        if self.is_rescue and not self.is_cloning:
            with self.summary_message_lock:
                self.summary_message += _("Rescue option is enabled.") + "\n"

        env = Utility.get_env_C_locale()

        failed_to_write = _(
            "Failed to write destination file. Please confirm it is valid to create the provided file path, and try again."
        )
        try:
            print("mkdir " + self.dest_dir)
            os.mkdir(self.dest_dir)
        except Exception:
            tb = traceback.format_exc()
            traceback.print_exc()
            error_message = failed_to_write + "\n\n" + tb
            self.ui_manager.completed_operation(
                callable_fn=self.completed_backup,
                succeeded=False,
                message=error_message,
            )
            return False, error_message

        short_selected_device_node = re.sub("/dev/", "", self.selected_drive_key)
        enduser_date = datetime.today().strftime("%Y-%m-%d-%H%M")
        clonezilla_img_filepath = os.path.join(self.dest_dir, "clonezilla-img")
        with open(clonezilla_img_filepath, "w") as filehandle:
            try:
                output = (
                    "This image was saved by Rescuezilla at "
                    + enduser_date
                    + "\nSaved by "
                    + self.human_readable_version
                    + "\nThe log during saving:\n----------------------------------------------------------\n\n"
                )
                filehandle.write(output)
            except Exception:
                tb = traceback.format_exc()
                traceback.print_exc()
                error_message = failed_to_write + "\n\n" + tb
                self.ui_manager.completed_operation(
                    callable_fn=self.completed_backup,
                    succeeded=False,
                    message=error_message,
                )
                return False, error_message

        self.logger = Logger(clonezilla_img_filepath)

        # Backup RAID information (Clonezilla's dump_software_raid_info_if_exists function)
        original_proc_mdstat_filepath = "/proc/mdstat"
        if os.path.isfile(original_proc_mdstat_filepath):
            proc_mdstat_string = Utility.read_file_into_string(
                original_proc_mdstat_filepath
            )
            proc_mdstat_dict = ProcMdstat.parse_proc_mdstat_string(proc_mdstat_string)
            if len(proc_mdstat_dict.keys()) > 0:
                # Copy the mdstat file to the destination disk, as CLonezilla
                copied_proc_mdstat_filepath = os.path.join(self.dest_dir, "mdstat.txt")
                self.ui_manager.display_status(
                    msg1=_("Saving: {file}").format(file=copied_proc_mdstat_filepath),
                    msg2="",
                )
                with open(copied_proc_mdstat_filepath, "w") as filehandle:
                    filehandle.write(proc_mdstat_string)
                    filehandle.flush()
                # Save mdadm.conf
                mdadm_conf_filepath = os.path.join(self.dest_dir, "mdadm.conf")
                self.ui_manager.display_status(
                    msg1=_("Saving: {file}").format(file=mdadm_conf_filepath), msg2=""
                )
                process, flat_command_string, failed_message = self._run_cancellable(
                    "Saving mdadm.conf",
                    ["mdadm", "--detail", "--scan"],
                    use_c_locale=True,
                    output_filepath=mdadm_conf_filepath,
                    logger=self.logger,
                )
                if process.returncode != 0:
                    # Clonezilla doesn't handle non-zero return code, so to maximize compatibility Rescuezilla doesn't
                    # either
                    self.ui_manager.display_error_message(
                        summary_message=failed_message
                    )
                # Export details for each RAID device
                for raid_dev_key in proc_mdstat_dict.keys():
                    raid_device_query_filepath = os.path.join(
                        self.dest_dir, raid_dev_key + ".txt"
                    )
                    self.ui_manager.display_status(
                        msg1=_("Saving: {file}").format(
                            file=raid_device_query_filepath
                        ),
                        msg2="",
                    )
                    process, flat_command_string, failed_message = (
                        self._run_cancellable(
                            "Saving " + raid_device_query_filepath,
                            [
                                "mdadm",
                                "--query",
                                "--detail",
                                "--export",
                                "/dev/" + raid_dev_key,
                            ],
                            use_c_locale=True,
                            output_filepath=raid_device_query_filepath,
                            logger=self.logger,
                        )
                    )
                    if process.returncode != 0:
                        # Clonezilla doesn't handle non-zero return code, so to maximize compatibility Rescuezilla
                        # doesn't either
                        self.ui_manager.display_error_message(
                            summary_message=failed_message
                        )

        if not self.is_cloning:
            backup_notes_filepath = os.path.join(
                self.dest_dir, "rescuezilla.description.txt"
            )
            if self.backup_notes.strip() != "":
                self.logger.write(
                    "Writing backup description file to " + backup_notes_filepath
                )
                self.ui_manager.display_status(
                    msg1=_("Saving: {file}").format(file=backup_notes_filepath), msg2=""
                )
                with open(backup_notes_filepath, "w") as filehandle:
                    filehandle.write(self.backup_notes)
                    filehandle.flush()

            blkdev_list_filepath = os.path.join(self.dest_dir, "blkdev.list")
            self.ui_manager.display_status(
                msg1=_("Saving: {file}").format(file=blkdev_list_filepath), msg2=""
            )
            process, flat_command_string, failed_message = self._run_cancellable(
                "Saving blkdev.list",
                [
                    "lsblk",
                    "-oKNAME,NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL",
                    self.selected_drive_key,
                ],
                use_c_locale=True,
                output_filepath=blkdev_list_filepath,
                logger=self.logger,
            )
            if process.returncode != 0:
                with self.summary_message_lock:
                    self.summary_message += failed_message
                self.ui_manager.completed_operation(
                    callable_fn=self.completed_backup,
                    succeeded=False,
                    message=failed_message,
                )
                return False, failed_message

            blkid_list_filepath = os.path.join(self.dest_dir, "blkid.list")
            self.ui_manager.display_status(
                msg1=_("Saving: {file}").format(file=blkid_list_filepath), msg2=""
            )
            blkid_cmd_list = ["blkid"]
            sort_cmd_list = ["sort", "-V"]
            Utility.print_cli_friendly("blkid ", [blkid_cmd_list, sort_cmd_list])
            process, flat_command_string, failed_message = self._run_cancellable(
                "Saving blkid.list",
                ["blkid"],
                use_c_locale=True,
                output_filepath=blkid_list_filepath,
                logger=self.logger,
            )
            if process.returncode != 0:
                with self.summary_message_lock:
                    self.summary_message += failed_message
                self.ui_manager.completed_operation(
                    callable_fn=self.completed_backup,
                    succeeded=False,
                    message=failed_message,
                )
                return False, failed_message

            info_lshw_filepath = os.path.join(self.dest_dir, "Info-lshw.txt")
            self.ui_manager.display_status(
                msg1=_("Saving: {file}").format(file=info_lshw_filepath), msg2=""
            )
            process, flat_command_string, failed_message = self._run_cancellable(
                "Saving Info-lshw.txt",
                ["lshw"],
                use_c_locale=True,
                output_filepath=info_lshw_filepath,
                logger=self.logger,
            )
            if process.returncode != 0:
                with self.summary_message_lock:
                    self.summary_message += failed_message
                self.ui_manager.completed_operation(
                    callable_fn=self.completed_backup,
                    succeeded=False,
                    message=failed_message,
                )
                return False, failed_message

            info_dmi_txt_filepath = os.path.join(self.dest_dir, "Info-dmi.txt")
            self.ui_manager.display_status(
                msg1=_("Saving: {file}").format(file=info_dmi_txt_filepath), msg2=""
            )
            with open(info_dmi_txt_filepath, "w") as filehandle:
                filehandle.write(
                    "# This image was saved from this machine with DMI info at "
                    + enduser_date
                    + ":\n"
                )
                filehandle.flush()
            process, flat_command_string, failed_message = self._run_cancellable(
                "Saving Info-dmi.txt",
                ["dmidecode"],
                use_c_locale=True,
                output_filepath=info_dmi_txt_filepath,
                logger=self.logger,
            )
            if process.returncode != 0:
                with self.summary_message_lock:
                    self.summary_message += failed_message
                self.ui_manager.completed_operation(
                    callable_fn=self.completed_backup,
                    succeeded=False,
                    message=failed_message,
                )
                return False, failed_message

            info_lspci_filepath = os.path.join(self.dest_dir, "Info-lspci.txt")
            with open(info_lspci_filepath, "w") as filehandle:
                # TODO: Improve datetime format string.
                filehandle.write(
                    "This image was saved from this machine with PCI info at "
                    + enduser_date
                    + "\n"
                )
                filehandle.write("'lspci' results:\n")
                filehandle.flush()

            process, flat_command_string, failed_message = self._run_cancellable(
                "Appending `lspci` output to Info-lspci.txt",
                ["lspci"],
                use_c_locale=True,
                output_filepath=info_lspci_filepath,
                logger=self.logger,
            )
            if process.returncode != 0:
                with self.summary_message_lock:
                    self.summary_message += failed_message
                self.ui_manager.completed_operation(
                    callable_fn=self.completed_backup,
                    succeeded=False,
                    message=failed_message,
                )
                return False, failed_message

            msg_delimiter_star_line = (
                "*****************************************************."
            )
            with open(info_lspci_filepath, "a+") as filehandle:
                filehandle.write(msg_delimiter_star_line + "\n")
                filehandle.write("'lspci -n' results:\n")
                filehandle.flush()

            # Show PCI vendor and device codes as numbers instead of looking them up in the PCI ID list.
            process, flat_command_string, failed_message = self._run_cancellable(
                "Appending `lspci -n` output to Info-lspci.txt",
                ["lspci", "-n"],
                use_c_locale=True,
                output_filepath=info_lspci_filepath,
                logger=self.logger,
            )
            if process.returncode != 0:
                with self.summary_message_lock:
                    self.summary_message += failed_message
                self.ui_manager.completed_operation(
                    callable_fn=self.completed_backup,
                    succeeded=False,
                    message=failed_message,
                )
                return False, failed_message

            info_smart_filepath = os.path.join(self.dest_dir, "Info-smart.txt")
            self.ui_manager.display_status(
                msg1=_("Saving: {file}").format(file=info_smart_filepath), msg2=""
            )
            with open(info_smart_filepath, "w") as filehandle:
                filehandle.write(
                    "This image was saved from this machine with hard drive S.M.A.R.T. info at "
                    + enduser_date
                    + "\n"
                )
                filehandle.write(msg_delimiter_star_line + "\n")
                filehandle.write("For the drive: " + self.selected_drive_key + "\n")
                filehandle.flush()

            # VirtualBox doesn't support smart, so ignoring the exit code here.
            # FIXME: Improve this.
            process, flat_command_string, failed_message = self._run_cancellable(
                "Saving Info-smart.txt",
                ["smartctl", "--all", self.selected_drive_key],
                use_c_locale=True,
                output_filepath=info_smart_filepath,
                logger=self.logger,
            )

            info_os_prober_filepath = os.path.join(self.dest_dir, "Info-OS-prober.txt")
            self.ui_manager.display_status(
                msg1=_("Saving: {file}").format(file=info_os_prober_filepath), msg2=""
            )
            with open(info_os_prober_filepath, "w") as filehandle:
                filehandle.write(
                    "This OS-related info was saved from this machine with os-prober at "
                    + enduser_date
                    + "\n"
                )
                filehandle.flush()
            process, flat_command_string, failed_message = self._run_cancellable(
                "Running os-prober and appending output to Info-OS-prober.txt",
                ["os-prober"],
                use_c_locale=True,
                output_filepath=info_os_prober_filepath,
                logger=self.logger,
            )
            if process.returncode != 0:
                self.logger.write(failed_message)
                # Not considering os-prober exit code as fatal, to match Clonezilla's behavior

            with open(info_os_prober_filepath, "a+") as filehandle:
                filehandle.write(msg_delimiter_star_line + "\n")
                filehandle.write(
                    "This Linux boot related info was saved from this machine with linux-boot-prober at "
                    + enduser_date
                    + "\n"
                )
                filehandle.flush()

            for partition_key in self.partitions_to_backup:
                process, flat_command_string, failed_message = self._run_cancellable(
                    "Running linux-boot-prober for " + partition_key,
                    ["linux-boot-prober", partition_key],
                    use_c_locale=True,
                    output_filepath=info_os_prober_filepath,
                    logger=self.logger,
                )
                if process.returncode != 0:
                    self.logger.write(failed_message)
                    # Not considering os-prober exit code as fatal, to match Clonezilla's behavior

            filepath = os.path.join(self.dest_dir, "Info-packages.txt")
            self.ui_manager.display_status(
                msg1=_("Saving: {file}").format(file=filepath), msg2=""
            )
            # Save Debian package information
            if shutil.which("dpkg") is not None:
                rescuezilla_package_list = [
                    "rescuezilla",
                    "partclone",
                    "util-linux",
                    "gdisk",
                ]
                with open(filepath, "w") as filehandle:
                    filehandle.write(
                        "Image was saved by these Rescuezilla-related packages:\n "
                    )
                    for pkg in rescuezilla_package_list:
                        dpkg_process = subprocess.run(
                            ["dpkg", "--status", pkg],
                            capture_output=True,
                            encoding="UTF-8",
                        )
                        if dpkg_process.returncode != 0:
                            continue
                        for line in dpkg_process.stdout.split("\n"):
                            if re.search("^Version: ", line):
                                version = line[len("Version: ") :]
                                filehandle.write(pkg + "-" + version + " ")
                    filehandle.write(
                        "\nSaved by " + self.human_readable_version + ".\n"
                    )

        # TODO: Clonezilla creates a file named "Info-saved-by-cmd.txt" file, to allow users to re-run the exact
        #  command again without going through the wizard. The proposed Rescuezilla approach to this feature is
        #  discussed here: https://github.com/rescuezilla/rescuezilla/issues/106

        filepath = os.path.join(self.dest_dir, "parts")
        self.ui_manager.display_status(
            msg1=_("Saving: {file}").format(file=filepath), msg2=""
        )
        with open(filepath, "w") as filehandle:
            i = 0
            for partition_key in self.partitions_to_backup:
                short_partition_key = re.sub("/dev/", "", partition_key)
                to_backup_dict = self.partitions_to_backup[partition_key]
                is_swap = False
                if (
                    "filesystem" in to_backup_dict.keys()
                    and to_backup_dict["filesystem"] == "swap"
                ):
                    is_swap = True
                if (
                    "type" not in to_backup_dict.keys()
                    or "type" in to_backup_dict.keys()
                    and "extended" != to_backup_dict["type"]
                    and not is_swap
                ):
                    # Clonezilla does not write the extended partition node into the parts file,
                    # nor does it write swap partition node
                    filehandle.write("%s" % short_partition_key)
                    # Ensure no trailing space on final iteration (to match Clonezilla format exactly)
                    if i + 1 != len(self.partitions_to_backup.keys()):
                        filehandle.write(" ")
                i += 1
            # FIXME: This diverges from Clonezilla behavior. It's possible for Rescuezilla to write just a newline for
            # FIXME: eg, disk full of only swap partitions. Rescuezilla can handle restoring such images, but Clonezilla
            # FIXME: cannot (a divergence of behavior).
            filehandle.write("\n")

        filepath = os.path.join(self.dest_dir, "disk")
        self.ui_manager.display_status(
            msg1=_("Saving: {file}").format(file=filepath), msg2=""
        )
        with open(filepath, "w") as filehandle:
            filehandle.write("%s\n" % short_selected_device_node)

        compact_parted_filepath = os.path.join(
            self.dest_dir, short_selected_device_node + "-pt.parted.compact"
        )
        self.ui_manager.display_status(
            msg1=_("Saving: {file}").format(file=compact_parted_filepath), msg2=""
        )
        # Parted drive information with human-readable "compact" units: KB/MB/GB rather than sectors.
        process, flat_command_string, failed_message = self._run_cancellable(
            "Saving " + compact_parted_filepath,
            ["parted", "--script", self.selected_drive_key, "unit", "compact", "print"],
            use_c_locale=True,
            output_filepath=compact_parted_filepath,
            logger=self.logger,
        )
        if process.returncode != 0:
            # Human-readable compact units is not that important.
            print(failed_message)
            if not self.is_cloning:
                self.ui_manager.display_error_message(summary_message=failed_message)

        # Parted drive information with standard sector units. Clonezilla doesn't output easily parsable output using
        # the --machine flag, so for maximum Clonezilla compatibility neither does Rescuezilla.
        parted_filepath = os.path.join(
            self.dest_dir, short_selected_device_node + "-pt.parted"
        )
        self.ui_manager.display_status(
            msg1=_("Saving: {file}").format(file=parted_filepath), msg2=""
        )
        parted_process, flat_command_string, failed_message = self._run_cancellable(
            "Saving " + parted_filepath,
            ["parted", "--script", self.selected_drive_key, "unit", "s", "print"],
            use_c_locale=True,
            output_filepath=parted_filepath,
            logger=self.logger,
        )
        if parted_process.returncode != 0:
            print(failed_message)
            # Clonezilla doesn't consider non-zero parted return code as fatal. Indeed, RAID md devices without a
            # partition table returns an error using parted but Clonezilla is happy to continue. Rescuezilla does the
            # same (but displays the error to the user). For cloning, the not displaying any error message is fine.
            if not self.is_cloning:
                with self.summary_message_lock:
                    self.summary_message += failed_message
                self.ui_manager.display_error_message(summary_message=failed_message)

        if os.path.isdir("/sys/firmware/efi/efivars"):
            # Save EFI NVRAM info. What we need is actually the label
            efi_nvram_filepath = os.path.join(self.dest_dir, "efi-nvram.dat")
            self.ui_manager.display_status(
                msg1=_("Saving: {file}").format(file=efi_nvram_filepath), msg2=""
            )
            process, flat_command_string, failed_message = self._run_cancellable(
                "Saving EFI NVRAM info",
                ["efibootmgr", "--verbose"],
                use_c_locale=True,
                output_filepath=efi_nvram_filepath,
                logger=self.logger,
            )
            if process.returncode != 0:
                with self.summary_message_lock:
                    self.summary_message += failed_message
                self.ui_manager.completed_operation(
                    callable_fn=self.completed_backup,
                    succeeded=False,
                    message=failed_message,
                )
                return False, failed_message
            if self.is_cloning:
                self.metadata_only_image_to_annotate.efi_nvram_dat_absolute_path = (
                    efi_nvram_filepath
                )

        parted_dict = Parted.parse_parted_output(parted_process.stdout)
        partition_table = parted_dict["partition_table"]

        # Save MBR for both msdos and GPT disks
        if "gpt" == partition_table or "msdos" == partition_table:
            filepath = os.path.join(self.dest_dir, short_selected_device_node + "-mbr")
            self.ui_manager.display_status(
                msg1=_("Saving: {file}").format(file=filepath), msg2=""
            )
            process, flat_command_string, failed_message = self._run_cancellable(
                "Saving " + filepath,
                [
                    "dd",
                    "if=" + self.selected_drive_key,
                    "of=" + filepath,
                    "bs=512",
                    "count=1",
                ],
                use_c_locale=False,
                logger=self.logger,
            )
            if process.returncode != 0:
                with self.summary_message_lock:
                    self.summary_message += failed_message
                self.ui_manager.completed_operation(
                    callable_fn=self.completed_backup,
                    succeeded=False,
                    message=failed_message,
                )
                return False, failed_message
            if self.is_cloning:
                self.metadata_only_image_to_annotate._mbr_absolute_path = filepath

        if "gpt" == partition_table and not self.is_cloning:
            first_gpt_filename = short_selected_device_node + "-gpt-1st"
            self.ui_manager.display_status(
                msg1=_("Saving: {file}").format(file=first_gpt_filename), msg2=""
            )
            dd_process, flat_command_string, failed_message = self._run_cancellable(
                "Saving " + first_gpt_filename,
                [
                    "dd",
                    "if=" + self.selected_drive_key,
                    "of=" + os.path.join(self.dest_dir, first_gpt_filename),
                    "bs=512",
                    "count=34",
                ],
                use_c_locale=False,
                logger=self.logger,
            )
            if dd_process.returncode != 0:
                with self.summary_message_lock:
                    self.summary_message += failed_message
                self.ui_manager.completed_operation(
                    callable_fn=self.completed_backup,
                    succeeded=False,
                    message=failed_message,
                )
                return False, failed_message

            # From Clonezilla's scripts/sbin/ocs-functions:
            # We need to get the total size of disk so that we can skip and dump the last block:
            # The output of 'parted -s /dev/sda unit s print' is like:
            # --------------------
            # Disk /dev/hda: 16777215s
            # Sector size (logical/physical): 512B/512B
            # Partition Table: gpt
            #
            # Number  Start     End        Size       File system  Name     Flags
            #  1      34s       409640s    409607s    fat32        primary  msftres
            #  2      409641s   4316406s   3906766s   ext2         primary
            #  3      4316407s  15625000s  11308594s  reiserfs     primary
            # --------------------
            # to_seek = "$((${src_disk_size_sec}-33+1))"
            to_skip = parted_dict["capacity"] - 32
            second_gpt_filename = short_selected_device_node + "-gpt-2nd"
            self.ui_manager.display_status(
                msg1=_("Saving: {file}").format(file=second_gpt_filename), msg2=""
            )
            process, flat_command_string, failed_message = self._run_cancellable(
                "Saving " + second_gpt_filename,
                [
                    "dd",
                    "if=" + self.selected_drive_key,
                    "of=" + os.path.join(self.dest_dir, second_gpt_filename),
                    "skip=" + str(to_skip),
                    "bs=512",
                    "count=33",
                ],
                use_c_locale=False,
                logger=self.logger,
            )
            if process.returncode != 0:
                with self.summary_message_lock:
                    self.summary_message += failed_message
                self.ui_manager.completed_operation(
                    callable_fn=self.completed_backup,
                    succeeded=False,
                    message=failed_message,
                )
                return False, failed_message

            # LC_ALL=C sgdisk -b $target_dir_fullpath/$(to_filename ${ihd})-gpt.gdisk /dev/$ihd | tee --append ${OCS_LOGFILE}
            gdisk_filename = short_selected_device_node + "-gpt.gdisk"
            self.ui_manager.display_status(
                msg1=_("Saving: {file}").format(file=gdisk_filename), msg2=""
            )
            process, flat_command_string, failed_message = self._run_cancellable(
                "Saving " + gdisk_filename,
                [
                    "sgdisk",
                    "--backup",
                    os.path.join(self.dest_dir, gdisk_filename),
                    self.selected_drive_key,
                ],
                use_c_locale=True,
                logger=self.logger,
            )
            if process.returncode != 0:
                with self.summary_message_lock:
                    self.summary_message += failed_message
                self.ui_manager.completed_operation(
                    callable_fn=self.completed_backup,
                    succeeded=False,
                    message=failed_message,
                )
                return False, failed_message

            sgdisk_filename = short_selected_device_node + "-gpt.sgdisk"
            self.ui_manager.display_status(
                msg1=_("Saving: {file}").format(file=sgdisk_filename), msg2=""
            )
            process, flat_command_string, failed_message = self._run_cancellable(
                "Saving " + sgdisk_filename,
                ["sgdisk", "--print", self.selected_drive_key],
                use_c_locale=True,
                output_filepath=os.path.join(self.dest_dir, sgdisk_filename),
                logger=self.logger,
            )
            if process.returncode != 0:
                with self.summary_message_lock:
                    self.summary_message += failed_message
                self.ui_manager.completed_operation(
                    callable_fn=self.completed_backup,
                    succeeded=False,
                    message=failed_message,
                )
                return False, failed_message
        elif "msdos" == partition_table:
            # image_save
            first_partition_key, first_partition_offset_bytes = (
                CombinedDriveState.get_first_partition(self.partitions_to_backup)
            )
            # Maximum hidden data to backup is 1024MB
            hidden_data_after_mbr_limit = 1024 * 1024 * 1024
            self.logger.write(
                "Detected first partition is "
                + first_partition_key
                + " at byte offset "
                + str(first_partition_offset_bytes)
                + "\n"
            )
            if first_partition_offset_bytes > hidden_data_after_mbr_limit:
                # If the post-mbr gap is too large, Clonezilla does not backup the data but instead creates a notes file
                post_mbr_gap_sector_count = 2047
                self.logger.write(
                    "Calculated very large hidden data after MBR size, so copying minimal post-MBR gap"
                )
                not_creating_hidden_data_info_filepath = os.path.join(
                    self.dest_dir,
                    short_selected_device_node + "-hidden-data-after-mbr.notes.txt",
                )
                self.ui_manager.display_status(
                    msg1=_("Saving: {file}").format(
                        file=not_creating_hidden_data_info_filepath
                    ),
                    msg2="",
                )
                with open(not_creating_hidden_data_info_filepath, "w") as filehandle:
                    try:
                        output = (
                            "The hidden data space size ("
                            + str(first_partition_offset_bytes)
                            + " bytes) is larger than the "
                            + str(hidden_data_after_mbr_limit)
                            + " byte limit. Copying minimal post-MBR gap.\n"
                        )
                        filehandle.write(output)
                    except Exception:
                        tb = traceback.format_exc()
                        traceback.print_exc()
                        error_message = (
                            _(
                                "Failed to write hidden data info file. Please confirm it is valid to create the provided file path, and try again."
                            )
                            + "\n\n"
                            + tb
                        )
                        return self.ui_manager.completed_operation(
                            callable_fn=self.completed_backup,
                            succeeded=False,
                            message=error_message,
                        )
                        return False, error_message

            else:
                first_partition_offset_sectors = int(first_partition_offset_bytes / 512)
                post_mbr_gap_sector_count = first_partition_offset_sectors - 1

            if post_mbr_gap_sector_count < 0:
                failed_message = (
                    "Rescuezilla was unable to identify the location of the first partition which indicates "
                    "the program was unable to gather information about the drive correctly. Please report "
                    "this to the Rescuezilla developer, ideally with logs."
                )
                with self.summary_message_lock:
                    self.summary_message += failed_message
                self.ui_manager.completed_operation(
                    callable_fn=self.completed_backup,
                    succeeded=False,
                    message=failed_message,
                )
                return False, failed_message

            hidden_mbr_data_filepath = os.path.join(
                self.dest_dir, short_selected_device_node + "-hidden-data-after-mbr"
            )
            self.ui_manager.display_status(
                msg1=_("Saving: {file}").format(file=hidden_mbr_data_filepath), msg2=""
            )
            process, flat_command_string, failed_message = self._run_cancellable(
                "Saving " + hidden_mbr_data_filepath,
                [
                    "dd",
                    "if=" + self.selected_drive_key,
                    "of=" + hidden_mbr_data_filepath,
                    "skip=1",
                    "bs=512",
                    "count=" + str(post_mbr_gap_sector_count),
                ],
                use_c_locale=False,
                logger=self.logger,
            )
            if process.returncode != 0:
                with self.summary_message_lock:
                    self.summary_message += failed_message
                self.ui_manager.completed_operation(
                    callable_fn=self.completed_backup,
                    succeeded=False,
                    message=failed_message,
                )
                return False, failed_message
            if self.is_cloning:
                self.metadata_only_image_to_annotate.post_mbr_gap_dict[
                    "absolute_path"
                ] = hidden_mbr_data_filepath

        else:
            self.logger.write("Partition table is: " + partition_table)

        # Parted sees drives with direct filesystem applied as loop partition table.
        if partition_table is not None and partition_table != "loop":
            sfdisk_filepath = os.path.join(
                self.dest_dir, short_selected_device_node + "-pt.sf"
            )
            self.ui_manager.display_status(
                msg1=_("Saving: {file}").format(file=sfdisk_filepath), msg2=""
            )
            process, flat_command_string, failed_message = self._run_cancellable(
                "Saving " + sfdisk_filepath,
                ["sfdisk", "--dump", self.selected_drive_key],
                output_filepath=sfdisk_filepath,
                use_c_locale=True,
                logger=self.logger,
            )
            if process.returncode != 0:
                print(failed_message)
                if not self.is_cloning:
                    with self.summary_message_lock:
                        self.summary_message += failed_message
                    # Matches Clonezilla by not considering sfdisk failure as fatal.
                    self.ui_manager.display_error_message(
                        summary_message=failed_message
                    )

        filepath = os.path.join(self.dest_dir, short_selected_device_node + "-chs.sf")
        self.ui_manager.display_status(
            msg1=_("Saving: {file}").format(file=filepath), msg2=""
        )
        process, flat_command_string, failed_message = self._run_cancellable(
            "Retrieving disk geometry with sfdisk ",
            ["sfdisk", "--show-geometry", self.selected_drive_key],
            use_c_locale=True,
            logger=self.logger,
        )
        if process.returncode != 0:
            self.logger.write(failed_message)
            with self.summary_message_lock:
                self.summary_message += (
                    "Failed to retrieve disk geometry for "
                    + self.selected_drive_key
                    + "."
                )
            self.ui_manager.completed_operation(
                callable_fn=self.completed_backup,
                succeeded=False,
                message=failed_message,
            )
            return False, failed_message
        else:
            geometry_dict = Sfdisk.parse_sfdisk_show_geometry(process.stdout)
            with open(filepath, "w") as filehandle:
                for key in geometry_dict.keys():
                    output = key + "=" + str(geometry_dict[key])
                    self.logger.write(output)
                    filehandle.write("%s\n" % output)
            if self.is_cloning:
                self.metadata_only_image_to_annotate.sfdisk_chs_dict = geometry_dict

        if not self.is_cloning:
            # Query all Physical Volumes (PV), Volume Group (VG) and Logical Volume (LV). See unit test for a worked example.
            # TODO: In the Rescuezilla application architecture, this LVM information is best extracted during the drive
            # TODO: query step, and then integrated into the "combined drive state" dictionary. Doing it during the backup
            # TODO: process matches how Clonezilla does it, which is sufficient for now.
            # FIXME: This section is duplicated in partitions_to_restore.py.
            # Start the Logical Volume Manager (LVM). Caller raises Exception on failure
            Lvm.start_lvm2(self.logger)
            relevant_vg_name_dict = {}
            vg_state_dict = Lvm.get_volume_group_state_dict(self.logger)
            for partition_key in list(self.partitions_to_backup.keys()):
                for report_dict in vg_state_dict["report"]:
                    for vg_dict in report_dict["vg"]:
                        if "pv_name" in vg_dict.keys() and (
                            partition_key == vg_dict["pv_name"]
                            or self.selected_drive_key == vg_dict["pv_name"]
                        ):
                            if "vg_name" in vg_dict.keys():
                                vg_name = vg_dict["vg_name"]
                            else:
                                self.ui_manager.display_error_message(
                                    summary_message="Could not find volume group name vg_name in "
                                    + str(vg_dict)
                                )
                                # TODO: Re-evaluate how exactly Clonezilla uses /NOT_FOUND and whether introducing it here
                                # TODO: could improve Rescuezilla/Clonezilla interoperability.
                                continue
                            if "pv_uuid" in vg_dict.keys():
                                pv_uuid = vg_dict["pv_uuid"]
                            else:
                                self.ui_manager.display_error_message(
                                    summary_message="Could not find physical volume UUID pv_uuid in "
                                    + str(vg_dict)
                                )
                                continue
                            relevant_vg_name_dict[vg_name] = partition_key
                            lvm_vg_dev_list_filepath = os.path.join(
                                self.dest_dir, "lvm_vg_dev.list"
                            )
                            self.ui_manager.display_status(
                                msg1=_("Saving: {file}").format(
                                    file=lvm_vg_dev_list_filepath
                                ),
                                msg2="",
                            )
                            with open(lvm_vg_dev_list_filepath, "a+") as filehandle:
                                if partition_key == vg_dict["pv_name"]:
                                    filehandle.write(
                                        vg_name
                                        + " "
                                        + partition_key
                                        + " "
                                        + pv_uuid
                                        + "\n"
                                    )
                                elif self.selected_drive_key == vg_dict["pv_name"]:
                                    filehandle.write(
                                        vg_name
                                        + " "
                                        + self.selected_drive_key
                                        + " "
                                        + pv_uuid
                                        + "\n"
                                    )

            lv_state_dict = Lvm.get_logical_volume_state_dict(self.logger)
            for report_dict in lv_state_dict["report"]:
                for lv_dict in report_dict["lv"]:
                    # Only consider VGs that match the partitions to backup list
                    if (
                        "vg_name" in lv_dict.keys()
                        and lv_dict["vg_name"] in relevant_vg_name_dict.keys()
                    ):
                        vg_name = lv_dict["vg_name"]
                        if "lv_path" in lv_dict.keys():
                            lv_path = lv_dict["lv_path"]
                        else:
                            self.ui_manager.display_error_message(
                                summary_message="Could not find lv_path name in "
                                + str(lv_dict)
                            )
                            continue
                        file_command_process, flat_command_string, failed_message = (
                            self._run_cancellable(
                                "logical volume file info",
                                ["file", "--dereference", "--special-files", lv_path],
                                use_c_locale=True,
                                logger=self.logger,
                            )
                        )
                        if file_command_process.returncode != 0:
                            with self.summary_message_lock:
                                self.summary_message += failed_message
                            self.ui_manager.completed_operation(
                                callable_fn=self.completed_backup,
                                succeeded=False,
                                message=failed_message,
                            )
                            return False, failed_message

                        output = file_command_process.stdout.split(" ", maxsplit=1)[
                            1
                        ].strip()
                        lvm_logv_list_filepath = os.path.join(
                            self.dest_dir, "lvm_logv.list"
                        )
                        self.ui_manager.display_status(
                            msg1=_("Saving: {file}").format(
                                file=lvm_logv_list_filepath
                            ),
                            msg2="",
                        )
                        # Append to file
                        with open(lvm_logv_list_filepath, "a+") as filehandle:
                            filehandle.write(lv_path + "  " + output + "\n")

                        if "lv_dm_path" in lv_dict.keys():
                            # Device mapper path, eg /dev/mapper/vgtest-lvtest
                            lv_dm_path = lv_dict["lv_dm_path"]
                        else:
                            failed_message = "Could not find lv_dm_path name in " + str(
                                lv_dict
                            )
                            self.ui_manager.completed_operation(
                                callable_fn=self.completed_backup,
                                succeeded=False,
                                message=failed_message,
                            )
                            return False, failed_message

                        if (
                            lv_dm_path in self.drive_state.keys()
                            and "partitions" in self.drive_state[lv_dm_path].keys()
                        ):
                            # Remove the partition key associated with the volume group that contains this LVM logical volume
                            # eg, /dev/sdc1 with detected filesystem, and replace it with  the logical volume filesystem.
                            # In other words, don't backup both the /dev/sdc1 device node AND the /dev/mapper node.
                            long_partition_key = relevant_vg_name_dict[
                                lv_dict["vg_name"]
                            ]
                            self.partitions_to_backup.pop(long_partition_key, None)
                            for logical_volume in self.drive_state[lv_dm_path][
                                "partitions"
                            ].keys():
                                # Use the system drive state to get the exact filesystem for this /dev/mapper/ node,
                                # as derived from multiple sources (parted, lsblk etc) like how Clonezilla does it.
                                self.partitions_to_backup[lv_path] = self.drive_state[
                                    lv_dm_path
                                ]["partitions"][logical_volume]
                                self.partitions_to_backup[lv_path]["type"] = "part"

                        lvm_vgname_filepath = os.path.join(
                            self.dest_dir, "lvm_" + vg_name + ".conf"
                        )
                        # TODO: Evaluate the Clonezilla message from 2013 message that this command won't work on NFS
                        # TODO: due to a vgcfgbackup file lock issue.
                        self.ui_manager.display_status(
                            msg1=_("Saving: {file}").format(file=lvm_vgname_filepath),
                            msg2="",
                        )
                        vgcfgbackup_process, flat_command_string, failed_message = (
                            self._run_cancellable(
                                "Saving LVM VG config " + lvm_vgname_filepath,
                                ["vgcfgbackup", "--file", lvm_vgname_filepath, vg_name],
                                use_c_locale=True,
                                logger=self.logger,
                            )
                        )
                        if vgcfgbackup_process.returncode != 0:
                            with self.summary_message_lock:
                                self.summary_message += failed_message
                            self.ui_manager.completed_operation(
                                callable_fn=self.completed_backup,
                                succeeded=False,
                                message=failed_message,
                            )
                            return False, failed_message

        filepath = os.path.join(self.dest_dir, "dev-fs.list")
        self.ui_manager.display_status(
            msg1=_("Saving: {file}").format(file=filepath), msg2=""
        )
        with open(filepath, "w") as filehandle:
            filehandle.write("# <Device name>   <File system>\n")
            filehandle.write(
                "# The file systems detected below are a combination of several sources. The values may differ from `blkid` and `parted`.\n"
            )
            for partition_key in self.partitions_to_backup.keys():
                filesystem = self.partitions_to_backup[partition_key]["filesystem"]
                filehandle.write("%s %s\n" % (partition_key, filesystem))

        # Determine the size of each partition, and the total size. This is used for the weighted progress bar
        total_size = 0
        for partition_key in self.partitions_to_backup.keys():
            size = self.partitions_to_backup[partition_key]["size"]
            self.partitions_to_backup[partition_key]["cumulative_bytes"] = total_size
            total_size += size

        partition_number = 0
        for partition_key in self.partitions_to_backup.keys():
            if self.requested_stop:
                self.ui_manager.completed_operation(
                    callable_fn=self.completed_backup,
                    succeeded=False,
                    message=_("User requested operation to stop."),
                )
                return False, _("User requested operation to stop.")
            self.ui_manager.display_status(msg1="", msg2="")
            partition_number += 1
            total_progress_float = Utility.calculate_progress_ratio(
                current_partition_completed_percentage=0,
                current_partition_bytes=self.partitions_to_backup[partition_key][
                    "size"
                ],
                cumulative_bytes=self.partitions_to_backup[partition_key][
                    "cumulative_bytes"
                ],
                total_bytes=total_size,
                image_number=partition_number,
                num_partitions=len(self.partitions_to_backup),
            )
            self.ui_manager.update_progress_bar(fraction=total_progress_float)
            is_unmounted, message = Utility.umount_warn_on_busy(partition_key)
            if not is_unmounted:
                self.logger.write(message)
                with self.summary_message_lock:
                    self.summary_message += message + "\n"
                self.ui_manager.completed_operation(
                    callable_fn=self.completed_backup, succeeded=False, message=message
                )
                return False, message

            short_device_node = re.sub("/dev/", "", partition_key)
            short_device_node = re.sub("/", "-", short_device_node)
            filesystem = self.partitions_to_backup[partition_key]["filesystem"]

            if (
                "type" in self.partitions_to_backup[partition_key].keys()
                and "extended" in self.partitions_to_backup[partition_key]["type"]
            ):
                self.logger.write(
                    "Detected "
                    + partition_key
                    + " as extended partition. Backing up EBR"
                )
                filepath = os.path.join(self.dest_dir, short_device_node + "-ebr")
                self.ui_manager.display_status(
                    msg1=_("Saving: {file}").format(file=filepath), msg2=""
                )
                process, flat_command_string, failed_message = self._run_cancellable(
                    "Saving " + filepath,
                    [
                        "dd",
                        "if=" + partition_key,
                        "of=" + filepath,
                        "bs=512",
                        "count=1",
                    ],
                    use_c_locale=False,
                    logger=self.logger,
                )
                if process.returncode != 0:
                    with self.summary_message_lock:
                        self.summary_message += failed_message
                    self.ui_manager.completed_operation(
                        callable_fn=self.completed_backup,
                        succeeded=False,
                        message=failed_message,
                    )
                    return False, failed_message

                # TODO: Handle exit code
                if self.is_cloning:
                    self.metadata_only_image_to_annotate.ebr_dict = {
                        "short_device_node": short_device_node,
                        "absolute_path": filepath,
                    }
                continue

            if filesystem == "swap":
                filepath = os.path.join(
                    self.dest_dir, "swappt-" + short_device_node + ".info"
                )
                self.ui_manager.display_status(
                    msg1=_("Saving: {file}").format(file=filepath), msg2=""
                )
                with open(filepath, "w") as filehandle:
                    uuid = ""
                    label = ""
                    if "uuid" in self.partitions_to_backup[partition_key].keys():
                        uuid = self.partitions_to_backup[partition_key]["uuid"]
                    if "label" in self.partitions_to_backup[partition_key].keys():
                        label = self.partitions_to_backup[partition_key]["label"]
                    filehandle.write('UUID="%s"\n' % uuid)
                    filehandle.write('LABEL="%s"\n' % label)
                    with self.summary_message_lock:
                        self.summary_message += (
                            _(
                                "Successful backup of swap partition {partition_name}"
                            ).format(partition_name=partition_key)
                            + "\n"
                        )
                    if self.is_cloning:
                        self.metadata_only_image_to_annotate.image_format_dict_dict[
                            short_device_node
                        ] = Swappt.parse_swappt_info(
                            Utility.read_file_into_string(filepath)
                        )
                        self.metadata_only_image_to_annotate.image_format_dict_dict[
                            short_device_node
                        ]["type"] = "swap"
                        self.metadata_only_image_to_annotate.image_format_dict_dict[
                            short_device_node
                        ]["prefix"] = short_device_node
                        self.metadata_only_image_to_annotate.image_format_dict_dict[
                            short_device_node
                        ]["is_lvm_logical_volume"] = False
                continue

            if filesystem == "ntfs":
                # Create Clonezilla's NTFS boot reserved partition "sda1.info"
                tmp_mount = "/tmp/rescuezilla.ntfs.mount"
                is_success, message, is_partition_windows_boot_reserved = (
                    self.is_partition_windows_boot_reserved(partition_key, tmp_mount)
                )
                if not is_success:
                    with self.summary_message_lock:
                        self.summary_message += message + "\n"
                    self.ui_manager.completed_operation(
                        callable_fn=self.completed_backup,
                        succeeded=False,
                        message=message,
                    )
                    return False, message
                if is_partition_windows_boot_reserved:
                    partition_info_filepath = os.path.join(
                        self.dest_dir, short_device_node + ".info"
                    )
                    self.ui_manager.display_status(
                        msg1=_("Saving: {file}").format(file=partition_info_filepath),
                        msg2="",
                    )
                    self.logger.write(
                        "Detected partition "
                        + partition_key
                        + " is a Windows NTFS boot reserved partition. Writing "
                        + partition_info_filepath
                    )
                    with open(partition_info_filepath, "w") as filehandle:
                        try:
                            output = "PARTITION_TYPE=Win_boot_reserved\n"
                            filehandle.write(output)
                        except Exception:
                            tb = traceback.format_exc()
                            traceback.print_exc()
                            error_message = (
                                _(
                                    "Failed to write NTFS boot reserve file. Please confirm it is valid to create the provided file path, and try again."
                                )
                                + "\n\n"
                                + tb
                            )
                            self.ui_manager.completed_operation(
                                callable_fn=self.completed_backup,
                                succeeded=False,
                                message=error_message,
                            )
                            return False, error_message

            # Clonezilla uses -q2 priority by default (partclone > partimage > dd).
            # PartImage does not appear to be maintained software, so for simplicity, Rescuezilla is using a
            # partclone > partclone.dd priority
            # [1] https://clonezilla.org/clonezilla-live/doc/01_Save_disk_image/advanced/09-advanced-param.php

            # TODO: Simplify logic
            compression_format = self.compression_dict["format"]
            compression_suffix = Utility.get_compression_suffix(compression_format)
            seekable_enabled = (
                bool(self.compression_dict.get("seekable_index", True))
                and os.environ.get("RESCUEZILLA_SEEKABLE_INDEX", "1") != "0"
                and not self.is_cloning
                and compression_format in ("gzip", "zstd")
            )
            seekidx_path = None
            if seekable_enabled:
                seekidx_path = os.path.join(
                    self.dest_dir,
                    Utility.get_seekable_index_filename(short_device_node),
                )
                compression_cmd_list = Utility.get_seekable_compression_cmd_list(
                    compression_format,
                    self.compression_dict["level"],
                    seekidx_path,
                )
            else:
                compression_cmd_list = Utility.get_compression_cmd_list(
                    compression_format, self.compression_dict["level"]
                )

            split_size = "4GB"
            # Partclone dd blocksize (16MB)
            partclone_dd_bs = "16777216"
            filepath = ""
            # TODO: Re-enable APFS support -- currently partclone Apple Filesystem is not used because it's too unstable [1]
            # [1] https://github.com/rescuezilla/rescuezilla/issues/65
            if (
                shutil.which("partclone." + filesystem) is not None
                and filesystem != "apfs"
            ):
                partclone_cmd_list = (
                    ["partclone." + filesystem]
                    + Utility.get_partclone_rescue_options(self.is_rescue)
                    + [
                        "--logfile",
                        "/var/log/partclone.log",
                        "--clone",
                        "--source",
                        partition_key,
                        "--output",
                        "-",
                    ]
                )
                filepath = os.path.join(
                    self.dest_dir,
                    short_device_node
                    + "."
                    + filesystem
                    + "-ptcl-img."
                    + compression_suffix
                    + ".",
                )
                split_cmd_list = [
                    "split",
                    "--suffix-length=2",
                    "--bytes=" + split_size,
                    "-",
                    filepath,
                ]
            elif shutil.which("partclone.dd") is not None:
                partclone_cmd_list = (
                    ["partclone.dd"]
                    + Utility.get_partclone_rescue_options(self.is_rescue)
                    + [
                        "--buffer_size=" + partclone_dd_bs,
                        "--logfile",
                        "/var/log/partclone.log",
                        "--source",
                        partition_key,
                        "--output",
                        "-",
                    ]
                )
                filepath = os.path.join(
                    self.dest_dir,
                    short_device_node + ".dd-ptcl-img." + compression_suffix + ".",
                )
                split_cmd_list = [
                    "split",
                    "--suffix-length=2",
                    "--bytes=" + split_size,
                    "-",
                    filepath,
                ]
            else:
                failed_message = "Partclone not found."
                self.ui_manager.completed_operation(
                    callable_fn=self.completed_backup,
                    succeeded=False,
                    message=failed_message,
                )
                return False, failed_message

            if self.is_cloning:
                # FIXME: Very ugly design
                # For the cloning case, write a dummy file with the filename that can be scanned by existing Clonezilla
                # image functions
                with open(filepath + "aa", "wb") as filehandle:
                    # Writes some arbitrary data to temp file so Clonezilla image functions will scan as an
                    # uncompressed image.
                    # FIXME: Very ugly design
                    filehandle.write(b"\x0012345678DEADBEEF")
                continue

            if self.requested_stop:
                self.ui_manager.completed_operation(
                    callable_fn=self.completed_backup,
                    succeeded=False,
                    message=_("User requested operation to stop."),
                )
                return False, _("User requested operation to stop.")

            filesystem_backup_message = _(
                "Backup {partition_name} containing filesystem {filesystem} to {destination}"
            ).format(
                partition_name=partition_key,
                filesystem=filesystem,
                destination=filepath,
            )
            self.ui_manager.update_main_statusbar(message=filesystem_backup_message)
            self._log(filesystem_backup_message)

            flat_command_string = Utility.print_cli_friendly(
                "Running ", [partclone_cmd_list, compression_cmd_list, split_cmd_list]
            )
            comp_stderr_file = tempfile.TemporaryFile(mode="w+b")
            split_stderr_file = tempfile.TemporaryFile(mode="w+b")
            p_partclone = None
            p_comp = None
            p_split = None
            try:
                try:
                    p_partclone = subprocess.Popen(
                        partclone_cmd_list,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=env,
                        encoding="utf-8",
                    )
                    p_comp = subprocess.Popen(
                        compression_cmd_list,
                        stdin=p_partclone.stdout,
                        stdout=subprocess.PIPE,
                        stderr=comp_stderr_file,
                        env=env,
                        encoding="utf-8",
                    )
                    # Close parent's copy of p_partclone.stdout immediately so p_partclone
                    # receives SIGPIPE instead of deadlocking if p_comp exits early!
                    p_partclone.stdout.close()
                    p_split = subprocess.Popen(
                        split_cmd_list,
                        stdin=p_comp.stdout,
                        stdout=subprocess.DEVNULL,
                        stderr=split_stderr_file,
                        env=env,
                        encoding="utf-8",
                    )
                    # Close parent's copy of p_comp.stdout immediately so p_comp receives
                    # SIGPIPE instead of deadlocking if p_split exits early (e.g. disk full)!
                    p_comp.stdout.close()
                except Exception:
                    for spawned in (p_split, p_comp, p_partclone):
                        if spawned is not None:
                            try:
                                if spawned.poll() is None:
                                    spawned.terminate()
                                    spawned.wait(timeout=3.0)
                            except Exception:
                                try:
                                    spawned.kill()
                                    spawned.wait(timeout=2.0)
                                except Exception:
                                    pass
                            for pipe in (spawned.stdout, spawned.stderr):
                                if pipe is not None:
                                    try:
                                        pipe.close()
                                    except Exception:
                                        pass
                    raise
                with self._proc_lock:
                    self.proc["partclone_backup_" + partition_key] = p_partclone
                    self.proc["compression_" + partition_key] = p_comp
                    self.proc["split_" + partition_key] = p_split

                partclone_stderr = ""
                # Process partclone output. Partclone outputs an update every 3 seconds, so processing the data
                # on the current thread, for simplicity.
                # Poll process.stdout to show stdout live
                while True:
                    if self.requested_stop:
                        self._terminate_and_wait_partition_procs(partition_key)
                        self._remove_partition_output_files(filepath, seekidx_path)
                        raise _BackupCancelledError()

                    output = p_partclone.stderr.readline()
                    if not output and p_partclone.poll() is not None:
                        break
                    if output:
                        partclone_stderr += output
                        temp_dict = Partclone.parse_partclone_output(output)
                        if "completed" in temp_dict.keys():
                            total_progress_float = Utility.calculate_progress_ratio(
                                current_partition_completed_percentage=temp_dict[
                                    "completed"
                                ]
                                / 100.0,
                                current_partition_bytes=self.partitions_to_backup[
                                    partition_key
                                ]["size"],
                                cumulative_bytes=self.partitions_to_backup[
                                    partition_key
                                ]["cumulative_bytes"],
                                total_bytes=total_size,
                                image_number=partition_number,
                                num_partitions=len(self.partitions_to_backup),
                            )
                            self.ui_manager.update_progress_bar(
                                fraction=total_progress_float
                            )
                        if "remaining" in temp_dict.keys():
                            self.ui_manager.update_progress_status(
                                message=filesystem_backup_message + "\n\n" + output
                            )

                if self.requested_stop:
                    self._terminate_and_wait_partition_procs(partition_key)
                    self._remove_partition_output_files(filepath, seekidx_path)
                    raise _BackupCancelledError()

                if p_partclone.stderr is not None:
                    try:
                        p_partclone.stderr.close()
                    except Exception:
                        pass
                _rc = p_partclone.wait()
                self._log("Partclone exit code: " + str(_rc))

                # Ensure compression (including atomic .rzidx flush) and split finish cleanly
                comp_rc = p_comp.wait()
                split_rc = p_split.wait()

                if self.requested_stop:
                    self._remove_partition_output_files(filepath, seekidx_path)
                    raise _BackupCancelledError()

                comp_stderr_file.seek(0)
                compression_stderr = (
                    comp_stderr_file.read(65536)
                    .decode("utf-8", errors="replace")
                    .strip()
                )
                split_stderr_file.seek(0)
                split_stderr = (
                    split_stderr_file.read(65536)
                    .decode("utf-8", errors="replace")
                    .strip()
                )
            finally:
                try:
                    comp_stderr_file.close()
                except Exception:
                    pass
                try:
                    split_stderr_file.close()
                except Exception:
                    pass

            if p_partclone.returncode != 0 or comp_rc != 0 or split_rc != 0:
                self.failed_partition_count += 1
                partition_summary = (
                    _("<b>Failed to backup partition</b> {partition_name}").format(
                        partition_name=partition_key
                    )
                    + "\n"
                )
                with self.summary_message_lock:
                    self.summary_message += partition_summary
                self.at_least_one_non_fatal_error = True
                extra_info = (
                    "\nThe command used internally was:\n\n"
                    + flat_command_string
                    + "\n\n"
                    + "The output of the command was: "
                    + partclone_stderr
                )
                if compression_stderr:
                    extra_info += (
                        "\n\n"
                        + str(compression_cmd_list)
                        + " stderr: "
                        + compression_stderr
                    )
                    self._log(
                        "Compression stderr ("
                        + partition_key
                        + "): "
                        + compression_stderr
                    )
                if split_stderr:
                    extra_info += (
                        "\n\n" + str(split_cmd_list) + " stderr: " + split_stderr
                    )
                    self._log("Split stderr (" + partition_key + "): " + split_stderr)

                # TODO: Try to backup again, but using partclone.dd
                self.ui_manager.display_error_message(
                    summary_message=partition_summary + extra_info
                )
                # Delete failed partclone files (and any seek index file)
                self._remove_partition_output_files(filepath, seekidx_path)

            else:
                self.succeeded_partition_count += 1
                with self.summary_message_lock:
                    self.summary_message += (
                        _("Successful backup of partition {partition_name}").format(
                            partition_name=partition_key
                        )
                        + "\n"
                    )

        if self.requested_stop:
            self.ui_manager.completed_operation(
                callable_fn=self.completed_backup,
                succeeded=False,
                message=_("User requested operation to stop."),
            )
            return False, _("User requested operation to stop.")

        i += 1
        sleep(1.0)

        """
            partclone_cmd_list = ["partclone", "--logfile", "/tmp/rescuezilla_logfile.txt", "--overwrite", "/dev/"]

              if [ "$fs_p" != "dd" ]; then
    cmd_partclone="partclone.${fs_p} $PARTCLONE_SAVE_OPT -L $partclone_img_info_tmp -c -s $source_dev --output - | $compress_prog_opt"
  else
    # Some parameters for partclone.dd are not required. Here "-c" is not provided by partclone.dd when saving.
    cmd_partclone="partclone.${fs_p} $PARTCLONE_SAVE_OPT --buffer_size ${partclone_dd_bs} -L $partclone_img_info_tmp -s $source_dev --output - | $compress_prog_opt"
  fi
  case "$VOL_LIMIT" in
    [1-9]*)
       # $tgt_dir/${tgt_file}.${fs_pre}-img. is prefix, the last "." is necessary make the output file is like hda1.${fs_pre}-img.aa, hda1.${fs_pre}-img.ab. We do not add -d to make it like hda1.${fs_pre}-img.00, hda1.${fs_pre}-img.01, since it will confuse people that it looks like created by partimage (hda1.${fs_pre}-img.000, hda1.${fs_pre}-img.001)
       cmd_partclone="${cmd_partclone} | split -a $split_suf_len -b ${VOL_LIMIT}MB - $tgt_dir/$(to_filename ${tgt_file}).${fs_pre}-img.${comp_suf}. 2> $split_error"
       ;;
    *)
       cmd_partclone="${cmd_partclone} > $tgt_dir/$(to_filename ${tgt_file}).${fs_pre}-img.${comp_suf} 2> $split_error"
       ;;
  esac
  echo "Run partclone: $cmd_partclone" | tee --append ${OCS_LOGFILE}
  LC_ALL=C eval "(${cmd_partclone} && exit \${PIPESTATUS[0]})"


            cmd_partimage = "partimage $DEFAULT_PARTIMAGE_SAVE_OPT $PARTIMAGE_SAVE_OPT -B gui=no save $source_dev stdout | $compress_prog_opt"
            #case
            #"$VOL_LIMIT" in
            #[1 - 9] *)
            # "$tgt_dir/${tgt_file}." is prefix, the last "." is necessary
            # make the output file is like hda1.aa, hda1.ab.
            # We do not add -d to make it like hda1.00, hda1.01, since it will confuse people that it looks like created by partimage (hda1.000, hda1.001)
            cmd_partimage = "${cmd_partimage} | split -a $split_suf_len -b ${VOL_LIMIT}MB - $tgt_dir/${tgt_file}."
            """

        # Do checksum
        # IMG_ID=$(LC_ALL=C sha512sum $img_dir/clonezilla-img | awk -F" " '{print $1}')" >> $img_dir/Info-img-id.txt

        self.ui_manager.completed_operation(
            callable_fn=self.completed_backup, succeeded=True, message=""
        )
        return True, ""

    # Implementation of Clonezilla "check_if_windows_boot_reserve_part" function
    def is_partition_windows_boot_reserved(
        self, partition_key, mount_point
    ) -> Tuple[bool, str, bool]:
        """
        :param partition_key: Partition to check
        :param mount_point: Location to mount
        :returns: whether the operation was a fatal error, the error message string (empty string if operation was a
                  success), and finally whether the partition is a Windows Boot Reserved partition
        """
        is_windows_reserved = False
        if not os.path.exists(mount_point) and not os.path.isdir(mount_point):
            os.mkdir(mount_point, 0o755)
        Utility.hack_sync_and_sleep()
        process, flat_command_string, failed_message = Utility.run(
            "Mounting NTFS filesystem "
            + partition_key
            + " to check whether filesystem is NTFS boot reserved",
            ["mount", "-o", "ro", partition_key, mount_point],
            use_c_locale=True,
            output_filepath=None,
            logger=self.logger,
        )
        if process.returncode != 0:
            # Not being able to mount the NTFS partition to check if it's NTFS boot reserved is NOT fatal in Clonezilla.
            self.ui_manager.display_error_message(summary_message=failed_message)
            print(
                "Unable to mount NTFS filesystem, assuming *not* Windows boot reserved"
            )
            # Again, not a fatal error so returning true
            return True, failed_message, is_windows_reserved
        Utility.hack_sync_and_sleep()

        boot_dir_path = os.path.join(mount_point, "Boot")
        bootmgr_path = os.path.join(mount_point, "bootmgr")
        bootsect_bak_path = os.path.join(mount_point, "BOOTSECT.BAK")

        if (
            os.path.isdir(boot_dir_path)
            and os.path.exists(bootmgr_path)
            and os.path.exists(bootsect_bak_path)
        ):
            is_windows_reserved = True

        is_unmounted, message = Utility.umount_warn_on_busy(partition_key)
        if not is_unmounted:
            # Being unable to unmount the partition that was just mounted is considered fatal.
            self.logger.write(message)
            return False, message, is_windows_reserved
        return True, "", is_windows_reserved

    def _finalize_clonezilla_img_and_id(self, dest_dir=None, write_img_id=True):
        """Close the active Logger and optionally generate Clonezilla's Info-img-id.txt checksum tag."""
        target_dir = dest_dir if dest_dir is not None else self.dest_dir
        clonezilla_img_filepath = os.path.join(target_dir, "clonezilla-img")
        info_img_id_filepath = os.path.join(target_dir, "Info-img-id.txt")
        if self.logger is not None:
            if write_img_id:
                self.logger.write(
                    "Closing the logfile "
                    + clonezilla_img_filepath
                    + " and generating a tag file for this image: "
                    + info_img_id_filepath
                )
            else:
                self.logger.write("Closing the logfile " + clonezilla_img_filepath)
            self.logger.close()
            self.logger = None

        if write_img_id and os.path.isfile(clonezilla_img_filepath):
            process, flat_command_string, failed_message = Utility.run(
                "Checksumming clonezilla-img file",
                ["sha512sum", clonezilla_img_filepath],
                use_c_locale=True,
                output_filepath=None,
                logger=None,
            )
            if process.returncode != 0:
                self.ui_manager.display_error_message(summary_message=failed_message)
            else:
                sha512sum_output = process.stdout
                split = sha512sum_output.split("  ")
                if len(split) == 2:
                    with open(info_img_id_filepath, "w") as filehandle:
                        filehandle.write(
                            "# This checksum is only for identifying the image. Created by the command: sha512sum clonezilla-img\n"
                        )
                        filehandle.write("IMG_ID=%s" % split[0])
                        filehandle.flush()
                else:
                    self.ui_manager.display_error_message(
                        summary_message="Failed to output checksum file Info-img-id.txt: "
                        + process.stdout
                        + "\n\n"
                        + process.stderr
                    )

    def completed_backup(self, succeeded, message):
        with self.backup_in_progress_lock:
            if self._single_backup_completed:
                return
            self._single_backup_completed = True
        callback_succeeded = succeeded
        try:
            self.ui_manager.update_progress_status(message="")
            self.ui_manager.remove_all_main_statusbar(context_id="backup")
            if not self.is_cloning:
                backup_timeend = datetime.now()
                duration_minutes = Utility.get_human_readable_minutes_seconds(
                    (backup_timeend - self.backup_timestart).total_seconds()
                )
                duration_message = _("Operation took {num_minutes} minutes.").format(
                    num_minutes=duration_minutes
                )
                self.ui_manager.remove_all_main_statusbar(context_id="backup")

                with self.summary_message_lock:
                    if succeeded:
                        if not self.at_least_one_non_fatal_error:
                            self.summary_message = (
                                _("Backup saved successfully.")
                                + "\n\n"
                                + self.summary_message
                                + "\n\n"
                                + message
                                + "\n"
                            )
                        else:
                            self.summary_message = (
                                _("Backup succeeded with some errors:")
                                + "\n\n"
                                + self.summary_message
                                + "\n\n"
                                + message
                                + "\n"
                            )
                    else:
                        heading = _("Backup operation failed:")
                        self.summary_message = (
                            heading
                            + "\n\n"
                            + self.summary_message
                            + "\n\n"
                            + message
                            + "\n"
                        )
                        self.ui_manager.display_error_message(
                            summary_message=self.summary_message, heading=heading
                        )

                    self.summary_message += duration_message + "\n"
                    post_task_action: str = self.ui_manager.get_post_task_action()
                    if post_task_action != "DO_NOTHING":
                        if succeeded:
                            has_scheduled, msg = Utility.schedule_shutdown_reboot(
                                post_task_action
                            )
                            self.summary_message += "\n" + msg
                        else:
                            self.summary_message += "\n" + _(
                                "Shutdown/Reboot cancelled due to errors."
                            )

                # TODO: Populate summary page after umounting /mnt/backup (and closing the logger), so than umount errors
                # TODO: are displayed.
                self.populate_summary_page()

                # Clonezilla writes a file named "Info-img-id.txt" which contains a sha512sum of the clonezilla-img log file.
                self._finalize_clonezilla_img_and_id(write_img_id=bool(succeeded))
            elif self.logger:
                self.logger.close()
                self.logger = None

            if not self.is_cloning:
                is_unmounted, umount_message = Utility.umount_warn_on_busy(
                    "/mnt/backup", is_lazy_umount=True
                )
                if not is_unmounted:
                    if self.logger is not None:
                        self.logger.write(umount_message)
                    else:
                        print(umount_message)
                    with self.summary_message_lock:
                        self.summary_message += umount_message + "\n"
        except Exception:
            callback_succeeded = False
            raise
        finally:
            with self.backup_in_progress_lock:
                self.backup_in_progress = False
            if not self.is_cloning:
                self.completed_backup_callback(callback_succeeded)

    def populate_summary_page(self):
        with self.summary_message_lock:
            if self.logger is not None:
                self.logger.write(
                    "Populating summary page with:\n\n" + self.summary_message
                )
            else:
                print(self.summary_message)
            text_to_display = """<b>{heading}</b>

{message}""".format(
                heading=_("Backup Summary"),
                message=self.ui_manager.escape_text(input=self.summary_message),
            )
        self.ui_manager.display_summary_text(text_to_display=text_to_display)

    def print_cli_friendly(self, message, cmd_list_list):
        self.logger.write(message + ". Running: ", end="")
        for cmd_list in cmd_list_list:
            for cmd in cmd_list:
                self.logger.write(cmd + " ", end="")
            self.logger.write(" | ", end="")
