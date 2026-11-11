import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_panel():
    spec = importlib.util.spec_from_file_location("lifecycle_panel_test", ROOT / "control-panel" / "app.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LifecycleCallerTests(unittest.TestCase):
    def test_panel_slot_blocks_competing_process_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            app_root, system_root = base / "app", base / "system"
            app_root.mkdir(); system_root.mkdir()
            old_root, old_system = os.environ.get("VESSELSTACK_SYSTEM_ROOT"), os.environ.get("VESSELSTACK_ROOT")
            os.environ["VESSELSTACK_SYSTEM_ROOT"] = str(system_root)
            os.environ["VESSELSTACK_ROOT"] = str(app_root)
            panel = load_panel(); panel.ROOT = app_root
            marker = base / "marker"
            slot = panel.take_lifecycle_slot()
            try:
                code = "import pathlib,sys; from common.lifecycle_lock import LifecycleLock; " \
                       "p=pathlib.Path(sys.argv[3]); " \
                       "\ntry: LifecycleLock(sys.argv[1],sys.argv[2]).acquire(); p.write_text('bad')\nexcept Exception as e: print(type(e).__name__)"
                result = subprocess.run(["python3", "-c", code, str(app_root), str(system_root), str(marker)],
                                        cwd=ROOT, text=True, capture_output=True)
                self.assertIn("LifecycleLockBusy", result.stdout)
                self.assertFalse(marker.exists())
            finally:
                panel.release_lifecycle_slot(slot)
                if old_root is None: os.environ.pop("VESSELSTACK_ROOT", None)
                else: os.environ["VESSELSTACK_ROOT"] = old_root
                if old_system is None: os.environ.pop("VESSELSTACK_SYSTEM_ROOT", None)
                else: os.environ["VESSELSTACK_SYSTEM_ROOT"] = old_system

    def test_apply_runs_installer_with_start(self):
        panel = load_panel()
        with tempfile.TemporaryDirectory() as tmp:
            installer = Path(tmp) / "install.sh"
            installer.write_text("#!/bin/sh\n")
            panel.INSTALLER = installer
            command = panel.action_command("apply", {})
            self.assertEqual(command[-1], "--start")

    def test_panel_slot_releases_shared_lock_before_browser_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            app_root, system_root = base / "app", base / "system"
            app_root.mkdir(); system_root.mkdir()
            old_root, old_system = os.environ.get("VESSELSTACK_SYSTEM_ROOT"), os.environ.get("VESSELSTACK_ROOT")
            os.environ["VESSELSTACK_SYSTEM_ROOT"] = str(system_root)
            os.environ["VESSELSTACK_ROOT"] = str(app_root)
            panel = load_panel(); panel.ROOT = app_root
            slot = panel.take_lifecycle_slot()
            original_release = panel.release_slot
            acquired = []

            def release_browser_after_shared(fd):
                contender = panel.LifecycleLock(app_root, str(system_root))
                try:
                    contender.acquire()
                    acquired.append(True)
                finally:
                    if acquired:
                        contender.release()
                original_release(fd)

            panel.release_slot = release_browser_after_shared
            try:
                panel.release_lifecycle_slot(slot)
                self.assertEqual(acquired, [True])
            finally:
                panel.release_slot = original_release
                if old_root is None: os.environ.pop("VESSELSTACK_ROOT", None)
                else: os.environ["VESSELSTACK_ROOT"] = old_root
                if old_system is None: os.environ.pop("VESSELSTACK_SYSTEM_ROOT", None)
                else: os.environ["VESSELSTACK_SYSTEM_ROOT"] = old_system


if __name__ == "__main__":
    unittest.main()
