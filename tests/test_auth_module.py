#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


auth = load("vesselstack_auth_under_test", "common/vesselstack_auth.py")
PASS = "correct horse battery"


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class HashTests(unittest.TestCase):
    def test_format_and_verify(self) -> None:
        stored = auth.hash_passphrase(PASS)
        parts = stored.split(":")
        self.assertEqual(parts[:4], ["scrypt", "15", "8", "1"])
        self.assertEqual(len(auth._unb64(parts[4])), 16)
        self.assertTrue(auth.verify_passphrase(PASS, stored))
        self.assertFalse(auth.verify_passphrase(PASS + "x", stored))
        self.assertNotEqual(stored, auth.hash_passphrase(PASS), "salted")

    def test_malformed_stored_values_are_false(self) -> None:
        for bad in (None, "", "scrypt:15:8:1:x", "md5:15:8:1:AA==:AA==", "scrypt:99:8:1:AA==:AA==", "scrypt:15:8:1:!!:!!"):
            self.assertFalse(auth.verify_passphrase(PASS, bad), bad)
        self.assertFalse(auth.verify_passphrase(None, auth.hash_passphrase(PASS)))

    def test_comparison_is_constant_time_helper(self) -> None:
        stored = auth.hash_passphrase(PASS)
        with mock.patch.object(auth.hmac, "compare_digest", wraps=auth.hmac.compare_digest) as compare:
            auth.verify_passphrase("wrong wrong wrong", stored)
        self.assertTrue(compare.called)

    def test_minimum_length(self) -> None:
        with self.assertRaises(auth.AuthError):
            auth.check_new_passphrase("short")
        self.assertEqual(auth.check_new_passphrase("twelve chars"), "twelve chars")


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config" / "control-panel-auth.json"
        self.clock = Clock()
        self.store = auth.AuthStore(self.path, clock=self.clock)

    def test_empty_store_has_no_passphrase(self) -> None:
        self.assertFalse(self.store.passphrase_set())
        self.assertFalse(self.path.exists())

    def test_write_is_0600_and_tokens_are_never_stored_raw(self) -> None:
        self.store.set_passphrase(PASS)
        token, device = self.store.create_device("Safari on Mac", True)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        raw = self.path.read_text()
        self.assertNotIn(token, raw)
        self.assertNotIn(PASS, raw)
        data = json.loads(raw)
        self.assertEqual(data["schema"], 1)
        self.assertEqual(data["devices"][0]["token_sha256"], auth.token_digest(token))
        self.assertEqual([p.name for p in self.path.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_existing_mode_is_preserved(self) -> None:
        self.store.set_passphrase(PASS)
        os.chmod(self.path, 0o640)
        self.store.create_device("x", False)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)

    def test_symlinked_store_is_refused(self) -> None:
        self.path.parent.mkdir(parents=True)
        target = Path(self.tmp.name) / "elsewhere.json"
        target.write_text("{}")
        self.path.symlink_to(target)
        self.assertFalse(self.store.passphrase_set(), "reads fail closed")
        with self.assertRaises(auth.StoreError):
            self.store.set_passphrase(PASS)
        self.assertEqual(target.read_text(), "{}")

    def test_corrupt_store_fails_closed_and_is_not_overwritten(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not json")
        self.assertFalse(self.store.passphrase_set())
        with self.assertRaises(auth.StoreError):
            self.store.set_passphrase(PASS)
        self.assertEqual(self.path.read_text(), "not json")

    def test_device_lifecycle_expiry_and_revoke(self) -> None:
        self.store.set_passphrase(PASS)
        short, short_device = self.store.create_device("a", False)
        long, long_device = self.store.create_device("b", True)
        self.assertEqual(short_device["expires"] - short_device["created"], 12 * 3600)
        self.assertEqual(long_device["expires"] - long_device["created"], 30 * 86400)
        self.assertEqual(self.store.verify_device_token(short)["id"], short_device["id"])
        self.assertIsNone(self.store.verify_device_token("x" * 43))
        self.assertIsNone(self.store.verify_device_token(""))
        self.clock.now += 13 * 3600
        self.assertIsNone(self.store.verify_device_token(short))
        self.assertIsNotNone(self.store.verify_device_token(long))
        self.clock.now += 30 * 86400
        self.assertIsNone(self.store.verify_device_token(long), "fixed lifetime, never sliding")
        self.clock.now = 1_800_000_000.0
        token, device = self.store.create_device("c", True)
        self.assertTrue(self.store.revoke(device["id"]))
        self.assertIsNone(self.store.verify_device_token(token))
        self.assertFalse(self.store.revoke("nope"))

    def test_last_seen_updates_at_most_hourly(self) -> None:
        self.store.set_passphrase(PASS)
        token, device = self.store.create_device("a", True)
        self.clock.now += 600
        self.store.verify_device_token(token)
        self.assertEqual(self.store.list_devices()[0]["last_seen"], device["created"])
        self.clock.now += 3600
        self.store.verify_device_token(token)
        self.assertEqual(self.store.list_devices()[0]["last_seen"], int(self.clock.now))

    def test_prune_and_limit(self) -> None:
        self.store.set_passphrase(PASS)
        for index in range(auth.MAX_DEVICES):
            self.store.create_device(f"d{index}", False)
        with self.assertRaises(auth.DeviceLimitError):
            self.store.create_device("one too many", False)
        self.clock.now += 13 * 3600
        self.store.create_device("fresh", False)
        self.assertEqual(len(self.store.list_devices()), 1, "expired devices are pruned on create")

    def test_passphrase_change_revokes_others_and_clear_revokes_all(self) -> None:
        self.store.set_passphrase(PASS)
        keep_token, keep = self.store.create_device("keep", True)
        other_token, _ = self.store.create_device("other", True)
        self.assertEqual(self.store.set_passphrase("another long phrase", keep_device=keep["id"]), 1)
        self.assertIsNotNone(self.store.verify_device_token(keep_token))
        self.assertIsNone(self.store.verify_device_token(other_token))
        self.assertTrue(self.store.check_passphrase("another long phrase"))
        self.assertFalse(self.store.check_passphrase(PASS))
        self.assertEqual(self.store.clear_passphrase(), 1)
        self.assertFalse(self.store.passphrase_set())
        self.assertFalse(self.store.check_passphrase("another long phrase"))

    def test_reload_when_file_changes_underneath(self) -> None:
        self.store.set_passphrase(PASS)
        token, _ = self.store.create_device("a", True)
        self.assertIsNotNone(self.store.verify_device_token(token))
        other = auth.AuthStore(self.path, clock=self.clock)
        other.revoke_all()
        self.assertIsNone(self.store.verify_device_token(token), "second writer's change is seen via mtime")

    def test_unknown_keys_such_as_pairings_survive_writes(self) -> None:
        self.store.set_passphrase(PASS)
        data = json.loads(self.path.read_text())
        data["pairings"] = [{"id": "p"}]
        self.path.write_text(json.dumps(data))
        self.store.create_device("a", False)
        self.assertEqual(json.loads(self.path.read_text())["pairings"], [{"id": "p"}])


class ThrottleTests(unittest.TestCase):
    def test_begin_counts_attempts_atomically_across_threads(self) -> None:
        import threading
        throttle = auth.LoginThrottle()
        allowed = []
        start = threading.Barrier(30)

        def attempt() -> None:
            start.wait()
            allowed.append(throttle.begin("192.0.2.9")[0])
        threads = [threading.Thread(target=attempt) for _ in range(30)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(allowed.count(True), auth.LoginThrottle.IP_FREE_FAILURES)
        self.assertFalse(throttle.begin("192.0.2.9")[0])

    def test_failure_after_begin_does_not_double_count_and_success_withdraws(self) -> None:
        throttle = auth.LoginThrottle(Clock())
        for _ in range(4):
            self.assertTrue(throttle.begin("192.0.2.5")[0])
            throttle.failure("192.0.2.5")
        self.assertEqual(throttle.retry_after("192.0.2.5"), 0, "4 attempts, none double counted")
        self.assertTrue(throttle.begin("192.0.2.5")[0])
        throttle.success("192.0.2.5")
        self.assertEqual(throttle.retry_after("192.0.2.5"), 0)
        self.assertEqual(len(throttle._global), 4, "the successful attempt left the global window")
        allowed, wait = throttle.begin("192.0.2.5")
        self.assertTrue(allowed)
        self.assertEqual(wait, 0)

    def test_begin_global_lockout(self) -> None:
        throttle = auth.LoginThrottle(Clock())
        results = [throttle.begin(f"192.0.2.{index + 10}")[0] for index in range(25)]
        self.assertEqual(results.count(True), auth.LoginThrottle.GLOBAL_LIMIT + 1)
        allowed, wait = throttle.begin("198.51.100.1")
        self.assertFalse(allowed)
        self.assertGreater(wait, 800)

    def test_per_ip_backoff_is_exponential_and_capped(self) -> None:
        clock = Clock()
        throttle = auth.LoginThrottle(clock)
        for _ in range(4):
            throttle.failure("192.0.2.5")
        self.assertEqual(throttle.retry_after("192.0.2.5"), 0)
        throttle.failure("192.0.2.5")
        first = throttle.retry_after("192.0.2.5")
        self.assertTrue(25 <= first <= 31)
        self.assertEqual(throttle.retry_after("192.0.2.6"), 0, "other addresses unaffected")
        clock.now += first
        throttle.failure("192.0.2.5")
        self.assertGreater(throttle.retry_after("192.0.2.5"), first)
        for _ in range(30):
            clock.now += 1000
            throttle.failure("192.0.2.5")
        self.assertLessEqual(throttle.retry_after("192.0.2.5"), 15 * 60 + 1)

    def test_success_clears_the_address(self) -> None:
        throttle = auth.LoginThrottle(Clock())
        for _ in range(5):
            throttle.failure("192.0.2.5")
        throttle.success("192.0.2.5")
        self.assertEqual(throttle.retry_after("192.0.2.5"), 0)

    def test_global_lockout_after_twenty_failures_from_any_addresses(self) -> None:
        clock = Clock()
        throttle = auth.LoginThrottle(clock)
        for index in range(21):
            throttle.failure(f"192.0.2.{index + 10}")
        self.assertGreater(throttle.retry_after("192.0.2.200"), 800)
        clock.now += 15 * 60 + 2
        self.assertEqual(throttle.retry_after("192.0.2.200"), 0)


class CliTests(unittest.TestCase):
    def run_cli(self, root: str, args: list[str], stdin: str = ""):
        cli = load("vesselstack_authctl_under_test", "common/vesselstack_authctl.py")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"VESSELSTACK_ROOT": root}):
            code = cli.main(args, io.StringIO(stdin), out, err)
        return code, out.getvalue(), err.getvalue()

    def test_set_status_devices_clear(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(self.run_cli(root, ["passphrase", "status"])[1].splitlines()[0], "panel passphrase: not set")
            code, out, err = self.run_cli(root, ["passphrase", "set", "panel", "--stdin"], "too short\n")
            self.assertEqual(code, 1)
            self.assertIn("at least 12", err)
            code, out, _ = self.run_cli(root, ["passphrase", "set", "panel", "--stdin"], PASS + "\n")
            self.assertEqual(code, 0)
            self.assertNotIn(PASS, out)
            store = auth.AuthStore(Path(root) / "config" / "control-panel-auth.json")
            self.assertTrue(store.check_passphrase(PASS))
            _, device = store.create_device("Phone", True)
            self.assertIn(device["id"], self.run_cli(root, ["devices", "list"])[1])
            self.assertEqual(self.run_cli(root, ["devices", "revoke", device["id"]])[0], 0)
            self.assertEqual(self.run_cli(root, ["devices", "revoke", device["id"]])[0], 1)
            store.create_device("Tablet", False)
            self.assertIn("Revoked 1", self.run_cli(root, ["devices", "revoke-all"])[1])
            self.assertEqual(self.run_cli(root, ["passphrase", "clear", "panel"])[0], 0)
            self.assertFalse(store.passphrase_set())

    def test_passphrase_is_never_taken_from_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            code, _, err = self.run_cli(root, ["passphrase", "set", "panel", PASS])
            self.assertEqual(code, 2)
            self.assertFalse((Path(root) / "config" / "control-panel-auth.json").exists())

    def test_pipe_without_stdin_flag_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(self.run_cli(root, ["passphrase", "set", "panel"], PASS + "\n")[0], 2)


if __name__ == "__main__":
    unittest.main()
