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
        self.assertTrue(bad(lambda r: r.update(schema=2)))

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


if __name__ == "__main__":
    unittest.main()
