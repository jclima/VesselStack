#!/usr/bin/env python3

from __future__ import annotations

import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "displays"))

import displayctl  # noqa: E402
import lifecycle  # noqa: E402
import mqtt_auth  # noqa: E402

EXAMPLE = json.loads((ROOT / "displays" / "displays.example.json").read_text())
BOARD = "waveshare-esp32-s3-touch-lcd-2.8c"
ADMIN_PASSWORD = "admin-secret-value"
WIFI_PASSWORD = "wifi-pass-123"


class FakeRunner:
    """Emulates docker for sync: the hashing container and the broker HUP."""

    def __init__(self, running=True):
        self.calls = []
        self.running = running
        self.passwords = ""

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:2] == ["docker", "run"]:
            mount = argv[argv.index("-v") + 1]
            mdir = Path(mount.rsplit(":", 1)[0])
            new = mdir / "passwords.new"
            self.passwords = new.read_text()
            hashed = "".join(f"{line.split(':', 1)[0]}:$7$fake\n"
                             for line in self.passwords.splitlines())
            new.write_text(hashed)
            os.chmod(new, 0o600)
            os.replace(new, mdir / "passwords")
        elif argv[:2] == ["docker", "inspect"] and argv[2] == "-f":
            return subprocess.CompletedProcess(argv, 0 if self.running else 1,
                                               "true\n" if self.running else "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class FakeExec:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.calls = []
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr

    def __call__(self, argv, input_text=None):
        self.calls.append((list(argv), input_text))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "root"
        (self.root / "config").mkdir(parents=True)
        (self.root / "compose.yml").write_text(
            "services:\n  mosquitto:\n    image: eclipse-mosquitto:9.9.9-alpine\n")
        self.config = self.root / "config" / "displays.json"
        self.secrets = self.root / "config" / mqtt_auth.SECRETS_NAME
        self.mdir = self.root / "config" / mqtt_auth.AUTH_DIR_NAME
        self.runner = FakeRunner()
        self.write_config(EXAMPLE["instruments"], {})

    def write_config(self, instruments, displays):
        cfg = json.loads(json.dumps(EXAMPLE))
        cfg["instruments"] = instruments
        cfg["displays"] = displays
        self.config.write_text(json.dumps(cfg, indent=2) + "\n")

    def ctx(self, enabled="true", **kw):
        env = {"MQTT_USERNAME": "homeassistant", "MQTT_PASSWORD": ADMIN_PASSWORD,
               "DISPLAYS_ENABLE": enabled}
        values = dict(root=self.root, env=env, runner=self.runner, require_root=False,
                      dir_owner=None, file_owner=None, lock_timeout=0.3)
        values.update(kw)
        self.lines = []
        values["out"] = self.lines.append
        return lifecycle.Context(**values)

    def cfg(self):
        return json.loads(self.config.read_text())

    def sec(self):
        return json.loads(self.secrets.read_text())

    def add(self, display_id="helm", board=BOARD, **kw):
        lifecycle.add(self.ctx(), display_id, board, **kw)

    def output(self):
        return "\n".join(self.lines)

    # --- add ---------------------------------------------------------------

    def test_add_creates_default_page_and_syncs_new_account(self):
        self.add("helm", name="Helm", home_assistant_api=True)
        entry = self.cfg()["displays"]["helm"]
        self.assertEqual(entry, {
            "name": "Helm", "board": BOARD, "home_assistant_api": True,
            "pages": [{"title": "Main", "layout": "grid-2x2",
                       "items": ["sog", "depth", "awa", "house_v"]}]})
        self.assertIn("user vsdisp-helm", (self.mdir / "acl").read_text())
        self.assertIn("vsdisp-helm:", self.runner.passwords)
        self.assertTrue(self.sec()["displays"]["helm"]["mqtt_password"])
        self.assertEqual(oct(self.config.stat().st_mode & 0o777), "0o644")
        self.assertEqual(oct(self.secrets.stat().st_mode & 0o777), "0o600")
        self.assertIn(["docker", "kill", "-s", "HUP", mqtt_auth.CONTAINER], self.runner.calls)
        text = self.output()
        self.assertIn("MQTT username: vsdisp-helm", text)
        self.assertIn("coming in a later release", text)
        self.assertNotIn(self.sec()["displays"]["helm"]["mqtt_password"], text)

    def test_add_uses_capacity_and_instrument_order(self):
        insts = EXAMPLE["instruments"]
        two = {k: insts[k] for k in list(insts)[:2]}
        self.write_config(two, {})
        self.add("aux")
        page = self.cfg()["displays"]["aux"]["pages"][0]
        self.assertEqual((page["layout"], page["items"]), ("grid-2x2", ["sog", "depth"]))
        self.assertEqual(self.cfg()["displays"]["aux"]["name"], "Aux")

    def test_add_largest_layout_per_board_registry(self):
        registry = lifecycle.load_registry()
        registry["boards"]["tiny"] = dict(registry["boards"][BOARD], layouts=["single"])
        registry["boards"]["big"] = dict(registry["boards"][BOARD],
                                         layouts=["single", "list-6", "grid-2x2"])
        config = json.loads(self.config.read_text())
        self.assertEqual(lifecycle.default_page(registry, config, "tiny")["items"], ["sog"])
        page = lifecycle.default_page(registry, config, "big")
        self.assertEqual((page["layout"], len(page["items"])), ("list-6", 4))

    def test_add_duplicate_refused_and_nothing_changes(self):
        self.add("helm")
        before = self.config.read_text()
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            self.add("helm")
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.config.read_text(), before)

    def test_add_unknown_board_and_bad_id_refused(self):
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            self.add("helm", board="nope")
        self.assertEqual(caught.exception.code, 2)
        with self.assertRaises(lifecycle.LifecycleError):
            self.add("Bad_ID")
        with self.assertRaises(lifecycle.LifecycleError):
            self.add("helm\n")  # a trailing newline is not a valid id
        self.assertEqual(self.cfg()["displays"], {})

    def test_add_with_zero_instruments_refused(self):
        self.write_config({}, {})
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            self.add("helm")
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("no instruments", str(caught.exception))

    def test_add_invalid_name_refused_before_writing(self):
        before = self.config.read_text()
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            self.add("helm", name="x" * 40)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.config.read_text(), before)

    def test_add_preserves_key_order_and_format(self):
        self.add("zulu")
        self.add("alpha")
        self.assertEqual(list(self.cfg()["displays"]), ["zulu", "alpha"])
        text = self.config.read_text()
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(text, json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n")
        self.assertEqual(list(self.cfg())[:2], ["schema", "mqtt_host"])

    def test_disabled_edits_files_but_skips_sync(self):
        lifecycle.add(self.ctx(enabled="false"), "helm", BOARD)
        self.assertIn("helm", self.cfg()["displays"])
        self.assertIn("disabled", self.output())
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(self.secrets.exists())

    def test_add_requires_mqtt_env_when_enabled_before_editing(self):
        ctx = self.ctx()
        ctx.env.pop("MQTT_PASSWORD")
        before = self.config.read_text()
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            lifecycle.add(ctx, "helm", BOARD)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.config.read_text(), before)

    def test_root_required_when_not_injected(self):
        ctx = self.ctx(require_root=True)
        if os.geteuid() == 0:
            self.skipTest("running as root")
        for call in (lambda: lifecycle.add(ctx, "helm", BOARD),
                     lambda: lifecycle.remove(ctx, "helm", True),
                     lambda: lifecycle.rotate(ctx, "helm"),
                     lambda: lifecycle.wifi(ctx, clear=True)):
            with self.assertRaises(lifecycle.LifecycleError) as caught:
                call()
            self.assertIn("root", str(caught.exception))

    # --- remove / rotate -----------------------------------------------------

    def test_remove_requires_yes(self):
        self.add("helm")
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            lifecycle.remove(self.ctx(), "helm", False)
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--yes", str(caught.exception))
        self.assertIn("helm", self.cfg()["displays"])

    def test_remove_deletes_entry_secret_and_account(self):
        self.add("helm")
        self.add("aux")
        old = self.sec()["displays"]["aux"]["mqtt_password"]
        lifecycle.remove(self.ctx(), "aux", True)
        self.assertEqual(list(self.cfg()["displays"]), ["helm"])
        self.assertNotIn("aux", self.sec()["displays"])
        self.assertNotIn("vsdisp-aux", (self.mdir / "acl").read_text())
        self.assertNotIn("vsdisp-aux", self.runner.passwords)
        self.assertIn("helm", self.sec()["displays"])
        self.add("aux")
        self.assertNotEqual(self.sec()["displays"]["aux"]["mqtt_password"], old)

    def test_remove_unknown_display(self):
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            lifecycle.remove(self.ctx(), "ghost", True)
        self.assertEqual(caught.exception.code, 2)

    def test_rotate_changes_only_that_display(self):
        self.add("helm")
        self.add("aux")
        before = self.sec()
        lifecycle.rotate(self.ctx(), "helm")
        after = self.sec()
        self.assertNotEqual(after["displays"]["helm"]["mqtt_password"],
                            before["displays"]["helm"]["mqtt_password"])
        self.assertEqual(after["displays"]["aux"], before["displays"]["aux"])
        self.assertEqual(after["bridge"], before["bridge"])
        self.assertIn("reflashed", self.output())
        self.assertIn(["docker", "kill", "-s", "HUP", mqtt_auth.CONTAINER], self.runner.calls)

    def test_rotate_covers_every_generated_key_and_keeps_others(self):
        self.add("helm")
        data = self.sec()
        data["displays"]["helm"]["note"] = "keep"
        self.secrets.write_text(json.dumps(data))
        original = dict(lifecycle.GENERATED_SECRETS)
        lifecycle.GENERATED_SECRETS["future_key"] = lambda: "generated-future"
        self.addCleanup(lambda: (lifecycle.GENERATED_SECRETS.clear(),
                                 lifecycle.GENERATED_SECRETS.update(original)))
        lifecycle.rotate(self.ctx(), "helm")
        entry = self.sec()["displays"]["helm"]
        self.assertEqual(entry["future_key"], "generated-future")
        self.assertEqual(entry["note"], "keep")
        self.assertNotEqual(entry["mqtt_password"], data["displays"]["helm"]["mqtt_password"])

    def test_rotate_unknown_display(self):
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.rotate(self.ctx(), "ghost")

    def test_sync_failure_is_reported_with_retry_hint(self):
        ctx = self.ctx()
        ctx.runner = lambda argv: subprocess.CompletedProcess(argv, 1, "", "no docker")
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            lifecycle.add(ctx, "helm", BOARD)
        self.assertIn("sync-mqtt", str(caught.exception))
        self.assertEqual(caught.exception.code, 1)

    # --- wifi ----------------------------------------------------------------

    def wifi(self, text, **kw):
        lifecycle.wifi(self.ctx(), stdin=io.StringIO(text), **kw)

    def test_wifi_from_stdin_is_stored_and_not_echoed(self):
        self.add("helm")
        self.wifi(f"Boat WiFi\n{WIFI_PASSWORD}\n")
        data = self.sec()
        self.assertEqual(data["wifi"], {"ssid": "Boat WiFi", "password": WIFI_PASSWORD})
        self.assertEqual(oct(self.secrets.stat().st_mode & 0o777), "0o600")
        self.assertIn("helm", data["displays"])
        text = self.output()
        self.assertNotIn(WIFI_PASSWORD, text)
        self.assertNotIn("Boat WiFi", text)
        # A later sync (ensure_secrets) must preserve the Wi-Fi block.
        lifecycle.rotate(self.ctx(), "helm")
        self.assertEqual(self.sec()["wifi"]["ssid"], "Boat WiFi")

    def test_wifi_validation(self):
        bad = [("", WIFI_PASSWORD), ("x" * 33, WIFI_PASSWORD), ("é" * 17, WIFI_PASSWORD),
               ("net\x01", WIFI_PASSWORD), ("net", "short"), ("net", "x" * 64),
               ("net", "pässword1"), ("net", "")]
        for ssid, password in bad:
            with self.subTest(ssid=ssid, password=password):
                with self.assertRaises(lifecycle.LifecycleError) as caught:
                    self.wifi(f"{ssid}\n{password}\n")
                self.assertEqual(caught.exception.code, 2)
        self.assertFalse(self.secrets.exists())
        self.wifi("é" * 16 + "\n" + "x" * 63 + "\n")  # 32 bytes / 63 chars are fine
        self.wifi("net\nabcdefgh\n")

    def test_wifi_empty_password_needs_open_flag(self):
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            self.wifi("Marina\n\n")
        self.assertIn("--open", str(caught.exception))
        self.wifi("Marina\n\n", allow_open=True)
        self.assertEqual(self.sec()["wifi"], {"ssid": "Marina", "password": ""})
        self.wifi("Marina2", allow_open=True)  # missing second line = empty
        self.assertEqual(self.sec()["wifi"]["ssid"], "Marina2")

    def test_wifi_clear(self):
        self.wifi(f"net\n{WIFI_PASSWORD}\n")
        lifecycle.wifi(self.ctx(), clear=True)
        self.assertNotIn("wifi", self.sec())
        lifecycle.wifi(self.ctx(), clear=True)
        self.assertIn("No Wi-Fi credentials", self.output())

    def test_wifi_never_accepts_values_on_argv(self):
        parser = displayctl.build_parser()
        for argv in (["wifi", "--ssid", "x"], ["wifi", "--password", "y"], ["wifi", "ssid"]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                parser.parse_args(argv)
            self.assertEqual(caught.exception.code, 2)

    # --- list ------------------------------------------------------------------

    def test_list_never_prints_secrets(self):
        self.add("helm")
        self.add("aux")
        self.wifi(f"Boat WiFi\n{WIFI_PASSWORD}\n")
        rows = lifecycle.list_displays(self.ctx())
        text = self.output()
        data = self.sec()
        for secret in (data["displays"]["helm"]["mqtt_password"], data["bridge"]["mqtt_password"],
                       WIFI_PASSWORD, "Boat WiFi", ADMIN_PASSWORD):
            self.assertNotIn(secret, text)
        self.assertEqual(rows[0], ["ID", "NAME", "BOARD", "PAGES", "MQTT USER", "SECRETS", "WIFI"])
        self.assertEqual(rows[1], ["helm", "Helm", BOARD, "1", "vsdisp-helm", "yes", "yes"])
        self.assertIn("vsdisp-aux", text)

    def test_list_without_secrets_or_wifi(self):
        lifecycle.add(self.ctx(enabled="false"), "helm", BOARD)
        rows = lifecycle.list_displays(self.ctx())
        self.assertEqual(rows[1][-2:], ["no", "no"])

    def test_list_unreadable_secrets_shows_unknown(self):
        self.add("helm")
        os.chmod(self.secrets, 0)
        self.addCleanup(os.chmod, self.secrets, 0o600)
        if os.access(self.secrets, os.R_OK):
            self.skipTest("running as root")
        rows = lifecycle.list_displays(self.ctx())
        self.assertEqual(rows[1][-2:], ["unknown", "unknown"])

    def test_list_empty(self):
        rows = lifecycle.list_displays(self.ctx())
        self.assertEqual(len(rows), 1)
        self.assertIn("No displays configured", self.output())

    # --- locking and symlinks --------------------------------------------------

    def hold_lock(self):
        fd = os.open(self.root / "config" / lifecycle.LOCK_NAME,
                     os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(self.close_quietly, fd)
        return fd

    @staticmethod
    def close_quietly(fd):
        try:
            os.close(fd)
        except OSError:
            pass

    def test_lock_blocks_concurrent_mutation_with_timeout(self):
        fd = self.hold_lock()
        before = self.config.read_text()
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            self.add("helm")
        self.assertIn("another displayctl", str(caught.exception))
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(self.config.read_text(), before)
        os.close(fd)  # released: the next call proceeds
        self.add("helm")
        self.assertIn("helm", self.cfg()["displays"])

    def test_lock_waits_for_release(self):
        import threading
        fd = self.hold_lock()
        timer = threading.Timer(0.3, self.close_quietly, args=(fd,))
        timer.start()
        self.addCleanup(timer.join)
        lifecycle.add(self.ctx(lock_timeout=5), "helm", BOARD)
        self.assertIn("helm", self.cfg()["displays"])

    def test_lock_file_is_mode_0600(self):
        self.add("helm")
        lock = self.root / "config" / lifecycle.LOCK_NAME
        self.assertEqual(oct(lock.stat().st_mode & 0o777), "0o600")

    def test_sync_mqtt_and_bridge_env_take_the_same_lock(self):
        self.add("helm")
        fd = self.hold_lock()
        env = {"MQTT_USERNAME": "homeassistant", "MQTT_PASSWORD": ADMIN_PASSWORD,
               "DISPLAYS_ENABLE": "true", "SIGNALK_URL": "http://127.0.0.1:3000"}
        real = mqtt_auth.acquire_lock
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(mqtt_auth, "acquire_lock",
                                  lambda config_fd, timeout=10: real(config_fd, 0.2)), \
                mock.patch.object(mqtt_auth, "default_runner", self.runner):
            err = io.StringIO()
            with redirect_stderr(err):
                code = displayctl.main(["sync-mqtt", "--root", str(self.root),
                                        "--data", str(self.root / "d"),
                                        "--image", "eclipse-mosquitto:9.9.9-alpine"])
            self.assertEqual(code, 1)
            self.assertIn("another displayctl", err.getvalue())
            err = io.StringIO()
            with redirect_stderr(err):
                code = displayctl.main(["bridge-env", "--root", str(self.root),
                                        "--output", str(self.root / "config" / "b.env")])
            self.assertEqual(code, 1)
            self.assertIn("another displayctl", err.getvalue())
        self.assertFalse((self.root / "config" / "b.env").exists())
        os.close(fd)

    def test_bridge_env_is_root_owned_0600(self):
        self.add("helm")
        out = self.root / "config" / "displays-bridge.env"
        env = {"SIGNALK_URL": "http://127.0.0.1:3000"}
        chowns = []
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(mqtt_auth.os, "geteuid", return_value=0), \
                mock.patch.object(displayctl.os, "chown",
                                  lambda path, uid, gid: chowns.append((str(path), uid, gid))), \
                mock.patch.object(mqtt_auth.os, "fchown", lambda *a: None):
            with redirect_stdout(io.StringIO()):
                code = displayctl.main(["bridge-env", "--root", str(self.root),
                                        "--output", str(out)])
        self.assertEqual(code, 0)
        self.assertEqual(oct(out.stat().st_mode & 0o777), "0o600")
        self.assertEqual(chowns, [(str(out), 0, 0)])
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                displayctl.build_parser().parse_args(["bridge-env", "--output", "x", "--owner", "u"])

    def test_preflight_command(self):
        env = {"MQTT_USERNAME": "homeassistant", "MQTT_PASSWORD": "a:b", "DISPLAYS_ENABLE": "true"}
        with mock.patch.dict(os.environ, env):
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(displayctl.main(["preflight", "--root", str(self.root)]), 1)
            self.assertIn("preflight failed", err.getvalue())
            self.assertFalse((self.root / "config" / lifecycle.LOCK_NAME).exists())
        env["MQTT_PASSWORD"] = "fine-password"
        with mock.patch.dict(os.environ, env), redirect_stdout(io.StringIO()):
            self.assertEqual(displayctl.main(["preflight", "--root", str(self.root)]), 0)

    def test_symlinked_lock_file_refused(self):
        target = Path(self._tmp.name) / "elsewhere"
        target.write_text("")
        os.symlink(target, self.root / "config" / lifecycle.LOCK_NAME)
        with self.assertRaises(lifecycle.LifecycleError):
            self.add("helm")

    def test_symlinked_displays_json_refused(self):
        real = Path(self._tmp.name) / "real.json"
        real.write_text(self.config.read_text())
        self.config.unlink()
        os.symlink(real, self.config)
        before = real.read_text()
        for call in (lambda: self.add("helm"), lambda: lifecycle.list_displays(self.ctx()),
                     lambda: lifecycle.remove(self.ctx(), "x", True)):
            with self.assertRaises(lifecycle.LifecycleError) as caught:
                call()
            self.assertIn("symbolic link", str(caught.exception))
        self.assertEqual(real.read_text(), before)
        self.assertTrue(self.config.is_symlink())

    def test_symlinked_secrets_refused(self):
        self.add("helm")
        real = Path(self._tmp.name) / "real-secrets.json"
        real.write_text(self.secrets.read_text())
        before = real.read_text()
        self.secrets.unlink()
        os.symlink(real, self.secrets)
        for call in (lambda: lifecycle.rotate(self.ctx(), "helm"),
                     lambda: lifecycle.remove(self.ctx(), "helm", True),
                     lambda: self.wifi(f"net\n{WIFI_PASSWORD}\n"),
                     lambda: self.add("aux")):
            with self.assertRaises(lifecycle.LifecycleError) as caught:
                call()
            self.assertIn("symbolic link", str(caught.exception))
        self.assertEqual(real.read_text(), before)

    def test_symlinked_config_directory_refused(self):
        other = Path(self._tmp.name) / "other"
        other.mkdir()
        (self.root / "config").rename(self.root / "config.real")
        os.symlink(other, self.root / "config")
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.list_displays(self.ctx())

    def test_stale_temp_symlink_is_not_followed(self):
        victim = Path(self._tmp.name) / "victim"
        victim.write_text("keep")
        os.symlink(victim, self.root / "config" / "displays.json.tmp")
        self.add("helm")
        self.assertEqual(victim.read_text(), "keep")
        self.assertIn("helm", self.cfg()["displays"])

    # --- status ----------------------------------------------------------------

    def test_status_parses_retained_and_hides_admin_password(self):
        self.add("helm")
        self.add("aux")
        self.runner.calls.clear()
        fake = FakeExec(stdout=(
            "vesselstack/v1/bridge/status\tonline\n"
            "vesselstack/v1/bridge/signalk\tconnected\n"
            "vesselstack/v1/displays/vsdisp-helm/status\tonline\n"
            "vesselstack/v1/displays/vsdisp-aux/status\toffline\x1b[31m\n"),
            returncode=27)
        ctx = self.ctx(exec_runner=fake)
        self.assertEqual(lifecycle.status(ctx), 0)
        text = self.output()
        self.assertRegex(text, r"Bridge\s+online")
        self.assertRegex(text, r"SignalK link\s+connected")
        self.assertRegex(text, r"helm\s+online")
        self.assertRegex(text, r"aux\s+offline\?")  # control characters are neutralised
        self.assertNotIn("\x1b", text)
        argv, stdin_text = fake.calls[0]
        every_argv = [a for call in self.runner.calls + [argv] for a in call]
        self.assertFalse(any(ADMIN_PASSWORD in part for part in every_argv))
        self.assertFalse(any("homeassistant" in part and part != "homeassistant"
                             for part in every_argv))
        self.assertEqual(stdin_text, f"-u homeassistant\n-P {ADMIN_PASSWORD}\n")
        self.assertEqual(argv[:5], ["docker", "exec", "-i", mqtt_auth.CONTAINER, "sh"])
        self.assertNotIn("-e", argv)

    def test_status_missing_messages(self):
        self.add("helm")
        ctx = self.ctx(exec_runner=FakeExec(stdout="", returncode=27))
        self.assertEqual(lifecycle.status(ctx), 0)
        self.assertIn("unknown", self.output())
        self.assertIn("never seen", self.output())

    def test_status_broker_not_running(self):
        self.runner.running = False
        fake = FakeExec()
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            lifecycle.status(self.ctx(exec_runner=fake))
        self.assertIn("not running", str(caught.exception))
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(fake.calls, [])

    def test_status_connection_failure(self):
        fake = FakeExec(returncode=5, stderr="Connection error: Connection Refused: not authorised.")
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            lifecycle.status(self.ctx(exec_runner=fake))
        self.assertNotIn(ADMIN_PASSWORD, str(caught.exception))

    def test_status_requires_admin_credentials(self):
        ctx = self.ctx()
        ctx.env.pop("MQTT_PASSWORD")
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            lifecycle.status(ctx)
        self.assertEqual(caught.exception.code, 2)

    # --- command line ------------------------------------------------------------

    def run_cli(self, *argv, **env):
        values = {"VESSELSTACK_ROOT": str(self.root), "DISPLAYS_ENABLE": "false"}
        values.update(env)
        old = {k: os.environ.get(k) for k in values}
        os.environ.update(values)
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = displayctl.main(list(argv))
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return code, out.getvalue(), err.getvalue()

    def test_cli_exit_codes(self):
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        code, _, err = self.run_cli("remove", "ghost")
        self.assertEqual((code, "--yes" in err), (2, True))
        code, _, err = self.run_cli("add", "BAD", "--board", BOARD)
        self.assertEqual(code, 2)
        code, _, err = self.run_cli("status", MQTT_USERNAME="a", MQTT_PASSWORD="b")
        self.assertEqual(code, 1)  # no docker / not running / not root: operational
        code, _, err = self.run_cli("list", VESSELSTACK_ROOT=str(self.root / "missing"))
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
