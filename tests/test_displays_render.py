#!/usr/bin/env python3
"""Tests for the ESPHome project generator (displays/render.py)."""

from __future__ import annotations

import copy
import io
import json
import math
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


def status_page(cfg, items=None, title="Status"):
    """Notifications plus a status-grid page (7 items by default: 3 notifications, 4 instruments)."""
    cfg["notifications"] = {
        "anchor": {"path": "notifications.navigation.anchor", "label": "Anchor"},
        "batt_low": {"path": "notifications.electrical.batteries.house.voltage", "label": "Low battery",
                     "absent": "unknown"},
        "bilge": {"path": "notifications.bilge.main", "label": "Bilge"},
    }
    with_thresholds(cfg)
    cfg["displays"]["helm"]["pages"] = [
        {"title": title, "layout": "status-grid",
         "items": items or ["anchor", "sog", "depth", "batt_low", "house_v", "awa", "bilge"]}]


def alarm_takeover(cfg, page=1, labels=None):
    """Thresholds, notifications, a Nav and a Status page, and the alarm takeover banner."""
    status_page(cfg)
    pages = cfg["displays"]["helm"]["pages"]
    pages.insert(0, {"title": "Nav", "layout": "grid-2x2", "items": ["sog", "depth", "awa", "house_v"]})
    cfg["displays"]["helm"]["ui"] = {"alarm_takeover": True, "alarm_page": page}
    for iid, label in (labels or {}).items():
        (cfg["notifications"] if iid in cfg["notifications"] else cfg["instruments"])[iid]["label"] = label


def dashboard_ui(cfg, **ui):
    """The alarm takeover display plus a Tanks page, swipe, header, a night schedule and idle dimming."""
    alarm_takeover(cfg)
    cfg["displays"]["helm"]["pages"].append({"title": "Tanks", "layout": "single", "items": ["house_v"]})
    options = {"swipe": True, "header": True, "night": "schedule", "night_brightness": 15, "dim_after_s": 120}
    options.update(ui)
    cfg["displays"]["helm"]["ui"].update(options)
    cfg["night_schedule"] = {"start": "21:30", "end": "06:15"}


def tank(label, path, unit="%", **extra):
    inst = {"path": path, "label": label, "convert": "ratio->%", "unit": unit, "precision": 0,
            "stale_after_s": 60, "min": 0, "max": 100}
    inst.update(extra)
    return inst


def gauge_pages(cfg, header=False, night=False):
    """Four tanks on a tank-bars page and the fuel tank on a gauge-arc page."""
    cfg["instruments"].update({
        "fuel": tank("Fuel", "tanks.fuel.0.currentLevel",
                     thresholds={"alarm_below": 10, "warn_below": 25, "hysteresis": 2}),
        "water": tank("Water", "tanks.freshWater.0.currentLevel", thresholds={"warn_below": 20}),
        "waste": tank("Waste", "tanks.blackWater.0.currentLevel",
                      thresholds={"warn_above": 75, "alarm_above": 90}),
        "lpg": tank("LPG", "tanks.lpg.0.currentLevel"),
    })
    cfg["displays"]["helm"]["pages"] = [
        {"title": "Tanks", "layout": "tank-bars", "items": ["fuel", "water", "waste", "lpg"]},
        {"title": "Fuel", "layout": "gauge-arc", "items": ["fuel"]},
    ]
    ui = {}
    if header:
        ui.update(header=True, swipe=True)
    if night:
        ui.update(night="manual")
    if ui:
        cfg["displays"]["helm"]["ui"] = ui


def electrical_pages(cfg, header=False, night=False, pages=("min", "shore", "full")):
    """House battery instruments plus up to three electrical pages (3, 4 and 5 items)."""
    cfg["instruments"].update({
        "batt_v": {"path": "electrical.batteries.house.voltage", "label": "House", "convert": "none", "unit": "V",
                   "precision": 2, "stale_after_s": 30,
                   "thresholds": {"alarm_below": 11.8, "warn_below": 12.2, "warn_above": 14.8, "hysteresis": 0.05}},
        "batt_soc": tank("SoC", "electrical.batteries.house.capacity.stateOfCharge",
                         thresholds={"alarm_below": 20, "warn_below": 30, "hysteresis": 2}),
        "batt_a": {"path": "electrical.batteries.house.current", "label": "Current", "convert": "none", "unit": "A",
                   "precision": 1, "stale_after_s": 30},
        "shore_a": {"path": "electrical.shore.current", "label": "Shore", "convert": "none", "unit": "A",
                    "precision": 1, "stale_after_s": 30},
        "solar_a": {"path": "electrical.solar.main.current", "label": "Solar", "convert": "none", "unit": "A",
                    "precision": 1, "stale_after_s": 30},
    })
    cfg["notifications"] = {"shore": {"path": "notifications.electrical.shorePower", "label": "Shore",
                                      "absent": "unknown"}}
    choices = {
        "min": {"title": "Power", "layout": "electrical", "items": ["batt_v", "batt_soc", "batt_a"]},
        "shore": {"title": "Shore", "layout": "electrical", "items": ["batt_v", "batt_soc", "batt_a", "shore_a"]},
        "full": {"title": "Full", "layout": "electrical",
                 "items": ["batt_v", "batt_soc", "batt_a", "shore", "solar_a"]},
    }
    cfg["displays"]["helm"]["pages"] = [choices[name] for name in pages]
    ui = {}
    if header:
        ui.update(header=True, swipe=True)
    if night:
        ui.update(night="manual")
    if ui:
        cfg["displays"]["helm"]["ui"] = ui


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

    def test_status_grid_matches_golden(self):
        self.set_config(status_page)
        self.check_golden("helm-status.yaml", self.device(allow_missing_wifi=True))

    def test_alarm_takeover_matches_golden(self):
        self.set_config(alarm_takeover)
        self.check_golden("helm-alarm.yaml", self.device(allow_missing_wifi=True))

    def test_dashboard_ui_matches_golden(self):
        self.set_config(dashboard_ui)
        self.check_golden("helm-ui.yaml", self.device(allow_missing_wifi=True))

    def test_tank_bars_and_gauge_arc_match_golden(self):
        self.set_config(gauge_pages)
        self.check_golden("helm-gauges.yaml", self.device(allow_missing_wifi=True))

    def test_electrical_pages_match_golden(self):
        def mutate(cfg):
            electrical_pages(cfg, header=True, night=True)
        self.set_config(mutate)
        self.check_golden("helm-electrical.yaml", self.device(allow_missing_wifi=True))

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


