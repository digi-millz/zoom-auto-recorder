"""Crash-marker guard tests: the thing that caused the safe-mode dialog loop."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
