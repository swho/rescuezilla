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
#   Modified 2026-11-28 (Community Fork): SeekZilla v2.2 moves the Backup
#   Mode & Differential Base selection to Step 4, adds the per-partition
#   Diff/Full preview, Needs-.rzbhash base discovery (incl. blkdev.list
#   drift matching) and the in-app background "Build Base Index (.rzbhash)"
#   builder (GPL-3.0-or-later).
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
import time
import traceback
from datetime import datetime

import block_hash
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
from gi.repository import Gtk, GObject, GLib, Pango  # noqa: E402

# SeekZilla default backup folder suffixes (full / differential)
SEEKZILLA_FULL_FOLDER_SUFFIX = "-img-seekzilla"
SEEKZILLA_DIFF_FOLDER_SUFFIX = "-diff-seekzilla"


def discover_compatible_base_images(
    parent_dir, drive_keys, drive_state, include_needs_bhash=False
):
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

    `include_needs_bhash=True` (SeekZilla v2.2 Step 4 UI) additionally:
    - offers full Rescuezilla / Clonezilla backups whose Partclone images
      (`<part>.<fs>-ptcl-img.<zst|gz|uncomp>.aa`) have no `.rzbhash` yet (a
      present but structurally invalid `.rzbhash` counts as missing);
    - matches a drive whose device name changed since the base was taken
      (e.g. nvme0n1 <-> nvme1n1 swap) through `blkdev.list` MODEL + SIZE vs
      `drive_state[dk]["model"]` / `["capacity"]`, only when exactly one
      drive of `drive_state` matches (`device_name_drifted` is set);
    - adds ready, has_any_bhash, needs_bhash, fully_indexed,
      missing_bhash_by_drive ({dk: [(base_dir, [PtclImageSet, ...])]}),
      missing_bhash_count, missing_bhash_bytes, device_name_drifted,
      drifted_drives ({dk: base drive name}), base_parts_by_drive;
    - labels candidates `<name>  [Ready]` / `<name>  [Needs .rzbhash index]`
      (multi-drive: `<name>  (<found>/<total> drives · Ready)`) and sorts
      Ready candidates first.
    With the default `include_needs_bhash=False` the behaviour is unchanged.
    """
    import collections as _collections
    import os as _os
    import re as _re

    manifest_name = "Info-rzdiff-manifest.json"
    bhash_prefix = "Info-bhash-"
    bhash_suffix = ".rzbhash"
    ts_re = _re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{4})")
    needs_mode = bool(include_needs_bhash)
    _bh = None
    if needs_mode:
        try:
            import block_hash as _bh
        except ImportError:
            from rescuezilla import block_hash as _bh  # type: ignore[no-redef]

    def _norm_model(text):
        return " ".join(str(text or "").replace("\\x20", " ").split()).lower()

    def _parse_lsblk_size(tok):
        m = _re.match(r"^([0-9]+(?:[.,][0-9]+)?)([BKMGTPEZ]?)(?:i?B)?$", str(tok).strip())
        if not m:
            return None
        unit = 1024 ** "BKMGTPEZ".index(m.group(2) or "B")
        return float(m.group(1).replace(",", ".")) * unit, unit

    def _read_blkdev_list(path):
        """First `disk` row of Clonezilla/Rescuezilla `blkdev.list` (lsblk table)."""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return None
        if not lines:
            return None
        model_off = lines[0].find("MODEL")
        for line in lines[1:]:
            toks = line.split()
            if len(toks) < 4 or toks[3] != "disk":
                continue
            parsed = _parse_lsblk_size(toks[2])
            model = ""
            if model_off >= 0 and len(line) > model_off:
                model = line[model_off:].strip()
            return {
                "kname": toks[0],
                "size": parsed[0] if parsed else None,
                "unit": parsed[1] if parsed else None,
                "model": _norm_model(model),
            }
        return None

    def _drive_capacity(info):
        try:
            return float(int(str(info.get("capacity")).strip()))
        except (TypeError, ValueError, AttributeError):
            return None

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
        status = None
        if needs_mode:
            try:
                status = _bh.inspect_base_dir_bhash_status(path)
            except Exception:
                status = None
            if not bhash_parts and not (status and status["ptcl_parts"]):
                continue
        elif not bhash_parts:
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
        folder = {"name": name, "path": path, "bhash_parts": bhash_parts, "disks": disks, "mtime": mtime}
        if needs_mode:
            ptcl_parts = list(status["ptcl_parts"]) if status else []
            folder["ptcl_parts"] = ptcl_parts
            folder["missing_images"] = list(status["missing_images"]) if status else []
            folder["missing_bytes"] = int(status["total_missing_compressed_bytes"]) if status else 0
            folder["has_any_bhash"] = (
                bool(status["bhash_parts"]) if ptcl_parts else bool(bhash_parts)
            )
            folder["blkdev"] = (
                _read_blkdev_list(_os.path.join(path, "blkdev.list"))
                if "blkdev.list" in entries
                else None
            )
        folders.append(folder)

    all_drives = drive_state if isinstance(drive_state, dict) else {}

    def _blkdev_contradicts(folder, dk):
        """True when blkdev.list MODEL+SIZE definitely describes a different physical disk."""
        info = folder.get("blkdev") if needs_mode else None
        if not info or not info["model"] or info["size"] is None:
            return False
        dinfo = all_drives.get(dk)
        if not isinstance(dinfo, dict):
            return False
        dmodel = _norm_model(dinfo.get("model"))
        cap = _drive_capacity(dinfo)
        if not dmodel or cap is None:
            return False
        tolerance = 0.051 * info["unit"] + 1
        return dmodel != info["model"] or abs(cap - info["size"]) > tolerance

    def _suffix_match(folder, dk):
        if not folder["name"].endswith("-" + shorts[dk]):
            return False
        return not _blkdev_contradicts(folder, dk)

    def _known_parts(folder):
        if needs_mode:
            return set(folder["bhash_parts"]) | set(folder["ptcl_parts"])
        return set(folder["bhash_parts"])

    def _content_match(folder, dk):
        if _blkdev_contradicts(folder, dk):
            return False
        if shorts[dk] in folder["disks"]:
            return True
        if not folder["disks"]:
            return bool(part_shorts[dk] & _known_parts(folder))
        return False

    def _matched_partitions(folder, dk):
        return sorted(part_shorts[dk] & set(folder["bhash_parts"]))

    # Hardware drift fallback (needs mode only): blkdev.list MODEL + SIZE.
    def _blkdev_match(folder):
        """The single selected drive matching the folder's blkdev.list, else None."""
        info = folder.get("blkdev") if needs_mode else None
        if not info or not info["model"] or info["size"] is None:
            return None
        tolerance = 0.051 * info["unit"] + 1
        hits = []
        for dk, dinfo in all_drives.items():
            if not isinstance(dinfo, dict):
                continue
            cap = _drive_capacity(dinfo)
            if cap is None or _norm_model(dinfo.get("model")) != info["model"]:
                continue
            if abs(cap - info["size"]) <= tolerance:
                hits.append(dk)
        if len(hits) != 1 or hits[0] not in shorts:
            return None
        return hits[0]

    def _base_drive_name(folder):
        info = folder.get("blkdev")
        if info and info.get("kname"):
            return info["kname"]
        if len(folder["disks"]) == 1:
            return folder["disks"][0]
        return None

    def _is_drifted(folder, dk):
        if _suffix_match(folder, dk) or (
            not _blkdev_contradicts(folder, dk) and shorts[dk] in folder["disks"]
        ):
            return False
        base_name = _base_drive_name(folder)
        return base_name is not None and base_name != shorts[dk]

    def _decorate(candidate, folder_by_drive):
        """needs-mode readiness fields + label for one candidate."""
        missing_by_drive = _collections.OrderedDict()
        count = 0
        nbytes = 0
        any_bhash = False
        drifted = _collections.OrderedDict()
        base_parts = {}
        for dk, folder in folder_by_drive.items():
            is_drifted = _is_drifted(folder, dk)
            if is_drifted:
                drifted[dk] = _base_drive_name(folder)
            else:
                any_bhash = any_bhash or folder["has_any_bhash"]
                if folder["missing_images"]:
                    missing_by_drive[dk] = [(folder["path"], list(folder["missing_images"]))]
                    count += len(folder["missing_images"])
                    nbytes += folder["missing_bytes"]
            base_parts[dk] = sorted(_known_parts(folder))
        all_drifted = bool(drifted) and len(drifted) == len(folder_by_drive)
        needs = bool(missing_by_drive)
        candidate.update(
            {
                "has_any_bhash": any_bhash,
                "needs_bhash": needs,
                "fully_indexed": (not needs) and (not all_drifted),
                "ready": any_bhash and not needs and not all_drifted,
                "missing_bhash_by_drive": dict(missing_by_drive),
                "missing_bhash_count": count,
                "missing_bhash_bytes": nbytes,
                "device_name_drifted": bool(drifted),
                "all_drifted": all_drifted,
                "drifted_drives": dict(drifted),
                "base_parts_by_drive": base_parts,
            }
        )
        if all_drifted:
            if candidate["is_multi_drive"]:
                candidate["label"] = "{name}  ({found}/{total} drives · Device name changed)".format(
                    name=candidate["name"],
                    found=len(candidate["base_dirs_by_drive"]),
                    total=len(keys),
                )
            else:
                dk_only, old_name = next(iter(drifted.items()))
                candidate["label"] = "{name}  [Device name changed: {old} -> {new}]".format(
                    name=candidate["name"],
                    old=old_name or "?",
                    new=shorts[dk_only],
                )
        else:
            state_text = "Ready" if candidate["ready"] else "Needs .rzbhash index"
            if candidate["is_multi_drive"]:
                candidate["label"] = "{name}  ({found}/{total} drives · {state})".format(
                    name=candidate["name"],
                    found=len(candidate["base_dirs_by_drive"]),
                    total=len(keys),
                    state=state_text,
                )
            else:
                candidate["label"] = "{name}  [{state}]".format(name=candidate["name"], state=state_text)
        return candidate

    def _needs_sort_key(c):
        return (bool(c.get("all_drifted")), not c["ready"], len(c["missing_drives"]), -c["mtime"], c["name"])

    results = []
    if len(keys) == 1:
        dk = keys[0]
        for folder in folders:
            if not (_suffix_match(folder, dk) or _content_match(folder, dk)):
                if not (needs_mode and _blkdev_match(folder) == dk):
                    continue
            candidate = {
                "label": folder["name"],
                "name": folder["name"],
                "base_dir": folder["path"],
                "base_dirs_by_drive": {dk: folder["path"]},
                "missing_drives": [],
                "matched_partitions": {dk: _matched_partitions(folder, dk)},
                "is_multi_drive": False,
                "mtime": folder["mtime"],
            }
            if needs_mode:
                _decorate(candidate, {dk: folder})
            results.append(candidate)
        if needs_mode:
            results.sort(key=_needs_sort_key)
        else:
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
    def _place(folder, dk):
        """Group a renamed / drifted folder by its timestamp prefix (True if placed)."""
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
            return True
        return False

    for folder in folders:  # pass 2: renamed siblings matched by disk file / partitions
        if folder["path"] in used:
            continue
        for dk in keys:
            if not _content_match(folder, dk):
                continue
            if _place(folder, dk):
                break

    if needs_mode:  # pass 3: device names drifted, matched by blkdev.list model + size
        for folder in folders:
            if folder["path"] in used:
                continue
            dk = _blkdev_match(folder)
            if dk is not None:
                _place(folder, dk)

    total = len(keys)
    for gkey, group in groups.items():
        by_drive = _collections.OrderedDict(
            (dk, group[dk]["path"]) for dk in keys if dk in group
        )
        if not by_drive:
            continue
        missing = [dk for dk in keys if dk not in group]
        candidate = {
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
        if needs_mode:
            _decorate(candidate, _collections.OrderedDict((dk, group[dk]) for dk in by_drive))
        results.append(candidate)
    if needs_mode:
        results.sort(key=_needs_sort_key)
    else:
        results.sort(key=lambda c: (len(c["missing_drives"]), -c["mtime"], c["name"]))
    return results


def build_differential_partition_preview(drive_keys, partitions_to_backup_by_drive, candidate):
    """Per-drive / per-partition Diff vs Full preview for the Step 4 UI (pure logic).

    For every selected drive `dk` and partition `pk`
    (`partitions_to_backup_by_drive[dk][pk]` is the drive_state partition dict,
    whose "filesystem" is used) decide:
      - "full": no base for the drive / partclone.dd (or other non-Partclone)
        partition / partition not in the base;
      - "diff": the base has `Info-bhash-<short_part>.rzbhash`;
      - "pending_bhash": the base has the Partclone image but no (valid)
        `.rzbhash` yet (Build Base Index makes it "diff"; otherwise it is saved
        as full).
    `omitted_base_parts` lists partitions present in the base that are not
    selected in Step 2.

    Returns {"drives": [...], "diff_count", "full_count", "pending_bhash_count",
    "omitted_base_parts": {dk: [...]}, "summary_text"}.
    """
    import os as _os
    import re as _re

    ptcl_re = _re.compile(
        r"^(?P<part>[^/\\]+?)\.(?P<fs>[^./\\]+)-ptcl-img\.(?P<comp>[A-Za-z0-9]+)\.aa$"
    )
    indexable_comps = ("zst", "gz", "uncomp")
    raw_fs = ("dd", "raw", "unknown", "", "none")
    bhash_prefix = "Info-bhash-"
    bhash_suffix = ".rzbhash"

    def _short_drive(dk):
        short = _re.sub(r"^/dev/", "", str(dk).strip())
        short = _re.sub(r"[^A-Za-z0-9._-]+", "_", short).strip("._-")
        return short or "disk"

    def _short_partition(pk):
        return _re.sub("/", "-", _re.sub(r"^/dev/", "", str(pk).strip()))

    def _fs_family(name):
        f = str(name or "").strip().lower()
        if f in ("fat12", "fat16", "fat32", "vfat", "fat"):
            return "fat"
        if f in ("ext2", "ext3", "ext4", "extfs"):
            return "ext"
        if f in ("hfs+", "hfsplus", "hfsp"):
            return "hfsp"
        return f

    parts_by_drive = partitions_to_backup_by_drive or {}
    if isinstance(drive_keys, str):
        keys = [drive_keys]
    else:
        keys = list(drive_keys or [])
    for dk in parts_by_drive:
        if dk not in keys:
            keys.append(dk)
    cand = candidate if isinstance(candidate, dict) else {}
    base_dirs = dict(cand.get("base_dirs_by_drive") or {})
    drifted = dict(cand.get("drifted_drives") or {})

    pending_by_drive = {}
    for dk, items in (cand.get("missing_bhash_by_drive") or {}).items():
        shorts = set()
        for item in items or []:
            try:
                _base_dir, images = item
            except (TypeError, ValueError):
                continue
            for img in images or []:
                sp = getattr(img, "short_part", None)
                if sp is None and isinstance(img, dict):
                    sp = img.get("short_part")
                if sp:
                    shorts.add(sp)
        pending_by_drive[dk] = shorts

    scanned = {}

    def _scan(base_dir):
        if base_dir not in scanned:
            bhash, images = set(), {}
            try:
                entries = sorted(_os.listdir(base_dir))
            except OSError:
                entries = []
            for e in entries:
                if (
                    e.startswith(bhash_prefix)
                    and e.endswith(bhash_suffix)
                    and len(e) > len(bhash_prefix) + len(bhash_suffix)
                ):
                    bhash.add(e[len(bhash_prefix) : -len(bhash_suffix)])
                m = ptcl_re.match(e)
                if m:
                    images.setdefault(m.group("part"), (m.group("fs"), m.group("comp")))
            scanned[base_dir] = (bhash, images)
        return scanned[base_dir]

    drives = []
    counts = {"diff": 0, "full": 0, "pending_bhash": 0}
    omitted_by_drive = {}
    lines = []
    for dk in keys:
        parts = parts_by_drive.get(dk) or {}
        base_dir = base_dirs.get(dk)
        bhash, images = _scan(base_dir) if base_dir else (set(), {})
        pending = pending_by_drive.get(dk, set())
        entries = []
        selected_shorts = set()
        for pk, pinfo in parts.items():
            short = _short_partition(pk)
            selected_shorts.add(short)
            info = pinfo if isinstance(pinfo, dict) else {}
            fs = str(info.get("filesystem") or "").strip().lower()
            is_extended = "extended" in str(info.get("type") or "")
            if not base_dir:
                mode, reason = "full", "no compatible base for this drive"
            elif dk in drifted:
                mode, reason = (
                    "full",
                    "drive name changed ({old} -> {new}) — saved as full".format(
                        old=drifted.get(dk) or "?", new=_short_drive(dk)
                    ),
                )
            elif fs in raw_fs:
                mode, reason = "full", "partclone.dd — differential not applicable"
            elif fs == "swap" or is_extended:
                mode, reason = "full", "not a Partclone filesystem — differential not applicable"
            elif (
                short in images
                and fs
                and images[short][0] != "dd"
                and _fs_family(fs) != _fs_family(images[short][0])
            ):
                mode, reason = (
                    "full",
                    "filesystem changed ({old} -> {new}) — saved as full".format(
                        old=images[short][0], new=fs
                    ),
                )
            elif short in pending and short in images:
                mode, reason = "pending_bhash", "missing .rzbhash — click Build Base Index"
            elif short in bhash:
                mode, reason = "diff", "differential vs base .rzbhash"
            elif short in images:
                base_fs, comp = images[short]
                if base_fs == "dd":
                    mode, reason = "full", "base saved it with partclone.dd — saved as full"
                elif comp in indexable_comps:
                    mode, reason = "pending_bhash", "missing .rzbhash — click Build Base Index"
                else:
                    mode, reason = "full", "base compression ." + comp + " cannot be indexed — saved as full"
            else:
                mode, reason = "full", "not in base — saved as standalone full partition"
            counts[mode] += 1
            entries.append(
                {"partition": pk, "short_part": short, "filesystem": fs, "mode": mode, "reason": reason}
            )
        omitted = sorted((set(bhash) | set(images)) - selected_shorts) if base_dir else []
        omitted_by_drive[dk] = omitted
        drives.append(
            {
                "drive": dk,
                "short_drive": _short_drive(dk),
                "base_dir": base_dir,
                "partitions": entries,
                "omitted_base_parts": omitted,
                "device_name_drifted": dk in drifted,
                "base_drive_name": drifted.get(dk),
            }
        )

        if base_dir:
            head = "{drive}  ← base: {base}".format(
                drive=_short_drive(dk), base=_os.path.basename(_os.path.normpath(base_dir))
            )
        else:
            head = "{drive}  ← no compatible base (full backup)".format(drive=_short_drive(dk))
        lines.append(head)
        if dk in drifted:
            lines.append(
                "  ! device name changed (base: {old}, now: {new}); partitions whose names differ are saved as full".format(
                    old=drifted.get(dk) or "?", new=_short_drive(dk)
                )
            )
        width = max([len(e["short_part"]) for e in entries] + [4])
        fs_width = max([len(e["filesystem"] or "-") for e in entries] + [2])
        tags = {"diff": "DIFF", "full": "FULL", "pending_bhash": "NEEDS INDEX"}
        for e in entries:
            lines.append(
                "  {part}  {fs}  {tag:<11}  {reason}".format(
                    part=e["short_part"].ljust(width),
                    fs=(e["filesystem"] or "-").ljust(fs_width),
                    tag=tags[e["mode"]],
                    reason=e["reason"],
                )
            )
        if omitted:
            lines.append(
                "  in base but not selected (not included): " + ", ".join(omitted)
            )
    lines.append(
        "Total: {d} differential · {f} full · {p} waiting for .rzbhash".format(
            d=counts["diff"], f=counts["full"], p=counts["pending_bhash"]
        )
    )
    if counts["pending_bhash"]:
        lines.append(
            "Partitions waiting for .rzbhash are saved as full unless you click 'Build Base Index (.rzbhash)...' first."
        )
    return {
        "drives": drives,
        "diff_count": counts["diff"],
        "full_count": counts["full"],
        "pending_bhash_count": counts["pending_bhash"],
        "omitted_base_parts": omitted_by_drive,
        "summary_text": "\n".join(lines),
    }


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

        # SeekZilla v2.2: Backup Mode & Differential Base selection on Step 4
        # (destination folder), refreshed whenever the folder scan completes.
        self._init_differential_backup_ui()
        if hasattr(self.image_folder_query, "set_on_scan_complete_callback"):
            self.image_folder_query.set_on_scan_complete_callback(
                self._on_image_folder_scan_complete
            )

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
                    # SeekZilla v2.2: the backup mode is chosen on this page, so
                    # Step 5 directly proposes the matching folder name.
                    diff_selection, diff_error = self.get_differential_backup_selection()
                    if diff_error is not None:
                        ErrorMessageModalPopup(self.builder, diff_error)
                    else:
                        self.diff_selection = diff_selection
                        enduser_date = datetime.today().strftime(
                            "%Y-%m-%d-%H%M"
                        ) + self.get_default_backup_folder_suffix(
                            diff_selection is not None
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
                        # The differential base was chosen on Step 4; only warn here
                        # when the chosen compression cannot produce a diff image.
                        self._update_diff_compression_warning()
                elif self.current_page == Page.BACKUP_COMPRESSION_CUSTOMIZATION:
                    # Backup mode + base were validated on Step 4 and the folder
                    # name was proposed accordingly on Step 5 (never renamed here).
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
                        drifted_set = set(diff_selection.get("drifted_drives") or ())
                        diff_base_dirs_by_drive = {
                            dk: path
                            for dk, path in (
                                diff_selection.get("base_dirs_by_drive") or {}
                            ).items()
                            if dk not in drifted_set
                        }
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
                            )
                            if (
                                diff_base_dir is None
                                and self.selected_drive_key
                                not in (diff_selection.get("drifted_drives") or {})
                            ):
                                diff_base_dir = diff_selection.get("base_dir")
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
    # SeekZilla v2.2: Backup Mode & Differential Base (Step 4)
    # ------------------------------------------------------------------

    def _get_builder_object(self, name):
        try:
            return self.builder.get_object(name)
        except Exception:
            return None

    @staticmethod
    def _insert_before_widget(container, widget, anchor):
        """Move `widget` (already packed in `container`) right above `anchor`."""
        if anchor is None or anchor.get_parent() is not container:
            return
        children = container.get_children()
        if anchor in children:
            container.reorder_child(widget, children.index(anchor))

    def _init_differential_backup_ui(self):
        """Inject the "Backup Mode & Differential Base" frame into Step 4.

        Built programmatically inside `backup_step4_contentvbox`, right above
        `backup_step4_when_ready_label` (no Glade edits / widget IDs change);
        silently skipped when the container is absent (minimal test builders).
        `self.diff_checkbutton` is the Differential radio button
        (Gtk.RadioButton subclasses Gtk.CheckButton, so get_active() /
        "toggled" keep working for existing callers).
        """
        self.diff_selection = None
        self.diff_base_candidates = []
        self.diff_custom_candidate = None
        self.diff_custom_dir = None
        self.diff_checkbutton = None
        self.diff_full_radiobutton = None
        self.diff_base_combobox = None
        self.diff_browse_button = None
        self.diff_build_bhash_button = None
        self.diff_status_label = None
        self.diff_preview_label = None
        self.diff_preview_scrolled = None
        self.diff_compression_warning_label = None
        self.diff_bhash_build_in_progress = False
        self._diff_last_parent_dir = None
        self._init_diff_compression_warning_label()
        container = self._get_builder_object("backup_step4_contentvbox")
        if container is None or not hasattr(container, "pack_start"):
            return
        try:
            frame = Gtk.Frame(label=_("Backup Mode & Differential Base"))
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            vbox.set_border_width(4)
            radio_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
            radio_full = Gtk.RadioButton.new_with_label_from_widget(
                None, _("Full Backup (standalone baseline)")
            )
            radio_diff = Gtk.RadioButton.new_with_label_from_widget(
                radio_full,
                _("Differential Backup (changed 1 MiB blocks vs. base)"),
            )
            for radio in (radio_full, radio_diff):
                child = radio.get_child()
                if isinstance(child, Gtk.Label):
                    child.set_xalign(0.0)
            radio_full.set_active(True)
            radio_row.pack_start(radio_full, False, False, 0)
            radio_row.pack_start(radio_diff, False, False, 0)

            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            combobox = Gtk.ComboBoxText()
            combobox.set_hexpand(True)
            # Long folder names must never widen the window past 1280 px.
            for cell in combobox.get_cells():
                cell.set_property("ellipsize", Pango.EllipsizeMode.END)
            browse_button = Gtk.Button(label=_("Browse..."))
            build_button = Gtk.Button(label=_("Build Base Index (.rzbhash)..."))
            build_button.set_tooltip_text(
                _(
                    "Read the selected full backup (read-only) and create the missing Info-bhash-<partition>.rzbhash index files next to it."
                )
            )
            build_button.set_no_show_all(True)
            hbox.pack_start(combobox, True, True, 0)
            hbox.pack_start(browse_button, False, False, 0)
            hbox.pack_start(build_button, False, False, 0)

            status_label = Gtk.Label()
            status_label.set_xalign(0.0)
            status_label.set_line_wrap(True)
            status_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)

            preview_label = Gtk.Label()
            preview_label.set_xalign(0.0)
            preview_label.set_yalign(0.0)
            preview_label.set_margin_start(4)
            preview_label.set_margin_end(4)
            viewport = Gtk.Viewport()
            viewport.set_shadow_type(Gtk.ShadowType.NONE)
            viewport.add(preview_label)
            scrolled = Gtk.ScrolledWindow()
            scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            scrolled.set_shadow_type(Gtk.ShadowType.IN)
            scrolled.set_min_content_height(110)
            scrolled.set_max_content_height(136)
            if hasattr(scrolled, "set_propagate_natural_height"):
                scrolled.set_propagate_natural_height(True)
            scrolled.add(viewport)
            # Only shown while the Differential radio is active.
            scrolled.set_no_show_all(True)
            viewport.show()
            preview_label.show()

            vbox.pack_start(radio_row, False, False, 0)
            vbox.pack_start(hbox, False, False, 0)
            vbox.pack_start(status_label, False, False, 0)
            vbox.pack_start(scrolled, True, True, 0)
            frame.add(vbox)
            container.pack_start(frame, False, False, 2)
            self._insert_before_widget(
                container, frame, self._get_builder_object("backup_step4_when_ready_label")
            )
            self._compact_backup_step4_labels()

            radio_diff.connect("toggled", self._on_differential_toggled)
            combobox.connect("changed", self._on_differential_base_changed)
            browse_button.connect("clicked", self._on_differential_browse_clicked)
            build_button.connect("clicked", self._on_build_base_index_clicked)
            frame.show_all()
            self.diff_full_radiobutton = radio_full
            self.diff_checkbutton = radio_diff
            self.diff_base_combobox = combobox
            self.diff_browse_button = browse_button
            self.diff_build_bhash_button = build_button
            self.diff_status_label = status_label
            self.diff_preview_label = preview_label
            self.diff_preview_scrolled = scrolled
            self._update_differential_widgets()
        except Exception:
            traceback.print_exc()
            self.diff_full_radiobutton = None
            self.diff_checkbutton = None
            self.diff_base_combobox = None
            self.diff_browse_button = None
            self.diff_build_bhash_button = None
            self.diff_status_label = None
            self.diff_preview_label = None
            self.diff_preview_scrolled = None

    def _compact_backup_step4_labels(self):
        """Reduce the 20 px vertical padding and 15 px margins on Step 4.

        Makes room for the Backup Mode frame and partition preview so the
        entire page (including status, preview, and Back/Next buttons) fits
        cleanly at 1280x720 (even minus titlebar and LXPanel).
        """
        for name in (
            "backup_step4_text",
            "backup_step4_text2",
            "backup_step4_when_ready_label",
        ):
            label = self._get_builder_object(name)
            if label is None or not hasattr(label, "set_padding"):
                continue
            try:
                xpad, _ypad = label.get_padding()
                label.set_padding(xpad, 2)
            except Exception:
                traceback.print_exc()
        for name in (
            "backup_step4_vbox",
            "backup_step4_frame",
            "backup_step4_contentvbox",
        ):
            widget = self._get_builder_object(name)
            if widget is not None and hasattr(widget, "set_margin_bottom"):
                try:
                    widget.set_margin_bottom(4)
                except Exception:
                    traceback.print_exc()
        treeview = self._get_builder_object(
            "backup_step4_destination_folder_image_files_treeview"
        )
        self._diff_step4_images_scrolled = None
        if treeview is not None and hasattr(treeview, "get_parent"):
            try:
                tree_scrolled = treeview.get_parent()
                if tree_scrolled is not None:
                    self._diff_step4_images_scrolled = tree_scrolled
                    if hasattr(tree_scrolled, "set_margin_bottom"):
                        tree_scrolled.set_margin_bottom(4)
                    if hasattr(tree_scrolled, "set_size_request"):
                        tree_scrolled.set_size_request(-1, 78)
                    container = self._get_builder_object("backup_step4_contentvbox")
                    if container is not None and hasattr(container, "child_set_property"):
                        try:
                            container.child_set_property(tree_scrolled, "expand", False)
                        except Exception:
                            pass
            except Exception:
                traceback.print_exc()

    def _init_diff_compression_warning_label(self):
        """Step 6 hint: differential backups need zstd or gzip compression."""
        container = self._get_builder_object("backup_step7_content_vbox2")
        if container is None or not hasattr(container, "pack_start"):
            return
        try:
            label = Gtk.Label()
            label.set_xalign(0.0)
            label.set_line_wrap(True)
            label.set_markup(
                "<b>"
                + GObject.markup_escape_text(
                    _(
                        "Differential Backup was selected on Step 4, but it requires zstd or gzip compression. With this compression format every partition is saved as a full image."
                    )
                )
                + "</b>"
            )
            label.set_no_show_all(True)
            container.pack_start(label, False, False, 6)
            self._insert_before_widget(
                container, label, self._get_builder_object("backup_step6_when_ready_label")
            )
            label.hide()
            self.diff_compression_warning_label = label
            combobox = self._get_builder_object("backup_step6_compression_format_combobox")
            if combobox is not None and hasattr(combobox, "connect"):
                combobox.connect(
                    "changed", lambda _combo: self._update_diff_compression_warning()
                )
        except Exception:
            traceback.print_exc()
            self.diff_compression_warning_label = None

    def _get_selected_compression_format(self):
        combobox = self._get_builder_object("backup_step6_compression_format_combobox")
        if combobox is None:
            return None
        try:
            tree_iter = combobox.get_active_iter()
            if tree_iter is None:
                return None
            return combobox.get_model()[tree_iter][0]
        except Exception:
            return None

    def _update_diff_compression_warning(self):
        label = getattr(self, "diff_compression_warning_label", None)
        if label is None:
            return
        fmt = self._get_selected_compression_format()
        label.set_visible(
            getattr(self, "diff_selection", None) is not None
            and fmt is not None
            and fmt not in ("zstd", "gzip")
        )

    def get_default_backup_folder_suffix(self, is_differential):
        return SEEKZILLA_DIFF_FOLDER_SUFFIX if is_differential else SEEKZILLA_FULL_FOLDER_SUFFIX

    def _on_image_folder_scan_complete(self):
        """ImageFolderQuery finished scanning (GTK main thread)."""
        if self.mode == Mode.BACKUP:
            self.refresh_differential_base_images()

    def _set_differential_mode(self, is_differential):
        if is_differential:
            if self.diff_checkbutton is not None:
                self.diff_checkbutton.set_active(True)
        elif self.diff_full_radiobutton is not None:
            # A radio button cannot be de-activated directly: activate Full.
            self.diff_full_radiobutton.set_active(True)
        elif self.diff_checkbutton is not None:
            self.diff_checkbutton.set_active(False)

    def _reset_differential_backup_selection(self):
        self.diff_selection = None
        self.diff_custom_candidate = None
        self.diff_custom_dir = None
        self.diff_base_candidates = []
        self._diff_last_parent_dir = None
        self._set_differential_mode(False)
        combobox = getattr(self, "diff_base_combobox", None)
        if combobox is not None:
            combobox.remove_all()
        self._update_differential_widgets()
        self._update_diff_compression_warning()

    def _get_backup_drive_keys(self):
        keys = list(getattr(self, "active_backup_drive_keys", None) or [])
        if not keys and getattr(self, "selected_drive_key", None):
            keys = [self.selected_drive_key]
        return keys

    def _get_backup_parent_dir(self):
        # Step 4 shows (and lets the user change) the destination folder in
        # backup_folder_label: it is authoritative over an older Step 5 value.
        label = self._get_builder_object("backup_folder_label")
        text = label.get_text() if label is not None else ""
        if text:
            return text
        return getattr(self, "backup_parent_dir", None) or ""

    @staticmethod
    def _candidate_identity(candidate):
        if not candidate:
            return None
        dirs = candidate.get("base_dirs_by_drive") or {}
        if dirs:
            return tuple(sorted(os.path.normpath(p) for p in dirs.values()))
        base_dir = candidate.get("base_dir")
        return (os.path.normpath(base_dir),) if base_dir else None

    def refresh_differential_base_images(self, preserve_selection=True):
        """Re-discover base images (Ready and Needs-.rzbhash) in the destination folder.

        Keeps the currently selected base folder selected when it is still
        offered (e.g. after Build Base Index turned it from Needs -> Ready).
        """
        previous = None
        if preserve_selection:
            previous = self._candidate_identity(self._get_selected_differential_candidate())
        parent_dir = self._get_backup_parent_dir()
        prev_parent_dir = getattr(self, "_diff_last_parent_dir", None)
        parent_changed = (
            prev_parent_dir is not None
            and parent_dir
            and os.path.normpath(parent_dir) != os.path.normpath(prev_parent_dir)
        )
        self._diff_last_parent_dir = parent_dir
        drive_state = getattr(getattr(self, "drive_query", None), "drive_state", None) or {}
        try:
            candidates = discover_compatible_base_images(
                parent_dir,
                self._get_backup_drive_keys(),
                drive_state,
                include_needs_bhash=True,
            )
        except Exception:
            traceback.print_exc()
            candidates = []
        custom_dir = getattr(self, "diff_custom_dir", None)
        if custom_dir:
            # Re-inspect the browsed folder: its .rzbhash state may have changed.
            custom = self._build_custom_base_candidate(custom_dir)
            self.diff_custom_candidate = custom
            if custom is None:
                self.diff_custom_dir = None
        custom = getattr(self, "diff_custom_candidate", None)
        if custom is not None:
            custom_id = self._candidate_identity(custom)
            candidates = [custom] + [
                c for c in candidates if self._candidate_identity(c) != custom_id
            ]
        self.diff_base_candidates = candidates
        combobox = getattr(self, "diff_base_combobox", None)
        matched_previous = False
        if combobox is not None:
            combobox.remove_all()
            for candidate in candidates:
                combobox.append_text(candidate["label"])
            if candidates:
                index = 0
                if previous is not None:
                    for i, candidate in enumerate(candidates):
                        if self._candidate_identity(candidate) == previous:
                            index = i
                            matched_previous = True
                            break
                combobox.set_active(index)
        if (
            parent_changed
            and previous is not None
            and not matched_previous
            and custom is None
            and self.diff_checkbutton is not None
            and self.diff_checkbutton.get_active()
        ):
            # User moved to a different folder where the prior base doesn't exist:
            # avoid silently switching to an unrelated base.
            self._set_differential_mode(False)
        self._update_differential_widgets()

    def _get_selected_differential_candidate(self):
        candidates = getattr(self, "diff_base_candidates", None) or []
        combobox = getattr(self, "diff_base_combobox", None)
        index = combobox.get_active() if combobox is not None else -1
        if index is None or index < 0 or index >= len(candidates):
            return None
        return candidates[index]

    def get_differential_partition_preview(self, candidate=None):
        """build_differential_partition_preview() for the current Step 2 selection."""
        if candidate is None:
            candidate = self._get_selected_differential_candidate()
        return build_differential_partition_preview(
            self._get_backup_drive_keys(),
            getattr(self, "partitions_to_backup_by_drive", None) or {},
            candidate,
        )

    @staticmethod
    def _format_bytes(num_bytes):
        try:
            return Utility.human_readable_filesize(int(num_bytes))
        except Exception:
            return str(num_bytes) + " B"

    def _update_differential_widgets(self):
        checkbutton = getattr(self, "diff_checkbutton", None)
        if checkbutton is None:
            return
        is_active = checkbutton.get_active()
        building = bool(getattr(self, "diff_bhash_build_in_progress", False))
        candidates = getattr(self, "diff_base_candidates", None) or []
        candidate = self._get_selected_differential_candidate() if is_active else None
        if self.diff_base_combobox is not None:
            self.diff_base_combobox.set_sensitive(
                is_active and len(candidates) > 0 and not building
            )
        if self.diff_browse_button is not None:
            self.diff_browse_button.set_sensitive(is_active and not building)
        build_button = getattr(self, "diff_build_bhash_button", None)
        if build_button is not None:
            show_build = bool(is_active and candidate and candidate.get("needs_bhash"))
            build_button.set_visible(show_build)
            build_button.set_sensitive(show_build and not building)
        scrolled = getattr(self, "diff_preview_scrolled", None)
        if scrolled is not None:
            scrolled.set_visible(is_active)
        # In Differential mode, collapse the static read-only existing-images
        # table so the active base selector + per-partition preview occupy that
        # space cleanly without scrolling or clipping on 1280x720 screens.
        for name in ("backup_step4_text2", "backup_step4_when_ready_label"):
            w = self._get_builder_object(name)
            if w is not None and hasattr(w, "set_visible"):
                w.set_visible(not is_active)
        tree_scrolled = getattr(self, "_diff_step4_images_scrolled", None)
        if tree_scrolled is not None and hasattr(tree_scrolled, "set_visible"):
            tree_scrolled.set_visible(not is_active)
        preview = None
        if is_active:
            try:
                preview = self.get_differential_partition_preview(candidate)
            except Exception:
                traceback.print_exc()
                preview = None
            preview_label = getattr(self, "diff_preview_label", None)
            if preview_label is not None:
                preview_label.set_markup(
                    "<tt>"
                    + GObject.markup_escape_text(
                        preview["summary_text"] if preview else ""
                    )
                    + "</tt>"
                )
        if self.diff_status_label is None:
            return
        if not is_active:
            text = _(
                "Full backup: seekable index + 1 MiB block-hash (.rzbhash), usable as a future differential base."
            )
        elif not candidates:
            text = _(
                "No full backup for the selected drive(s) was found in {folder}. Use Browse... to select one, or choose Full Backup."
            ).format(folder=self._get_backup_parent_dir())
        elif candidate is None:
            text = _("Select the full backup to use as the differential base.")
        else:
            notes = []
            if candidate.get("all_drifted"):
                drifted_map = ", ".join(
                    "{old} -> {new}".format(old=old or "?", new=dk)
                    for dk, old in (candidate.get("drifted_drives") or {}).items()
                )
                notes.append(
                    _(
                        "Cannot use for differential backup: drive device name changed ({mapping}). Choose Full Backup, or reboot if your drives enumerated in a different order."
                    ).format(mapping=drifted_map)
                )
            elif not candidate.get("has_any_bhash", True):
                notes.append(
                    _(
                        "This backup has no .rzbhash index yet: click 'Build Base Index (.rzbhash)...' (read-only on the original backup), or choose Full Backup."
                    )
                )
            elif candidate.get("needs_bhash"):
                notes.append(
                    _(
                        "{count} partition(s) of this base have no .rzbhash index yet ({size} to read). Click 'Build Base Index (.rzbhash)...' to index them, otherwise they are saved as full."
                    ).format(
                        count=candidate.get("missing_bhash_count", 0),
                        size=self._format_bytes(candidate.get("missing_bhash_bytes", 0)),
                    )
                )
            elif preview is not None:
                notes.append(
                    _("Ready: {diff} partition(s) differential, {full} full.").format(
                        diff=preview["diff_count"], full=preview["full_count"]
                    )
                )
            if candidate.get("missing_drives"):
                notes.append(
                    _(
                        "No base image for: {drives} (these drives get a full backup)."
                    ).format(drives=", ".join(candidate["missing_drives"]))
                )
            if candidate.get("device_name_drifted") and not candidate.get("all_drifted"):
                notes.append(
                    _(
                        "Drive name changed since this base was saved for some drive(s); affected drives will be saved as full."
                    )
                )
            text = " ".join(notes) or _(
                "Partitions that are incompatible with the base (resized, other filesystem, partclone.dd) are saved as full images automatically."
            )
        self.diff_status_label.set_text(text)

    def _on_differential_toggled(self, toggle_button):
        if toggle_button.get_active() and not (getattr(self, "diff_base_candidates", None) or []):
            self.refresh_differential_base_images()
        self._update_differential_widgets()

    def _on_differential_base_changed(self, combobox):
        self._update_differential_widgets()

    # ------------------------------------------------------------------
    # Live "Build Base Index (.rzbhash)..." (background thread)
    # ------------------------------------------------------------------

    @staticmethod
    def collect_missing_bhash_work(candidate):
        """Ordered, de-duplicated [(drive_key, base_dir, PtclImageSet), ...]."""
        work = []
        seen = set()
        for dk, items in ((candidate or {}).get("missing_bhash_by_drive") or {}).items():
            for base_dir, images in items or []:
                for img in images or []:
                    key = (os.path.normpath(base_dir), img.short_part)
                    if key in seen:
                        continue
                    seen.add(key)
                    work.append((dk, base_dir, img))
        return work

    def _on_build_base_index_clicked(self, button):
        if getattr(self, "diff_bhash_build_in_progress", False):
            return
        candidate = self._get_selected_differential_candidate()
        work = self.collect_missing_bhash_work(candidate)
        if not work:
            self._update_differential_widgets()
            return
        base_dirs = []
        for _dk, base_dir, _img in work:
            if base_dir not in base_dirs:
                base_dirs.append(base_dir)
        read_only = [d for d in base_dirs if not os.access(d, os.W_OK | os.X_OK)]
        if read_only:
            ErrorMessageModalPopup(
                self.builder,
                _(
                    "Cannot create the .rzbhash index: the base backup folder is read-only (or not writable):\n\n{folders}\n\nNothing was changed. Choose Full Backup, or a base backup on a writable drive."
                ).format(folders="\n".join(read_only)),
            )
            return
        total_bytes = 0
        for _dk, _base_dir, img in work:
            for path in img.split_paths:
                try:
                    total_bytes += os.path.getsize(path)
                except OSError:
                    pass
        message = _(
            "Build the differential base index for {count} partition(s) ({size} of backup data to read) in:\n\n{folders}\n\nOriginal backup split files are opened read-only and never modified. Only Info-bhash-<part>.rzbhash sidecar files will be created."
        ).format(
            count=len(work),
            size=self._format_bytes(total_bytes),
            folders="\n".join(base_dirs),
        )
        dialog = Gtk.MessageDialog(
            transient_for=self._get_builder_object("main_window"),
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=_("Build Base Index (.rzbhash)?"),
        )
        dialog.format_secondary_text(message)
        try:
            response = dialog.run()
        finally:
            dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        self._start_bhash_build(work, total_bytes)

    def _start_bhash_build(self, work, total_bytes):
        cancel_event = threading.Event()
        state = {
            "work": work,
            "total_bytes": max(int(total_bytes), 1),
            "done_before": 0,
            "start": time.monotonic(),
            "cancel_event": cancel_event,
            "built": [],
            "skipped": [],
            "errors": [],
            "cancelled": False,
            "selection_id": self._candidate_identity(
                self._get_selected_differential_candidate()
            ),
        }
        dialog = Gtk.Dialog(
            title=_("Building differential base index (.rzbhash)"),
            transient_for=self._get_builder_object("main_window"),
            modal=True,
        )
        dialog.set_default_size(560, -1)
        dialog.set_deletable(False)
        cancel_button = dialog.add_button(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL)
        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)
        part_label = Gtk.Label(label=_("Preparing…"))
        part_label.set_xalign(0.0)
        part_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        progress_bar = Gtk.ProgressBar()
        progress_bar.set_show_text(True)
        stats_label = Gtk.Label(label="")
        stats_label.set_xalign(0.0)
        note_label = Gtk.Label(
            label=_("Original backup files are only read, never modified.")
        )
        note_label.set_xalign(0.0)
        for widget in (part_label, progress_bar, stats_label, note_label):
            box.pack_start(widget, False, False, 0)
        state.update(
            dialog=dialog,
            part_label=part_label,
            progress_bar=progress_bar,
            stats_label=stats_label,
            cancel_button=cancel_button,
        )

        def _request_cancel(*_args):
            if not cancel_event.is_set():
                cancel_event.set()
                part_label.set_text(_("Cancelling… (finishing the current block)"))
                cancel_button.set_sensitive(False)
            return True  # keep the dialog until the worker has stopped

        dialog.connect("response", _request_cancel)
        dialog.connect("delete-event", _request_cancel)
        dialog.show_all()
        self._diff_bhash_build_state = state
        self.diff_bhash_build_in_progress = True
        self._update_differential_widgets()
        thread = threading.Thread(
            target=self._bhash_build_worker, args=(state,), name="rzbhash-build"
        )
        thread.daemon = True
        state["thread"] = thread
        thread.start()

    def _bhash_build_worker(self, state):
        """Background thread: never touches GTK, only queues GLib.idle_add updates."""
        work = state["work"]
        cancel_event = state["cancel_event"]
        total = len(work)
        try:
            for i, (_dk, base_dir, img) in enumerate(work, start=1):
                if cancel_event.is_set():
                    state["cancelled"] = True
                    break
                title = "[{i}/{n}] {part} ({fs})".format(
                    i=i, n=total, part=img.short_part, fs=img.fs
                )
                part_bytes = 0
                for path in img.split_paths:
                    try:
                        part_bytes += os.path.getsize(path)
                    except OSError:
                        pass
                base_done = state["done_before"]
                GLib.idle_add(self._bhash_build_progress_ui, state, title, 0, base_done)
                out_path = os.path.join(base_dir, block_hash.bhash_index_filename(img.short_part))
                seekidx = os.path.join(base_dir, "Info-seekidx-" + img.short_part + ".rzidx")

                def _progress(done, _total, _decompressed, _title=title, _base=base_done):
                    GLib.idle_add(self._bhash_build_progress_ui, state, _title, int(done), _base)

                try:
                    idx = block_hash.build_bhash_from_splits(
                        img.split_paths,
                        img.compression,
                        out_path,
                        seekidx_path=seekidx if os.path.isfile(seekidx) else None,
                        progress_cb=_progress,
                        cancel_event=cancel_event,
                    )
                except block_hash.BhashBuildCancelledError:
                    state["cancelled"] = True
                    break
                except Exception as exc:
                    traceback.print_exc()
                    state["errors"].append("{part}: {err}".format(part=img.short_part, err=exc))
                else:
                    if idx is None:
                        state["skipped"].append(img.short_part)
                    else:
                        state["built"].append(img.short_part)
                state["done_before"] = base_done + part_bytes
        except Exception as exc:
            traceback.print_exc()
            state["errors"].append(str(exc))
        finally:
            # Always close the dialog / re-enable the UI, whatever happened.
            GLib.idle_add(self._on_bhash_build_finished, state)

    def _bhash_build_progress_ui(self, state, title, current_done, done_before=0):
        """GTK main thread: refresh the progress dialog."""
        if state.get("finished"):
            return False
        done = int(done_before) + max(0, int(current_done))
        total = state["total_bytes"]
        fraction = min(1.0, done / float(total))
        elapsed = max(time.monotonic() - state["start"], 1e-6)
        rate = done / elapsed
        if not state["cancel_event"].is_set():
            state["part_label"].set_text(title)
        state["progress_bar"].set_fraction(fraction)
        state["progress_bar"].set_text("{:.0f}%".format(fraction * 100.0))
        eta = (total - done) / rate if rate > 0 else 0
        state["stats_label"].set_text(
            _("{done} / {total} read · {rate:.1f} MB/s · ETA {eta}").format(
                done=self._format_bytes(done),
                total=self._format_bytes(total),
                rate=rate / 1e6,
                eta="{:d}:{:02d}".format(int(eta) // 60, int(eta) % 60),
            )
        )
        return False

    def _on_bhash_build_finished(self, state):
        """GTK main thread: close the dialog, refresh candidates + preview."""
        state["finished"] = True
        self.diff_bhash_build_in_progress = False
        dialog = state.get("dialog")
        if dialog is not None:
            dialog.destroy()
        # Re-select the same base (now [Ready]) and refresh the preview.
        self.refresh_differential_base_images(preserve_selection=True)
        selection_id = state.get("selection_id")
        combobox = getattr(self, "diff_base_combobox", None)
        if selection_id is not None and combobox is not None:
            for i, candidate in enumerate(self.diff_base_candidates or []):
                if self._candidate_identity(candidate) == selection_id:
                    combobox.set_active(i)
                    break
        self._update_differential_widgets()
        summary = _("Built {built} .rzbhash index file(s).").format(built=len(state["built"]))
        if state["skipped"]:
            summary += " " + _("Skipped (not Partclone v0002): {parts}.").format(
                parts=", ".join(state["skipped"])
            )
        if state["cancelled"]:
            summary += " " + _("Cancelled: the remaining partitions were not indexed.")
        if state["errors"]:
            ErrorMessageModalPopup(
                self.builder,
                summary
                + "\n\n"
                + _("Errors (these partitions will be saved as full):")
                + "\n"
                + "\n".join(state["errors"]),
            )
        elif self.diff_status_label is not None:
            self.diff_status_label.set_text(
                summary + " " + self.diff_status_label.get_text()
            )
        return False

    def _build_custom_base_candidate(self, chosen_dir):
        """Candidate dict for a user-chosen base folder, or None if it is unusable."""
        if not chosen_dir or not os.path.isdir(chosen_dir):
            return None
        drive_keys = self._get_backup_drive_keys()
        drive_state = getattr(getattr(self, "drive_query", None), "drive_state", None) or {}
        parent_dir = os.path.dirname(os.path.normpath(chosen_dir))
        try:
            discovered = discover_compatible_base_images(
                parent_dir, drive_keys, drive_state, include_needs_bhash=True
            )
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
        if block_hash.is_differential_backup_dir_entries(entries):
            return None  # differential (or partial differential) backups are never a base
        status = block_hash.inspect_base_dir_bhash_status(chosen_dir)
        has_bhash_file = any(
            e.startswith("Info-bhash-") and e.endswith(".rzbhash") for e in entries
        )
        if not status["ptcl_parts"] and not has_bhash_file:
            return None
        has_any_bhash = bool(status["bhash_parts"]) if status["ptcl_parts"] else has_bhash_file
        needs = bool(status["missing_images"])
        name = os.path.basename(norm_chosen)
        state_text = _("Ready") if (has_any_bhash and not needs) else _("Needs .rzbhash index")
        # Not matched to a drive by name/disk file: offer it to every drive;
        # partitions without a matching .rzbhash fall back to full backups.
        # Record missing images for every selected drive so per-drive previews
        # reflect invalid/missing .rzbhash accurately (collect_missing_bhash_work
        # deduplicates by (base_dir, short_part)).
        missing_by_drive = {}
        if needs and drive_keys:
            for dk in drive_keys:
                missing_by_drive[dk] = [(chosen_dir, list(status["missing_images"]))]
        return {
            "label": "{name}  [{state}] {selected}".format(
                name=name, state=state_text, selected=_("(selected)")
            ),
            "name": name,
            "base_dir": chosen_dir,
            "base_dirs_by_drive": {dk: chosen_dir for dk in drive_keys},
            "missing_drives": [],
            "matched_partitions": {},
            "is_multi_drive": len(drive_keys) > 1,
            "mtime": 0.0,
            "has_any_bhash": has_any_bhash,
            "needs_bhash": needs,
            "fully_indexed": not needs,
            "ready": has_any_bhash and not needs,
            "missing_bhash_by_drive": missing_by_drive,
            "missing_bhash_count": len(status["missing_images"]),
            "missing_bhash_bytes": int(status["total_missing_compressed_bytes"]),
            "device_name_drifted": False,
            "all_drifted": False,
            "drifted_drives": {},
            "base_parts_by_drive": {
                dk: sorted(set(status["ptcl_parts"]) | set(status["bhash_parts"]))
                for dk in drive_keys
            },
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
                    "The selected folder cannot be used as a differential base: {folder}\n\nA base must be a full Rescuezilla / Clonezilla / SeekZilla backup containing Partclone images (.gz, .zst or .uncomp split files) and must not be a differential backup itself."
                ).format(folder=chosen_dir),
            )
            return
        self.diff_custom_dir = chosen_dir
        self.diff_custom_candidate = candidate
        self._set_differential_mode(True)
        self.refresh_differential_base_images(preserve_selection=False)
        if self.diff_base_combobox is not None and self.diff_base_candidates:
            self.diff_base_combobox.set_active(0)
        self._update_differential_widgets()

    def get_differential_backup_selection(self):
        """Return (candidate_dict_or_None, error_message_or_None).

        Full Backup -> (None, None). Differential -> the selected candidate, or
        an actionable error when no base is selected or the base has no
        `.rzbhash` at all yet (a partially indexed base is allowed: its
        unindexed partitions are saved as full, as shown in the preview).
        """
        checkbutton = getattr(self, "diff_checkbutton", None)
        if checkbutton is None or not checkbutton.get_active():
            return None, None
        candidate = self._get_selected_differential_candidate()
        if candidate is None:
            return None, _(
                "Differential Backup is selected, but no full base backup is selected.\n\nSelect a full backup from the list (or use Browse...), or choose Full Backup."
            )
        if candidate.get("all_drifted"):
            drifted_map = ", ".join(
                "{old} -> {new}".format(old=old or "?", new=dk)
                for dk, old in (candidate.get("drifted_drives") or {}).items()
            )
            return None, _(
                "This backup was saved when the drive had a different device name ({mapping}).\n\nDifferential backup requires matching partition names. Choose Full Backup, or reboot if your drives enumerated in a different order."
            ).format(mapping=drifted_map)
        if not candidate.get("has_any_bhash", True):
            return None, _(
                "The selected backup does not have a differential base index (Info-bhash-*.rzbhash) yet.\n\nClick 'Build Base Index (.rzbhash)...' on this screen to generate it now (read-only on your original backup), or choose Full Backup."
            )
        return candidate, None

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
        else:
            try:
                preview = self.get_differential_partition_preview(selection)
            except Exception:
                traceback.print_exc()
                preview = None
            if preview is not None:
                text += (
                    "\n<b>"
                    + GObject.markup_escape_text(_("Per-partition plan:"))
                    + "</b>\n<tt>"
                    + GObject.markup_escape_text(preview["summary_text"])
                    + "</tt>"
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
