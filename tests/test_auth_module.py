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

    def test_success_clears_the_address(self) -> None:
        throttle = auth.LoginThrottle(Clock())
        for _ in range(5):
            throttle.failure("192.0.2.5")
        self.assertGreater(throttle.retry_after("192.0.2.5"), 0)
        _, _, handle = throttle.begin("192.0.2.6")
        throttle.failure("192.0.2.6")  # unrelated address
        ok, _, mine = throttle.begin("192.0.2.7")
        throttle.success(mine)
        self.assertEqual(throttle.retry_after("192.0.2.7"), 0)
        self.assertGreater(throttle.retry_after("192.0.2.5"), 0, "success clears only its own address")
        with self.assertRaises(TypeError):
            throttle.success("192.0.2.5")

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

    def test_root_over_foreign_directory_drops_privileges_and_never_chowns(self) -> None:
        marker = self.dir.parent / "dropped"
        calls = []

        class Store(auth.AuthStore):
            def _euid(self) -> int:
                return 0

            def _parent_owner(self) -> tuple[int, int]:
                return 4242, 4343

            def _drop_privileges(self, uid: int, gid: int) -> None:
                marker.write_text(f"{uid}:{gid}")  # runs in the forked child

        store = Store(self.path, inherit_owner=True, expose_role=True)
        with mock.patch("os.fchown", side_effect=lambda *a: calls.append(a)), mock.patch("os.chown", side_effect=lambda *a, **k: calls.append(a)):
            store.set_passphrase(PASS)
            token, device = store.create_device("Phone", True, role="admin")
            self.assertEqual(device["role"], "admin")
            self.assertEqual(store.verify_device_token(token)["role"], "admin")
            with self.assertRaises(auth.DeviceLimitError):
                for _ in range(auth.MAX_DEVICES + 1):
                    store.create_device("x", True)
        self.assertEqual(marker.read_text(), "4242:4343")
        self.assertEqual(calls, [])
        self.assertTrue(store.check_passphrase(PASS))

    def test_root_over_root_owned_directory_stays_in_process(self) -> None:
        class Store(auth.AuthStore):
            def _euid(self) -> int:
                return 0

            def _parent_owner(self) -> tuple[int, int]:
                return 0, 0

            def _transaction_as(self, owner, mutate):
                raise AssertionError("must not fork for a root-owned directory")
        Store(self.path, inherit_owner=True).set_passphrase(PASS)

    def test_non_root_never_forks(self) -> None:
        class Store(auth.AuthStore):
            def _transaction_as(self, owner, mutate):
                raise AssertionError("must not fork when not root")
        Store(self.path, inherit_owner=True).set_passphrase(PASS)


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
        code, _ = chat.create_pairing("admin")
        _, device = chat.redeem_pairing(code, "Skipper")
        self.assertEqual(device["role"], "admin")
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
        store.set_passphrase(PASS)
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
    def run_cli(self, args, env_extra=None):
        cli = load("vesselstack_authctl_under_test", "common/vesselstack_authctl.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = Path(self.tmp.name) / "data"
        (data / "boat-chat").mkdir(parents=True)
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

    def test_chat_admin_link_and_validation(self) -> None:
        code, out, err, data = self.run_cli(["panel-link", "--for", "chat", "--admin", "--label", "Skipper"], {"VS_CHAT_HOST": "198.51.100.9", "VS_CHAT_PORT": "8765"})
        self.assertEqual(code, 0, err)
        self.assertIn("http://198.51.100.9:8765/#pair=", out)
        self.assertIn("(admin)", out)
        stored = json.loads((data / "boat-chat" / "auth.json").read_text())
        self.assertEqual([p["role"] for p in stored["pairings"]], ["admin"])
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
