#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "displays"))

import mqtt_auth  # noqa: E402

EXAMPLE = json.loads((ROOT / "displays" / "displays.example.json").read_text())
ADMIN_PASSWORD = "admin-secret-value"

DISABLED_ACL = """user homeassistant
topic readwrite #
topic read $SYS/#
"""

ENABLED_ACL = """user homeassistant
topic readwrite #
topic read $SYS/#

user vesselstack-bridge
topic write vesselstack/v1/instruments/#
topic write vesselstack/v1/bridge/#
topic read vesselstack/v1/displays/+/status

user vsdisp-aux
topic read vesselstack/v1/instruments/#
topic read vesselstack/v1/bridge/#
topic write vesselstack/v1/displays/vsdisp-aux/status

user vsdisp-helm
topic read vesselstack/v1/instruments/#
topic read vesselstack/v1/bridge/#
topic write vesselstack/v1/displays/vsdisp-helm/status
"""


class FakeRunner:
    """Stands in for docker: emulates the hashing container, records argv."""

    def __init__(self, running=True, hash_ok=True):
        self.calls = []
        self.running = running
        self.hash_ok = hash_ok

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:2] == ["docker", "run"]:
            if not self.hash_ok:
                return subprocess.CompletedProcess(argv, 1, "", "boom")
            mount = argv[argv.index("-v") + 1]
            assert mount.endswith(":/auth"), mount
            mdir = Path(mount.rsplit(":", 1)[0])
            new = mdir / "passwords.new"
            text = new.read_text()
            hashed = "".join(f"{line.split(':', 1)[0]}:$7$fake\n" for line in text.splitlines())
            new.write_text(hashed)
            os.chmod(new, 0o600)
            os.replace(new, mdir / "passwords")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(argv, 0 if self.running else 1,
                                               "true\n" if self.running else "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def write_displays(path, ids):
    cfg = json.loads(json.dumps(EXAMPLE))
    template = cfg["displays"]["helm"]
    cfg["displays"] = {i: dict(template, name=i.title()) for i in ids}
    path.write_text(json.dumps(cfg))


class MqttAuthTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "root"
        self.data = Path(self._tmp.name) / "data"
        (self.root / "config").mkdir(parents=True)
        (self.root / "compose.yml").write_text(
            "services:\n  mosquitto:\n    image: eclipse-mosquitto:9.9.9-alpine\n")
        self.config = self.root / "config" / "displays.json"
        self.mdir = self.root / "config" / mqtt_auth.AUTH_DIR_NAME

    def env(self, enabled="true", **extra):
        values = {"MQTT_USERNAME": "homeassistant", "MQTT_PASSWORD": ADMIN_PASSWORD,
                  "DISPLAYS_ENABLE": enabled}
        values.update(extra)
        return values

    def run_sync(self, runner=None, **env):
        runner = runner or FakeRunner()
        summary = mqtt_auth.sync(self.root, self.data, self.env(**env), runner=runner,
                                 dir_owner=None, file_owner=None)
        return summary, runner

    def acl(self):
        return (self.mdir / "acl").read_text()

    def test_disabled_acl_is_admin_only_and_ignores_config(self):
        write_displays(self.config, ["helm"])
        self.run_sync(enabled="false")
        self.assertEqual(self.acl(), DISABLED_ACL)
        users = [line.split(":")[0] for line in (self.mdir / "passwords").read_text().splitlines()]
        self.assertEqual(users, ["homeassistant"])
        self.assertFalse((self.root / "config" / mqtt_auth.SECRETS_NAME).exists())

    def test_enabled_acl_golden_and_order(self):
        write_displays(self.config, ["helm", "aux"])
        self.run_sync()
        self.assertEqual(self.acl().rstrip("\n"), ENABLED_ACL.rstrip("\n"))

    def test_enabled_without_config_has_zero_displays(self):
        summary, _ = self.run_sync()
        self.assertEqual(summary["displays"], 0)
        self.assertEqual([l for l in self.acl().splitlines() if l.startswith("user ")],
                         ["user homeassistant", "user vesselstack-bridge"])

    def test_plaintext_password_list_render(self):
        accounts = mqtt_auth.build_accounts(
            "homeassistant", ADMIN_PASSWORD, True, "vesselstack-bridge", ["b", "a"],
            {"bridge": {"mqtt_password": "bp"},
             "displays": {"a": {"mqtt_password": "ap"}, "b": {"mqtt_password": "bpw"}}})
        self.assertEqual(
            mqtt_auth.render_passwords(accounts),
            f"homeassistant:{ADMIN_PASSWORD}\nvesselstack-bridge:bp\nvsdisp-a:ap\nvsdisp-b:bpw\n")

    def test_invalid_admin_values_rejected(self):
        for name in ("bad name", "a:b", "", "x" * 65, "line\nbreak", "ünï"):
            with self.assertRaises(mqtt_auth.MqttAuthError, msg=name):
                self.run_sync(MQTT_USERNAME=name)
        for password in ("", "a\nb", "a\rb", "a:b", " lead", "trail "):
            with self.assertRaises(mqtt_auth.MqttAuthError, msg=repr(password)):
                self.run_sync(MQTT_PASSWORD=password)
        with self.assertRaises(mqtt_auth.MqttAuthError):
            self.run_sync(DISPLAYS_BRIDGE_MQTT_USERNAME="bad name")
        with self.assertRaises(mqtt_auth.MqttAuthError):
            self.run_sync(enabled="yes")
        self.assertFalse((self.mdir / "acl").exists())

    def test_name_collisions_rejected(self):
        write_displays(self.config, ["helm"])
        with self.assertRaises(mqtt_auth.MqttAuthError):
            self.run_sync(DISPLAYS_BRIDGE_MQTT_USERNAME="homeassistant")
        with self.assertRaises(mqtt_auth.MqttAuthError):
            self.run_sync(MQTT_USERNAME="vsdisp-helm")
        with self.assertRaises(mqtt_auth.MqttAuthError):
            self.run_sync(DISPLAYS_BRIDGE_MQTT_USERNAME="vsdisp-helm")

    def test_secrets_created_once_and_reused(self):
        write_displays(self.config, ["helm"])
        self.run_sync()
        secrets_path = self.root / "config" / mqtt_auth.SECRETS_NAME
        first = json.loads(secrets_path.read_text())
        self.assertEqual(first["schema"], 1)
        self.assertGreaterEqual(len(first["bridge"]["mqtt_password"]), 32)
        self.assertGreaterEqual(len(first["displays"]["helm"]["mqtt_password"]), 32)
        self.run_sync()
        self.assertEqual(json.loads(secrets_path.read_text()), first)
        write_displays(self.config, ["helm", "aux"])
        self.run_sync()
        second = json.loads(secrets_path.read_text())
        self.assertEqual(second["displays"]["helm"], first["displays"]["helm"])
        self.assertEqual(second["bridge"], first["bridge"])
        self.assertIn("aux", second["displays"])

    def test_removed_display_leaves_acl_and_passwords_but_keeps_secret(self):
        write_displays(self.config, ["helm", "aux"])
        self.run_sync()
        write_displays(self.config, ["helm"])
        self.run_sync()
        self.assertNotIn("vsdisp-aux", self.acl())
        self.assertNotIn("vsdisp-aux", (self.mdir / "passwords").read_text())
        secrets = json.loads((self.root / "config" / mqtt_auth.SECRETS_NAME).read_text())
        self.assertIn("aux", secrets["displays"])
        self.run_sync(enabled="false")
        self.assertEqual(self.acl(), DISABLED_ACL)

    def test_modes_and_previous_copies(self):
        write_displays(self.config, ["helm"])
        self.run_sync()
        self.run_sync()
        for name in ("acl", "passwords", "acl.prev", "passwords.prev"):
            self.assertEqual((self.mdir / name).stat().st_mode & 0o777, 0o600, name)
        self.assertEqual(self.mdir.stat().st_mode & 0o777, 0o750)
        self.assertFalse((self.data / "mosquitto").exists())
        self.assertEqual((self.root / "config" / mqtt_auth.SECRETS_NAME).stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.mdir / "acl.new").exists())
        self.assertFalse((self.mdir / "passwords.new").exists())

    def test_runner_never_receives_a_password(self):
        write_displays(self.config, ["helm"])
        _, runner = self.run_sync()
        secrets = json.loads((self.root / "config" / mqtt_auth.SECRETS_NAME).read_text())
        sensitive = [ADMIN_PASSWORD, secrets["bridge"]["mqtt_password"],
                     secrets["displays"]["helm"]["mqtt_password"]]
        for argv in runner.calls:
            for value in sensitive:
                self.assertNotIn(value, " ".join(argv))
        run = next(c for c in runner.calls if c[:2] == ["docker", "run"])
        self.assertIn("eclipse-mosquitto:9.9.9-alpine", run)
        self.assertIn(f"{self.mdir}:/auth", run)
        self.assertIn("chown 1883:1883", run[-1])
        self.assertIn("chmod 0600", run[-1])
        self.assertIn("mosquitto_passwd -U", run[-1])
        self.assertNotIn(" -b ", run[-1])

    def test_hup_only_when_running(self):
        summary, runner = self.run_sync(runner=FakeRunner(running=True))
        self.assertTrue(summary["reloaded"])
        self.assertIn(["docker", "kill", "-s", "HUP", "vesselstack-mosquitto"], runner.calls)
        summary, runner = self.run_sync(runner=FakeRunner(running=False))
        self.assertFalse(summary["reloaded"])
        self.assertFalse(any(c[:2] == ["docker", "kill"] for c in runner.calls))

    def test_failed_hash_cleans_up_and_keeps_live_files(self):
        self.run_sync(enabled="false")
        before = (self.mdir / "acl").read_text()
        write_displays(self.config, ["helm"])
        with self.assertRaises(mqtt_auth.MqttAuthError):
            self.run_sync(runner=FakeRunner(hash_ok=False))
        self.assertEqual((self.mdir / "acl").read_text(), before)
        self.assertFalse((self.mdir / "acl.new").exists())
        self.assertFalse((self.mdir / "passwords.new").exists())

    def test_invalid_displays_config_is_an_error(self):
        self.config.write_text("{not json")
        with self.assertRaises(mqtt_auth.MqttAuthError):
            self.run_sync()

    # --- symlink hardening ------------------------------------------------

    def victim(self, text="victim\n"):
        path = Path(self._tmp.name) / "victim"
        path.write_text(text)
        os.chmod(path, 0o644)
        return path

    def test_symlinked_auth_dir_refused(self):
        target = Path(self._tmp.name) / "elsewhere"
        target.mkdir()
        (self.root / "config" / mqtt_auth.AUTH_DIR_NAME).symlink_to(target)
        with self.assertRaises(mqtt_auth.MqttAuthError):
            self.run_sync()
        self.assertEqual(list(target.iterdir()), [])

    def test_symlinked_config_dir_refused(self):
        real = Path(self._tmp.name) / "realconfig"
        real.mkdir()
        (self.root / "config").rmdir()
        (self.root / "config").symlink_to(real)
        with self.assertRaises(mqtt_auth.MqttAuthError):
            self.run_sync()
        self.assertEqual(list(real.iterdir()), [])

    def test_auth_path_that_is_a_file_refused(self):
        (self.root / "config" / mqtt_auth.AUTH_DIR_NAME).write_text("x")
        with self.assertRaises(mqtt_auth.MqttAuthError):
            self.run_sync()

    def test_symlinked_files_refused_and_target_untouched(self):
        self.run_sync()
        for name in ("acl", "passwords", "acl.prev", "passwords.prev",
                     "acl.new", "passwords.new", "acl.prev.tmp"):
            victim = self.victim()
            link = self.mdir / name
            existing = None
            if link.exists() or link.is_symlink():
                existing = link.read_bytes()
                link.unlink()
            link.symlink_to(victim)
            runner = FakeRunner()
            with self.assertRaises(mqtt_auth.MqttAuthError, msg=name):
                self.run_sync(runner=runner)
            self.assertEqual(victim.read_text(), "victim\n", name)
            self.assertEqual(victim.stat().st_mode & 0o777, 0o644, name)
            self.assertTrue(link.is_symlink(), name)
            self.assertFalse(any(c[:2] == ["docker", "run"] for c in runner.calls), name)
            link.unlink()
            if existing is not None:
                link.write_bytes(existing)
                os.chmod(link, 0o600)

    def test_stale_regular_temp_files_are_cleaned(self):
        self.run_sync()
        for name in ("acl.new", "passwords.new", "acl.prev.tmp", "passwords.prev.tmp"):
            (self.mdir / name).write_text("stale")
        self.run_sync()
        for name in ("acl.new", "passwords.new", "acl.prev.tmp", "passwords.prev.tmp"):
            self.assertFalse((self.mdir / name).exists(), name)

    def test_directory_as_temp_file_refused(self):
        self.run_sync()
        (self.mdir / "acl.new").mkdir()
        with self.assertRaises(mqtt_auth.MqttAuthError):
            self.run_sync()

    def test_legacy_data_dir_files_are_left_alone(self):
        legacy = self.data / "mosquitto"
        legacy.mkdir(parents=True)
        outside = self.victim()
        for name in ("acl", "acl.prev", "acl.new", "passwords", "passwords.prev", "passwords.new"):
            (legacy / name).symlink_to(outside) if name.endswith(".new") else \
                (legacy / name).write_text(f"old {name}\n")
        self.run_sync()
        for name in ("acl", "acl.prev", "passwords", "passwords.prev"):
            self.assertEqual((legacy / name).read_text(), f"old {name}\n")
        self.assertTrue((legacy / "acl.new").is_symlink())
        self.assertEqual(outside.read_text(), "victim\n")

    def test_existing_auth_dir_mode_is_corrected(self):
        self.mdir.mkdir()
        os.chmod(self.mdir, 0o777)
        self.run_sync()
        self.assertEqual(self.mdir.stat().st_mode & 0o777, 0o750)

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "needs root")
    def test_root_refuses_writable_auth_dir(self):
        self.mdir.mkdir()
        os.chmod(self.mdir, 0o770)
        with self.assertRaises(mqtt_auth.MqttAuthError):
            mqtt_auth.sync(self.root, self.data, self.env(), runner=FakeRunner())
        os.chmod(self.mdir, 0o757)
        with self.assertRaises(mqtt_auth.MqttAuthError):
            mqtt_auth.sync(self.root, self.data, self.env(), runner=FakeRunner())

    def test_writable_auth_dir_refused_when_ownership_enforced(self):
        # Exercise the owner/mode check without root by enforcing the current uid.
        self.mdir.mkdir()
        os.chmod(self.mdir, 0o770)
        info = self.mdir.stat()
        with mock.patch.object(mqtt_auth.os, "geteuid", return_value=0, create=True), \
                mock.patch.object(mqtt_auth.os, "fchown"):
            with self.assertRaises(mqtt_auth.MqttAuthError):
                mqtt_auth.sync(self.root, self.data, self.env(), runner=FakeRunner(),
                               dir_owner=(info.st_uid, info.st_gid),
                               file_owner=(info.st_uid, info.st_gid))

    def test_missing_image_and_failed_pull_fail_before_any_write(self):
        class Offline(FakeRunner):
            def __call__(self, argv):
                self.calls.append(list(argv))
                if argv[:3] == ["docker", "image", "inspect"]:
                    return subprocess.CompletedProcess(argv, 1, "", "")
                if argv[:2] == ["docker", "pull"]:
                    return subprocess.CompletedProcess(argv, 1, "", "network unreachable")
                return super().__call__(argv)
        runner = Offline()
        with self.assertRaises(mqtt_auth.MqttAuthError) as ctx:
            self.run_sync(runner=runner)
        self.assertIn("could not be pulled", str(ctx.exception))
        self.assertFalse(self.mdir.exists())
        self.assertFalse((self.root / "config" / mqtt_auth.SECRETS_NAME).exists())
        self.assertFalse(any(c[:2] == ["docker", "run"] for c in runner.calls))

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "needs root")
    def test_root_refuses_foreign_owned_auth_dir(self):
        self.mdir.mkdir()
        os.chown(self.mdir, 1234, 1234)
        with self.assertRaises(mqtt_auth.MqttAuthError):
            mqtt_auth.sync(self.root, self.data, self.env(), runner=FakeRunner())

    def test_source_never_touches_the_broker_data_directory(self):
        source = (ROOT / "displays" / "mqtt_auth.py").read_text()
        self.assertNotIn('"mosquitto"', source)
        self.assertNotIn("/mosquitto/data", source)

    def test_image_is_read_from_compose(self):
        self.assertEqual(mqtt_auth.find_image(self.root), "eclipse-mosquitto:9.9.9-alpine")
        self.assertRegex(mqtt_auth.find_image(ROOT), r"^eclipse-mosquitto:")


if __name__ == "__main__":
    unittest.main()
