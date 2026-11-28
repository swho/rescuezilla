# ----------------------------------------------------------------------
#   Copyright (C) 2012 RedoBackup.org
#   Copyright (C) 2003-2025 Steven Shiau <steven _at_ clonezilla org>
#   Copyright (C) 2019-2025 Rescuezilla.com <rescuezilla@gmail.com>
#   Modified 2026-11-27 (Community Fork): Added multi-disk backup checkbox
#   selection, per-drive destination folder naming, and batch backup execution
#   wiring (GPL-3.0-or-later).
#   Modified 2026-11-28 (Community Fork): zstd (level 3) is now the default
#   GUI compression format (GPL-3.0-or-later).
#   Modified 2026-11-28 (Community Fork): SeekZilla differential backup
#   selection (base image discovery, Browse..., confirmation text) and
#   "-img-seekzilla" / "-diff-seekzilla" default folder names
#   (GPL-3.0-or-later).
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
import os
import shutil
import subprocess
import threading
import traceback
from datetime import datetime

from backup_manager import BackupManager, compute_backup_dest_dirs
from gtk_ui_manager import GtkUiManager
from clone_manager import CloneManager
from image_explorer_manager import ImageExplorerManager
from mount_local_path import MountLocalPath
from mount_network_path import MountNetworkPath
from parser.fogproject_image import FogProjectImage
from parser.metadata_only_image import MetadataOnlyImage
from parser.qemu_image import QemuImage
from restore_manager import RestoreManager
from verify_manager import VerifyManager

from partitions_to_restore import PartitionsToRestore
from drive_query import DriveQuery
from image_folder_query import ImageFolderQuery
from parser.sfdisk import Sfdisk
from utility import (
    ErrorMessageModalPopup,
    BrowseSelectionPopup,
    Utility,
    AreYouSureModalPopup,
    _,
)
from wizard_state import (
    Mode,
    Page,
    MOUNT_DIR,
    IMAGE_EXPLORER_DIR,
    NETWORK_UI_WIDGET_MODES,
    RESCUEZILLA_MOUNT_TMP_DIR,
)

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GObject  # noqa: E402

# SeekZilla default backup folder suffixes (full / differential)
SEEKZILLA_FULL_FOLDER_SUFFIX = "-img-seekzilla"
SEEKZILLA_DIFF_FOLDER_SUFFIX = "-diff-seekzilla"


def discover_compatible_base_images(parent_dir, drive_keys, drive_state):
    """Find full SeekZilla images in `parent_dir` usable as a differential base.

    Pure logic (stdlib only, no GTK): scans the immediate subdirectories of
    `parent_dir` for full backups that contain at least one
    `Info-bhash-*.rzbhash` and are NOT differential backups themselves (no
    `Info-rzdiff-manifest.json`).

    - One selected drive: every folder whose `disk` file lists the drive, whose
      name ends with `-<drive>`, or whose `.rzbhash` partitions belong to the
      drive is a candidate.
    - Several selected drives: sibling folders of a multi-drive backup
      (`<name>-<drive>`, e.g. `2026-11-27-1606-img-rescuezilla-nvme0n1` and
      `...-nvme1n1`) are grouped by their common name, or by their timestamp
      prefix / `disk` file when the names were changed.

    Returns a list of dicts (complete matches first, then newest first) with
    keys: label, name, base_dir, base_dirs_by_drive, missing_drives,
    matched_partitions, is_multi_drive, mtime.
    """
    import collections as _collections
    import os as _os
    import re as _re

    manifest_name = "Info-rzdiff-manifest.json"
    bhash_prefix = "Info-bhash-"
    bhash_suffix = ".rzbhash"
    ts_re = _re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{4})")

    def _short_drive(dk):
        short = _re.sub(r"^/dev/", "", str(dk).strip())
        short = _re.sub(r"[^A-Za-z0-9._-]+", "_", short).strip("._-")
        return short or "disk"

    def _short_partition(pk):
        return _re.sub("/", "-", _re.sub(r"^/dev/", "", str(pk).strip()))

    if isinstance(drive_keys, str):
        keys = [drive_keys]
    else:
        keys = list(drive_keys or [])
    if not parent_dir or not keys or not _os.path.isdir(parent_dir):
        return []

    shorts = _collections.OrderedDict((dk, _short_drive(dk)) for dk in keys)
    part_shorts = {}
    for dk in keys:
        parts = {}
        if isinstance(drive_state, dict) and isinstance(drive_state.get(dk), dict):
            parts = drive_state[dk].get("partitions") or {}
        part_shorts[dk] = {_short_partition(pk) for pk in parts}

    try:
        names = sorted(_os.listdir(parent_dir))
    except OSError:
        return []
    folders = []
    for name in names:
        path = _os.path.join(parent_dir, name)
        if not _os.path.isdir(path):
            continue
        try:
            entries = set(_os.listdir(path))
        except OSError:
            continue
        if manifest_name in entries or any(
            e.endswith("-rzdiff.meta") or "-rzdiff-img." in e for e in entries
        ):
            continue  # differential (or partial differential) backups are never used as a base
        bhash_parts = sorted(
            e[len(bhash_prefix) : -len(bhash_suffix)]
            for e in entries
            if e.startswith(bhash_prefix)
            and e.endswith(bhash_suffix)
            and len(e) > len(bhash_prefix) + len(bhash_suffix)
        )
        if not bhash_parts:
            continue
        disks = []
        if "disk" in entries:
            try:
                with open(_os.path.join(path, "disk"), "r", encoding="utf-8", errors="replace") as fh:
                    disks = fh.read().split()
            except OSError:
                disks = []
        try:
            mtime = _os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        folders.append(
            {"name": name, "path": path, "bhash_parts": bhash_parts, "disks": disks, "mtime": mtime}
        )

    def _suffix_match(folder, dk):
        return folder["name"].endswith("-" + shorts[dk])

    def _content_match(folder, dk):
        if shorts[dk] in folder["disks"]:
            return True
        if not folder["disks"]:
            return bool(part_shorts[dk] & set(folder["bhash_parts"]))
        return False

    def _matched_partitions(folder, dk):
        return sorted(part_shorts[dk] & set(folder["bhash_parts"]))

    results = []
    if len(keys) == 1:
        dk = keys[0]
        for folder in folders:
            if not (_suffix_match(folder, dk) or _content_match(folder, dk)):
                continue
            results.append(
                {
                    "label": folder["name"],
                    "name": folder["name"],
                    "base_dir": folder["path"],
                    "base_dirs_by_drive": {dk: folder["path"]},
                    "missing_drives": [],
                    "matched_partitions": {dk: _matched_partitions(folder, dk)},
                    "is_multi_drive": False,
                    "mtime": folder["mtime"],
                }
            )
        results.sort(key=lambda c: (-c["mtime"], c["name"]))
        return results

    # Multi-drive: group sibling folders.
    groups = _collections.OrderedDict()
    used = set()
    for folder in folders:  # pass 1: "<name>-<drive>" siblings
        for dk in keys:
            if _suffix_match(folder, dk):
                gkey = folder["name"][: -(len(shorts[dk]) + 1)]
                group = groups.setdefault(gkey, _collections.OrderedDict())
                if dk not in group:
                    group[dk] = folder
                    used.add(folder["path"])
                break
    for folder in folders:  # pass 2: renamed siblings matched by disk file / partitions
        if folder["path"] in used:
            continue
        for dk in keys:
            if not _content_match(folder, dk):
                continue
            m = ts_re.match(folder["name"])
            ts = m.group(1) if m else None
            target = None
            if ts is not None:
                for gkey, group in groups.items():
                    if gkey.startswith(ts) and dk not in group:
                        target = gkey
                        break
            if target is None:
                target = ts if ts is not None else folder["name"]
            group = groups.setdefault(target, _collections.OrderedDict())
            if dk not in group:
                group[dk] = folder
                used.add(folder["path"])
                break

    total = len(keys)
    for gkey, group in groups.items():
        by_drive = _collections.OrderedDict(
            (dk, group[dk]["path"]) for dk in keys if dk in group
        )
        if not by_drive:
            continue
        missing = [dk for dk in keys if dk not in group]
        results.append(
            {
                "label": "{name} ({found}/{total} drives)".format(
                    name=gkey, found=len(by_drive), total=total
                ),
                "name": gkey,
                "base_dir": next(iter(by_drive.values())),
                "base_dirs_by_drive": dict(by_drive),
                "missing_drives": missing,
                "matched_partitions": {
                    dk: _matched_partitions(group[dk], dk) for dk in by_drive
                },
                "is_multi_drive": True,
                "mtime": max(group[dk]["mtime"] for dk in by_drive),
            }
        )
    results.sort(key=lambda c: (len(c["missing_drives"]), -c["mtime"], c["name"]))
    return results


