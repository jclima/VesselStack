#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import stat
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
topic write vesselstack/v1/notifications/#
topic write vesselstack/v1/ha/#
topic write vesselstack/v1/alarms/#
topic write vesselstack/v1/bridge/#
topic read vesselstack/v1/displays/+/status

user vsdisp-aux
topic read vesselstack/v1/instruments/#
topic read vesselstack/v1/notifications/#
topic read vesselstack/v1/ha/#
topic read vesselstack/v1/alarms/#
topic read vesselstack/v1/bridge/#
topic write vesselstack/v1/displays/vsdisp-aux/status

user vsdisp-helm
topic read vesselstack/v1/instruments/#
topic read vesselstack/v1/notifications/#
topic read vesselstack/v1/ha/#
topic read vesselstack/v1/alarms/#
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

    def _plaintext_probe(self):
        seen = {}
        mdir = self.mdir
        inner = FakeRunner()

        def runner(argv):
            if argv[:2] == ["docker", "run"]:
                new = mdir / "passwords.new"
                seen["exists"] = new.exists()
                seen["mode"] = new.stat().st_mode & 0o777
                seen["text"] = new.read_text()
            return inner(argv)
        return runner, seen

    def test_plaintext_list_is_0600_when_hashing_runs(self):
        write_displays(self.config, ["helm"])
        runner, seen = self._plaintext_probe()
        self.run_sync(runner=runner)
        self.assertTrue(seen["exists"])
        self.assertEqual(seen["mode"], 0o600)
        self.assertIn("homeassistant:", seen["text"])
        self.assertFalse((self.mdir / "passwords.new").exists())

    def test_plaintext_list_is_chowned_root_never_broker(self):
        write_displays(self.config, ["helm"])
        runner, seen = self._plaintext_probe()
        chowns = []
        names = {}
        real_open = os.open

        def fake_fchown(fd, uid, gid):
            chowns.append((names.get(fd), uid, gid))

        def fake_open(path, *a, **kw):
            fd = real_open(path, *a, **kw)
            names[fd] = path
            return fd
        with mock.patch.object(mqtt_auth.os, "geteuid", return_value=0), \
                mock.patch.object(mqtt_auth.os, "fchown", side_effect=fake_fchown), \
                mock.patch.object(mqtt_auth.os, "open", side_effect=fake_open):
            mqtt_auth.sync(self.root, self.data, self.env(), runner=runner,
                           dir_owner=None, file_owner=mqtt_auth.FILE_OWNER)
        self.assertEqual(seen["mode"], 0o600)
        plain = [(u, g) for n, u, g in chowns if n == "passwords.new"]
        self.assertEqual(plain, [(0, 0)])
        self.assertIn((1883, 1883), [(u, g) for _, u, g in chowns])

    def test_plaintext_removed_when_hashing_fails(self):
        write_displays(self.config, ["helm"])
        with self.assertRaises(mqtt_auth.MqttAuthError):
            self.run_sync(runner=FakeRunner(hash_ok=False))
        self.assertFalse((self.mdir / "passwords.new").exists())

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

    def test_write_atomic_refuses_symlinks(self):
        target = Path(self._tmp.name) / "victim"
        target.write_text("keep")
        final = Path(self._tmp.name) / "out.json"
        final.symlink_to(target)
        with self.assertRaises(mqtt_auth.MqttAuthError):
            mqtt_auth.write_atomic(final, "new")
        self.assertEqual(target.read_text(), "keep")
        self.assertTrue(final.is_symlink())
        final.unlink()
        # a planted temp file symlink is replaced, never written through
        tmp = Path(self._tmp.name) / "out.json.tmp"
        tmp.symlink_to(target)
        mqtt_auth.write_atomic(final, "new")
        self.assertEqual(target.read_text(), "keep")
        self.assertEqual(final.read_text(), "new")
        self.assertFalse(final.is_symlink())
        self.assertEqual(stat.S_IMODE(final.stat().st_mode), 0o600)

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

    # --- hardening: trailing newlines, leftovers, hashing container -------------

    def test_trailing_newline_usernames_rejected(self):
        for name in ("homeassistant\n", "bad\n"):
            with self.assertRaises(mqtt_auth.MqttAuthError):
                mqtt_auth._check_username("MQTT_USERNAME", name)
        self.assertEqual(mqtt_auth._check_username("MQTT_USERNAME", "ok-user_1.x"), "ok-user_1.x")
        with self.assertRaises(mqtt_auth.MqttAuthError):
            self.run_sync(DISPLAYS_BRIDGE_MQTT_USERNAME="bridge\n")

    def test_hashing_container_is_locked_down(self):
        _, runner = self.run_sync()
        run = next(c for c in runner.calls if c[:2] == ["docker", "run"])
        flags = " ".join(run)
        self.assertIn("--network none", flags)
        self.assertIn("--cap-drop ALL", flags)
        self.assertIn("--cap-add CHOWN", flags)
        self.assertEqual([a for i, a in enumerate(run) if run[i - 1] == "--cap-add"], ["CHOWN"])
        self.assertIn("umask 077", run[-1])
        self.assertLess(run[-1].index("chmod 0600"), run[-1].index("chown 1883:1883"))

    def test_mosquitto_passwd_leftovers_are_removed_on_success_and_failure(self):
        inner = FakeRunner()
        mdir = self.mdir

        def leaving(argv):
            if argv[:2] == ["docker", "run"]:
                (mdir / "passwords.new.backup.AbC123").write_text("u:plaintext\n")
                (mdir / "passwords.new.new").write_text("u:plaintext\n")
            return inner(argv)
        self.run_sync(runner=leaving)
        self.assertEqual(sorted(p.name for p in mdir.iterdir() if "new" in p.name), [])

        def failing(argv):
            if argv[:2] == ["docker", "run"]:
                (mdir / "passwords.new.backup.XyZ789").write_text("u:plaintext\n")
                return subprocess.CompletedProcess(argv, 1, "", "boom")
            return inner(argv)
        with self.assertRaises(mqtt_auth.MqttAuthError):
            self.run_sync(runner=failing)
        self.assertEqual(sorted(p.name for p in mdir.iterdir() if "new" in p.name), [])

    def test_stale_plaintext_leftovers_are_removed_before_a_sync(self):
        self.run_sync()
        stale = self.mdir / "passwords.new.backup.OLD000"
        stale.write_text("u:plaintext\n")
        self.run_sync()
        self.assertFalse(stale.exists())

    # --- lock ---------------------------------------------------------------------

    def test_config_lock_is_exclusive_and_times_out(self):
        with mqtt_auth.config_lock(self.root):
            with self.assertRaises(mqtt_auth.MqttAuthError) as caught:
                with mqtt_auth.config_lock(self.root, timeout=0.2):
                    pass
            self.assertIn("another displayctl", str(caught.exception))
        with mqtt_auth.config_lock(self.root, timeout=0.2):
            pass

    def test_config_lock_creates_a_missing_config_directory(self):
        fresh = Path(self._tmp.name) / "fresh"
        fresh.mkdir()
        with mqtt_auth.config_lock(fresh):
            self.assertTrue((fresh / "config" / mqtt_auth.LOCK_NAME).exists())

    # --- preflight ------------------------------------------------------------------

    def preflight(self, runner=None, mode="none", **env):
        return mqtt_auth.preflight(self.root, self.env(**env), runner=runner or FakeRunner(),
                                   image_mode=mode)

    def test_preflight_passes_and_changes_nothing(self):
        write_displays(self.config, ["helm"])
        runner = FakeRunner()
        self.assertEqual(self.preflight(runner, mode="check"), [])
        self.assertFalse((self.root / "config" / mqtt_auth.LOCK_NAME).exists())
        self.assertFalse(self.mdir.exists())
        self.assertFalse((self.root / "config" / mqtt_auth.SECRETS_NAME).exists())
        self.assertEqual([c[:3] for c in runner.calls], [["docker", "image", "inspect"]])

    def test_preflight_rejects_unusable_admin_passwords(self):
        for bad in ("a:b", " lead", "trail ", "x\ny"):
            with self.assertRaises(mqtt_auth.MqttAuthError, msg=repr(bad)):
                self.preflight(MQTT_PASSWORD=bad)
        # Values the installer replaces with a generated secret are fine.
        self.preflight(MQTT_PASSWORD="GENERATE")
        self.preflight(MQTT_PASSWORD="")

    def test_preflight_rejects_collisions_and_bad_names(self):
        write_displays(self.config, ["helm"])
        with self.assertRaises(mqtt_auth.MqttAuthError):
            self.preflight(DISPLAYS_BRIDGE_MQTT_USERNAME="homeassistant")
        with self.assertRaises(mqtt_auth.MqttAuthError):
            self.preflight(MQTT_USERNAME="vsdisp-helm")
        with self.assertRaises(mqtt_auth.MqttAuthError):
            self.preflight(DISPLAYS_BRIDGE_MQTT_USERNAME="bad name")
        # The bridge name only matters when displays are enabled.
        self.preflight(enabled="false", DISPLAYS_BRIDGE_MQTT_USERNAME="homeassistant")

    def test_preflight_rejects_invalid_displays_json_only_when_enabled(self):
        self.config.write_text("{not json")
        with self.assertRaises(mqtt_auth.MqttAuthError):
            self.preflight()
        self.preflight(enabled="false")
        self.config.unlink()
        self.preflight()  # no file yet: nothing to validate

    def test_preflight_warns_about_ha_items_without_a_token_or_with_a_bad_url(self):
        cfg = json.loads(json.dumps(EXAMPLE))
        cfg["ha"] = {"bilge": {"entity_id": "binary_sensor.bilge", "label": "Bilge", "kind": "binary",
                               "states": [{"match": "on", "sev": "warn", "text": "ON"}]}}
        self.config.write_text(json.dumps(cfg))
        warnings = self.preflight()
        self.assertEqual(len(warnings), 1)
        self.assertIn("DISPLAYS_HA_TOKEN is empty", warnings[0])
        self.assertIn("SignalK items are not affected", warnings[0])
        self.assertEqual(self.preflight(DISPLAYS_HA_TOKEN="tok"), [])
        self.assertEqual(self.preflight(DISPLAYS_HA_TOKEN="tok", HOME_ASSISTANT_URL="https://ha.local"), [])
        bad = self.preflight(DISPLAYS_HA_TOKEN="tok", HOME_ASSISTANT_URL="ftp://ha.local")
        self.assertEqual(len(bad), 1)
        self.assertIn("HOME_ASSISTANT_URL", bad[0])
        creds = self.preflight(DISPLAYS_HA_TOKEN="tok", HOME_ASSISTANT_URL="http://u" + ":pw-secret@ha.local")
        self.assertEqual(len(creds), 1)
        self.assertNotIn("pw-secret", creds[0])
        self.assertEqual(self.preflight(enabled="false"), [])          # displays off: nothing to warn about
        write_displays(self.config, ["helm"])
        self.assertEqual(self.preflight(), [])                          # no ha items: silent as before

    def test_preflight_image_modes(self):
        class Absent(FakeRunner):
            registry = True

            def __call__(self, argv):
                self.calls.append(list(argv))
                if argv[:3] == ["docker", "image", "inspect"]:
                    return subprocess.CompletedProcess(argv, 1, "", "")
                if argv[:3] == ["docker", "manifest", "inspect"]:
                    return subprocess.CompletedProcess(argv, 0 if self.registry else 1, "", "")
                if argv[:2] == ["docker", "pull"]:
                    return subprocess.CompletedProcess(argv, 0 if self.registry else 1, "", "offline")
                return super().__call__(argv)
        online = Absent()
        warnings = self.preflight(online, mode="check")
        self.assertEqual(len(warnings), 1)
        self.assertIn("will be pulled", warnings[0])
        self.assertFalse(any(c[:2] in (["docker", "pull"], ["docker", "run"]) for c in online.calls))
        offline = Absent()
        offline.registry = False
        with self.assertRaises(mqtt_auth.MqttAuthError) as caught:
            self.preflight(offline, mode="check")
        self.assertIn("docker pull eclipse-mosquitto:9.9.9-alpine", str(caught.exception))
        self.assertFalse(any(c[:2] == ["docker", "pull"] for c in offline.calls))
        pulled = Absent()
        self.preflight(pulled, mode="pull")
        self.assertIn(["docker", "pull", "eclipse-mosquitto:9.9.9-alpine"], pulled.calls)
        with self.assertRaises(mqtt_auth.MqttAuthError):
            self.preflight(offline, mode="pull")

    def test_compose_argument_overrides_the_installed_definition(self):
        other = Path(self._tmp.name) / "release-compose.yml"
        other.write_text("services:\n  mosquitto:\n    image: eclipse-mosquitto:8.8.8-alpine\n")
        self.assertEqual(mqtt_auth.find_image(self.root, other), "eclipse-mosquitto:8.8.8-alpine")
        runner = FakeRunner()
        mqtt_auth.sync(self.root, self.data, self.env(), runner=runner, dir_owner=None,
                       file_owner=None, compose=other)
        run = next(c for c in runner.calls if c[:2] == ["docker", "run"])
        self.assertIn("eclipse-mosquitto:8.8.8-alpine", run)


