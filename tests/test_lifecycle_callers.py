import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common.lifecycle_lock import LifecycleLockBusy


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


RESTORE_ID = "0123456789abcdef0123456789abcdef"


def plant_pending_restore(system_root: Path, phase: str = "starting") -> Path:
    journal = system_root / "var/lib/vesselstack/restore/current"
    journal.mkdir(parents=True)
    (journal / "state").write_text(f"format=1\nid={RESTORE_ID}\nphase={phase}\n")
    return journal


class PendingRestoreCallerTests(unittest.TestCase):
    """A Python lock taker gets the same refusal as for a busy lock while a restore is unfinished."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.app_root, self.system_root = self.base / "app", self.base / "system"
        self.app_root.mkdir()
        self.system_root.mkdir()
        env = {k: v for k, v in os.environ.items() if k not in ("VESSELSTACK_LIFECYCLE_LOCK_FD", "VESSELSTACK_RESTORE_ID")}
        env.update(VESSELSTACK_SYSTEM_ROOT=str(self.system_root), VESSELSTACK_ROOT=str(self.app_root))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_control_panel_admission_is_refused_and_releases_what_it_took(self):
        panel = load_panel(); panel.ROOT = self.app_root
        journal = plant_pending_restore(self.system_root)
        with self.assertRaises(LifecycleLockBusy) as caught:
            panel.take_lifecycle_slot()
        self.assertIn(f"sudo {journal}/recover status", str(caught.exception))
        # Nothing stays held: the panel's own slot and the shared lock are free again once the journal is gone.
        self.assertTrue(panel.LIFECYCLE_LOCK.acquire(blocking=False))
        panel.LIFECYCLE_LOCK.release()
        (journal / "state").write_text(f"id={RESTORE_ID}\nphase=committed\n")
        panel.release_lifecycle_slot(panel.take_lifecycle_slot())

    def test_control_panel_actions_report_the_refusal_as_an_error_not_a_success(self):
        panel = load_panel(); panel.ROOT = self.app_root
        plant_pending_restore(self.system_root, "prepared")
        with self.assertRaises(LifecycleLockBusy):
            panel.OPERATIONS.start("test", ["true"])
        self.assertNotEqual((panel.OPERATIONS.public() or {}).get("state"), "running")

    def test_browser_update_worker_fails_the_operation_without_running_anything(self):
        spec = importlib.util.spec_from_file_location("pending_updates_test", ROOT / "control-panel" / "browser_updates.py")
        updates = importlib.util.module_from_spec(spec); spec.loader.exec_module(updates)
        (self.app_root / "config").mkdir()
        (self.app_root / "common").mkdir()
        (self.app_root / "common" / "lifecycle_lock.py").write_text((ROOT / "common" / "lifecycle_lock.py").read_text())
        plant_pending_restore(self.system_root, "root-swap")
        request_dir = self.app_root / "var" / "lib" / "vesselstack" / "job"
        request_dir.mkdir(parents=True)
        request = {"id": 7, "root": str(self.app_root), "config": str(self.app_root / "config" / "vesselstack.env"),
                   "ctl": updates.FIXED_CTL, "action": "restart-panel", "system_root": str(self.system_root)}
        (request_dir / "request.json").write_text(json.dumps(request))
        updates.write_operation(self.app_root, {"id": 7, "state": "running", "label": "restart-panel", "output": []})
        with mock.patch.object(updates.subprocess, "run") as run:
            updates.run_worker(request_dir / "request.json")
        run.assert_not_called()
        state = updates.read_operation(self.app_root)
        self.assertEqual(state["state"], "failed")
        self.assertIn("a restore is unfinished", " ".join(state["output"]))

    def test_boat_profile_command_exits_2_with_the_error_and_writes_nothing(self):
        cli = ROOT / "common" / "boat_profile_cli.py"
        plant_pending_restore(self.system_root)
        result = subprocess.run([sys.executable, str(cli), "init", "--root", str(self.app_root)],
                                env=dict(os.environ), capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("a restore is unfinished", result.stderr)
        self.assertFalse((self.app_root / "config" / "boat-profile.json").exists())


if __name__ == "__main__":
    unittest.main()
