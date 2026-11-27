# ----------------------------------------------------------------------
#   Copyright (C) 2026 Rescuezilla Seekable-Index Community Fork Contributors
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


if __name__ == "__main__":
    unittest.main()