class StatusGrid(Base):
    def status_device(self, items=None, mutate=None):
        def apply(cfg):
            status_page(cfg, items)
            if mutate:
                mutate(cfg)
        self.set_config(apply)
        return self.device(allow_missing_wifi=True)

    def items(self, count):
        pool = ["anchor", "sog", "depth", "batt_low", "house_v", "awa", "bilge"]
        extra = [f"x{n}" for n in range(9 - len(pool))]

        def mutate(cfg):
            for iid in extra:
                cfg["instruments"][iid] = dict(cfg["instruments"]["sog"])
        return (pool + extra)[:count], mutate

    def test_grid_shape_and_cell_size_follow_the_item_count(self):
        expected = {1: (2, 2, 167), 2: (2, 2, 167), 4: (2, 2, 167), 5: (3, 2, 109), 6: (3, 2, 109),
                    7: (3, 3, 109), 8: (3, 3, 109), 9: (3, 3, 109)}
        for count, (cols, rows, side) in expected.items():
            items, mutate = self.items(count)
            text = self.status_device(items, mutate)
            self.assertIn(f"grid_rows: [{', '.join(['FR(1)'] * rows)}]\n", text, count)
            self.assertIn(f"grid_columns: [{', '.join(['FR(1)'] * cols)}]\n", text, count)
            self.assertIn("pad_row: 6\n              pad_column: 6", text)
            self.assertIn("width: 340\n            height: 340", text)
            self.assertEqual(text.count("grid_cell_row_pos:"), count)
            width = render.status_cell_size(340, 340, count)
            if rows == 2 and cols == 3:
                self.assertEqual(width, (109, 167), count)
            else:
                self.assertEqual(width, (side, side), count)
            font = "font_status_lg" if cols == 2 else "font_status_sm"
            self.assertIn(f"text_font: {font}", text)
            self.assertNotIn("font_status_sm" if cols == 2 else "font_status_lg", text)

    def test_cells_are_placed_row_major(self):
        items, mutate = self.items(7)
        text = self.status_device(items, mutate)
        cells = re.findall(r"grid_cell_row_pos: (\d)\n\s+grid_cell_column_pos: (\d)", text)
        self.assertEqual(cells, [(str(i // 3), str(i % 3)) for i in range(7)])

    def test_capacity_is_nine_and_one_is_enough(self):
        self.assertEqual(schema.layout_specs(REGISTRY)["status-grid"], {
            "capacity": 9, "min_items": 1, "shapes": ["round", "rect"], "types": ["instrument", "notification", "ha"]})
        text = self.status_device(["anchor"])
        self.assertEqual(text.count("grid_cell_row_pos:"), 1)

    def test_notification_words_are_fixed_literals_chosen_by_generated_branches(self):
        text = self.status_device()
        for number, word in ((3, "ALARM"), (2, "WARN"), (1, "OK"), (0, "--")):
            self.assertIn(f"== {number};'\n          then:\n            - lvgl.label.update:\n"
                          f"                id: p0_anchor_value\n                text: !literal \"{word}\"", text)
        self.assertEqual(render.STATUS_WORDS, {1: "OK", 2: "WARN", 3: "ALARM", 0: "--"})
        # the gated severity is used, so stale/unknown/disconnected can only ever read "--"
        branch = text.split("id(ins_anchor_sev) : 0) == 1;")[0].splitlines()[-1]
        for gate in ("id(bridge_online)", "mqtt::global_mqtt_client->is_connected()", "id(ins_anchor_ok)",
                     "(millis() - id(ins_anchor_seen)) < 15000UL"):
            self.assertIn(gate, branch)

    def test_notification_gate_is_three_heartbeats(self):
        def mutate(cfg):
            cfg["heartbeat_s"] = 7
            for inst in cfg["instruments"].values():
                inst["stale_after_s"] = 14
        text = self.status_device(mutate=mutate)
        self.assertIn("(millis() - id(ins_anchor_seen)) < 21000UL", text)
        self.assertEqual(render.item_limit_ms({"notifications": {"n": {}}, "instruments": {}}, "n", 5), 15000)
        self.assertIn("id(ins_sog_seen)) < 28000UL", text)  # instruments keep stale_after_s + 2 heartbeats

    def test_notification_message_handler_and_globals(self):
        text = self.status_device()
        self.assertIn("topic: vesselstack/v1/notifications/anchor", text)
        for suffix, kind in (("ok", "bool"), ("seen", "uint32_t"), ("sev", "uint8_t"), ("drawn_sev", "uint8_t")):
            self.assertIn(f"id: ins_anchor_{suffix}, type: {kind}", text)
        self.assertIn('id(ins_anchor_ok) = strcmp(st, "ok") == 0;', text)
        self.assertNotIn("ins_anchor_val", text)
        self.assertNotIn('x["v"]', text.split("notifications/anchor")[1].split("instruments/sog")[0])
        for number, (bg, fg) in render.SEV_COLORS.items():
            self.assertIn(f"0x{bg:06X}", text)

    def test_colour_and_tile_for_every_notification_and_thresholded_instrument_only(self):
        text = self.status_device()
        for iid in ("anchor", "batt_low", "bilge", "depth", "house_v"):
            self.assertIn(f"id: p0_{iid}_tile", text)
            self.assertIn(f"lv_obj_set_style_bg_color(id(p0_{iid}_tile), lv_color_hex(bg), LV_PART_MAIN);", text)
            self.assertIn(f"lv_obj_set_style_text_color(id(p0_{iid}_value), lv_color_hex(fg), LV_PART_MAIN);", text)
        for iid in ("sog", "awa"):
            self.assertNotIn(f"p0_{iid}_tile", text)
            self.assertNotIn(f"ins_{iid}_sev", text)
        self.assertEqual(text.count("ins_anchor_drawn_sev) = sev"), 1)

    def test_instrument_cells_show_value_and_unit(self):
        text = self.status_device()
        self.assertIn("id: p0_sog_value, text: \"--\", text_font: font_status_sm", text)
        self.assertIn('text: !literal "kn"', text)
        self.assertRegex(text, r"instruments/house_v[\s\S]*?as<float>\(\), 2\)")
        self.assertNotIn("lvgl.label.update:\n          id: p0_anchor_value\n          text: !lambda", text)

    def test_hostile_notification_label_stays_literal_and_out_of_lambdas(self):
        hostile = '$x ${y} "q" \\ #:'

        def mutate(cfg):
            cfg["notifications"]["anchor"]["label"] = hostile
        text = self.status_device(mutate=mutate)
        self.assertIn(f"text: !literal {render.yq(hostile)}", text)
        for line in text.splitlines():
            if "${y}" in line:
                self.assertTrue(line.lstrip().startswith("- label:") or "glyphs:" in line, line)
        self.assertNotIn("${y}", "".join(l for l in text.splitlines() if "lambda" in l or "lv_obj" in l))
        self.assertEqual(text.count("!literal"), len(re.findall(r"!literal", text)))
        # fixed words are literals too; no word is built at run time
        self.assertNotIn("std::string(\"ALARM\")", text)

    def test_glyphs_cover_the_fixed_words_and_values(self):
        text = self.status_device()
        bold = {chr(c) for c in render.font_coverage("bold")}
        for word in render.STATUS_WORDS.values():
            self.assertTrue(set(word) <= bold, word)
        glyphs = json.loads(re.search(r"id: font_status_sm\n    size: 30\n    bpp: 4\n    glyphs: !literal (\".*\")",
                                      text).group(1))
        self.assertTrue(set("".join(render.STATUS_WORDS.values()) + render.VALUE_GLYPHS) <= set(glyphs))
        self.assertEqual(len(glyphs), len(set(glyphs)))
        # only the fixed words: a page of notifications needs no digits
        only = self.status_device(["anchor", "bilge"])
        only_glyphs = json.loads(re.search(r"id: font_status_lg\n    size: 48\n    bpp: 4\n    glyphs: !literal (\".*\")",
                                           only).group(1))
        self.assertEqual(set(only_glyphs), set("".join(render.STATUS_WORDS.values())))
        small = json.loads(re.search(r"id: font_small\n    size: 20\n    bpp: 4\n    glyphs: !literal (\".*\")",
                                     only).group(1))
        self.assertTrue(set("Anchor" + "Bilge") <= set(small))

    def test_unsupported_notification_label_glyph_is_reported(self):
        def mutate(cfg):
            cfg["notifications"]["anchor"]["label"] = "日本"
        self.set_config(lambda cfg: (status_page(cfg), mutate(cfg)))
        with self.assertRaises(render.RenderError) as caught:
            self.collect(allow_missing_wifi=True)
        self.assertIn("notifications.anchor.label", str(caught.exception))

    def test_only_status_grid_pages_add_status_fonts(self):
        self.set_config(None)
        self.assertNotIn("font_status", self.device(allow_missing_wifi=True))

    def test_status_grid_subscribes_to_used_notifications_only(self):
        text = self.status_device(["anchor", "sog"])
        self.assertIn("notifications/anchor", text)
        self.assertNotIn("notifications/bilge", text)
        self.assertNotIn("batt_low", text)

    def test_status_page_with_navigation_and_other_layouts(self):
        def mutate(cfg):
            cfg["displays"]["helm"]["pages"].append(
                {"title": "Nav", "layout": "grid-2x2", "items": ["sog", "depth"]})
        text = self.status_device(mutate=mutate)
        self.assertEqual(text.count("lvgl.page.show"), 2)
        self.assertIn("id: p1_depth_tile", text)
        self.assertIn("lv_obj_set_style_bg_color(id(p0_depth_tile)", text)
        self.assertIn("lv_obj_set_style_bg_color(id(p1_depth_tile)", text)

    def test_status_subscriptions_are_readable_by_the_display_acl(self):
        rules = mqtt_auth.render_acl([{"role": "display", "user": "vsdisp-helm", "password": "x"}])
        self.assertIn("topic read vesselstack/v1/notifications/#", rules)
        self.assertIn("topic read vesselstack/v1/ha/#", rules)
        self.assertEqual([l for l in rules.splitlines() if l.startswith("topic write")],
                         ["topic write vesselstack/v1/displays/vsdisp-helm/status"])


class AlarmTakeover(Base):
    def alarm_device(self, mutate=None, **kw):
        def both(cfg):
            alarm_takeover(cfg)
            if mutate:
                mutate(cfg)
        self.set_config(both)
        return self.device(allow_missing_wifi=True, **kw)

    def test_nothing_is_generated_without_the_option(self):
        for mutate in (None, status_page, lambda c: (status_page(c), c["displays"]["helm"].update(ui={}),
                                                       c["displays"]["helm"]["ui"].update(alarm_takeover=False))):
            self.set_config(mutate)
            text = self.device(allow_missing_wifi=True)
            for needle in ("alarms/summary", "alarm_banner", "top_layer", "summary_max", "render_alarm",
                           "board_backlight", "font_banner", "500ms"):
                self.assertNotIn(needle, text, needle)

    def test_banner_subscription_and_top_layer(self):
        text = self.alarm_device()
        self.assertEqual(text.count("topic: vesselstack/v1/alarms/summary"), 1)
        self.assertEqual(text.count("  top_layer:\n"), 1)
        self.assertIn("id: alarm_banner\n", text)
        self.assertIn("bg_color: 0xC62828", text)
        self.assertIn("width: 340", text)    # the board content box, centred
        self.assertIn("height: 96", text)
        self.assertIn("align: CENTER", text.split("top_layer:")[1])
        self.assertIn("hidden: true", text)
        self.assertIn("- interval: 500ms", text)
        self.assertIn("LV_OPA_40 : LV_OPA_COVER", text)
        self.assertIn("- script.execute: render_alarm", text.split("- interval: 1s")[1].split("lvgl:")[0])

    def test_ids_map_to_compile_time_item_indexes(self):
        text = self.alarm_device()
        mapping = re.findall(r'(?:else )?if \(strcmp\(id0, "(\w+)"\) == 0\) first = (\d+);', text)
        self.assertEqual(mapping, [("sog", "0"), ("depth", "1"), ("awa", "2"), ("house_v", "3"),
                                   ("anchor", "4"), ("batt_low", "5"), ("bilge", "6")])
        self.assertIn("int first = -1;", text)
        names = re.findall(r"alarm_idx\) == (-?\d+) && .*\n\s+then:\n\s+- lvgl.label.update:\n\s+id: "
                           r"alarm_banner_name\n\s+text: (.*)", text)
        self.assertEqual(names, [("-1", '""'), ("0", '!literal "SOG"'), ("1", '!literal "Depth"'),
                                 ("2", '!literal "AWA"'), ("3", '!literal "House"'),
                                 ("4", '!literal "Anchor"'), ("5", '!literal "Low battery"'),
                                 ("6", '!literal "Bilge"')])

    def test_only_this_displays_items_are_mapped(self):
        def mutate(cfg):
            cfg["displays"]["helm"]["pages"] = [
                {"title": "Status", "layout": "status-grid", "items": ["anchor", "depth"]}]
            cfg["displays"]["helm"]["ui"] = {"alarm_takeover": True}
        text = self.alarm_device(mutate)
        self.assertEqual(re.findall(r'strcmp\(id0, "(\w+)"\)', text), ["anchor", "depth"])
        self.assertNotIn("bilge", text)

    def test_alarm_page_and_default(self):
        self.assertIn("lvgl.page.show:\n                  id: page_1\n", self.alarm_device())

        def default(cfg):
            cfg["displays"]["helm"]["ui"] = {"alarm_takeover": True}
        text = self.alarm_device(default)
        self.assertRegex(text, r"- lvgl.page.show:\n\s+id: page_0\n\s+animation: NONE\n\s+time: 0ms\n"
                               r"\s+- light.turn_on:\n\s+id: board_backlight\n\s+brightness: 100%")

    def test_edge_detection_staleness_and_dismissal(self):
        text = self.alarm_device()
        self.assertIn("bool rising = m == 3 && (id(summary_max) != 3 || na > id(summary_na));", text)
        self.assertIn("(millis() - id(summary_seen)) < 15000UL", text)   # 3 x heartbeat_s (5 s)
        show = re.search(r"bool show = (.*);", text).group(1)
        for guard in ("seen", "id(bridge_online)", "mqtt::global_mqtt_client->is_connected()",
                      "id(summary_max) == 3", "!id(alarm_dismissed)"):
            self.assertIn(guard, show)
        self.assertIn("on_click:\n            - lambda: 'id(alarm_dismissed) = true;'", text)
        self.assertNotIn("mqtt.publish", text)

    def test_staleness_follows_heartbeat(self):
        def mutate(cfg):
            cfg["heartbeat_s"] = 7
            for inst in cfg["instruments"].values():
                inst["stale_after_s"] = 14
        self.assertIn("< 21000UL;", self.alarm_device(mutate).split("id: render_alarm")[1][:400])

    def test_hostile_labels_stay_literal_and_out_of_lambdas(self):
        hostile = {"anchor": '$x ${y} "q" \\ #:', "depth": "<% 1+1 %>"}
        text = self.alarm_device(lambda cfg: [(cfg["notifications"] if k in cfg["notifications"]
                                               else cfg["instruments"])[k].update(label=v)
                                              for k, v in hostile.items()])
        for label in hostile.values():
            self.assertIn(f"text: !literal {render.yq(label)}", text)
        for line in text.splitlines():
            if any(label in line for label in hostile.values()):
                self.assertTrue(line.lstrip().startswith(("text:", "- label:")) or "glyphs:" in line, line)
        inside = "".join(l for l in text.splitlines() if "lambda" in l or "lv_" in l or "snprintf" in l)
        for label in hostile.values():
            self.assertNotIn(label, inside)
        # the only composed text is the count and the two fixed words
        composed = next(l for l in text.splitlines() if "snprintf" in l)
        self.assertEqual(re.findall(r'"(ALARMS?)"', composed), ["ALARM", "ALARMS"])

    def test_banner_glyphs_cover_the_fixed_text_and_labels(self):
        text = self.alarm_device()
        bold = {chr(c) for c in render.font_coverage("bold")}
        self.assertTrue(set(render.BANNER_GLYPHS) <= bold)
        banner = json.loads(re.search(r"id: font_banner\n    size: 48\n    bpp: 4\n    glyphs: !literal (\".*\")",
                                      text).group(1))
        self.assertTrue(set("0123456789+ ALARMS") <= set(banner))
        name = json.loads(re.search(r"id: font_banner_name\n    size: 28\n    bpp: 4\n    glyphs: !literal (\".*\")",
                                    text).group(1))
        self.assertTrue(set("SOGDepthAWAHouseAnchorLow batteryBilge") <= set(name))

    def test_unsupported_label_glyph_is_still_reported(self):
        def mutate(cfg):
            alarm_takeover(cfg)
            cfg["notifications"]["bilge"]["label"] = "日本"
        self.set_config(mutate)
        with self.assertRaises(render.RenderError):
            self.collect(allow_missing_wifi=True)

    def test_summary_subscription_fits_the_display_acl(self):
        rules = AclConsistency.acl_rules(None, "vsdisp-helm")
        topic = "vesselstack/v1/alarms/summary"
        self.assertTrue(any(AclConsistency.matches(r, topic) for r in rules["read"]))
        self.assertFalse(any(AclConsistency.matches(r, topic) for r in rules["write"]))
        self.assertEqual(rules["write"], ["vesselstack/v1/displays/vsdisp-helm/status"])

    def test_backlight_stays_internal_so_nothing_is_published(self):
        board = self.collect().files["helm/boards/waveshare-esp32-s3-touch-lcd-2.8c.yaml"].decode()
        self.assertRegex(board, r"id: board_backlight\n\s+name: Backlight\n\s+internal: true")


class DashboardUi(Base):
    """swipe, header, night and dimming (display dashboard slice S4)."""

    def ui_device(self, **ui):
        self.set_config(lambda cfg: dashboard_ui(cfg, **ui))
        return self.device(allow_missing_wifi=True)

    def plain_device(self, **ui):
        def mutate(cfg):
            multipage(cfg)
            if ui:
                cfg["displays"]["helm"]["ui"] = ui
                if ui.get("night") == "schedule":
                    cfg["night_schedule"] = {"start": "21:30", "end": "06:15"}
        self.set_config(mutate)
        return self.device(allow_missing_wifi=True)

    MARKERS = ("touch_ignore", "dim_idle", "bl_drawn", "night_", "render_header", "apply_night",
               "apply_power", "clock_", "sk_state", "bridge/clock", "bridge/signalk", "on_swipe",
               "on_short_click", "on_long_press", "on_press", "p0_clock", "p0_title", "p0_pages",
               "p0_sog_name", "lvgl.is_idle", "lv_display")

    def test_nothing_is_generated_without_the_keys(self):
        for text in (self.plain_device(), self.plain_device(swipe=False, header=False, night="off",
                                                            dim_after_s=0),
                     self.plain_device(alarm_takeover=False)):
            for marker in self.MARKERS:
                self.assertNotIn(marker, text, marker)
            self.assertIn("on_click:", text)
            self.assertEqual(text.count("on_click:"), 3)

    def test_each_key_only_adds_its_own_part(self):
        only = {"swipe": ("on_swipe_left", "touch_ignore"), "header": ("render_header", "bridge/clock"),
                "night": ("apply_night", "on_long_press"), "dim_after_s": ("lvgl.is_idle", "dim_idle"),
                "day_brightness": ("apply_power", "bl_drawn")}
        values = {"swipe": True, "header": True, "night": "manual", "dim_after_s": 60, "day_brightness": 70}
        for key, wanted in only.items():
            text = self.plain_device(**{key: values[key]})
            for marker in wanted:
                self.assertIn(marker, text, (key, marker))
            others = [m for k, w in only.items() if k != key for m in w if m not in wanted]
            for marker in others:
                if marker in ("apply_power", "bl_drawn") and key in ("night", "dim_after_s"):
                    continue   # night and dimming drive the backlight too
                if marker == "touch_ignore" and key in ("night", "dim_after_s"):
                    continue   # they share the gesture-aware touch layer
                self.assertNotIn(marker, text, (key, marker))

    def test_swipe_pages_with_computed_ids_and_a_short_click_fallback(self):
        text = self.ui_device()
        tap0 = text.split("id: p0_tap")[1].split("- obj:")[0]
        self.assertIn("on_swipe_left:", tap0)
        self.assertIn("on_swipe_right:", tap0)
        self.assertIn("on_short_click:", tap0)
        self.assertNotIn("on_click:", tap0)
        shows = re.findall(r"on_(short_click|swipe_left|swipe_right):.*?id: page_(\d)", tap0, re.S)
        self.assertEqual(shows, [("short_click", "1"), ("swipe_left", "1"), ("swipe_right", "2")])
        tap2 = text.split("id: p2_tap")[1]
        self.assertEqual(re.findall(r"on_(short_click|swipe_left|swipe_right):.*?id: page_(\d)", tap2, re.S),
                         [("short_click", "0"), ("swipe_left", "0"), ("swipe_right", "1")])
        self.assertNotIn("page.next", text)
        self.assertNotIn("page.previous", text)

    def test_a_gesture_that_acted_cannot_act_twice(self):
        tap = self.ui_device().split("id: p0_tap")[1].split("- obj:")[0]
        self.assertIn("on_press:\n              - lambda: 'id(touch_ignore) = id(dim_idle);'", tap)
        self.assertEqual(tap.count("lambda: 'return !id(touch_ignore);'"), 4)   # click, 2 swipes, long press
        for event in ("on_swipe_left:", "on_swipe_right:", "on_long_press:"):
            body = tap.split(event)[1].split("\n            on_")[0]
            self.assertIn("- lambda: 'id(touch_ignore) = true;'", body, event)
        # a plain tap only pages; the flag is never set by it
        click = tap.split("on_short_click:")[1].split("\n            on_")[0]
        self.assertNotIn("= true", click)

    def test_without_dimming_a_press_never_ignores(self):
        text = self.plain_device(swipe=True)
        self.assertIn("id(touch_ignore) = false;", text)
        self.assertNotIn("dim_idle", text)

    def test_swipe_on_a_single_page_display_adds_nothing_to_swipe(self):
        def mutate(cfg):
            cfg["displays"]["helm"]["pages"] = [{"title": "One", "layout": "single", "items": ["sog"]}]
            cfg["displays"]["helm"]["ui"] = {"swipe": True}
        self.set_config(mutate)
        text = self.device(allow_missing_wifi=True)
        self.assertNotIn("p0_tap", text)
        self.assertNotIn("on_swipe", text)

    def test_long_press_on_a_single_page_display_gets_a_tap_layer_without_paging(self):
        def mutate(cfg):
            cfg["displays"]["helm"]["pages"] = [{"title": "One", "layout": "single", "items": ["sog"]}]
            cfg["displays"]["helm"]["ui"] = {"night": "manual"}
        self.set_config(mutate)
        text = self.device(allow_missing_wifi=True)
        tap = text.split("id: p0_tap")[1]
        self.assertIn("on_long_press:", tap)
        self.assertNotIn("page.show", tap)
        self.assertNotIn("on_short_click", tap)

    def test_night_uses_short_click_so_a_long_press_does_not_page(self):
        tap = self.plain_device(night="manual").split("id: p0_tap")[1].split("- obj:")[0]
        self.assertIn("on_short_click:", tap)
        self.assertNotIn("on_click:", tap)
        self.assertNotIn("on_swipe", tap)
        self.assertIn("on_long_press:", tap)

    def test_header_stays_inside_the_ring_and_outside_the_content_box(self):
        text = self.ui_device()
        board = REGISTRY["boards"][BOARD]
        ring = board["ring"]
        top = (board["height"] - board["content_height"]) // 2
        for pi in range(3):
            page = text.split(f"- id: page_{pi}\n")[1].split("\n    - id: page_")[0]
            page = page.split(f"id: p{pi}_tap")[0]
            ys = [int(v) for v in re.findall(r"\by: (\d+)", page)]
            self.assertTrue(ys and min(ys) >= ring["header_y"], ys)
            self.assertLessEqual(max(y for y in ys if y < 100) + 24, top)   # title row ends above the content
            self.assertIn(f"y: {ring['footer_y']}\n", page)
            self.assertGreaterEqual(ring["footer_y"], top + board["content_height"])
            self.assertIn(f"id: p{pi}_content\n            align: CENTER", page)
            for name in ("sk", "bridge", "mqtt"):
                self.assertEqual(page.count(f"id: p{pi}_dot_{name}\n"), 1)
            self.assertEqual(page.count(f"id: p{pi}_clock,"), 1)
            self.assertEqual(page.count(f"id: p{pi}_title,"), 1)
            # the touch layer stays last, above the header, so swipes work over the whole screen
            self.assertEqual(len(re.findall(r"- obj: \{width: 8, height: 8", page)), 3)

    def test_the_touch_layer_is_the_last_widget_of_every_page(self):
        text = self.ui_device()
        for pi in range(3):
            page = text.split(f"- id: page_{pi}\n")[1].split("\n    - id: page_")[0]
            self.assertLess(page.index(f"id: p{pi}_title,"), page.index(f"id: p{pi}_tap"))
            self.assertLess(page.index(f"id: p{pi}_pages"), page.index(f"id: p{pi}_tap"))
            self.assertEqual(page.count("- obj:\n            id: p%d_tap" % pi), 1)
            self.assertNotIn("\n        - ", page.split(f"id: p{pi}_tap")[1])   # nothing after it

    def test_round_geometry_fits_the_chord_of_the_circle(self):
        # width available at a y on a 480 px circle; the clock row is the narrowest
        import math
        def chord(y):
            return 2 * math.sqrt(240 ** 2 - (240 - y) ** 2)
        y = REGISTRY["boards"][BOARD]["ring"]["header_y"]
        row = render.HEADER_CLOCK_X - 22, render.HEADER_DOT_X[-1] + render.HEADER_DOT_SIZE // 2
        self.assertLess(row[1] - row[0], chord(y))
        self.assertLessEqual(max(abs(row[0]), abs(row[1])) * 2, chord(y))
        self.assertLess(render.HEADER_TITLE_WIDTH, chord(y + render.HEADER_TITLE_ROW))
        footer = REGISTRY["boards"][BOARD]["ring"]["footer_y"] + render.PAGE_DOT_SIZE + 2
        self.assertLess(8 * (render.PAGE_DOT_SIZE + render.PAGE_DOT_GAP), chord(480 - footer))

    def test_page_dots_highlight_the_current_page_and_are_absent_for_one_page(self):
        text = self.ui_device()
        for pi in range(3):
            block = text.split(f"id: p{pi}_pages")[1].split("- label:")[0].split("- obj:\n")[0]
            colours = re.findall(r"bg_color: (0x[0-9A-F]{6})", block)
            self.assertEqual(colours, ["0xFFFFFF" if j == pi else "0x37474F" for j in range(3)])
        def one(cfg):
            cfg["displays"]["helm"]["pages"] = [{"title": "One", "layout": "single", "items": ["sog"]}]
            cfg["displays"]["helm"]["ui"] = {"header": True}
        self.set_config(one)
        text = self.device(allow_missing_wifi=True)
        self.assertIn("id: p0_title", text)
        self.assertNotIn("_pages", text)

    def test_page_titles_are_literal_and_never_in_lambdas(self):
        hostile = '$x ${y} <% 1 %>"\\'
        def mutate(cfg):
            dashboard_ui(cfg)
            cfg["displays"]["helm"]["pages"][0]["title"] = hostile[:16]
        self.set_config(mutate)
        text = self.device(allow_missing_wifi=True)
        self.assertIn('id: p0_title, text: !literal "' + hostile[:16].replace("\\", "\\\\").replace('"', '\\"'), text)
        for lam in re.findall(r"lambda: [^\n]*|- lambda: \|-\n(?:\s{10,}[^\n]*\n)+", text):
            self.assertNotIn("${y}", lam)
            self.assertNotIn("<% 1 %>", lam)

    def test_unsupported_title_glyph_is_reported_only_with_a_header(self):
        def mutate(cfg, header):
            dashboard_ui(cfg, header=header)
            cfg["displays"]["helm"]["pages"][1]["title"] = "日本"
        self.set_config(lambda cfg: mutate(cfg, True))
        with self.assertRaises(render.RenderError) as ctx:
            self.collect(allow_missing_wifi=True)
        self.assertIn("pages[1].title", str(ctx.exception))
        self.set_config(lambda cfg: mutate(cfg, False))
        self.collect(allow_missing_wifi=True)   # titles are never drawn without a header

    def test_font_covers_clock_digits_and_titles_only_with_the_header(self):
        def glyphs(text):
            return re.search(r"id: font_small\n    size: 20\n    bpp: 4\n    glyphs: !literal \"(.*)\"", text).group(1)
        self.assertTrue(set("0123456789:-") <= set(glyphs(self.ui_device())))
        self.assertTrue(set("Tanks") <= set(glyphs(self.ui_device())))
        without = glyphs(self.plain_device())
        self.assertNotIn(":", without)
        self.assertNotIn("T", without)

    def test_clock_is_generated_digits_from_two_integers_and_gated(self):
        text = self.ui_device()
        handler = text.split("topic: vesselstack/v1/bridge/clock")[1].split("  on_disconnect:")[0]
        self.assertIn("int h = -1, m = -1;", handler)
        self.assertIn("if (h > 23 || m > 59) { h = -1; m = -1; }", handler)
        script = text.split("id: render_header")[1].split("\n  - id:")[0].split("\ninterval:")[0]
        self.assertIn('snprintf(buf, sizeof(buf), "%02d:%02d", hm / 60, hm % 60);', script)
        self.assertIn("(millis() - id(clock_seen)) < 15000UL", script)
        self.assertIn("bool online = id(bridge_online) && mq;", script)
        self.assertEqual(script.count("lv_label_set_text("), 3)
        self.assertNotIn("hm)", script.replace("hm / 60", "").replace("hm % 60", "").replace(
            "(hm != id", "").replace("hm < 0", "").replace("hm =", ""))
        # the clock text is only ever set from buf, never from the payload string
        self.assertNotIn("lv_label_set_text(id(p0_clock), hm", text)

    def test_clock_staleness_follows_the_heartbeat(self):
        def mutate(cfg):
            dashboard_ui(cfg)
            cfg["heartbeat_s"] = 7
            for inst in cfg["instruments"].values():
                inst["stale_after_s"] = 30
        self.set_config(mutate)
        self.assertIn("< 21000UL", self.device(allow_missing_wifi=True).split("id: render_header")[1][:600])

    def test_dots_are_good_only_when_everything_behind_them_is(self):
        script = self.ui_device().split("id: render_header")[1].split("\n  - id:")[0]
        self.assertIn("int dsk = !online ? 0 :", script)    # a retained SignalK flag needs a live bridge
        self.assertIn("int dbr = online ? 1 : 2;", script)
        self.assertIn("int dmq = mq ? 1 : 2;", script)
        self.assertIn("uint32_t sk = dsk == 1 ? good : (dsk == 2 ? bad : unk);", script)

    def test_header_subscriptions_fit_the_display_acl(self):
        rules = AclConsistency.acl_rules(None, "vsdisp-helm")
        text = self.ui_device()
        for topic in ("vesselstack/v1/bridge/clock", "vesselstack/v1/bridge/signalk"):
            self.assertEqual(text.count(f"topic: {topic}\n"), 1, topic)
            self.assertTrue(any(AclConsistency.matches(r, topic) for r in rules["read"]), topic)
            self.assertFalse(any(AclConsistency.matches(r, topic) for r in rules["write"]), topic)
        self.assertEqual(rules["write"], ["vesselstack/v1/displays/vsdisp-helm/status"])

    def test_clock_is_subscribed_for_a_schedule_without_a_header_but_signalk_is_not(self):
        text = self.plain_device(night="schedule")
        self.assertIn("topic: vesselstack/v1/bridge/clock", text)
        self.assertNotIn("bridge/signalk", text)
        self.assertNotIn("render_header", text)
        manual = self.plain_device(night="manual")
        self.assertNotIn("bridge/clock", manual)
        self.assertNotIn("night_sched", manual)

    def test_only_known_top_level_components_are_used(self):
        for text in (self.ui_device(), self.plain_device(night="manual", dim_after_s=30)):
            keys = set(re.findall(r"(?m)^([a-z_]+):", text))
            self.assertLessEqual(keys, {"substitutions", "esphome", "packages", "globals", "font", "mqtt",
                                        "script", "interval", "lvgl"})
            self.assertNotIn("mqtt.publish", text)
        self.assertEqual(self.ui_device().count("\n  top_layer:"), 1)   # the banner's, not a second one

    # -- night --------------------------------------------------------------------------------

    def test_night_palette_is_dark_red_and_keeps_states_apart(self):
        self.assertEqual(set(render.NIGHT_SEV_COLORS), {0, 1, 2, 3})
        for palette in (render.SEV_COLORS, render.NIGHT_SEV_COLORS):
            self.assertNotEqual(palette[0], palette[1])           # unknown is never drawn like ok
            self.assertEqual(len({palette[n][0] for n in range(4)}), 4)
        for name, colour in render.NIGHT.items():
            self.assertEqual(colour & 0xFF, 0, name)               # no blue at night
            self.assertLessEqual((colour >> 8) & 0xFF, 0x30, name)  # and next to no green
        for bg, fg in render.NIGHT_SEV_COLORS.values():
            self.assertEqual(bg & 0xFF, 0)
            self.assertLessEqual((bg >> 8) & 0xFF, 0x3E)
        bg, _ = render.NIGHT_SEV_COLORS[3]
        self.assertGreaterEqual(bg >> 16, 0xB0)                    # an alarm tile is still clearly red
        self.assertEqual(bg & 0xFFFF, 0)
        self.assertLess(max(render.NIGHT["tile"], render.NIGHT_SEV_COLORS[1][0]), 0x300000)

    def test_severity_lambdas_use_the_night_palette_only_when_night_is_enabled(self):
        day = self.plain_device(swipe=True)
        self.assertNotIn("night_active", day)
        text = self.ui_device()
        sev = text.split("uint8_t sev = fresh ?")[1].split("          }\n")[0]
        self.assertIn("if (id(night_active)) {", sev)
        self.assertIn("if (sev == 3) { bg = 0xB00000;", sev)
        self.assertLess(sev.index("0xC62828"), sev.index("if (id(night_active))"))

    def test_palette_flip_repaints_everything_once(self):
        text = self.ui_device()
        script = text.split("  - id: apply_night")[1].split("\n  - id:")[0]
        self.assertIn("if (n != (id(night_drawn) == 1)) {", script)
        self.assertIn("id(night_dirty) = true;", script)
        for iid in ("depth", "house_v", "anchor", "batt_low", "bilge"):
            self.assertIn(f"id(ins_{iid}_drawn_sev) = 255;", script)
        self.assertNotIn("ins_sog_drawn_sev", script)     # no thresholds: no severity colour
        self.assertIn("- script.execute: render_tiles", script)
        # every tile, name, value and unit of every page is reached through an id
        for pi, iid in ((0, "sog"), (1, "anchor"), (2, "house_v")):
            self.assertIn(f"id(p{pi}_{iid}_name)", script)
            self.assertIn(f"id(p{pi}_{iid}_value)", script)
        self.assertNotIn("p1_anchor_unit", script)
        for tile in re.findall(r"set_style_bg_color\(id\((\w+)\)", script):
            self.assertRegex(text, rf"id: {tile}\b")

    def test_the_banner_is_never_themed(self):
        text = self.ui_device()
        self.assertNotIn("alarm_banner", text.split("  - id: apply_night")[1].split("\n  - id:")[0])
        self.assertIn("bg_color: 0xC62828", text.split("top_layer:")[1])
        self.assertIn("text_color: 0xFFFFFF", text.split("top_layer:")[1])

    def test_manual_override_lasts_until_the_schedule_flag_changes(self):
        text = self.ui_device()
        self.assertIn("bool n = id(night_override) >= 0 ? id(night_override) == 1 : base;",
                      text)
        self.assertIn("bool base = id(night_sched);", text)
        clock = text.split("topic: vesselstack/v1/bridge/clock")[1].split("  on_disconnect:")[0]
        self.assertIn("if (n != id(night_sched)) { id(night_sched) = n; id(night_override) = -1; }", clock)
        self.assertIn('x["night"].is<bool>()', clock)
        self.assertIn("id(night_override) = id(night_active) ? 0 : 1;", text)
        # the flag is only trusted together with a valid time
        self.assertIn("if (h >= 0 && x[", clock)

    def test_manual_mode_ignores_the_schedule(self):
        text = self.plain_device(night="manual")
        self.assertIn("bool base = false;", text)
        self.assertNotIn("night_sched", text)

    def test_stale_clock_keeps_the_last_night_state(self):
        # the night flag is held, not reset, when the clock goes stale (documented behaviour)
        script = self.ui_device().split("  - id: apply_night")[1].split("\n  - id:")[0]
        self.assertNotIn("clock_seen", script)
        self.assertNotIn("millis()", script)

    # -- backlight ----------------------------------------------------------------------------

    def power(self, text):
        return text.split("  - id: apply_power")[1].split("\n  - id:")[0].split("\ninterval:")[0]

    def test_one_controller_orders_day_night_dim_and_banner(self):
        script = self.power(self.ui_device())
        order = [script.index(s) for s in ("int target = 100;", "if (id(night_active)) target = 15;",
                                           "if (id(dim_idle) && target > 15) target = 15;",
                                           "LV_OBJ_FLAG_HIDDEN)) target = 100;", "if (target != id(bl_drawn))")]
        self.assertEqual(order, sorted(order))
        self.assertIn("call.set_brightness(target / 100.0f);", script)

    def test_default_brightness_values(self):
        script = self.power(self.plain_device(night="manual", dim_after_s=10))
        self.assertIn("int target = 100;", script)
        self.assertIn("target = 20;", script)                     # night_brightness default
        self.assertIn("target > 10) target = 10;", script)        # dimming without night_brightness: 10 %
        custom = self.power(self.plain_device(night="manual", day_brightness=80, night_brightness=7,
                                              dim_after_s=10))
        self.assertIn("int target = 80;", custom)
        self.assertIn("target = 7;", custom)
        self.assertIn("target > 7) target = 7;", custom)

    def test_no_banner_term_without_takeover(self):
        script = self.power(self.plain_device(night="manual"))
        self.assertNotIn("alarm_banner", script)
        self.assertNotIn("dim_idle", script)

    def test_takeover_keeps_the_controller_in_step_with_its_direct_100_percent(self):
        text = self.ui_device()
        handler = text.split("topic: vesselstack/v1/alarms/summary")[1].split("on_disconnect")[0]
        self.assertIn("brightness: 100%\n              - lambda: 'id(bl_drawn) = 100;'", handler)
        plain = self.alarm_text()
        self.assertNotIn("bl_drawn", plain)
        self.assertIn("brightness: 100%\n        - script.execute: render_alarm", plain)

    def alarm_text(self):
        self.set_config(alarm_takeover)
        return self.device(allow_missing_wifi=True)

    def test_dimming_polls_the_touch_idle_time_and_never_pauses_lvgl(self):
        text = self.ui_device()
        self.assertIn("  - interval: 250ms\n    then:\n      - if:\n          condition:\n"
                      "            lvgl.is_idle:\n              timeout: 120s\n", text)
        self.assertNotIn("lvgl.pause", text)
        self.assertNotIn("lvgl.resume", text)
        self.assertNotIn("on_idle", text)
        self.assertEqual(text.count("interval: 250ms"), 1)
        self.assertNotIn("interval: 250ms", self.plain_device(night="manual"))

    def test_controller_runs_each_second_and_after_every_change(self):
        text = self.ui_device()
        tick = text.split("  - interval: 1s")[1].split("  - interval: 500ms")[0]
        for step in ("render_tiles", "render_alarm", "apply_night", "render_header", "apply_power"):
            self.assertIn(f"- script.execute: {step}", tick, step)
        self.assertEqual(text.count("  - interval: 1s"), 1)
        clock = text.split("topic: vesselstack/v1/bridge/clock")[1].split("  on_disconnect:")[0]
        for step in ("apply_night", "render_header", "apply_power"):
            self.assertIn(f"- script.execute: {step}", clock, step)
        press = text.split("on_long_press:")[1].split("\n        - obj:")[0]
        self.assertIn("- script.execute: apply_night", press)
        self.assertIn("- script.execute: apply_power", press)

    def test_only_changes_reach_lvgl(self):
        text = self.ui_device()
        self.assertIn("if (hm != id(clock_drawn)) {", text)
        self.assertIn("if (state != id(hdr_drawn)) {", text)
        self.assertIn("if (target != id(bl_drawn)) {", text)
        self.assertIn("if (n != (id(night_drawn) == 1)) {", text)

    def test_backlight_still_publishes_nothing(self):
        self.assertNotIn("mqtt.publish", self.ui_device())
        board = self.collect().files["helm/boards/waveshare-esp32-s3-touch-lcd-2.8c.yaml"].decode()
        self.assertRegex(board, r"id: board_backlight\n\s+name: Backlight\n\s+internal: true")


class Gauges(Base):
    """tank-bars and gauge-arc (display dashboard slice S6)."""

    def gauge_device(self, mutate=None, **kw):
        def build(cfg):
            gauge_pages(cfg, **kw)
            if mutate:
                mutate(cfg)
        self.set_config(build)
        return self.device(allow_missing_wifi=True)

    def lambdas(self, text):
        return "\n".join(l for l in text.splitlines() if "lambda" in l or "lv_" in l or "id(" in l)

    def page(self, text, pi):
        return text.split(f"- id: page_{pi}\n")[1].split("\n    - id: page_")[0]

    def test_nothing_is_generated_without_the_layouts(self):
        text = self.device(allow_missing_wifi=True)
        for marker in ("lv_bar", "lv_arc", "font_value_bar", "_num,", "_gdrawn", "- bar:", "- arc:"):
            self.assertNotIn(marker, text)

    # -- scaling ------------------------------------------------------------------------------

    def test_bar_percentage_rounds_half_away_and_clamps(self):
        cases = [(0, 0), (100, 100), (62.4, 62), (62.5, 63), (62.6, 63), (-5, 0), (140, 100), (0.4, 0), (99.5, 100)]
        for value, expected in cases:
            self.assertEqual(render.bar_percent(value, 0, 100), expected, value)
        self.assertEqual(render.bar_percent(12.0, 10, 16), 33)        # not 0..100: (12-10)/(16-10)
        self.assertEqual(render.bar_percent(0, -10, 10), 50)
        self.assertEqual(render.bar_percent(float("nan"), 0, 100), 0)
        self.assertEqual(render.bar_percent(1e15, 0, 100), 100)
        self.assertEqual(render.bar_percent(-1e15, 0, 100), 0)

    def test_arc_permille_scales_to_1000_and_clamps(self):
        for value, expected in [(0, 0), (100, 1000), (62.5, 625), (0.04, 0), (0.05, 1), (-3, 0), (101, 1000)]:
            self.assertEqual(render.arc_permille(value, 0, 100), expected, value)
        self.assertEqual(render.arc_permille(13, 10, 16), 500)

    def test_firmware_uses_the_same_formula_with_float_constants(self):
        text = self.gauge_device(lambda cfg: cfg["instruments"]["water"].update(min=-2.5, max=97.5))
        lam = text.split("float n = id(ins_water_num);")[1].split("uint8_t g")[0]
        self.assertIn("float pb = fresh ? 100.0f * (n - -2.5f) / (97.5f - -2.5f) : 0.0f;", lam)
        self.assertIn("if (!(pb > 0.0f)) pb = 0.0f;", lam)
        self.assertIn("if (pb > 100.0f) pb = 100.0f;", lam)
        self.assertIn("lv_bar_set_value(id(p0_water_bar), lroundf(pb), LV_ANIM_OFF);", lam)
        fuel = text.split("float n = id(ins_fuel_num);")[1].split("uint8_t g")[0]
        self.assertIn("float pa = fresh ? 1000.0f * (n - 0.0f) / (100.0f - 0.0f) : 0.0f;", fuel)
        self.assertIn("lv_arc_set_value(id(p1_arc), lroundf(pa));", fuel)
        self.assertIn("lv_bar_set_value(id(p0_fuel_bar), lroundf(pb), LV_ANIM_OFF);", fuel)   # both on one instrument

    def test_the_number_comes_from_the_payload_not_from_text(self):
        text = self.gauge_device()
        for iid in ("fuel", "water", "waste", "lpg"):
            handler = text.split(f"instruments/{iid}\n")[1].split("    - topic:")[0]
            self.assertIn(f'id(ins_{iid}_num) = x["v"].as<float>();', handler)
            self.assertIn(f"{{id: ins_{iid}_num, type: float, initial_value: \"0\"}}", text)
        self.assertNotIn("ins_sog_num", text)

    # -- zones and geometry -------------------------------------------------------------------

    def test_zone_angles_follow_thresholds_min_and_max(self):
        both = {"thresholds": {"alarm_below": 10, "warn_below": 25}, "min": 0, "max": 100}
        self.assertEqual(render.zone_arcs(both, 135), [(3, 135, 162), (2, 162, 203)])
        self.assertEqual(render.zone_arcs(both, 315), [(3, 315, 342), (2, 342, 23)])
        high = {"thresholds": {"warn_above": 75, "alarm_above": 90}, "min": 0, "max": 100}
        self.assertEqual(render.zone_arcs(high, 135), [(2, 338, 18), (3, 18, 45)])    # wraps through 0
        warn_only = {"thresholds": {"warn_below": 20}, "min": 0, "max": 100}
        self.assertEqual(render.zone_arcs(warn_only, 135), [(2, 135, 189)])
        window = {"thresholds": {"alarm_below": 11.8, "warn_below": 12.2, "warn_above": 14.8,
                                 "alarm_above": 15.2}, "min": 10, "max": 16}
        self.assertEqual([z[0] for z in render.zone_arcs(window, 135)], [3, 2, 2, 3])
        self.assertEqual(render.zone_arcs({"min": 0, "max": 100}, 135), [])           # no thresholds, no zones

    def test_zones_outside_the_range_or_too_short_are_dropped(self):
        inst = {"thresholds": {"alarm_below": -5, "warn_below": 0.1}, "min": 0, "max": 100}
        self.assertEqual(render.zone_arcs(inst, 135), [])                              # 0.27 deg rounds to nothing
        above = {"thresholds": {"warn_above": 120}, "min": 0, "max": 100}
        self.assertEqual(render.zone_arcs(above, 135), [])
        clipped = {"thresholds": {"warn_above": 80, "alarm_above": 400}, "min": 0, "max": 100}
        self.assertEqual(render.zone_arcs(clipped, 135), [(2, 351, 45)])               # alarm zone has no length

    def test_zone_arcs_are_generated_inside_the_track(self):
        text = self.gauge_device()
        page = self.page(text, 1)
        self.assertEqual(re.findall(r"id: (p1_zone\d)", page), ["p1_zone0", "p1_zone1"])
        self.assertIn("start_angle: 135\n            end_angle: 162\n            arc_width: 6\n"
                      "            arc_color: 0xC62828", page)
        zone = next(l for l in page.split("- arc:") if "p1_zone0" in l)
        self.assertEqual(int(re.search(r"width: (\d+)", zone).group(1)),
                         440 - 2 * (render.ARC_WIDTH + render.ZONE_GAP))
        self.assertIn("arc_opa: TRANSP", zone)
        self.assertIn("clickable: false", zone)

    def test_arc_geometry_and_angles(self):
        board = REGISTRY["boards"][BOARD]
        self.assertEqual(render.gauge_diameter(board), 440)
        page = self.page(self.gauge_device(), 1)
        arc = page.split("- arc:")[1]
        for line in ("id: p1_arc", "width: 440", "height: 440", "align: CENTER", "clickable: false",
                     "min_value: 0", "max_value: 1000", "start_angle: 135", "end_angle: 45",
                     "adjustable: false", "arc_width: 24"):
            self.assertIn(line, arc)
        knob = arc.split("knob:")[1]
        self.assertIn("bg_opa: TRANSP", knob)
        self.assertEqual((render.arc_start_angle({}), render.arc_end_angle(135)), (135, 45))
        self.assertEqual(render.arc_end_angle(315), 225)
        self.assertEqual(render.ARC_SWEEP, 270)

    def band_points(self, start, outer=220.0, width=24):
        """Points (x, y) on the screen of the arc band, sampled each degree of the sweep."""
        pts = []
        for step in range(render.ARC_SWEEP + 1):
            angle = math.radians(start + step)
            for radius in (outer, outer - width):
                pts.append((240 + radius * math.cos(angle), 240 + radius * math.sin(angle)))
        return pts

    def header_regions(self):
        ring = REGISTRY["boards"][BOARD]["ring"]
        half = render.HEADER_TITLE_WIDTH / 2
        return [(240 - half, ring["header_y"], 240 + half, ring["header_y"] + ring["header_h"]),    # clock, dots, title
                (240 - 64, ring["footer_y"], 240 + 64, ring["footer_y"] + render.PAGE_DOT_SIZE + 2)]  # page dots

    def test_with_the_header_the_gap_is_at_the_top_and_clears_it(self):
        header = render.arc_start_angle({"ui": {"header": True}})
        self.assertEqual(header, 315)
        for x, y in self.band_points(header):
            for x0, y0, x1, y1 in self.header_regions():
                self.assertFalse(x0 <= x <= x1 and y0 <= y <= y1, (x, y))
        # the outer edge stays on the 480 px screen
        self.assertTrue(all(math.hypot(x - 240, y - 240) <= 220.01 for x, y in self.band_points(header)))
        # the default orientation (gap at the bottom) would run straight through the header
        hit = [(x, y) for x, y in self.band_points(135)
               if self.header_regions()[0][0] <= x <= self.header_regions()[0][2]
               and self.header_regions()[0][1] <= y <= self.header_regions()[0][3]]
        self.assertTrue(hit)

    def test_the_header_gap_is_wide_enough_for_the_title_row(self):
        board = REGISTRY["boards"][BOARD]
        ring = board["ring"]
        lowest = ring["header_y"] + ring["header_h"]
        for angle in (render.arc_end_angle(315), 315):                  # the two ends of the arc
            outer_y = 240 + 220 * math.sin(math.radians(angle))
            self.assertGreater(outer_y, lowest)

    def test_header_and_gauge_arc_render_together(self):
        text = self.gauge_device(header=True)
        page = self.page(text, 1)
        self.assertIn("start_angle: 315", page)
        self.assertIn("end_angle: 225", page)
        self.assertIn("id: p1_clock,", page)
        self.assertLess(page.index("id: p1_arc"), page.index("id: p1_tap"))
        self.assertEqual(self.page(self.gauge_device(), 1).count("start_angle: 135"), 2)   # the arc and the first zone

    def test_bars_fit_the_content_box(self):
        text = self.gauge_device()
        page = self.page(text, 0)
        self.assertEqual(page.count("- bar:"), 4)
        widths = [int(w) for w in re.findall(r"- bar:\n\s+id: \S+\n\s+width: (\d+)", page)]
        self.assertEqual(widths, [60] * 4)
        heights = {int(h) for h in re.findall(r"- bar:\n\s+id: \S+\n\s+width: \d+\n\s+height: (\d+)", page)}
        self.assertEqual(heights, {render.BAR_HEIGHT})
        self.assertTrue(200 <= render.BAR_HEIGHT <= 220)
        cols = [int(w) for w in re.findall(r"- obj:\n\s+width: (\d+)\n\s+height: 100%", page)]
        self.assertEqual(cols, [85] * 4)
        self.assertLessEqual(sum(cols), 340)
        self.assertIn("flex_align_main: SPACE_EVENLY", page)
        self.assertIn("id: p0_content\n            align: CENTER\n            width: 340\n            height: 340", page)
        # label + bar + value row stack inside the 340 px column
        stack = render.FONT_SIZES["small"] + render.FONT_SIZES["bar"] * 1.2 + render.BAR_HEIGHT + 2 * render.BAR_PAD_ROW
        self.assertLess(stack, 340)
        for count, width in ((1, 340), (2, 170), (3, 113), (4, 85)):
            self.assertEqual(render.bar_column_width(340, count), width)
            self.assertGreaterEqual(render.bar_column_width(340, count), render.BAR_WIDTH)

    def test_one_bar_alone_is_laid_out_and_valid(self):
        def mutate(cfg):
            cfg["displays"]["helm"]["pages"][0]["items"] = ["fuel"]
        text = self.gauge_device(mutate)
        self.assertEqual(self.page(text, 0).count("- bar:"), 1)
        self.assertIn("width: 340\n                  height: 100%", self.page(text, 0))

    # -- colours, stale behaviour -------------------------------------------------------------

    def test_indicator_colour_follows_severity_and_goes_grey_when_not_fresh(self):
        text = self.gauge_device()
        lam = text.split("float n = id(ins_fuel_num);")[1].split("          }\n")[0]
        self.assertIn("uint8_t g = fresh ? id(ins_fuel_sev) : 0;", lam)
        self.assertIn("if (g != id(ins_fuel_gdrawn)) {", lam)
        self.assertIn("uint32_t c = 0x37474F;", lam)
        for number, colour in ((1, "1E7B34"), (2, "F2A900"), (3, "C62828")):
            self.assertIn(f"if (g == {number}) c = 0x{colour};", lam)
        self.assertIn("lv_obj_set_style_bg_color(id(p0_fuel_bar), lv_color_hex(c), LV_PART_INDICATOR);", lam)
        self.assertIn("lv_obj_set_style_arc_color(id(p1_arc), lv_color_hex(c), LV_PART_INDICATOR);", lam)
        self.assertEqual(lam.count("0x1E7B34"), 1)               # green only for sev ok
        fresh = text.split("float n = id(ins_fuel_num);")[0].rsplit("bool fresh = ", 1)[1].split(";")[0]
        for term in ("id(bridge_online)", "mqtt::global_mqtt_client->is_connected()", "id(ins_fuel_ok)",
                     "millis() - id(ins_fuel_seen)) < 70000UL"):
            self.assertIn(term, fresh)
        self.assertIn("fresh ?", lam.split("float pb")[1].split(";")[0])                  # level 0 when stale

    def test_stale_text_is_the_existing_blank_and_the_unit_is_a_literal(self):
        text = self.gauge_device()
        tiles = text.split("  - id: render_tiles")[1].split("\n  - id:")[0]
        self.assertIn("id: p0_fuel_value\n          text: !lambda", tiles)
        self.assertIn("id: p1_fuel_value\n          text: !lambda", tiles)
        self.assertIn('return fresh ? id(ins_fuel_val) : std::string("--");', tiles)
        self.assertIn('id: p0_fuel_value, text: "--"', text)
        self.assertIn('id: p1_fuel_value, text: "--"', text)

    def test_without_thresholds_the_indicator_is_neutral_never_green(self):
        text = self.gauge_device()
        lam = text.split("float n = id(ins_lpg_num);")[1].split("          }\n")[0]
        self.assertIn("uint8_t g = fresh ? 4 : 0;", lam)
        self.assertIn("if (g == 4) c = 0x2E86C1;", lam)
        self.assertNotIn("ins_lpg_sev", text)
        self.assertNotIn("ins_lpg_drawn_sev", text)            # no tile colour either
        self.assertNotEqual(render.GAUGE_DAY[render.GAUGE_NEUTRAL], render.GAUGE_DAY[1])
        self.assertNotEqual(render.GAUGE_NIGHT[render.GAUGE_NEUTRAL], render.GAUGE_NIGHT[1])
        self.assertEqual(len(set(render.GAUGE_DAY.values())), 5)
        self.assertEqual(len(set(render.GAUGE_NIGHT.values())), 5)

    def test_an_instrument_on_a_tile_and_a_bar_keeps_both_colour_paths_apart(self):
        def mutate(cfg):
            cfg["displays"]["helm"]["pages"].append({"title": "Tiles", "layout": "grid-2x2", "items": ["fuel", "water"]})
        text = self.gauge_device(mutate)
        sev = text.split("uint8_t sev = fresh ?")[1].split("          }\n")[0]
        self.assertIn("id(p2_fuel_tile)", sev)
        self.assertNotIn("_bar", sev)
        self.assertNotIn("p0_", sev)
        self.assertIn("ins_fuel_drawn_sev", text)
        self.assertIn("ins_fuel_gdrawn", text)
        ids = set(re.findall(r"\bid: (\w+)", text))
        for ref in set(re.findall(r"id\((p\d+_\w+)\)", text)):
            self.assertIn(ref, ids, ref)

    def test_a_gauge_only_instrument_gets_no_tile_colour_code(self):
        text = self.gauge_device()
        self.assertNotIn("uint8_t sev = fresh", text)
        self.assertNotIn("drawn_sev", text)
        self.assertIn("ins_fuel_sev", text)                                    # still read by the gauge

    def test_every_referenced_id_exists(self):
        for kw in ({}, {"header": True, "night": True}):
            text = self.gauge_device(**kw)
            ids = set(re.findall(r"\bid: (\w+)", text)) | set(re.findall(r"\{id: (\w+)", text))
            for ref in set(re.findall(r"id\((\w+)\)", text)) - {"board_backlight"}:   # the board package's
                self.assertIn(ref, ids, ref)

    # -- night, header ------------------------------------------------------------------------

    def test_night_repaints_tracks_zones_text_and_indicators(self):
        text = self.gauge_device(night=True)
        script = text.split("  - id: apply_night")[1].split("\n  - id:")[0]
        for needle in ("lv_obj_set_style_bg_color(id(p0_fuel_bar), lv_color_hex(n ? 0x1A0800 : 0x263238), LV_PART_MAIN);",
                       "lv_obj_set_style_arc_color(id(p1_arc), lv_color_hex(n ? 0x1A0800 : 0x263238), LV_PART_MAIN);",
                       "lv_obj_set_style_arc_color(id(p1_zone0), lv_color_hex(n ? 0xC00000 : 0xC62828), LV_PART_MAIN);",
                       "lv_obj_set_style_arc_color(id(p1_zone1), lv_color_hex(n ? 0xB06A00 : 0xF2A900), LV_PART_MAIN);",
                       "id(p0_lpg_name)", "id(p0_lpg_value)", "id(p0_lpg_unit)", "id(p1_fuel_name)",
                       "id(p1_fuel_value)", "id(p1_fuel_unit)"):
            self.assertIn(needle, script)
        for iid in ("fuel", "water", "waste", "lpg"):
            self.assertIn(f"id(ins_{iid}_gdrawn) = 255;", script)
        self.assertNotIn("drawn_sev", script)
        lam = text.split("float n = id(ins_fuel_num);")[1].split("          }\n")[0]
        self.assertIn("if (id(night_active)) {", lam)
        self.assertLess(lam.index("0xC62828"), lam.index("if (id(night_active))"))
        self.assertIn("if (g == 3) c = 0xC00000;", lam)
        self.assertIn("id: p0_fuel_name, ", text)                  # static labels reachable by the palette

    def test_no_night_code_without_night(self):
        text = self.gauge_device()
        self.assertNotIn("night_active", text)
        self.assertNotIn("id: p0_fuel_name", text)

    def test_night_gauge_palette_has_no_blue_and_stays_dim(self):
        for colour in list(render.GAUGE_NIGHT.values()) + [render.TRACK_COLOR["night"]]:
            self.assertEqual(colour & 0xFF, 0)
            self.assertLessEqual((colour >> 8) & 0xFF, 0x6A)

    def test_bars_stay_inside_the_content_box_below_the_header(self):
        text = self.gauge_device(header=True)
        page = self.page(text, 0)
        board = REGISTRY["boards"][BOARD]
        top = (board["height"] - board["content_height"]) // 2
        self.assertGreaterEqual(top, board["ring"]["header_y"] + board["ring"]["header_h"])
        self.assertIn("id: p0_content\n            align: CENTER\n            width: 340", page)
        self.assertEqual(page.count("id: p0_title,"), 1)
        self.assertLess(page.index("id: p0_content"), page.index("id: p0_clock"))
        self.assertLess(page.index("id: p0_pages"), page.index("id: p0_tap"))

    # -- text ---------------------------------------------------------------------------------

    def test_hostile_labels_and_units_stay_literal_and_out_of_lambdas(self):
        hostile = {"fuel": ("$x ${y} \"q\" \\ #:", "${u}"), "water": ("<% 1+1 %>", "$$")}

        def mutate(cfg):
            for iid, (label, unit) in hostile.items():
                cfg["instruments"][iid].update(label=label[:16], unit=unit)
        text = self.gauge_device(mutate)
        for iid, (label, unit) in hostile.items():
            self.assertIn(f"text: !literal {render.yq(label[:16])}", text)
            self.assertIn(f"text: !literal {render.yq(unit)}", text)
        inside = "".join(l for l in text.splitlines() if "lambda" in l or "lv_" in l or "snprintf" in l)
        for label, unit in hostile.values():
            self.assertNotIn(label[:16], inside)
            self.assertNotIn(unit, inside)
        for line in text.splitlines():
            if "${y}" in line or "<% 1+1" in line:
                self.assertTrue(line.lstrip().startswith("- label:") or "glyphs:" in line, line)

    def test_unsupported_glyph_in_a_gauge_label_is_still_reported(self):
        self.set_config(lambda cfg: (gauge_pages(cfg), cfg["instruments"]["fuel"].update(label="Fuel \u2603")))
        with self.assertRaises(render.RenderError) as caught:
            self.collect(allow_missing_wifi=True)
        self.assertIn("U+2603", str(caught.exception))

    def test_fonts_exist_only_for_the_layouts_that_need_them(self):
        both = self.gauge_device()
        self.assertIn("id: font_value_bar\n    size: 36", both)
        self.assertIn("id: font_value_single\n    size: 96", both)
        for fid in ("font_value_bar", "font_value_single"):
            glyphs = re.search(rf'id: {fid}\n    size: \d+\n    bpp: 4\n    glyphs: !literal (".*")', both)
            self.assertEqual(json.loads(glyphs.group(1)), "0123456789.- ")
        bold = {chr(c) for c in render.font_coverage("bold")}
        self.assertTrue(set(render.VALUE_GLYPHS) <= bold)
        self.assertNotIn("font_value_grid", both)
        bars_only = self.gauge_device(lambda cfg: cfg["displays"]["helm"].update(pages=cfg["displays"]["helm"]["pages"][:1]))
        self.assertIn("font_value_bar", bars_only)
        self.assertNotIn("font_value_single", bars_only)
        arc_only = self.gauge_device(lambda cfg: cfg["displays"]["helm"].update(pages=cfg["displays"]["helm"]["pages"][1:]))
        self.assertNotIn("font_value_bar", arc_only)
        self.assertIn("font_value_single", arc_only)
        self.assertIn("id: font_small", both)
        # the value row fits a 85 px column: "100" plus the unit
        self.assertLess(3 * render.FONT_SIZES["bar"] * 0.5 + 25, render.bar_column_width(340, 4))

    def test_gauge_page_in_a_larger_display_keeps_the_other_pages_unchanged(self):
        def mutate(cfg):
            cfg["displays"]["helm"]["pages"] = ([{"title": "Nav", "layout": "grid-2x2",
                                                  "items": ["sog", "depth", "awa", "house_v"]}]
                                                + cfg["displays"]["helm"]["pages"])
        text = self.gauge_device(mutate)
        base = self.device(allow_missing_wifi=True)
        self.assertEqual(self.page(text, 0).replace("\n", ""), self.page(base, 0).replace("\n", ""))

    def test_subscribes_to_the_gauge_instruments_only(self):
        text = self.gauge_device()
        self.assertEqual(sorted(re.findall(r"- topic: vesselstack/v1/instruments/(\w+)", text)),
                         ["fuel", "lpg", "waste", "water"])

    def test_registry_lists_the_new_layouts_and_keeps_the_default_page(self):
        board = REGISTRY["boards"][BOARD]
        self.assertEqual(board["layouts"][:3], ["single", "grid-2x2", "status-grid"])
        self.assertEqual(REGISTRY["layouts"]["tank-bars"], {"capacity": 4, "min_items": 1,
                                                            "shapes": ["round", "rect"], "types": ["instrument", "ha"]})
        self.assertEqual(REGISTRY["layouts"]["gauge-arc"], {"capacity": 1, "min_items": 1,
                                                            "shapes": ["round"], "types": ["instrument", "ha"]})
        page = lifecycle.default_page(REGISTRY, EXAMPLE, BOARD)
        self.assertEqual(page["layout"], "grid-2x2")           # tank-bars needs min/max: never a default


class Electrical(Base):
    """The electrical layout (display dashboard slice S7)."""

    def elec_device(self, mutate=None, **kw):
        def build(cfg):
            electrical_pages(cfg, **kw)
            if mutate:
                mutate(cfg)
        self.set_config(build)
        return self.device(allow_missing_wifi=True)

    def page(self, text, pi):
        return text.split(f"- id: page_{pi}\n")[1].split("\n    - id: page_")[0]

    def label(self, text, wid):
        """The one-line label widget with this id."""
        return next(l for l in text.splitlines() if f"id: {wid}," in l)

    def test_nothing_is_generated_without_the_layout(self):
        text = self.device(allow_missing_wifi=True)
        for marker in ("CHG", "DSG", "_word", "_sdrawn", "- arc:", "_num,"):
            self.assertNotIn(marker, text)

    # -- slots ----------------------------------------------------------------------------------

    def test_items_fill_the_slots_in_order(self):
        text = self.elec_device()
        minimal, shore, full = (self.page(text, i) for i in range(3))
        self.assertIn("text_font: font_value_grid", self.label(minimal, "p0_batt_v_value"))      # voltage: big digits
        for wid in ("p0_batt_soc_value", "p0_batt_a_value"):
            self.assertIn("text_font: font_small", self.label(minimal, wid))
        self.assertIn("id: p0_batt_a_word,", minimal)                                          # only the current has a word
        self.assertNotIn("_word", minimal.replace("p0_batt_a_word", ""))
        self.assertNotIn("_tile", minimal)                                                     # no pill without shore
        self.assertIn("id: p1_shore_a_tile", shore)
        self.assertIn("id: p2_shore_tile", full)
        self.assertIn("id: p2_solar_a_value", full)
        self.assertNotIn("solar", shore)
        order = [full.index(f"id: p2_{iid}_value") for iid in ("batt_v", "batt_soc", "batt_a", "shore", "solar_a")]
        self.assertEqual(order, sorted(order))                                                 # top to bottom

    def test_the_ring_is_the_gauge_arc_ring_of_the_soc(self):
        text = self.elec_device(pages=("min",))
        arc = lambda t: "".join(t.split("- obj:")[0].split("widgets:\n")[1:])   # noqa: E731 - widgets before the first obj

        def gauge(cfg):
            cfg["displays"]["helm"]["pages"] = [{"title": "Power", "layout": "gauge-arc", "items": ["batt_soc"]}]
        gtext = self.elec_device(gauge)
        self.assertEqual(arc(self.page(text, 0)), arc(self.page(gtext, 0)))
        self.assertEqual(re.findall(r"id: (p0_zone\d)", self.page(text, 0)), ["p0_zone0", "p0_zone1"])
        self.assertIn("lv_arc_set_value(id(p0_arc), lroundf(pa));", text)
        self.assertIn("float n = id(ins_batt_soc_num);", text)
        self.assertNotIn("ins_batt_v_num", text)                                               # voltage needs only text
        self.assertNotIn("ins_batt_v_gdrawn", text)
        self.assertEqual(text.count("lv_arc_set_value"), 1)

    def test_soc_ring_colours_follow_severity_and_neutral_blue_without_thresholds(self):
        text = self.elec_device(pages=("min",))
        lam = text.split("float n = id(ins_batt_soc_num);")[1].split("          }\n")[0]
        self.assertIn("uint8_t g = fresh ? id(ins_batt_soc_sev) : 0;", lam)
        self.assertIn("uint32_t c = 0x37474F;", lam)
        self.assertEqual(lam.count("0x1E7B34"), 1)

        def plain(cfg):
            del cfg["instruments"]["batt_soc"]["thresholds"]
        text = self.elec_device(plain, pages=("min",))
        lam = text.split("float n = id(ins_batt_soc_num);")[1].split("          }\n")[0]
        self.assertIn("uint8_t g = fresh ? 4 : 0;", lam)
        self.assertNotIn("ins_batt_soc_sev", text)
        self.assertNotIn("p0_zone0", text)

    # -- current: CHG / DSG ---------------------------------------------------------------------

    def current_branches(self, text):
        return re.findall(r"return \((.*)\) == (\d);'\n\s+then:\n\s+- lvgl.label.update:\n\s+id: (p\d_batt_a_word)\n"
                          r"\s+text: !literal \"(\w*)\"", text)

    def test_chg_dsg_and_empty_are_three_exhaustive_literal_branches(self):
        text = self.elec_device(pages=("min",))
        branches = self.current_branches(text)
        self.assertEqual([(n, w) for _, n, _, w in branches], [("1", "CHG"), ("2", "DSG"), ("0", "")])
        self.assertEqual({cond for cond, _, _, _ in branches}.__len__(), 1)       # one gated state for all three
        cond = branches[0][0]
        self.assertIn("? (id(ins_batt_a_num) > 0.05f ? 1 : id(ins_batt_a_num) < -0.05f ? 2 : 0) : 0", cond)
        self.assertEqual(render.CURRENT_DEADBAND, 0.05)
        self.assertEqual(render.CURRENT_WORDS, {1: "CHG", 2: "DSG", 0: ""})
        # the words are never built at run time
        self.assertNotIn("snprintf", text.split("  - id: render_tiles")[1].split("\n  - id: apply_night")[0])

    def test_the_current_word_is_gated_on_freshness_so_stale_means_empty(self):
        cond = self.current_branches(self.elec_device(pages=("min",)))[0][0]
        for term in ("id(bridge_online)", "mqtt::global_mqtt_client->is_connected()", "id(ins_batt_a_ok)",
                     "(millis() - id(ins_batt_a_seen)) < 40000UL"):
            self.assertIn(term, cond)
        self.assertLess(cond.index("(millis()"), cond.index("id(ins_batt_a_num)"))
        # the word shown when stale is the empty one, in the state-0 branch
        self.assertIn("? ", cond)
        self.assertTrue(cond.rstrip().endswith(": 0"))

    def test_deadband_boundaries_match_the_firmware_expression(self):
        def word(n):
            return "CHG" if n > render.CURRENT_DEADBAND else ("DSG" if n < -render.CURRENT_DEADBAND else "")
        self.assertEqual([word(n) for n in (0.06, 0.05, 0.0, -0.05, -0.06, 12.3, -40)],
                         ["CHG", "", "", "", "DSG", "CHG", "DSG"])

    def test_the_value_text_is_signed_and_blanks_when_stale(self):
        text = self.elec_device(pages=("min",))
        tiles = text.split("  - id: render_tiles")[1].split("\n  - id:")[0]
        self.assertIn("id: p0_batt_a_value\n          text: !lambda", tiles)
        self.assertIn('return fresh ? id(ins_batt_a_val) : std::string("--");', tiles)
        self.assertIn('id: p0_batt_a_value, text: "--"', text)

    def test_negate_is_a_valid_instrument_conversion_for_the_current(self):
        def negate(cfg):
            cfg["instruments"]["batt_a"]["convert"] = "negate"
        text = self.elec_device(negate, pages=("min",))
        self.assertIn("id: p0_batt_a_word", text)                          # the firmware never sees the sign flip
        self.assertEqual(schema.validate_config(json.loads((self.root / "config" / "displays.json").read_text()),
                                                REGISTRY), [])

    # -- shore ----------------------------------------------------------------------------------

    def test_a_notification_pill_reuses_the_severity_words_and_colours(self):
        text = self.elec_device(pages=("full",))
        sev = text.split("uint8_t sev = fresh ?")[1].split("          }\n")[0]
        self.assertIn("lv_obj_set_style_bg_color(id(p0_shore_tile), lv_color_hex(bg), LV_PART_MAIN);", sev)
        self.assertIn("lv_obj_set_style_text_color(id(p0_shore_value), lv_color_hex(fg), LV_PART_MAIN);", sev)
        self.assertEqual(sev.count("0x1E7B34"), 1)
        words = re.findall(r"== (\d);'\n\s+then:\n\s+- lvgl.label.update:\n\s+id: p0_shore_value\n\s+text: !literal \"([A-Z-]+)\"",
                           text)
        self.assertEqual(words, [("3", "ALARM"), ("2", "WARN"), ("1", "OK"), ("0", "--")])
        self.assertIn("? id(ins_shore_sev) : 0) == 1", text)                     # OK only from a fresh severity
        self.assertNotIn("ins_shore_sdrawn", text)                                # no instrument pill code
        self.assertNotIn("ins_shore_num", text)

    def test_an_instrument_pill_reads_on_above_the_threshold_and_off_otherwise(self):
        text = self.elec_device(pages=("shore",))
        self.assertEqual(render.SHORE_ON_ABOVE, 0.5)
        words = re.findall(r"return \((.*)\) == (\d);'\n\s+then:\n\s+- lvgl.label.update:\n\s+id: p0_shore_a_value\n"
                           r"\s+text: !literal \"([A-Z-]+)\"", text)
        self.assertEqual([(n, w) for _, n, w in words], [("1", "ON"), ("2", "OFF"), ("0", "--")])
        cond = words[0][0]
        self.assertIn("? (id(ins_shore_a_num) > 0.5f ? 1 : 2) : 0", cond)
        for term in ("id(bridge_online)", "id(ins_shore_a_ok)", "(millis() - id(ins_shore_a_seen)) < 40000UL"):
            self.assertIn(term, cond)
        # the number never reaches the pill text
        tiles = text.split("  - id: render_tiles")[1].split("\n  - id:")[0]
        self.assertNotIn("ins_shore_a_val", tiles)
        self.assertNotIn("id: p0_shore_a_value\n          text: !lambda", tiles)

    def test_the_instrument_pill_is_green_only_when_fresh_and_on(self):
        text = self.elec_device(pages=("shore",))
        lam = text.split("uint8_t s = fresh ?")[1].split("          }\n")[0]
        self.assertIn("uint32_t c = 0x37474F;", lam)                       # stale: grey
        self.assertIn("if (s == 1) c = 0x1E7B34;", lam)
        self.assertIn("if (s == 2) c = 0x2E86C1;", lam)                    # off: neutral blue, not green
        self.assertEqual(lam.count("0x1E7B34"), 1)
        self.assertIn("if (s != id(ins_shore_a_sdrawn)) {", lam)
        self.assertIn("{id: ins_shore_a_sdrawn, type: uint8_t, initial_value: \"255\"}", text)
        self.assertIn("{id: ins_shore_a_num, type: float", text)
        self.assertIn("uint8_t s = fresh ? (id(ins_shore_a_num) > 0.5f ? 1 : 2) : 0;", text)

    def test_pill_geometry_and_names(self):
        full = self.page(self.elec_device(pages=("full",)), 0)
        pill = full.split("id: p0_shore_tile")[1].split("- label:")[0]
        self.assertIn(f"width: {render.ELEC_PILL_WIDTH}", pill)
        self.assertIn(f"radius: {render.ELEC_PILL_HEIGHT // 2}", pill)
        self.assertLessEqual(render.ELEC_NAME_WIDTH + 6 + render.ELEC_PILL_WIDTH, render.ELEC_WIDTH)

    # -- stale / unknown ------------------------------------------------------------------------

    def test_every_derived_state_is_gated_on_freshness(self):
        text = self.elec_device()
        for lam in re.findall(r"return \((.*?)\) == \d;'", text):
            self.assertIn("millis() - id(ins_", lam)
        # nothing on the page reads a raw number outside a fresh guard
        for match in re.finditer(r"id\(ins_(batt_a|shore_a|batt_soc)_num\)", text):
            line = text[:match.start()].rsplit("\n", 1)[1] + text[match.start():].split("\n", 1)[0]
            if 'x["v"].as<float>()' in line:      # the handler storing the payload number
                continue
            self.assertTrue("fresh" in line or "millis()" in line or "float n = id(" in line, line)
        lam = text.split("float n = id(ins_batt_soc_num);")[0].rsplit("bool fresh", 1)[1]
        self.assertIn("id(ins_batt_soc_ok)", lam)

    # -- geometry, header -----------------------------------------------------------------------

    def test_the_text_column_fits_inside_the_free_circle_of_the_ring(self):
        board = REGISTRY["boards"][BOARD]
        diameter = render.gauge_diameter(board)
        free = render.arc_zone_diameter(diameter) // 2 - render.ZONE_WIDTH          # inner edge of the zone arcs
        self.assertEqual(free, 188)
        for pages in (("min",), ("shore",), ("full",)):
            page = {"items": {"min": 3, "shore": 4, "full": 5}[pages[0]] * ["x"]}
            height = render.electrical_stack_height(page)
            self.assertLessEqual(math.hypot(render.ELEC_WIDTH / 2, height / 2), free, pages)
        # the voltage digits at 56 px: six glyphs, a decimal point and the unit
        self.assertLess(5 * 0.56 * render.FONT_SIZES["grid"] + 0.28 * render.FONT_SIZES["grid"]
                        + 2 * render.FONT_SIZES["small"], render.ELEC_WIDTH)

    def test_the_column_clears_the_header_and_the_page_dots(self):
        board = REGISTRY["boards"][BOARD]
        ring = board["ring"]
        for count in (3, 4, 5):
            height = render.electrical_stack_height({"items": ["x"] * count})
            top, bottom = 240 - height / 2, 240 + height / 2
            self.assertGreater(top, ring["header_y"] + ring["header_h"])
            self.assertLess(bottom, ring["footer_y"])
        for x in (-render.ELEC_WIDTH / 2, render.ELEC_WIDTH / 2):
            self.assertGreaterEqual(240 + x, board["safe_inset"] - 70)

    def test_header_orientation_moves_the_gap_to_the_top(self):
        plain = self.page(self.elec_device(pages=("min",)), 0)
        self.assertIn("start_angle: 135", plain)
        self.assertIn("end_angle: 45", plain)
        text = self.elec_device(header=True, pages=("min", "full"))
        page = self.page(text, 0)
        self.assertIn("start_angle: 315", page)
        self.assertIn("end_angle: 225", page)
        self.assertEqual(render.arc_start_angle({"ui": {"header": True}}), 315)
        self.assertLess(page.index("id: p0_arc"), page.index("id: p0_content"))
        self.assertLess(page.index("id: p0_content"), page.index("id: p0_clock"))
        self.assertLess(page.index("id: p0_pages"), page.index("id: p0_tap"))
        self.assertIn("id: p0_title,", page)

    # -- night, ids -----------------------------------------------------------------------------

    def test_night_repaints_text_ring_and_resets_the_guards(self):
        text = self.elec_device(night=True)
        script = text.split("  - id: apply_night")[1].split("\n  - id:")[0]
        for needle in ("lv_obj_set_style_arc_color(id(p0_arc), lv_color_hex(n ? 0x1A0800 : 0x263238), LV_PART_MAIN);",
                       "id(p0_batt_v_name)", "id(p0_batt_v_value)", "id(p0_batt_v_unit)", "id(p0_batt_soc_value)",
                       "id(p0_batt_a_word)", "id(p1_shore_a_name)", "id(p1_shore_a_value)", "id(p2_solar_a_unit)",
                       "id(p2_shore_name)", "id(p0_zone0)"):
            self.assertIn(needle, script)
        for guard in ("id(ins_batt_soc_gdrawn) = 255;", "id(ins_shore_a_sdrawn) = 255;",
                      "id(ins_shore_drawn_sev) = 255;"):
            self.assertIn(guard, script)
        self.assertNotIn("p2_shore_value", script)                       # a notification pill's text is severity-coloured
        self.assertNotIn("_tile)", script)
        pill = text.split("uint8_t s = fresh ?")[1].split("          }\n")[0]
        self.assertIn("if (id(night_active)) {", pill)
        self.assertIn("if (s == 1) c = 0x6B3000;", pill)
        self.assertNotIn("drawn_sev", pill)

    def test_without_night_there_is_no_theming_code(self):
        text = self.elec_device()
        self.assertNotIn("night_active", text)
        self.assertNotIn("id: p0_batt_v_name", text)

    def test_every_referenced_id_exists(self):
        for kw in ({}, {"header": True}, {"night": True}, {"header": True, "night": True},
                   {"pages": ("min",)}, {"pages": ("shore",), "night": True}, {"pages": ("full",), "night": True}):
            text = self.elec_device(**kw)
            ids = set(re.findall(r"\bid: (\w+)", text)) | set(re.findall(r"\{id: (\w+)", text))
            for ref in set(re.findall(r"id\((\w+)\)", text)) - {"board_backlight"}:
                self.assertIn(ref, ids, (kw, ref))

    def test_a_thresholded_voltage_on_the_page_gets_no_tile_colour_code(self):
        text = self.elec_device()
        self.assertNotIn("ins_batt_v_drawn_sev", text)
        self.assertNotIn("p0_batt_v_tile", text)
        self.assertIn("ins_batt_v_sev", text)                            # still published for the alarm summary

    def test_the_same_instrument_on_a_tile_page_keeps_its_own_colour_path(self):
        def mutate(cfg):
            cfg["displays"]["helm"]["pages"].append({"title": "Tiles", "layout": "grid-2x2",
                                                     "items": ["batt_v", "batt_soc"]})
        text = self.elec_device(mutate, pages=("min",))
        sev = text.split("uint8_t sev = fresh ?")[1].split("          }\n")[0]
        self.assertIn("id(p1_batt_v_tile)", sev)
        self.assertNotIn("p0_", sev)
        ids = set(re.findall(r"\bid: (\w+)", text))
        for ref in set(re.findall(r"id\((p\d+_\w+)\)", text)):
            self.assertIn(ref, ids, ref)

    # -- text -----------------------------------------------------------------------------------

    def test_hostile_labels_and_units_stay_literal_and_out_of_lambdas(self):
        hostile = {"batt_v": ("$x ${y} \"q\" \\ #:", "${u}"), "batt_soc": ("<% 1+1 %>", "$$"),
                   "batt_a": ("${y}", "<%"), "shore_a": ("$x", "#"), "solar_a": ("'s", "\\")}

        def mutate(cfg):
            for iid, (label, unit) in hostile.items():
                cfg["instruments"][iid].update(label=label[:16], unit=unit)
            cfg["notifications"]["shore"]["label"] = "${y} Shore"
        text = self.elec_device(mutate, night=True)
        for iid, (label, unit) in hostile.items():
            self.assertIn(f"text: !literal {render.yq(label[:16])}", text)
            if iid != "shore_a":                     # the pill shows a word, not the unit
                self.assertIn(f"text: !literal {render.yq(unit)}", text)
        self.assertIn(f"text: !literal {render.yq('${y} Shore')}", text)
        inside = "".join(l for l in text.splitlines() if "lambda" in l or "lv_" in l or "snprintf" in l)
        for label, unit in hostile.values():
            self.assertNotIn(label[:16], inside)
        for line in text.splitlines():
            if "${y}" in line or "<% 1+1" in line:
                self.assertTrue(line.lstrip().startswith("- label:") or "glyphs:" in line, line)

    def test_unsupported_glyph_in_an_electrical_label_is_reported(self):
        self.set_config(lambda cfg: (electrical_pages(cfg), cfg["instruments"]["batt_a"].update(label="A \u2603")))
        with self.assertRaises(render.RenderError) as caught:
            self.collect(allow_missing_wifi=True)
        self.assertIn("U+2603", str(caught.exception))

    def test_fixed_words_and_values_are_in_the_glyph_sets_of_their_fonts(self):
        text = self.elec_device()
        glyphs = {fid: json.loads(g) for fid, g in re.findall(
            r'id: (font_\w+)\n    size: \d+\n    bpp: 4\n    glyphs: !literal (".*")', text)}
        self.assertEqual(glyphs["font_value_grid"], "0123456789.- ")
        small = set(glyphs["font_small"])
        regular = {chr(c) for c in render.font_coverage("regular")}
        needed = set(render.VALUE_GLYPHS) | set("CHGDSGONFALRMW-")
        for words in (render.CURRENT_WORDS, render.SHORE_WORDS, render.STATUS_WORDS):
            for word in words.values():
                needed |= set(word)
        self.assertTrue(needed <= small, needed - small)
        self.assertTrue(needed <= regular, needed - regular)
        for font, label in (("font_value_grid", "p0_batt_v_value"), ("font_small", "p0_batt_soc_value"),
                            ("font_small", "p0_batt_a_word"), ("font_small", "p1_shore_a_value"),
                            ("font_small", "p2_shore_value")):
            self.assertIn(f"text_font: {font}", self.label(text, label), label)

    def test_fonts_only_appear_with_the_layouts_that_need_them(self):
        text = self.elec_device(pages=("min",))
        self.assertIn("id: font_value_grid\n    size: 56", text)
        self.assertNotIn("font_value_single", text)
        self.assertNotIn("font_value_bar", text)

    def test_an_electrical_page_leaves_the_other_pages_unchanged(self):
        def mutate(cfg):
            cfg["displays"]["helm"]["pages"] = ([{"title": "Nav", "layout": "grid-2x2",
                                                  "items": ["sog", "depth", "awa", "house_v"]}]
                                                + cfg["displays"]["helm"]["pages"])
        text = self.elec_device(mutate, pages=("min",))
        self.assertEqual(self.page(text, 0).replace("\n", ""),
                         self.page(self.device(allow_missing_wifi=True), 0).replace("\n", ""))

    def test_subscribes_to_the_electrical_items_only(self):
        text = self.elec_device()
        topics_ = re.findall(r"- topic: vesselstack/v1/(instruments|notifications)/(\w+)", text)
        self.assertEqual(sorted(t for _, t in topics_), ["batt_a", "batt_soc", "batt_v", "shore", "shore_a", "solar_a"])

    def test_registry_keeps_the_default_page(self):
        board = REGISTRY["boards"][BOARD]
        self.assertEqual(board["layouts"][-1], "electrical")
        page = lifecycle.default_page(REGISTRY, EXAMPLE, BOARD)
        self.assertEqual(page["layout"], "grid-2x2")


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
