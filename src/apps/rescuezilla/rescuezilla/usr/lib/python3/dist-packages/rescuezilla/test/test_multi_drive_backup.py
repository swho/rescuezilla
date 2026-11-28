# ----------------------------------------------------------------------
#   Copyright (C) 2026 Rescuezilla Seekable-Index Community Fork Contributors
#   Modified 2026-11-28 (Community Fork): os-prober cancel cleanup order,
#   pipeline early-close, telemetry and zstd GUI default tests.
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
import argparse
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from backup_manager import (
    BackupManager,
    _BatchDriveUiProxy,
    compute_backup_dest_dirs,
    normalize_drive_key,
    prepare_cli_backup_jobs,
    sanitize_drive_suffix,
)
from cli.args import parse_arguments
from ui_manager import UiManager


class _RecordingUiManager(UiManager):
    def __init__(self, post_task_action="DO_NOTHING"):
        super().__init__()
        self._post_task_action = post_task_action
        self.progress_values = []
        self.status_messages = []
        self.summary_texts = []
        self.error_messages = []
        self.completed_calls = []

    def update_progress_bar(self, fraction: float):
        self.progress_values.append(fraction)

    def update_progress_status(self, message: str):
        self.status_messages.append(message)

    def display_status(self, msg1: str, msg2: str):
        self.status_messages.append(f"{msg1} | {msg2}")

    def get_post_task_action(self) -> str:
        return self._post_task_action

    def display_error_message(self, summary_message: str, heading: str = ""):
        self.error_messages.append((heading, summary_message))

    def display_summary_text(self, text_to_display):
        self.summary_texts.append(text_to_display)

    def completed_operation(self, callable_fn, succeeded: bool, message: str):
        self.completed_calls.append((succeeded, message))
        callable_fn(succeeded=succeeded, message=message)
        return succeeded, message


