#!/usr/bin/env python3
"""Boat Chat passphrase sign-in: endpoints, auth matrix, Host/Origin rules, admin code unchanged."""
import http.client
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "boat-chat"))
import app

MANAGED = ("BOAT_CHAT_ACCESS_TOKEN", "BOAT_CHAT_ALLOW_OPEN", "BOAT_CHAT_SETTINGS_TOKEN", "BOAT_CHAT_ENV",
           "BOAT_CHAT_STATE_DIR", "BOAT_CHAT_ALLOWED_HOSTS", "BOAT_CHAT_MEMORY_DB", "BOAT_CHAT_PROVIDER")
CODE = "access-code-for-tests"
ADMIN = "admin-code-for-tests"
CREW = "crew passphrase"


class SignInCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {key: os.environ.pop(key, None) for key in MANAGED}
        os.environ["BOAT_CHAT_STATE_DIR"] = self.tmp.name
        os.environ["BOAT_CHAT_MEMORY_DB"] = str(Path(self.tmp.name) / "m.sqlite")
        os.environ["BOAT_CHAT_PROVIDER"] = "local"
        os.environ["BOAT_CHAT_ACCESS_TOKEN"] = CODE
        os.environ["BOAT_CHAT_SETTINGS_TOKEN"] = ADMIN
        for patch in (mock.patch.object(app, "ENV_CONFIG", Path(self.tmp.name) / "boat-chat.env"),
                      mock.patch.object(app, "LOGIN_THROTTLE", app.vsauth.LoginThrottle()),
                      mock.patch.dict(app._AUTH_STORES, clear=True),
                      mock.patch.dict(app._HOST_ALLOWLISTS, clear=True)):
            patch.start()
            self.addCleanup(patch.stop)
        app.RATE_LIMIT_REQUESTS.clear()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.BoatChatHandler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    @property
    def origin(self):
        return {"Origin": f"http://127.0.0.1:{self.port}"}

    def call(self, method, path, payload=None, headers=None, host=None, origin=True):
        all_headers = {"Content-Type": "application/json"}
        if method == "POST" and origin:
            all_headers.update(self.origin)
        all_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.putrequest(method, path, skip_host=True)
            connection.putheader("Host", host if host is not None else f"127.0.0.1:{self.port}")
            data = json.dumps(payload).encode() if payload is not None else (b"{}" if method == "POST" else None)
            for name, value in all_headers.items():
                connection.putheader(name, value)
            if data is not None:
                connection.putheader("Content-Length", str(len(data)))
            connection.endheaders(data)
            response = connection.getresponse()
            raw = response.read()
            try:
                return response.status, json.loads(raw or b"{}")
            except ValueError:
                return response.status, {"text": raw.decode()}
        finally:
            connection.close()

    def code(self):
        return {"X-Boat-Chat-Token": CODE}

    def admin(self):
        return {"X-Boat-Chat-Token": ADMIN}

    def set_passphrase(self, value=CREW):
        """The first passphrase needs the admin code (or an admin device), not just the access code."""
        status, body = self.call("POST", "/api/auth/passphrase", {"new": value}, self.admin())
        self.assertEqual(status, 200, body)

    def login(self, passphrase=CREW, remember=True, **kwargs):
        return self.call("POST", "/api/auth/login", {"passphrase": passphrase, "remember": remember, "label": "Phone"}, **kwargs)

    def device(self, token):
        return {"X-Boat-Chat-Device": token}

    def redeem_admin(self, store):
        code, _ = store.create_pairing("admin", "", store.admin_binding(ADMIN, create=True))
        status, body = self.call("POST", "/api/auth/pair", {"code": code})
        self.assertEqual((status, body["role"]), (200, "admin"), body)
        return body


