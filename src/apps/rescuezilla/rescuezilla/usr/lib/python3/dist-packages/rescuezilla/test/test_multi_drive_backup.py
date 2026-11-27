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


if __name__ == "__main__":
    unittest.main()
