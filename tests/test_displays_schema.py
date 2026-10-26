#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "displays"))

import schema  # noqa: E402

REGISTRY = schema.load_registry()
EXAMPLE = schema.load_json(ROOT / "displays" / "displays.example.json")


def errors_for(mutate):
    cfg = copy.deepcopy(EXAMPLE)
    mutate(cfg)
    return schema.validate_config(cfg, REGISTRY)


def inst(cfg, name="sog"):
    return cfg["instruments"][name]


def page(cfg):
    return cfg["displays"]["helm"]["pages"][0]


class SchemaTests(unittest.TestCase):
    def assertError(self, mutate, fragment):
        errors = errors_for(mutate)
        self.assertTrue(any(fragment in e for e in errors), f"{fragment!r} not in {errors}")

    def test_example_and_registry_are_valid(self) -> None:
        self.assertEqual(schema.validate_config(EXAMPLE, REGISTRY), [])
        self.assertEqual(schema.validate_registry(REGISTRY), [])

    def test_trailing_newlines_are_never_accepted(self) -> None:
        for pattern in (schema.INSTRUMENT_ID, schema.DISPLAY_ID, schema.BOARD_ID,
                        schema.LAYOUT_ID, schema.SK_PATH, schema.HOST_LABEL):
            self.assertTrue(pattern.pattern)  # patterns are always applied with fullmatch
        self.assertIsNone(schema.DISPLAY_ID.fullmatch("ab\n"))
        self.assertTrue(schema.DISPLAY_ID.fullmatch("ab"))

    def test_display_ids_never_end_in_a_hyphen(self) -> None:
        for good in ("a", "helm", "helm-2", "a" + "b" * 22 + "c", "x9"):
            self.assertTrue(schema.DISPLAY_ID.fullmatch(good), good)
        for bad in ("helm-", "a-", "-a", "1a", "Helm", "a" * 25, "", "a--"):
            self.assertIsNone(schema.DISPLAY_ID.fullmatch(bad), bad)

        def rename(cfg, old, new, key):
            cfg[key][new] = cfg[key].pop(old)
        self.assertError(lambda c: rename(c, "helm", "helm\n", "displays"), "helm")
        self.assertError(lambda c: rename(c, "sog", "sog\n", "instruments"), "sog")
        self.assertError(lambda c: inst(c).update(path="navigation.speedOverGround\n"), "path")
        self.assertError(lambda c: c.update(mqtt_host="boat.local\n"), "$.mqtt_host")

    def test_top_level_rules(self) -> None:
        self.assertError(lambda c: c.update(schema=2), "$.schema")
        self.assertError(lambda c: c.update(schema=True), "$.schema")
        self.assertError(lambda c: c.update(extra=1), "$.extra: unknown key")
        self.assertError(lambda c: c.update(mqtt_host="bad host"), "$.mqtt_host")
        self.assertError(lambda c: c.update(mqtt_host="999.1.1.1"), "$.mqtt_host")
        self.assertError(lambda c: c.update(mqtt_host="-a.local"), "$.mqtt_host")
        self.assertError(lambda c: c.update(heartbeat_s=0), "$.heartbeat_s")
        self.assertError(lambda c: c.update(heartbeat_s=61), "$.heartbeat_s")
        self.assertError(lambda c: c.update(heartbeat_s=True), "$.heartbeat_s")
        self.assertError(lambda c: c.pop("mqtt_host"), "$.mqtt_host: required")
        self.assertEqual(errors_for(lambda c: c.pop("heartbeat_s")), [])
        self.assertEqual(schema.DEFAULT_HEARTBEAT_S, 5)
        self.assertError(lambda c: (c.pop("heartbeat_s"), c["instruments"][next(iter(c["instruments"]))].update(stale_after_s=9)), "at least 2 x heartbeat_s (10)")
        self.assertEqual(errors_for(lambda c: c.update(mqtt_host="192.0.2.1")), [])
        self.assertEqual(errors_for(lambda c: c.update(mqtt_host="2001:db8::1")), [])

    def test_ids(self) -> None:
        self.assertError(lambda c: c["instruments"].update(Bad=inst(c)), "$.instruments.Bad")
        self.assertError(lambda c: c["instruments"].update({"a" * 33: inst(c)}), "instrument id")
        self.assertError(lambda c: c["displays"].update(Bad_1=c["displays"]["helm"]), "display id")
        self.assertError(lambda c: c["displays"].update({"h" * 25: c["displays"]["helm"]}), "display id")

    def test_instrument_rules(self) -> None:
        w = "$.instruments.sog"
        self.assertError(lambda c: inst(c).update(extra=1), f"{w}.extra: unknown key")
        self.assertError(lambda c: inst(c).update(path="nodots"), f"{w}.path")
        self.assertError(lambda c: inst(c).update(path="a b.c"), f"{w}.path")
        self.assertError(lambda c: inst(c).update(convert="furlongs"), f"{w}.convert")
        self.assertError(lambda c: inst(c).update(unit="123456789"), f"{w}.unit")
        self.assertError(lambda c: inst(c).update(precision=4), f"{w}.precision")
        self.assertError(lambda c: inst(c).update(precision=True), f"{w}.precision")
        self.assertError(lambda c: inst(c).update(precision=1.5), f"{w}.precision")
        self.assertError(lambda c: inst(c).update(stale_after_s=3601), f"{w}.stale_after_s")
        self.assertError(lambda c: inst(c).update(stale_after_s=9), f"{w}.stale_after_s")
        self.assertError(lambda c: inst(c).update(min_interval_ms=99), f"{w}.min_interval_ms")
        self.assertError(lambda c: inst(c).update(min_interval_ms=60001), f"{w}.min_interval_ms")
        self.assertError(lambda c: inst(c).update(label=""), f"{w}.label")
        self.assertError(lambda c: inst(c).update(label="x" * 17), f"{w}.label")
        self.assertError(lambda c: inst(c).update(label="a\nb"), f"{w}.label")
        self.assertError(lambda c: inst(c).pop("path"), f"{w}.path: required")

    def test_stale_depends_on_heartbeat(self) -> None:
        self.assertError(lambda c: c.update(heartbeat_s=30), "at least 2 x heartbeat_s")

    def test_display_rules(self) -> None:
        w = "$.displays.helm"
        d = lambda c: c["displays"]["helm"]  # noqa: E731
        self.assertError(lambda c: d(c).update(name=""), f"{w}.name")
        self.assertError(lambda c: d(c).update(name="x" * 33), f"{w}.name")
        self.assertError(lambda c: d(c).update(board="nope"), f"{w}.board")
        self.assertError(lambda c: d(c).update(home_assistant_api="yes"), f"{w}.home_assistant_api")
        self.assertError(lambda c: d(c).update(extra=1), f"{w}.extra: unknown key")
        self.assertError(lambda c: d(c).update(pages=[]), f"{w}.pages")
        self.assertError(lambda c: d(c).update(pages=[page(c)] * 9), f"{w}.pages")

    def test_page_rules(self) -> None:
        w = "$.displays.helm.pages[0]"
        self.assertError(lambda c: page(c).update(title=""), f"{w}.title")
        self.assertError(lambda c: page(c).update(title="x" * 17), f"{w}.title")
        self.assertError(lambda c: page(c).update(layout="grid-9x9"), f"{w}.layout")
        self.assertError(lambda c: page(c).update(layout="list-6"), "not supported by board")
        self.assertError(lambda c: page(c).update(layout="single"), "exceed layout capacity")
        self.assertError(lambda c: page(c).update(items=["sog", "nope"]), f"{w}.items[1]")
        self.assertError(lambda c: page(c).update(items=["sog", "sog"]), "duplicate")
        self.assertError(lambda c: page(c).update(extra=1), f"{w}.extra: unknown key")

    def test_all_errors_are_collected(self) -> None:
        def mutate(c):
            c["schema"] = 3
            inst(c)["precision"] = 9
            page(c)["layout"] = "nope"
        self.assertGreaterEqual(len(errors_for(mutate)), 3)

    def test_registry_negative_cases(self) -> None:
        def bad(mutate):
            reg = copy.deepcopy(REGISTRY)
            mutate(reg)
            return schema.validate_registry(reg)
        board = lambda r: r["boards"]["waveshare-esp32-s3-touch-lcd-2.8c"]  # noqa: E731
        self.assertTrue(bad(lambda r: board(r).update(shape="oval")))
        self.assertTrue(bad(lambda r: board(r).update(layouts=["nope"])))
        self.assertTrue(bad(lambda r: board(r).update(content_width=9999)))
        self.assertTrue(bad(lambda r: board(r).update(extra=1)))
        self.assertTrue(bad(lambda r: r["layouts"].update(single=0)))
        self.assertTrue(bad(lambda r: r.update(schema=3)))
        self.assertTrue(bad(lambda r: r.update(schema=1)))  # objects are not schema 1

    def test_registry_v1_integer_layouts_are_normalised(self) -> None:
        v1 = {"schema": 1, "layouts": {"single": 1, "grid-2x2": 4},
              "boards": {"b": dict(REGISTRY["boards"]["waveshare-esp32-s3-touch-lcd-2.8c"],
                                   layouts=["single", "grid-2x2"])}}
        self.assertEqual(schema.validate_registry(v1), [])
        self.assertEqual(schema.layout_specs(v1)["grid-2x2"], {
            "capacity": 4, "min_items": 1, "shapes": ["round", "rect"], "types": ["instrument"]})
        v1["layouts"]["single"] = 0
        self.assertTrue(schema.validate_registry(v1))
        # a v1 registry validates configurations exactly like the v2 one
        v1["layouts"]["single"] = 1
        v1["boards"]["waveshare-esp32-s3-touch-lcd-2.8c"] = v1["boards"].pop("b")
        self.assertEqual(schema.validate_config(EXAMPLE, v1), [])

    def test_registry_v2_layout_rules(self) -> None:
        def bad(mutate):
            reg = copy.deepcopy(REGISTRY)
            mutate(reg)
            return schema.validate_registry(reg)
        grid = lambda r: r["layouts"]["grid-2x2"]  # noqa: E731
        self.assertEqual(schema.validate_registry(REGISTRY), [])
        self.assertEqual(REGISTRY["schema"], 2)
        for mutate in (lambda r: r["layouts"].update({"grid-2x2": 4}),
                       lambda r: grid(r).update(capacity=0),
                       lambda r: grid(r).update(capacity=True),
                       lambda r: grid(r).update(min_items=5),
                       lambda r: grid(r).pop("types"),
                       lambda r: grid(r).update(types=[]),
                       lambda r: grid(r).update(types=["instrument", "instrument"]),
                       lambda r: grid(r).update(types=["ha"]),
                       lambda r: grid(r).update(shapes=["oval"]),
                       lambda r: grid(r).update(extra=1)):
            self.assertTrue(bad(mutate))

    def test_a_board_may_only_list_layouts_that_support_its_shape(self) -> None:
        errors = None
        reg = copy.deepcopy(REGISTRY)
        reg["layouts"]["grid-2x2"]["shapes"] = ["rect"]
        errors = schema.validate_registry(reg)
        self.assertTrue(any("does not support the round shape" in e for e in errors), errors)
        cfg = copy.deepcopy(EXAMPLE)
        self.assertTrue(any("does not support the round shape" in e
                            for e in schema.validate_config(cfg, reg)))

    def test_duplicate_json_keys_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dup.json"
            path.write_text('{"schema":1,"schema":1}')
            with self.assertRaises(ValueError):
                schema.load_json(path)


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "displays" / "displayctl.py"), *args],
                              capture_output=True, text=True)

    def test_default_validates(self) -> None:
        result = self.run_cli("validate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)

    def test_broken_config_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            bad = copy.deepcopy(EXAMPLE)
            bad["schema"] = 9
            path.write_text(json.dumps(bad))
            result = self.run_cli("validate", str(path))
        self.assertEqual(result.returncode, 1)
        self.assertIn("$.schema", result.stderr)

    def test_io_and_parse_errors_exit_2(self) -> None:
        self.assertEqual(self.run_cli("validate", "/nonexistent/displays.json").returncode, 2)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.json"
            path.write_text("{not json")
            self.assertEqual(self.run_cli("validate", str(path)).returncode, 2)


class ThresholdTests(unittest.TestCase):
    def assertError(self, mutate, fragment):
        errors = errors_for(mutate)
        self.assertTrue(any(fragment in e for e in errors), f"{fragment!r} not in {errors}")

    def ok(self, **extra):
        def mutate(cfg):
            inst(cfg).update(extra)
        self.assertEqual(errors_for(mutate), [])

    def th(self, **bounds):
        return lambda cfg: inst(cfg).update(thresholds=bounds)

    def test_valid_shapes(self) -> None:
        self.ok(thresholds={"alarm_below": 1, "warn_below": 2, "warn_above": 8.5, "alarm_above": 9, "hysteresis": 0.4})
        self.ok(thresholds={"warn_below": 2})
        self.ok(thresholds={"alarm_above": 2, "hysteresis": 100})
        self.ok(thresholds={"alarm_below": 2, "warn_below": 2})
        self.ok(thresholds={"alarm_below": -5, "alarm_above": 5}, min=-10, max=10, alarm=False)
        self.assertEqual(schema.validate_config(
            schema.load_json(ROOT / "displays" / "displays.dashboard.example.json"), REGISTRY), [])

    def test_thresholds_must_be_a_non_empty_object_with_a_bound(self) -> None:
        self.assertError(lambda c: inst(c).update(thresholds=[]), "thresholds: must be an object")
        self.assertError(self.th(), "needs at least one")
        self.assertError(self.th(hysteresis=1), "needs at least one")
        self.assertError(self.th(warn_below=1, bogus=2), "thresholds.bogus: unknown key")

    def test_numbers_are_finite_and_not_bool(self) -> None:
        for bad in (True, "1", None, float("nan"), float("inf"), 10 ** 400):
            self.assertError(self.th(warn_below=bad), "warn_below: must be a finite number")
            self.assertError(self.th(warn_below=1, hysteresis=bad), "hysteresis: must be a finite number")
            self.assertError(lambda c, b=bad: inst(c).update(min=b), "min: must be a finite number")
            self.assertError(lambda c, b=bad: inst(c).update(max=b), "max: must be a finite number")

    def test_ordering(self) -> None:
        self.assertError(self.th(alarm_below=3, warn_below=2), "alarm_below must not be greater than warn_below")
        self.assertError(self.th(warn_above=3, alarm_above=2), "warn_above must not be greater than alarm_above")
        self.assertError(self.th(warn_below=3, warn_above=3), "warn_below must be less than warn_above")
        self.assertError(self.th(warn_below=4, warn_above=3), "warn_below must be less than warn_above")
        self.assertError(self.th(alarm_below=5, alarm_above=4), "alarm_below must be less than alarm_above")
        self.assertError(self.th(alarm_below=1, warn_above=1), "alarm_below must be less than warn_above")

    def test_hysteresis(self) -> None:
        self.assertError(self.th(warn_below=1, hysteresis=-0.1), "hysteresis: must be a finite number >= 0")
        self.assertError(self.th(warn_below=1, warn_above=2, hysteresis=1), "smallest gap")
        self.assertError(self.th(alarm_below=1, warn_below=1.5, warn_above=9, hysteresis=0.5), "smallest gap")
        self.assertError(self.th(alarm_below=2, warn_below=2, hysteresis=0.1), "smallest gap")
        self.ok(thresholds={"warn_below": 1, "warn_above": 2, "hysteresis": 0.99})

    def test_min_max_alarm(self) -> None:
        self.assertError(lambda c: inst(c).update(min=5, max=5), "min: must be less than max")
        self.assertError(lambda c: inst(c).update(min=6, max=5), "min: must be less than max")
        self.assertError(lambda c: inst(c).update(alarm=1), "alarm: must be true or false")
        self.assertError(lambda c: inst(c).update(alarm="true"), "alarm: must be true or false")
        self.ok(min=0, max=1)

    def test_unknown_instrument_keys_still_rejected(self) -> None:
        self.assertError(lambda c: inst(c).update(threshold={"warn_below": 1}), "threshold: unknown key")


if __name__ == "__main__":
    unittest.main()


NOTE = {"path": "notifications.navigation.anchor", "label": "Anchor"}


def with_notes(cfg, **notes):
    cfg["notifications"] = {"anchor": dict(NOTE), **notes}
    cfg["displays"]["helm"]["pages"].append(
        {"title": "Status", "layout": "status-grid", "items": ["sog", "anchor"]})


class NotificationTests(unittest.TestCase):
    def assertError(self, mutate, fragment):
        errors = errors_for(mutate)
        self.assertTrue(any(fragment in e for e in errors), f"{fragment!r} not in {errors}")

    def test_valid_notifications_and_status_grid(self) -> None:
        def mutate(cfg):
            with_notes(cfg, mooring={"path": "notifications.mooring", "label": "Moor",
                                      "absent": "unknown", "alarm": False})
        self.assertEqual(errors_for(mutate), [])

    def test_notification_fields(self) -> None:
        def note(**kw):
            def mutate(cfg):
                with_notes(cfg)
                cfg["notifications"]["anchor"].update(kw)
            return mutate
        for path in ("navigation.anchor", "notification.x.y", "notifications", "notifications.", "notifications.a b",
                     "notifications.x\n", 5):
            self.assertError(note(path=path), "anchor.path")
        for label in ("", "x" * 17, "a\nb", 5):
            self.assertError(note(label=label), "anchor.label")
        for absent in ("alarm", "", None, True):
            self.assertError(note(absent=absent), "anchor.absent")
        self.assertError(note(alarm="yes"), "anchor.alarm")
        self.assertError(note(extra=1), "anchor.extra: unknown key")

        def missing(cfg):
            with_notes(cfg)
            del cfg["notifications"]["anchor"]["label"]
        self.assertError(missing, "anchor.label: required")
        self.assertError(lambda c: c.update(notifications=[]), "$.notifications: must be an object")

    def test_ids_are_unique_across_instruments_and_notifications(self) -> None:
        def mutate(cfg):
            cfg["notifications"] = {"sog": dict(NOTE)}
        self.assertError(mutate, "already used by an instrument")
        self.assertError(lambda c: c.update(notifications={"Bad-Id": dict(NOTE)}), "notification id must match")

    def test_layouts_declare_which_item_types_they_accept(self) -> None:
        def on_grid(cfg):
            with_notes(cfg)
            page(cfg)["items"] = ["sog", "anchor"]
        self.assertError(on_grid, "does not accept notification items")

        def unknown(cfg):
            with_notes(cfg)
            cfg["displays"]["helm"]["pages"][1]["items"] = ["sog", "nope"]
        self.assertError(unknown, "unknown instrument or notification 'nope'")

        def dup(cfg):
            with_notes(cfg)
            cfg["displays"]["helm"]["pages"][1]["items"] = ["anchor", "anchor"]
        self.assertError(dup, "duplicate")

        def over(cfg):
            with_notes(cfg)
            cfg["displays"]["helm"]["pages"][1]["items"] = [f"i{n}" for n in range(10)]
            for n in range(10):
                cfg["instruments"][f"i{n}"] = dict(inst(cfg))
        self.assertError(over, "exceed layout capacity 9")

    # -- tank-bars and gauge-arc (display dashboard slice S6) --------------------------------

    def gauge_cfg(self, layout="tank-bars", items=("fuel",), **inst_extra):
        cfg = copy.deepcopy(EXAMPLE)
        cfg["instruments"]["fuel"] = {"path": "tanks.fuel.0.currentLevel", "label": "Fuel", "convert": "ratio->%",
                                      "unit": "%", "precision": 0, "stale_after_s": 60, "min": 0, "max": 100}
        cfg["instruments"]["fuel"].update(inst_extra)
        cfg["instruments"]["water"] = dict(cfg["instruments"]["fuel"], label="Water")
        cfg["displays"]["helm"]["pages"] = [{"title": "T", "layout": layout, "items": list(items)}]
        return cfg

    def test_range_layouts_are_in_the_registry(self) -> None:
        specs = schema.layout_specs(REGISTRY)
        self.assertEqual(specs["tank-bars"]["capacity"], 4)
        self.assertEqual(specs["tank-bars"]["shapes"], ["round", "rect"])
        self.assertEqual(specs["gauge-arc"]["capacity"], 1)
        self.assertEqual(specs["gauge-arc"]["shapes"], ["round"])
        self.assertEqual(specs["gauge-arc"]["types"], ["instrument"])
        self.assertEqual(schema.RANGE_LAYOUTS, ("tank-bars", "gauge-arc"))

    def test_valid_tank_bars_and_gauge_arc_pages(self) -> None:
        self.assertEqual(schema.validate_config(self.gauge_cfg("tank-bars", ["fuel", "water"]), REGISTRY), [])
        self.assertEqual(schema.validate_config(self.gauge_cfg("gauge-arc", ["fuel"]), REGISTRY), [])
        dashboard = schema.load_json(ROOT / "displays" / "displays.dashboard.example.json")
        self.assertEqual(schema.validate_config(dashboard, REGISTRY), [])

    def test_min_and_max_are_required_and_the_error_names_item_and_layout(self) -> None:
        for layout in ("tank-bars", "gauge-arc"):
            for missing in ("min", "max"):
                cfg = self.gauge_cfg(layout)
                del cfg["instruments"]["fuel"][missing]
                errors = schema.validate_config(cfg, REGISTRY)
                self.assertTrue(any("pages[0].items[0]: instrument 'fuel' needs min and max on layout "
                                    f"'{layout}'" in e for e in errors), errors)
        cfg = self.gauge_cfg("tank-bars", ["fuel", "water"])
        del cfg["instruments"]["water"]["max"]
        errors = schema.validate_config(cfg, REGISTRY)
        self.assertEqual([e for e in errors if "needs min and max" in e],
                         ["$.displays.helm.pages[0].items[1]: instrument 'water' needs min and max on layout 'tank-bars'"])

    def test_min_and_max_are_not_required_on_the_other_layouts(self) -> None:
        cfg = self.gauge_cfg("single")
        del cfg["instruments"]["fuel"]["min"], cfg["instruments"]["fuel"]["max"]
        self.assertEqual(schema.validate_config(cfg, REGISTRY), [])

    def test_range_layout_capacity_types_and_a_bad_range(self) -> None:
        over = self.gauge_cfg("tank-bars", ["fuel", "water", "sog", "depth", "awa"])
        for iid in ("sog", "depth", "awa"):
            over["instruments"][iid].update(min=0, max=10)
        self.assertTrue(any("exceed layout capacity 4" in e for e in schema.validate_config(over, REGISTRY)))
        arc = self.gauge_cfg("gauge-arc", ["fuel", "water"])
        self.assertTrue(any("exceed layout capacity 1" in e for e in schema.validate_config(arc, REGISTRY)))
        notes = self.gauge_cfg("gauge-arc", ["anchor"])
        notes["notifications"] = {"anchor": {"path": "notifications.navigation.anchor", "label": "Anchor"}}
        self.assertTrue(any("does not accept notification items" in e for e in schema.validate_config(notes, REGISTRY)))
        flat = self.gauge_cfg("tank-bars", min=5, max=5)
        self.assertTrue(any("min: must be less than max" in e for e in schema.validate_config(flat, REGISTRY)))

    def rect_registry(self):
        reg = copy.deepcopy(REGISTRY)
        board = copy.deepcopy(reg["boards"]["waveshare-esp32-s3-touch-lcd-2.8c"])
        board.update(shape="rect", layouts=["single", "grid-2x2", "tank-bars"])
        del board["ring"]
        reg["boards"]["rect-board"] = board
        return reg

    def test_gauge_arc_needs_a_round_board(self) -> None:
        reg = self.rect_registry()
        self.assertEqual(schema.validate_registry(reg), [])
        cfg = self.gauge_cfg("gauge-arc")
        cfg["displays"]["helm"]["board"] = "rect-board"
        errors = schema.validate_config(cfg, reg)
        self.assertTrue(any("pages[0].layout: 'gauge-arc' is not supported by board 'rect-board'; "
                            "gauge-arc needs a round board; use single" in e for e in errors), errors)
        # a registry that lists it for the rect board is rejected outright
        reg["boards"]["rect-board"]["layouts"].append("gauge-arc")
        self.assertTrue(any("does not support the rect shape" in e for e in schema.validate_registry(reg)))
        # and a config page that still names it gets the same hint from the shape check
        errors = schema.validate_config(cfg, reg)
        self.assertTrue(any("gauge-arc needs a round board; use single" in e for e in errors), errors)

    def test_tank_bars_work_on_a_rect_board_and_other_layouts_get_no_hint(self) -> None:
        reg = self.rect_registry()
        cfg = self.gauge_cfg("tank-bars")
        cfg["displays"]["helm"]["board"] = "rect-board"
        self.assertEqual(schema.validate_config(cfg, reg), [])
        cfg = self.gauge_cfg("list-6")
        cfg["displays"]["helm"]["board"] = "rect-board"
        errors = schema.validate_config(cfg, reg)
        self.assertTrue(errors and not any("use single" in e for e in errors), errors)

    def test_min_items_is_enforced(self) -> None:
        reg = copy.deepcopy(REGISTRY)
        reg["layouts"]["status-grid"]["min_items"] = 3
        cfg = copy.deepcopy(EXAMPLE)
        with_notes(cfg)
        errors = schema.validate_config(cfg, reg)
        self.assertTrue(any("needs at least 3" in e for e in errors), errors)


class UiOptionTests(unittest.TestCase):
    def assertError(self, mutate, fragment):
        errors = errors_for(mutate)
        self.assertTrue(any(fragment in e for e in errors), f"{fragment!r} not in {errors}")

    @staticmethod
    def with_ui(**ui):
        def mutate(cfg):
            with_notes(cfg)
            cfg["displays"]["helm"]["ui"] = ui
        return mutate

    def test_valid_ui(self) -> None:
        self.assertEqual(errors_for(self.with_ui(alarm_takeover=True, alarm_page=1)), [])
        self.assertEqual(errors_for(self.with_ui(alarm_takeover=False)), [])
        self.assertEqual(errors_for(self.with_ui()), [])

    def test_ui_keys_and_types(self) -> None:
        w = "$.displays.helm.ui"
        self.assertError(self.with_ui(extra=1), f"{w}.extra: unknown key")
        self.assertError(self.with_ui(alarm_takeover="yes"), f"{w}.alarm_takeover")
        self.assertError(self.with_ui(alarm_takeover=1), f"{w}.alarm_takeover")
        self.assertError(lambda c: c["displays"]["helm"].update(ui=[]), f"{w}: must be an object")

    def test_alarm_page_is_a_valid_page_index(self) -> None:
        w = "$.displays.helm.ui.alarm_page"
        for bad in (-1, "1", 1.5, True, None):
            self.assertError(self.with_ui(alarm_page=bad), w)
        self.assertError(self.with_ui(alarm_page=2), "out of range")

    def test_takeover_needs_something_to_count(self) -> None:
        def nothing(cfg):
            cfg["displays"]["helm"]["ui"] = {"alarm_takeover": True}
        self.assertError(nothing, "nothing counts towards the alarm summary")

        def thresholded(cfg):
            inst(cfg)["thresholds"] = {"alarm_below": 1}
            cfg["displays"]["helm"]["ui"] = {"alarm_takeover": True}
        self.assertEqual(errors_for(thresholded), [])

        def muted(cfg):
            thresholded(cfg)
            inst(cfg)["alarm"] = False
        self.assertError(muted, "nothing counts")

        def forced(cfg):
            nothing(cfg)
            inst(cfg)["alarm"] = True
        self.assertEqual(errors_for(forced), [])


class DashboardUiOptionTests(unittest.TestCase):
    """swipe, header, night, brightness and dim_after_s (display dashboard slice S4)."""

    def check(self, ui, schedule=None, registry=REGISTRY):
        cfg = copy.deepcopy(EXAMPLE)
        cfg["displays"]["helm"]["ui"] = ui
        if schedule is not None:
            cfg["night_schedule"] = schedule
        return schema.validate_config(cfg, registry)

    def assertError(self, errors, fragment):
        self.assertTrue(any(fragment in e for e in errors), f"{fragment!r} not in {errors}")

    def test_valid_combinations(self) -> None:
        sched = {"start": "21:30", "end": "06:15"}
        self.assertEqual(self.check({"swipe": True, "header": True, "night": "schedule",
                                     "day_brightness": 90, "night_brightness": 10, "dim_after_s": 120},
                                    sched), [])
        self.assertEqual(self.check({"swipe": False, "header": False, "night": "off",
                                     "dim_after_s": 0}), [])
        self.assertEqual(self.check({"night": "manual", "night_brightness": 5}), [])

    def test_booleans(self) -> None:
        for key in ("swipe", "header"):
            for bad in ("yes", 1, None):
                self.assertError(self.check({key: bad}), f"ui.{key}: must be true or false")

    def test_night_mode(self) -> None:
        for bad in ("on", True, 1, "SCHEDULE", None):
            self.assertError(self.check({"night": bad}), "ui.night: must be one of off, manual, schedule")

    def test_night_schedule_mode_requires_the_top_level_schedule(self) -> None:
        self.assertError(self.check({"night": "schedule"}), "needs a top-level night_schedule")
        self.assertError(self.check({"night": "schedule"}, {"start": "25:00", "end": "06:00"}),
                         "needs a top-level night_schedule")  # an invalid schedule does not count
        self.assertEqual(self.check({"night": "manual"}), [])  # manual needs none
        self.assertEqual(self.check({"night": "off"}, {"start": "22:00", "end": "06:00"}), [])

    def test_night_schedule_rules(self) -> None:
        for good in ({"start": "00:00", "end": "23:59"}, {"start": "21:30", "end": "06:15"},
                     {"start": "06:00", "end": "07:00"}):
            self.assertEqual(self.check({}, good), [])
        for bad in ({"start": "9:00", "end": "10:00"}, {"start": "24:00", "end": "01:00"},
                    {"start": "12:60", "end": "13:00"}, {"start": "12:00\n", "end": "13:00"},
                    {"start": 1200, "end": "13:00"}, {"start": "12:00", "end": None}):
            self.assertError(self.check({}, bad), "night_schedule")
        self.assertError(self.check({}, {"start": "12:00", "end": "12:00"}), "start and end must differ")
        self.assertError(self.check({}, {"start": "12:00"}), "night_schedule.end: required")
        self.assertError(self.check({}, {"start": "12:00", "end": "13:00", "tz": "UTC"}), "tz: unknown key")
        self.assertError(self.check({}, "21:00-06:00"), "night_schedule: must be an object")

    def test_brightness_ranges(self) -> None:
        for key in ("day_brightness", "night_brightness"):
            for bad in (4, 101, 0, -1, 50.5, "50", True):
                self.assertError(self.check({key: bad}), f"ui.{key}: must be an integer 5..100")
            for good in (5, 100):
                self.assertEqual(self.check({key: good}), [])

    def test_night_brightness_must_not_exceed_day_brightness(self) -> None:
        self.assertError(self.check({"day_brightness": 30, "night_brightness": 40}),
                         "night_brightness: must not exceed day_brightness (30)")
        self.assertEqual(self.check({"day_brightness": 30, "night_brightness": 30}), [])
        self.assertEqual(self.check({"night_brightness": 100}), [])

    def test_dim_after_s_range(self) -> None:
        for bad in (-1, 3601, 1.5, "60", True):
            self.assertError(self.check({"dim_after_s": bad}), "ui.dim_after_s: must be an integer 0..3600")
        for good in (0, 1, 3600):
            self.assertEqual(self.check({"dim_after_s": good}), [])

    def test_unknown_ui_keys_are_still_rejected(self) -> None:
        self.assertError(self.check({"nightmode": True}), "ui.nightmode: unknown key")

    def test_header_needs_a_board_with_a_ring(self) -> None:
        reg = copy.deepcopy(REGISTRY)
        del reg["boards"]["waveshare-esp32-s3-touch-lcd-2.8c"]["ring"]
        self.assertEqual(schema.validate_registry(reg), [])
        self.assertError(self.check({"header": True}, registry=reg), "ui.header: the board has no header band")
        self.assertEqual(self.check({"header": False}, registry=reg), [])
        self.assertEqual(self.check({"header": True}), [])

    def test_ring_geometry_is_validated(self) -> None:
        def errors(ring):
            reg = copy.deepcopy(REGISTRY)
            reg["boards"]["waveshare-esp32-s3-touch-lcd-2.8c"]["ring"] = ring
            return schema.validate_registry(reg)
        self.assertEqual(errors({"header_y": 14, "header_h": 52, "footer_y": 414}), [])
        self.assertError(errors("x"), "ring: must be an object")
        self.assertError(errors({"header_y": 14, "header_h": 52}), "ring.footer_y: required")
        self.assertError(errors({"header_y": 14, "header_h": 52, "footer_y": 414, "x": 1}), "ring.x: unknown key")
        self.assertError(errors({"header_y": 14, "header_h": "52", "footer_y": 414}), "ring.header_h")
        self.assertError(errors({"header_y": 14, "header_h": 40, "footer_y": 414}), "at least 50")
        self.assertError(errors({"header_y": 30, "header_h": 52, "footer_y": 414}), "overlaps the content box")
        self.assertError(errors({"header_y": 14, "header_h": 52, "footer_y": 400}), "footer_y")
        self.assertError(errors({"header_y": 14, "header_h": 52, "footer_y": 475}), "footer_y")

    def test_ring_is_optional_in_the_registry(self) -> None:
        reg = copy.deepcopy(REGISTRY)
        del reg["boards"]["waveshare-esp32-s3-touch-lcd-2.8c"]["ring"]
        self.assertEqual(schema.validate_registry(reg), [])


class ElectricalLayoutTests(unittest.TestCase):
    """The ``electrical`` layout and positional slots (display dashboard slice S7)."""

    def cfg(self, items=("batt_v", "batt_soc", "batt_a"), **page_extra):
        cfg = copy.deepcopy(EXAMPLE)
        cfg["instruments"].update({
            "batt_v": {"path": "electrical.batteries.house.voltage", "label": "House", "convert": "none",
                       "unit": "V", "precision": 2, "stale_after_s": 30},
            "batt_soc": {"path": "electrical.batteries.house.capacity.stateOfCharge", "label": "SoC",
                         "convert": "ratio->%", "unit": "%", "precision": 0, "stale_after_s": 60,
                         "min": 0, "max": 100},
            "batt_a": {"path": "electrical.batteries.house.current", "label": "Current", "convert": "none",
                       "unit": "A", "precision": 1, "stale_after_s": 30},
            "solar_a": {"path": "electrical.solar.main.current", "label": "Solar", "convert": "none",
                        "unit": "A", "precision": 1, "stale_after_s": 30},
        })
        cfg["notifications"] = {"shore": {"path": "notifications.electrical.shorePower", "label": "Shore",
                                          "absent": "unknown"}}
        cfg["displays"]["helm"]["pages"] = [{"title": "Power", "layout": "electrical", "items": list(items)}]
        return cfg

    def errors(self, cfg, registry=REGISTRY):
        return schema.validate_config(cfg, registry)

    def test_registry_entry_and_normalised_slots(self) -> None:
        self.assertEqual(schema.validate_registry(REGISTRY), [])
        spec = schema.layout_specs(REGISTRY)["electrical"]
        self.assertEqual((spec["capacity"], spec["min_items"], spec["shapes"], spec["types"]),
                         (5, 3, ["round"], ["instrument", "notification"]))
        self.assertEqual([s["role"] for s in spec["slots"]], ["voltage", "soc", "current", "shore", "solar"])
        self.assertEqual(spec["slots"][1], {"role": "soc", "types": ["instrument"], "needs": ["numeric", "min_max"]})
        self.assertEqual([bool(s.get("optional")) for s in spec["slots"]], [False, False, False, True, True])
        self.assertNotIn("slots", schema.layout_specs(REGISTRY)["gauge-arc"])      # other layouts are unchanged
        self.assertIn("electrical", REGISTRY["boards"]["waveshare-esp32-s3-touch-lcd-2.8c"]["layouts"])
        self.assertEqual(REGISTRY["boards"]["waveshare-esp32-s3-touch-lcd-2.8c"]["layouts"][-2:],
                         ["gauge-arc", "electrical"])

    def test_three_four_and_five_items_are_valid(self) -> None:
        for items in (("batt_v", "batt_soc", "batt_a"), ("batt_v", "batt_soc", "batt_a", "shore"),
                      ("batt_v", "batt_soc", "batt_a", "shore", "solar_a"),
                      ("batt_v", "batt_soc", "batt_a", "solar_a")):      # an instrument may fill the shore slot
            with self.subTest(items):
                self.assertEqual(self.errors(self.cfg(items)), [])

    def test_too_few_and_too_many_items(self) -> None:
        self.assertTrue(any("needs at least 3" in e for e in self.errors(self.cfg(("batt_v", "batt_soc")))))
        over = self.cfg(("batt_v", "batt_soc", "batt_a", "shore", "solar_a", "sog"))
        self.assertTrue(any("exceed layout capacity 5" in e for e in self.errors(over)))

    def test_soc_needs_min_and_max_and_the_error_names_the_role(self) -> None:
        cfg = self.cfg(("batt_v", "batt_soc", "batt_a"))
        del cfg["instruments"]["batt_soc"]["max"]
        self.assertEqual(self.errors(cfg), [
            "$.displays.helm.pages[0].items[1] (soc): instrument 'batt_soc' needs min and max on layout 'electrical'"])

    def test_voltage_and_current_do_not_need_a_range(self) -> None:
        cfg = self.cfg()
        for iid in ("batt_v", "batt_a"):
            self.assertNotIn("min", cfg["instruments"][iid])
        self.assertEqual(self.errors(cfg), [])

    def test_items_map_positionally_so_a_swapped_order_names_the_role(self) -> None:
        errors = self.errors(self.cfg(("batt_soc", "batt_v", "batt_a")))
        self.assertEqual(errors, [
            "$.displays.helm.pages[0].items[1] (soc): instrument 'batt_v' needs min and max on layout 'electrical'"])

    def test_a_notification_is_only_accepted_in_the_shore_slot(self) -> None:
        self.assertEqual(self.errors(self.cfg(("batt_v", "batt_soc", "batt_a", "shore"))), [])
        for position, role in ((0, "voltage"), (1, "soc"), (2, "current")):
            items = ["batt_v", "batt_soc", "batt_a"]
            items[position] = "shore"
            errors = self.errors(self.cfg(items))
            self.assertTrue(any(f"items[{position}] ({role}): layout 'electrical' does not accept notification "
                                "items here ('shore'); this slot takes instrument" in e for e in errors), errors)
        solar = self.errors(self.cfg(("batt_v", "batt_soc", "batt_a", "batt_a", "shore")))
        self.assertTrue(any("items[4] (solar)" in e and "does not accept notification" in e for e in solar), solar)

    def test_duplicates_and_unknown_items_are_still_rejected(self) -> None:
        self.assertTrue(any("duplicate item" in e for e in self.errors(self.cfg(("batt_v", "batt_soc", "batt_v")))))
        self.assertTrue(any("unknown" in e for e in self.errors(self.cfg(("batt_v", "batt_soc", "nope")))))

    def test_electrical_needs_a_round_board(self) -> None:
        reg = copy.deepcopy(REGISTRY)
        board = copy.deepcopy(reg["boards"]["waveshare-esp32-s3-touch-lcd-2.8c"])
        board.update(shape="rect", layouts=["single", "grid-2x2"])
        del board["ring"]
        reg["boards"]["rect-board"] = board
        cfg = self.cfg()
        cfg["displays"]["helm"]["board"] = "rect-board"
        errors = self.errors(cfg, reg)
        self.assertTrue(any("'electrical' is not supported by board 'rect-board'; electrical needs a round board"
                            in e for e in errors), errors)
        reg["boards"]["rect-board"]["layouts"].append("electrical")
        self.assertTrue(any("does not support the rect shape" in e for e in schema.validate_registry(reg)))

    # -- registry validation of slots ------------------------------------------------------------

    def slot_errors(self, mutate):
        reg = copy.deepcopy(REGISTRY)
        mutate(reg["layouts"]["electrical"])
        return schema.validate_registry(reg)

    def assertSlotError(self, mutate, fragment):
        errors = self.slot_errors(mutate)
        self.assertTrue(any(fragment in e for e in errors), errors)

    def test_slot_definitions_are_validated(self) -> None:
        self.assertSlotError(lambda l: l.update(slots=[]), "slots: must be a non-empty list")
        self.assertSlotError(lambda l: l.update(slots="x"), "slots: must be a non-empty list")
        self.assertSlotError(lambda l: l["slots"].append({"role": "extra", "types": ["instrument"]}),
                             "must have one slot per item (capacity 5)")
        self.assertSlotError(lambda l: l["slots"][0].update(role="Voltage"), "slots[0].role")
        self.assertSlotError(lambda l: l["slots"][1].update(role="voltage"), "duplicate role 'voltage'")
        self.assertSlotError(lambda l: l["slots"][0].update(types=[]), "slots[0].types")
        self.assertSlotError(lambda l: l["slots"][0].update(types=["gauge"]), "slots[0].types")
        self.assertSlotError(lambda l: (l.update(types=["instrument"]), l["slots"][3].update(types=["notification"])),
                             "subset of the layout types")
        self.assertSlotError(lambda l: l["slots"][0].update(needs=["numeric", "numeric"]), "slots[0].needs")
        self.assertSlotError(lambda l: l["slots"][0].update(needs=["bogus"]), "slots[0].needs")
        self.assertSlotError(lambda l: l["slots"][0].update(optional="yes"), "slots[0].optional")
        self.assertSlotError(lambda l: l["slots"][0].update(color=1), "slots[0].color: unknown key")
        self.assertSlotError(lambda l: l["slots"][2].update(optional=True), "min_items: must equal the number of required slots (2)")
        self.assertSlotError(lambda l: l["slots"][4].pop("optional"), "a required slot cannot follow an optional one")
        self.assertSlotError(lambda l: l["slots"].__setitem__(0, "x"), "slots[0]: must be an object")
        self.assertSlotError(lambda l: l["slots"][0].pop("role"), "slots[0].role: required")
        self.assertSlotError(lambda l: l.update(min_items=2), "must equal the number of required slots (3)")

    def test_a_layout_without_slots_keeps_working(self) -> None:
        self.assertEqual(self.slot_errors(lambda l: l.pop("slots")), [])
        self.assertEqual(self.slot_errors(lambda l: None), [])