class Handler:
    """As an event driven application, this handler is where most of the application logic is done.
    The widgets in the GTKBuilder (.glade) XML file are configured with the function names defined in this file (eg,
    when a button is clicked).

    Thread-safety note: All functions (other than the constructor) are expected to be called via the GTK event loop.
    Do long-running operations in a separate Python thread and update the UI by queueing a function to run on the GTK
    event loop up using GLib.idle_add(). Please note, these Python threads created by the threading module are subject
    to the CPython GIL (Global Interpreter Lock) which can be leveraged for some limited threadsafety guarantees.
    If/when the GIL is leveraged for the threadsafety guarantees, this will be clearly commented.

    Documentation/API reference:
    For developers with some knowledge of other UI frameworks but less experience with Python/PyGTK specifically, here is
    a gentle reference/tutorial with good examples: https://python-gtk-3-tutorial.readthedocs.io/en/latest/
    Generic GTK3 API documentation https://developer.gnome.org/gtk3/stable/
    PyGTK-specific API documentation https://developer.gnome.org/pygtk/stable/class-gtknotebook.html
    """

    def __init__(self, builder, human_readable_version: str):
        # The GTKBuilder is used to retrieve any arbitrary GTK widgets using "id" attributes in the GTKBuilder
        # (.glade) XML file
        self.builder = builder
        self.save_partition_list_store = self.builder.get_object("save_partition_list")
        self.drive_list_store = self.builder.get_object("drive_list")
        self.mount_partition_list_store = self.builder.get_object(
            "mount_partition_list"
        )
        self.image_list_store = self.builder.get_object("image_list")
        self.human_readable_version = human_readable_version

        self.has_prior_summary_page = False
        self.main_statusbar = self.builder.get_object("main_statusbar")
        self.mode = Mode.BACKUP
        self.current_page = Page.WELCOME
        self.drive_query = DriveQuery(
            self.builder,
            self.drive_list_store,
            self.save_partition_list_store,
            self.mount_partition_list_store,
        )
        self.image_folder_query = ImageFolderQuery(self.builder, self.image_list_store)

        self.partition_selection_list = self.builder.get_object(
            "partition_selection_list"
        )
        self.backup_image = PartitionsToRestore(self.builder)
        self.image_explorer_partition_selection_list = self.builder.get_object(
            "image_explorer_partition_selection_list"
        )
        self.set_support_information_linkbutton_visible(False)
        self.set_patreon_call_to_action_visible(True)
        # TODO: Remove the need to set this variable to None
        self.selected_drive_key = None
        self.selected_drive_keys = []
        self.active_backup_drive_keys = []
        self.selected_drives_meta = collections.OrderedDict()
        self.partitions_to_backup_by_drive = collections.OrderedDict()
        self.dest_dirs_by_drive = collections.OrderedDict()
        # Ensuring toggle button is ticked and consistency of the user-interface elements that are associated with this
        # checkbox.

        # Initialize perform action option
        self.post_operation_action_list = self.builder.get_object(
            "post_operation_action_list"
        )
        self.post_operation_action_list.append(["DO_NOTHING", _("Do nothing")])
        self.post_operation_action_list.append(["SHUTDOWN", _("Shutdown")])
        self.post_operation_action_list.append(["REBOOT", _("Reboot")])

        # Initialize compression tool option
        self.compression_tool_list = self.builder.get_object("compression_tool_list")
        # Community Fork v1.1: zstd (level 3) is the GUI default (faster & smaller
        # than gzip); gzip remains available for maximum legacy compatibility.
        self.compression_tool_list.append(
            ["zstd", _("zstandard") + " " + "(" + _("default") + ")"]
        )
        self.compression_tool_list.append(["gzip", _("gzip")])
        self.compression_tool_list.append(["bzip2", _("bzip2")])
        self.compression_tool_list.append(
            ["uncompressed", _("Uncompressed (Suitable for use with Image Explorer)")]
        )
        compression_format_combobox = self.builder.get_object(
            "backup_step6_compression_format_combobox"
        )
        compression_format_combobox.set_active(0)
        self.compression_tool_changed(compression_format_combobox)

        # SeekZilla: optional differential backup selection on the compression page
        self._init_differential_backup_ui()

        backup_ui_manager = GtkUiManager(
            builder=builder,
            main_statusbar=self.builder.get_object("main_statusbar"),
            main_statusbar_context_id="backup",
            progress_bars=[self.builder.get_object("backup_progress")],
            progress_statuses=[self.builder.get_object("backup_progress_status")],
            post_task_action_combobox=self.builder.get_object(
                "backup_step8_perform_action_combobox"
            ),
            summary_program_defined_texts=[
                self.builder.get_object("backup_step9_summary_program_defined_text")
            ],
        )
        self.backup_manager = BackupManager(
            ui_manager=backup_ui_manager,
            human_readable_version=self.human_readable_version,
        )
        restore_ui_manager = GtkUiManager(
            builder=builder,
            main_statusbar=self.builder.get_object("main_statusbar"),
            main_statusbar_context_id="restore",
            progress_bars=[
                self.builder.get_object("restore_progress"),
                self.builder.get_object("clone_progress"),
            ],
            progress_statuses=[
                self.builder.get_object("restore_progress_status"),
                self.builder.get_object("clone_progress_status"),
            ],
            post_task_action_combobox=self.builder.get_object(
                "restore_step6_perform_action_combobox"
            ),
            summary_program_defined_texts=[
                self.builder.get_object("restore_step7_summary_program_defined_text")
            ],
        )
        self.restore_manager = RestoreManager(ui_manager=restore_ui_manager)
        verify_ui_manager = GtkUiManager(
            builder=builder,
            main_statusbar=self.builder.get_object("main_statusbar"),
            main_statusbar_context_id="verify",
            progress_bars=[self.builder.get_object("verify_progress")],
            progress_statuses=[self.builder.get_object("verify_progress_status")],
            post_task_action_combobox=None,
            summary_program_defined_texts=[
                self.builder.get_object("verify_step4_summary_program_defined_text")
            ],
        )
        self.verify_manager = VerifyManager(ui_manager=verify_ui_manager)
        clone_ui_manager = GtkUiManager(
            builder=builder,
            main_statusbar=self.builder.get_object("main_statusbar"),
            main_statusbar_context_id="clone",
            progress_bars=[
                self.builder.get_object("restore_progress"),
                self.builder.get_object("clone_progress"),
            ],
            progress_statuses=[
                self.builder.get_object("restore_progress_status"),
                self.builder.get_object("clone_progress_status"),
            ],
            post_task_action_combobox=self.builder.get_object(
                "clone_step6_perform_action_combobox"
            ),
            summary_program_defined_texts=[
                self.builder.get_object("clone_step7_summary_program_defined_text"),
                self.builder.get_object("restore_step7_summary_program_defined_text"),
            ],
        )
        self.clone_manager = CloneManager(
            ui_manager=clone_ui_manager,
            backup_manager=self.backup_manager,
            restore_manager=self.restore_manager,
        )
        # FIXME: Remove need to passing the support info / patreon visibility functions for improved abstraction
        self.image_explorer_manager = ImageExplorerManager(
            builder,
            self.image_explorer_partition_selection_list,
            self.set_support_information_linkbutton_visible,
            self.set_patreon_call_to_action_visible,
        )

        # Add network protocols combobox model
        self.network_share_protocol_list = self.builder.get_object(
            "network_share_protocol_list"
        )
        self.network_share_protocol_list.append(
            ["SMB", _("Windows shared folder (SMB/CIFS, Samba)")]
        )
        # No need to translate "SSH" strings (unless the description string is expanded upon)
        self.network_share_protocol_list.append(["SSH", "SSH"])
        self.network_share_protocol_list.append(["NFS", "NFS"])

        # Manage all network protocol UI widgets
        self.network_protocol_widget_dict = {
            "network_protocol_combobox": {},
            "frame_local": {},
            "frame_network": {},
            "network_use_local_radiobutton": {},
            "network_server_label": {},
            "network_server": {},
            "network_username_label": {},
            "network_username": {},
            "network_remote_path_label": {},
            "network_remote_path": {},
            "network_password_label": {},
            "network_password": {},
            "network_domain_label": {},
            "network_domain": {},
            "network_version_label": {},
            "network_version_combobox": {},
            "network_ssh_idfile": {},
            "network_ssh_idfile_box": {},
            "network_ssh_idfile_label": {},
            "network_port_label": {},
            "network_port": {},
        }
        for mode in NETWORK_UI_WIDGET_MODES:
            for prefix in self.network_protocol_widget_dict.keys():
                id = mode.name.lower() + "_" + prefix
                object = self.builder.get_object(id)
                if object is None:
                    raise ValueError("Could not find: " + id)
                self.network_protocol_widget_dict[prefix][mode] = object

        self.mount_partition_selection_treeselection_id_dict = {
            Mode.BACKUP: "backup_mount_partition_selection_treeselection",
            Mode.RESTORE: "restore_mount_partition_selection_treeselection",
            Mode.VERIFY: "verify_mount_partition_selection_treeselection",
            Mode.IMAGE_EXPLORER: "image_explorer_mount_partition_selection_treeselection",
        }

        self.larger_to_smaller_details_msg = _(
            "The source partition table's final partition ({source}: {source_size} bytes) must refer to a region completely within the destination disk ({destination_size} bytes)."
        )
        self.larger_to_smaller_info_msg = _(
            "Rescuezilla cannot yet automatically shrink partitions to restore from large disks to smaller disks. The final partition currently must always completely reside within the destination disk.\n\nCurrently the only way to restore to disks smaller than original is to first use GParted Partition Editor to shrink the final partition of the original disk before making a new backup image. Please read the following instructions for more information:\n\n{url}"
        ).format(
            url="https://github.com/rescuezilla/rescuezilla/wiki/HOWTO:-Restoring-to-a-smaller-disk.-Eg,-1000GB-HDD-to-500GB-SSD"
        )
        self.source_contains_raid_member_msg = _(
            "Source drive cannot containing RAID member filesystem. The RAID device may be available as as eg, /dev/md0 or /dev/md127."
        )

        self.rescue_checkbutton_dict = {
            Mode.BACKUP: self.builder.get_object("backup_step7_rescue_checkbutton"),
            Mode.RESTORE: self.builder.get_object("restore_step5_rescue_checkbutton"),
            Mode.CLONE: self.builder.get_object("clone_step5_rescue_checkbutton"),
        }

        self.rescue_warning_label_dict = {
            Mode.BACKUP: self.builder.get_object("backup_step7_rescue_warning_label"),
            Mode.RESTORE: self.builder.get_object("restore_step5_rescue_warning_label"),
            Mode.CLONE: self.builder.get_object("clone_step5_rescue_warning_label"),
        }

        self.requested_shutdown_lock = threading.Lock()
        self.requested_shutdown = False
        self.display_welcome_page()

    # Suggest the user read the frequently asked questions, then potentially proceed to the support forum.
    def set_support_information_linkbutton_visible(self, is_visible):
        self.builder.get_object("welcome_support_linkbutton").set_visible(is_visible)
        self.builder.get_object("backup_step9_support_linkbutton").set_visible(
            is_visible
        )
        self.builder.get_object("restore_step7_support_linkbutton").set_visible(
            is_visible
        )
        self.builder.get_object("verify_step4_support_linkbutton").set_visible(
            is_visible
        )
        self.builder.get_object("clone_step7_support_linkbutton").set_visible(
            is_visible
        )
        self.builder.get_object("image_explorer_step3_support_linkbutton").set_visible(
            is_visible
        )

    # Ask users to contribute on the crowdfunding website Patreon.
    def set_patreon_call_to_action_visible(self, is_visible):
        self.builder.get_object("welcome_patreon_linkbutton").set_visible(is_visible)
        self.builder.get_object("backup_step9_patreon_linkbutton").set_visible(
            is_visible
        )
        self.builder.get_object("restore_step7_patreon_linkbutton").set_visible(
            is_visible
        )
        self.builder.get_object("verify_step4_patreon_linkbutton").set_visible(
            is_visible
        )
        self.builder.get_object("clone_step6_patreon_linkbutton").set_visible(
            is_visible
        )
        self.builder.get_object("image_explorer_step3_patreon_linkbutton").set_visible(
            is_visible
        )

    def display_welcome_page(self):
        self.current_page = Page.WELCOME

        self.main_statusbar.push(
            self.main_statusbar.get_context_id("version"), self.human_readable_version
        )
        self.selected_drive_key = None
        self.selected_drive_keys = []
        self.active_backup_drive_keys = []
        self.selected_drives_meta = collections.OrderedDict()
        self.partitions_to_backup_by_drive = collections.OrderedDict()
        self.dest_dirs_by_drive = collections.OrderedDict()

        self.builder.get_object("mode_tabs").set_current_page(0)
        # Disable the forward/previous navigation buttons on the welcome page
        self.builder.get_object("button_next").set_sensitive(False)
        if self.has_prior_summary_page:
            self.builder.get_object("button_back").set_sensitive(True)
        else:
            self.builder.get_object("button_back").set_sensitive(False)

        # TODO: Find more efficient way to do this
        network_protocol_combobox_list = []
        # Re-initialize the image selection network protocol
        for mode in NETWORK_UI_WIDGET_MODES:
            self.network_protocol_widget_dict["network_use_local_radiobutton"][
                mode
            ].set_active(True)
            network_protocol_combobox_list.append(
                self.network_protocol_widget_dict["network_protocol_combobox"][mode]
            )
        # Reset all comboboxes
        for network_protocol_combobox in network_protocol_combobox_list:
            network_protocol_combobox.set_active(0)
            network_protocol_combobox.set_active_iter(
                network_protocol_combobox.get_active_iter()
            )
            self.network_protocol_combobox_changed(network_protocol_combobox)

        # Post action comboboxes don't have signal handlers so no need to trigger anything
        self.builder.get_object("backup_step8_perform_action_combobox").set_active(0)
        self.builder.get_object("restore_step6_perform_action_combobox").set_active(0)
        self.builder.get_object("clone_step6_perform_action_combobox").set_active(0)

    def display_backup_wizard(self, button):
        self.mode = Mode.BACKUP
        self.selected_drive_key = None
        self.selected_drive_keys = []
        self.active_backup_drive_keys = []
        self.selected_drives_meta = collections.OrderedDict()
        self.partitions_to_backup_by_drive = collections.OrderedDict()
        self.dest_dirs_by_drive = collections.OrderedDict()
        self._reset_differential_backup_selection()
        self.drive_list_store.clear()
        self.drive_query.start_query(self._display_error_message_callback)
        self.main_statusbar.pop(self.main_statusbar.get_context_id("version"))
        self.current_page = Page.BACKUP_SOURCE_DRIVE_SELECTION
        self.builder.get_object("mode_tabs").set_current_page(1)
        self.builder.get_object("backup_tabs").set_current_page(0)
        # Re-enable the forward/previous navigation buttons
        self.builder.get_object("button_back").set_sensitive(True)
        self.builder.get_object("button_next").set_sensitive(True)
        # Remove access to any summary pages from prior operations
        self.has_prior_summary_page = False

    def display_restore_wizard(self, button):
        self.mode = Mode.RESTORE
        self.drive_query.start_query(self._display_error_message_callback)
        self.main_statusbar.pop(self.main_statusbar.get_context_id("version"))
        self.current_page = Page.RESTORE_SOURCE_LOCATION_SELECTION
        self.builder.get_object("mode_tabs").set_current_page(2)
        self.builder.get_object("restore_tabs").set_current_page(0)
        # Re-enable the forward/previous navigation buttons
        self.builder.get_object("button_back").set_sensitive(True)
        self.builder.get_object("button_next").set_sensitive(True)
        # Remove access to any summary pages from prior operations
        self.has_prior_summary_page = False

    def display_verify_wizard(self, button):
        self.mode = Mode.VERIFY
        self.drive_query.start_query(self._display_error_message_callback)
        self.main_statusbar.pop(self.main_statusbar.get_context_id("version"))
        self.current_page = Page.VERIFY_SOURCE_LOCATION_SELECTION
        self.builder.get_object("mode_tabs").set_current_page(3)
        self.builder.get_object("verify_tabs").set_current_page(0)
        # Re-enable the forward/previous navigation buttons
        self.builder.get_object("button_back").set_sensitive(True)
        self.builder.get_object("button_next").set_sensitive(True)
        # Remove access to any summary pages from prior operations
        self.has_prior_summary_page = False

    def display_clone_wizard(self, button):
        self.mode = Mode.CLONE
        self.main_statusbar.pop(self.main_statusbar.get_context_id("version"))
        self.builder.get_object("mode_tabs").set_current_page(4)
        self.builder.get_object("clone_tabs").set_current_page(0)
        self.current_page = Page.CLONE_INTRODUCTION
        # Re-enable the forward/previous navigation buttons
        self.builder.get_object("button_back").set_sensitive(True)
        self.builder.get_object("button_next").set_sensitive(True)
        # Remove access to any summary pages from prior operations
        self.has_prior_summary_page = False
        self.builder.get_object("button_mount").set_sensitive(False)
        self.builder.get_object("button_open_file_manager").set_sensitive(False)
        self.image_explorer_manager.set_parts_of_image_explorer_page_sensitive(True)
        self.is_partition_mounted = False

    def display_image_explorer_wizard(self, button):
        self.mode = Mode.IMAGE_EXPLORER
        self.drive_query.start_query(self._display_error_message_callback)
        self.main_statusbar.pop(self.main_statusbar.get_context_id("version"))
        self.builder.get_object("mode_tabs").set_current_page(5)
        self.builder.get_object("image_explorer_tabs").set_current_page(0)
        self.current_page = Page.IMAGE_EXPLORER_SOURCE_LOCATION_SELECTION
        # Re-enable the forward/previous navigation buttons
        self.builder.get_object("button_back").set_sensitive(True)
        self.builder.get_object("button_next").set_sensitive(True)
        # Remove access to any summary pages from prior operations
        self.has_prior_summary_page = False
        self.builder.get_object("button_mount").set_sensitive(False)
        self.builder.get_object("button_open_file_manager").set_sensitive(False)
        self.image_explorer_manager.set_parts_of_image_explorer_page_sensitive(True)
        self.is_partition_mounted = False

    def _display_error_message_callback(self, is_success, message):
        if not is_success:
            ErrorMessageModalPopup(self.builder, message)
            self.display_welcome_page()

    # Convenience function to get the model and iterators using a TreeSelection.
    # In other words, selecting one or more rows from a table.
    #
    # Returns
    # list_store: GTK ListStore
    # iters: List of iterators
    def get_rows(self, id):
        treeselection = self.builder.get_object(id)
        iters = []
        list_store, tree_path_list = treeselection.get_selected_rows()
        if len(list_store) == 0 or len(tree_path_list) == 0:
            return list_store, iters
        for tree_path in tree_path_list:
            iters += [list_store.get_iter(tree_path)]
        return list_store, iters

    def next_tab(self, button):
        try:
            print(
                "Currently mode="
                + str(self.mode)
                + " on page "
                + str(self.current_page)
            )
            if self.mode == Mode.BACKUP:
                if self.current_page == Page.BACKUP_SOURCE_DRIVE_SELECTION:
                    checked_rows = []
                    if self.drive_list_store.get_n_columns() > 6:
                        for row in self.drive_list_store:
                            if row[6]:
                                checked_rows.append((row[0], row[1], row[2], row[3]))
                    # Backwards-compatible fallback: if user didn't tick any checkbox,
                    # accept a highlighted single row just like upstream Rescuezilla.
                    if len(checked_rows) == 0:
                        list_store, iters = self.get_rows(
                            "backup_drive_selection_treeselection"
                        )
                        if len(iters) > 0:
                            checked_rows.append(
                                (
                                    list_store.get(iters[0], 0)[0],
                                    list_store.get(iters[0], 1)[0],
                                    list_store.get(iters[0], 2)[0],
                                    list_store.get(iters[0], 3)[0],
                                )
                            )
                    if len(checked_rows) == 0:
                        ErrorMessageModalPopup(
                            self.builder,
                            "No source drive selected. Please select source "
                            "drive to backup",
                        )
                    else:
                        selected_keys = []
                        selected_meta = collections.OrderedDict()
                        for key, drive_num, drive_cap, drive_model in checked_rows:
                            if self.drive_query.drive_state[key][
                                "has_raid_member_filesystem"
                            ]:
                                ErrorMessageModalPopup(
                                    self.builder,
                                    self.source_contains_raid_member_msg
                                    + f"\n\n({key})",
                                )
                                return
                            elif (
                                "partitions"
                                not in self.drive_query.drive_state[key].keys()
                            ):
                                ErrorMessageModalPopup(
                                    self.builder,
                                    f"({key}) "
                                    + "Backup of drives without a partition table *and* no filesystem is not yet supported by current version of Rescuezilla.\n\nSupport for this will be added in a future version.",
                                )
                                return
                            selected_keys.append(key)
                            selected_meta[key] = {
                                "number": drive_num,
                                "capacity": drive_cap,
                                "model": drive_model,
                            }

                        self.selected_drive_keys = selected_keys
                        self.selected_drives_meta = selected_meta
                        self.selected_drive_key = selected_keys[0]
                        print(
                            "User selected drive(s): "
                            + ", ".join(self.selected_drive_keys)
                        )
                        self.drive_query.populate_partition_selection_table(
                            self.selected_drive_keys
                        )

                        first_meta = selected_meta[self.selected_drive_key]
                        self.selected_drive_enduser_friendly_drive_number = first_meta[
                            "number"
                        ]
                        self.selected_drive_capacity = first_meta["capacity"]
                        # FIXME: May be None for devices like /dev/md127
                        self.selected_drive_model = first_meta["model"]

                        self.current_page = Page.BACKUP_SOURCE_PARTITION_SELECTION
                        self.builder.get_object("backup_tabs").set_current_page(1)
                elif self.current_page == Page.BACKUP_SOURCE_PARTITION_SELECTION:
                    partition_list_store = self.builder.get_object(
                        "save_partition_list"
                    )
                    self.partitions_to_backup_by_drive = collections.OrderedDict()
                    has_atleast_one = False
                    has_parent_col = partition_list_store.get_n_columns() > 3
                    for row in partition_list_store:
                        print("row is " + str(row))
                        if row[1]:
                            parent_drive = (
                                row[3]
                                if has_parent_col and row[3]
                                else self.selected_drive_key
                            )
                            if parent_drive not in self.partitions_to_backup_by_drive:
                                self.partitions_to_backup_by_drive[parent_drive] = (
                                    collections.OrderedDict()
                                )
                            part_dict = self.drive_query.drive_state[parent_drive][
                                "partitions"
                            ][row[0]]
                            part_dict["description"] = row[2]
                            self.partitions_to_backup_by_drive[parent_drive][row[0]] = (
                                part_dict
                            )
                            has_atleast_one = True
                    if not has_atleast_one:
                        ErrorMessageModalPopup(self.builder, "Nothing selected!")
                    else:
                        active_drive_keys = list(
                            self.partitions_to_backup_by_drive.keys()
                        )
                        self.active_backup_drive_keys = active_drive_keys
                        self.selected_drive_key = active_drive_keys[0]
                        self.partitions_to_backup = self.partitions_to_backup_by_drive[
                            self.selected_drive_key
                        ]
                        first_meta = self.selected_drives_meta[self.selected_drive_key]
                        self.selected_drive_enduser_friendly_drive_number = first_meta[
                            "number"
                        ]
                        self.selected_drive_capacity = first_meta["capacity"]
                        self.selected_drive_model = first_meta["model"]
                        self.drive_query.populate_mount_partition_table(
                            ignore_drive_keys=self.active_backup_drive_keys
                        )
                        self.current_page = Page.BACKUP_DESTINATION_LOCATION_SELECTION
                        self.builder.get_object("backup_tabs").set_current_page(2)
                elif self.current_page == Page.BACKUP_DESTINATION_LOCATION_SELECTION:
                    selected_partition_key, self.destination_partition_description = (
                        self.handle_mount_local_or_remote()
                    )
                elif self.current_page == Page.BACKUP_DESTINATION_FOLDER:
                    enduser_date = (
                        datetime.today().strftime("%Y-%m-%d-%H%M")
                        + SEEKZILLA_FULL_FOLDER_SUFFIX
                    )
                    self.builder.get_object("backup_name").set_text(enduser_date)
                    self.current_page = Page.BACKUP_IMAGE_NAME_SELECTION
                    self.builder.get_object("backup_tabs").set_current_page(4)
                elif self.current_page == Page.BACKUP_IMAGE_NAME_SELECTION:
                    selected_directory = self.builder.get_object(
                        "backup_folder_label"
                    ).get_text()
                    folder_name = self.builder.get_object("backup_name").get_text()
                    dest_dir = os.path.join(selected_directory, folder_name)
                    planned_dest_dirs = compute_backup_dest_dirs(
                        dest_dir, self.active_backup_drive_keys
                    )
                    existing_folders = [
                        os.path.basename(d)
                        for d in planned_dest_dirs.values()
                        if os.path.exists(d)
                    ]
                    if existing_folders:
                        # TODO: Find best wording, then add to translation files
                        ErrorMessageModalPopup(
                            self.builder,
                            "There already exists a folder with the same name: {folder}\n\nPlease select a different name for the backup image.".format(
                                folder=", ".join(existing_folders)
                            ),
                        )
                    else:
                        print("User selected: " + str(list(planned_dest_dirs.values())))
                        self.dest_dir = dest_dir
                        self.dest_dirs_by_drive = planned_dest_dirs
                        self.backup_parent_dir = selected_directory
                        self.backup_folder_name = folder_name
                        self.backup_notes = self.builder.get_object(
                            "backup_notes"
                        ).get_text()
                        self.current_page = Page.BACKUP_COMPRESSION_CUSTOMIZATION
                        self.builder.get_object("backup_tabs").set_current_page(5)
                        # Offer the full images in the destination folder as diff bases
                        self.refresh_differential_base_images()
                elif self.current_page == Page.BACKUP_COMPRESSION_CUSTOMIZATION:
                    diff_selection, diff_error = self.get_differential_backup_selection()
                    if diff_error is not None:
                        ErrorMessageModalPopup(self.builder, diff_error)
                    else:
                        self.diff_selection = diff_selection
                        self._apply_backup_mode_folder_name(diff_selection is not None)
                        self.compression_dict = self.get_compression_dict()
                        self.confirm_backup_configuration()
                        self.current_page = Page.BACKUP_CONFIRM_CONFIGURATION
                        self.builder.get_object("backup_tabs").set_current_page(6)
                elif self.current_page == Page.BACKUP_CONFIRM_CONFIGURATION:
                    self.current_page = Page.BACKUP_PROGRESS
                    self.builder.get_object("backup_tabs").set_current_page(7)
                    self.backup_manager.ui_manager.update_progress_bar(fraction=0)
                    is_rescue = self.get_rescue_state()
                    diff_selection = getattr(self, "diff_selection", None)
                    diff_base_dirs_by_drive = None
                    if diff_selection is not None:
                        diff_base_dirs_by_drive = dict(
                            diff_selection.get("base_dirs_by_drive") or {}
                        )
                    if len(self.active_backup_drive_keys) > 1:
                        backup_jobs = [
                            (
                                dk,
                                self.partitions_to_backup_by_drive[dk],
                                self.dest_dirs_by_drive[dk],
                            )
                            for dk in self.active_backup_drive_keys
                        ]
                        self.backup_manager.start_multi_drive_backup(
                            backup_jobs=backup_jobs,
                            drive_state=self.drive_query.drive_state,
                            backup_notes=self.backup_notes,
                            compression_dict=self.compression_dict,
                            is_rescue=is_rescue,
                            completed_backup_callback=self._on_operation_completed_callback,
                            diff_base_dirs_by_drive=diff_base_dirs_by_drive,
                        )
                    else:
                        diff_base_dir = None
                        if diff_selection is not None:
                            diff_base_dir = (diff_base_dirs_by_drive or {}).get(
                                self.selected_drive_key
                            ) or diff_selection.get("base_dir")
                        self.backup_manager.start_backup(
                            self.selected_drive_key,
                            self.partitions_to_backup,
                            self.drive_query.drive_state,
                            self.dest_dirs_by_drive.get(
                                self.selected_drive_key, self.dest_dir
                            ),
                            self.backup_notes,
                            self.compression_dict,
                            is_rescue,
                            self._on_operation_completed_callback,
                            diff_base_dir=diff_base_dir,
                        )
                    # Disable back/next button until the restore completes
                    self.builder.get_object("button_next").set_sensitive(False)
                    self.builder.get_object("button_back").set_sensitive(False)
                    # On success, display the Patreon call-to-action.
                    self.set_patreon_call_to_action_visible(True)
                elif self.current_page == Page.BACKUP_PROGRESS:
                    self.current_page = Page.BACKUP_SUMMARY_SCREEN
                    self.builder.get_object("backup_tabs").set_current_page(8)
                    self.builder.get_object("button_next").set_sensitive(True)
                    self.builder.get_object("button_back").set_sensitive(False)
                elif self.current_page == Page.BACKUP_SUMMARY_SCREEN:
                    self.has_prior_summary_page = True
                    self.display_welcome_page()
                else:
                    print("Unexpected page " + str(self.current_page))
            elif self.mode == Mode.RESTORE:
                if self.current_page == Page.RESTORE_SOURCE_LOCATION_SELECTION:
                    self.image_source_partition_key, description = (
                        self.handle_mount_local_or_remote()
                    )
                elif self.current_page == Page.RESTORE_SOURCE_IMAGE_SELECTION:
                    list_store, iters = self.get_rows(
                        "restore_image_selection_treeselection"
                    )
                    if len(iters) == 0:
                        ErrorMessageModalPopup(self.builder, "No image selected")
                    else:
                        self.selected_image_absolute_path = list_store.get(iters[0], 0)[
                            0
                        ]
                        print("User image: " + self.selected_image_absolute_path)
                        image = self.image_folder_query.image_dict[
                            self.selected_image_absolute_path
                        ]
                        if image.is_needs_decryption:
                            ErrorMessageModalPopup(
                                self.builder,
                                "Ecryptfs encrypted images are not supported by current version of Rescuezilla.\n\nSupport for ecryptfs will be improved in a future version.\n\nHowever, as a temporary workaround, it is possible to carefully use the mount command line utility to decrypt the image, and then point Rescuezilla to this ecryptfs mount point and then use Rescuezilla to restore the image as normal.",
                            )
                        else:
                            if isinstance(image, FogProjectImage):
                                ErrorMessageModalPopup(
                                    self.builder,
                                    "WARNING: Support for restoring images from FOG Project is still EXPERIMENTAL and NOT RECOMMENDED.\n\nWindows MBR and GPT disks will restore and boot fine.\n\nLinux environments using GRUB will restore fine BUT after booting GRUB they won't boot further. It looks like FOG might be modifying unique UUID identifiers etc.\n\nCompatibility with FOG Project images will be improved in a future version.",
                                )
                                # Not returning here to allow user to continue.
                            self.current_page = Page.RESTORE_DESTINATION_DRIVE_SELECTION
                            self.builder.get_object("restore_tabs").set_current_page(2)
                elif self.current_page == Page.RESTORE_DESTINATION_DRIVE_SELECTION:
                    list_store, iters = self.get_rows(
                        "restore_step3_drive_selection_treeselection"
                    )
                    if len(iters) == 0:
                        ErrorMessageModalPopup(
                            self.builder, "Please select destination drive to mount"
                        )
                    else:
                        key = list_store.get(iters[0], 0)[0]
                        if self.drive_query.drive_state[key][
                            "has_raid_member_filesystem"
                        ]:
                            ErrorMessageModalPopup(
                                self.builder,
                                "Warning: Destination drive has a RAID member filesystem. Please make sure you understanding implications of this before continuing.",
                            )
                        self.restore_destination_drive = key
                        if (
                            self.image_source_partition_key is not None
                            and self.image_source_partition_key.startswith(
                                self.restore_destination_drive
                            )
                        ):
                            # TODO: Handle this situation more effectively than "startswith" on the device nodes.
                            ErrorMessageModalPopup(
                                self.builder,
                                "Destination device cannot be the same as source image device.",
                            )
                            return
                        # Set a nice description like "sdc: 8.00 GB (TOSHIBA USB DRV)
                        self.restore_destination_drive_desc = (
                            list_store.get(iters[0], 1)[0]
                            + ": "
                            + list_store.get(iters[0], 2)[0]
                        )
                        drive_model = list_store.get(iters[0], 3)[0]
                        if drive_model is not None:
                            # Some devices, such as RAID devices /dev/md127 don't set the drive model field.
                            self.restore_destination_drive_desc += (
                                " (" + drive_model + ")"
                            )
                        print(
                            "User selected destination drive: "
                            + self.restore_destination_drive
                        )
                        self.selected_image = self.image_folder_query.image_dict[
                            self.selected_image_absolute_path
                        ]
                        drive_dict = self.drive_query.drive_state[
                            self.restore_destination_drive
                        ]
                        # TODO: Compare self.selected_image.size_bytes to drive_dict["capacity"] in bytes
                        try:
                            self.backup_image.initialize_individual_partition_restore_list(
                                self.selected_image,
                                self.restore_destination_drive,
                                self.restore_destination_drive_desc,
                                drive_dict,
                            )
                        except Exception:
                            tb = traceback.format_exc()
                            traceback.print_exc()
                            ErrorMessageModalPopup(
                                self.builder,
                                "Unable to initialize partition restore list:\n\n" + tb,
                            )
                        self.current_page = Page.RESTORE_DESTINATION_PARTITION_SELECTION
                        self.builder.get_object("restore_tabs").set_current_page(3)
                elif self.current_page == Page.RESTORE_DESTINATION_PARTITION_SELECTION:
                    self.is_overwriting_partition_table = self.builder.get_object(
                        "restore_overwrite_partition_table_checkbutton"
                    ).get_active()
                    self.partitions_to_restore = collections.OrderedDict()
                    has_atleast_one = False
                    for row in self.partition_selection_list:
                        print("row is " + str(row))
                        if row[1]:
                            image_key = row[0]
                            self.partitions_to_restore[image_key] = {
                                "description": row[2],
                                "dest_key": row[3],
                                "dest_description": row[4],
                            }
                            print(
                                "Added "
                                + image_key
                                + " "
                                + str(self.partitions_to_restore[image_key])
                            )
                            has_atleast_one = True
                    if not has_atleast_one:
                        ErrorMessageModalPopup(
                            self.builder, "Please select partitions to restore!"
                        )
                    else:
                        last_image_partition_key, last_image_partition_final_byte = (
                            Sfdisk.get_highest_offset_partition(
                                self.selected_image.normalized_sfdisk_dict
                            )
                        )
                        destination_capacity_bytes = self.drive_query.drive_state[
                            self.restore_destination_drive
                        ]["capacity"]
                        # Rough check if restoring to a smaller disk. Note: For GPT disks the secondary GPT backup
                        # should mean the final partition should be a few bytes smaller than the capacity on GPT disks
                        if (
                            self.is_overwriting_partition_table
                            and last_image_partition_final_byte
                            > destination_capacity_bytes
                        ):
                            details = self.larger_to_smaller_details_msg.format(
                                source=last_image_partition_key,
                                source_size=last_image_partition_final_byte,
                                destination_size=destination_capacity_bytes,
                            )
                            ErrorMessageModalPopup(
                                self.builder,
                                details + "\n\n" + self.larger_to_smaller_info_msg,
                            )
                        else:
                            self.confirm_restore_configuration()
                            self.current_page = Page.RESTORE_CONFIRM_CONFIGURATION
                            self.builder.get_object("restore_tabs").set_current_page(4)
                elif self.current_page == Page.RESTORE_CONFIRM_CONFIGURATION:
                    # Disable back/next button until the restore completes
                    self.builder.get_object("button_next").set_sensitive(False)
                    self.builder.get_object("button_back").set_sensitive(False)
                    AreYouSureModalPopup(
                        self.builder,
                        _(
                            "Are you sure you want to restore the backup to {destination_drive}? Doing so will permanently overwrite data on this drive!"
                        ).format(destination_drive=self.restore_destination_drive),
                        self._restore_confirmation_callback,
                    )
                elif self.current_page == Page.RESTORE_PROGRESS:
                    self.current_page = Page.RESTORE_SUMMARY_SCREEN
                    self.builder.get_object("restore_tabs").set_current_page(6)
                    self.builder.get_object("button_back").set_sensitive(False)
                    self.builder.get_object("button_next").set_sensitive(True)
                elif self.current_page == Page.RESTORE_SUMMARY_SCREEN:
                    self.has_prior_summary_page = True
                    self.display_welcome_page()
                else:
                    print("Unexpected page " + str(self.current_page))
            elif self.mode == Mode.VERIFY:
                if self.current_page == Page.VERIFY_SOURCE_LOCATION_SELECTION:
                    image_source_partition_key, description = (
                        self.handle_mount_local_or_remote()
                    )
                elif self.current_page == Page.VERIFY_SOURCE_IMAGE_SELECTION:
                    list_store, iters = self.get_rows(
                        "verify_image_selection_treeselection"
                    )
                    if len(iters) == 0:
                        ErrorMessageModalPopup(self.builder, "No image selected")
                    else:
                        image_list = []
                        is_needs_decryption = False
                        # Gather images for multiple selection case
                        for iterator in iters:
                            image_absolute_path = list_store.get(iterator, 0)[0]
                            print("User image: " + image_absolute_path)
                            image = self.image_folder_query.image_dict[
                                image_absolute_path
                            ]
                            image_list += [image]
                            is_needs_decryption = (
                                is_needs_decryption & image.is_needs_decryption
                            )
                        if is_needs_decryption:
                            ErrorMessageModalPopup(
                                self.builder,
                                "Ecryptfs encrypted images are not supported by current version of Rescuezilla.\n\nSupport for ecryptfs will be improved in a future version.\n\nHowever, as a temporary workaround, it is possible to carefully use the mount command line utility to decrypt the image, and then point Rescuezilla to this ecryptfs mount point and then use Rescuezilla to restore the image as normal.",
                            )
                        else:
                            self.current_page = Page.VERIFY_PROGRESS
                            self.builder.get_object("verify_tabs").set_current_page(2)
                            self.builder.get_object("button_back").set_sensitive(False)
                            self.builder.get_object("button_next").set_sensitive(False)
                            self.verify_manager.start_verify(
                                image_list, self._on_operation_completed_callback
                            )
                elif self.current_page == Page.VERIFY_PROGRESS:
                    self.current_page = Page.VERIFY_SUMMARY_SCREEN
                    self.builder.get_object("verify_tabs").set_current_page(3)
                    self.builder.get_object("button_back").set_sensitive(True)
                    self.builder.get_object("button_next").set_sensitive(True)
                elif self.current_page == Page.VERIFY_SUMMARY_SCREEN:
                    self.has_prior_summary_page = True
                    self.display_welcome_page()
                else:
                    print("Unexpected page " + str(self.current_page))
            elif self.mode == Mode.CLONE:
                if self.current_page == Page.CLONE_INTRODUCTION:
                    self.drive_query.start_query(self._display_error_message_callback)
                    self.current_page = Page.CLONE_SOURCE_DRIVE_SELECTION
                    self.builder.get_object("clone_tabs").set_current_page(1)
                elif self.current_page == Page.CLONE_SOURCE_DRIVE_SELECTION:
                    list_store, iters = self.get_rows(
                        "clone_source_drive_selection_treeselection"
                    )
                    if len(iters) == 0:
                        ErrorMessageModalPopup(
                            self.builder,
                            _(
                                "No source drive selected. Please select source drive to clone"
                            ),
                        )
                    else:
                        # Get first column (which is hidden/invisible) containing the drive shortdevname (eg, 'sda')
                        key = list_store.get(iters[0], 0)[0]
                        if self.drive_query.drive_state[key][
                            "has_raid_member_filesystem"
                        ]:
                            ErrorMessageModalPopup(
                                self.builder,
                                "Backup of RAID member drives is not allowed. Choose another drive such as /dev/md0 or /dev/md127.",
                            )
                            return
                        elif (
                            "partitions" not in self.drive_query.drive_state[key].keys()
                        ):
                            ErrorMessageModalPopup(
                                self.builder,
                                "Clone of drives without a partition table *and* no filesystem is not yet supported by current version of Rescuezilla.\n\nSupport for this will be added in a future version.",
                            )
                            return
                        self.clone_source_drive_key = key
                        print(
                            "User selected source drive: " + self.clone_source_drive_key
                        )
                        self.source_drive_enduser_friendly_drive_number = (
                            list_store.get(iters[0], 1)[0]
                        )
                        self.source_drive_capacity = list_store.get(iters[0], 2)[0]
                        # FIXME: May be None for devices like /dev/md127
                        self.source_drive_model = list_store.get(iters[0], 3)[0]
                        # Make metaimage
                        # FIXME: Do on separate thread, with please wait popup. Like eg, mounting paths
                        print(
                            "Creating MetadataOnlyImage (currently temporarily done on UI thread). This may take a moment..."
                        )
                        self.source_drive_metadata_only_image = MetadataOnlyImage(
                            self.clone_source_drive_key
                        )
                        if len(self.source_drive_metadata_only_image.warning_dict) > 0:
                            error_msg = ""
                            for value in self.source_drive_metadata_only_image.warning_dict.values():
                                error_msg += value + "\n"
                            ErrorMessageModalPopup(
                                self.builder,
                                _("Unable to process {source}:").format(
                                    source=self.clone_source_drive_key
                                )
                                + "\n\n"
                                + error_msg,
                            )
                        else:
                            self.current_page = Page.CLONE_DESTINATION_DRIVE_SELECTION
                            self.builder.get_object("clone_tabs").set_current_page(2)
                elif self.current_page == Page.CLONE_DESTINATION_DRIVE_SELECTION:
                    list_store, iters = self.get_rows(
                        "clone_step3_destination_drive_selection_treeselection"
                    )
                    if len(iters) == 0:
                        ErrorMessageModalPopup(
                            self.builder,
                            _(
                                "No destination drive selected. Please select destination drive to overwrite"
                            ),
                        )
                    else:
                        self.clone_destination_drive = list_store.get(iters[0], 0)[0]
                        if self.clone_source_drive_key == self.clone_destination_drive:
                            ErrorMessageModalPopup(
                                self.builder,
                                "Destination device cannot be the same as source device.",
                            )
                            return
                        # Set a nice description like "sdc: 8.00 GB (TOSHIBA USB DRV)
                        self.clone_destination_drive_desc = (
                            list_store.get(iters[0], 1)[0]
                            + ": "
                            + list_store.get(iters[0], 2)[0]
                        )
                        drive_model = list_store.get(iters[0], 3)[0]
                        if drive_model is not None:
                            # Some devices, such as RAID devices /dev/md127 don't set the drive model field.
                            self.clone_destination_drive_desc += (
                                " (" + drive_model + ")"
                            )
                        print(
                            "User selected destination drive: "
                            + self.clone_destination_drive
                        )
                        drive_dict = self.drive_query.drive_state[
                            self.clone_destination_drive
                        ]
                        # TODO: Compare source / destination size in bytes
                        try:
                            self.backup_image.initialize_individual_partition_restore_list(
                                self.source_drive_metadata_only_image,
                                self.clone_destination_drive,
                                self.clone_destination_drive_desc,
                                drive_dict,
                            )
                        except Exception:
                            tb = traceback.format_exc()
                            traceback.print_exc()
                            ErrorMessageModalPopup(
                                self.builder,
                                "Unable to initialize partition list:\n\n" + tb,
                            )
                            return
                        self.current_page = Page.CLONE_PARTITIONS_TO_CLONE_SELECTION
                        self.builder.get_object("clone_tabs").set_current_page(3)
                elif self.current_page == Page.CLONE_PARTITIONS_TO_CLONE_SELECTION:
                    self.is_overwriting_partition_table = self.builder.get_object(
                        "clone_overwrite_partition_table_checkbutton"
                    ).get_active()
                    self.partitions_to_clone = collections.OrderedDict()
                    has_atleast_one = False
                    for row in self.partition_selection_list:
                        print("row is " + str(row))
                        if row[1]:
                            image_key = row[0]
                            self.partitions_to_clone[image_key] = {
                                "description": row[2],
                                "dest_key": row[3],
                                "dest_description": row[4],
                            }
                            print(
                                "Added "
                                + image_key
                                + " "
                                + str(self.partitions_to_clone[image_key])
                            )
                            has_atleast_one = True
                    if not has_atleast_one:
                        ErrorMessageModalPopup(
                            self.builder, "Please select partitions to clone!"
                        )
                    else:
                        last_image_partition_key, last_image_partition_final_byte = (
                            Sfdisk.get_highest_offset_partition(
                                self.source_drive_metadata_only_image.normalized_sfdisk_dict
                            )
                        )
                        destination_capacity_bytes = self.drive_query.drive_state[
                            self.clone_destination_drive
                        ]["capacity"]
                        # Rough check if restoring to a smaller disk. Note: For GPT disks the secondary GPT backup
                        # should mean the final partition should be a few bytes smaller than the capacity on GPT disks
                        if (
                            self.is_overwriting_partition_table
                            and last_image_partition_final_byte
                            > destination_capacity_bytes
                        ):
                            details = self.larger_to_smaller_details_msg.format(
                                source=last_image_partition_key,
                                source_size=last_image_partition_final_byte,
                                destination_size=destination_capacity_bytes,
                            )
                            ErrorMessageModalPopup(
                                self.builder,
                                details + "\n\n" + self.larger_to_smaller_info_msg,
                            )
                        else:
                            self.confirm_clone_configuration()
                            self.current_page = Page.CLONE_CONFIRM_CONFIGURATION
                            self.builder.get_object("clone_tabs").set_current_page(4)
                elif self.current_page == Page.CLONE_CONFIRM_CONFIGURATION:
                    # Disable back/next button until the clone completes
                    self.builder.get_object("button_next").set_sensitive(False)
                    self.builder.get_object("button_back").set_sensitive(False)
                    AreYouSureModalPopup(
                        self.builder,
                        _(
                            "Are you sure you want to clone the drive to {destination_drive}? Doing so will permanently overwrite data on this drive!"
                        ).format(destination_drive=self.clone_destination_drive),
                        self._clone_confirmation_callback,
                    )
                elif self.current_page == Page.CLONE_PROGRESS:
                    self.current_page = Page.CLONE_SUMMARY_SCREEN
                    self.builder.get_object("clone_tabs").set_current_page(6)
                    self.builder.get_object("button_back").set_sensitive(False)
                    self.builder.get_object("button_next").set_sensitive(True)
                elif self.current_page == Page.CLONE_SUMMARY_SCREEN:
                    self.has_prior_summary_page = True
                    self.display_welcome_page()
                else:
                    print("Unexpected page " + str(self.current_page))
            elif self.mode == Mode.IMAGE_EXPLORER:
                if self.current_page == Page.IMAGE_EXPLORER_SOURCE_LOCATION_SELECTION:
                    image_source_partition_key, description = (
                        self.handle_mount_local_or_remote()
                    )
                elif self.current_page == Page.IMAGE_EXPLORER_SOURCE_IMAGE_SELECTION:
                    list_store, iters = self.get_rows(
                        "image_explorer_image_selection_treeselection"
                    )
                    if len(iters) == 0:
                        ErrorMessageModalPopup(self.builder, "No image selected")
                    else:
                        self.selected_image_absolute_path = list_store.get(iters[0], 0)[
                            0
                        ]
                        print("User image: " + self.selected_image_absolute_path)
                        image = self.image_folder_query.image_dict[
                            self.selected_image_absolute_path
                        ]
                        if image.image_format == "FSARCHIVER_FORMAT":
                            ErrorMessageModalPopup(
                                self.builder,
                                "FSArchiver images cannot be mounted with Image Explorer with the current version of Rescuezilla. But FSArchiver images CAN be restored with the current version of Rescuezilla.\n\nSupport for exploring FSArchiver images may be added in a future version.",
                            )
                        elif image.is_needs_decryption:
                            ErrorMessageModalPopup(
                                self.builder,
                                "Ecryptfs encrypted images are not supported by current version of Rescuezilla.\n\nSupport for ecryptfs will be improved in a future version.\n\nHowever, as a temporary workaround, it is possible to carefully use the mount command line utility to decrypt the image, and then point Rescuezilla to this ecryptfs mount point and then use Rescuezilla to access the image as normal.",
                            )
                        else:
                            self.current_page = Page.IMAGE_EXPLORER_PARTITION_MOUNT
                            self.builder.get_object(
                                "image_explorer_tabs"
                            ).set_current_page(2)
                            # Temporarily disable the next button until the user has successfully mounted a partition.
                            # This indicate pressing 'Next' is NOT the way to mount the partition.
                            self.builder.get_object("button_next").set_sensitive(False)
                            try:
                                self.image_explorer_manager.populate_partition_selection_table(
                                    image
                                )
                            except Exception:
                                tb = traceback.format_exc()
                                traceback.print_exc()
                                ErrorMessageModalPopup(
                                    self.builder, "Unable to process image " + tb
                                )
                elif self.current_page == Page.IMAGE_EXPLORER_PARTITION_MOUNT:
                    self.has_prior_summary_page = True
                    self.display_welcome_page()
                else:
                    print("Unexpected page " + str(self.current_page))
            else:
                print("Unexpected mode " + str(self.mode))
            print(
                " Moving to mode="
                + str(self.mode)
                + " on page "
                + str(self.current_page)
            )
        except Exception:
            tb = traceback.format_exc()
            traceback.print_exc()
            ErrorMessageModalPopup(self.builder, tb)

    def prev_tab(self, button):
        try:
            print(
                "Currently mode="
                + str(self.mode)
                + " on page "
                + str(self.current_page)
            )
            if self.mode == Mode.BACKUP:
                if self.current_page == Page.WELCOME:
                    print("Previous backup summary page")
                    self.builder.get_object("mode_tabs").set_current_page(1)
                    self.builder.get_object("backup_tabs").set_current_page(8)
                    self.current_page = Page.BACKUP_SUMMARY_SCREEN
                    self.builder.get_object("button_back").set_sensitive(False)
                    self.builder.get_object("button_next").set_sensitive(True)
                elif self.current_page == Page.BACKUP_SOURCE_DRIVE_SELECTION:
                    self.current_page = Page.WELCOME
                    self.display_welcome_page()
                elif self.current_page == Page.BACKUP_SOURCE_PARTITION_SELECTION:
                    self.current_page = Page.BACKUP_SOURCE_DRIVE_SELECTION
                    self.builder.get_object("backup_tabs").set_current_page(0)
                elif self.current_page == Page.BACKUP_DESTINATION_LOCATION_SELECTION:
                    self.current_page = Page.BACKUP_SOURCE_PARTITION_SELECTION
                    self.builder.get_object("backup_tabs").set_current_page(1)
                elif self.current_page == Page.BACKUP_DESTINATION_FOLDER:
                    self.current_page = Page.BACKUP_DESTINATION_LOCATION_SELECTION
                    self.builder.get_object("backup_tabs").set_current_page(2)
                elif self.current_page == Page.BACKUP_IMAGE_NAME_SELECTION:
                    self.current_page = Page.BACKUP_DESTINATION_FOLDER
                    self.builder.get_object("backup_tabs").set_current_page(3)
                elif self.current_page == Page.BACKUP_COMPRESSION_CUSTOMIZATION:
                    self.current_page = Page.BACKUP_IMAGE_NAME_SELECTION
                    self.builder.get_object("backup_tabs").set_current_page(4)
                elif self.current_page == Page.BACKUP_CONFIRM_CONFIGURATION:
                    self.current_page = Page.BACKUP_COMPRESSION_CUSTOMIZATION
                    self.builder.get_object("backup_tabs").set_current_page(5)
                elif self.current_page == Page.BACKUP_PROGRESS:
                    self.current_page = Page.BACKUP_CONFIRM_CONFIGURATION
                    self.builder.get_object("backup_tabs").set_current_page(6)
                elif self.current_page == Page.BACKUP_SUMMARY_SCREEN:
                    self.current_page = Page.BACKUP_PROGRESS
                    self.builder.get_object("backup_tabs").set_current_page(7)
                else:
                    print("Unexpected page " + str(self.current_page))
            elif self.mode == Mode.RESTORE:
                if self.current_page == Page.WELCOME:
                    print("Previous restore summary page")
                    self.builder.get_object("mode_tabs").set_current_page(2)
                    self.builder.get_object("restore_tabs").set_current_page(6)
                    self.current_page = Page.RESTORE_SUMMARY_SCREEN
                    self.builder.get_object("button_back").set_sensitive(False)
                    self.builder.get_object("button_next").set_sensitive(True)
                elif self.current_page == Page.RESTORE_SOURCE_LOCATION_SELECTION:
                    self.current_page = Page.WELCOME
                    self.display_welcome_page()
                elif self.current_page == Page.BACKUP_SOURCE_PARTITION_SELECTION:
                    self.builder.get_object("mode_tabs").set_current_page(0)
                elif self.current_page == Page.RESTORE_SOURCE_IMAGE_SELECTION:
                    self.current_page = Page.RESTORE_SOURCE_LOCATION_SELECTION
                    self.builder.get_object("restore_tabs").set_current_page(0)
                elif self.current_page == Page.RESTORE_DESTINATION_DRIVE_SELECTION:
                    self.current_page = Page.RESTORE_SOURCE_IMAGE_SELECTION
                    self.builder.get_object("restore_tabs").set_current_page(1)
                elif self.current_page == Page.RESTORE_DESTINATION_PARTITION_SELECTION:
                    self.current_page = Page.RESTORE_DESTINATION_DRIVE_SELECTION
                    self.builder.get_object("restore_tabs").set_current_page(2)
                elif self.current_page == Page.RESTORE_CONFIRM_CONFIGURATION:
                    self.current_page = Page.RESTORE_DESTINATION_PARTITION_SELECTION
                    self.builder.get_object("restore_tabs").set_current_page(3)
                elif self.current_page == Page.RESTORE_PROGRESS:
                    self.current_page = Page.RESTORE_CONFIRM_CONFIGURATION
                    self.builder.get_object("restore_tabs").set_current_page(4)
                elif self.current_page == Page.RESTORE_SUMMARY_SCREEN:
                    self.current_page = Page.RESTORE_PROGRESS
                    self.builder.get_object("restore_tabs").set_current_page(5)
                else:
                    print("Unexpected page " + str(self.current_page))
            elif self.mode == Mode.VERIFY:
                if self.current_page == Page.WELCOME:
                    print("Previous verify summary page")
                    self.builder.get_object("mode_tabs").set_current_page(3)
                    self.current_page = Page.VERIFY_SUMMARY_SCREEN
                    self.builder.get_object("verify_tabs").set_current_page(3)
                    self.builder.get_object("button_back").set_sensitive(True)
                    self.builder.get_object("button_next").set_sensitive(True)
                elif self.current_page == Page.VERIFY_SOURCE_LOCATION_SELECTION:
                    self.current_page = Page.WELCOME
                    self.display_welcome_page()
                elif self.current_page == Page.VERIFY_SOURCE_IMAGE_SELECTION:
                    self.current_page = Page.VERIFY_SOURCE_LOCATION_SELECTION
                    self.builder.get_object("verify_tabs").set_current_page(0)
                elif self.current_page == Page.VERIFY_PROGRESS:
                    self.current_page = Page.VERIFY_SOURCE_IMAGE_SELECTION
                    self.builder.get_object("verify_tabs").set_current_page(1)
                elif self.current_page == Page.VERIFY_SUMMARY_SCREEN:
                    self.current_page = Page.VERIFY_SOURCE_IMAGE_SELECTION
                    self.builder.get_object("verify_tabs").set_current_page(1)
                else:
                    print("Unexpected page " + str(self.current_page))
            elif self.mode == Mode.CLONE:
                if self.current_page == Page.WELCOME:
                    print("Previous clone summary page")
                    self.builder.get_object("mode_tabs").set_current_page(4)
                    self.builder.get_object("clone_tabs").set_current_page(6)
                    self.current_page = Page.CLONE_SUMMARY_SCREEN
                    self.builder.get_object("button_back").set_sensitive(False)
                    self.builder.get_object("button_next").set_sensitive(True)
                elif self.current_page == Page.CLONE_INTRODUCTION:
                    self.current_page = Page.WELCOME
                    self.display_welcome_page()
                elif self.current_page == Page.CLONE_SOURCE_DRIVE_SELECTION:
                    self.current_page = Page.CLONE_INTRODUCTION
                    self.builder.get_object("clone_tabs").set_current_page(0)
                elif self.current_page == Page.CLONE_DESTINATION_DRIVE_SELECTION:
                    self.current_page = Page.CLONE_SOURCE_DRIVE_SELECTION
                    self.builder.get_object("clone_tabs").set_current_page(1)
                elif self.current_page == Page.CLONE_PARTITIONS_TO_CLONE_SELECTION:
                    self.current_page = Page.CLONE_DESTINATION_DRIVE_SELECTION
                    self.builder.get_object("clone_tabs").set_current_page(2)
                elif self.current_page == Page.CLONE_CONFIRM_CONFIGURATION:
                    self.current_page = Page.CLONE_PARTITIONS_TO_CLONE_SELECTION
                    self.builder.get_object("clone_tabs").set_current_page(3)
                elif self.current_page == Page.CLONE_PROGRESS:
                    self.current_page = Page.CLONE_CONFIRM_CONFIGURATION
                    self.builder.get_object("clone_tabs").set_current_page(4)
                elif self.current_page == Page.CLONE_SUMMARY_SCREEN:
                    self.current_page = Page.CLONE_PROGRESS
                    self.builder.get_object("clone_tabs").set_current_page(5)
                else:
                    print("Unexpected page " + str(self.current_page))
            elif self.mode == Mode.IMAGE_EXPLORER:
                if self.current_page == Page.WELCOME:
                    print("Previous image explorer summary page")
                    self.builder.get_object("mode_tabs").set_current_page(5)
                    self.current_page = Page.IMAGE_EXPLORER_PARTITION_MOUNT
                    self.builder.get_object("image_explorer_tabs").set_current_page(2)
                    self.builder.get_object("button_back").set_sensitive(True)
                    self.builder.get_object("button_next").set_sensitive(True)
                elif self.current_page == Page.IMAGE_EXPLORER_SOURCE_LOCATION_SELECTION:
                    self.current_page = Page.WELCOME
                    self.display_welcome_page()
                elif self.current_page == Page.IMAGE_EXPLORER_SOURCE_IMAGE_SELECTION:
                    self.current_page = Page.IMAGE_EXPLORER_SOURCE_LOCATION_SELECTION
                    self.builder.get_object("image_explorer_tabs").set_current_page(0)
                elif self.current_page == Page.IMAGE_EXPLORER_PARTITION_MOUNT:
                    self.current_page = Page.IMAGE_EXPLORER_SOURCE_IMAGE_SELECTION
                    self.builder.get_object("image_explorer_tabs").set_current_page(1)
                else:
                    print("Unexpected page " + str(self.current_page))
            else:
                print("Unexpected mode " + str(self.mode))
            print(
                "Moving to mode="
                + str(self.mode)
                + " on page "
                + str(self.current_page)
            )
        except Exception:
            tb = traceback.format_exc()
            traceback.print_exc()
            ErrorMessageModalPopup(self.builder, tb)

    def _post_mount_callback(self, is_success, error_message, mounted_path=None):
        if not is_success or mounted_path is None:
            ErrorMessageModalPopup(self.builder, error_message)
        else:
            if self.mode == Mode.BACKUP:
                self.current_page = Page.BACKUP_DESTINATION_FOLDER
                self.builder.get_object("backup_tabs").set_current_page(3)
                self.selected_image_folder(mounted_path, False)
            elif self.mode == Mode.RESTORE:
                self.current_page = Page.RESTORE_SOURCE_IMAGE_SELECTION
                self.builder.get_object("restore_tabs").set_current_page(1)
                self.selected_image_folder(mounted_path, True)
            elif self.mode == Mode.VERIFY:
                self.current_page = Page.VERIFY_SOURCE_IMAGE_SELECTION
                self.builder.get_object("verify_tabs").set_current_page(1)
                self.selected_image_folder(mounted_path, True)
            elif self.mode == Mode.IMAGE_EXPLORER:
                self.current_page = Page.IMAGE_EXPLORER_SOURCE_IMAGE_SELECTION
                self.builder.get_object("image_explorer_tabs").set_current_page(1)
                self.selected_image_folder(mounted_path, True)

    # Called via AreYouSure prompt
    def _restore_confirmation_callback(self, is_affirmative):
        if is_affirmative:
            self.current_page = Page.RESTORE_PROGRESS
            self.builder.get_object("restore_tabs").set_current_page(5)
            is_rescue = self.get_rescue_state()
            if not isinstance(self.selected_image, QemuImage):
                self.restore_manager.ui_manager.update_progress_bar(fraction=0)
                self.restore_manager.start_restore(
                    self.selected_image,
                    self.restore_destination_drive,
                    self.partitions_to_restore,
                    self.is_overwriting_partition_table,
                    is_rescue,
                    self._on_operation_completed_callback,
                )
            else:
                self.restore_manager.ui_manager.update_progress_bar(fraction=0)
                self.clone_manager.start_clone(
                    image=self.selected_image,
                    clone_destination_drive=self.restore_destination_drive,
                    clone_mapping_dict=self.partitions_to_restore,
                    drive_state=self.drive_query.drive_state,
                    is_overwriting_partition_table=self.is_overwriting_partition_table,
                    is_rescue=is_rescue,
                    completed_callback=self._on_operation_completed_callback,
                )
            # Display the Patreon call-to-action.
            self.set_patreon_call_to_action_visible(True)
        else:
            self.builder.get_object("button_back").set_sensitive(True)
            self.builder.get_object("button_next").set_sensitive(True)

    def _clone_confirmation_callback(self, is_affirmative):
        if is_affirmative:
            self.current_page = Page.CLONE_PROGRESS
            self.builder.get_object("clone_tabs").set_current_page(5)
            is_rescue = self.get_rescue_state()
            self.clone_manager.start_clone(
                image=self.source_drive_metadata_only_image,
                clone_destination_drive=self.clone_destination_drive,
                clone_mapping_dict=self.partitions_to_clone,
                drive_state=self.drive_query.drive_state,
                is_overwriting_partition_table=self.is_overwriting_partition_table,
                is_rescue=is_rescue,
                completed_callback=self._on_operation_completed_callback,
            )
            # Display the Patreon call-to-action.
            self.set_patreon_call_to_action_visible(True)
        else:
            self.builder.get_object("button_back").set_sensitive(True)
            self.builder.get_object("button_next").set_sensitive(True)

    def _on_operation_completed_callback(self, is_success):
        if is_success:
            self.set_support_information_linkbutton_visible(False)
            self.set_patreon_call_to_action_visible(True)
        else:
            self.set_support_information_linkbutton_visible(True)
            self.set_patreon_call_to_action_visible(False)
        self.next_tab(None)

    def row_activated_next_tab(self, treeview, path, view_column):
        # Callback for double click (row-activate).
        self.next_tab(None)
        # Not using index = path.get_indices()[0], as the next button could also be used not double-click.

    def backup_drive_toggled(self, cellrendertoggle, path):
        print("Backup drive toggled: " + str(path))
        iterator = self.drive_list_store.get_iter(path)
        state = self.drive_list_store.get(iterator, 6)[0]
        self.drive_list_store.set_value(iterator, 6, not state)

    def row_activated_backup_drive_toggle(self, treeview, path, view_column):
        # If the user double-clicked directly on the checkbox column, the
        # CellRendererToggle 'toggled' signal already handled it; avoid toggling twice.
        if view_column is not None and any(
            isinstance(cell, Gtk.CellRendererToggle) for cell in view_column.get_cells()
        ):
            return
        iterator = self.drive_list_store.get_iter(path)
        state = self.drive_list_store.get(iterator, 6)[0]
        self.drive_list_store.set_value(iterator, 6, not state)

    def save_partition_toggled(self, cellrendertoggle, path):
        print("Backup partition oggled: " + str(path))
        iterator = self.save_partition_list_store.get_iter(path)
        # Get the first column
        state = self.save_partition_list_store.get(iterator, 1)[0]
        # invert toggle
        self.save_partition_list_store.set_value(iterator, 1, not state)

    # Callback for double click (row-activate) on backup mode partitions to backup toggle
    # TODO: Directly call save_partition_toggled from above, to reduce duplication
    def row_activated_backup_partition_toggle(self, treeview, path, view_column):
        list_store, iters = self.get_rows("backup_partitions_treeselection")
        state = list_store.get(iters[0], 1)[0]
        list_store.set_value(iters[0], 1, not state)

    def exit_app(self):
        print("Exiting Rescuezilla.")
        try:
            returncode, failed_message = ImageExplorerManager._do_unmount(
                IMAGE_EXPLORER_DIR, is_deassociate_qemu_nbd_device=False
            )
            if not returncode:
                print(failed_message)

            returncode, failed_message = ImageExplorerManager._do_unmount(
                RESCUEZILLA_MOUNT_TMP_DIR, is_deassociate_qemu_nbd_device=False
            )
            if not returncode:
                print(failed_message)

            # Launch subprocess to unmount target directory. Subprocess is detached so it doesn't block Rescuezilla's
            # shutdown.
            subprocess.Popen(["umount", MOUNT_DIR])
        except Exception:
            traceback.format_exc()
            traceback.print_exc()
        Gtk.main_quit()

    def _cancel_current_operations(self, is_affirmative):
        if is_affirmative:
            with self.requested_shutdown_lock:
                self.requested_shutdown = True
            print("Cancelling current operations.")
            if self.image_folder_query.is_image_folder_query_in_progress():
                self.image_folder_query.cancel_image_folder_query()
            if self.clone_manager.is_clone_in_progress():
                self.clone_manager.cancel_clone()
            if self.restore_manager.is_restore_in_progress():
                self.restore_manager.cancel_restore()
            if self.backup_manager.is_backup_in_progress():
                self.backup_manager.cancel_backup()
            if self.verify_manager.is_verify_in_progress():
                self.verify_manager.cancel_verify()
            if self.image_explorer_manager.is_image_explorer_in_progress():
                self.image_explorer_manager.cancel_image_explorer()

    # Main window receives close signal
    def main_window_delete_event(self, widget, event):
        print("Received Rescuezilla shutdown request")
        has_already_requested_shutdown = False
        with self.requested_shutdown_lock:
            has_already_requested_shutdown = self.requested_shutdown

        if not has_already_requested_shutdown and (
            self.backup_manager.is_backup_in_progress()
            or self.restore_manager.is_restore_in_progress()
            or self.verify_manager.is_verify_in_progress()
            or self.clone_manager.is_clone_in_progress()
        ):
            print("An operation is in progress. Do you wish to cancel?")
            AreYouSureModalPopup(
                self.builder,
                _("An operation is in progress. Do you wish to cancel?"),
                self._cancel_current_operations,
            )
        else:
            self.exit_app()
        # Return True to prevent the window closing from this signal, and instead rely on the dialog box callback.
        # ("True to stop other handlers from being invoked for the event. False to propagate the event further.")
        return True

    def initialize_smb_cifs_version_selection_combobox(self, mode):
        """
        Configure SMB/CIFS network drop-down.
        """
        # Create a new model
        # For some reason, trying to reuse an associated model of combobox doesn't work.
        network_share_protocol_version_list = Gtk.ListStore(str, str)
        # Descriptions adapted from: https://manpages.ubuntu.com/manpages/focal/man8/mount.cifs.8.html
        network_share_protocol_version_list.append(
            [
                "default",
                _("default (Negotiate highest SMB2+ supported by client and server)"),
            ]
        )
        network_share_protocol_version_list.append(
            ["1.0", _("1.0 (The classic CIFS/SMBv1 protocol)")]
        )
        network_share_protocol_version_list.append(
            ["2.0", _("2.0 (Introduced in Vista SP1, Windows Server 2008)")]
        )
        network_share_protocol_version_list.append(
            ["2.1", _("2.1 (Introduced in Windows 7, Windows Server 2008R2)")]
        )
        network_share_protocol_version_list.append(
            ["3", _("3 (The SMBv3.0 protocol version and above)")]
        )
        network_share_protocol_version_list.append(
            ["3.0", _("3.0 (Introduced in Windows 8, Windows Server 2012)")]
        )
        network_share_protocol_version_list.append(
            ["3.0.2", _("3.0.2 (Introduced in Windows 8.1, Windows Server 2012R2)")]
        )
        network_share_protocol_version_list.append(
            ["3.1.1", _("3.1.1 (Introduced in Windows 10, Windows Server 2016)")]
        )
        self.network_protocol_widget_dict["network_version_combobox"][mode].set_model(
            network_share_protocol_version_list
        )
        # Initialize the network version dropdown menu
        self.network_protocol_widget_dict["network_version_combobox"][mode].set_active(
            0
        )

    def initialize_nfs_version_selection_combobox(self, mode):
        """
        Configure SMB/CIFS network drop-down.
        """
        # Create a new model
        # For some reason, trying to reuse an associated model of combobox doesn't work.
        network_share_protocol_version_list = Gtk.ListStore(str, str)
        network_share_protocol_version_list.append(
            [
                "unspecified",
                _(
                    "Unspecified (Tries NFS v4.2 first, then negotiates with server down until finds a supported version)"
                ),
            ]
        )
        network_share_protocol_version_list.append(["3", _("NFSv3")])
        network_share_protocol_version_list.append(["4", _("NFSv4")])
        self.network_protocol_widget_dict["network_version_combobox"][mode].set_model(
            network_share_protocol_version_list
        )
        # Initialize the network version dropdown menu
        self.network_protocol_widget_dict["network_version_combobox"][mode].set_active(
            0
        )

    def network_protocol_combobox_changed(self, combobox):
        # Shows and hides certain fields depending on the protocol.
        # Shows different label text depending on the protocol (eg, "Remote path" for SSH and "Exported path" for NFS).
        # Also whether a field is displayed depends on the network protocol.
        network_protocol_key = Utility.get_combobox_key(combobox)
        if network_protocol_key == "SMB":
            for mode in NETWORK_UI_WIDGET_MODES:
                self.initialize_smb_cifs_version_selection_combobox(mode)
                self.network_protocol_widget_dict["network_server_label"][
                    mode
                ].set_visible(True)
                self.network_protocol_widget_dict["network_server_label"][
                    mode
                ].set_text(_("Share location (UNC path)") + ": ")
                self.network_protocol_widget_dict["network_server"][mode].set_visible(
                    True
                )
                self.network_protocol_widget_dict["network_username_label"][
                    mode
                ].set_visible(True)
                self.network_protocol_widget_dict["network_username_label"][
                    mode
                ].set_text(_("Username"))
                self.network_protocol_widget_dict["network_username"][mode].set_visible(
                    True
                )
                self.network_protocol_widget_dict["network_remote_path_label"][
                    mode
                ].set_visible(False)
                self.network_protocol_widget_dict["network_remote_path"][
                    mode
                ].set_visible(False)
                self.network_protocol_widget_dict["network_password_label"][
                    mode
                ].set_visible(True)
                self.network_protocol_widget_dict["network_password_label"][
                    mode
                ].set_text(_("Password"))
                self.network_protocol_widget_dict["network_password"][mode].set_visible(
                    True
                )
                self.network_protocol_widget_dict["network_domain_label"][
                    mode
                ].set_visible(True)
                self.network_protocol_widget_dict["network_domain_label"][
                    mode
                ].set_text(_("Domain"))
                self.network_protocol_widget_dict["network_domain"][mode].set_visible(
                    True
                )
                self.network_protocol_widget_dict["network_version_label"][
                    mode
                ].set_visible(True)
                self.network_protocol_widget_dict["network_version_label"][
                    mode
                ].set_text(_("Version"))
                self.network_protocol_widget_dict["network_version_combobox"][
                    mode
                ].set_visible(True)
                self.network_protocol_widget_dict["network_ssh_idfile_label"][
                    mode
                ].set_visible(False)
                self.network_protocol_widget_dict["network_ssh_idfile_box"][
                    mode
                ].set_visible(False)
                self.network_protocol_widget_dict["network_port_label"][
                    mode
                ].set_visible(False)
                self.network_protocol_widget_dict["network_port_label"][mode].set_text(
                    _("Port")
                )
                self.network_protocol_widget_dict["network_port"][mode].set_visible(
                    False
                )
        elif network_protocol_key == "SSH":
            for mode in NETWORK_UI_WIDGET_MODES:
                self.network_protocol_widget_dict["network_server_label"][
                    mode
                ].set_visible(True)
                self.network_protocol_widget_dict["network_server_label"][
                    mode
                ].set_text(_("Server") + ": ")
                self.network_protocol_widget_dict["network_server"][mode].set_visible(
                    True
                )
                self.network_protocol_widget_dict["network_username_label"][
                    mode
                ].set_visible(True)
                self.network_protocol_widget_dict["network_username_label"][
                    mode
                ].set_text(_("Username") + ": ")
                self.network_protocol_widget_dict["network_username"][mode].set_visible(
                    True
                )
                self.network_protocol_widget_dict["network_remote_path_label"][
                    mode
                ].set_visible(True)
                self.network_protocol_widget_dict["network_remote_path_label"][
                    mode
                ].set_text(_("Remote path"))
                self.network_protocol_widget_dict["network_remote_path"][
                    mode
                ].set_visible(True)
                self.network_protocol_widget_dict["network_password_label"][
                    mode
                ].set_visible(True)
                self.network_protocol_widget_dict["network_password_label"][
                    mode
                ].set_text(_("Password"))
                self.network_protocol_widget_dict["network_password"][mode].set_visible(
                    True
                )
                self.network_protocol_widget_dict["network_domain_label"][
                    mode
                ].set_visible(False)
                self.network_protocol_widget_dict["network_domain"][mode].set_visible(
                    False
                )
                self.network_protocol_widget_dict["network_version_label"][
                    mode
                ].set_visible(False)
                self.network_protocol_widget_dict["network_version_combobox"][
                    mode
                ].set_visible(False)
                self.network_protocol_widget_dict["network_ssh_idfile_label"][
                    mode
                ].set_visible(True)
                self.network_protocol_widget_dict["network_ssh_idfile_label"][
                    mode
                ].set_text(_("Identity File"))
                self.network_protocol_widget_dict["network_ssh_idfile_box"][
                    mode
                ].set_visible(True)
                self.network_protocol_widget_dict["network_port_label"][
                    mode
                ].set_visible(True)
                self.network_protocol_widget_dict["network_port_label"][mode].set_text(
                    _("Port")
                )
                self.network_protocol_widget_dict["network_port"][mode].set_visible(
                    True
                )
        elif network_protocol_key == "NFS":
            for mode in NETWORK_UI_WIDGET_MODES:
                self.initialize_nfs_version_selection_combobox(mode)
                self.network_protocol_widget_dict["network_server_label"][
                    mode
                ].set_visible(True)
                self.network_protocol_widget_dict["network_server_label"][
                    mode
                ].set_text(_("Server") + ": ")
                self.network_protocol_widget_dict["network_server"][mode].set_visible(
                    True
                )
                self.network_protocol_widget_dict["network_username_label"][
                    mode
                ].set_visible(False)
                self.network_protocol_widget_dict["network_username"][mode].set_visible(
                    False
                )
                self.network_protocol_widget_dict["network_remote_path_label"][
                    mode
                ].set_visible(True)
                self.network_protocol_widget_dict["network_remote_path_label"][
                    mode
                ].set_text(_("Exported path") + ": ")
                self.network_protocol_widget_dict["network_remote_path"][
                    mode
                ].set_visible(True)
                self.network_protocol_widget_dict["network_password_label"][
                    mode
                ].set_visible(False)
                self.network_protocol_widget_dict["network_password"][mode].set_visible(
                    False
                )
                self.network_protocol_widget_dict["network_domain_label"][
                    mode
                ].set_visible(False)
                self.network_protocol_widget_dict["network_domain"][mode].set_visible(
                    False
                )
                self.network_protocol_widget_dict["network_version_label"][
                    mode
                ].set_visible(True)
                self.network_protocol_widget_dict["network_version_label"][
                    mode
                ].set_text(_("Version"))
                self.network_protocol_widget_dict["network_version_combobox"][
                    mode
                ].set_visible(True)
                self.network_protocol_widget_dict["network_ssh_idfile_label"][
                    mode
                ].set_visible(False)
                self.network_protocol_widget_dict["network_ssh_idfile_box"][
                    mode
                ].set_visible(False)
                self.network_protocol_widget_dict["network_port_label"][
                    mode
                ].set_visible(False)
                self.network_protocol_widget_dict["network_port_label"][mode].set_text(
                    _("Port")
                )
                self.network_protocol_widget_dict["network_port"][mode].set_visible(
                    False
                )
        else:
            raise ValueError("Unknown network protocol")
        return

    def select_ssh_idfile(self, button):
        BrowseSelectionPopup(
            self.builder,
            callback=self.selected_ssh_idfile,
            default_directory="/",
            is_allow_selecting_folder_outside_mount=True,
            select_file=True,
        )
        return

    def selected_ssh_idfile(self, text, is_allow_selecting_folder_outside_mount):
        print("Received SSH ID file path" + text)
        for mode in NETWORK_UI_WIDGET_MODES:
            self.network_protocol_widget_dict["network_ssh_idfile"][mode].set_text(text)

    def compression_tool_changed(self, combobox):
        compression_level_scale = self.builder.get_object(
            "backup_step6_compression_level_scale"
        )
        # "The step size is used when the user clicks the gtk.Scrollbar arrows or moves gtk.Scale via the arrow keys.
        # The page size is used for example when moving via Page Up or Page Down keys."
        compression_level_scale.set_increments(step=1, page=5)

        compression_level_box = self.builder.get_object(
            "backup_step6_compression_level_selection_box"
        )
        tree_iter = combobox.get_active_iter()
        range = (0, 0)
        default_value = 0
        if tree_iter is not None:
            model = combobox.get_model()
            (compression_key,) = model[tree_iter][:1]
            if compression_key == "uncompressed":
                compression_level_box.set_visible(False)

            elif compression_key == "gzip":
                compression_level_box.set_visible(True)
                # Like gzip binary, the pigz multithreaded gzip arguments typically takes compression levels 1-9,
                # though pigz can actually take 0 (no compression) and 11 ("-11 gives a few percent better
                # compression at a severe cost in execution time").
                range = (0, 9)
                # Default compression for pigz is 6
                default_value = 6
            elif compression_key == "zstd":
                compression_level_box.set_visible(True)
                # As with zstd command-line utility, hiding high compression levels 20+ (maximum 22) by default as it
                # "a lot more memory. Note that decompression will also require more memory when using these levels"
                range = (1, 19)
                # Default compression for zstd is 3
                default_value = 3
            elif compression_key == "bzip2":
                compression_level_box.set_visible(True)
                range = (1, 9)
                # Default compression for bzip2 is 9
                default_value = 9

        compression_level_scale.clear_marks()
        compression_level_scale.set_range(range[0], range[1])
        compression_level_scale.set_value(default_value)
        compression_level_scale.add_mark(range[0], Gtk.PositionType.TOP, _("Fastest"))
        compression_level_scale.add_mark(range[1], Gtk.PositionType.TOP, _("Best"))
        return

    def get_compression_dict(self):
        compression_dict = {}

        compression_level_combobox = self.builder.get_object(
            "backup_step6_compression_format_combobox"
        )
        tree_iter = compression_level_combobox.get_active_iter()
        compression_key = ""
        if tree_iter is not None:
            model = compression_level_combobox.get_model()
            (compression_key,) = model[tree_iter][:1]

        if compression_key == "":
            raise ValueError("Compression not specified")

        compression_level_scale = self.builder.get_object(
            "backup_step6_compression_level_scale"
        )
        level = int(compression_level_scale.get_value())

        compression_dict = {"format": compression_key, "level": level}

        return compression_dict

    def get_rescue_state(self):
        return self.rescue_checkbutton_dict[self.mode].get_active()

    def rescue_toggle(self, toggle_button):
        self.rescue_warning_label_dict[self.mode].set_visible(
            toggle_button.get_active()
        )

    # GtkToggleButton handler for switching the image location selection between local folder, and network share.
    def image_location_toggle(self, toggle_button):
        is_local_active = self.network_protocol_widget_dict[
            "network_use_local_radiobutton"
        ][self.mode].get_active()
        for mode in NETWORK_UI_WIDGET_MODES:
            self.network_protocol_widget_dict["frame_local"][mode].set_visible(
                is_local_active
            )
            self.network_protocol_widget_dict["frame_network"][mode].set_visible(
                not is_local_active
            )
        return

    def handle_mount_local_or_remote(self):
        selected_partition_key = None
        partition_description = None
        if self.network_protocol_widget_dict["network_use_local_radiobutton"][
            self.mode
        ].get_active():
            list_store, iters = self.get_rows(
                self.mount_partition_selection_treeselection_id_dict[self.mode]
            )
            if len(iters) == 0:
                ErrorMessageModalPopup(self.builder, "Please select drive to mount")
            else:
                # Get first column (which is hidden/invisible) containing the drive shortdevname (eg, 'sda')
                selected_partition_key = list_store.get(iters[0], 0)[0]
                partition_description = list_store.get(iters[0], 3)[0]
                print("User selected partition: " + selected_partition_key)
                # Callback determines whether wizard proceeds
                MountLocalPath(
                    self.builder,
                    self._post_mount_callback,
                    selected_partition_key,
                    MOUNT_DIR,
                )
        else:
            partition_description = "network share"
            MountNetworkPath(
                self.builder,
                self._post_mount_callback,
                self.mode,
                self.network_protocol_widget_dict,
                MOUNT_DIR,
            )
        return selected_partition_key, partition_description

    def restore_partition_toggled(self, cell_render_toggle, path):
        iterator = self.partition_selection_list.get_iter(path)
        new_state = not self.partition_selection_list.get(iterator, 1)[0]
        self.backup_image.toggle_restore_of_row(iterator, new_state)

    # Same function for restore case, but with clear function name.
    # TODO: Rename the original function to remove need for this alias.
    def clone_partition_toggled(self, cell_render_toggle, path):
        self.restore_partition_toggled(cell_render_toggle, path)

    # Callback for double click (row-activate) on restore partition mapping toggle
    # TODO: Directly call restore_partition_toggled from above, to reduce duplication
    def row_activated_restore_partition_toggle(self, treeview, path, view_column):
        list_store, iters = self.get_rows(
            "restore_step4_image_partition_treeview_treeselection"
        )
        new_state = not list_store.get(iters[0], 1)[0]
        self.backup_image.toggle_restore_of_row(iters[0], new_state)

    # Callback for double click (row-activate) on restore partition mapping toggle
    # TODO: Directly call restore_partition_toggled from above, to reduce duplication
    def row_activated_clone_partition_toggle(self, treeview, path, view_column):
        list_store, iters = self.get_rows(
            "clone_step4_image_partition_treeview_treeselection"
        )
        new_state = not list_store.get(iters[0], 1)[0]
        self.backup_image.toggle_restore_of_row(iters[0], new_state)

    def destination_partition_node_changed(
        self, cell_renderer_combo, path, cell_render_combo_iter
    ):
        # Retrieve the selected string from the CellRendererCombo combo widget.
        cell_render_model = cell_renderer_combo.get_property("model")
        key = cell_render_model.get_value(cell_render_combo_iter, 0)
        enduser_friendly_string = cell_render_model.get_value(cell_render_combo_iter, 1)
        self.backup_image.change_combo_box(path, key, enduser_friendly_string)

    def overwrite_partition_table_toggle(self, toggle_button):
        self.backup_image.overwrite_partition_table_toggle(toggle_button.get_active())

    def show_hidden_devices_toggle(self, toggle_button):
        new_state = toggle_button.get_active()
        # Ensure all the similar toggle buttons in the application are consistent.
        # FIXME: Not ideal from an abstraction perspective
        self.builder.get_object("backup_step1_show_hidden_devices").set_active(
            new_state
        )
        self.builder.get_object("backup_step3_show_hidden_devices").set_active(
            new_state
        )
        self.builder.get_object("restore_step1_show_hidden_devices").set_active(
            new_state
        )
        self.builder.get_object("restore_step3_show_hidden_devices").set_active(
            new_state
        )
        self.builder.get_object("clone_step2_show_hidden_devices").set_active(new_state)
        self.builder.get_object("clone_step3_show_hidden_devices").set_active(new_state)
        self.drive_query.set_show_hidden_information(new_state)
        # Refresh the tables.
        # FIXME: Not ideal from an abstraction perspective
        if self.selected_drive_keys:
            self.drive_query.populate_partition_selection_table(
                self.selected_drive_keys
            )
            self.drive_query.populate_mount_partition_table(
                ignore_drive_keys=self.selected_drive_keys
            )
        elif self.selected_drive_key is not None:
            self.drive_query.populate_partition_selection_table(self.selected_drive_key)
            self.drive_query.populate_mount_partition_table(
                ignore_drive_key=self.selected_drive_key
            )
        self.drive_query.populate_drive_selection_table()

    def open_url_as_non_root(self, button):
        uri = button.get_uri()
        target_user = "ubuntu"
        is_success, failed_message = Utility.open_url_as_user(target_user, uri)
        if not is_success:
            ErrorMessageModalPopup.display_nonfatal_warning_message(
                self.builder, failed_message
            )

    def select_image_folder(self, button):
        BrowseSelectionPopup(
            self.builder,
            callback=self.selected_image_folder,
            default_directory=MOUNT_DIR,
            is_allow_selecting_folder_outside_mount=True,
        )
        return

    def select_image_explorer_image_folder(self, button):
        BrowseSelectionPopup(
            self.builder,
            callback=self.selected_image_folder,
            default_directory="/",
            is_allow_selecting_folder_outside_mount=True,
        )
        return

    def selected_image_folder(self, text, is_allow_selecting_folder_outside_mount):
        print("Received path " + text)
        if not is_allow_selecting_folder_outside_mount and MOUNT_DIR not in text:
            ErrorMessageModalPopup(
                self.builder,
                _("You must select a folder inside {location}").format(
                    location=MOUNT_DIR
                )
                + "\n"
                + _("Please select a different folder."),
            )
        else:
            self.image_folder_query.query_folder(text)

    # ------------------------------------------------------------------
    # SeekZilla differential backup selection (compression page)
    # ------------------------------------------------------------------

    def _init_differential_backup_ui(self):
        """Append the "Differential Backup (Optional)" frame to the compression page.

        Built programmatically (no Glade widget IDs change); silently skipped
        when the container widget is absent (e.g. minimal test builders).
        """
        self.diff_selection = None
        self.diff_base_candidates = []
        self.diff_custom_candidate = None
        self.diff_checkbutton = None
        self.diff_base_combobox = None
        self.diff_browse_button = None
        self.diff_status_label = None
        try:
            container = self.builder.get_object("backup_step7_content_vbox2")
        except Exception:
            container = None
        if container is None or not hasattr(container, "pack_start"):
            return
        try:
            frame = Gtk.Frame(label=_("Differential Backup (Optional)"))
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            vbox.set_border_width(8)
            checkbutton = Gtk.CheckButton(
                label=_(
                    "Differential backup against an existing full base image (only store changed 1 MiB blocks)"
                )
            )
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            combobox = Gtk.ComboBoxText()
            combobox.set_hexpand(True)
            browse_button = Gtk.Button(label=_("Browse..."))
            hbox.pack_start(combobox, True, True, 0)
            hbox.pack_start(browse_button, False, False, 0)
            status_label = Gtk.Label()
            status_label.set_xalign(0.0)
            status_label.set_line_wrap(True)
            vbox.pack_start(checkbutton, False, False, 0)
            vbox.pack_start(hbox, False, False, 0)
            vbox.pack_start(status_label, False, False, 0)
            frame.add(vbox)
            container.pack_start(frame, False, False, 6)
            when_ready_label = self.builder.get_object("backup_step6_when_ready_label")
            if when_ready_label is not None and when_ready_label.get_parent() is container:
                children = container.get_children()
                if when_ready_label in children:
                    container.reorder_child(frame, children.index(when_ready_label))
            checkbutton.connect("toggled", self._on_differential_toggled)
            combobox.connect("changed", self._on_differential_base_changed)
            browse_button.connect("clicked", self._on_differential_browse_clicked)
            frame.show_all()
            self.diff_checkbutton = checkbutton
            self.diff_base_combobox = combobox
            self.diff_browse_button = browse_button
            self.diff_status_label = status_label
            self._update_differential_widgets()
        except Exception:
            traceback.print_exc()
            self.diff_checkbutton = None
            self.diff_base_combobox = None
            self.diff_browse_button = None
            self.diff_status_label = None

    def _reset_differential_backup_selection(self):
        self.diff_selection = None
        self.diff_custom_candidate = None
        self.diff_base_candidates = []
        checkbutton = getattr(self, "diff_checkbutton", None)
        if checkbutton is not None:
            checkbutton.set_active(False)
        combobox = getattr(self, "diff_base_combobox", None)
        if combobox is not None:
            combobox.remove_all()
        self._update_differential_widgets()

    def _get_backup_drive_keys(self):
        keys = list(getattr(self, "active_backup_drive_keys", None) or [])
        if not keys and getattr(self, "selected_drive_key", None):
            keys = [self.selected_drive_key]
        return keys

    def _get_backup_parent_dir(self):
        parent_dir = getattr(self, "backup_parent_dir", None)
        if parent_dir:
            return parent_dir
        label = self.builder.get_object("backup_folder_label")
        return label.get_text() if label is not None else ""

    def refresh_differential_base_images(self):
        """Re-discover compatible base images in the destination folder."""
        drive_state = getattr(getattr(self, "drive_query", None), "drive_state", None) or {}
        try:
            candidates = discover_compatible_base_images(
                self._get_backup_parent_dir(), self._get_backup_drive_keys(), drive_state
            )
        except Exception:
            traceback.print_exc()
            candidates = []
        custom = getattr(self, "diff_custom_candidate", None)
        if custom is not None:
            candidates = [custom] + [
                c for c in candidates if c.get("base_dir") != custom.get("base_dir")
            ]
        self.diff_base_candidates = candidates
        combobox = getattr(self, "diff_base_combobox", None)
        if combobox is not None:
            combobox.remove_all()
            for candidate in candidates:
                combobox.append_text(candidate["label"])
            if candidates:
                combobox.set_active(0)
        self._update_differential_widgets()

    def _update_differential_widgets(self):
        checkbutton = getattr(self, "diff_checkbutton", None)
        if checkbutton is None:
            return
        is_active = checkbutton.get_active()
        candidates = getattr(self, "diff_base_candidates", None) or []
        if self.diff_base_combobox is not None:
            self.diff_base_combobox.set_sensitive(is_active and len(candidates) > 0)
        if self.diff_browse_button is not None:
            self.diff_browse_button.set_sensitive(is_active)
        if self.diff_status_label is None:
            return
        if not is_active:
            text = _(
                "Full backup: seekable index + 1 MiB block-hash (.rzbhash), usable as a future differential base."
            )
        elif not candidates:
            text = _(
                "No compatible full base image (with .rzbhash) found in {folder}. Use Browse... to select one."
            ).format(folder=self._get_backup_parent_dir())
        else:
            selection, _error = self.get_differential_backup_selection()
            text = ""
            if selection is not None and selection.get("missing_drives"):
                text = _(
                    "No base image for: {drives} (these drives get a full backup)."
                ).format(drives=", ".join(selection["missing_drives"]))
            if not text:
                text = _(
                    "Partitions that are incompatible with the base (resized, other filesystem, partclone.dd) are saved as full images automatically."
                )
        self.diff_status_label.set_text(text)

    def _on_differential_toggled(self, toggle_button):
        if toggle_button.get_active() and not (getattr(self, "diff_base_candidates", None) or []):
            self.refresh_differential_base_images()
        self._update_differential_widgets()

    def _on_differential_base_changed(self, combobox):
        self._update_differential_widgets()

    def _build_custom_base_candidate(self, chosen_dir):
        """Candidate dict for a user-chosen base folder, or None if it is unusable."""
        if not chosen_dir or not os.path.isdir(chosen_dir):
            return None
        drive_keys = self._get_backup_drive_keys()
        drive_state = getattr(getattr(self, "drive_query", None), "drive_state", None) or {}
        parent_dir = os.path.dirname(os.path.normpath(chosen_dir))
        try:
            discovered = discover_compatible_base_images(parent_dir, drive_keys, drive_state)
        except Exception:
            traceback.print_exc()
            discovered = []
        norm_chosen = os.path.normpath(chosen_dir)
        for candidate in discovered:
            if norm_chosen in [os.path.normpath(p) for p in candidate["base_dirs_by_drive"].values()]:
                found = dict(candidate)
                found["label"] = candidate["label"] + " " + _("(selected)")
                return found
        try:
            entries = os.listdir(chosen_dir)
        except OSError:
            return None
        if "Info-rzdiff-manifest.json" in entries:
            return None
        if not any(e.startswith("Info-bhash-") and e.endswith(".rzbhash") for e in entries):
            return None
        # Not matched to a drive by name/disk file: offer it to every drive;
        # partitions without a matching .rzbhash fall back to full backups.
        return {
            "label": os.path.basename(norm_chosen) + " " + _("(selected)"),
            "name": os.path.basename(norm_chosen),
            "base_dir": chosen_dir,
            "base_dirs_by_drive": {dk: chosen_dir for dk in drive_keys},
            "missing_drives": [],
            "matched_partitions": {},
            "is_multi_drive": len(drive_keys) > 1,
            "mtime": 0.0,
        }

    def _on_differential_browse_clicked(self, button):
        dialog = Gtk.FileChooserDialog(
            title=_("Select the full base image folder"),
            parent=self.builder.get_object("main_window"),
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OPEN, Gtk.ResponseType.OK
        )
        start_dir = self._get_backup_parent_dir()
        if start_dir and os.path.isdir(start_dir):
            dialog.set_current_folder(start_dir)
        chosen_dir = None
        try:
            if dialog.run() == Gtk.ResponseType.OK:
                chosen_dir = dialog.get_filename()
        finally:
            dialog.destroy()
        if not chosen_dir:
            return
        candidate = self._build_custom_base_candidate(chosen_dir)
        if candidate is None:
            ErrorMessageModalPopup(
                self.builder,
                _(
                    "The selected folder cannot be used as a differential base: {folder}\n\nA base must be a full SeekZilla backup containing Info-bhash-*.rzbhash files (and must not be a differential backup itself)."
                ).format(folder=chosen_dir),
            )
            return
        self.diff_custom_candidate = candidate
        self.refresh_differential_base_images()
        if self.diff_checkbutton is not None:
            self.diff_checkbutton.set_active(True)
        if self.diff_base_combobox is not None and self.diff_base_candidates:
            self.diff_base_combobox.set_active(0)
        self._update_differential_widgets()

    def get_differential_backup_selection(self):
        """Return (candidate_dict_or_None, error_message_or_None)."""
        checkbutton = getattr(self, "diff_checkbutton", None)
        if checkbutton is None or not checkbutton.get_active():
            return None, None
        candidates = getattr(self, "diff_base_candidates", None) or []
        combobox = getattr(self, "diff_base_combobox", None)
        index = combobox.get_active() if combobox is not None else -1
        if not candidates or index is None or index < 0 or index >= len(candidates):
            return None, _(
                "Differential backup is enabled, but no base image is selected.\n\nSelect a full SeekZilla backup (with .rzbhash files) using Browse..., or untick the differential backup option."
            )
        return candidates[index], None

    def _apply_backup_mode_folder_name(self, is_differential):
        """Use "-diff-seekzilla" instead of the default "-img-seekzilla" folder suffix."""
        parent_dir = getattr(self, "backup_parent_dir", None)
        folder_name = getattr(self, "backup_folder_name", None)
        if parent_dir is None or folder_name is None:
            return
        new_name = folder_name
        if is_differential and folder_name.endswith(SEEKZILLA_FULL_FOLDER_SUFFIX):
            new_name = (
                folder_name[: -len(SEEKZILLA_FULL_FOLDER_SUFFIX)]
                + SEEKZILLA_DIFF_FOLDER_SUFFIX
            )
        dest_dir = os.path.join(parent_dir, new_name)
        planned_dest_dirs = compute_backup_dest_dirs(dest_dir, self.active_backup_drive_keys)
        if new_name != folder_name and any(
            os.path.exists(d) for d in planned_dest_dirs.values()
        ):
            # Keep the (already validated) user-chosen name instead.
            dest_dir = os.path.join(parent_dir, folder_name)
            planned_dest_dirs = compute_backup_dest_dirs(
                dest_dir, self.active_backup_drive_keys
            )
        self.dest_dir = dest_dir
        self.dest_dirs_by_drive = planned_dest_dirs

    def get_backup_mode_markup(self):
        """Pango markup line describing the full / differential backup mode."""
        heading = GObject.markup_escape_text(_("Backup Mode:"))
        selection = getattr(self, "diff_selection", None)
        compression_format = (getattr(self, "compression_dict", None) or {}).get("format")
        if selection is None:
            if compression_format in ("zstd", "gzip"):
                mode = _("Full (Seekable Index + 1 MiB Block-Hash)")
            else:
                mode = _("Full")
            return "<b>" + heading + "</b> " + GObject.markup_escape_text(mode)
        base_names = []
        for dk, path in (selection.get("base_dirs_by_drive") or {}).items():
            base_names.append(
                (dk + ": " if len(selection.get("base_dirs_by_drive")) > 1 else "")
                + os.path.basename(os.path.normpath(path))
            )
        mode = _("Differential (Base: {base})").format(base=", ".join(base_names))
        text = "<b>" + heading + "</b> " + GObject.markup_escape_text(mode)
        if selection.get("missing_drives"):
            text += "\n" + GObject.markup_escape_text(
                _("No base image for: {drives} (full backup)").format(
                    drives=", ".join(selection["missing_drives"])
                )
            )
        if compression_format not in ("zstd", "gzip"):
            text += "\n" + GObject.markup_escape_text(
                _(
                    "Differential backup requires zstd or gzip compression; partitions will be saved as full images."
                )
            )
        return text

    def confirm_backup_configuration(self):
        description = GObject.markup_escape_text(self.destination_partition_description)
        compression_string = (
            "<b>"
            + _("Compression format: ")
            + "</b>"
            + self.compression_dict["format"]
            + ", "
            + _("Compression level: ")
            + str(self.compression_dict["level"])
        )
        compression_string += "\n" + self.get_backup_mode_markup()
        source_drive_heading = GObject.markup_escape_text(_("Source drive"))
        backup_partitions_heading = GObject.markup_escape_text(
            _("Backing up the following partition(s)")
        )

        active_drives = self.active_backup_drive_keys or self.selected_drive_keys
        if len(active_drives) > 1:
            sections = []
            total = len(active_drives)
            for idx, dk in enumerate(active_drives, start=1):
                meta = self.selected_drives_meta.get(dk, {})
                number = GObject.markup_escape_text(meta.get("number", dk) or dk)
                size = GObject.markup_escape_text(meta.get("capacity", "") or "")
                raw_model = meta.get("model")
                model = (
                    "" if raw_model is None else GObject.markup_escape_text(raw_model)
                )
                drive_parts = self.partitions_to_backup_by_drive.get(dk, {})
                part_lines = ""
                for pkey, pval in drive_parts.items():
                    part_lines += (
                        "    "
                        + GObject.markup_escape_text(pkey)
                        + ":  "
                        + GObject.markup_escape_text(pval["description"])
                        + "\n"
                    )
                dest_line = GObject.markup_escape_text(
                    _(
                        "The backup image will be written into folder {dest_dir} on {description}"
                    ).format(
                        dest_dir=self.dest_dirs_by_drive.get(dk, self.dest_dir),
                        description=self.destination_partition_description,
                    )
                )
                sections.append(
                    f"<b>[{idx}/{total}] {source_drive_heading}</b> {number} [{size}] ({model})\n"
                    f"<b>{backup_partitions_heading}</b>:\n"
                    f"{part_lines}"
                    f"<b>{dest_line}</b>"
                )
            text_to_display = (
                "\n" + "\n\n".join(sections) + f"\n\n{compression_string}\n"
            )
        else:
            number = GObject.markup_escape_text(
                self.selected_drive_enduser_friendly_drive_number
            )
            _device = GObject.markup_escape_text(self.selected_drive_key)
            size = GObject.markup_escape_text(self.selected_drive_capacity)
            if self.selected_drive_model is None:
                model = ""
            else:
                model = GObject.markup_escape_text(self.selected_drive_model)
            partition_list_string = ""
            for key in self.partitions_to_backup.keys():
                partition_list_string += (
                    "    "
                    + GObject.markup_escape_text(key)
                    + ":  "
                    + GObject.markup_escape_text(
                        self.partitions_to_backup[key]["description"]
                    )
                    + "\n"
                )
            target_dir = self.dest_dirs_by_drive.get(
                self.selected_drive_key, self.dest_dir
            )
            backup_image_destination_heading = GObject.markup_escape_text(
                _(
                    "The backup image will be written into folder {dest_dir} on {description}"
                ).format(dest_dir=target_dir, description=description)
            )

            text_to_display = f"""
<b>{source_drive_heading}</b> {number} [{size}] ({model})

<b>{backup_partitions_heading}</b>:
{partition_list_string}

{compression_string}

<b>{backup_image_destination_heading}</b>
"""
        self.builder.get_object(
            "backup_step7_confirm_config_program_defined_text"
        ).set_markup(text_to_display)

    def confirm_restore_configuration(self):
        # number = GObject.markup_escape_text(self.selected_drive_enduser_friendly_drive_number)

        is_overwriting_base_destination = False
        print("Partitions to restore is " + str(self.partitions_to_restore))
        source_image_absolute_path = self.selected_image_absolute_path
        destination_drive_description = self.restore_destination_drive_desc
        restore_partition_list_string = ""
        for key in self.partitions_to_restore.keys():
            image_part_description = GObject.markup_escape_text(
                self.partitions_to_restore[key]["description"]
            )
            dest_key = GObject.markup_escape_text(
                self.partitions_to_restore[key]["dest_key"]
            )
            if Utility.is_base_device_node(dest_key):
                is_overwriting_base_destination = True
            dest_description = GObject.markup_escape_text(
                self.partitions_to_restore[key]["dest_description"]
            )
            restore_partition_list_string += (
                "    "
                + GObject.markup_escape_text(key)
                + " ("
                + image_part_description
                + ")  ---->  "
                + dest_key
                + " ("
                + dest_description
                + ")\n"
            )
        restore_partition_list_string += "\n"

        if self.is_overwriting_partition_table or is_overwriting_base_destination:
            overwriting_partition_table_string = (
                "<b>" + _("WILL BE OVERWRITING PARTITION TABLE") + "</b>"
            )
        else:
            overwriting_partition_table_string = _(
                "Will <b>NOT</b> be overwriting partition table"
            )

        source_image_heading = _("Source image")
        destination_drive_msg = _("Destination drive")
        restoring_following_partition_msg = _("Restoring the following partitions")

        text_to_display = f"""
<b>{source_image_heading}</b> {source_image_absolute_path}
<b>{destination_drive_msg}</b> {destination_drive_description}

<b>{restoring_following_partition_msg}</b>:
{restore_partition_list_string}

{overwriting_partition_table_string}
"""
        self.builder.get_object(
            "restore_step5_confirm_config_program_defined_text"
        ).set_markup(text_to_display)

    def confirm_clone_configuration(self):
        print("Partitions to clone is " + str(self.partitions_to_clone))
        is_overwriting_base_destination = False
        source_image_absolute_path = self.source_drive_metadata_only_image.absolute_path
        destination_drive_description = self.clone_destination_drive_desc
        clone_partition_list_string = ""
        for key in self.partitions_to_clone.keys():
            image_part_description = GObject.markup_escape_text(
                self.partitions_to_clone[key]["description"]
            )
            dest_key = GObject.markup_escape_text(
                self.partitions_to_clone[key]["dest_key"]
            )
            if Utility.is_base_device_node(dest_key):
                is_overwriting_base_destination = True
            dest_description = GObject.markup_escape_text(
                self.partitions_to_clone[key]["dest_description"]
            )
            clone_partition_list_string += (
                "    "
                + GObject.markup_escape_text(key)
                + " ("
                + image_part_description
                + ")  ---->  "
                + dest_key
                + " ("
                + dest_description
                + ")\n"
            )
        clone_partition_list_string += "\n"

        if self.is_overwriting_partition_table or is_overwriting_base_destination:
            overwriting_partition_table_string = (
                "<b>" + _("WILL BE OVERWRITING PARTITION TABLE") + "</b>"
            )
        else:
            overwriting_partition_table_string = _(
                "Will <b>NOT</b> be overwriting partition table"
            )

        source_image_heading = _("Source drive")
        destination_drive_msg = _("Destination drive")
        restoring_following_partition_msg = _("Cloning the following partitions")

        text_to_display = f"""
<b>{source_image_heading}</b> {source_image_absolute_path}
<b>{destination_drive_msg}</b> {destination_drive_description}

<b>{restoring_following_partition_msg}</b>:
{clone_partition_list_string}

{overwriting_partition_table_string}
"""
        self.builder.get_object(
            "clone_step5_confirm_config_program_defined_text"
        ).set_markup(text_to_display)

    def find_network_share(self, button):
        # FIXME: Overhaul network share handling.
        ErrorMessageModalPopup(
            self.builder,
            "Search network function is disabled and will be re-introduced in the next version.\n\nPlease enter the network details manually.",
        )

    def partition_selection_changed(self, treeselection):
        self.builder.get_object("button_mount").set_sensitive(True)

    def open_file_manager(self, button):
        if shutil.which("xdg-open") is None:
            ErrorMessageModalPopup(
                self.builder,
                "Cannot launch file manager using xdg-open."
                "Please manually navigate to the following path using a file manager: "
                + IMAGE_EXPLORER_DIR,
            )
        else:
            target_user = "ubuntu"
            if not Utility.is_user_valid(target_user):
                print(target_user + " does not exist, using root")
                target_user = "root"
            is_success, failed_message = Utility.open_app_as_target_user(
                target_user, ["xdg-open", IMAGE_EXPLORER_DIR]
            )
            if not is_success:
                ErrorMessageModalPopup(self.builder, failed_message)

    # Callback for double click (row-activate).
    def row_activated_partition_selected(self, treeview, path, view_column):
        self.mount_partition(button=None)

    # Called via AreYouSure prompt
    def _mount_partition_confirmation_callback(self, is_affirmative):
        if is_affirmative:
            list_store, iters = self.get_rows(
                "image_explorer_image_partition_treeselection"
            )
            selected_partition_key = list_store.get(iters[0], 0)[0]
            self.image_explorer_manager.mount_partition(selected_partition_key)

    def mount_partition(self, button):
        list_store, iters = self.get_rows(
            "image_explorer_image_partition_treeselection"
        )
        selected_partition_key = list_store.get(iters[0], 0)[0]

        error_message = ""
        compression = self.image_explorer_manager.get_partition_compression(
            selected_partition_key
        )
        image_type = self.image_explorer_manager.get_partition_type(
            selected_partition_key
        )
        if image_type == "swap":
            error_message += "Swap partitions cannot be mounted.\n"
        elif image_type == "missing":
            # This shouldn't be printed because missing partitions should being filtered out.
            error_message += (
                "There are no saved image associated with this partition {key}.".format(
                    key=selected_partition_key
                )
                + "\n\n"
                + _(
                    "This may occur if Clonezilla was originally unable to backup this partition."
                )
                + "\n"
            )
        if compression is None:
            error_message += "Could not extract image compression.\n"
        if error_message != "":
            ErrorMessageModalPopup(self.builder, error_message)
            return

        # Don't show reminder when unmounting or if the image is uncompressed.
        if (
            not self.image_explorer_manager.get_mounted_state()
            and not compression == "uncompressed"
        ):
            AreYouSureModalPopup(
                self.builder,
                _(
                    "Reminder: Mounting large gzipped-compressed images WILL be UNUSABLY slow.\n\ngzip is the default compression format used by Clonezilla and Rescuezilla. If Rescuezilla doesn't cleanly unmount the image being explored a reboot may be required.\n\nIf you want the best experience with very large images with this BETA feature, it is highly recommended you make an image WITHOUT COMPRESSION.\n\nAre you sure you want to continue?"
                ),
                self._mount_partition_confirmation_callback,
            )
        else:
            # Unmounting doesn't need an AreYouSure popup.
            self._mount_partition_confirmation_callback(True)