class ReadRegularTests(unittest.TestCase):
    """_read_regular must never block on, or read from, a non-regular file."""

    PROBE = (
        "import os, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import mqtt_auth\n"
        "fd = os.open(sys.argv[2], os.O_RDONLY | os.O_DIRECTORY)\n"
        "try:\n"
        "    mqtt_auth._read_regular(sys.argv[3], fd)\n"
        "except mqtt_auth.MqttAuthError as exc:\n"
        "    print('refused:', exc)\n"
    )

    def test_fifo_is_refused_without_hanging(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("no FIFOs on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            os.mkfifo(os.path.join(tmp, "artifact.bin"))
            # A subprocess with a timeout: a regression would block in open() forever.
            result = subprocess.run(
                [sys.executable, "-c", self.PROBE, str(ROOT / "displays"), tmp, "artifact.bin"],
                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("refused", result.stdout)
        self.assertIn("not a regular file", result.stdout)

    def test_symlink_and_regular_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "real").write_bytes(b"data")
            os.symlink("real", os.path.join(tmp, "link"))
            fd = os.open(tmp, os.O_RDONLY | os.O_DIRECTORY)
            try:
                self.assertEqual(mqtt_auth._read_regular("real", fd), b"data")
                self.assertIsNone(mqtt_auth._read_regular("absent", fd))
                with self.assertRaises(mqtt_auth.MqttAuthError):
                    mqtt_auth._read_regular("link", fd)
            finally:
                os.close(fd)


if __name__ == "__main__":
    unittest.main()
