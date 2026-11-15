"""Crash-marker guard tests: the thing that caused the safe-mode dialog loop."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import obs_controller


class CrashFlagTests(unittest.TestCase):
    def test_path_is_under_appdata(self) -> None:
        path = obs_controller.crash_flag_path(r"C:\Users\me\AppData\Roaming")
        self.assertEqual(path, Path(r"C:\Users\me\AppData\Roaming") / "obs-studio" / ".sentinel")

    def test_missing_marker_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(obs_controller.clear_crash_flag(tmp))

    def _no_obs_running(self):
        original = obs_controller.is_obs_running
        obs_controller.is_obs_running = lambda: False
        return original

    def test_directory_marker_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = obs_controller.crash_flag_path(tmp)
            marker.mkdir(parents=True)
            original = self._no_obs_running()
            try:
                self.assertTrue(obs_controller.clear_crash_flag(tmp))
            finally:
                obs_controller.is_obs_running = original
            self.assertFalse(marker.exists())

    def test_file_marker_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = obs_controller.crash_flag_path(tmp)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("crash", encoding="utf-8")
            original = self._no_obs_running()
            try:
                self.assertTrue(obs_controller.clear_crash_flag(tmp))
            finally:
                obs_controller.is_obs_running = original
            self.assertFalse(marker.exists())

    def test_running_obs_keeps_its_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = obs_controller.crash_flag_path(tmp)
            marker.mkdir(parents=True)
            original = obs_controller.is_obs_running
            obs_controller.is_obs_running = lambda: True
            try:
                self.assertFalse(obs_controller.clear_crash_flag(tmp))
                self.assertTrue(marker.exists())
            finally:
                obs_controller.is_obs_running = original


class FindObsExeTests(unittest.TestCase):
    """A winget / per-user install never touches PATH or Program Files."""

    def test_winget_package_layout_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            exe = (
                local
                / "Microsoft"
                / "WinGet"
                / "Packages"
                / "OBSProject.OBSStudio_Microsoft.Winget.Source_8wekyb3d8bbwe"
                / "bin"
                / "64bit"
                / "obs64.exe"
            )
            exe.parent.mkdir(parents=True)
            exe.write_text("", encoding="utf-8")

            with mock.patch.dict(
                os.environ,
                {"LOCALAPPDATA": str(local), "ProgramFiles": str(local / "nope"),
                 "ProgramFiles(x86)": str(local / "nope86")},
            ), mock.patch.object(obs_controller, "shutil", mock.Mock(which=lambda _n: None)):
                self.assertEqual(obs_controller.find_obs_exe(), exe)

    def test_explicit_path_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "obs64.exe"
            exe.write_text("", encoding="utf-8")
            self.assertEqual(obs_controller.find_obs_exe(exe), exe)

    def test_absent_install_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"LOCALAPPDATA": tmp, "ProgramFiles": str(Path(tmp) / "a"),
                 "ProgramFiles(x86)": str(Path(tmp) / "b")},
            ), mock.patch.object(obs_controller, "shutil", mock.Mock(which=lambda _n: None)), \
               mock.patch.object(obs_controller, "_obs_from_registry", lambda: None):
                self.assertIsNone(obs_controller.find_obs_exe())


if __name__ == "__main__":
    unittest.main()
