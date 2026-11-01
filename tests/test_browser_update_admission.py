#!/usr/bin/env python3
"""Admission and durable-state tests for the authenticated browser updater."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="vesselstack-browser-admission-")
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        (self.root / "installer").mkdir()
        (self.root / "config" / "vesselstack.env").write_text("BOAT_NAME=Original\n")
        (self.root / "config" / "boat-chat.env").write_text("\n")
        (self.root / "config" / "control-panel.env").write_text(
            "CONTROL_PANEL_TOKEN=test-token\nCONTROL_PANEL_HOST=127.0.0.1\nCONTROL_PANEL_PORT=8780\n"
        )
        (self.root / "installer" / "VERSION").write_text("0.5.3\n")
        os.environ["VESSELSTACK_ROOT"] = str(self.root)
        self.panel = load_module(ROOT / "control-panel" / "app.py", f"panel_{id(self)}")
        self.updates = load_module(ROOT / "control-panel" / "browser_updates.py", f"updates_{id(self)}")
        self.panel.ROOT = self.root
        self.panel.VESSEL_ENV = self.root / "config" / "vesselstack.env"
        self.panel.CHAT_ENV = self.root / "config" / "boat-chat.env"
        self.panel.PANEL_ENV = self.root / "config" / "control-panel.env"
        self.panel.CONFIG_BACKUP_ROOT = self.root / "backups"
        self.panel.OPERATIONS = self.panel.OperationStore()

    def tearDown(self) -> None:
        os.environ.pop("VESSELSTACK_ROOT", None)
        self.tmp.cleanup()

    def durable(self, state: str, **extra: object) -> None:
        operation = {"id": 1, "label": "update-release", "state": state, "output": [], **extra}
        self.updates.write_operation(self.root, operation)

    def test_durable_running_state_rejects_writes_and_process_start(self) -> None:
        self.durable("running", phase="backing-up")
        before = (self.root / "config" / "vesselstack.env").read_bytes()
        update_subprocess = self.panel.acquire_slot.__globals__["subprocess"]
        with mock.patch.object(update_subprocess, "run", return_value=update_subprocess.CompletedProcess([], 0, stdout="active")):
            with self.assertRaisesRegex(ValueError, "already running"):
                self.panel.update_configuration({"BOAT_NAME": "Should not write"})
        self.assertEqual((self.root / "config" / "vesselstack.env").read_bytes(), before)

        store = self.panel.OperationStore()
        with mock.patch.object(update_subprocess, "run", return_value=update_subprocess.CompletedProcess([], 0, stdout="active")), mock.patch.object(self.panel.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "already running"):
                store.start("component action", ["never-run"])
        popen.assert_not_called()

    def test_cross_instance_lock_and_terminal_state_admission(self) -> None:
        first = load_module(ROOT / "control-panel" / "browser_updates.py", f"updates_first_{id(self)}")
        second = load_module(ROOT / "control-panel" / "browser_updates.py", f"updates_second_{id(self)}")
        self.durable("succeeded", phase="complete")
        fd = first.acquire_slot(self.root)
        try:
            with self.assertRaisesRegex(ValueError, "already running"):
                second.acquire_slot(self.root)
        finally:
            first.release_slot(fd)

        snapshot = {"version": "0.5.4"}
        with mock.patch.object(self.updates, "release_snapshot", return_value=snapshot), mock.patch.object(self.updates, "assert_snapshot_unchanged"), mock.patch.object(self.updates.subprocess, "run") as run:
            operation = self.updates.start_detached("update-release", self.root, {"boot_id": "old", "running_version": "0.5.3"}, 42, "stable", snapshot=snapshot)
        run.assert_called_once()
        self.assertEqual(operation["state"], "running")
        self.assertEqual(self.updates.read_operation(self.root)["label"], "update-release")
        self.assertEqual(self.updates.read_operation(self.root)["state"], "running")

    def test_malformed_durable_state_fails_closed(self) -> None:
        path = self.updates.operation_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-json")
        with self.assertRaisesRegex(ValueError, "Malformed durable"):
            self.updates.acquire_slot(self.root)

    def request(self, server, path: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}{path}",
            data=body,
            headers={"Content-Type": "application/json", "X-VesselStack-Token": "test-token"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_authenticated_http_admission_rejects_config_component_and_update(self) -> None:
        self.durable("running", phase="installing")
        before = (self.root / "config" / "vesselstack.env").read_bytes()
        server = self.panel.ThreadingHTTPServer(("127.0.0.1", 0), self.panel.ControlPanelHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        lock_fd = self.panel.start_detached.__globals__["acquire_runner_lock"](self.root)
        self.addCleanup(self.panel.start_detached.__globals__["release_slot"], lock_fd)
        config_status, config_body = self.request(server, "/api/config", {"settings": {"BOAT_NAME": "No write"}})
        component_status, component_body = self.request(server, "/api/component", {"component": "grafana", "action": "start"})
        update_subprocess = self.panel.start_detached.__globals__["subprocess"]
        with mock.patch.object(update_subprocess, "run") as run:
            update_status, update_body = self.request(server, "/api/action", {"action": "update-release", "release_id": 42, "channel": "stable"})

        self.assertEqual(config_status, 400)
        self.assertIn("already running", str(config_body))
        self.assertEqual(component_status, 400)
        self.assertIn("already running", str(component_body))
        self.assertEqual(update_status, 400)
        self.assertIn("already running", str(update_body))
        run.assert_not_called()
        self.assertEqual((self.root / "config" / "vesselstack.env").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