class EndpointTests(SignInCase):
    def test_status_and_login_off_until_set(self):
        self.assertEqual(self.call("GET", "/api/auth/status"), (200, {"passphrase_set": False}))
        self.assertEqual(self.login()[0], 409)
        self.set_passphrase()
        self.assertEqual(self.call("GET", "/api/auth/status"), (200, {"passphrase_set": True}))

    def test_first_passphrase_needs_admin_and_minimum_length(self):
        self.assertEqual(self.call("POST", "/api/auth/passphrase", {"new": CREW})[0], 401)
        status, body = self.call("POST", "/api/auth/passphrase", {"new": CREW}, self.code())
        self.assertEqual(status, 403, "mere access (the access code) cannot set the first passphrase")
        self.assertIn("admin code", body["error"])
        self.assertFalse(self.call("GET", "/api/auth/status")[1]["passphrase_set"])
        status, body = self.call("POST", "/api/auth/passphrase", {"new": "short"}, self.admin())
        self.assertEqual(status, 400)
        self.assertIn("at least 8", body["error"])
        self.set_passphrase("12345678")

    def test_login_issues_access_role_device_and_stores_only_a_hash(self):
        self.set_passphrase()
        status, body = self.login(remember=False)
        self.assertEqual((status, body["role"]), (200, "access"))
        token = body["device_token"]
        raw = (Path(self.tmp.name) / "auth.json")
        self.assertNotIn(token, raw.read_text())
        self.assertEqual(stat.S_IMODE(raw.stat().st_mode), 0o600)
        status, devices = self.call("GET", "/api/auth/devices", headers=self.device(token))
        self.assertEqual(status, 200)
        self.assertEqual([(d["role"], d["current"]) for d in devices["devices"]], [("access", True)])
        self.assertNotIn("token_sha256", json.dumps(devices))

    def test_wrong_passphrase_is_generic_and_throttled(self):
        self.set_passphrase()
        for _ in range(5):
            self.assertEqual(self.login(passphrase="wrong wrong")[0], 401)
        status, body = self.login()
        self.assertEqual(status, 429)
        self.assertGreater(body["retry_after"], 0)
        self.assertEqual(self.call("GET", "/api/settings", headers=self.code())[0], 200, "access code unaffected")

    def test_concurrent_attempts_are_counted_before_evaluation(self):
        self.set_passphrase()
        evaluated, results = [], []
        real = app.vsauth.AuthStore.check_passphrase

        def counting(store, candidate):
            evaluated.append(candidate)
            return real(store, candidate)
        with mock.patch.object(app.vsauth.AuthStore, "check_passphrase", counting):
            threads = [threading.Thread(target=lambda: results.append(self.login(passphrase="wrong wrong")[0])) for _ in range(12)]
            [t.start() for t in threads]
            [t.join() for t in threads]
        self.assertEqual(len(evaluated), 5)
        self.assertEqual(results.count(429), 7)

    def test_bad_payloads(self):
        self.set_passphrase()
        self.assertEqual(self.call("POST", "/api/auth/login", {"passphrase": 5, "remember": True})[0], 400)
        self.assertEqual(self.call("POST", "/api/auth/login", {"passphrase": CREW, "remember": "yes"})[0], 400)

    def test_logout_revoke_and_revoke_all(self):
        self.set_passphrase()
        first, second = self.login()[1], self.login()[1]
        mine = self.device(first["device_token"])
        self.assertEqual(self.call("POST", "/api/auth/devices/revoke", {"id": "missing"}, self.admin())[0], 404)
        self.assertEqual(self.call("POST", "/api/auth/devices/revoke", {}, self.admin())[0], 400)
        self.assertEqual(self.call("POST", "/api/auth/devices/revoke", {"id": second["device_id"]}, self.admin())[0], 200)
        self.assertEqual(self.call("GET", "/api/settings", headers=self.device(second["device_token"]))[0], 401)
        self.assertEqual(self.call("POST", "/api/auth/logout", {}, mine)[0], 200)
        self.assertEqual(self.call("GET", "/api/settings", headers=mine)[0], 401)
        third = self.login()[1]
        self.assertEqual(self.call("POST", "/api/auth/devices/revoke", {"id": "all"}, self.admin())[0], 200)
        self.assertEqual(self.call("GET", "/api/settings", headers=self.device(third["device_token"]))[0], 401)
        self.assertEqual(self.call("GET", "/api/auth/devices")[0], 401)

    def test_crew_may_revoke_only_their_own_device(self):
        self.set_passphrase()
        mine, other = self.login()[1], self.login()[1]
        crew = self.device(mine["device_token"])
        for headers in (crew, self.code()):
            for target in ("all", other["device_id"], "missing", ""):
                status, body = self.call("POST", "/api/auth/devices/revoke", {"id": target}, headers)
                self.assertEqual(status, 403, (target, body))
            self.assertEqual(self.call("POST", "/api/auth/devices/revoke", {}, headers)[0], 403)
        self.assertEqual(self.call("GET", "/api/settings", headers=self.device(other["device_token"]))[0], 200, "nothing was revoked")
        status, body = self.call("GET", "/api/auth/devices", headers=crew)
        self.assertEqual((status, body["admin"]), (200, False))
        status, body = self.call("POST", "/api/auth/devices/revoke", {"id": mine["device_id"]}, crew)
        self.assertEqual((status, body["revoked_current"]), (200, True))
        self.assertEqual(self.call("GET", "/api/settings", headers=crew)[0], 401)

    def test_admin_code_or_admin_device_may_revoke_others_and_everyone(self):
        self.set_passphrase()
        store = app.chat_auth_store()
        admin = self.redeem_admin(store)
        crew = [self.login()[1] for _ in range(2)]
        status, body = self.call("GET", "/api/auth/devices", headers=self.admin())
        self.assertEqual((status, body["admin"]), (200, True), "the admin code alone is enough to list and manage")
        self.assertEqual(self.call("POST", "/api/auth/devices/revoke", {"id": crew[0]["device_id"]}, self.device(admin["device_token"]))[0], 200)
        self.assertEqual(self.call("POST", "/api/auth/devices/revoke", {"id": "all"}, self.admin())[0], 200)
        for device in (crew[1], admin):
            self.assertEqual(self.call("GET", "/api/settings", headers=self.device(device["device_token"]))[0], 401)

    def test_rotating_the_access_code_in_settings_signs_out_all_devices(self):
        self.set_passphrase()
        a, b = self.login()[1], self.login()[1]
        store = app.chat_auth_store()
        store.create_pairing("access")
        with mock.patch.object(app, "ENV_CONFIG", Path(self.tmp.name) / "boat-chat.env"):
            result = app.update_settings({"BOAT_CHAT_MAX_TOKENS": "321"})
            self.assertNotIn("devices_signed_out", result)
            self.assertEqual(len(store.list_devices()), 2, "unrelated settings keep devices")
            result = app.update_settings({"BOAT_CHAT_ACCESS_TOKEN": CODE})
            self.assertEqual(len(store.list_devices()), 2, "saving the same code is not a rotation")
            result = app.update_settings({"BOAT_CHAT_ACCESS_TOKEN": "a-brand-new-access-code"})
        self.assertEqual(result["devices_signed_out"], 2)
        self.assertEqual((store.list_devices(), store.pending_pairings()), ([], 0))
        self.assertEqual(self.call("GET", "/api/settings", headers=self.device(a["device_token"]))[0], 401)

    def test_rotating_the_admin_code_in_settings_ends_admin_devices_only(self):
        self.set_passphrase()
        store = app.chat_auth_store()
        admin = self.redeem_admin(store)
        crew = self.login()[1]
        with mock.patch.object(app, "ENV_CONFIG", Path(self.tmp.name) / "boat-chat.env"):
            app.update_settings({"BOAT_CHAT_SETTINGS_TOKEN": "a-brand-new-admin-code"})
        self.assertEqual(self.call("GET", "/api/settings", headers=self.device(admin["device_token"]))[0], 401, "admin device pruned")
        self.assertEqual(self.call("GET", "/api/settings", headers=self.device(crew["device_token"]))[0], 200)

    def test_legacy_unkeyed_admin_binding_is_not_admin(self):
        self.set_passphrase()
        store = app.chat_auth_store()
        code, _ = store.create_pairing("access")
        device = self.call("POST", "/api/auth/pair", {"code": code})[1]
        store.transaction(lambda data: data["devices"][0].update(role="admin", binding=app.vsauth.token_digest(ADMIN)))
        store.admin_binding(ADMIN, create=True)
        self.assertEqual(self.call("POST", "/api/settings", {"settings": {}}, self.device(device["device_token"]))[0], 403)
        self.assertEqual(self.call("POST", "/api/auth/devices/revoke", {"id": "all"}, self.device(device["device_token"]))[0], 403)

    def test_corrupt_store_degrades_without_a_500(self):
        (Path(self.tmp.name) / "auth.json").write_text(json.dumps({"schema": 1, "devices": 5, "pairings": [{"created": []}]}))
        self.assertEqual(self.call("GET", "/api/auth/status"), (200, {"passphrase_set": False}))
        self.assertEqual(self.login()[0], 409)
        self.assertEqual(self.call("GET", "/api/settings", headers=self.device("x" * 30))[0], 401)
        self.assertEqual(self.call("GET", "/api/settings", headers=self.code())[0], 200, "the access code still works")
        status, body = self.call("POST", "/api/auth/passphrase", {"new": CREW}, self.admin())
        self.assertEqual(status, 500, "never overwritten; reported, not crashed")
        self.assertEqual(json.loads((Path(self.tmp.name) / "auth.json").read_text())["devices"], 5)

    def test_static_pages_cannot_be_framed(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("GET", "/", headers={"Host": f"127.0.0.1:{self.port}"})
        response = connection.getresponse(); response.read(); connection.close()
        self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")

    def _race(self, rival, method="current_hash"):
        """Make ``rival`` write the store right after the handler's read (or current-passphrase check)."""
        real = getattr(app.vsauth.AuthStore, method)

        def racing(store, *args):
            value = real(store, *args)
            racing.fired or rival()
            racing.fired = True
            return value
        racing.fired = False
        return mock.patch.object(app.vsauth.AuthStore, method, racing)

    def test_first_set_loses_to_a_concurrent_first_set_with_409(self):
        rival = lambda: app.vsauth.AuthStore(Path(self.tmp.name) / "auth.json", min_passphrase=8).set_passphrase("rival passphrase")
        with self._race(rival):
            status, body = self.call("POST", "/api/auth/passphrase", {"new": CREW}, self.admin())
        self.assertEqual((status, body), (409, {"error": "passphrase changed, try again"}))
        self.assertEqual(self.login(passphrase="rival passphrase")[0], 200)
        self.assertEqual(self.login(passphrase=CREW)[0], 401)

    def test_change_loses_to_a_concurrent_change_with_409(self):
        self.set_passphrase()
        mine = self.login()[1]["device_token"]
        rival = lambda: app.vsauth.AuthStore(Path(self.tmp.name) / "auth.json", min_passphrase=8).set_passphrase("rival passphrase")
        with self._race(rival, "check_passphrase"):
            status, body = self.call("POST", "/api/auth/passphrase", {"current": CREW, "new": "another pass"}, self.device(mine))
        self.assertEqual((status, body["error"]), (409, "passphrase changed, try again"))
        self.assertEqual(self.login(passphrase="rival passphrase")[0], 200)

    def test_change_needs_current_passphrase_or_admin_code(self):
        os.environ["BOAT_CHAT_SETTINGS_TOKEN"] = ADMIN
        self.set_passphrase()
        mine = self.login()[1]["device_token"]
        other = self.login()[1]["device_token"]
        self.assertEqual(self.call("POST", "/api/auth/passphrase", {"new": "another pass"}, self.device(mine))[0], 401)
        self.assertEqual(self.call("POST", "/api/auth/passphrase", {"current": "nope nope", "new": "another pass"}, self.device(mine))[0], 401)
        status, body = self.call("POST", "/api/auth/passphrase", {"current": CREW, "new": "another pass"}, self.device(mine))
        self.assertEqual((status, body["revoked_others"]), (200, 1))
        self.assertEqual(self.call("GET", "/api/settings", headers=self.device(other))[0], 401)
        self.assertEqual(self.call("GET", "/api/settings", headers=self.device(mine))[0], 200)
        self.assertEqual(self.login(passphrase=CREW)[0], 401)
        self.assertEqual(self.call("POST", "/api/auth/passphrase", {"new": "admin chosen"}, {"X-Boat-Chat-Token": ADMIN})[0], 200)
        self.assertEqual(self.login(passphrase="admin chosen")[0], 200)


class MatrixAndHostTests(SignInCase):
    def test_device_works_wherever_the_access_code_does(self):
        self.set_passphrase()
        token = self.login()[1]["device_token"]
        for method, path in (("GET", "/api/settings"), ("POST", "/api/session/clear")):
            self.assertEqual(self.call(method, path, headers=self.device(token))[0], 200, path)
            self.assertEqual(self.call(method, path)[0], 401, path)
            self.assertEqual(self.call(method, path, headers=self.device("x" * 30))[0], 401, path)
        self.assertEqual(self.call("GET", "/api/settings", headers=self.code())[0], 200, "access code unchanged")

    def test_passphrase_sign_in_never_grants_admin(self):
        os.environ["BOAT_CHAT_SETTINGS_TOKEN"] = ADMIN
        self.set_passphrase()
        token = self.login()[1]["device_token"]
        status, _ = self.call("POST", "/api/settings", {"settings": {"BOAT_CHAT_MAX_TOKENS": "321"}}, self.device(token))
        self.assertEqual(status, 403)
        status, _ = self.call("POST", "/api/settings", {"settings": {"BOAT_CHAT_MAX_TOKENS": "321"}}, {**self.device(token), "X-Boat-Chat-Token": "wrong"})
        self.assertEqual(status, 403)
        status, _ = self.call("POST", "/api/settings", {"settings": {"BOAT_CHAT_MAX_TOKENS": "321"}}, {**self.device(token), "X-Boat-Chat-Token": ADMIN})
        self.assertEqual(status, 200, "admin code still required, and still sufficient")

    def test_origin_is_required_for_login_passphrase_and_device_posts(self):
        self.set_passphrase()
        self.assertEqual(self.login(origin=False)[0], 403)
        self.assertEqual(self.call("POST", "/api/auth/passphrase", {"new": "another pass"}, self.code(), origin=False)[0], 403)
        token = self.login()[1]["device_token"]
        self.assertEqual(self.call("POST", "/api/session/clear", headers=self.device(token), origin=False)[0], 403)
        self.assertEqual(self.call("POST", "/api/session/clear", headers={**self.device(token), "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.call("POST", "/api/session/clear", headers=self.code(), origin=False)[0], 200, "access-code POSTs unchanged")

    def test_wrong_host_is_refused_for_sign_in_and_device_requests(self):
        self.set_passphrase()
        token = self.login()[1]["device_token"]
        for host in ("evil.example", f"evil.example:{self.port}", f"127.0.0.1:{self.port + 1}", "127.0.0.1"):
            self.assertEqual(self.call("GET", "/api/auth/status", host=host)[0], 421, host)
            self.assertEqual(self.call("GET", "/api/settings", headers=self.device(token), host=host)[0], 421, host)
            self.assertEqual(self.call("POST", "/api/auth/login", {"passphrase": CREW, "remember": True}, host=host, headers={"Origin": f"http://{host}"})[0], 421, host)
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"):
            self.assertEqual(self.call("GET", "/api/auth/status", host=host)[0], 200, host)
        self.assertEqual(app.LOGIN_THROTTLE.retry_after("127.0.0.1"), 0)

    def test_access_code_flows_do_not_depend_on_the_host_name(self):
        self.assertEqual(self.call("GET", "/api/settings", headers=self.code(), host="boat.tailnet.example:8765")[0], 200)

    def test_extra_hosts_from_environment(self):
        os.environ["BOAT_CHAT_ALLOWED_HOSTS"] = "boat.tailnet.example"
        app._HOST_ALLOWLISTS.clear()
        # the listener here is on a random port, so a name needs that port
        self.assertEqual(self.call("GET", "/api/auth/status", host=f"boat.tailnet.example:{self.port}")[0], 200)
        self.assertEqual(self.call("GET", "/api/auth/status", host="boat.tailnet.example")[0], 421)

    def test_signin_is_off_without_the_shared_module(self):
        with mock.patch.object(app, "vsauth", None):
            self.assertEqual(self.call("GET", "/api/auth/status")[0], 404)
            self.assertEqual(self.call("GET", "/api/settings", headers=self.code())[0], 200)


if __name__ == "__main__":
    unittest.main()


class PairingTests(SignInCase):
    def pair_store(self):
        return app.chat_auth_store()

    def redeem(self, code, **extra):
        return self.call("POST", "/api/auth/pair", {"code": code, **extra})

    def test_pair_round_trip_roles_and_single_use(self):
        store = self.pair_store()
        code, _ = store.create_pairing("access")
        status, body = self.redeem(auth_format(code), label="Crew phone", remember=False)
        self.assertEqual((status, body["role"], body["remember"]), (200, "access", True))
        status, devices = self.call("GET", "/api/auth/devices", headers=self.device(body["device_token"]))
        self.assertEqual((status, devices["devices"][0]["label"]), (200, "Crew phone"))
        self.assertEqual(self.redeem(code)[0], 401, "single use")

    def test_matrix_origin_host_and_bad_codes(self):
        code, _ = self.pair_store().create_pairing("access")
        self.assertEqual(self.call("POST", "/api/auth/pair", {"code": code}, origin=False)[0], 403)
        self.assertEqual(self.call("POST", "/api/auth/pair", {"code": code}, host="evil.example")[0], 421)
        self.assertEqual(self.call("POST", "/api/auth/pair", {"code": 5})[0], 400)
        self.assertEqual(self.redeem("not a code")[0], 401)
        self.assertEqual(self.redeem(code)[0], 200, "the real code still works after a typo")

    def test_bad_codes_are_throttled(self):
        results = [self.redeem("ZZZZZ-ZZZZZ")[0] for _ in range(8)]
        self.assertEqual(results.count(401), app.vsauth.LoginThrottle.IP_FREE_FAILURES)
        self.assertEqual(results.count(429), 8 - app.vsauth.LoginThrottle.IP_FREE_FAILURES)

    def test_admin_device_passes_admin_checks_and_access_device_does_not(self):
        self.set_passphrase()
        store = self.pair_store()
        os.environ["BOAT_CHAT_SETTINGS_TOKEN"] = ADMIN
        admin = self.redeem(store.create_pairing("admin", "", store.admin_binding(ADMIN, create=True))[0])[1]
        access = self.redeem(store.create_pairing("access")[0])[1]
        self.assertEqual(admin["role"], "admin")
        status, body = self.call("POST", "/api/settings", {"settings": {}}, self.device(access["device_token"]))
        self.assertEqual(status, 403, body)
        status, body = self.call("POST", "/api/settings", {"settings": {}}, self.device(admin["device_token"]))
        self.assertNotEqual(status, 403, body)
        # an admin device may set the crew passphrase without the admin code or the current passphrase
        status, body = self.call("POST", "/api/auth/passphrase", {"new": "another crew pass"}, self.device(admin["device_token"]))
        self.assertEqual(status, 200, body)
        status, body = self.call("POST", "/api/auth/passphrase", {"new": "third crew pass", "current": "wrong"}, self.device(access["device_token"]))
        self.assertIn(status, (401, 403))

    def test_admin_device_is_bound_to_the_admin_code(self):
        self.set_passphrase()
        store = self.pair_store()
        os.environ["BOAT_CHAT_SETTINGS_TOKEN"] = ADMIN
        admin = self.redeem(store.create_pairing("admin", "", store.admin_binding(ADMIN, create=True))[0])[1]
        headers = self.device(admin["device_token"])
        self.assertNotEqual(self.call("POST", "/api/settings", {"settings": {}}, headers)[0], 403)
        os.environ["BOAT_CHAT_SETTINGS_TOKEN"] = "rotated-admin-code"          # rotate: the old device loses admin
        self.assertEqual(self.call("POST", "/api/settings", {"settings": {}}, headers)[0], 403)
        os.environ.pop("BOAT_CHAT_SETTINGS_TOKEN")                            # no token: a device is never an admin
        status, body = self.call("POST", "/api/auth/passphrase", {"new": "another crew pass"}, headers)
        self.assertIn(status, (401, 403), body)
        with self.assertRaises(app.vsauth.AuthError):
            store.create_pairing("admin", "", store.admin_binding(""))


def auth_format(code):
    return code[:5].lower() + "-" + code[5:].lower()
