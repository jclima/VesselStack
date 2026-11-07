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


    # ---- one-shot backup choices over HTTP

    def serve(self):
        server = self.panel.ThreadingHTTPServer(("127.0.0.1", 0), self.panel.ControlPanelHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def get(self, server, path: str, token: str | None = "test-token") -> tuple[int, dict[str, object]]:
        headers = {"X-VesselStack-Token": token} if token else {}
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def post_update(self, server, backup: object = "absent") -> tuple[int, dict[str, object], mock.Mock]:
        payload: dict[str, object] = {"action": "update-release", "release_id": 42, "channel": "stable"}
        if backup != "absent":
            payload["backup"] = backup
        with mock.patch.object(self.panel, "release_snapshot", return_value={"version": "0.6.2"}), \
                mock.patch.object(self.panel, "start_detached", return_value={"state": "running", "id": 1}) as start:
            status, body = self.request(server, "/api/action", payload)
        return status, body, start

    def test_release_upgrade_default_call_is_unchanged_without_a_backup_choice(self) -> None:
        server = self.serve()
        for backup in ("absent", None, {"mode": "require_mounted"}):
            with self.subTest(backup=backup):
                status, _, start = self.post_update(server, backup)
                self.assertEqual(status, 202)
                start.assert_called_once_with("update-release", self.root, self.panel.RUNNING_IDENTITY, 42, "stable", target_version="0.6.2", snapshot={"version": "0.6.2"})

    def test_release_upgrade_passes_each_valid_choice_to_the_worker_request(self) -> None:
        server = self.serve()
        (self.root / "config" / "vesselstack.env").write_text("VESSELSTACK_BACKUP=/media/backup-drive/backups\n")
        valid = ({"mode": "mount_fstab"}, {"mode": "mount_drive", "uuid": "AB12-CD34"},
                 {"mode": "system_disk_once", "confirm": "system-disk-once"}, {"mode": "skip", "confirm": "install-without-backup"})
        with mock.patch.object(self.panel.os.path, "ismount", return_value=False), mock.patch.object(self.updates.os.path, "ismount", return_value=False), \
                mock.patch.object(self.panel, "effective_backup_choice", side_effect=lambda c, _: (c, "")):
            for backup in valid:
                with self.subTest(backup=backup):
                    status, _, start = self.post_update(server, backup)
                    self.assertEqual(status, 202)
                    self.assertEqual(start.call_args.kwargs["backup"], backup)

    def test_release_upgrade_rejects_bad_choices_before_starting(self) -> None:
        server = self.serve()
        bad = ({"mode": "format"}, {"mode": "mount_drive"}, {"mode": "mount_drive", "uuid": "/dev/sda1"}, {"mode": "mount_drive", "uuid": "a b"},
               {"mode": "mount_fstab", "device": "/dev/sda1"}, {"mode": "mount_fstab", "path": "/media/x"}, {"mode": "skip"},
               {"mode": "skip", "confirm": "system-disk-once"}, {"mode": "system_disk_once", "confirm": "install-without-backup"},
               {"mode": "system_disk_once"}, {"mode": "require_mounted", "confirm": "x"}, "skip", ["skip"], 7)
        for backup in bad:
            with self.subTest(backup=backup):
                status, body, start = self.post_update(server, backup)
                self.assertEqual(status, 400, body)
                start.assert_not_called()

    def test_lock_in_the_environment_file_refuses_skip_and_system_disk(self) -> None:
        server = self.serve()
        (self.root / "config" / "vesselstack.env").write_text("VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=false\n")
        for backup in ({"mode": "skip", "confirm": "install-without-backup"}, {"mode": "system_disk_once", "confirm": "system-disk-once"}):
            with self.subTest(backup=backup):
                status, body, start = self.post_update(server, backup)
                self.assertEqual(status, 400)
                self.assertIn("turned off", str(body))
                start.assert_not_called()
        with mock.patch.object(self.panel.os.path, "ismount", return_value=False), mock.patch.object(self.panel, "effective_backup_choice", side_effect=lambda c, _: (c, "")):
            status, _, start = self.post_update(server, {"mode": "mount_fstab"})
        self.assertEqual(status, 202)

    def test_backup_choice_is_refused_for_other_actions(self) -> None:
        server = self.serve()
        for action in ("backup", "restart-panel", "restart", "unknown"):
            with self.subTest(action=action), mock.patch.object(self.panel.OPERATIONS, "start", return_value={"state": "running"}) as start, \
                    mock.patch.object(self.panel, "start_detached", return_value={"state": "running"}) as detached:
                status, body = self.request(server, "/api/action", {"action": action, "backup": {"mode": "skip", "confirm": "install-without-backup"}})
                self.assertEqual(status, 400, body)
                start.assert_not_called(); detached.assert_not_called()

    def test_process_environment_lock_refuses_skip_over_http(self) -> None:
        server = self.serve()
        with mock.patch.dict(self.panel.os.environ, {"VESSELSTACK_BACKUP_ALLOW_UNMOUNTED": "false"}):
            status, body, start = self.post_update(server, {"mode": "skip", "confirm": "install-without-backup"})
        self.assertEqual(status, 400)
        start.assert_not_called()

    def test_folder_update_choices_are_validated_and_run_the_mount_as_a_separate_step(self) -> None:
        server = self.serve()
        (self.root / "config" / "vesselstack.env").write_text("VESSELSTACK_BACKUP=/media/backup-drive/backups\n")
        def post(backup):
            with mock.patch.object(self.panel.OPERATIONS, "start", return_value={"state": "running"}) as start, \
                    mock.patch.object(self.panel, "effective_backup_choice", side_effect=lambda c, _: (c, "")), \
                    mock.patch.object(self.panel, "ctl_supports", return_value=True):
                payload = {"action": "update", "source_directory": "/var/lib/vesselstack/releases/x"}
                if backup != "absent":
                    payload["backup"] = backup
                status, body = self.request(server, "/api/action", payload)
            return status, body, start
        status, _, start = post("absent")
        self.assertEqual(status, 202)
        self.assertEqual(start.call_args.args[0], "update"); self.assertEqual(start.call_args.kwargs, {})
        status, _, start = post({"mode": "mount_drive", "uuid": "AB12-CD34"})
        self.assertEqual(status, 202)
        self.assertEqual(start.call_args.kwargs["extra"], {"backup_mode": "mount_drive"})
        self.assertEqual(start.call_args.kwargs["before"]()[0][-2:], ["--uuid", "AB12-CD34"])
        status, _, start = post({"mode": "skip", "confirm": "install-without-backup"})
        self.assertEqual(start.call_args.kwargs["extra"], {"backup_mode": "skip", "backup_skipped": True})
        self.assertEqual(start.call_args.kwargs["before"](), [])
        for bad in ({"mode": "skip"}, {"mode": "mount_drive", "uuid": "../x"}, {"mode": "nope"}):
            status, _, start = post(bad)
            self.assertEqual(status, 400)
            start.assert_not_called()

    def test_preflight_requires_the_token_and_is_read_only(self) -> None:
        server = self.serve()
        self.assertEqual(self.get(server, "/api/backup-preflight", token=None)[0], 401)
        self.assertEqual(self.get(server, "/api/backup-preflight", token="wrong")[0], 401)
        before = (self.root / "config" / "vesselstack.env").read_bytes()
        with mock.patch.object(self.panel, "command_stdout", return_value=(1, "")):
            status, body = self.get(server, "/api/backup-preflight")
        self.assertEqual(status, 200)
        self.assertIn("default_ok", body)
        self.assertEqual((self.root / "config" / "vesselstack.env").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
