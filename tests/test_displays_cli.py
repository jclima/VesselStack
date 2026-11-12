#!/usr/bin/env python3

from __future__ import annotations

import fcntl
import importlib.util
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
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import catalog  # noqa: E402
import displayctl  # noqa: E402
import editor  # noqa: E402
import fake_signalk  # noqa: E402
import firmware  # noqa: E402
import lifecycle  # noqa: E402
import mqtt_auth  # noqa: E402
import render  # noqa: E402
import schema  # noqa: E402

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
        self.assertIn("display export", text)
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

    def test_add_default_page_prefers_instrument_only_layouts_over_status_grid(self):
        registry = lifecycle.load_registry()
        config = json.loads(self.config.read_text())
        self.assertIn("status-grid", registry["boards"][BOARD]["layouts"])
        self.assertEqual(lifecycle.default_page(registry, config, BOARD)["layout"], "grid-2x2")
        registry["boards"]["only"] = dict(registry["boards"][BOARD], layouts=["status-grid"])
        page = lifecycle.default_page(registry, config, "only")
        self.assertEqual((page["layout"], len(page["items"])), ("status-grid", 4))

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

    # --- themes ----------------------------------------------------------------

    def test_themes_lists_the_four_themes(self):
        code, out, _ = self.run_cli("themes")
        self.assertEqual(code, 0)
        listed = [line.split()[0] for line in out.splitlines()[:-1]]
        self.assertEqual(listed, ["classic", "daylight", "high-contrast", "ocean"])
        self.assertIn("set-theme", out)

    def test_themes_json_has_the_full_palettes(self):
        code, out, _ = self.run_cli("themes", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(list(data["themes"]), ["classic", "daylight", "high-contrast", "ocean"])
        self.assertEqual(data["themes"]["ocean"]["day"]["tile"], "#12305A")
        self.assertIn("night", data["themes"]["daylight"])
        self.assertTrue(all(theme["description"] for theme in data["themes"].values()))

    def test_set_theme_sets_validates_and_prints_next_steps(self):
        self.add("helm")
        lifecycle.set_theme(self.ctx(), "helm", "ocean")
        self.assertEqual(self.cfg()["displays"]["helm"]["ui"], {"theme": "ocean"})
        text = self.output()
        self.assertIn("display build helm", text)
        self.assertIn("display flash helm", text)
        self.assertLess(text.index("display build helm"), text.index("display flash helm"))
        self.assertEqual(oct(self.config.stat().st_mode & 0o777), "0o644")

    def test_set_theme_keeps_other_ui_keys(self):
        self.add("helm")
        cfg = self.cfg()
        cfg["displays"]["helm"]["ui"] = {"swipe": True}
        self.config.write_text(json.dumps(cfg))
        lifecycle.set_theme(self.ctx(), "helm", "daylight")
        self.assertEqual(self.cfg()["displays"]["helm"]["ui"], {"swipe": True, "theme": "daylight"})
        lifecycle.set_theme(self.ctx(), "helm", "classic")
        self.assertEqual(self.cfg()["displays"]["helm"]["ui"], {"swipe": True})

    def test_set_theme_classic_deletes_the_key_and_an_emptied_ui(self):
        self.add("helm")
        before = self.cfg()
        lifecycle.set_theme(self.ctx(), "helm", "high-contrast")
        lifecycle.set_theme(self.ctx(), "helm", "classic")
        self.assertEqual(self.cfg(), before)   # readable by older versions again
        self.assertNotIn("ui", self.cfg()["displays"]["helm"])

    def test_set_theme_classic_removes_an_explicit_classic_key(self):
        self.add("helm")
        cfg = self.cfg()
        cfg["displays"]["helm"]["ui"] = {"theme": "classic"}
        self.config.write_text(json.dumps(cfg))
        lifecycle.set_theme(self.ctx(), "helm", "classic")
        self.assertNotIn("ui", self.cfg()["displays"]["helm"])
        self.assertIn("removed", self.output())

    def test_set_theme_classic_on_a_display_without_a_theme_changes_nothing(self):
        self.add("helm")
        before = self.config.read_text()
        lifecycle.set_theme(self.ctx(), "helm", "classic")
        self.assertEqual(self.config.read_text(), before)
        self.assertIn("already", self.output())
        self.assertNotIn("display build", self.output())

    def test_set_theme_rejects_unknown_theme_and_display(self):
        self.add("helm")
        before = self.config.read_text()
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            lifecycle.set_theme(self.ctx(), "helm", "neon")
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("classic, daylight, high-contrast, ocean", str(caught.exception))
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            lifecycle.set_theme(self.ctx(), "ghost", "ocean")
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.config.read_text(), before)

    def test_set_theme_waits_for_the_build_lock_then_gives_up_unchanged(self):
        import render
        self.add("helm")
        before = self.config.read_text()
        self.make_build_tree("helm")
        with render.build_lock(self.ctx(), "helm"):
            with self.assertRaises(lifecycle.LifecycleError) as caught:
                lifecycle.set_theme(self.ctx(build_lock_timeout=0.2), "helm", "ocean")
        self.assertIn("another build/flash", str(caught.exception))
        self.assertEqual(self.config.read_text(), before)

    def test_set_theme_cli(self):
        self.add("helm")
        code, out, err = self.run_cli("set-theme", "helm", "ocean", "--allow-non-root")
        self.assertEqual((code, err), (0, ""), err)
        self.assertEqual(self.cfg()["displays"]["helm"]["ui"]["theme"], "ocean")
        code, _, err = self.run_cli("set-theme", "helm", "neon", "--allow-non-root")
        self.assertEqual(code, 2)
        self.assertIn("ocean", err)

    def test_set_theme_needs_root(self):
        self.add("helm")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("running as root")
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.set_theme(self.ctx(require_root=True), "helm", "ocean")

    # --- build tree cleanup --------------------------------------------------

    def make_build_tree(self, *ids):
        for display_id in ids:
            base = self.root / "build" / "displays" / display_id
            for sub in ("project", "project.prev", "firmware", "firmware.prev"):
                (base / sub).mkdir(parents=True, mode=0o700)
            for sub in ("project", "project.prev"):
                (base / sub / "secrets.yaml").write_text("wifi_password: old")
                (base / sub / f"{display_id}.yaml").write_text("device")
            (base / "firmware" / "firmware.ota.bin").write_text("compiled-in secrets")
            (base / "firmware.prev" / "firmware.ota.bin").write_text("older compiled-in secrets")

    def test_remove_deletes_exactly_that_displays_build_tree(self):
        self.add("helm")
        self.add("helm-2")
        self.make_build_tree("helm", "helm-2")
        displays = self.root / "build" / "displays"
        for legacy in ("helm.new", "helm.prev"):  # old-layout siblings
            (displays / legacy).mkdir()
            (displays / legacy / "secrets.yaml").write_text("old")
        lifecycle.remove(self.ctx(), "helm", True)
        self.assertEqual(sorted(p.name for p in displays.iterdir() if not p.name.startswith(".")),
                         ["helm-2"])  # not a prefix glob: helm-2 is a different display
        self.assertTrue((displays / "helm-2" / "firmware" / "firmware.ota.bin").exists())
        self.assertIn("Deleted build/displays/helm", self.output())

    def test_remove_without_a_build_tree_creates_nothing(self):
        self.add("helm")
        lifecycle.remove(self.ctx(), "helm", True)
        self.assertFalse((self.root / "build").exists())
        self.assertNotIn("Deleted build", self.output())

    def test_remove_waits_for_a_running_build_and_changes_nothing_if_it_stays_busy(self):
        import render
        self.add("helm")
        self.make_build_tree("helm")
        with render.build_lock(self.ctx(), "helm", notify=lambda text: None):
            with self.assertRaises(lifecycle.LifecycleError) as caught:
                lifecycle.remove(self.ctx(build_lock_timeout=0.3), "helm", True)
        self.assertIn("another build/flash for helm is running", str(caught.exception))
        self.assertIn("helm", self.cfg()["displays"])
        self.assertTrue((self.root / "build" / "displays" / "helm" / "firmware").exists())

    def test_wifi_clear_deletes_rendered_project_secrets_but_not_firmware(self):
        self.wifi(f"net\n{WIFI_PASSWORD}\n")
        self.make_build_tree("helm", "aux")
        lifecycle.wifi(self.ctx(), clear=True)
        for display_id in ("helm", "aux"):
            base = self.root / "build" / "displays" / display_id
            self.assertFalse((base / "project" / "secrets.yaml").exists())
            self.assertFalse((base / "project.prev" / "secrets.yaml").exists())
            self.assertTrue((base / "project" / f"{display_id}.yaml").exists())
            self.assertTrue((base / "firmware" / "firmware.ota.bin").exists())
        text = self.output()
        self.assertIn("aux, helm", text)
        self.assertIn("still contains the old Wi-Fi credentials", text)

    def secrets_everywhere(self, display_id):
        """Plant a secrets.yaml in every location a rendered tree has ever lived."""
        base = self.root / "build" / "displays"
        paths = [base / display_id / "project.new", base / display_id,
                 base / f"{display_id}.new", base / f"{display_id}.prev",
                 base / f"{display_id}.new" / "project", base / f"{display_id}.prev" / "project.prev"]
        for path in paths:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            (path / "secrets.yaml").write_text("wifi_password: old")
        return [path / "secrets.yaml" for path in paths] + [
            base / display_id / "project" / "secrets.yaml",
            base / display_id / "project.prev" / "secrets.yaml"]

    def test_wifi_clear_scrubs_every_build_tree_location(self):
        self.wifi(f"net\n{WIFI_PASSWORD}\n")
        self.make_build_tree("helm", "aux")
        planted = self.secrets_everywhere("helm")
        self.assertTrue(all(path.exists() for path in planted))
        lifecycle.wifi(self.ctx(), clear=True)
        self.assertEqual([str(path) for path in planted if path.exists()], [])
        base = self.root / "build" / "displays"
        self.assertTrue((base / "helm" / "firmware" / "firmware.ota.bin").exists())
        self.assertTrue((base / "helm" / "project" / "helm.yaml").exists())

    def test_wifi_clear_scrubs_even_when_no_credentials_were_stored(self):
        self.make_build_tree("helm")
        planted = self.secrets_everywhere("helm")
        lifecycle.wifi(self.ctx(), clear=True)
        text = self.output()
        self.assertIn("No Wi-Fi credentials were stored", text)
        self.assertIn("Deleted secrets.yaml from the rendered project of: helm", text)
        self.assertEqual([str(path) for path in planted if path.exists()], [])

    def test_scrub_covers_legacy_siblings_alone_and_ignores_other_names(self):
        import render
        base = self.root / "build" / "displays"
        (base / "ghost.new").mkdir(parents=True, mode=0o700)
        (base / "ghost.new" / "secrets.yaml").write_text("x")
        (base / "not.an.id").mkdir(mode=0o700)
        (base / "not.an.id" / "secrets.yaml").write_text("keep")
        cleaned, skipped = render.scrub_project_secrets(self.ctx())
        self.assertEqual((cleaned, skipped), (["ghost"], []))
        self.assertFalse((base / "ghost.new" / "secrets.yaml").exists())
        self.assertTrue((base / "not.an.id" / "secrets.yaml").exists())

    def test_scrub_follows_no_symlink(self):
        import render
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        (outside / "secrets.yaml").write_text("must survive")
        self.make_build_tree("helm")
        base = self.root / "build" / "displays"
        (base / "helm.new").symlink_to(outside)
        (base / "helm" / "project.new").symlink_to(outside)
        render.scrub_project_secrets(self.ctx())
        self.assertTrue((outside / "secrets.yaml").exists())
        self.assertFalse((base / "helm" / "project" / "secrets.yaml").exists())

    def test_scrub_waits_for_each_displays_lock_including_its_siblings(self):
        import render
        self.make_build_tree("helm")
        planted = self.secrets_everywhere("helm")
        with render.build_lock(self.ctx(), "helm", notify=lambda text: None):
            cleaned, skipped = render.scrub_project_secrets(self.ctx(), timeout=0.2)
        self.assertEqual((cleaned, skipped), ([], ["helm"]))
        self.assertTrue(all(path.exists() for path in planted))

    def test_wifi_clear_skips_a_display_that_is_being_built(self):
        import render
        self.wifi(f"net\n{WIFI_PASSWORD}\n")
        self.make_build_tree("helm", "aux")
        with render.build_lock(self.ctx(), "helm", notify=lambda text: None):
            lifecycle.wifi(self.ctx(), clear=True)
        base = self.root / "build" / "displays"
        self.assertTrue((base / "helm" / "project" / "secrets.yaml").exists())
        self.assertFalse((base / "aux" / "project" / "secrets.yaml").exists())
        self.assertNotIn("wifi", self.sec())  # the credentials themselves are gone regardless
        self.assertIn("a build or flash was running for helm", self.output())

    def test_wifi_change_says_existing_projects_and_firmware_keep_the_old_credentials(self):
        self.wifi(f"net\n{WIFI_PASSWORD}\n")
        self.make_build_tree("helm")
        self.wifi("othernet\nabcdefgh2\n")
        self.assertTrue((self.root / "build/displays/helm/project/secrets.yaml").exists())
        self.assertIn("OLD Wi-Fi credentials", self.output())

    def test_rotate_leaves_build_tree_alone(self):
        self.add("helm")
        self.make_build_tree("helm")
        lifecycle.rotate(self.ctx(), "helm")
        self.assertTrue((self.root / "build/displays/helm/firmware/firmware.ota.bin").exists())

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


