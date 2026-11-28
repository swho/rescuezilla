# ----------------------------------------------------------------------
#   Copyright (C) 2026 Rescuezilla Seekable-Index Community Fork Contributors
#   Added 2026-11-28 (Community Fork): tests for the live USB
#   seekzilla-set-display-mode 4K / HiDPI readability script.
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
import importlib.machinery
import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# test/ -> rescuezilla -> dist-packages -> python3 -> lib -> usr -> rescuezilla
#       -> rescuezilla -> apps -> src
_REPO_SCRIPT = (
    Path(__file__).resolve().parents[9]
    / "livecd"
    / "chroot"
    / "usr"
    / "local"
    / "bin"
    / "seekzilla-set-display-mode"
)
SCRIPT_PATH = (
    _REPO_SCRIPT
    if _REPO_SCRIPT.is_file()
    else Path("/usr/local/bin/seekzilla-set-display-mode")
)


def _load_script():
    loader = importlib.machinery.SourceFileLoader("seekzilla_set_display_mode", str(SCRIPT_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    # Never drop a __pycache__/ into the live chroot tree (it would ship in the ISO).
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = saved
    return module


sdm = _load_script()


# ----------------------------------------------------------------------
# Realistic `xrandr --query` fixtures
# ----------------------------------------------------------------------
XRANDR_4K_MONITOR = """\
Screen 0: minimum 320 x 200, current 3840 x 2160, maximum 16384 x 16384
HDMI-1 connected primary 3840x2160+0+0 (normal left inverted right x axis y axis) 600mm x 340mm
   3840x2160     60.00*+  30.00    29.97    25.00    24.00    23.98
   2560x1440     59.95
   1920x1200     59.88
   1920x1080     60.00    50.00    59.94    30.00    25.00    24.00    29.97    23.98
   1920x1080i    60.00    50.00    59.94
   1600x900      60.00
   1280x1024     75.02    60.02
   1280x800      59.81
   1280x720      60.00    50.00    59.94
   1024x768      75.03    60.00
   800x600       75.00    60.32
   720x400       70.08
DP-1 disconnected (normal left inverted right x axis y axis)
DP-2 disconnected (normal left inverted right x axis y axis)
"""

XRANDR_4K_16_10_LAPTOP = """\
Screen 0: minimum 320 x 200, current 3840 x 2400, maximum 16384 x 16384
eDP-1 connected 3840x2400+0+0 (normal left inverted right x axis y axis) 302mm x 189mm
   3840x2400     60.00*+  59.99    59.97
   3200x2000     59.96    59.98
   2880x1800     59.95    59.88
   2560x1600     59.99    59.97
   1920x1440     60.00
   1920x1200     59.88    59.95
   1920x1080     60.01    59.97    59.96    59.93
   1680x1050     59.95    59.88
   1280x800      59.81    59.91
   1280x720      60.00    59.99
HDMI-1 disconnected (normal left inverted right x axis y axis)
"""

XRANDR_QHD = """\
Screen 0: minimum 320 x 200, current 2560 x 1440, maximum 16384 x 16384
DP-1 connected primary 2560x1440+0+0 (normal left inverted right x axis y axis) 597mm x 336mm
   2560x1440     143.97 + 120.00*  59.95
   1920x1200     59.95
   1920x1080     119.88   60.00    50.00    59.94
   1600x900      60.00
   1280x720      60.00    50.00    59.94
   640x480       59.94
"""

XRANDR_SINGLE_MODE_4K_EDP = """\
Screen 0: minimum 320 x 200, current 3840 x 2160, maximum 16384 x 16384
eDP-1 connected primary 3840x2160+0+0 (normal left inverted right x axis y axis) 344mm x 194mm
   3840x2160     60.00*+
"""

XRANDR_1080P = """\
Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 16384 x 16384
HDMI-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 527mm x 296mm
   1920x1080     60.00*+  50.00    59.94
   1280x720      60.00    50.00    59.94
"""

XRANDR_VMWARE = """\
Screen 0: minimum 1 x 1, current 1280 x 800, maximum 16384 x 16384
Virtual1 connected primary 1280x800+0+0 (normal left inverted right x axis y axis) 0mm x 0mm
   1280x800      59.81*+
   3840x2400     59.97
   2560x1600     59.99
   1920x1200     59.88
   1280x720      59.86
Virtual2 disconnected (normal left inverted right x axis y axis)
"""

XRANDR_720P = """\
Screen 0: minimum 320 x 200, current 1280 x 720, maximum 8192 x 8192
HDMI-1 connected 1280x720+0+0 (normal left inverted right x axis y axis) 0mm x 0mm
   1920x1080     60.00 +
   1280x720      60.00*
"""

XRANDR_MULTI = """\
Screen 0: minimum 320 x 200, current 5760 x 2160, maximum 16384 x 16384
DP-1 connected primary 3840x2160+0+0 (normal left inverted right x axis y axis) 600mm x 340mm
   3840x2160     60.00*+  30.00
   1920x1080     60.00    59.94
   1280x720      60.00
HDMI-1 connected 1920x1080+3840+0 (normal left inverted right x axis y axis) 527mm x 296mm
   1920x1080     60.00*+  50.00
   1280x720      60.00
DP-2 disconnected (normal left inverted right x axis y axis)
"""


class FakeRunner:
    """Records calls; returns (rc, out, err) via a callback."""

    def __init__(self, query_text="", query_rc=0, fail=None):
        self.calls = []
        self.query_text = query_text
        self.query_rc = query_rc
        self.fail = fail or (lambda argv: False)

    def __call__(self, argv, input_text=None, timeout=5):
        self.calls.append((list(argv), input_text))
        if argv[:2] == ["xrandr", "--query"]:
            return self.query_rc, self.query_text, "" if self.query_rc == 0 else "Can't open display"
        if self.fail(argv):
            return 1, "", "simulated failure"
        return 0, "", ""

    def changes(self):
        return [c for c in self.calls if c[0][:2] != ["xrandr", "--query"]]


class TestScriptFile(unittest.TestCase):
    def test_executable_0755(self):
        mode = stat.S_IMODE(os.stat(SCRIPT_PATH).st_mode)
        self.assertEqual(mode, 0o755)

    def test_shebang_and_license(self):
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/usr/bin/env python3\n"))
        self.assertIn("either version 3 of the License, or", text)
        self.assertIn("(at your option) any later version.", text)


class TestParseXrandrQuery(unittest.TestCase):
    def test_parse_4k(self):
        outputs = sdm.parse_xrandr_query(XRANDR_4K_MONITOR)
        self.assertEqual([o["name"] for o in outputs], ["HDMI-1", "DP-1", "DP-2"])
        hdmi = outputs[0]
        self.assertTrue(hdmi["connected"])
        self.assertTrue(hdmi["primary"])
        self.assertEqual((hdmi["width"], hdmi["height"], hdmi["x"], hdmi["y"]), (3840, 2160, 0, 0))
        self.assertEqual(hdmi["rotation"], "normal")
        first = hdmi["modes"][0]
        self.assertEqual(first["name"], "3840x2160")
        self.assertTrue(first["current"])
        self.assertTrue(first["preferred"])
        self.assertEqual(first["rates"], ["60.00", "30.00", "29.97", "25.00", "24.00", "23.98"])
        interlaced = [m for m in hdmi["modes"] if m["interlaced"]]
        self.assertEqual([m["name"] for m in interlaced], ["1920x1080i"])
        self.assertFalse(outputs[1]["connected"])
        self.assertIsNone(outputs[1]["width"])
        self.assertEqual(outputs[1]["modes"], [])
        self.assertEqual(sdm.current_mode_name(hdmi), "3840x2160")

    def test_parse_edp_without_primary(self):
        outputs = sdm.parse_xrandr_query(XRANDR_4K_16_10_LAPTOP)
        edp = outputs[0]
        self.assertFalse(edp["primary"])
        self.assertEqual((edp["width"], edp["height"]), (3840, 2400))

    def test_parse_separate_plus_and_star(self):
        outputs = sdm.parse_xrandr_query(XRANDR_QHD)
        mode = outputs[0]["modes"][0]
        self.assertTrue(mode["preferred"])
        self.assertTrue(mode["current"])
        self.assertEqual(mode["rates"], ["143.97", "120.00", "59.95"])

    def test_parse_multi(self):
        outputs = sdm.parse_xrandr_query(XRANDR_MULTI)
        hdmi = outputs[1]
        self.assertEqual((hdmi["x"], hdmi["y"]), (3840, 0))
        self.assertEqual(len(sdm.active_outputs(outputs)), 2)

    def test_parse_rotated(self):
        text = ("eDP-1 connected primary 2160x3840+0+0 left (normal left inverted right x axis y axis) 1mm x 1mm\n"
                "   3840x2160     60.00*+\n")
        out = sdm.parse_xrandr_query(text)[0]
        self.assertEqual(out["rotation"], "left")
        self.assertIsNone(sdm.choose_target_action(out, "1080p"))

    def test_parse_garbage(self):
        self.assertEqual(sdm.parse_xrandr_query(""), [])
        self.assertEqual(sdm.parse_xrandr_query(None), [])
        self.assertEqual(sdm.parse_xrandr_query("Can't open display :0\n\x00\x01 junk\n   1920x1080 60.00*\n"), [])

    def test_connected_but_inactive_output(self):
        text = ("HDMI-1 connected (normal left inverted right x axis y axis)\n"
                "   3840x2160     60.00 +\n"
                "   1920x1080     60.00\n")
        outputs = sdm.parse_xrandr_query(text)
        self.assertTrue(outputs[0]["connected"])
        self.assertEqual(sdm.active_outputs(outputs), [])
        self.assertEqual(sdm.plan_actions(outputs, "1080p"), [])


class TestReadDisplayPreference(unittest.TestCase):
    def test_default(self):
        self.assertEqual(sdm.read_display_preference({}, "BOOT_IMAGE=/casper/vmlinuz quiet splash")[0], "1080p")

    def test_cmdline(self):
        self.assertEqual(sdm.read_display_preference({}, "quiet seekzilla_display=720p splash")[0], "720p")
        self.assertEqual(sdm.read_display_preference({}, "seekzilla_display=native")[0], "native")
        self.assertEqual(sdm.read_display_preference({}, "seekzilla_display=off")[0], "native")
        self.assertEqual(sdm.read_display_preference({}, "seekzilla_display=0")[0], "native")
        self.assertEqual(sdm.read_display_preference({}, "seekzilla_display=1080p")[0], "1080p")

    def test_env_beats_cmdline(self):
        env = {"SEEKZILLA_DISPLAY_MODE": "native"}
        self.assertEqual(sdm.read_display_preference(env, "seekzilla_display=720p")[0], "native")
        env = {"SEEKZILLA_DISPLAY_MODE": " 720P "}
        self.assertEqual(sdm.read_display_preference(env, "")[0], "720p")

    def test_empty_env_falls_through_to_cmdline(self):
        env = {"SEEKZILLA_DISPLAY_MODE": ""}
        self.assertEqual(sdm.read_display_preference(env, "seekzilla_display=720p")[0], "720p")

    def test_unrecognised_uses_default(self):
        self.assertEqual(sdm.read_display_preference({}, "seekzilla_display=banana")[0], "1080p")
        self.assertEqual(sdm.read_display_preference({"SEEKZILLA_DISPLAY_MODE": "4k"}, "")[0], "1080p")


class TestChooseTargetAction(unittest.TestCase):
    def _action(self, text, pref="1080p", idx=0):
        return sdm.choose_target_action(sdm.parse_xrandr_query(text)[idx], pref)

    def test_4k_default_picks_1080p(self):
        a = self._action(XRANDR_4K_MONITOR)
        self.assertEqual((a["kind"], a["mode"], a["logical"]), ("mode", "1920x1080", (1920, 1080)))

    def test_4k_720p_picks_1280x720(self):
        a = self._action(XRANDR_4K_MONITOR, "720p")
        self.assertEqual((a["kind"], a["mode"]), ("mode", "1280x720"))

    def test_4k_plus_16_10_picks_1920x1200(self):
        a = self._action(XRANDR_4K_16_10_LAPTOP)
        self.assertEqual((a["kind"], a["mode"]), ("mode", "1920x1200"))

    def test_qhd_picks_1080p(self):
        a = self._action(XRANDR_QHD)
        self.assertEqual((a["kind"], a["mode"]), ("mode", "1920x1080"))

    def test_single_mode_4k_edp_scales_half(self):
        a = self._action(XRANDR_SINGLE_MODE_4K_EDP)
        self.assertEqual(a["kind"], "scale")
        self.assertEqual(a["scale"], 0.5)
        self.assertEqual(a["mode"], "3840x2160")
        self.assertEqual(a["logical"], (1920, 1080))

    def test_within_cap_noop(self):
        for text in (XRANDR_1080P, XRANDR_VMWARE, XRANDR_720P):
            self.assertIsNone(self._action(text, "1080p"))
        for text in (XRANDR_VMWARE, XRANDR_720P):
            self.assertIsNone(self._action(text, "720p"))

    def test_never_upgrades_into_larger_mode(self):
        # 1366x768 under 720p: 1920x1080 is in the 720p list but larger.
        text = ("HDMI-1 connected 1366x768+0+0 (normal) 0mm x 0mm\n"
                "   1920x1080     60.00 +\n"
                "   1366x768      60.00*\n")
        self.assertIsNone(self._action(text, "720p"))

    def test_fallback_highest_area_non_interlaced(self):
        text = ("DP-1 connected 3840x2160+0+0 (normal) 0mm x 0mm\n"
                "   3840x2160     60.00*+\n"
                "   1792x1008i    60.00\n"
                "   1720x968      60.00\n"
                "   1440x1080     60.00\n")
        a = self._action(text)
        self.assertEqual((a["kind"], a["mode"]), ("mode", "1720x968"))

    def test_fallback_interlaced_only_if_nothing_else(self):
        text = ("DP-1 connected 3840x2160+0+0 (normal) 0mm x 0mm\n"
                "   3840x2160     60.00*+\n"
                "   1920x1080i    60.00\n")
        a = self._action(text)
        self.assertEqual((a["kind"], a["mode"]), ("mode", "1920x1080i"))

    def test_compute_scale(self):
        self.assertEqual(sdm.compute_scale(3840, 2160), 0.5)
        self.assertEqual(sdm.compute_scale(5120, 2880), 0.5)
        self.assertEqual(sdm.compute_scale(3200, 1800), 0.5)
        self.assertEqual(sdm.compute_scale(2560, 1440), 0.75)
        self.assertEqual(sdm.compute_scale(2880, 1800), round(1920.0 / 2880, 4))
        self.assertEqual(sdm.compute_scale(2400, 1600), 0.8)
        self.assertEqual(sdm.compute_scale(3000, 2000), 0.64)
        self.assertEqual(sdm.compute_scale(1920, 1440), 0.8333)
        self.assertEqual(sdm.format_scale(0.5), "0.5x0.5")
        self.assertEqual(sdm.format_scale(0.6667), "0.6667x0.6667")

    def test_ultrawide_21_9_picks_2560x1080(self):
        text = (
            "DP-1 connected primary 3440x1440+0+0 (normal) 800mm x 335mm\n"
            "   3440x1440     60.00*+\n"
            "   2560x1080     60.00\n"
            "   1920x1080     60.00\n"
        )
        a = self._action(text, "1080p")
        self.assertEqual((a["kind"], a["mode"]), ("mode", "2560x1080"))

    def test_super_ultrawide_32_9_picks_3840x1080(self):
        text = (
            "DP-1 connected primary 5120x1440+0+0 (normal) 1190mm x 335mm\n"
            "   5120x1440     60.00*+\n"
            "   3840x1080     60.00\n"
            "   1920x1080     60.00\n"
        )
        a = self._action(text, "1080p")
        self.assertEqual((a["kind"], a["mode"]), ("mode", "3840x1080"))

    def test_ultrawide_1080p_height_is_noop(self):
        text = (
            "DP-1 connected primary 2560x1080+0+0 (normal) 800mm x 335mm\n"
            "   2560x1080     60.00*+\n"
            "   1920x1080     60.00\n"
        )
        self.assertIsNone(self._action(text, "1080p"))

    def test_native_pref_returns_none(self):
        self.assertIsNone(self._action(XRANDR_4K_MONITOR, "native"))


class TestBuildXrandrCommands(unittest.TestCase):
    def test_single_output_mode(self):
        outputs = sdm.parse_xrandr_query(XRANDR_4K_MONITOR)
        cmds = sdm.build_xrandr_commands(outputs, sdm.plan_actions(outputs, "1080p"))
        self.assertEqual(cmds, [["xrandr", "--output", "HDMI-1", "--mode", "1920x1080"]])

    def test_single_output_scale(self):
        outputs = sdm.parse_xrandr_query(XRANDR_SINGLE_MODE_4K_EDP)
        cmds = sdm.build_xrandr_commands(outputs, sdm.plan_actions(outputs, "1080p"))
        self.assertEqual(cmds, [["xrandr", "--output", "eDP-1", "--mode", "3840x2160", "--scale", "0.5x0.5"]])

    def test_multi_monitor_non_overlapping(self):
        outputs = sdm.parse_xrandr_query(XRANDR_MULTI)
        actions = sdm.plan_actions(outputs, "1080p")
        self.assertEqual([(a["output"], a["mode"]) for a in actions], [("DP-1", "1920x1080")])
        cmds = sdm.build_xrandr_commands(outputs, actions)
        self.assertEqual(cmds, [[
            "xrandr",
            "--output", "DP-1", "--mode", "1920x1080", "--pos", "0x0",
            "--output", "HDMI-1", "--pos", "1920x0",
        ]])

    def test_multi_monitor_secondary_on_left(self):
        text = XRANDR_MULTI.replace("3840x2160+0+0", "3840x2160+1920+0").replace("1920x1080+3840+0", "1920x1080+0+0")
        outputs = sdm.parse_xrandr_query(text)
        cmds = sdm.build_xrandr_commands(outputs, sdm.plan_actions(outputs, "1080p"))
        self.assertEqual(cmds, [[
            "xrandr",
            "--output", "HDMI-1", "--pos", "0x0",
            "--output", "DP-1", "--mode", "1920x1080", "--pos", "1920x0",
        ]])

    def test_multi_monitor_cloned_preserved(self):
        text = XRANDR_MULTI.replace("1920x1080+3840+0", "1920x1080+0+0")
        outputs = sdm.parse_xrandr_query(text)
        cmds = sdm.build_xrandr_commands(outputs, sdm.plan_actions(outputs, "1080p"))
        self.assertEqual(cmds[0][1:7], ["--output", "DP-1", "--mode", "1920x1080", "--pos", "0x0"])
        self.assertEqual(cmds[0][7:], ["--output", "HDMI-1", "--pos", "0x0"])

    def test_no_actions_no_commands(self):
        outputs = sdm.parse_xrandr_query(XRANDR_MULTI)
        self.assertEqual(sdm.build_xrandr_commands(outputs, []), [])


class TestApplyDisplayMode(unittest.TestCase):
    def test_4k_applies_mode(self):
        r = FakeRunner(XRANDR_4K_MONITOR)
        res = sdm.apply_display_mode("1080p", runner=r)
        self.assertEqual(res["status"], "mode")
        self.assertEqual(r.changes(), [(["xrandr", "--output", "HDMI-1", "--mode", "1920x1080"], None)])

    def test_4k_720p_applies_mode(self):
        r = FakeRunner(XRANDR_4K_MONITOR)
        res = sdm.apply_display_mode("720p", runner=r)
        self.assertEqual(res["status"], "mode")
        self.assertEqual(r.changes(), [(["xrandr", "--output", "HDMI-1", "--mode", "1280x720"], None)])

    def test_single_mode_edp_applies_scale(self):
        r = FakeRunner(XRANDR_SINGLE_MODE_4K_EDP)
        res = sdm.apply_display_mode("1080p", runner=r)
        self.assertEqual(res["status"], "scale")
        self.assertEqual(r.changes()[0][0][-2:], ["--scale", "0.5x0.5"])

    def test_mode_failure_falls_back_to_scale(self):
        r = FakeRunner(XRANDR_4K_MONITOR, fail=lambda argv: "--mode" in argv and "--scale" not in argv)
        res = sdm.apply_display_mode("1080p", runner=r)
        self.assertEqual(res["status"], "scale")
        self.assertEqual(r.changes()[-1][0], ["xrandr", "--output", "HDMI-1", "--mode", "3840x2160", "--scale", "0.5x0.5"])

    def test_scale_failure_falls_back_to_xrdb_192(self):
        r = FakeRunner(XRANDR_SINGLE_MODE_4K_EDP, fail=lambda argv: argv[0] == "xrandr")
        with tempfile.TemporaryDirectory() as td:
            xr = os.path.join(td, ".Xresources")
            st_file = os.path.join(td, "seekzilla-xft-dpi")
            with open(xr, "w", encoding="utf-8") as f:
                f.write("Xft.dpi: 100\nXft.antialias: true\n")
            res = sdm.apply_display_mode(
                "1080p", runner=r, xresources_path=xr, dpi_state_file=st_file
            )
            self.assertEqual(res["status"], "xrdb")
            self.assertEqual(res["dpi"], 192)
            self.assertTrue(res["dpi_state_written"])
            self.assertTrue(res["xresources_updated"])
            self.assertEqual(r.changes()[-1], (["xrdb", "-merge"], "Xft.dpi: 192\n"))
            with open(st_file, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "Xft.dpi: 192\n")
            with open(xr, "r", encoding="utf-8") as f:
                xr_text = f.read()
            self.assertIn("Xft.dpi: 192", xr_text)
            self.assertNotIn("Xft.dpi: 100", xr_text)
            self.assertIn("Xft.antialias: true", xr_text)

    def test_tier3_dpi_state_works_even_when_xresources_unwritable_and_xsession_reapplies(self):
        """Simulate root-owned ~/.Xresources (EACCES) before rc-local chown; Xsession.d drop-in re-applies DPI."""
        r = FakeRunner(XRANDR_SINGLE_MODE_4K_EDP, fail=lambda argv: argv[0] == "xrandr")
        with tempfile.TemporaryDirectory() as td:
            unwritable_xr = os.path.join(td, "no_such_dir", ".Xresources")
            st_file = os.path.join(td, "seekzilla-xft-dpi")
            res = sdm.apply_display_mode(
                "1080p", runner=r, xresources_path=unwritable_xr, dpi_state_file=st_file
            )
            self.assertEqual(res["status"], "xrdb")
            self.assertTrue(res["dpi_state_written"])
            self.assertFalse(res["xresources_updated"])
            with open(st_file, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "Xft.dpi: 192\n")

            # Verify the actual Xsession.d drop-in script re-merges the DPI file cleanly.
            dropin = (
                Path(__file__).resolve().parents[9]
                / "livecd"
                / "chroot"
                / "etc"
                / "X11"
                / "Xsession.d"
                / "35seekzilla-hidpi"
            )
            if dropin.is_file():
                self.assertRegex(dropin.name, r"^[A-Za-z0-9_-]+$")
                bin_dir = os.path.join(td, "bin")
                os.mkdir(bin_dir)
                out_log = os.path.join(td, "xrdb_called.txt")
                stub = os.path.join(bin_dir, "xrdb")
                with open(stub, "w", encoding="utf-8") as f:
                    f.write(
                        f"#!/bin/sh\necho \"$@\" > {out_log}\ncat \"$2\" >> {out_log}\n"
                    )
                os.chmod(stub, 0o755)
                env = dict(os.environ)
                env["XDG_RUNTIME_DIR"] = td
                env["PATH"] = bin_dir + ":" + env.get("PATH", "/usr/bin:/bin")
                proc = subprocess.run(
                    ["/bin/sh", "-c", f'. "{dropin}"'],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                with open(out_log, "r", encoding="utf-8") as f:
                    logged = f.read()
                self.assertIn("-merge", logged)
                self.assertIn("Xft.dpi: 192", logged)

    def test_xresources_symlink_refused(self):
        with tempfile.TemporaryDirectory() as td:
            real_f = os.path.join(td, "real")
            link_f = os.path.join(td, "link")
            with open(real_f, "w", encoding="utf-8") as f:
                f.write("Xft.dpi: 100\n")
            os.symlink(real_f, link_f)
            self.assertFalse(sdm.update_xresources_dpi(192, path=link_f))
            with open(real_f, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "Xft.dpi: 100\n")

    def test_mode_and_scale_failure_falls_back_to_xrdb(self):
        r = FakeRunner(XRANDR_4K_MONITOR, fail=lambda argv: argv[0] == "xrandr")
        with tempfile.TemporaryDirectory() as td:
            xr = os.path.join(td, ".Xresources")
            st_file = os.path.join(td, "seekzilla-xft-dpi")
            res = sdm.apply_display_mode(
                "1080p", runner=r, xresources_path=xr, dpi_state_file=st_file
            )
            self.assertEqual(res["status"], "xrdb")
            self.assertEqual([c[0][0] for c in r.changes()], ["xrandr", "xrandr", "xrdb"])
            self.assertEqual(r.changes()[-1][1], "Xft.dpi: 192\n")

    def test_xrdb_144_for_qhd(self):
        r = FakeRunner(XRANDR_QHD, fail=lambda argv: argv[0] == "xrandr")
        with tempfile.TemporaryDirectory() as td:
            xr = os.path.join(td, ".Xresources")
            st_file = os.path.join(td, "seekzilla-xft-dpi")
            res = sdm.apply_display_mode(
                "1080p", runner=r, xresources_path=xr, dpi_state_file=st_file
            )
            self.assertEqual(res["status"], "xrdb")
            self.assertEqual(r.changes()[-1], (["xrdb", "-merge"], "Xft.dpi: 144\n"))

    def test_everything_fails_still_returns(self):
        r = FakeRunner(XRANDR_4K_MONITOR, fail=lambda argv: True)
        res = sdm.apply_display_mode("1080p", runner=r)
        self.assertEqual(res["status"], "failed")

    def test_no_change_cases(self):
        cases = [(XRANDR_1080P, "1080p"), (XRANDR_VMWARE, "1080p"), (XRANDR_720P, "1080p"),
                 (XRANDR_VMWARE, "720p"), (XRANDR_720P, "720p")]
        for text, pref in cases:
            with self.subTest(output=text.splitlines()[1], pref=pref):
                r = FakeRunner(text)
                res = sdm.apply_display_mode(pref, runner=r)
                self.assertEqual(res["status"], "noop")
                self.assertEqual(r.changes(), [])

    def test_native_override_noop(self):
        r = FakeRunner(XRANDR_4K_MONITOR)
        res = sdm.apply_display_mode("native", runner=r)
        self.assertEqual(res["status"], "native")
        self.assertEqual(r.calls, [])

    def test_multi_monitor_single_command(self):
        r = FakeRunner(XRANDR_MULTI)
        res = sdm.apply_display_mode("1080p", runner=r)
        self.assertEqual(res["status"], "mode")
        self.assertEqual(len(r.changes()), 1)
        cmd = r.changes()[0][0]
        # Placement is via explicit --pos using the new logical widths.
        self.assertEqual(cmd[:7], ["xrandr", "--output", "DP-1", "--mode", "1920x1080", "--pos", "0x0"])
        self.assertEqual(cmd[cmd.index("HDMI-1") + 1:], ["--pos", "1920x0"])

    def test_empty_and_malformed_xrandr(self):
        for text in ("", "garbage\nmore garbage\n", "Screen 0: minimum 1 x 1\n"):
            r = FakeRunner(text)
            res = sdm.apply_display_mode("1080p", runner=r)
            self.assertEqual(res["status"], "noop")
            self.assertEqual(r.changes(), [])

    def test_xrandr_query_fails(self):
        r = FakeRunner("", query_rc=1)
        res = sdm.apply_display_mode("1080p", runner=r)
        self.assertEqual(res["status"], "no-xrandr")
        self.assertEqual(r.changes(), [])

    def test_missing_binary_via_default_runner(self):
        rc, _out, _err = sdm.default_runner(["/nonexistent/seekzilla-no-such-binary"])
        self.assertEqual(rc, 127)

    def test_runner_raising_is_contained(self):
        def boom(argv, input_text=None, timeout=5):
            raise RuntimeError("kaboom")
        res = sdm.apply_display_mode("1080p", runner=boom)
        self.assertEqual(res["status"], "error")

    def test_dry_run_changes_nothing(self):
        r = FakeRunner(XRANDR_4K_MONITOR)
        res = sdm.apply_display_mode("1080p", runner=r, dry_run=True)
        self.assertEqual(res["status"], "dry-run")
        self.assertEqual(res["commands"], [["xrandr", "--output", "HDMI-1", "--mode", "1920x1080"]])
        self.assertEqual(r.changes(), [])


class TestMainExitCodes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._log_path = sdm.LOG_PATH
        sdm.LOG_PATH = os.path.join(self._tmp.name, "seekzilla-display.log")
        self._env = os.environ.get("SEEKZILLA_DISPLAY_MODE")
        os.environ.pop("SEEKZILLA_DISPLAY_MODE", None)

    def tearDown(self):
        sdm.LOG_PATH = self._log_path
        self._tmp.cleanup()
        if self._env is not None:
            os.environ["SEEKZILLA_DISPLAY_MODE"] = self._env

    def _fixture(self, text):
        path = os.path.join(self._tmp.name, "xrandr.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_runtime_always_exit_zero_with_missing_xrandr(self):
        r = FakeRunner("", query_rc=127)
        self.assertEqual(sdm.main(["--mode", "1080p"], runner=r), 0)

    def test_runtime_exit_zero_when_runner_raises(self):
        def boom(argv, input_text=None, timeout=5):
            raise OSError("no X")
        self.assertEqual(sdm.main([], runner=boom), 0)

    def test_runtime_exit_zero_on_bad_args(self):
        self.assertEqual(sdm.main(["--bogus", "--xrandr-file"], runner=FakeRunner("")), 0)

    def test_check_flag(self):
        self.assertEqual(sdm.main(["--check", "--mode", "1080p", "--xrandr-file", self._fixture(XRANDR_4K_MONITOR)]), 1)
        self.assertEqual(sdm.main(["--check", "--mode", "1080p", "--xrandr-file", self._fixture(XRANDR_1080P)]), 0)
        self.assertEqual(sdm.main(["--check", "--mode", "native", "--xrandr-file", self._fixture(XRANDR_4K_MONITOR)]), 0)

    def test_subprocess_without_display_exits_zero(self):
        env = dict(os.environ)
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
        env["PATH"] = self._tmp.name  # no xrandr on PATH
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["SEEKZILLA_DISPLAY_LOG"] = os.path.join(self._tmp.name, "subproc.log")
        proc = subprocess.run([sys.executable, str(SCRIPT_PATH)], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.isfile(env["SEEKZILLA_DISPLAY_LOG"]))


if __name__ == "__main__":
    unittest.main()
