#!/usr/bin/env python3
"""Restart verification tests for the detached Control Panel updater."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_updates(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "control-panel" / "browser_updates.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RestartFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="vesselstack-restart-test-")
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        (self.root / "installer").mkdir()
        (self.root / "config" / "control-panel.env").write_text(
            "CONTROL_PANEL_HOST=127.0.0.1\nCONTROL_PANEL_PORT=8780\n"
        )
        (self.root / "installer" / "VERSION").write_text("0.5.4\n")
        self.updates = load_updates(f"restart_{id(self)}")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_verify_restart_accepts_only_new_boot_and_target_version(self) -> None:
        old = "a" * 32
        new = "b" * 32
        sequence = [
            {"boot_id": old, "running_version": "0.5.4"},
            {"boot_id": new, "running_version": "0.5.3"},
            {"boot_id": new, "running_version": "0.5.4"},
        ]
        clock = [0.0]
        def sleep(_delay: float) -> None:
            clock[0] += 0.2
        with mock.patch.object(self.updates, "_probe_panel_health", side_effect=sequence), \
             mock.patch.object(self.updates.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(self.updates.time, "sleep", side_effect=sleep), \
             mock.patch.object(self.updates.subprocess, "run", return_value=self.updates.subprocess.CompletedProcess([], 0, stdout="active")):
            result = self.updates.verify_restart(self.root, old, "0.5.4", timeout=1)
        self.assertEqual(result, sequence[-1])

    def test_verify_restart_rejects_unchanged_mismatch_unreachable_and_timeout(self) -> None:
        cases = [
            ([{"boot_id": "a" * 32, "running_version": "0.5.4"}], "a" * 32),
            ([{"boot_id": "b" * 32, "running_version": "0.5.3"}], "a" * 32),
            ([OSError("unreachable")], "old"),
            ([ValueError("redirect")], "old"),
        ]
        for values, old_boot in cases:
            with self.subTest(values=values):
                with mock.patch.object(self.updates, "_probe_panel_health", side_effect=values), \
                     mock.patch.object(self.updates.time, "monotonic", side_effect=[0.0, 2.0]), \
                     mock.patch.object(self.updates.time, "sleep"), \
                     mock.patch.object(self.updates.subprocess, "run", return_value=self.updates.subprocess.CompletedProcess([], 0, stdout="active")):
                    with self.assertRaisesRegex(RuntimeError, "timed out"):
                        self.updates.verify_restart(self.root, old_boot, "0.5.4", timeout=1)

    def test_health_url_normalizes_wildcards_and_brackets_ipv6(self) -> None:
        cases = [
            ("0.0.0.0", "http://127.0.0.1:8780/health"),
            ("::", "http://[::1]:8780/health"),
            ("192.0.2.44", "http://192.0.2.44:8780/health"),
            ("2001:db8::44", "http://[2001:db8::44]:8780/health"),
        ]
        for host, expected in cases:
            with self.subTest(host=host):
                (self.root / "config" / "control-panel.env").write_text(
                    f"CONTROL_PANEL_HOST={host}\nCONTROL_PANEL_PORT=8780\n"
                )
                self.assertEqual(self.updates._panel_health_url(self.root), expected)

    def test_probe_uses_fixed_url_and_no_auth_or_proxy_headers(self) -> None:
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            status = 200
            def read(self, _limit): return b'{"ok": true, "boot_id": "' + (b"b" * 32) + b'", "version": "0.5.4"}'

        opener = mock.Mock()
        opener.open.return_value = Response()
        with mock.patch.object(self.updates.urllib.request, "build_opener", return_value=opener) as build:
            self.assertEqual(self.updates._probe_panel_health(self.root), {"boot_id": "b" * 32, "running_version": "0.5.4"})
        build.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8780/health")
        self.assertEqual(dict(request.header_items()), {"Accept": "application/json"})
        self.assertNotIn("Authorization", dict(request.header_items()))

        handlers = build.call_args.args
        self.assertTrue(any(isinstance(handler, self.updates.urllib.request.ProxyHandler) for handler in handlers))

    def test_probe_rejects_invalid_health_payload(self) -> None:
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, _limit): return b'{"ok": true, "boot_id": "not-a-boot", "version": "0.5.4"}'
        opener = mock.Mock()
        opener.open.return_value = Response()
        with mock.patch.object(self.updates.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "invalid panel health"):
                self.updates._probe_panel_health(self.root)

    def test_inactive_unit_cannot_complete_restart_verification(self) -> None:
        with mock.patch.object(self.updates.subprocess, "run", return_value=self.updates.subprocess.CompletedProcess([], 3, stdout="inactive")), \
             mock.patch.object(self.updates, "_probe_panel_health") as probe, \
             mock.patch.object(self.updates.time, "monotonic", side_effect=[0.0, 2.0]), \
             mock.patch.object(self.updates.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                self.updates.verify_restart(self.root, "a" * 32, "0.5.4", timeout=1)
        probe.assert_not_called()

    def test_start_detached_failure_records_failed_state_and_private_job(self) -> None:
        with mock.patch.object(self.updates.subprocess, "run", side_effect=OSError("systemd unavailable")):
            with self.assertRaisesRegex(OSError, "systemd unavailable"):
                self.updates.start_detached("restart-panel", self.root, {"boot_id": "old", "running_version": "0.5.4"})
        state = self.updates.read_operation(self.root)
        self.assertEqual(state["state"], "failed")
        jobs = list((self.root / "var" / "lib" / "vesselstack").glob("browser-update-*"))
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.stat().st_mode & 0o777, 0o700)
        self.assertEqual((job / "request.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((job / "runner.py").stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads((job / "request.json").read_text())["root"], str(self.root))

    def test_running_identity_keeps_captured_source_version(self) -> None:
        first = self.updates.running_identity(self.root)
        (self.root / "installer" / "VERSION").write_text("9.9.9\n")
        second = self.updates.running_identity(self.root)
        self.assertEqual(first["running_version"], "0.5.4")
        self.assertEqual(second["running_version"], "9.9.9")

    def test_worker_requires_same_id_and_persists_verified_fields(self) -> None:
        request_dir = self.root / "var" / "lib" / "vesselstack" / "job"
        request_dir.mkdir(parents=True)
        request = {"id": 7, "root": str(self.root), "config": str(self.root / "config" / "vesselstack.env"), "ctl": self.updates.FIXED_CTL, "action": "restart-panel", "old_boot_id": "old", "target_version": "0.5.4"}
        (request_dir / "request.json").write_text(json.dumps(request))
        self.updates.write_operation(self.root, {"id": 7, "state": "running", "label": "restart-panel", "output": []})
        completed = [mock.Mock(stdout=None), mock.Mock(stdout="active")]
        with mock.patch.object(self.updates.subprocess, "run", side_effect=completed), \
             mock.patch.object(self.updates, "verify_restart", return_value={"boot_id": "new", "running_version": "0.5.4"}):
            self.updates.run_worker(request_dir / "request.json")
        result = self.updates.read_operation(self.root)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["phase"], "complete")
        self.assertEqual(result["boot_id"], "new")
        self.assertEqual(result["running_version"], "0.5.4")


if __name__ == "__main__":
    unittest.main()
