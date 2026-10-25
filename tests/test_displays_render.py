#!/usr/bin/env python3
"""Tests for the ESPHome project generator (displays/render.py)."""

from __future__ import annotations

import copy
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "displays"))

import displayctl  # noqa: E402
import lifecycle  # noqa: E402
import mqtt_auth  # noqa: E402
import render  # noqa: E402
import schema  # noqa: E402

GOLDEN = ROOT / "tests" / "fixtures" / "displays" / "golden"
EXAMPLE = json.loads((ROOT / "displays" / "displays.example.json").read_text())
REGISTRY = schema.load_registry()
BOARD = "waveshare-esp32-s3-touch-lcd-2.8c"
WIFI = {"ssid": "boat-wifi", "password": "wifi-pass-123"}
MQTT_PW = "mqtt-secret-value"


def multipage(cfg):
    cfg["displays"]["helm"]["pages"] = [
        {"title": "Nav", "layout": "grid-2x2", "items": ["sog", "depth", "awa"]},
        {"title": "Big", "layout": "single", "items": ["house_v"]},
        {"title": "Two", "layout": "grid-2x2", "items": ["house_v", "sog"]},
    ]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "root"
        (self.root / "config").mkdir(parents=True)
        self.messages: list = []
        self.set_config(None)

    def set_config(self, mutate, wifi=WIFI, display_secrets=None):
        cfg = copy.deepcopy(EXAMPLE)
        if mutate:
            mutate(cfg)
        (self.root / "config" / "displays.json").write_text(json.dumps(cfg, indent=2))
        secrets = {"schema": 1, "bridge": {}, "displays": {
            d: {"mqtt_password": MQTT_PW} for d in cfg["displays"]}}
        if display_secrets is not None:
            secrets["displays"] = display_secrets
        if wifi:
            secrets["wifi"] = wifi
        self.secrets_path = self.root / "config" / mqtt_auth.SECRETS_NAME
        self.secrets_path.write_text(json.dumps(secrets))
        return cfg

    def ctx(self, **kw):
        values = dict(root=self.root, require_root=False, lock_timeout=0.3, build_lock_timeout=0.3,
                      out=self.messages.append)
        values.update(kw)
        return lifecycle.Context(**values)

    def collect(self, display="helm", **kw):
        return render.collect(self.ctx(), display, **kw)

    def device(self, display="helm", **kw):
        return self.collect(display, **kw).files[f"{display}/{display}.yaml"].decode()

    def stored(self):
        return json.loads(self.secrets_path.read_text())


