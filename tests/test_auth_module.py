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


BKEY = "ab" * 32

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

    def test_unknown_keys_survive_writes(self) -> None:
        self.store.set_passphrase(PASS)
        data = json.loads(self.path.read_text())
        data["future"] = [{"id": "p"}]
        self.path.write_text(json.dumps(data))
        self.store.create_device("a", False)
        self.assertEqual(json.loads(self.path.read_text())["future"], [{"id": "p"}])


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
            ok, _, handle = throttle.begin("192.0.2.5")
            self.assertTrue(ok)
            throttle.failure(handle)
        self.assertEqual(throttle.retry_after("192.0.2.5"), 0, "4 attempts, none double counted")
        ok, _, handle = throttle.begin("192.0.2.5")
        self.assertTrue(ok)
        throttle.success(handle)
        self.assertEqual(throttle.retry_after("192.0.2.5"), 0)
        self.assertEqual(len(throttle._global), 4, "the successful attempt left the global window")
        allowed, wait, _ = throttle.begin("192.0.2.5")
        self.assertTrue(allowed)
        self.assertEqual(wait, 0)

    def test_begin_global_lockout(self) -> None:
        throttle = auth.LoginThrottle(Clock())
        results = [throttle.begin(f"192.0.2.{index + 10}")[0] for index in range(25)]
        self.assertEqual(results.count(True), auth.LoginThrottle.GLOBAL_LIMIT + 1)
        allowed, wait, _ = throttle.begin("198.51.100.1")
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

    def test_success_does_not_clear_the_address_backoff(self) -> None:
        clock = Clock()
        throttle = auth.LoginThrottle(clock)
        for _ in range(5):
            throttle.failure("192.0.2.5")
        blocked = throttle.retry_after("192.0.2.5")
        self.assertGreater(blocked, 0)
        # a success from another address (or a stale handle) never lifts it
        ok, _, mine = throttle.begin("192.0.2.7")
        throttle.success(mine)
        self.assertEqual(throttle.retry_after("192.0.2.7"), 0)
        self.assertEqual(throttle.retry_after("192.0.2.5"), blocked)
        with self.assertRaises(TypeError):
            throttle.success("192.0.2.5")
        # the blocked address cannot sign in again until the backoff runs out; the count then decays with time
        self.assertFalse(throttle.begin("192.0.2.5")[0])
        clock.now += blocked
        ok, _, handle = throttle.begin("192.0.2.5")
        self.assertTrue(ok)
        throttle.success(handle)
        self.assertGreater(throttle._ips["192.0.2.5"]["count"], 0, "earlier failures stay counted after a success")

    def test_success_withdraws_only_its_own_count_and_block(self) -> None:
        throttle = auth.LoginThrottle(Clock())
        ip = "192.0.2.8"
        for _ in range(4):
            throttle.failure(ip)
        ok, _, fifth = throttle.begin(ip)  # this attempt would trip the first backoff
        self.assertTrue(ok)
        self.assertGreater(throttle.retry_after(ip), 0)
        throttle.success(fifth)
        self.assertEqual(throttle.retry_after(ip), 0, "the block this very attempt set is undone")
        self.assertEqual(throttle._ips[ip]["count"], 4, "the four real failures remain")
        ok, _, again = throttle.begin(ip)
        self.assertTrue(ok)
        throttle.failure(again)
        self.assertGreater(throttle.retry_after(ip), 0)

    def test_interleaved_success_never_erases_other_failures(self) -> None:
        throttle = auth.LoginThrottle(Clock())
        ip = "192.0.2.50"
        ok1, _, a = throttle.begin(ip); throttle.failure(a)
        ok2, _, b = throttle.begin(ip); throttle.failure(b)
        ok3, _, c = throttle.begin(ip); throttle.success(c)
        ok4, _, d = throttle.begin(ip); throttle.failure(d)
        self.assertTrue(all((ok1, ok2, ok3, ok4)))
        self.assertEqual([stamp for stamp, _ in throttle._global], [a.stamp_id, b.stamp_id, d.stamp_id])

    def test_concurrent_pending_attempts_keep_their_stamps_when_one_succeeds(self) -> None:
        throttle = auth.LoginThrottle(Clock())
        handles = [throttle.begin("192.0.2.60")[2] for _ in range(3)]
        winner = handles[1]
        throttle.success(winner)
        remaining = [stamp for stamp, _ in throttle._global]
        self.assertEqual(remaining, [handles[0].stamp_id, handles[2].stamp_id])
        other = throttle.begin("198.51.100.4")[2]
        throttle.success(other)
        self.assertEqual(len(throttle._global), 2)
        # many failing addresses plus one logging in repeatedly still reach the global lockout
        for index in range(15):
            throttle.failure(f"203.0.113.{index}")
            h = throttle.begin("198.51.100.9")[2]
            if h:
                throttle.success(h)
        self.assertEqual(len(throttle._global), 17)
        for index in range(10):
            throttle.failure(f"203.0.114.{index}")
        self.assertGreater(throttle.retry_after("192.0.2.200"), 800)

    def test_global_lockout_after_twenty_failures_from_any_addresses(self) -> None:
        clock = Clock()
        throttle = auth.LoginThrottle(clock)
        for index in range(21):
            throttle.failure(f"192.0.2.{index + 10}")
        self.assertGreater(throttle.retry_after("192.0.2.200"), 800)
        clock.now += 15 * 60 + 2
        self.assertEqual(throttle.retry_after("192.0.2.200"), 0)


class StoreHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "chat"
        self.dir.mkdir()
        self.path = self.dir / "auth.json"

    def test_hardlinked_lock_file_is_refused(self) -> None:
        victim = Path(self._tmp.name) / "root-owned"
        victim.write_text("secret")
        os.link(victim, self.dir / "auth.json.lock")
        store = auth.AuthStore(self.path)
        with self.assertRaises(auth.StoreError):
            store.set_passphrase(PASS)
        self.assertEqual(victim.read_text(), "secret")

    def test_symlinked_store_or_lock_is_refused(self) -> None:
        victim = Path(self._tmp.name) / "target.json"
        victim.write_text("{}")
        os.symlink(victim, self.path)
        store = auth.AuthStore(self.path)
        self.assertFalse(store.passphrase_set())
        with self.assertRaises((auth.StoreError, OSError)):
            store.set_passphrase(PASS)
        self.assertEqual(victim.read_text(), "{}")
        self.path.unlink()
        (self.dir / "auth.json.lock").unlink(missing_ok=True)
        os.symlink(victim, self.dir / "auth.json.lock")
        with self.assertRaises(OSError):
            auth.AuthStore(self.path).set_passphrase(PASS)

    def test_hardlinked_or_non_regular_store_is_refused(self) -> None:
        victim = Path(self._tmp.name) / "other.json"
        victim.write_text(json.dumps({"schema": 1, "passphrase_hash": None, "devices": []}))
        os.link(victim, self.path)
        store = auth.AuthStore(self.path)
        with self.assertRaises(auth.StoreError):
            store._read_disk()
        with self.assertRaises(auth.StoreError):
            store.set_passphrase(PASS)
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaises(auth.StoreError):
            auth.AuthStore(self.path)._read_disk()
        with self.assertRaises(auth.StoreError):
            auth.AuthStore(self.path).set_passphrase(PASS)

    def test_symlinked_directory_is_refused(self) -> None:
        link = Path(self._tmp.name) / "link"
        os.symlink(self.dir, link)
        with self.assertRaises((auth.StoreError, OSError)):
            auth.AuthStore(link / "auth.json").set_passphrase(PASS)

    def test_precreated_temp_names_do_not_matter_and_no_leftovers(self) -> None:
        store = auth.AuthStore(self.path)
        store.set_passphrase(PASS)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["auth.json", "auth.json.lock"])

    def _root_store(self, owner=(4242, 4343), runner=None):
        class Store(auth.AuthStore):
            def _euid(self) -> int:
                return 0

            def _parent_owner(self) -> tuple[int, int]:
                return owner
        return Store(self.path, inherit_owner=True, expose_role=True, runner=runner, helper_path="/opt/vesselstack/common/vesselstack_authctl.py")

    def test_root_over_foreign_directory_uses_helper_process_with_dropped_identity(self) -> None:
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            reply = {"passphrase": {"set-hash": {"ok": True, "revoked": 2}, "clear": {"ok": True, "revoked": 3}},
                     "pairing": {"create": {"ok": True, "code": "ABCDE12345", "expires": 1800000600}},
                     "devices": {"revoke": {"ok": True, "found": True}, "revoke-all": {"ok": True, "revoked": 4}},
                     "binding-key": {"ensure": {"ok": True, "key": BKEY}}}[command[3]][command[4]]
            return mock.Mock(stdout=json.dumps(reply).encode(), returncode=0)
        store = self._root_store(runner=runner)
        with mock.patch("os.fchown") as fchown, mock.patch("os.chown") as chown:
            self.assertEqual(store.set_passphrase(PASS, keep_device="abc"), 2)
            self.assertEqual(store.clear_passphrase(), 3)
            self.assertEqual(store.create_pairing("admin", "Phone", auth.admin_binding(BKEY, "admin-code")), ("ABCDE12345", 1800000600))
            self.assertTrue(store.revoke("dev1"))
            self.assertEqual(store.revoke_all(keep="k"), 4)
            self.assertEqual(store.ensure_binding_key(), BKEY)
        fchown.assert_not_called()
        chown.assert_not_called()
        self.assertFalse(self.path.exists(), "root wrote nothing itself")
        import sys
        for command, kwargs in calls:
            self.assertEqual(command[:3], [sys.executable, "-I", "/opt/vesselstack/common/vesselstack_authctl.py"])
            self.assertEqual(command[-1], "--json")
            self.assertEqual((kwargs["user"], kwargs["group"], kwargs["extra_groups"]), (4242, 4343, []))
            self.assertEqual((kwargs["capture_output"], kwargs["timeout"]), (True, 30))
            self.assertEqual(set(kwargs["env"]), {"PATH", "LANG", "VESSELSTACK_DATA", "VESSELSTACK_ROOT"})
        self.assertEqual([c[0][3:-1] for c in calls], [["passphrase", "set-hash", "chat"], ["passphrase", "clear", "chat"], ["pairing", "create", "chat"],
                                                      ["devices", "revoke", "chat"], ["devices", "revoke-all", "chat"], ["binding-key", "ensure", "chat"]])
        sent = json.loads(calls[0][1]["input"])
        self.assertTrue(auth.valid_hash(sent["hash"]))
        self.assertNotIn(PASS, calls[0][1]["input"].decode(), "the helper gets a hash, never the passphrase")
        self.assertEqual(sent["keep_device"], "abc")
        self.assertEqual(json.loads(calls[2][1]["input"]), {"role": "admin", "label": "Phone", "binding": auth.admin_binding(BKEY, "admin-code")})
        self.assertEqual(json.loads(calls[3][1]["input"]), {"id": "dev1"})
        self.assertEqual(json.loads(calls[4][1]["input"]), {"keep": "k"})

    def test_helper_errors_map_back_to_exceptions(self) -> None:
        for kind, exc in (("limit", auth.DeviceLimitError), ("auth", auth.AuthError), ("changed", auth.PassphraseChanged), ("store", auth.StoreError)):
            store = self._root_store(runner=lambda *a, _k=kind, **k: mock.Mock(stdout=json.dumps({"ok": False, "kind": _k, "message": "m"}).encode()))
            with self.assertRaises(exc):
                store.clear_passphrase()
        store = self._root_store(runner=lambda *a, **k: mock.Mock(stdout=b"not json"))
        with self.assertRaises(auth.StoreError):
            store.clear_passphrase()

    def test_transaction_refuses_foreign_directory_as_root(self) -> None:
        store = self._root_store(runner=lambda *a, **k: self.fail("no helper expected"))
        with self.assertRaises(auth.StoreError):
            store.transaction(lambda data: None)
        with self.assertRaises(auth.StoreError):
            store.create_device("x", True)

    def test_helper_json_commands_round_trip_as_owner(self) -> None:
        ctl = load("vesselstack_authctl_under_test", "common/vesselstack_authctl.py")
        module = ctl.load_auth()
        stores = {"chat": module.AuthStore(self.path, expose_role=True, inherit_owner=True)}
        stores["panel"] = stores["chat"]

        def run(args, payload):
            out = io.StringIO()
            code = ctl.json_command(module, stores, args, io.StringIO(json.dumps(payload)), out)
            return code, json.loads(out.getvalue())
        encoded = module.hash_passphrase(PASS)
        self.assertEqual(run(["passphrase", "set-hash", "chat"], {"hash": encoded}), (0, {"ok": True, "revoked": 0}))
        self.assertTrue(stores["chat"].check_passphrase(PASS))
        code, reply = run(["passphrase", "set-hash", "chat"], {"hash": "scrypt:bad"})
        self.assertEqual((code, reply["ok"], reply["kind"]), (1, False, "auth"))
        code, reply = run(["passphrase", "set-hash", "chat"], {"hash": encoded, "expect_hash": None})
        self.assertEqual((code, reply["kind"]), (1, "changed"))
        code, reply = run(["pairing", "create", "chat"], {"role": "admin", "label": "Tablet", "binding": auth.admin_binding(BKEY, "admin-code")})
        self.assertEqual((code, reply["ok"], len(reply["code"])), (0, True, 10))
        code, reply = run(["binding-key", "ensure", "chat"], {})
        self.assertEqual((code, reply["ok"], len(reply["key"])), (0, True, 64))
        self.assertEqual(run(["binding-key", "ensure", "chat"], {})[1]["key"], reply["key"], "created once")
        self.assertEqual(run(["devices", "revoke", "chat"], {"id": "nope"}), (0, {"ok": True, "found": False}))
        self.assertEqual(run(["devices", "revoke-all", "chat"], {}), (0, {"ok": True, "revoked": 0}))
        self.assertEqual(run(["passphrase", "clear", "chat"], {}), (0, {"ok": True, "revoked": 0}))
        self.assertEqual(run(["bogus", "x", "chat"], {})[0], 2)

    def test_compare_and_set_refuses_changed_passphrase(self) -> None:
        store = auth.AuthStore(self.path)
        store.set_passphrase(PASS)
        before = store.current_hash()
        store.set_passphrase("somebody else wins")
        with self.assertRaises(auth.PassphraseChanged):
            store.set_passphrase("late writer wins!", expect_hash=before)
        self.assertTrue(store.check_passphrase("somebody else wins"))
        store.set_passphrase("fine now yes", expect_hash=store.current_hash())

    def test_compare_and_set_first_set_loses_to_concurrent_first_set(self) -> None:
        store = auth.AuthStore(self.path)
        self.assertIsNone(store.current_hash())
        store.set_passphrase(PASS)
        with self.assertRaises(auth.PassphraseChanged):
            store.set_passphrase("second first-setter", expect_hash=None)

    def test_no_fork_left_in_auth_module(self) -> None:
        text = (ROOT / "common" / "vesselstack_auth.py").read_text()
        self.assertNotIn("os.fork", text)
        self.assertNotIn("os.setuid", text)
        self.assertNotIn("setgroups", text)


