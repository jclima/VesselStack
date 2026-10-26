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


def with_thresholds(cfg):
    cfg["instruments"]["depth"]["thresholds"] = {"alarm_below": 4, "warn_below": 8, "hysteresis": 0.5}
    cfg["instruments"]["house_v"]["thresholds"] = {
        "alarm_below": 11.8, "warn_below": 12.2, "warn_above": 14.8, "alarm_above": 15.2}


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

    def test_thresholds_match_golden(self):
        self.set_config(with_thresholds)
        self.check_golden("helm-thresholds.yaml", self.device(allow_missing_wifi=True))

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

    def test_thresholds_only_touch_the_instruments_that_have_them(self):
        base = self.device(allow_missing_wifi=True)
        self.set_config(with_thresholds)
        text = self.device(allow_missing_wifi=True)
        for iid in ("sog", "awa"):
            self.assertNotIn(f"ins_{iid}_sev", text)
            self.assertNotIn(f"p0_{iid}_tile", text)
        for iid in ("depth", "house_v"):
            self.assertIn(f"ins_{iid}_sev, type: uint8_t", text)
            self.assertIn(f"ins_{iid}_drawn_sev", text)
            self.assertIn(f"id: p0_{iid}_tile", text)
        self.assertNotIn("sev", base)

    def test_colour_lambda_is_gated_guarded_and_literal_only(self):
        self.set_config(with_thresholds)
        text = self.device(allow_missing_wifi=True)
        self.assertIn("uint8_t sev = fresh ? id(ins_depth_sev) : 0;", text)
        self.assertIn("if (sev != id(ins_depth_drawn_sev))", text)
        self.assertIn("lv_obj_set_style_bg_color(id(p0_depth_tile), lv_color_hex(bg), LV_PART_MAIN);", text)
        self.assertIn("lv_obj_set_style_text_color(id(p0_depth_value), lv_color_hex(fg), LV_PART_MAIN);", text)
        self.assertIn("(millis() - id(ins_depth_seen)) < 20000UL", text.split("uint8_t sev = fresh")[0].splitlines()[-2])
        self.assertIn('strcmp(sv, "alarm") == 0 ? 3', text)
        for colour in ("0x1E7B34", "0xF2A900", "0xC62828", "0x37474F", "0x9AA5B1"):
            self.assertIn(colour, text)

    def test_single_layout_colours_the_content_tile_and_every_page(self):
        def mutate(cfg):
            with_thresholds(cfg)
            multipage(cfg)
        self.set_config(mutate)
        text = self.device()
        self.assertIn("lv_obj_set_style_bg_color(id(p1_content), lv_color_hex(bg), LV_PART_MAIN);", text)
        self.assertIn("lv_obj_set_style_bg_color(id(p2_house_v_tile), lv_color_hex(bg), LV_PART_MAIN);", text)
        self.assertIn("lv_obj_set_style_text_color(id(p2_house_v_value), lv_color_hex(fg), LV_PART_MAIN);", text)
        self.assertEqual(text.count("id: p1_content"), 1)
        self.assertEqual(text.count("ins_house_v_drawn_sev) = sev"), 1)  # one guard for all pages

    def test_hostile_label_with_thresholds_stays_literal(self):
        hostile = '$x ${y} "q" \\ #:'

        def mutate(cfg):
            with_thresholds(cfg)
            cfg["instruments"]["depth"]["label"] = hostile
        self.set_config(mutate)
        text = self.device(allow_missing_wifi=True)
        self.assertIn(f"text: !literal {render.yq(hostile)}", text)
        self.assertNotIn("${y}", "".join(l for l in text.splitlines() if "lambda" in l or "lv_obj" in l))

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