class MultiDriveBackupTest(unittest.TestCase):
    def setUp(self):
        # Safety net: NEVER allow any unit test in this class to invoke real host
        # shutdown/reboot or umount commands even if run outside Docker on a host!
        p_shutdown = patch(
            "backup_manager.Utility.schedule_shutdown_reboot",
            side_effect=AssertionError(
                "Unmocked Utility.schedule_shutdown_reboot called in unit test!"
            ),
        )
        p_umount = patch(
            "backup_manager.Utility.umount_warn_on_busy",
            side_effect=AssertionError(
                "Unmocked Utility.umount_warn_on_busy called in unit test!"
            ),
        )
        p_shutdown.start()
        p_umount.start()
        self.addCleanup(p_shutdown.stop)
        self.addCleanup(p_umount.stop)

    def test_sanitize_and_compute_backup_dest_dirs(self):
        self.assertEqual(sanitize_drive_suffix("/dev/nvme0n1"), "nvme0n1")
        self.assertEqual(sanitize_drive_suffix("/dev/sda"), "sda")
        self.assertEqual(
            sanitize_drive_suffix("/dev/mapper/vg0-root"), "mapper_vg0-root"
        )

        # Single drive keeps exact folder name (100% upstream-compatible)
        single = compute_backup_dest_dirs(
            "/mnt/backup/2026-11-27-img-rescuezilla", ["/dev/nvme0n1"]
        )
        self.assertEqual(
            list(single.items()),
            [("/dev/nvme0n1", "/mnt/backup/2026-11-27-img-rescuezilla")],
        )

        # Multi-drive appends -<drive> to each standalone folder
        multi = compute_backup_dest_dirs(
            "/mnt/backup/2026-11-27-img-rescuezilla",
            ["/dev/nvme0n1", "/dev/nvme1n1"],
        )
        self.assertEqual(
            list(multi.items()),
            [
                ("/dev/nvme0n1", "/mnt/backup/2026-11-27-img-rescuezilla-nvme0n1"),
                ("/dev/nvme1n1", "/mnt/backup/2026-11-27-img-rescuezilla-nvme1n1"),
            ],
        )

    def test_batch_ui_proxy_scales_progress_and_prefixes_status(self):
        real_ui = _RecordingUiManager()
        p0 = _BatchDriveUiProxy(
            real_ui, drive_index=0, total_drives=2, drive_key="/dev/nvme0n1"
        )
        p1 = _BatchDriveUiProxy(
            real_ui, drive_index=1, total_drives=2, drive_key="/dev/nvme1n1"
        )

        p0.update_progress_bar(0.5)
        p1.update_progress_bar(0.5)
        self.assertAlmostEqual(real_ui.progress_values[0], 0.25)
        self.assertAlmostEqual(real_ui.progress_values[1], 0.75)

        p0.display_status("Saving", "/dev/nvme0n1p1")
        self.assertTrue(real_ui.status_messages[-1].startswith("[1/2 nvme0n1] Saving"))

        # completed_operation on proxy must NOT invoke real_ui.completed_operation
        p0.completed_operation(lambda **kw: None, True, "")
        self.assertEqual(real_ui.completed_calls, [])
        self.assertTrue(p0.succeeded)

    def test_multi_drive_batch_execution_unmounts_and_shuts_down_once(self):
        real_ui = _RecordingUiManager(post_task_action="SHUTDOWN")
        bm = BackupManager(ui_manager=real_ui, human_readable_version="test-ver")
        completed_flags = []

        with tempfile.TemporaryDirectory() as td:
            dest_dirs = compute_backup_dest_dirs(
                os.path.join(td, "2026-11-27-img-rescuezilla"),
                ["/dev/sda", "/dev/sdb"],
            )
            executed_drives = []

            def fake_do_backup():
                os.makedirs(bm.dest_dir, exist_ok=True)
                with open(os.path.join(bm.dest_dir, "clonezilla-img"), "w") as fh:
                    fh.write(f"fake backup log for {bm.selected_drive_key}\n")
                bm.summary_message = f"Successfully backed up {bm.selected_drive_key}"
                bm.at_least_one_non_fatal_error = False
                bm.ui_manager.update_progress_bar(1.0)
                executed_drives.append((bm.selected_drive_key, bm.dest_dir))
                return bm.ui_manager.completed_operation(
                    callable_fn=bm.completed_backup, succeeded=True, message=""
                )

            with (
                patch.object(bm, "do_backup", side_effect=fake_do_backup),
                patch(
                    "backup_manager.Utility.umount_warn_on_busy",
                    return_value=(True, ""),
                ) as mock_umount,
                patch(
                    "backup_manager.Utility.schedule_shutdown_reboot",
                    return_value=(True, "Scheduled shutdown"),
                ) as mock_shutdown,
            ):
                jobs = [
                    ("/dev/sda", ["all"], dest_dirs["/dev/sda"]),
                    ("/dev/sdb", ["all"], dest_dirs["/dev/sdb"]),
                ]
                bm.start_multi_drive_backup(
                    backup_jobs=jobs,
                    drive_state={},
                    backup_notes="test notes",
                    compression_dict={"format": "gzip", "level": 3},
                    is_rescue=False,
                    completed_backup_callback=lambda ok: completed_flags.append(ok),
                    on_separate_thread=False,
                )

            # Both drives backed up into separate folders with Info-img-id.txt generated per folder
            self.assertEqual(len(executed_drives), 2)
            self.assertEqual(executed_drives[0][0], "/dev/sda")
            self.assertEqual(executed_drives[1][0], "/dev/sdb")
            self.assertTrue(
                os.path.isfile(os.path.join(dest_dirs["/dev/sda"], "Info-img-id.txt"))
            )
            self.assertTrue(
                os.path.isfile(os.path.join(dest_dirs["/dev/sdb"], "Info-img-id.txt"))
            )

            # /mnt/backup unmounted ONCE, shutdown scheduled ONCE, completion callback fired ONCE
            self.assertEqual(mock_umount.call_count, 1)
            self.assertEqual(mock_shutdown.call_count, 1)
            self.assertEqual(completed_flags, [True])
            self.assertFalse(bm.is_backup_in_progress())
            self.assertIn("2/2 drive(s) succeeded", real_ui.summary_texts[-1])

    def test_multi_drive_cancel_stops_remaining_drives_and_skips_shutdown(self):
        real_ui = _RecordingUiManager(post_task_action="SHUTDOWN")
        bm = BackupManager(ui_manager=real_ui, human_readable_version="test-ver")
        completed_flags = []
        executed = []

        with tempfile.TemporaryDirectory() as td:
            dest_dirs = compute_backup_dest_dirs(
                os.path.join(td, "batch-cancel"), ["/dev/sda", "/dev/sdb"]
            )

            def fake_do_backup_cancel():
                executed.append(bm.selected_drive_key)
                bm.cancel_backup()
                bm.summary_message = "Interrupted"
                bm.at_least_one_non_fatal_error = False
                return bm.ui_manager.completed_operation(
                    callable_fn=bm.completed_backup,
                    succeeded=False,
                    message="Operation cancelled by user.",
                )

            with (
                patch.object(bm, "do_backup", side_effect=fake_do_backup_cancel),
                patch(
                    "backup_manager.Utility.umount_warn_on_busy",
                    return_value=(True, ""),
                ) as mock_umount,
                patch(
                    "backup_manager.Utility.schedule_shutdown_reboot"
                ) as mock_shutdown,
            ):
                jobs = [
                    ("/dev/sda", ["all"], dest_dirs["/dev/sda"]),
                    ("/dev/sdb", ["all"], dest_dirs["/dev/sdb"]),
                ]
                bm.start_multi_drive_backup(
                    backup_jobs=jobs,
                    drive_state={},
                    backup_notes="",
                    compression_dict={"format": "gzip", "level": 3},
                    is_rescue=False,
                    completed_backup_callback=lambda ok: completed_flags.append(ok),
                    on_separate_thread=False,
                )

            # Second drive MUST NOT run after user cancels during first drive
            self.assertEqual(executed, ["/dev/sda"])
            self.assertEqual(completed_flags, [False])
            self.assertEqual(mock_umount.call_count, 1)
            self.assertEqual(mock_shutdown.call_count, 0)
            self.assertFalse(bm.is_backup_in_progress())

    def test_cli_args_single_and_multi_source(self):
        parser1 = argparse.ArgumentParser()
        with patch(
            "sys.argv",
            [
                "rescuezillapy",
                "backup",
                "--source",
                "/dev/sda",
                "--destination",
                "/mnt/b/img",
            ],
        ):
            a1 = parse_arguments(parser1)
        self.assertEqual(a1.source, "/dev/sda")

        parser2 = argparse.ArgumentParser()
        with patch(
            "sys.argv",
            [
                "rescuezillapy",
                "backup",
                "--source",
                "/dev/sda",
                "/dev/sdb",
                "--destination",
                "/mnt/b/img",
            ],
        ):
            a2 = parse_arguments(parser2)
        self.assertEqual(a2.source, ["/dev/sda", "/dev/sdb"])

    def test_glade_xml_has_multi_disk_columns_and_handlers(self):
        glade_path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "..",
                "..",
                "..",
                "..",
                "share",
                "rescuezilla",
                "rescuezilla.glade",
            )
        )
        tree = ET.parse(glade_path)
        root = tree.getroot()

        # Verify drive_list has 7 columns (7th is gboolean for backup checkbox)
        drive_list = root.find(".//object[@id='drive_list']")
        self.assertIsNotNone(drive_list)
        cols = [c.attrib["type"] for c in drive_list.findall("./columns/column")]
        self.assertEqual(len(cols), 7)
        self.assertEqual(cols[6], "gboolean")

        # Verify save_partition_list has 4 columns (4th is gchararray parent_drive_key)
        part_list = root.find(".//object[@id='save_partition_list']")
        self.assertIsNotNone(part_list)
        pcols = [c.attrib["type"] for c in part_list.findall("./columns/column")]
        self.assertEqual(len(pcols), 4)
        self.assertEqual(pcols[3], "gchararray")

        # Verify backup_drive_selection_treeview has toggle handler
        tv = root.find(".//object[@id='backup_drive_selection_treeview']")
        self.assertIsNotNone(tv)
        sig_handlers = {s.attrib["handler"] for s in tv.findall(".//signal")}
        self.assertIn("backup_drive_toggled", sig_handlers)
        self.assertIn("row_activated_backup_drive_toggle", sig_handlers)

    def test_cli_duplicate_normalization_and_partitions_validation(self):
        # F7: Duplicate drives (including '/dev/sda' vs 'sda') MUST be rejected
        self.assertEqual(normalize_drive_key("sda"), "/dev/sda")
        self.assertEqual(normalize_drive_key("/dev//sda"), "/dev/sda")
        with self.assertRaises(ValueError):
            compute_backup_dest_dirs("/mnt/x", ["/dev/sda", "/dev/sda"])
        with self.assertRaises(ValueError):
            compute_backup_dest_dirs("/mnt/x", ["/dev/sda", "sda"])

        # F5: Repeated --source AND space-separated --source AND positional forms all work
        for argv, exp_src, exp_dst in [
            (["backup", "/dev/sda", "/mnt/x"], "/dev/sda", "/mnt/x"),
            (["backup", "/dev/sda", "--destination", "/mnt/x"], "/dev/sda", "/mnt/x"),
            (
                ["backup", "--source", "/dev/sda", "--destination", "/mnt/x"],
                "/dev/sda",
                "/mnt/x",
            ),
            (
                [
                    "backup",
                    "--source",
                    "/dev/sda",
                    "--source",
                    "/dev/sdb",
                    "--destination",
                    "/mnt/x",
                ],
                ["/dev/sda", "/dev/sdb"],
                "/mnt/x",
            ),
            (
                [
                    "backup",
                    "--source",
                    "/dev/sda",
                    "/dev/sdb",
                    "--destination",
                    "/mnt/x",
                ],
                ["/dev/sda", "/dev/sdb"],
                "/mnt/x",
            ),
        ]:
            p = argparse.ArgumentParser()
            with patch("sys.argv", ["rescuezillapy"] + argv):
                ns = parse_arguments(p)
            self.assertEqual(ns.source, exp_src)
            self.assertEqual(ns.destination, exp_dst)

        # F6: prepare_cli_backup_jobs properly validates partitions across drives
        fake_drive_state = {
            "/dev/sda": {
                "partitions": {
                    "/dev/sda1": {"filesystem": "ext4", "size": 1000},
                    "/dev/sda2": {"filesystem": "btrfs", "size": 2000},
                }
            },
            "/dev/sdb": {
                "partitions": {
                    "/dev/sdb1": {"filesystem": "vfat", "size": 500},
                }
            },
        }
        # Missing drive for requested partition -> ValueError before mkdir
        with self.assertRaises(ValueError):
            prepare_cli_backup_jobs(
                fake_drive_state,
                ["/dev/sda", "/dev/sdb"],
                ["/dev/sda1"],
                "/mnt/backup/img",
            )
        # Matching partitions on both drives -> valid OrderedDict per drive (no TypeError!)
        with patch(
            "backup_manager.CombinedDriveState.flatten_partition_description",
            return_value="desc",
        ):
            jobs = prepare_cli_backup_jobs(
                fake_drive_state,
                ["sda", "/dev/sdb"],
                ["sda1", "/dev/sdb1"],
                "/mnt/backup/img",
            )
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0][0], "/dev/sda")
        self.assertEqual(list(jobs[0][1].keys()), ["/dev/sda1"])
        self.assertEqual(jobs[0][2], "/mnt/backup/img-sda")
        self.assertEqual(jobs[1][0], "/dev/sdb")
        self.assertEqual(list(jobs[1][1].keys()), ["/dev/sdb1"])
        self.assertEqual(jobs[1][2], "/mnt/backup/img-sdb")

    def test_partition_failure_marks_drive_failed_and_skips_img_id_and_resets_state(
        self,
    ):
        # F3 & F6 & F10: Partition failure MUST mark drive failed, omit Info-img-id.txt,
        # skip shutdown, and reset _is_batch_mode even if populate_summary_page raises.
        real_ui = _RecordingUiManager(post_task_action="SHUTDOWN")
        bm = BackupManager(ui_manager=real_ui, human_readable_version="test-ver")
        completed_flags = []

        with tempfile.TemporaryDirectory() as td:
            dest_dirs = compute_backup_dest_dirs(
                os.path.join(td, "part-fail-[test]"), ["/dev/sda", "/dev/sdb"]
            )

            def fake_do_backup_with_part_fail():
                os.makedirs(bm.dest_dir, exist_ok=True)
                with open(os.path.join(bm.dest_dir, "clonezilla-img"), "w") as fh:
                    fh.write("log\n")
                if bm.selected_drive_key == "/dev/sda":
                    bm.failed_partition_count = 1
                    bm.at_least_one_non_fatal_error = True
                    bm.summary_message = "Failed to backup partition /dev/sda1\n"
                else:
                    bm.succeeded_partition_count = 1
                    bm.failed_partition_count = 0
                    bm.at_least_one_non_fatal_error = False
                    bm.summary_message = "Successful backup of partition /dev/sdb1\n"
                return bm.ui_manager.completed_operation(
                    callable_fn=bm.completed_backup, succeeded=True, message=""
                )

            with (
                patch.object(
                    bm, "do_backup", side_effect=fake_do_backup_with_part_fail
                ),
                patch(
                    "backup_manager.Utility.umount_warn_on_busy",
                    return_value=(True, ""),
                ),
                patch(
                    "backup_manager.Utility.schedule_shutdown_reboot"
                ) as mock_shutdown,
            ):
                jobs = [
                    ("/dev/sda", ["all"], dest_dirs["/dev/sda"]),
                    ("/dev/sdb", ["all"], dest_dirs["/dev/sdb"]),
                ]
                bm.start_multi_drive_backup(
                    backup_jobs=jobs,
                    drive_state={},
                    backup_notes="",
                    compression_dict={"format": "gzip", "level": 3},
                    is_rescue=False,
                    completed_backup_callback=lambda ok: completed_flags.append(ok),
                    on_separate_thread=False,
                )

            # /dev/sda had failed partition -> 1/2 succeeded, 1 failed, overall False, NO shutdown!
            self.assertEqual(completed_flags, [False])
            self.assertEqual(mock_shutdown.call_count, 0)
            self.assertIn("1/2 drive(s) succeeded, 1 failed", real_ui.summary_texts[-1])
            self.assertFalse(
                os.path.isfile(os.path.join(dest_dirs["/dev/sda"], "Info-img-id.txt"))
            )
            self.assertTrue(
                os.path.isfile(os.path.join(dest_dirs["/dev/sdb"], "Info-img-id.txt"))
            )

            # F12: glob.escape removes failed files even when path contains '[' brackets!
            bracket_prefix = os.path.join(
                dest_dirs["/dev/sda"], "sda1.ext4-ptcl-img.gz."
            )
            with open(bracket_prefix + "aa", "wb") as f:
                f.write(b"partial")
            bm._remove_partition_output_files(bracket_prefix)
            self.assertFalse(os.path.exists(bracket_prefix + "aa"))

            # F10 & L5: Even if populate_summary_page raises during batch completion,
            # _is_batch_mode and backup_in_progress MUST be reset in finally AND
            # completed_backup_callback MUST still fire with False so GUI won't hang.
            safe_ui = _RecordingUiManager(post_task_action="DO_NOTHING")
            bm2 = BackupManager(ui_manager=safe_ui, human_readable_version="test-ver")
            bm2._is_batch_mode = True
            bm2.backup_in_progress = True
            bm2.backup_timestart = __import__("datetime").datetime.now()
            bm2.summary_message_lock = __import__("threading").Lock()
            bm2.summary_message = ""
            bm2.at_least_one_non_fatal_error = False
            cb_calls = []
            bm2.completed_backup_callback = lambda ok: cb_calls.append(ok)
            with (
                patch(
                    "backup_manager.Utility.umount_warn_on_busy",
                    return_value=(True, ""),
                ),
                patch(
                    "backup_manager.Utility.schedule_shutdown_reboot",
                    return_value=(True, "Mocked shutdown"),
                ),
                patch.object(
                    bm2, "populate_summary_page", side_effect=RuntimeError("boom")
                ),
            ):
                with self.assertRaises(RuntimeError):
                    bm2._completed_multi_drive_backup(True, "")
            self.assertFalse(bm2._is_batch_mode)
            self.assertFalse(bm2.is_backup_in_progress())
            self.assertEqual(cb_calls, [False])

    def test_cancel_interrupts_metadata_command_immediately(self):
        # H1: Cancelling during a slow metadata command (e.g. smartctl/os-prober)
        # MUST terminate the active subprocess immediately and raise _BackupCancelledError.
        import sys
        import threading
        import time
        from backup_manager import _BackupCancelledError

        real_ui = _RecordingUiManager(post_task_action="DO_NOTHING")
        bm = BackupManager(ui_manager=real_ui, human_readable_version="test-ver")

        def cancel_soon():
            time.sleep(0.15)
            bm.cancel_backup()

        t = threading.Thread(target=cancel_soon, daemon=True)
        t.start()
        t0 = time.monotonic()
        with self.assertRaises(_BackupCancelledError):
            bm._run_cancellable(
                "Simulated slow smartctl",
                [sys.executable, "-c", "import time; time.sleep(30)"],
                use_c_locale=True,
                logger=None,
            )
        elapsed = time.monotonic() - t0
        t.join(timeout=2.0)
        self.assertLess(elapsed, 3.0)
        self.assertTrue(
            any("Stopping" in m for m in real_ui.status_messages),
            real_ui.status_messages,
        )