SK_TOKEN = "signalk-token-secret"


class EditorTests(unittest.TestCase):
    """catalog / show / set-pages / history / restore-config / serial-ports."""

    write_config = CliTests.write_config
    ctx = CliTests.ctx
    cfg = CliTests.cfg
    sec = CliTests.sec
    add = CliTests.add
    output = CliTests.output
    run_cli = CliTests.run_cli
    make_build_tree = CliTests.make_build_tree

    def setUp(self):
        CliTests.setUp(self)
        self.add("helm")
        secrets = self.sec()
        secrets["wifi"] = {"ssid": "boat-net-ssid", "password": WIFI_PASSWORD}
        self.secrets.write_text(json.dumps(secrets))
        self.backups = self.root / "config" / editor.BACKUP_DIR
        self.runner.calls.clear()

    # --- helpers ---

    def revision(self):
        return editor.revision_of(self.config.read_text())

    def pages(self, title="Nav", items=("sog", "depth"), layout="grid-2x2"):
        return [{"title": title, "layout": layout, "items": list(items)}]

    def set_pages(self, payload, rev=None, ctx=None, display="helm"):
        return editor.set_pages(ctx or self.ctx(), display, rev or self.revision(), json.dumps(payload))

    def run_stdin(self, text, *argv, **env):
        stdin = io.TextIOWrapper(io.BytesIO(text.encode()), encoding="utf-8")
        with mock.patch.object(sys, "stdin", stdin):
            return self.run_cli(*argv, **env)

    def restarts(self):
        return [c for c in self.runner.calls if c[:1] == ["systemctl"]]

    def stamps(self):
        return sorted(p.name for p in self.backups.iterdir())

    # --- show ---

    def test_show_describes_the_display_the_board_and_the_layouts(self):
        info = editor.show(self.ctx(), "helm")
        self.assertEqual(info["revision"], self.revision())
        self.assertEqual(info["display"]["id"], "helm")
        self.assertEqual(info["theme"], "classic")
        self.assertEqual([t["id"] for t in info["themes"]], ["classic", "daylight", "high-contrast", "ocean"])
        board = info["board"]
        self.assertEqual((board["width"], board["height"], board["shape"]), (480, 480, "round"))
        self.assertEqual(board["content_width"], 340)
        self.assertEqual(sorted(info["layouts"]), sorted(board["layouts"]))
        self.assertNotIn("list-6", info["layouts"])
        self.assertEqual(info["layouts"]["grid-2x2"]["capacity"], 4)
        self.assertTrue(info["layouts"]["tank-bars"]["needs_range"])
        self.assertEqual([s["role"] for s in info["layouts"]["electrical"]["slots"]],
                         ["voltage", "soc", "current", "shore", "solar"])
        self.assertEqual(info["layouts"]["electrical"]["min_items"], 3)
        self.assertEqual(info["layouts"]["status-grid"]["types"], ["instrument", "notification", "ha"])
        self.assertEqual(len(info["preview"]), len(info["display"]["pages"]))
        self.assertEqual(info["preview"][0]["layout"], "grid-2x2")
        self.assertEqual(len(info["preview"][0]["cells"]), 4)
        self.assertEqual(info["preview_layouts"]["grid-2x2"][4], info["preview"][0])
        self.assertEqual(info["preview_layouts"]["status-grid"][0]["cells"], [])
        self.assertEqual(len(info["preview_layouts"]["status-grid"][9]["cells"]), 9)
        self.assertEqual(info["instruments"]["sog"]["used_on"], ["helm"])
        self.assertEqual(info["alarm_sources"], [])

    def test_preview_geometry_is_computed_by_the_render_helpers(self):
        board = schema.load_registry()["boards"][BOARD]
        grid = render.preview_geometry(board, "status-grid", 9, header=True)
        cell_w, cell_h = render.status_cell_size(340, 340, 9)
        self.assertEqual((grid["cells"][0]["width"], grid["cells"][0]["height"]), (cell_w, cell_h))
        self.assertEqual(grid["cells"][1]["x"] - grid["cells"][0]["x"], cell_w + render.STATUS_GAP)
        self.assertEqual(grid["header"]["y"], board["ring"]["header_y"])
        arc = render.preview_geometry(board, "gauge-arc", 1, header=True)["arc"]
        self.assertEqual(arc["diameter"], render.gauge_diameter(board))
        self.assertEqual(arc["start_angle"], render.ARC_START_GAP_TOP)
        self.assertEqual(render.preview_geometry(board, "gauge-arc", 1)["arc"]["start_angle"],
                         render.ARC_START_GAP_BOTTOM)
        bars = render.preview_geometry(board, "tank-bars", 3)["cells"]
        self.assertEqual(len(bars), 3)
        self.assertEqual(bars[0]["width"], min(render.BAR_WIDTH, render.bar_column_width(340, 3)))

    def test_show_json_never_contains_a_secret_value(self):
        secrets = self.sec()
        values = [WIFI_PASSWORD, ADMIN_PASSWORD, "boat-net-ssid", SK_TOKEN,
                  *secrets["displays"]["helm"].values()]
        self.assertTrue(all(isinstance(v, str) and len(v) > 8 for v in values))
        ctx = self.ctx(runner=self.runner)
        ctx.env["DISPLAYS_SIGNALK_TOKEN"] = SK_TOKEN
        ctx.env["SIGNALK_URL"] = "http://192.0.2.5:3000"
        text = json.dumps(editor.show(ctx, "helm"))
        for value in values:
            self.assertNotIn(value, text)
        self.assertNotIn("192.0.2.5", text)
        ready = json.loads(text)["readiness"]
        self.assertIs(ready["mqtt_password_stored"], True)
        self.assertIs(ready["wifi_stored"], True)
        code, out, err = self.run_cli("show", "helm", "--json", "--allow-non-root", DISPLAYS_SIGNALK_TOKEN=SK_TOKEN)
        self.assertEqual(code, 0, err)
        for value in values:
            self.assertNotIn(value, out)

    def test_list_json_has_only_id_name_board_and_page_count(self):
        self.add("aux")
        code, out, err = self.run_cli("list", "--json", "--allow-non-root", DISPLAYS_SIGNALK_TOKEN=SK_TOKEN)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {"schema": 1, "displays": [
            {"id": "helm", "name": "Helm", "board": BOARD, "pages": 1},
            {"id": "aux", "name": "Aux", "board": BOARD, "pages": 1}]})
        for value in (ADMIN_PASSWORD, WIFI_PASSWORD, SK_TOKEN, "vsdisp-helm", "boat-net-ssid"):
            self.assertNotIn(value, out)
        self.assertNotIn(self.sec()["displays"]["helm"]["mqtt_password"], out)

    def test_list_json_without_a_configuration_is_empty(self):
        self.config.unlink()
        self.assertEqual(editor.list_displays(self.ctx()), {"schema": 1, "displays": []})

    def test_list_without_json_is_still_the_table(self):
        code, out, err = self.run_cli("list", "--allow-non-root")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("ID "))
        self.assertNotIn("{", out)

    def test_show_readiness_flags(self):
        ready = editor.show(self.ctx(runner=self.runner), "helm")["readiness"]
        self.assertIs(ready["displays_enabled"], True)
        self.assertIs(ready["docker_available"], True)
        self.assertIs(ready["esphome_image_present"], True)
        self.assertFalse(ready["data_bind_non_loopback"])
        self.assertIn("lan", ready["data_bind_hint"])
        self.assertEqual(ready["firmware"]["status"], "none")
        self.assertIsNone(ready["last_flash"])
        self.assertIsInstance(ready["free_disk_bytes"], int)
        ctx = self.ctx(runner=self.runner)
        ctx.env["VESSELSTACK_DATA_BIND_ADDRESS"] = "192.0.2.10"
        ready = editor.show(ctx, "helm")["readiness"]
        self.assertTrue(ready["data_bind_non_loopback"])
        self.assertIsNone(ready["data_bind_hint"])
        for loopback in ("127.0.0.1", "127.0.1.1", "::1", "localhost"):
            ctx.env["VESSELSTACK_DATA_BIND_ADDRESS"] = loopback
            self.assertFalse(editor.show(ctx, "helm")["readiness"]["data_bind_non_loopback"], loopback)

    def test_show_readiness_without_docker_wifi_or_secrets(self):
        secrets = self.sec()
        del secrets["wifi"]
        self.secrets.write_text(json.dumps(secrets))
        failing = mock.Mock(side_effect=FileNotFoundError("docker"))
        ready = editor.show(self.ctx(runner=failing), "helm")["readiness"]
        self.assertFalse(ready["docker_available"])
        self.assertFalse(ready["esphome_image_present"])
        self.assertIs(ready["wifi_stored"], False)
        self.secrets.unlink()
        ready = editor.show(self.ctx(runner=failing), "helm")["readiness"]
        self.assertIs(ready["mqtt_password_stored"], False)

    def firmware_manifest(self, files, **override):
        base = self.root / "build" / "displays" / "helm"
        (base / "firmware").mkdir(parents=True, mode=0o700, exist_ok=True)
        manifest = {"schema": 1, "display": "helm", "built_at_utc": "2026-01-01T00:00:00Z",
                    "esphome_version": "2026.10.0",
                    "source_sha256": firmware.source_hash(files, "helm"),
                    "secrets_sha256": firmware.secrets_fingerprint(files, "helm")}
        manifest.update(override)
        (base / "firmware" / "manifest.json").write_text(json.dumps(manifest))
        return base

    def test_show_firmware_status_current_then_outdated(self):
        files = render.collect(self.ctx(), "helm").files
        self.firmware_manifest(files)
        state = editor.show(self.ctx(runner=self.runner), "helm")["readiness"]["firmware"]
        self.assertEqual((state["status"], state["reasons"], state["credentials_checked"]),
                         ("current", [], True))
        self.assertEqual(state["built_at_utc"], "2026-01-01T00:00:00Z")
        text = json.dumps(state)
        self.assertNotIn(json.loads((self.root / "build/displays/helm/firmware/manifest.json").read_text())
                         ["source_sha256"], text)
        # A page change makes the stored firmware outdated.
        self.set_pages({"pages": self.pages(items=("sog", "awa"))})
        state = editor.show(self.ctx(runner=self.runner), "helm")["readiness"]["firmware"]
        self.assertEqual(state["status"], "outdated")
        self.assertIn("configuration", state["reasons"][0])

    def test_show_firmware_outdated_when_credentials_changed(self):
        files = render.collect(self.ctx(), "helm").files
        self.firmware_manifest(files)
        lifecycle.rotate(self.ctx(), "helm")
        state = editor.show(self.ctx(runner=self.runner), "helm")["readiness"]["firmware"]
        self.assertEqual(state["status"], "outdated")
        self.assertIn("credentials", " ".join(state["reasons"]))

    def test_show_last_flash_passes_only_known_fields(self):
        files = render.collect(self.ctx(), "helm").files
        base = self.firmware_manifest(files)
        sha = "a" * 64
        entry = {"method": "ota", "target": "192.0.2.77", "image_sha256": sha,
                 "source_sha256": "b" * 64, "secrets_sha256": "c" * 64, "at": "2026-01-02T00:00:00Z",
                 "version": "1.2.3", "password": "hunter2-secret"}
        (base / "last-flash.json").write_text(json.dumps({"schema": 1, "current": entry, "previous": None}))
        flash = editor.show(self.ctx(runner=self.runner), "helm")["readiness"]["last_flash"]
        self.assertEqual(flash, {"ok": True, "method": "ota", "at": "2026-01-02T00:00:00Z", "version": "1.2.3",
                                 "ota_target": "192.0.2.77", "credentials_changed": True,
                                 "matches_settings": False})
        self.assertNotIn("hunter2", json.dumps(flash))
        # a target that is not an IP or host name is never passed on; USB targets never are
        entry["target"] = "http://evil/x?token=1"
        (base / "last-flash.json").write_text(json.dumps({"schema": 1, "current": entry, "previous": None}))
        flash = editor.show(self.ctx(runner=self.runner), "helm")["readiness"]["last_flash"]
        self.assertIsNone(flash["ota_target"])
        entry.update(method="serial", target="/dev/ttyACM0")
        (base / "last-flash.json").write_text(json.dumps({"schema": 1, "current": entry, "previous": None}))
        flash = editor.show(self.ctx(runner=self.runner), "helm")["readiness"]["last_flash"]
        self.assertIsNone(flash["ota_target"])
        self.assertNotIn("ttyACM0", json.dumps(flash))
        (base / "last-flash.json").write_text("not json")
        self.assertIsNone(editor.show(self.ctx(runner=self.runner), "helm")["readiness"]["last_flash"])

    def test_show_unknown_display_and_cli_exit_codes(self):
        code, out, _ = self.run_cli("show", "ghost", "--json", "--allow-non-root")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["ok"], False)
        code, out, _ = self.run_cli("show", "helm", "--allow-non-root")     # text form
        self.assertEqual(code, 0)
        self.assertIn("Revision:", out)

    # --- set-pages ---

    def test_set_pages_writes_validates_backs_up_and_reports(self):
        before = self.config.read_text()
        result = self.set_pages({"pages": self.pages("Wind", ("awa", "sog")), "theme": "ocean"})
        self.assertEqual((result["ok"], result["changed"]), (True, True))
        config = self.cfg()
        self.assertEqual(config["displays"]["helm"]["pages"][0]["items"], ["awa", "sog"])
        self.assertEqual(config["displays"]["helm"]["ui"]["theme"], "ocean")
        self.assertEqual(result["revision"], self.revision())
        self.assertEqual(result["rebuild_needed"], ["helm"])
        self.assertEqual(schema.validate_config(config, schema.load_registry()), [])
        backup = self.backups / f"displays-{result['backup']}.json"
        self.assertEqual(backup.read_text(), before)
        self.assertEqual(oct(backup.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(self.backups.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct(self.config.stat().st_mode & 0o777), "0o644")

    def test_theme_classic_removes_the_key_like_set_theme(self):
        self.set_pages({"pages": self.pages(), "theme": "daylight"})
        self.set_pages({"pages": self.pages(), "theme": "classic"})
        self.assertNotIn("ui", self.cfg()["displays"]["helm"])

    def test_new_instruments_and_edits(self):
        new = {"depth_b": {"path": "environment.depth.belowKeel", "label": "Keel", "convert": "m->ft", "unit": "ft",
                           "precision": 1, "stale_after_s": 10}}
        result = self.set_pages({"pages": self.pages(items=("sog", "depth_b")), "new_instruments": new,
                                 "instrument_edits": {"sog": {"precision": 2, "icon": "speed", "min_interval_ms": None}}})
        config = self.cfg()
        self.assertEqual(config["instruments"]["depth_b"]["path"], "environment.depth.belowKeel")
        self.assertEqual(config["instruments"]["sog"]["precision"], 2)
        self.assertNotIn("min_interval_ms", config["instruments"]["sog"])
        self.assertEqual(result["added_instruments"], ["depth_b"])
        self.assertEqual(result["new_alarm_sources"], [])

    def test_edits_cannot_touch_the_path_or_unknown_fields(self):
        for edit in ({"path": "navigation.position"}, {"bogus": 1}):
            with self.assertRaises(editor.EditError) as caught:
                self.set_pages({"pages": self.pages(), "instrument_edits": {"sog": edit}})
            self.assertEqual(caught.exception.code, 2)
        with self.assertRaises(editor.EditError):
            self.set_pages({"pages": self.pages(), "instrument_edits": {"ghost": {"precision": 1}}})
        with self.assertRaises(editor.EditError):   # an existing id with another definition is not overwritten
            self.set_pages({"pages": self.pages(), "new_instruments": {"sog": {
                "path": "navigation.speedThroughWater", "label": "STW", "convert": "none", "unit": "",
                "precision": 1, "stale_after_s": 10}}})

    def test_adding_thresholds_reports_new_alarm_sources(self):
        result = self.set_pages({"pages": self.pages(), "instrument_edits": {
            "depth": {"thresholds": {"alarm_below": 4, "warn_below": 8}}}})
        self.assertEqual(result["new_alarm_sources"], ["depth"])

    def test_new_notifications(self):
        note = {"anchor": {"path": "notifications.navigation.anchor", "label": "Anchor", "absent": "ok",
                           "alarm": False}}
        result = self.set_pages({"pages": [{"title": "S", "layout": "status-grid", "items": ["sog", "anchor"]}],
                                 "new_notifications": note})
        self.assertEqual(result["added_notifications"], ["anchor"])
        self.assertEqual(self.cfg()["notifications"]["anchor"]["alarm"], False)

    def test_validation_errors_are_a_json_list_with_exit_2_and_nothing_changes(self):
        before = self.config.read_text()
        cases = [
            {"pages": self.pages(items=("sog", "nope"))},
            {"pages": self.pages(items=("sog",), layout="tank-bars")},         # no min/max
            {"pages": self.pages(layout="warp")},
            {"pages": []},
            {"pages": self.pages(), "theme": "neon"},
            {"pages": self.pages(title="✓ tick")},   # nothing wrong with a title without a header... see glyph test
        ]
        for payload in cases[:5]:
            code, out, err = self.run_stdin(json.dumps(payload), "set-pages", "helm", "--if-revision",
                                            self.revision(), "--stdin", "--allow-non-root")
            self.assertEqual(code, 2, payload)
            body = json.loads(out)
            self.assertEqual((body["ok"], body["code"]), (False, 2))
            self.assertIsInstance(body["errors"], list)
            self.assertTrue(body["errors"])
            self.assertEqual(self.config.read_text(), before)
        self.assertFalse(self.backups.exists())

    def test_glyph_errors_are_reported(self):
        new = {"x": {"path": "navigation.speedOverGround", "label": "Spd✓", "convert": "none", "unit": "",
                     "precision": 1, "stale_after_s": 10}}
        with self.assertRaises(editor.EditError) as caught:
            self.set_pages({"pages": self.pages(items=("x",)), "new_instruments": new})
        self.assertIn("glyph", " ".join(caught.exception.errors))
        self.assertFalse(self.backups.exists())

    def test_bad_requests(self):
        for text in ("not json", "[]", '{"pages": 1}', '{"pages": [], "bogus": 1}', '{"pages": [], "pages": []}',
                     "{}", "x" * (editor.STDIN_MAX_BYTES + 1)):
            with self.assertRaises(editor.EditError, msg=text[:20]) as caught:
                editor.set_pages(self.ctx(), "helm", self.revision(), text)
            self.assertEqual(caught.exception.code, 2)
        for rev in ("", "abc", "G" * 64, "A" * 64):
            with self.assertRaises(editor.EditError):
                editor.set_pages(self.ctx(), "helm", rev, json.dumps({"pages": self.pages()}))

    def test_wrong_revision_exits_3_and_changes_nothing(self):
        before = self.config.read_text()
        code, out, err = self.run_stdin(json.dumps({"pages": self.pages("Other")}), "set-pages", "helm",
                                        "--if-revision", "0" * 64, "--stdin", "--allow-non-root")
        self.assertEqual(code, 3)
        body = json.loads(out)
        self.assertEqual((body["ok"], body["code"], body["current_revision"]), (False, 3, self.revision()))
        self.assertEqual(self.config.read_text(), before)
        self.assertFalse(self.backups.exists())

    def test_set_pages_cli_success_and_required_flags(self):
        code, out, err = self.run_stdin(json.dumps({"pages": self.pages("Cli")}), "set-pages", "helm",
                                        "--if-revision", self.revision(), "--stdin", "--allow-non-root")
        self.assertEqual((code, err), (0, ""), err)
        self.assertEqual(json.loads(out)["revision"], self.revision())
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                displayctl.main(["set-pages", "helm", "--stdin"])
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                displayctl.main(["set-pages", "helm", "--if-revision", "0" * 64])

    def test_a_running_build_makes_set_pages_refuse_with_exit_4(self):
        self.make_build_tree("helm")
        before = self.config.read_text()
        with render.build_lock(self.ctx(), "helm"):
            code, out, err = self.run_stdin(json.dumps({"pages": self.pages("Busy")}), "set-pages", "helm",
                                            "--if-revision", self.revision(), "--stdin", "--allow-non-root")
        self.assertEqual(code, 4)
        self.assertIn("build/flash for helm is running", json.loads(out)["errors"][0])
        self.assertEqual(self.config.read_text(), before)
        self.assertFalse(self.backups.exists())
        self.set_pages({"pages": self.pages("Free")})       # released: the same call now works

    def test_set_pages_does_not_wait_for_the_build_lock(self):
        import time
        self.make_build_tree("helm")
        with render.build_lock(self.ctx(), "helm"):
            started = time.monotonic()
            with self.assertRaises(editor.EditError) as caught:
                self.set_pages({"pages": self.pages("Busy")}, ctx=self.ctx(build_lock_timeout=30.0))
            self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(caught.exception.code, 4)

    def test_set_pages_waits_for_the_config_lock_then_gives_up(self):
        with lifecycle.ConfigDir(self.ctx()) as held:
            held.lock()
            with self.assertRaises(lifecycle.LifecycleError) as caught:
                self.set_pages({"pages": self.pages("Locked")})
        self.assertIn("another displayctl", str(caught.exception))

    def test_set_pages_needs_root(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("running as root")
        with self.assertRaises(lifecycle.LifecycleError):
            self.set_pages({"pages": self.pages()}, ctx=self.ctx(require_root=True))

    def test_unchanged_request_is_a_noop(self):
        current = self.cfg()["displays"]["helm"]["pages"]
        result = self.set_pages({"pages": current})
        self.assertEqual((result["changed"], result["backup"]), (False, None))
        self.assertFalse(self.backups.exists())
        self.assertEqual(self.restarts(), [])

    # --- bridge restart ---

    def test_bridge_restarts_only_when_what_it_reads_changes(self):
        self.set_pages({"pages": self.pages("A")})
        self.set_pages({"pages": self.pages("B", ("awa",)), "theme": "ocean"})
        self.assertEqual(self.restarts(), [])
        new = {"keel": {"path": "environment.depth.belowKeel", "label": "Keel", "convert": "none", "unit": "m",
                        "precision": 1, "stale_after_s": 10}}
        result = self.set_pages({"pages": self.pages("C", ("keel",)), "new_instruments": new})
        self.assertEqual(self.restarts(), [["systemctl", "try-restart", "vesselstack-displays-bridge.service"]])
        self.assertEqual(result["bridge_restart"], "restarted")
        self.set_pages({"pages": self.pages("C", ("keel",)), "instrument_edits": {"keel": {"label": "Keel 2"}}})
        self.assertEqual(len(self.restarts()), 2)
        self.set_pages({"pages": self.pages("D", ("keel",))})
        self.assertEqual(len(self.restarts()), 2)

    def test_no_restart_when_displays_are_disabled_and_failures_are_reported(self):
        new = {"keel": {"path": "environment.depth.belowKeel", "label": "Keel", "convert": "none", "unit": "m",
                        "precision": 1, "stale_after_s": 10}}
        result = self.set_pages({"pages": self.pages(items=("keel",)), "new_instruments": new},
                                ctx=self.ctx(enabled="false"))
        self.assertEqual(result["bridge_restart"], "skipped_displays_disabled")
        self.assertEqual(self.restarts(), [])
        failing = mock.Mock(return_value=subprocess.CompletedProcess([], 5, "", "boom"))
        result = self.set_pages({"pages": self.pages("T", ("keel",)),
                                 "instrument_edits": {"keel": {"label": "K3"}}}, ctx=self.ctx(runner=failing))
        self.assertTrue(result["bridge_restart"].startswith("failed"))
        self.assertTrue(result["ok"])
        self.assertEqual(self.cfg()["instruments"]["keel"]["label"], "K3")

    # --- backups, history, restore ---

    def test_backups_rotate_at_twenty_with_unique_stamps(self):
        fixed = mock.Mock(return_value=editor._now().replace(year=2026, month=1, day=1, hour=0, minute=0, second=0))
        with mock.patch.object(editor, "_now", fixed):
            for index in range(25):
                self.set_pages({"pages": self.pages(f"P{index}")})
        names = self.stamps()
        self.assertEqual(len(names), 20)
        self.assertEqual(names[0], "displays-20260101T000000Z-10.json")      # -2 .. -5 and the first are gone
        self.assertIn("displays-20260101T000000Z-25.json", names)
        self.assertNotIn("displays-20260101T000000Z.json", names)
        self.assertTrue(all(oct((self.backups / n).stat().st_mode & 0o777) == "0o600" for n in names))

    def test_history_lists_newest_first_with_revisions(self):
        stamps = iter(["20260101T000001Z", "20260101T000002Z", "20260101T000003Z"])
        clock = mock.Mock(side_effect=lambda: __import__("datetime").datetime.strptime(
            next(stamps), "%Y%m%dT%H%M%SZ").replace(tzinfo=__import__("datetime").timezone.utc))
        texts = []
        with mock.patch.object(editor, "_now", clock):
            for index in range(3):
                texts.append(self.config.read_text())
                self.set_pages({"pages": self.pages(f"H{index}")})
        info = editor.history(self.ctx())
        self.assertEqual([b["stamp"] for b in info["backups"]],
                         ["20260101T000003Z", "20260101T000002Z", "20260101T000001Z"])
        self.assertEqual([b["revision"] for b in info["backups"]], [editor.revision_of(t) for t in reversed(texts)])
        self.assertEqual(info["backups"][0]["size"], len(texts[-1].encode()))
        self.assertEqual(info["current_revision"], self.revision())
        code, out, _ = self.run_cli("history", "--json", "--allow-non-root")
        self.assertEqual((code, json.loads(out)["keep"]), (0, 20))
        code, out, _ = self.run_cli("history", "--allow-non-root")
        self.assertIn("20260101T000003Z", out)

    def test_history_without_backups_is_empty_and_creates_nothing(self):
        self.assertEqual(editor.history(self.ctx())["backups"], [])
        self.assertFalse(self.backups.exists())

    def test_restore_puts_the_exact_backup_back_after_backing_up_the_current_file(self):
        original = self.config.read_text()
        first = self.set_pages({"pages": self.pages("One")})
        self.set_pages({"pages": self.pages("Two", ("awa",))})
        changed = self.config.read_text()
        result = editor.restore_config(self.ctx(), first["backup"])
        self.assertEqual(self.config.read_text(), original)
        self.assertEqual(result["revision"], editor.revision_of(original))
        self.assertEqual(result["previous_revision"], editor.revision_of(changed))
        self.assertEqual((self.backups / f"displays-{result['backup']}.json").read_text(), changed)
        self.assertEqual(result["rebuild_needed"], ["helm"])

    def test_restore_cli_and_revision_guard(self):
        first = self.set_pages({"pages": self.pages("One")})
        code, out, _ = self.run_cli("restore-config", first["backup"], "--if-revision", "0" * 64, "--allow-non-root")
        self.assertEqual((code, json.loads(out)["code"]), (3, 3))
        code, out, err = self.run_cli("restore-config", first["backup"], "--if-revision", self.revision(),
                                      "--allow-non-root")
        self.assertEqual((code, json.loads(out)["ok"]), (0, True), err)

    def test_restore_validates_the_stamp_strictly(self):
        self.set_pages({"pages": self.pages("One")})
        outside = self.root / "config" / "displays-evil.json"
        outside.write_text(self.config.read_text())
        for stamp in ("../displays", "../../etc/passwd", "20260101T000000Z/../x", "20260101T000000", "", "latest",
                      "20260101T000000Z-", "20260101T000000Zx", "2026-01-01", "20260101T000000Z.json", " 20260101T000000Z",
                      "20260101T000000Z\n", "..", ".", "/etc/passwd", "evil"):
            code, out, _ = self.run_cli("restore-config", stamp, "--allow-non-root")
            self.assertEqual(code, 2, repr(stamp))
            self.assertEqual(json.loads(out)["code"], 2)
        code, out, _ = self.run_cli("restore-config", "20990101T000000Z", "--allow-non-root")
        self.assertEqual(code, 2)
        self.assertIn("no backup", json.loads(out)["errors"][0])

    def test_restore_refuses_a_backup_that_is_no_longer_valid(self):
        self.set_pages({"pages": self.pages("One")})
        bad = json.loads(self.config.read_text())
        bad["displays"]["helm"]["board"] = "no-such-board"
        (self.backups / "displays-20260101T000000Z.json").write_text(json.dumps(bad))
        (self.backups / "displays-20260101T000001Z.json").write_text("{not json")
        before = self.config.read_text()
        for stamp in ("20260101T000000Z", "20260101T000001Z"):
            code, out, _ = self.run_cli("restore-config", stamp, "--allow-non-root")
            self.assertEqual(code, 2, stamp)
            self.assertEqual(self.config.read_text(), before)

    def test_restore_waits_for_no_build_and_restarts_the_bridge_for_instrument_changes(self):
        base = self.set_pages({"pages": self.pages("One")})
        new = {"keel": {"path": "environment.depth.belowKeel", "label": "Keel", "convert": "none", "unit": "m",
                        "precision": 1, "stale_after_s": 10}}
        self.set_pages({"pages": self.pages("Two", ("keel",)), "new_instruments": new})
        self.runner.calls.clear()
        self.make_build_tree("helm")
        with render.build_lock(self.ctx(), "helm"):
            with self.assertRaises(editor.EditError) as caught:
                editor.restore_config(self.ctx(), base["backup"])
        self.assertEqual(caught.exception.code, 4)
        self.assertEqual(self.restarts(), [])
        result = editor.restore_config(self.ctx(), base["backup"])
        self.assertNotIn("keel", self.cfg()["instruments"])
        self.assertEqual(result["bridge_restart"], "restarted")
        self.assertEqual(len(self.restarts()), 1)

    def _two_display_texts(self):
        one = self.config.read_text()
        data = json.loads(one)
        data["displays"]["deck"] = json.loads(json.dumps(data["displays"]["helm"]))
        return one, json.dumps(data, indent=2) + "\n"

    def test_restore_refuses_a_backup_with_an_extra_display(self):
        one, two = self._two_display_texts()
        self.backups.mkdir(exist_ok=True)
        (self.backups / "displays-20260101T000000Z.json").write_text(two)
        before, stamps = self.config.read_text(), self.stamps()
        self.runner.calls.clear()
        with mock.patch.object(lifecycle, "_sync") as sync:
            with self.assertRaises(editor.EditError) as caught:
                editor.restore_config(self.ctx(), "20260101T000000Z")
            code, out, _ = self.run_cli("restore-config", "20260101T000000Z", "--allow-non-root")
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("adds display(s) deck", caught.exception.errors[0])
        self.assertIn("display add", caught.exception.errors[0])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["ok"], False)
        self.assertEqual(self.config.read_text(), before)
        self.assertEqual(self.stamps(), stamps)
        sync.assert_not_called()
        self.assertEqual(self.restarts(), [])

    def test_restore_refuses_a_backup_with_a_missing_display(self):
        one, two = self._two_display_texts()
        self.config.write_text(two)
        self.backups.mkdir(exist_ok=True)
        (self.backups / "displays-20260101T000000Z.json").write_text(one)
        stamps = self.stamps()
        with self.assertRaises(editor.EditError) as caught:
            editor.restore_config(self.ctx(), "20260101T000000Z")
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("removes display(s) deck", caught.exception.errors[0])
        self.assertIn("display remove", caught.exception.errors[0])
        self.assertEqual(self.config.read_text(), two)
        self.assertEqual(self.stamps(), stamps)

    def test_restore_of_the_same_display_set_needs_no_mqtt_sync(self):
        first = self.set_pages({"pages": self.pages("One")})
        self.set_pages({"pages": self.pages("Two", ("awa",))})
        with mock.patch.object(lifecycle, "_sync") as sync:
            result = editor.restore_config(self.ctx(), first["backup"])
        self.assertEqual(result["mqtt_sync"], "not_needed")
        sync.assert_not_called()

    def test_restore_syncs_mqtt_when_an_acl_input_differs(self):
        first = self.set_pages({"pages": self.pages("One")})
        self.set_pages({"pages": self.pages("Two", ("awa",))})
        with mock.patch.object(editor, "acl_inputs", side_effect=[["helm"], ["helm", "x"]]), \
                mock.patch.object(lifecycle, "_sync") as sync:
            result = editor.restore_config(self.ctx(), first["backup"])
        sync.assert_called_once()
        self.assertEqual(result["mqtt_sync"], "synced")
        newest = result["backup"]   # the "Two" file the first restore replaced
        with mock.patch.object(editor, "acl_inputs", side_effect=[None, ["helm"]]), \
                mock.patch.object(lifecycle, "_sync", side_effect=lifecycle.LifecycleError("boom")):
            result = editor.restore_config(self.ctx(), newest)
        self.assertEqual(result["mqtt_sync"], "failed: boom")

    def test_the_acl_depends_only_on_the_display_ids_and_set_pages_cannot_change_them(self):
        config = self.cfg()
        self.assertEqual(editor.acl_inputs(config), ["helm"])
        other = json.loads(json.dumps(config))
        other["displays"]["helm"]["pages"] = self.pages("Else")
        other["displays"]["helm"]["name"] = "Renamed"
        other["instruments"]["x"] = {"path": "a.b", "label": "X", "convert": "none", "unit": "m"}
        other["ha"] = {"anything": 1}
        self.assertEqual(editor.acl_inputs(other), ["helm"])
        new = {"keel": {"path": "environment.depth.belowKeel", "label": "Keel", "convert": "none", "unit": "m",
                        "precision": 1, "stale_after_s": 10}}
        with mock.patch.object(lifecycle, "_sync") as sync:
            self.set_pages({"pages": self.pages("Two", ("keel",)), "new_instruments": new, "theme": "ocean"})
            with self.assertRaises(editor.EditError):
                self.set_pages({"pages": self.pages("Two"), "displays": {"deck": {}}})
            with self.assertRaises(lifecycle.LifecycleError):
                self.set_pages({"pages": self.pages("Two")}, display="deck")
        self.assertEqual(editor.acl_inputs(self.cfg()), ["helm"])
        sync.assert_not_called()

    # --- serial ports ---

    def test_serial_ports_lists_only_devices_flash_would_accept(self):
        by_id = Path(self._tmp.name) / "by-id"
        by_id.mkdir()
        for name in ("usb-Fake_Board_0001-if00", "usb-Other-if00", "bad-link"):
            (by_id / name).write_text("")
        import stat as stat_module

        def lstat(path):
            mode = stat_module.S_IFLNK if "by-id" in path else stat_module.S_IFCHR
            return os.stat_result((mode, 0, 0, 1, 0, 0, 0, 0, 0, 0))

        def realpath(path):
            return "/dev/ttyACM0" if "Fake_Board" in path else "/dev/sda" if "bad-link" in path else "/dev/ttyUSB0"

        def listdir(path):
            return ["ttyACM0", "ttyUSB1", "ttyS0"] if path == Path("/dev") else os.listdir(path)

        info = editor.serial_ports(self.ctx(), by_id=by_id, lstat=lstat, realpath=realpath, listdir=listdir)
        self.assertEqual(info["ports"], [
            {"path": "/dev/serial/by-id/usb-Fake_Board_0001-if00", "device": "/dev/ttyACM0"},
            {"path": "/dev/serial/by-id/usb-Other-if00", "device": "/dev/ttyUSB0"},
            {"path": "/dev/ttyUSB1", "device": "/dev/ttyUSB1"},
        ])
        self.assertIsNone(info["error"])

        def denied(path):
            raise PermissionError("permission denied")

        info = editor.serial_ports(self.ctx(), listdir=denied)
        self.assertEqual(info["ports"], [])
        self.assertIn("permission denied", info["error"])
        code, out, _ = self.run_cli("serial-ports", "--json", "--allow-non-root")
        self.assertEqual(code, 0)
        self.assertIn("ports", json.loads(out))

    # --- catalog ---

    def serve(self, **kw):
        server, _thread = fake_signalk.serve_rest(0, **kw)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, f"http://127.0.0.1:{server.server_port}"

    def test_catalog_cli_without_live_data(self):
        code, out, err = self.run_cli("catalog", "--json", "--allow-non-root", BOAT_UNITS="metric")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual((data["schema"], data["units"]), (1, "metric"))
        self.assertEqual(data["live"], {"requested": False, "ok": None, "error": None, "truncated": False,
                                        "leaf_count": 0, "notification_count": 0})
        rows = {r["row_id"]: r for r in data["rows"]}
        self.assertEqual(rows["depth"]["convert"], "none")
        self.assertEqual(rows["sog"]["configured_as"], "sog")          # the example config already has it
        self.assertEqual(rows["sog"]["used_on"], ["helm"])
        self.assertTrue(all(r["live_state"] == "unknown" for r in data["rows"]))
        self.assertEqual(data["groups"][0], "Navigation")
        code, out, _ = self.run_cli("catalog", "--allow-non-root", BOAT_UNITS="")
        self.assertEqual(code, 0)
        self.assertIn("Navigation", out)
        code, out, _ = self.run_cli("catalog", "--json", "--allow-non-root")
        self.assertEqual(json.loads(out)["units"], "us_customary")
        code, out, _ = self.run_cli("catalog", "--json", "--allow-non-root", BOAT_UNITS="nautical")
        self.assertEqual(json.loads(out)["units"], "metric")

    def test_catalog_cli_live_filters_private_data_and_hides_the_token(self):
        server, url = self.serve(token=SK_TOKEN)
        code, out, err = self.run_cli("catalog", "--live", "--json", "--allow-non-root", SIGNALK_URL=url,
                                      DISPLAYS_SIGNALK_TOKEN=SK_TOKEN, BOAT_UNITS="us_customary")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertTrue(data["live"]["ok"])
        self.assertGreater(data["live"]["leaf_count"], 10)
        for secret in (*[getattr(fake_signalk, n) for n in ("FAKE_MMSI", "FAKE_NAME", "FAKE_CALLSIGN", "FAKE_UUID",
                                                           "FAKE_DESTINATION")], "latitude", "longitude", SK_TOKEN):
            self.assertNotIn(secret, out)
            self.assertNotIn(secret, err)
        rows = {r["row_id"]: r for r in data["rows"]}
        self.assertEqual(rows["depth"]["convert"], "m->ft")
        self.assertEqual(rows["depth"]["live_state"], "live")
        self.assertEqual(rows["batt_voltage:house"]["live_state"], "live")
        self.assertIn("Uncatalogued", data["groups"])
        self.assertEqual(server.handler_class.seen[-1][1], f"Bearer {SK_TOKEN}")

    def test_catalog_cli_with_an_unreachable_server_reports_the_error(self):
        server, url = self.serve()
        server.shutdown()
        server.server_close()
        code, out, _ = self.run_cli("catalog", "--live", "--json", "--allow-non-root", SIGNALK_URL=url)
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["live"]["ok"], False)
        self.assertTrue(data["live"]["error"])
        self.assertEqual(len(data["rows"]), 42)

    def test_catalog_only_asks_the_configured_server(self):
        server, url = self.serve()
        other, other_url = self.serve()
        self.run_cli("catalog", "--live", "--json", "--allow-non-root", SIGNALK_URL=url)
        self.assertEqual(len(server.handler_class.seen), 1)
        self.assertEqual(other.handler_class.seen, [])
        code, out, _ = self.run_cli("catalog", "--live", "--json", "--allow-non-root", SIGNALK_URL="")
        self.assertEqual(json.loads(out)["live"]["ok"], False)

    def test_picking_catalog_rows_never_adds_thresholds_or_alarm_sources(self):
        server, url = self.serve()
        code, out, _ = self.run_cli("catalog", "--live", "--json", "--allow-non-root", SIGNALK_URL=url,
                                    BOAT_UNITS="metric")
        rows = {r["row_id"]: r for r in json.loads(out)["rows"]}
        picks = [rows[k] for k in ("stw", "depth_keel", "tank_fuel:0", "batt_soc:house", "anchor")]
        new_instruments = {r["proposed_id"]: r["definition"] for r in picks if r["kind"] == "instrument"}
        new_notifications = {r["proposed_id"]: r["definition"] for r in picks if r["kind"] == "notification"}
        items = [r["proposed_id"] for r in picks]
        result = self.set_pages({"pages": [{"title": "Mix", "layout": "status-grid", "items": items}],
                                 "new_instruments": new_instruments, "new_notifications": new_notifications})
        config = self.cfg()
        self.assertTrue(all("thresholds" not in i for i in config["instruments"].values()))
        self.assertFalse(schema.counts_for_alarm_summary(config))
        self.assertEqual(result["new_alarm_sources"], [])
        self.assertEqual(result["added_notifications"], ["anchor"])

    # --- plumbing ---

    def test_vesselstackctl_passes_the_catalog_environment(self):
        text = (ROOT / "scripts" / "vesselstackctl").read_text()
        body = text[text.index("display_command() {"):text.index("memory_index_helper() {")]
        for name in ("SIGNALK_URL", "DISPLAYS_SIGNALK_TOKEN", "BOAT_UNITS", "VESSELSTACK_DATA_BIND_ADDRESS"):
            self.assertIn(f'{name}="${{{name}', body)
        for command in ("catalog", "show", "set-pages", "history", "restore-config", "serial-ports"):
            self.assertIn(command, text[:text.index("display_command() {")])

    def test_backups_and_last_flash_are_never_committed(self):
        self.assertIn("displays-backups/", (ROOT / ".gitignore").read_text())
        spec = importlib.util.spec_from_file_location("check_public_tree", ROOT / "scripts" / "check-public-tree.py")
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        for path in ("config/displays-backups/displays-20260101T000000Z.json",
                     "displays-backups/displays-20260101T000000Z-2.json",
                     "build/displays/helm/last-flash.json", "last-flash.json"):
            self.assertTrue(checker.findings(path, b"{}"), path)
        self.assertFalse(checker.findings("displays/catalog.json", b"{}"))

    def test_bridge_view_follows_what_the_bridge_reads(self):
        config = self.cfg()
        view = editor._bridge_view(config)
        pages_only = json.loads(json.dumps(config))
        pages_only["displays"]["helm"]["pages"][0]["title"] = "Other"
        pages_only["displays"]["helm"]["ui"] = {"theme": "ocean"}
        self.assertEqual(editor._bridge_view(pages_only), view)
        clock = json.loads(json.dumps(config))
        clock["displays"]["helm"]["ui"] = {"header": True}
        self.assertNotEqual(editor._bridge_view(clock), view)       # the bridge publishes the clock only then
        inst = json.loads(json.dumps(config))
        inst["instruments"]["sog"]["precision"] = 3
        self.assertNotEqual(editor._bridge_view(inst), view)

    def test_restore_restarts_the_bridge_when_only_the_clock_need_changes(self):
        config = self.cfg()
        config["displays"]["helm"]["ui"] = {"header": True}
        self.config.write_text(json.dumps(config, indent=2) + "\n")
        first = self.set_pages({"pages": self.pages("Hdr")})     # the backup it makes still has the header
        config = self.cfg()
        del config["displays"]["helm"]["ui"]
        self.config.write_text(json.dumps(config, indent=2) + "\n")
        self.runner.calls.clear()
        result = editor.restore_config(self.ctx(), first["backup"])
        self.assertEqual(result["bridge_restart"], "restarted")