class AclConsistency(Base):
    """What the generated firmware publishes and subscribes to must fit the display ACL."""

    SECTIONS = ("birth_message", "will_message", "on_message", "on_json_message")

    @staticmethod
    def matches(pattern, topic):
        """Mosquitto topic filter semantics for a concrete topic."""
        p, t = pattern.split("/"), topic.split("/")
        for index, part in enumerate(p):
            if part == "#":
                return True
            if index >= len(t) or (part != "+" and part != t[index]):
                return False
        return len(p) == len(t)

    def acl_rules(self, user):
        accounts = [{"role": "display", "user": user, "password": "x"}]
        rules = {"read": [], "write": []}
        for line in mqtt_auth.render_acl(accounts).splitlines():
            if line.startswith("topic "):
                _t, mode, topic = line.split(" ", 2)
                rules[mode].append(topic)
        return rules

    def project_texts(self, home_assistant_api=False):
        def mutate(cfg):
            multipage(cfg)
            cfg["displays"]["helm"]["home_assistant_api"] = home_assistant_api
        self.set_config(mutate)
        rendered = self.collect()
        package = REGISTRY["boards"][BOARD]["esphome_package"]
        return [rendered.files["helm/helm.yaml"].decode(),
                rendered.files["helm/app/vesselstack.yaml"].decode(),
                rendered.files[f"helm/{package}"].decode()]

    def topics(self, texts):
        user = mqtt_auth.display_username("helm")
        found, section = [], None
        for text in texts:
            text = text.replace("${mqtt_username}", user)
            for line in text.splitlines():
                stripped = line.strip().rstrip(":")
                if stripped.split(":")[0] in self.SECTIONS:
                    section = stripped.split(":")[0]
                match = re.match(r"^\s*-?\s*topic:\s*(\S+)\s*$", line)
                if match:
                    found.append((section, match.group(1)))
        return found

    def test_published_and_subscribed_topics_are_permitted(self):
        for api in (False, True):
            texts = self.project_texts(api)
            user = mqtt_auth.display_username("helm")
            rules = self.acl_rules(user)
            found = self.topics(texts)
            published = {t for s, t in found if s in ("birth_message", "will_message")}
            subscribed = {t for s, t in found if s in ("on_message", "on_json_message")}
            # exactly one published topic: the display's own status (birth and will)
            self.assertEqual(published, {f"vesselstack/v1/displays/{user}/status"})
            self.assertEqual(len([1 for s, _ in found if s in ("birth_message", "will_message")]), 2)
            for topic in published:
                self.assertTrue(any(self.matches(r, topic) for r in rules["write"]), topic)
            self.assertTrue(subscribed)
            for topic in subscribed:
                self.assertTrue(any(self.matches(r, topic) for r in rules["read"]), topic)
                self.assertFalse(any(self.matches(r, topic) for r in rules["write"]), topic)
            # the ACL grants nothing beyond what the firmware uses for writing
            self.assertEqual(rules["write"], [f"vesselstack/v1/displays/{user}/status"])

    def test_nothing_else_can_publish_under_the_prefix(self):
        for api in (False, True):
            joined = "\n".join(self.project_texts(api))
            self.assertIn("log_topic: null", joined)
            self.assertIn("discovery: false", joined)
            self.assertIn("discover_ip: false", joined)
            self.assertNotIn("mqtt.publish", joined)
            self.assertNotIn("mqtt_subscribe", joined)
            self.assertNotIn("shutdown_message", joined)
            # entities that ESPHome would publish state for must not exist or must be internal
            for key in re.findall(r"(?m)^([a-z_]+):", joined):
                self.assertIn(key, {"substitutions", "esphome", "packages", "globals", "font",
                                    "mqtt", "script", "interval", "lvgl", "wifi", "ota",
                                    "logger", "psram", "spi", "display", "touchscreen",
                                    "output", "light", "api"}, key)
            for block in re.split(r"(?m)^light:", joined)[1:]:
                self.assertIn("internal: true", block.split("\n\n")[0])


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

    # Other code legitimately calls os.fchown(fd, 0, 0) when the suite runs as root, so these
    # tests only look at chowns to a non-root account (12345 stands in for the sudo user).
    FAKE = {"SUDO_UID": "12345", "SUDO_GID": "12345"}

    def fake_accounts(self):
        from unittest import mock
        return (mock.patch.object(render.pwd, "getpwuid", return_value=object()),
                mock.patch.object(render.grp, "getgrgid", return_value=object()),
                mock.patch.object(render, "_may_chown", return_value=True))

    def dir_info(self, uid=12345, mode=0o040700):
        """What fstat of the output directory reports (the real owner is the test runner)."""
        return os.stat_result((mode, 1, 1, 2, uid, 0, 4096, 0, 0, 0))

    def export_with(self, name, env, fchown, info=None, subdir=None):
        from unittest import mock
        base = Path(self._tmp.name)
        if subdir:
            (base / subdir).mkdir(exist_ok=True)
            base = base / subdir
        out = base / name
        patches = self.fake_accounts()
        info = info or self.dir_info()
        with patches[0], patches[1], patches[2], mock.patch("os.fchown", side_effect=fchown), \
                mock.patch.object(render, "_fstat_dir", return_value=info):
            render.export(self.ctx(), "helm", str(out), False, warn=self.messages.append,
                          allow_missing_wifi=True, environ=env)
        return out

    def test_output_is_handed_to_the_sudo_user_but_stays_0600(self):
        calls = []
        out = self.export_with("owned.tar.gz", self.FAKE, lambda fd, uid, gid: calls.append((uid, gid)))
        self.assertEqual([c for c in calls if c != (0, 0)], [(12345, 12345)])
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertTrue(any("owned by uid 12345" in m for m in self.messages))

    def chowns(self, **kw):
        calls = []
        out = self.export_with(kw.pop("name", "x.tar.gz"), self.FAKE,
                               lambda fd, uid, gid: calls.append((uid, gid)), **kw)
        return out, [c for c in calls if c != (0, 0)]

    def test_output_in_a_directory_owned_by_the_sudo_user_is_handed_over(self):
        out, calls = self.chowns(info=self.dir_info(uid=12345, mode=0o040755))
        self.assertEqual(calls, [(12345, 12345)])
        self.assertFalse(any("stays owned by root" in m for m in self.messages))
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)

    def test_output_in_a_root_owned_directory_is_not_handed_over_and_a_hint_is_printed(self):
        out, calls = self.chowns(info=self.dir_info(uid=0, mode=0o040755))
        self.assertEqual(calls, [])
        self.assertTrue(out.stat().st_size > 0)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        hint = [m for m in self.messages if "stays owned by root" in m]
        self.assertEqual(len(hint), 1)
        self.assertIn('sudo chown "$USER"', hint[0])
        self.assertIn(str(out), hint[0])
        self.assertIn("--stdout", hint[0])

    def test_directory_owned_by_someone_else_is_not_handed_over(self):
        self.assertEqual(self.chowns(info=self.dir_info(uid=999))[1], [])

    def test_group_or_world_writable_directory_is_not_handed_over(self):
        for index, mode in enumerate((0o040775, 0o040757, 0o041777, 0o040777)):
            with self.subTest(mode=oct(mode)):
                self.messages.clear()
                _, calls = self.chowns(name=f"w{index}.tar.gz", info=self.dir_info(mode=mode))
                self.assertEqual(calls, [])
                self.assertTrue(any("writable by group or others" in m for m in self.messages))

    def test_handover_decision_is_a_pure_function_of_owner_and_mode(self):
        owner = (12345, 12345)
        self.assertIsNone(render._handover_allowed(self.dir_info(12345, 0o040700), owner))
        self.assertIsNone(render._handover_allowed(self.dir_info(12345, 0o040755), owner))
        self.assertIsNotNone(render._handover_allowed(self.dir_info(0, 0o040700), owner))
        self.assertIsNotNone(render._handover_allowed(self.dir_info(12345, 0o040770), owner))

    def test_the_checked_directory_is_the_one_the_file_is_created_in(self):
        # The fstat is taken on the descriptor the file was created through.
        from unittest import mock
        seen = []
        real = os.open

        def spy(path, flags, mode=0o777, *, dir_fd=None):
            fd = real(path, flags, mode, dir_fd=dir_fd)
            seen.append((path, dir_fd, fd))
            return fd

        info_fds = []
        with mock.patch("os.open", side_effect=spy), mock.patch.object(
                render, "_fstat_dir", side_effect=lambda fd: info_fds.append(fd) or self.dir_info()):
            out = Path(self._tmp.name) / "same.tar.gz"
            with mock.patch.object(render, "_may_chown", return_value=True), \
                    mock.patch.object(render.pwd, "getpwuid", return_value=object()), \
                    mock.patch.object(render.grp, "getgrgid", return_value=object()), \
                    mock.patch("os.fchown"):
                render.export(self.ctx(), "helm", str(out), False, warn=lambda t: None,
                              allow_missing_wifi=True, environ=self.FAKE)
        created = [entry for entry in seen if entry[0] == "same.tar.gz"]
        self.assertEqual(len(created), 1)
        self.assertEqual(info_fds, [created[0][1]])  # fstat'd the dir fd used as dir_fd

    def test_symlinked_output_directory_is_refused_and_nothing_is_written(self):
        base = Path(self._tmp.name)
        target = base / "realdir"
        target.mkdir()
        (base / "linkdir").symlink_to(target)
        with self.assertRaises(render.RenderError) as caught:
            render.export(self.ctx(), "helm", str(base / "linkdir" / "a.tar.gz"), False,
                          warn=lambda t: None, allow_missing_wifi=True)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(list(target.iterdir()), [])

    def test_output_must_name_a_file(self):
        base = Path(self._tmp.name)
        for bad in (str(base) + "/", str(base / "."), str(base / "..")):
            with self.assertRaises(render.RenderError, msg=bad):
                render.export(self.ctx(), "helm", bad, False, warn=lambda t: None,
                              allow_missing_wifi=True)

    def test_relative_output_uses_the_current_directory(self):
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self._tmp.name)
        render.export(self.ctx(), "helm", "rel.tar.gz", False, warn=lambda t: None,
                      allow_missing_wifi=True)
        self.assertEqual(stat.S_IMODE(os.stat("rel.tar.gz").st_mode), 0o600)

    def test_invalid_or_missing_sudo_ids_are_ignored(self):
        bad = [{}, {"SUDO_UID": "0", "SUDO_GID": "0"}, {"SUDO_UID": "abc", "SUDO_GID": "1"},
               {"SUDO_UID": "-5", "SUDO_GID": "1"}, {"SUDO_UID": "1" * 11, "SUDO_GID": "1"},
               {"SUDO_UID": "12345"}]
        for index, env in enumerate(bad):
            with self.subTest(env=env):
                self.assertIsNone(render.sudo_owner(env))
                calls = []
                out = self.export_with(f"bad{index}.tar.gz", env,
                                       lambda fd, uid, gid: calls.append((uid, gid)))
                self.assertEqual([c for c in calls if c != (0, 0)], [])
                self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)

    def test_ids_without_an_account_are_ignored(self):
        # no patching of pwd/grp: uid and gid 4000000000 do not exist anywhere
        self.assertIsNone(render.sudo_owner({"SUDO_UID": "4000000000", "SUDO_GID": "4000000000"}))

    def test_failed_chown_warns_but_keeps_the_archive(self):
        def refuse(fd, uid, gid):
            if uid != 0:
                raise PermissionError(1, "Operation not permitted")
        out = self.export_with("warn.tar.gz", self.FAKE, refuse)
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