class PairingStoreTests(unittest.TestCase):
    def store(self, clock=None, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return auth.AuthStore(Path(tmp.name) / "auth.json", clock=clock or Clock(), **kwargs)

    def test_code_format_and_normalisation(self) -> None:
        codes = {auth.new_pairing_code() for _ in range(50)}
        self.assertEqual(len(codes), 50)
        for code in codes:
            self.assertRegex(code, r"^[0-9A-HJKMNP-TV-Z]{10}$")
        self.assertEqual(auth.format_pairing_code("ABCDEFGHJK"), "ABCDE-FGHJK")
        self.assertEqual(auth.normalize_pairing_code("abcde-fghjk"), "ABCDEFGHJK")
        self.assertEqual(auth.normalize_pairing_code(" abcde fghjk "), "ABCDEFGHJK")
        self.assertEqual(auth.normalize_pairing_code("OILOI-LOILO"), "0110110110")
        for bad in ("", "short", "ABCDEFGHJKM", "ABCDE-FGHJU", None, 7, "ABCDE-FGHJ!"):
            self.assertIsNone(auth.normalize_pairing_code(bad))

    def test_only_the_hash_is_stored_and_redeem_is_single_use(self) -> None:
        store = self.store(expose_role=True)
        code, expires = store.create_pairing("access", "Crew phone")
        raw = store.path.read_text()
        self.assertNotIn(code, raw)
        self.assertIn(auth.token_digest(code), raw)
        token, device = store.redeem_pairing(code, "Pixel")
        self.assertEqual((device["role"], device["label"]), ("access", "Pixel"))
        self.assertEqual(device["expires"] - device["created"], auth.REMEMBER_SECONDS, "pair links always remember 30 days")
        self.assertEqual(store.verify_device_token(token)["id"], device["id"])
        self.assertIsNone(store.redeem_pairing(code, "again"), "single use")
        self.assertEqual(store.snapshot()["pairings"], [])

    def test_expiry_and_max_outstanding_and_pruning(self) -> None:
        clock = Clock()
        store = self.store(clock, expose_role=True)
        codes = [store.create_pairing()[0] for _ in range(auth.MAX_PAIRINGS)]
        with self.assertRaises(auth.AuthError):
            store.create_pairing()
        clock.now += auth.PAIRING_TTL_SECONDS + 1
        self.assertIsNone(store.redeem_pairing(codes[0], "late"), "expired")
        fresh = store.create_pairing()[0]  # expired ones were pruned
        self.assertEqual(store.pending_pairings(), 1)
        self.assertIsNotNone(store.redeem_pairing(fresh, "x"))

    def test_role_propagation_and_panel_cannot_issue_admin(self) -> None:
        chat = self.store(expose_role=True)
        with self.assertRaises(auth.AuthError):
            chat.create_pairing("admin")
        with self.assertRaises(auth.AuthError):
            chat.create_pairing("admin", "", auth.admin_binding(BKEY, ""))
        code, _ = chat.create_pairing("admin", "", auth.admin_binding(BKEY, "admin-code"))
        token, device = chat.redeem_pairing(code, "Skipper")
        self.assertEqual(device["role"], "admin")
        self.assertEqual(chat.verify_device_token(token)["binding"], auth.admin_binding(BKEY, "admin-code"))
        self.assertEqual(chat.prune_admin_devices(auth.admin_binding(BKEY, "rotated")), 1)
        self.assertEqual(chat.list_devices(), [])
        with self.assertRaises(auth.AuthError):
            self.store().create_pairing("admin")

    def test_device_limit_does_not_burn_the_code(self) -> None:
        store = self.store()
        for _ in range(auth.MAX_DEVICES):
            store.create_device("x", True)
        code, _ = store.create_pairing()
        with self.assertRaises(auth.DeviceLimitError):
            store.redeem_pairing(code, "x")
        self.assertEqual(store.pending_pairings(), 1)

    def test_pairings_survive_other_writes_and_are_cleared_by_revoke_all(self) -> None:
        store = self.store()
        store.create_pairing()
        self.assertEqual(store.pending_pairings(), 1)
        store.set_passphrase(PASS)
        self.assertEqual(store.pending_pairings(), 0, "setting or changing the passphrase clears waiting pairing links")
        store.create_pairing()
        store.set_passphrase("another passphrase here")
        self.assertEqual(store.pending_pairings(), 0)
        store.create_pairing()
        self.assertEqual(store.pending_pairings(), 1)
        store.revoke_all()
        self.assertEqual(store.pending_pairings(), 0)

    def test_links_and_terminal_qr(self) -> None:
        loop = auth.pairing_urls("ABCDEFGHJK", "127.0.0.1", 8780)
        self.assertEqual(loop, {"urls": ["http://127.0.0.1:8780/#pair=ABCDEFGHJK"], "tunnel": True})
        lan = auth.pairing_urls("ABCDEFGHJK", "0.0.0.0", 8765, hostname="boat.local", addresses=["198.51.100.5", "198.51.100.7"])
        self.assertEqual(lan["urls"], ["http://boat.local:8765/#pair=ABCDEFGHJK", "http://198.51.100.5:8765/#pair=ABCDEFGHJK", "http://198.51.100.7:8765/#pair=ABCDEFGHJK"])
        self.assertEqual(auth.pairing_urls("ABCDEFGHJK", "198.51.100.5", 80)["urls"], ["http://198.51.100.5:80/#pair=ABCDEFGHJK"])
        url = "http://boat.local:8780/#pair=ABCDEFGHJK"
        matrix = auth.qr_matrix(url)
        size = len(matrix)
        self.assertEqual(size, 29, "version 3 for this URL at level L")
        self.assertTrue(all(len(row) == size for row in matrix))
        finder = [[1] * 7, [1, 0, 0, 0, 0, 0, 1], [1, 0, 1, 1, 1, 0, 1], [1, 0, 1, 1, 1, 0, 1], [1, 0, 1, 1, 1, 0, 1], [1, 0, 0, 0, 0, 0, 1], [1] * 7]
        for ox, oy in ((0, 0), (size - 7, 0), (0, size - 7)):
            self.assertEqual([row[ox:ox + 7] for row in matrix[oy:oy + 7]], finder)
        text = auth.qr_terminal(url, color=False)
        lines = text.splitlines()
        self.assertEqual(len(lines), (size + 4 + 1) // 2)
        self.assertTrue(all(len(line) == size + 4 for line in lines))
        self.assertIn("\x1b[30;47m", auth.qr_terminal(url, color=True))
        self.assertNotIn("ABCDEFGHJK", text)


class ChatScopeTests(unittest.TestCase):
    def test_chat_store_minimum_roles_and_distinct_passphrases(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            panel = auth.AuthStore(Path(root) / "panel.json")
            panel.set_passphrase(PASS)
            chat = auth.AuthStore(Path(root) / "chat" / "auth.json", min_passphrase=auth.CHAT_MIN_PASSPHRASE_LENGTH,
                                  expose_role=True, inherit_owner=True,
                                  differs_from=lambda: panel.snapshot()["passphrase_hash"])
            with self.assertRaises(auth.AuthError):
                chat.set_passphrase("short")
            with self.assertRaises(auth.AuthError):
                chat.set_passphrase(PASS)
            chat.set_passphrase("crew pass")
            token, device = chat.create_device("Phone", True)
            self.assertEqual(device["role"], "access")
            self.assertEqual(chat.list_devices()[0]["role"], "access")
            self.assertEqual(chat.verify_device_token(token)["role"], "access")
            self.assertEqual(stat.S_IMODE(os.stat(chat.path).st_mode), 0o600)
            self.assertNotIn("role", panel.create_device("x", False)[1], "panel devices do not expose a role")
            self.assertEqual(chat.create_device("Admin", False, role="admin")[1]["role"], "admin")
            self.assertEqual(chat.create_device("Bogus", False, role="root")[1]["role"], "access")

    def test_panel_refuses_the_chat_passphrase_and_cli_covers_both(self) -> None:
        cli = load("vesselstack_authctl_under_test", "common/vesselstack_authctl.py")
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as data:
            (Path(data) / "boat-chat").mkdir()

            def run(args, stdin=""):
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.dict(os.environ, {"VESSELSTACK_ROOT": root, "VESSELSTACK_DATA": data}):
                    code = cli.main(args, io.StringIO(stdin), out, err)
                return code, out.getvalue(), err.getvalue()
            self.assertEqual(run(["passphrase", "set", "chat", "--stdin"], "short\n")[0], 1)
            self.assertEqual(run(["passphrase", "set", "chat", "--stdin"], "crew pass\n")[0], 0)
            code, _, err = run(["passphrase", "set", "panel", "--stdin"], "crew pass and more\n")
            self.assertEqual(code, 0)
            code, _, err = run(["passphrase", "set", "chat", "--stdin"], "crew pass and more\n")
            self.assertEqual(code, 1)
            self.assertIn("different from the Control Panel", err)
            code, _, err = run(["passphrase", "set", "panel", "--stdin"], "crew pass\n")
            self.assertEqual(code, 1)
            self.assertEqual(run(["passphrase", "status", "chat"])[1].splitlines()[0], "chat passphrase: set")
            chat_store = auth.AuthStore(Path(data) / "boat-chat" / "auth.json", expose_role=True)
            chat_store.create_device("Tablet", True)
            self.assertIn("access", run(["devices", "list", "chat"])[1])
            self.assertEqual(run(["devices", "list", "panel"])[1], "No signed-in devices.\n")
            self.assertIn("Revoked 1", run(["devices", "revoke-all", "chat"])[1])
            self.assertEqual(run(["passphrase", "clear", "chat"])[0], 0)
            self.assertFalse(chat_store.passphrase_set())
            self.assertTrue(auth.AuthStore(Path(root) / "config" / "control-panel-auth.json").passphrase_set())


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


class PairLinkCliTests(unittest.TestCase):
    def run_cli(self, args, env_extra=None, settings_token=None, override_token=None):
        cli = load("vesselstack_authctl_under_test", "common/vesselstack_authctl.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = Path(self.tmp.name) / "data"
        (data / "boat-chat").mkdir(parents=True)
        if override_token is not None:
            (data / "boat-chat" / "boat-chat.env").write_text(f"BOAT_CHAT_SETTINGS_TOKEN={override_token}\n")
        if settings_token:
            (Path(self.tmp.name) / "config").mkdir()
            (Path(self.tmp.name) / "config" / "boat-chat.env").write_text(f"BOAT_CHAT_PORT='8765'\nBOAT_CHAT_SETTINGS_TOKEN='{settings_token}'\n")
        out, err = io.StringIO(), io.StringIO()
        env = {"VESSELSTACK_ROOT": self.tmp.name, "VESSELSTACK_DATA": str(data), **(env_extra or {})}
        with mock.patch.dict(os.environ, env):
            code = cli.main(args, io.StringIO(), out, err)
        return code, out.getvalue(), err.getvalue(), data

    def test_panel_link_on_loopback_prints_tunnel_form_code_and_qr(self) -> None:
        code, out, err, _ = self.run_cli(["panel-link"], {"VS_PANEL_HOST": "127.0.0.1", "VS_PANEL_PORT": "8780"})
        self.assertEqual((code, err), (0, ""))
        import re
        match = re.search(r"http://127\.0\.0\.1:8780/#pair=([0-9A-Z]{10})", out)
        self.assertTrue(match, out)
        self.assertIn(f"{match.group(1)[:5]}-{match.group(1)[5:]}", out)
        self.assertIn("ssh -L 8780:127.0.0.1:8780", out)
        self.assertIn("panel-access lan", out)
        self.assertIn("\u2580", out + "\u2580")  # QR block characters are present
        self.assertRegex(out, "[\u2580\u2584\u2588]")
        self.assertNotIn("passphrase_hash", out)
        self.assertNotIn("token_sha256", out)

    def test_admin_binding_uses_the_effective_token_override_first_then_env(self) -> None:
        def binding(**kwargs):
            code, _, err, data = self.run_cli(["panel-link", "--for", "chat", "--admin"], {"VS_CHAT_HOST": "198.51.100.9"}, **kwargs)
            stored = json.loads((data / "boat-chat" / "auth.json").read_text()) if code == 0 else {}
            return code, stored.get("pairings", [{}])[0].get("binding"), stored.get("binding_key")
        code, from_env, key = binding(settings_token="env-code")
        self.assertEqual((code, from_env), (0, auth.admin_binding(key, "env-code")))
        code, from_override, key = binding(settings_token="env-code", override_token="'settings-page-code'")
        self.assertEqual((code, from_override), (0, auth.admin_binding(key, "settings-page-code")), "the Settings-page value is what Boat Chat enforces")
        code, only_override, key = binding(override_token="override-only")
        self.assertEqual((code, only_override), (0, auth.admin_binding(key, "override-only")))
        self.assertEqual(binding(settings_token="env-code", override_token="")[0], 1, "an explicit empty override means no admin code")

    def test_chat_admin_link_and_validation(self) -> None:
        code, out, err, data = self.run_cli(["panel-link", "--for", "chat", "--admin"], {"VS_CHAT_HOST": "198.51.100.9", "VS_CHAT_PORT": "8765"})
        self.assertEqual(code, 1, "no BOAT_CHAT_SETTINGS_TOKEN: admin pairing is refused")
        self.assertIn("BOAT_CHAT_SETTINGS_TOKEN", err)
        self.assertFalse((data / "boat-chat" / "auth.json").exists())
        code, out, err, data = self.run_cli(["panel-link", "--for", "chat", "--admin", "--label", "Skipper"], {"VS_CHAT_HOST": "198.51.100.9", "VS_CHAT_PORT": "8765"}, settings_token="admin-code")
        self.assertEqual(code, 0, err)
        self.assertIn("http://198.51.100.9:8765/#pair=", out)
        self.assertIn("(admin)", out)
        stored = json.loads((data / "boat-chat" / "auth.json").read_text())
        self.assertEqual([p["role"] for p in stored["pairings"]], ["admin"])
        self.assertEqual(stored["pairings"][0]["binding"], auth.admin_binding(stored["binding_key"], "admin-code"))
        self.assertRegex(stored["binding_key"], "^[0-9a-f]{64}$")
        self.assertTrue(stored["pairings"][0]["binding"].startswith("h1:"))
        self.assertEqual(self.run_cli(["panel-link", "--admin"])[0], 2)
        self.assertEqual(self.run_cli(["panel-link", "--for", "nope"])[0], 2)
        self.assertEqual(self.run_cli(["panel-link", "--bogus"])[0], 2)

    def test_fourth_link_is_refused(self) -> None:
        cli = load("vesselstack_authctl_under_test", "common/vesselstack_authctl.py")
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"VESSELSTACK_ROOT": root, "VESSELSTACK_DATA": root}):
                codes = [cli.main(["panel-link"], None, io.StringIO(), io.StringIO()) for _ in range(4)]
        self.assertEqual(codes, [0, 0, 0, 1])


if __name__ == "__main__":
    unittest.main()


class InstallerImportTests(unittest.TestCase):
    """install.sh imports wizard hashes with `vesselstack_authctl.py passphrase set-hash` and keeps them out of env files."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "config").mkdir()
        (self.root / "data" / "boat-chat").mkdir(parents=True)

    def run_ctl(self, scope: str, text: str, *flags: str):
        import subprocess, sys
        env = {"PATH": os.environ.get("PATH", ""), "VESSELSTACK_ROOT": str(self.root), "VESSELSTACK_DATA": str(self.root / "data")}
        return subprocess.run([sys.executable, "-I", str(ROOT / "common" / "vesselstack_authctl.py"), "passphrase", "set-hash", scope, *flags],
                              input=text + "\n", capture_output=True, text=True, env=env)

    def test_valid_hash_is_imported_into_each_store(self) -> None:
        for scope, relative in (("panel", "config/control-panel-auth.json"), ("chat", "data/boat-chat/auth.json")):
            encoded = auth.hash_passphrase("imported passphrase")
            done = self.run_ctl(scope, encoded)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue(auth.AuthStore(self.root / relative).check_passphrase("imported passphrase"))
            self.assertNotIn("imported passphrase", done.stdout + done.stderr)

    def test_if_unset_stores_only_when_no_passphrase_exists(self) -> None:
        for scope, relative in (("panel", "config/control-panel-auth.json"), ("chat", "data/boat-chat/auth.json")):
            store = auth.AuthStore(self.root / relative)
            first, later = auth.hash_passphrase("first passphrase"), auth.hash_passphrase("later passphrase")
            done = self.run_ctl(scope, first, "--if-unset")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue(store.check_passphrase("first passphrase"))
            done = self.run_ctl(scope, later, "--if-unset")
            self.assertEqual(done.returncode, 3, done.stderr)
            self.assertIn("already set", done.stderr)
            self.assertTrue(store.check_passphrase("first passphrase"))
            self.assertFalse(store.check_passphrase("later passphrase"))
            # Without the flag the explicit command still replaces it.
            self.assertEqual(self.run_ctl(scope, later).returncode, 0)
            self.assertTrue(store.check_passphrase("later passphrase"))

    def test_if_unset_still_refuses_a_malformed_hash(self) -> None:
        self.assertNotIn(self.run_ctl("panel", "scrypt:bad", "--if-unset").returncode, (0, 3))
        self.assertFalse((self.root / "config" / "control-panel-auth.json").exists())

    def test_scrub_is_per_key_and_follows_each_import(self) -> None:
        """Run the installer's own functions with a stub authctl: a failed import keeps only its own key."""
        import subprocess
        text = (ROOT / "install.sh").read_text()
        start, end = text.index("scrub_signin_hash() {"), text.index("import_signin_hash CONTROL_PANEL_PASSPHRASE_HASH")
        functions = text[start:end]
        common = self.root / "app" / "common"
        common.mkdir(parents=True)
        # Stub: exit status comes from STUB_<SCOPE>; records that --if-unset was passed.
        (common / "vesselstack_authctl.py").write_text(
            "import os, sys\nassert sys.argv[1:3] == ['passphrase', 'set-hash'] and '--if-unset' in sys.argv, sys.argv\n"
            "sys.exit(int(os.environ['STUB_' + sys.argv[3].upper()]))\n")
        config, installed = self.root / "vesselstack.env", self.root / "app" / "config" / "vesselstack.env"
        installed.parent.mkdir(parents=True)

        def run(panel: int, chat: int) -> tuple[str, str, str]:
            for path in (config, installed):
                path.write_text("KEEP=1\nCONTROL_PANEL_PASSPHRASE_HASH=scrypt:p\nBOAT_CHAT_PASSPHRASE_HASH=scrypt:c\nOTHER=2\n")
            script = (f"CONFIG_FILE={config}; VESSELSTACK_ROOT={self.root / 'app'}; VESSELSTACK_DATA={self.root}\n"
                      f"CONTROL_PANEL_PASSPHRASE_HASH=scrypt:p; BOAT_CHAT_PASSPHRASE_HASH=scrypt:c\n{functions}\n"
                      'import_signin_hash CONTROL_PANEL_PASSPHRASE_HASH panel "Control Panel" || true\n'
                      'import_signin_hash BOAT_CHAT_PASSPHRASE_HASH chat "Boat Chat" || true\n')
            done = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                  env={"PATH": os.environ.get("PATH", ""), "STUB_PANEL": str(panel), "STUB_CHAT": str(chat)})
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(config.read_text(), installed.read_text())
            return config.read_text(), done.stdout, done.stderr

        left, out, _ = run(0, 0)
        self.assertEqual(left, "KEEP=1\nOTHER=2\n")
        left, out, err = run(1, 0)
        self.assertEqual(left, "KEEP=1\nCONTROL_PANEL_PASSPHRASE_HASH=scrypt:p\nOTHER=2\n")
        self.assertIn("sudo vesselstackctl passphrase set panel", err)
        left, out, _ = run(0, 1)
        self.assertEqual(left, "KEEP=1\nBOAT_CHAT_PASSPHRASE_HASH=scrypt:c\nOTHER=2\n")
        left, out, _ = run(3, 3)
        self.assertEqual(left, "KEEP=1\nOTHER=2\n")
        self.assertIn("a Control Panel passphrase is already set; the one from the installer settings was ignored", out)
        self.assertIn("sudo vesselstackctl passphrase set panel", out)
        self.assertIn("a Boat Chat passphrase is already set", out)

    def test_malformed_hash_is_refused_and_nothing_is_written(self) -> None:
        for bad in ("imported passphrase", "scrypt:15:8:1:AAAA:BBBB", "", "scrypt:99:8:1:" + "A" * 22 + "==:" + "A" * 43 + "="):
            done = self.run_ctl("panel", bad)
            self.assertNotEqual(done.returncode, 0, bad)
        self.assertFalse((self.root / "config" / "control-panel-auth.json").exists())

    def test_installer_lists_and_scrubs_the_hash_keys(self) -> None:
        text = (ROOT / "install.sh").read_text()
        keys = ("CONTROL_PANEL_PASSPHRASE_HASH", "BOAT_CHAT_PASSPHRASE_HASH")
        extra = text[text.index("extra_env_keys() {"):text.index("# env_value_problem VALUE")]
        secret = text[text.index("config_key_is_secret() {"):text.index("# config_env_snapshot")]
        written = text[text.index("vesselstack_env_keys() {"):text.index("# Keys written to boat-chat.env")]
        for key in keys:
            self.assertIn(key, extra)
            self.assertIn(key, secret)
            self.assertNotIn(key, written, "never written to vesselstack.env")
        self.assertIn("scrub_signin_hash", text)
        self.assertIn("passphrase set-hash", text)
        self.assertIn("--if-unset", text)


class CorruptStoreTests(unittest.TestCase):
    """Wrong types anywhere in the store are corruption: fail closed (no passphrase, no devices), never overwrite."""

    CASES = {
        "devices_not_a_list": {"schema": 1, "devices": 5},
        "pairings_not_a_list": {"schema": 1, "pairings": "x"},
        "created_is_a_list": {"schema": 1, "devices": [{"id": "a", "token_sha256": "b", "created": [1]}]},
        "expires_is_a_dict": {"schema": 1, "devices": [{"id": "a", "token_sha256": "b", "expires": {}}]},
        "last_seen_is_text": {"schema": 1, "devices": [{"id": "a", "token_sha256": "b", "last_seen": "soon"}]},
        "pairing_created_bool": {"schema": 1, "pairings": [{"id": "a", "code_sha256": "b", "created": True}]},
        "root_is_a_list": [],
        "root_is_text": "text",
        "wrong_schema": {"schema": 2},
    }

    def make(self, content: str) -> tuple[Path, "auth.AuthStore"]:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        path = directory / "auth.json"
        path.write_text(content)
        return path, auth.AuthStore(path, min_passphrase=8, expose_role=True)

    def test_every_malformed_shape_fails_closed_and_is_not_overwritten(self) -> None:
        for name, document in {**self.CASES, "infinite_number": None}.items():
            text = '{"schema": 1, "devices": [{"id": "a", "token_sha256": "b", "created": Infinity}]}' if document is None else json.dumps(document)
            with self.subTest(name):
                path, store = self.make(text)
                self.assertFalse(store.passphrase_set())
                self.assertFalse(store.check_passphrase("anything at all"))
                self.assertIsNone(store.current_hash())
                self.assertEqual(store.list_devices(), [])
                self.assertIsNone(store.verify_device_token("x" * 40))
                self.assertEqual(store.pending_pairings(), 0)
                for mutate in (lambda: store.set_passphrase("a good passphrase"), lambda: store.create_device("x", True),
                               lambda: store.revoke_all(), lambda: store.create_pairing(), lambda: store.ensure_binding_key()):
                    with self.assertRaises(auth.StoreError):
                        mutate()
                self.assertEqual(path.read_text(), text, "a store that cannot be understood is never rewritten")

    def test_garbage_bytes_and_deep_nesting_are_corruption(self) -> None:
        for text in ("{" * 100000, "\x00\x01", "not json", ""):
            path, store = self.make(text)
            self.assertFalse(store.passphrase_set())
            with self.assertRaises(auth.StoreError):
                store.revoke_all()

    def test_null_lists_and_unknown_extra_keys_are_fine(self) -> None:
        path, store = self.make(json.dumps({"schema": 1, "devices": None, "pairings": None, "future": {"x": 1}}))
        token, _ = store.create_device("ok", True)
        self.assertIsNotNone(store.verify_device_token(token))
        self.assertEqual(json.loads(path.read_text())["future"], {"x": 1})


class AdminBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "auth.json"
        self.store = auth.AuthStore(self.path, min_passphrase=8, expose_role=True)

    def test_binding_is_an_hmac_under_a_random_stored_key(self) -> None:
        self.assertEqual(self.store.admin_binding(""), "none")
        self.assertIsNone(self.store.admin_binding("admin-code"), "a check never creates the key")
        self.assertFalse(self.path.exists())
        first = self.store.admin_binding("admin-code", create=True)
        key = json.loads(self.path.read_text())["binding_key"]
        self.assertRegex(key, "^[0-9a-f]{64}$")
        self.assertEqual(first, auth.admin_binding(key, "admin-code"))
        self.assertTrue(first.startswith("h1:"))
        self.assertNotEqual(first, "h1:" + auth.token_digest("admin-code"), "not a plain hash of the code")
        self.assertEqual(self.store.admin_binding(" admin-code ", create=True), first)
        self.assertNotEqual(self.store.admin_binding("rotated", create=True), first)
        other = auth.AuthStore(Path(self.directory.name) / "other.json")
        self.assertNotEqual(other.admin_binding("admin-code", create=True), first, "each store has its own key")

    def test_key_survives_passphrase_and_revoke_operations(self) -> None:
        binding = self.store.admin_binding("admin-code", create=True)
        self.store.set_passphrase("a good passphrase")
        self.store.revoke_all()
        self.store.clear_passphrase()
        self.assertEqual(self.store.admin_binding("admin-code"), binding)

    def test_legacy_sha256_binding_never_matches(self) -> None:
        legacy = auth.token_digest("admin-code")  # the previous, unkeyed format
        code, _ = self.store.create_pairing("access")
        token, _ = self.store.redeem_pairing(code, "old admin")
        self.store.transaction(lambda data: data["devices"][0].update(role="admin", binding=legacy))
        current = self.store.admin_binding("admin-code", create=True)
        self.assertNotEqual(self.store.verify_device_token(token)["binding"], current)
        self.assertEqual(self.store.prune_admin_devices(current), 1, "pruned as soon as the admin code is next checked")

    def test_admin_pairing_rejects_legacy_and_none_bindings(self) -> None:
        for bad in ("", "none", auth.token_digest("admin-code")):
            with self.assertRaises(auth.AuthError):
                self.store.create_pairing("admin", "", bad)

    def test_effective_setting_reads_override_file_first_then_base(self) -> None:
        base, override = Path(self.directory.name) / "base.env", Path(self.directory.name) / "override.env"
        key = "BOAT_CHAT_SETTINGS_TOKEN"
        self.assertEqual(auth.effective_chat_setting(key, override, base), "")
        base.write_text("BOAT_NAME=x\nBOAT_CHAT_SETTINGS_TOKEN='from base'\n")
        self.assertEqual(auth.effective_chat_setting(key, override, base), "from base")
        override.write_text("# c\nBOAT_CHAT_SETTINGS_TOKEN=from-override\n")
        self.assertEqual(auth.effective_chat_setting(key, override, base), "from-override")
        override.write_text("BOAT_CHAT_SETTINGS_TOKEN=\n")
        self.assertEqual(auth.effective_chat_setting(key, override, base), "", "an explicit empty override wins, as in Boat Chat")
        override.unlink()
        override.symlink_to(base)
        self.assertEqual(auth.effective_chat_setting(key, override, base), "from base", "a symlinked override is ignored")
        self.assertEqual(auth.effective_chat_setting(key, None, None), "")


class RotationWiringTests(unittest.TestCase):
    def test_installer_revokes_after_a_token_rotation_and_ctl_hardens_the_helper(self) -> None:
        text = (ROOT / "install.sh").read_text()
        self.assertIn('revoke_after_rotation panel "Control Panel" "$PREVIOUS_CONTROL_PANEL_TOKEN" "$CONTROL_PANEL_TOKEN"', text)
        self.assertIn('revoke_after_rotation chat "Boat Chat" "$PREVIOUS_BOAT_CHAT_ACCESS_TOKEN"', text)
        self.assertIn('revoke_after_rotation chat "Boat Chat" "$PREVIOUS_BOAT_CHAT_SETTINGS_TOKEN"', text)
        self.assertIn('vesselstack_authctl.py" devices revoke-all "$scope"', text)
        self.assertLess(text.index("PREVIOUS_CONTROL_PANEL_TOKEN=$("), text.index("mv -f -- \"$control_panel_temp\""), "old values are read before the files are replaced")
        ctl = (ROOT / "scripts" / "vesselstackctl").read_text()
        self.assertIn('python3 -B -I "$helper" "$@"', ctl)