class _FakeCompletedRun:
    def __init__(self, returncode):
        self.returncode = returncode


class _FakeOsProberProc:
    """Fake os-prober Popen whose communicate() simulates a user cancel mid-run."""

    def __init__(self, bm):
        self._bm = bm
        self.pid = 999999
        self.returncode = 0

    def communicate(self, timeout=None):
        self._bm.requested_stop = True
        return "", ""

    def poll(self):
        return 0


class OsProberCancelCleanupTest(unittest.TestCase):
    """L1: os-prober cancel cleanup must SIGKILL grub-mount BEFORE final umount -l."""

    MNT = "/var/lib/os-prober/mount"

    def _run_cleanup(self, ismount_value, proc_mounts_value, pkill_term_rc=0):
        from backup_manager import _BackupCancelledError

        bm = BackupManager(
            ui_manager=_RecordingUiManager(), human_readable_version="test-ver"
        )
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if cmd[0] == "umount" and "-l" not in cmd:
                return _FakeCompletedRun(32)  # busy
            if cmd[0] == "pkill" and cmd[1] == "-TERM":
                return _FakeCompletedRun(pkill_term_rc)
            return _FakeCompletedRun(0)

        real_exists = os.path.exists
        real_ismount = os.path.ismount

        def fake_exists(p):
            return True if p == self.MNT else real_exists(p)

        def fake_ismount(p):
            return ismount_value if p == self.MNT else real_ismount(p)

        with (
            patch(
                "backup_manager.subprocess.Popen",
                side_effect=lambda *a, **k: _FakeOsProberProc(bm),
            ),
            patch("backup_manager.subprocess.run", side_effect=fake_run),
            patch("backup_manager.os.path.exists", side_effect=fake_exists),
            patch("backup_manager.os.path.ismount", side_effect=fake_ismount),
            patch(
                "backup_manager._is_listed_in_proc_mounts",
                return_value=proc_mounts_value,
            ),
            patch("backup_manager.sleep"),
        ):
            with self.assertRaises(_BackupCancelledError):
                bm._run_cancellable(
                    "Running os-prober", ["os-prober"], use_c_locale=True
                )
        return calls

    def test_persistent_mount_kills_before_final_lazy_umount(self):
        calls = self._run_cleanup(ismount_value=True, proc_mounts_value=True)
        kill_cmd = ["pkill", "-KILL", "-f", "grub-mount.*/var/lib/os-prober/mount"]
        lazy_cmd = ["umount", "-l", self.MNT]
        self.assertIn(kill_cmd, calls)
        # Final two cleanup commands: SIGKILL first, then lazy umount
        self.assertEqual(calls[-2:], [kill_cmd, lazy_cmd])
        self.assertLess(
            calls.index(kill_cmd), len(calls) - 1 - calls[::-1].index(lazy_cmd)
        )
        # Retry loop: synchronous umount attempted before pkill -TERM each round
        self.assertEqual(calls[0], ["umount", self.MNT])
        term_idx = [i for i, c in enumerate(calls) if c[:2] == ["pkill", "-TERM"]]
        self.assertEqual(len(term_idx), 4)

    def test_dead_fuse_mount_still_lazily_unmounted(self):
        # Dead FUSE mount: lstat() -> ENOTCONN so os.path.ismount() is False,
        # but /proc/self/mounts still lists it -> final umount -l MUST still run.
        calls = self._run_cleanup(ismount_value=False, proc_mounts_value=True)
        self.assertEqual(
            calls[-2:],
            [
                ["pkill", "-KILL", "-f", "grub-mount.*/var/lib/os-prober/mount"],
                ["umount", "-l", self.MNT],
            ],
        )

    def test_clean_unmount_skips_final_lazy_umount(self):
        calls = self._run_cleanup(
            ismount_value=False, proc_mounts_value=False, pkill_term_rc=1
        )
        self.assertEqual(
            calls[-1], ["pkill", "-KILL", "-f", "grub-mount.*/var/lib/os-prober/mount"]
        )
        self.assertNotIn(["umount", "-l", self.MNT], calls)

    def test_is_listed_in_proc_mounts(self):
        from unittest.mock import mock_open
        from backup_manager import _is_listed_in_proc_mounts

        if os.path.exists("/proc/self/mounts"):
            self.assertTrue(_is_listed_in_proc_mounts("/"))
        self.assertFalse(_is_listed_in_proc_mounts("/nonexistent/rescuezilla-xyz"))
        fake = (
            "/dev/sda1 / ext4 rw 0 0\n"
            "/dev/sda2 /var/lib/os-prober/mount fuse rw 0 0\n"
            "/dev/sdb1 /media/my\\040disk vfat rw 0 0\n"
        )
        with patch("builtins.open", mock_open(read_data=fake)):
            self.assertTrue(_is_listed_in_proc_mounts(self.MNT))
            self.assertTrue(_is_listed_in_proc_mounts(self.MNT + "/"))
            self.assertTrue(_is_listed_in_proc_mounts("/media/my disk"))
            self.assertFalse(_is_listed_in_proc_mounts("/var/lib/os-prober"))
        with patch("builtins.open", side_effect=OSError("no procfs")):
            self.assertFalse(_is_listed_in_proc_mounts("/"))