class GoldenAndDeterminism(Base):
    def check_golden(self, name, text):
        path = GOLDEN / name
        if os.environ.get("UPDATE_GOLDEN"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.assertEqual(text, path.read_text(encoding="utf-8"),
                         f"{name} differs; run with UPDATE_GOLDEN=1 to regenerate and review the diff")

    def test_example_matches_golden(self):
        self.check_golden("helm.yaml", self.device(allow_missing_wifi=True))

    def test_multipage_with_api_matches_golden(self):
        def mutate(cfg):
            multipage(cfg)
            cfg["displays"]["helm"]["home_assistant_api"] = True
            cfg["heartbeat_s"] = 7
            for inst in cfg["instruments"].values():
                inst["stale_after_s"] = 14
        self.set_config(mutate)
        self.check_golden("helm-multipage-api.yaml", self.device())

    def test_device_file_is_deterministic(self):
        self.assertEqual(self.device(), self.device())

    def test_repeated_collect_keeps_generated_secrets(self):
        first = self.stored()
        self.collect()
        after_first = self.stored()["displays"]["helm"]
        self.collect()
        self.assertEqual(self.stored()["displays"]["helm"], after_first)
        self.assertNotIn("ota_password", first["displays"]["helm"])
        self.assertTrue(after_first["ota_password"])
        key = after_first["api_encryption_key"]
        self.assertEqual(len(__import__("base64").b64decode(key)), 32)


class Secrets(Base):
    def test_secrets_only_in_secrets_yaml(self):
        self.set_config(lambda c: c["displays"]["helm"].update(home_assistant_api=True))
        rendered = self.collect()
        entry = self.stored()["displays"]["helm"]
        values = [WIFI["password"], MQTT_PW, entry["ota_password"], entry["api_encryption_key"]]
        for path, data in rendered.files.items():
            text = data.decode("utf-8", "ignore") if not path.endswith(".ttf") else ""
            for value in values:
                if path.endswith("/secrets.yaml"):
                    self.assertIn(value, text)
                else:
                    self.assertNotIn(value, text, path)
        secrets_text = rendered.files["helm/secrets.yaml"].decode()
        for key in ("wifi_ssid", "wifi_password", "mqtt_host", "mqtt_password", "ota_password",
                    "api_encryption_key"):
            self.assertRegex(secrets_text, rf"(?m)^{key}: !literal \"")

    def test_api_key_only_when_api_enabled(self):
        rendered = self.collect()
        self.assertNotIn("api_encryption_key", rendered.files["helm/secrets.yaml"].decode())
        self.assertNotIn("helm/app/api.yaml", rendered.files)
        self.assertNotIn("api.yaml", rendered.files["helm/helm.yaml"].decode())
        self.set_config(lambda c: c["displays"]["helm"].update(home_assistant_api=True))
        rendered = self.collect()
        self.assertIn("helm/app/api.yaml", rendered.files)
        self.assertIn("api: !include app/api.yaml", rendered.files["helm/helm.yaml"].decode())
        self.assertIn("api_encryption_key", rendered.files["helm/secrets.yaml"].decode())

    def test_secret_values_are_escaped(self):
        self.set_config(None, wifi={"ssid": 'a"b\\c$d${e}', "password": "p$$w<% x %>\\\""})
        text = self.collect().files["helm/secrets.yaml"].decode()
        self.assertIn('wifi_ssid: !literal "a\\"b\\\\c$d${e}"', text)
        self.assertIn('wifi_password: !literal "p$$w<% x %>\\\\\\""', text)

    def test_missing_wifi_fails_unless_allowed(self):
        self.set_config(None, wifi=None)
        with self.assertRaises(render.RenderError) as caught:
            self.collect()
        self.assertIn("display wifi", str(caught.exception))
        self.assertEqual(caught.exception.code, 2)
        rendered = self.collect(allow_missing_wifi=True)
        self.assertTrue(rendered.warnings and "PLACEHOLDER" in rendered.warnings[0])
        self.assertIn("placeholder-ssid", rendered.files["helm/secrets.yaml"].decode())

    def test_missing_mqtt_password_fails(self):
        self.set_config(None, display_secrets={})
        with self.assertRaises(render.RenderError) as caught:
            self.collect()
        self.assertIn("sync-mqtt", str(caught.exception))

    def test_rotate_rotates_ota_and_api_keys(self):
        self.collect()
        before = self.stored()["displays"]["helm"]
        runner = lambda argv: subprocess.CompletedProcess(argv, 0, "", "")  # noqa: E731
        lifecycle.rotate(self.ctx(runner=runner, env={"DISPLAYS_ENABLE": "false"}), "helm")
        after = self.stored()["displays"]["helm"]
        for key in ("mqtt_password", "ota_password", "api_encryption_key"):
            self.assertNotEqual(after[key], before[key], key)
        self.assertEqual(set(lifecycle.GENERATED_SECRETS),
                         {"mqtt_password", "ota_password", "api_encryption_key"})

    def test_mdns_host_warns(self):
        warnings = self.collect().warnings
        self.assertTrue(any(".local" in w and "mDNS" in w for w in warnings))
        self.set_config(lambda c: c.update(mqtt_host="192.0.2.7"))
        self.assertFalse(any(".local" in w for w in self.collect().warnings))


class Content(Base):
    def test_yq_round_trips_through_json(self):
        for text in ['plain', 'q"uote', 'back\\slash', '$x ${y} <% z %>', '#:', "tab\tend",
                     "°C", "a\nb"]:
            self.assertEqual(json.loads(render.yq(text)), text)
        self.assertTrue(render.yq("°").isascii())

    def test_hostile_label_and_unit_are_literal_scalars(self):
        hostile_label, hostile_unit = '$x ${y} "q" \\ #:', "${z}\\\""

        def mutate(cfg):
            cfg["instruments"]["sog"]["label"] = hostile_label
            cfg["instruments"]["sog"]["unit"] = hostile_unit
        self.set_config(mutate)
        self.assertEqual(schema.validate_config(json.loads(
            (self.root / "config" / "displays.json").read_text()), REGISTRY), [])
        text = self.device()
        self.assertIn(f"text: !literal {render.yq(hostile_label)}", text)
        self.assertIn(f"text: !literal {render.yq(hostile_unit)}", text)
        # Never inside C++: raw user text appears only in label widgets and the glyph list.
        for line in text.splitlines():
            if "${y}" in line or "${z}" in line:
                self.assertTrue(line.lstrip().startswith(("- label:", "glyphs:", "text:")) or "glyphs:" in line,
                                line)
        self.assertNotIn("lambda", "".join(l for l in text.splitlines() if "${y}" in l))

    def test_schema_rejects_newlines_in_labels(self):
        cfg = copy.deepcopy(EXAMPLE)
        cfg["instruments"]["sog"]["label"] = "a\nb"
        cfg["instruments"]["sog"]["unit"] = "k\rn"
        errors = schema.validate_config(cfg, REGISTRY)
        self.assertTrue(any("label" in e for e in errors) and any("unit" in e for e in errors))

    def test_small_font_glyphs_cover_every_used_character(self):
        def mutate(cfg):
            cfg["instruments"]["sog"]["label"] = "Zq!?"
            cfg["instruments"]["depth"]["unit"] = "°F%"
        self.set_config(mutate)
        text = self.device()
        glyphs = re.search(r'id: font_small\n    size: 20\n    bpp: 4\n    glyphs: !literal (".*")', text)
        decoded = json.loads(glyphs.group(1))
        cfg = json.loads((self.root / "config" / "displays.json").read_text())
        needed = {" "}
        for iid in ("sog", "depth", "awa", "house_v"):
            needed.update(cfg["instruments"][iid]["label"] + cfg["instruments"][iid]["unit"])
        self.assertEqual(set(decoded), needed)
        self.assertEqual(len(decoded), len(set(decoded)), "duplicate glyphs fail in ESPHome")
        value_glyphs = re.search(r'id: font_value_grid\n    size: 56\n    bpp: 4\n    glyphs: !literal (".*")', text)
        self.assertEqual(json.loads(value_glyphs.group(1)), "0123456789.- ")

    def test_font_coverage_and_unsupported_glyphs(self):
        covered = render.font_coverage("regular")
        for ch in "0123456789.-+ AZaz°%":
            self.assertIn(ord(ch), covered, ch)
        self.assertEqual(render.unsupported_chars("Kn日"), ["日"])

        def mutate(cfg):
            cfg["instruments"]["sog"]["label"] = "日本"
        self.set_config(mutate)
        with self.assertRaises(render.RenderError) as caught:
            self.collect()
        self.assertIn("instruments.sog.label", str(caught.exception))
        self.assertIn("U+65E5", str(caught.exception))

    def test_validate_command_reports_unsupported_glyphs(self):
        cfg = copy.deepcopy(EXAMPLE)
        cfg["instruments"]["sog"]["unit"] = "¶日"
        path = Path(self._tmp.name) / "bad.json"
        path.write_text(json.dumps(cfg))
        result = subprocess.run([sys.executable, str(ROOT / "displays" / "displayctl.py"),
                                 "validate", str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no glyph", result.stderr)

    def test_staleness_threshold_is_per_tile(self):
        text = self.device()
        self.assertIn("id(ins_sog_seen)) < 20000UL", text)
        self.assertIn("id(ins_depth_seen)) < 20000UL", text)
        self.assertIn("id(ins_house_v_seen)) < 40000UL", text)

        def mutate(cfg):
            cfg["heartbeat_s"] = 7
            for inst in cfg["instruments"].values():
                inst["stale_after_s"] = 14
            cfg["instruments"]["depth"]["stale_after_s"] = 100
        self.set_config(mutate)
        text = self.device()
        self.assertIn("id(ins_sog_seen)) < 28000UL", text)
        self.assertIn("id(ins_depth_seen)) < 114000UL", text)
        self.assertEqual(render.stale_threshold_s({"stale_after_s": 10}, 5), 20)

    def test_tile_blanks_unless_bridge_online_connected_and_ok(self):
        text = self.device()
        self.assertIn("id(bridge_online) && mqtt::global_mqtt_client->is_connected() && id(ins_sog_ok)", text)
        self.assertIn("on_disconnect:\n    - lambda: 'id(bridge_online) = false;'", text)
        self.assertIn('strcmp(st, "ok") == 0', text)
        self.assertNotIn('x["age"]', text)

    def test_subscribes_only_to_used_instruments(self):
        def mutate(cfg):
            cfg["displays"]["helm"]["pages"][0]["items"] = ["sog", "depth"]
        self.set_config(mutate)
        text = self.device()
        self.assertIn("topic: vesselstack/v1/instruments/sog", text)
        self.assertIn("topic: vesselstack/v1/instruments/depth", text)
        self.assertNotIn("instruments/awa", text)
        self.assertNotIn("house_v", text)

    def test_decimals_follow_precision(self):
        text = self.device()
        self.assertRegex(text, r"instruments/sog[\s\S]*?value_accuracy_to_buf\(buf, x\[\"v\"\]\.as<float>\(\), 1\)")
        self.assertRegex(text, r"instruments/awa[\s\S]*?as<float>\(\), 0\)")
        self.assertRegex(text, r"instruments/house_v[\s\S]*?as<float>\(\), 2\)")

    def test_navigation_only_for_multiple_pages(self):
        single = self.device()
        self.assertNotIn("lvgl.page.show", single)
        self.assertNotIn("_tap", single)
        self.set_config(multipage)
        text = self.device()
        self.assertEqual(text.count("lvgl.page.show"), 3)
        for here, there in ((0, 1), (1, 2), (2, 0)):
            self.assertRegex(text, rf"id: p{here}_tap[\s\S]*?id: page_{there}\n")
        self.assertIn("font_value_single", text)
        self.assertIn("font_value_grid", text)

    def test_content_box_comes_from_the_registry(self):
        text = self.device()
        self.assertIn("width: 340\n            height: 340", text)
        self.assertIn("min_version: 2026.10.0", text)
        self.assertIn("device_name: vs-helm", text)
        self.assertIn("mqtt_username: vsdisp-helm", text)

    def test_unsupported_layout_and_hyphen_id(self):
        registry = copy.deepcopy(REGISTRY)
        cfg = copy.deepcopy(EXAMPLE)
        cfg["displays"]["helm"]["pages"][0]["layout"] = "list-6"
        with self.assertRaises(render.RenderError):
            render.render_device_yaml(cfg, registry, "helm", board_package="boards/x.yaml")
        with self.assertRaises(render.RenderError):
            render.firmware_name("helm-")

    def test_single_image_pin_and_version(self):
        image = render.pinned_image()
        self.assertRegex(image, r"^ghcr\.io/esphome/esphome:2026\.10\.0@sha256:[0-9a-f]{64}$")
        self.assertEqual(render.esphome_version(), "2026.10.0")
        self.assertIn(image, self.collect().files["helm/README.txt"].decode())

    def test_registry_esphome_package(self):
        for bad in ("../x.yaml", "boards/../x.yaml", "x.yaml", "boards/x.txt", 5):
            reg = copy.deepcopy(REGISTRY)
            reg["boards"][BOARD]["esphome_package"] = bad
            self.assertTrue(schema.validate_registry(reg), bad)
        reg = copy.deepcopy(REGISTRY)
        del reg["boards"][BOARD]["esphome_package"]
        self.assertTrue(schema.validate_registry(reg))
        for board in REGISTRY["boards"].values():
            self.assertTrue((ROOT / "displays" / "esphome" / board["esphome_package"]).is_file())


class BuildDirectory(Base):
    def build(self, display="helm"):
        return self.root / "build" / "displays" / display

    def test_tree_modes_and_contents(self):
        path = render.render(self.ctx(), "helm", allow_missing_wifi=True, warn=lambda t: None)
        self.assertEqual(path, self.build() / "project")
        self.assertEqual(self.messages[-1], str(path))
        for directory, dirs, files in os.walk(self.root / "build"):
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700, directory)
            for name in files:
                self.assertEqual(stat.S_IMODE(os.stat(Path(directory, name)).st_mode), 0o600, name)
        names = {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()}
        self.assertEqual(names, {
            "helm.yaml", "secrets.yaml", "README.txt", f"boards/{BOARD}.yaml",
            "app/vesselstack.yaml", "fonts/OFL.txt", "fonts/RobotoCondensed-Bold.ttf",
            "fonts/RobotoCondensed-Regular.ttf"})

    def test_rerender_keeps_previous_build(self):
        render.render(self.ctx(), "helm", allow_missing_wifi=True, warn=lambda t: None)
        (self.build() / "project" / "marker").write_text("old")
        render.render(self.ctx(), "helm", allow_missing_wifi=True, warn=lambda t: None)
        self.assertFalse((self.build() / "project" / "marker").exists())
        self.assertEqual((self.build() / "project.prev" / "marker").read_text(), "old")
        self.assertFalse((self.build() / "project.new").exists())

    def test_symlinked_build_directory_is_refused(self):
        target = Path(self._tmp.name) / "elsewhere"
        target.mkdir()
        os.symlink(target, self.root / "build")
        with self.assertRaises(render.RenderError) as caught:
            render.render(self.ctx(), "helm", allow_missing_wifi=True, warn=lambda t: None)
        self.assertIn("symbolic link", str(caught.exception))
        self.assertEqual(list(target.iterdir()), [])

    def test_symlinked_displays_directory_is_refused(self):
        target = Path(self._tmp.name) / "elsewhere"
        target.mkdir()
        (self.root / "build").mkdir(mode=0o700)
        os.symlink(target, self.root / "build" / "displays")
        with self.assertRaises(render.RenderError):
            render.render(self.ctx(), "helm", allow_missing_wifi=True, warn=lambda t: None)
        self.assertEqual(list(target.iterdir()), [])

    def test_symlinked_display_directory_is_refused_not_followed(self):
        target = Path(self._tmp.name) / "victim"
        target.mkdir()
        (target / "keep").write_text("data")
        (self.root / "build" / "displays").mkdir(parents=True, mode=0o700)
        os.symlink(target, self.build())
        with self.assertRaises(render.RenderError) as caught:
            render.render(self.ctx(), "helm", allow_missing_wifi=True, warn=lambda t: None)
        self.assertIn("symbolic link", str(caught.exception))
        self.assertEqual([p.name for p in target.iterdir()], ["keep"])

    def test_planted_project_new_symlink_is_unlinked_not_followed(self):
        target = Path(self._tmp.name) / "victim"
        target.mkdir()
        (target / "keep").write_text("data")
        (self.build()).mkdir(parents=True, mode=0o700)
        os.symlink(target, self.build() / "project.new")  # stale planted temp
        render.render(self.ctx(), "helm", allow_missing_wifi=True, warn=lambda t: None)
        self.assertEqual([p.name for p in target.iterdir()], ["keep"])
        self.assertTrue((self.build() / "project" / "helm.yaml").is_file())

    def test_render_never_touches_the_firmware_directories(self):
        render.render(self.ctx(), "helm", allow_missing_wifi=True, warn=lambda t: None)
        for name in ("firmware", "firmware.prev", "firmware.new"):
            (self.build() / name).mkdir(mode=0o700)
            (self.build() / name / "manifest.json").write_text(name)
        for _ in range(3):
            render.render(self.ctx(), "helm", allow_missing_wifi=True, warn=lambda t: None)
        for name in ("firmware", "firmware.prev", "firmware.new"):
            self.assertEqual((self.build() / name / "manifest.json").read_text(), name)
        # nothing of the firmware ends up inside the mounted project
        self.assertEqual(sorted(p.name for p in (self.build() / "project").iterdir()),
                         ["README.txt", "app", "boards", "fonts", "helm.yaml", "secrets.yaml"])

    def test_old_layout_is_migrated_keeping_firmware(self):
        old = self.build()
        (old / "firmware").mkdir(parents=True, mode=0o700)
        (old / "firmware.prev").mkdir(mode=0o700)
        (old / "firmware" / "manifest.json").write_text("new-build")
        (old / "firmware.prev" / "manifest.json").write_text("older-build")
        for leftover in (old, old.parent / "helm.new", old.parent / "helm.prev"):
            leftover.mkdir(exist_ok=True, mode=0o700)
            (leftover / "secrets.yaml").write_text("wifi_password: old")
        (old / "helm.yaml").write_text("old: project")
        (old / ".esphome").mkdir()
        (old / ".esphome" / "big").write_text("scratch")
        render.render(self.ctx(), "helm", allow_missing_wifi=True, warn=lambda t: None)
        self.assertEqual((old / "firmware" / "manifest.json").read_text(), "new-build")
        self.assertEqual((old / "firmware.prev" / "manifest.json").read_text(), "older-build")
        self.assertFalse((old.parent / "helm.new").exists())
        self.assertFalse((old.parent / "helm.prev").exists())
        self.assertEqual(sorted(p.name for p in old.iterdir()),
                         ["firmware", "firmware.prev", "project"])
        self.assertNotIn("wifi_password: old", (old / "project" / "secrets.yaml").read_text())
        self.assertFalse((old / "project.prev").exists())  # the old project was not carried over

    def test_requires_root_by_default(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("running as root")
        with self.assertRaises(lifecycle.LifecycleError):
            render.render(self.ctx(require_root=True), "helm", allow_missing_wifi=True)

    def test_unknown_display(self):
        with self.assertRaises(lifecycle.LifecycleError):
            render.render(self.ctx(), "ghost", allow_missing_wifi=True)


class Export(Base):
    def archive(self, **kw):
        stream = io.BytesIO()
        stream.isatty = lambda: False
        render.export(self.ctx(), "helm", None, True, warn=self.messages.append, stdout=stream,
                      allow_missing_wifi=True, **kw)
        return stream.getvalue()

    def test_archive_contents_modes_and_determinism(self):
        data = self.archive()
        self.assertEqual(data, self.archive())
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            members = tar.getmembers()
        names = [m.name for m in members]
        self.assertEqual(names, sorted(names, key=lambda n: n))
        self.assertEqual(names[0], "helm")
        self.assertIn("helm/helm.yaml", names)
        self.assertIn("helm/secrets.yaml", names)
        self.assertIn("helm/README.txt", names)
        self.assertIn(f"helm/boards/{BOARD}.yaml", names)
        self.assertIn("helm/fonts/RobotoCondensed-Bold.ttf", names)
        for member in members:
            self.assertEqual((member.uid, member.gid, member.mtime), (0, 0, 0))
            self.assertEqual(member.mode, 0o700 if member.isdir() else 0o600, member.name)
            self.assertTrue(member.name == "helm" or member.name.startswith("helm/"))
        self.assertTrue(any("secrets" in m for m in self.messages))
        # gzip header carries no timestamp or file name
        self.assertEqual(data[4:8], b"\0\0\0\0")

    def test_stdout_refuses_a_terminal(self):
        stream = io.BytesIO()
        stream.isatty = lambda: True
        with self.assertRaises(render.RenderError) as caught:
            render.export(self.ctx(), "helm", None, True, warn=lambda t: None, stdout=stream,
                          allow_missing_wifi=True)
        self.assertIn("terminal", str(caught.exception))
        self.assertEqual(stream.getvalue(), b"")
        self.assertFalse((self.root / "build").exists())

    def test_output_file_mode_and_refusals(self):
        out = Path(self._tmp.name) / "helm.tar.gz"
        render.export(self.ctx(), "helm", str(out), False, warn=lambda t: None,
                      allow_missing_wifi=True)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertEqual(out.read_bytes(), self.archive())
        with self.assertRaises(render.RenderError) as caught:
            render.export(self.ctx(), "helm", str(out), False, warn=lambda t: None,
                          allow_missing_wifi=True)
        self.assertIn("already exists", str(caught.exception))
        link = Path(self._tmp.name) / "link.tar.gz"
        os.symlink(Path(self._tmp.name) / "victim", link)
        with self.assertRaises(render.RenderError):
            render.export(self.ctx(), "helm", str(link), False, warn=lambda t: None,
                          allow_missing_wifi=True)
        self.assertFalse((Path(self._tmp.name) / "victim").exists())

    def test_output_is_handed_to_the_sudo_user_but_stays_0600(self):
        from unittest import mock
        out = Path(self._tmp.name) / "owned.tar.gz"
        calls = []
        env = {"SUDO_UID": str(os.getuid()), "SUDO_GID": str(os.getgid())}
        with mock.patch.object(render, "_may_chown", return_value=True), \
                mock.patch("os.fchown", side_effect=lambda fd, uid, gid: calls.append((uid, gid))):
            render.export(self.ctx(), "helm", str(out), False, warn=self.messages.append,
                          allow_missing_wifi=True, environ=env)
        self.assertEqual(calls, [(os.getuid(), os.getgid())])
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertTrue(any("owned by uid" in m for m in self.messages))

    def test_invalid_or_missing_sudo_ids_are_ignored(self):
        from unittest import mock
        bad = [{}, {"SUDO_UID": "0", "SUDO_GID": "0"}, {"SUDO_UID": "abc", "SUDO_GID": "1"},
               {"SUDO_UID": "-5", "SUDO_GID": "1"}, {"SUDO_UID": "1" * 11, "SUDO_GID": "1"},
               {"SUDO_UID": "4000000000", "SUDO_GID": "4000000000"},  # no such account
               {"SUDO_UID": str(os.getuid())}]
        for index, env in enumerate(bad):
            with self.subTest(env=env):
                self.assertIsNone(render.sudo_owner(env))
                calls = []
                out = Path(self._tmp.name) / f"bad{index}.tar.gz"
                with mock.patch.object(render, "_may_chown", return_value=True), \
                        mock.patch("os.fchown", side_effect=lambda *a: calls.append(a)):
                    render.export(self.ctx(), "helm", str(out), False, warn=lambda t: None,
                                  allow_missing_wifi=True, environ=env)
                self.assertEqual(calls, [])
                self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)

    def test_failed_chown_warns_but_keeps_the_archive(self):
        from unittest import mock
        out = Path(self._tmp.name) / "warn.tar.gz"
        env = {"SUDO_UID": str(os.getuid()), "SUDO_GID": str(os.getgid())}
        with mock.patch.object(render, "_may_chown", return_value=True), \
                mock.patch("os.fchown", side_effect=PermissionError(1, "Operation not permitted")):
            render.export(self.ctx(), "helm", str(out), False, warn=self.messages.append,
                          allow_missing_wifi=True, environ=env)
        self.assertTrue(out.stat().st_size > 0)
        self.assertTrue(any("could not give" in m for m in self.messages))

    def test_needs_exactly_one_target(self):
        with self.assertRaises(render.RenderError):
            render.export(self.ctx(), "helm", None, False, warn=lambda t: None)
        with self.assertRaises(render.RenderError):
            render.export(self.ctx(), "helm", "x", True, warn=lambda t: None, stdout=io.BytesIO())


class Cli(Base):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "displays" / "displayctl.py"), *args,
                               "--root", str(self.root), "--allow-non-root"],
                              capture_output=True)

    def test_render_prints_only_the_path_on_stdout(self):
        result = self.run_cli("render", "helm", "--allow-missing-wifi")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode().strip(),
                         str(self.root / "build/displays/helm/project"))
        self.assertIn(b"mDNS", result.stderr)

    def test_placeholder_warning_without_wifi(self):
        self.set_config(None, wifi=None)
        result = self.run_cli("render", "helm", "--allow-missing-wifi")
        self.assertIn(b"PLACEHOLDER", result.stderr)
        self.assertEqual(self.run_cli("render", "helm").returncode, 2)

    def test_export_stdout_pipe(self):
        result = self.run_cli("export", "helm", "--stdout")
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:gz") as tar:
            self.assertIn("helm/helm.yaml", tar.getnames())
        self.assertIn(b"WARNING", result.stderr)

    def test_errors_exit_nonzero_with_message(self):
        result = self.run_cli("render", "ghost", "--allow-missing-wifi")
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"unknown display", result.stderr)
        both = subprocess.run([sys.executable, str(ROOT / "displays" / "displayctl.py"), "export",
                               "helm", "--root", str(self.root)], capture_output=True)
        self.assertEqual(both.returncode, 2)  # argparse: one of --output/--stdout is required


if __name__ == "__main__":
    unittest.main()