class BackupPipelineTest(unittest.TestCase):
    """Early pipe closing (deadlock prevention) and compression telemetry logging."""

    def setUp(self):
        import tempfile as _tf

        self.comp_err = _tf.TemporaryFile(mode="w+b")
        self.split_err = _tf.TemporaryFile(mode="w+b")
        self.addCleanup(self.comp_err.close)
        self.addCleanup(self.split_err.close)

    def _wait_all(self, procs, timeout=20.0):
        import subprocess

        rcs = []
        for p in procs:
            try:
                rcs.append(p.wait(timeout=timeout))
            except subprocess.TimeoutExpired:
                for q in procs:
                    q.kill()
                self.fail("Pipeline deadlocked (process did not exit in time)")
            if p.stderr is not None:
                p.stderr.close()
        return rcs

    def test_parent_closes_intermediate_stdout_immediately_after_downstream_spawn(
        self,
    ):
        from unittest.mock import MagicMock
        from backup_manager import spawn_backup_pipeline

        events = []
        names = iter(["partclone", "comp", "split"])

        def fake_popen(cmd, **kwargs):
            name = next(names)
            events.append(("spawn", name, kwargs.get("stdin")))
            proc = MagicMock(name=name)
            proc.stdout.close.side_effect = lambda n=name: events.append(
                ("close_stdout", n, None)
            )
            return proc

        with patch("backup_manager.subprocess.Popen", side_effect=fake_popen):
            p_pc, p_comp, p_split = spawn_backup_pipeline(
                ["partclone.ext4"], ["comp"], ["split"], {}, self.comp_err, None
            )
        self.assertEqual(
            [(e[0], e[1]) for e in events],
            [
                ("spawn", "partclone"),
                ("spawn", "comp"),
                ("close_stdout", "partclone"),
                ("spawn", "split"),
                ("close_stdout", "comp"),
            ],
        )
        # Downstream stdin wired to upstream stdout
        self.assertIs(events[1][2], p_pc.stdout)
        self.assertIs(events[3][2], p_comp.stdout)
        p_split.stdout.close.assert_not_called()

    def test_downstream_early_exit_does_not_deadlock(self):
        import sys
        from backup_manager import spawn_backup_pipeline

        # Writes forever until EPIPE; os._exit() avoids the exit-time flush on a
        # broken pipe so the exit code is deterministic (141).
        endless_writer_src = "\n".join(
            [
                "import os, sys",
                "b = b'x' * 65536",
                "try:",
                "    while True:",
                "        sys.stdout.buffer.write(b)",
                "except BrokenPipeError:",
                "    os._exit(141)",
            ]
        )
        endless_writer = [sys.executable, "-c", endless_writer_src]
        passthrough = [sys.executable, "-c", endless_writer_src]
        exit_now = [sys.executable, "-c", "import sys; sys.exit(3)"]

        # (a) compressor dies immediately -> partclone gets EPIPE, no hang
        procs = spawn_backup_pipeline(
            endless_writer,
            exit_now,
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
            None,
            self.comp_err,
            self.split_err,
        )
        rcs = self._wait_all(procs)
        self.assertEqual(rcs[1], 3)
        self.assertEqual(rcs[0], 141)
        self.assertEqual(rcs[2], 0)

        # (b) split dies immediately (e.g. disk full) -> compressor gets EPIPE, no hang
        procs = spawn_backup_pipeline(
            [sys.executable, "-c", "pass"],
            passthrough,
            exit_now,
            None,
            self.comp_err,
            self.split_err,
        )
        rcs = self._wait_all(procs)
        self.assertEqual(rcs[2], 3)
        self.assertEqual(rcs[1], 141)

    def test_spawn_failure_terminates_started_processes(self):
        import sys
        from backup_manager import spawn_backup_pipeline

        started = []
        import subprocess as _sp

        real_popen = _sp.Popen

        def tracking_popen(*a, **k):
            p = real_popen(*a, **k)
            started.append(p)
            return p

        with patch("backup_manager.subprocess.Popen", side_effect=tracking_popen):
            with self.assertRaises(FileNotFoundError):
                spawn_backup_pipeline(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    ["/nonexistent/rescuezilla-split-binary"],
                    None,
                    self.comp_err,
                    self.split_err,
                )
        self.assertEqual(len(started), 2)
        for p in started:
            self.assertIsNotNone(p.poll())

    def _make_bm_with_clonezilla_img(self, td):
        from logger import Logger

        bm = BackupManager(
            ui_manager=_RecordingUiManager(), human_readable_version="test-ver"
        )
        log_path = os.path.join(td, "clonezilla-img")
        bm.logger = Logger(log_path)
        self.addCleanup(bm.logger.close)
        return bm, log_path

    def _read_stderr(self, f):
        f.seek(0)
        return f.read(65536).decode("utf-8", errors="replace").strip()

    def test_seekable_compressor_telemetry_logged_into_clonezilla_img(self):
        import shutil
        import sys
        from backup_manager import (
            extract_seekable_compressor_stats,
            spawn_backup_pipeline,
        )
        from utility import Utility

        if not shutil.which("split"):
            self.skipTest("GNU split not installed")
        with tempfile.TemporaryDirectory() as td:
            bm, log_path = self._make_bm_with_clonezilla_img(td)
            idx = os.path.join(td, Utility.get_seekable_index_filename("sda1"))
            comp_cmd = Utility.get_seekable_compression_cmd_list(
                "gzip", 3, idx, print_stats=True
            )
            self.assertEqual(comp_cmd[-1], "--print-stats")
            fake_partclone = [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'partclone data ' * 200000)",
            ]
            split_cmd = [
                "split",
                "--suffix-length=2",
                "--bytes=1048576",
                "-",
                os.path.join(td, "sda1.ext4-ptcl-img.gz."),
            ]
            procs = spawn_backup_pipeline(
                fake_partclone,
                comp_cmd,
                split_cmd,
                None,
                self.comp_err,
                self.split_err,
            )
            rcs = self._wait_all(procs)
            self.assertEqual(rcs, [0, 0, 0])
            comp_stderr = self._read_stderr(self.comp_err)
            self.assertEqual(len(extract_seekable_compressor_stats(comp_stderr)), 1)

            lines = bm._log_compression_telemetry("/dev/sda1", 0, comp_stderr)
            self.assertEqual(len(lines), 1)
            self.assertTrue(lines[0].startswith("[seekable_compressor] fmt=gzip"))
            bm.logger.close()
            with open(log_path) as fh:
                content = fh.read()
            self.assertIn(
                "Compression telemetry (/dev/sda1): [seekable_compressor]", content
            )
            self.assertIn("in_bytes=3000000", content)

    def test_failed_compression_keeps_stderr_and_logs_no_telemetry(self):
        import sys
        from backup_manager import spawn_backup_pipeline

        with tempfile.TemporaryDirectory() as td:
            bm, log_path = self._make_bm_with_clonezilla_img(td)
            failing_comp = [
                sys.executable,
                "-c",
                "import sys; sys.stdin.buffer.read(); "
                "sys.stderr.write('[seekable_compressor] fmt=gzip partial\\n"
                "seekable_compressor error: simulated ENOSPC\\n'); sys.exit(1)",
            ]
            procs = spawn_backup_pipeline(
                [sys.executable, "-c", "print('x' * 1000)"],
                failing_comp,
                [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                None,
                self.comp_err,
                self.split_err,
            )
            rcs = self._wait_all(procs)
            self.assertEqual(rcs[1], 1)
            comp_stderr = self._read_stderr(self.comp_err)
            # Full stderr still captured for the failure report
            self.assertIn("seekable_compressor error: simulated ENOSPC", comp_stderr)
            self.assertEqual(
                bm._log_compression_telemetry("/dev/sda1", 1, comp_stderr), []
            )
            bm.logger.close()
            with open(log_path) as fh:
                self.assertNotIn("Compression telemetry", fh.read())

    def test_strip_seekable_compressor_stats_on_partclone_failure(self):
        from backup_manager import strip_seekable_compressor_stats

        only_stats = (
            "[seekable_compressor] fmt=zstd level=3 backend=libzstd workers=16 "
            "frame_size=4194304 frames=1 in_bytes=0 out_bytes=13 ratio=0.00% "
            "elapsed=0.001s in_MBps=0.00 out_MBps=0.01\n"
        )
        self.assertEqual(strip_seekable_compressor_stats(only_stats), "")
        mixed = only_stats + "real warning line\n"
        self.assertEqual(strip_seekable_compressor_stats(mixed), "real warning line")

    def test_backup_manager_source_wiring(self):
        # The partition loop must request --print-stats, use the pipeline helper,
        # gate telemetry on full pipeline success, and strip telemetry from comp_rc==0 error text.
        import backup_manager

        with open(backup_manager.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("print_stats=True", src)
        self.assertIn("= spawn_backup_pipeline(", src)
        self.assertIn(
            "if p_partclone.returncode == 0 and comp_rc == 0 and split_rc == 0:", src
        )
        self.assertIn("strip_seekable_compressor_stats(compression_stderr)", src)
        tele = src.index("self._log_compression_telemetry(\n")
        fail = src.index("if p_partclone.returncode != 0 or comp_rc != 0")
        self.assertLess(tele, fail)


class HandlerDefaultCompressionTest(unittest.TestCase):
    """P2a: GUI default compression is zstd (level 3), verified without X11/GTK."""

    def test_zstd_is_index0_default_with_level_3(self):
        import ast

        handler_path = os.path.join(os.path.dirname(__file__), "..", "handler.py")
        with open(handler_path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        handler_cls = next(
            n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler"
        )
        funcs = {n.name: n for n in handler_cls.body if isinstance(n, ast.FunctionDef)}

        identity_ns = {"_": lambda s: s}
        rows = []
        set_active_args = []
        # ast.walk() is breadth-first, so sort calls by source position
        calls = sorted(
            (
                n
                for n in ast.walk(funcs["__init__"])
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            ),
            key=lambda n: (n.lineno, n.col_offset),
        )
        for node in calls:
            target = ast.unparse(node.func.value)
            if target == "self.compression_tool_list" and node.func.attr == "append":
                rows.append(eval(ast.unparse(node.args[0]), identity_ns))
            if (
                target == "compression_format_combobox"
                and node.func.attr == "set_active"
            ):
                set_active_args.append(ast.literal_eval(node.args[0]))
        self.assertEqual(
            rows,
            [
                ["zstd", "zstandard (default)"],
                ["gzip", "gzip"],
                ["bzip2", "bzip2"],
                ["uncompressed", "Uncompressed (Suitable for use with Image Explorer)"],
            ],
        )
        self.assertEqual(set_active_args, [0])

        # Execute the real compression_tool_changed() against fake GTK widgets
        mod = ast.Module(body=[funcs["compression_tool_changed"]], type_ignores=[])

        class _FakeGtk:
            class PositionType:
                TOP = "TOP"

        ns = {"_": lambda s: s, "Gtk": _FakeGtk}
        exec(compile(mod, handler_path, "exec"), ns)

        class _Recorder:
            def __init__(self):
                self.calls = []

            def __getattr__(self, name):
                return lambda *a, **k: self.calls.append((name, a, k))

        scale, box = _Recorder(), _Recorder()
        objects = {
            "backup_step6_compression_level_scale": scale,
            "backup_step6_compression_level_selection_box": box,
        }

        class _FakeBuilder:
            def get_object(self, name):
                return objects[name]

        class _FakeHandler:
            builder = _FakeBuilder()

        fake_self = _FakeHandler()

        class _Combo:
            def __init__(self, model):
                self._model = model
                self._active = None

            def set_active(self, i):
                self._active = i

            def get_active_iter(self):
                return self._active

            def get_model(self):
                return self._model

        combo = _Combo(rows)
        combo.set_active(0)
        ns["compression_tool_changed"](fake_self, combo)
        self.assertIn(("set_range", (1, 19), {}), scale.calls)
        self.assertIn(("set_value", (3,), {}), scale.calls)
        self.assertIn(("set_visible", (True,), {}), box.calls)


if __name__ == "__main__":
    unittest.main()
