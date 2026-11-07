#!/usr/bin/env python3
"""displays/themes.json: the classic theme loads, validates, and the validator's rules bite."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "displays"))

import render  # noqa: E402
import schema  # noqa: E402

THEMES = schema.load_json(ROOT / "displays" / "themes.json")


def errors_for(mutate):
    data = copy.deepcopy(THEMES)
    mutate(data["themes"]["classic"])
    return schema.validate_themes(data)


class ClassicThemeTests(unittest.TestCase):
    def test_classic_loads_and_validates(self):
        self.assertEqual(schema.validate_themes(THEMES), [])
        self.assertIn("classic", schema.load_themes()["themes"])

    def test_palette_matches_the_historical_constants(self):
        pal = render.load_palette("classic")
        self.assertEqual(pal.day["tile"], 0x101820)
        self.assertEqual(pal.day["name"], 0x9AA5B1)
        self.assertEqual(pal.night["value"], 0xB02400)
        self.assertEqual(pal.sev[2], (0xF2A900, 0x000000))
        self.assertEqual(pal.sev_night[3], (0xB00000, 0xFFB090))
        self.assertEqual(pal.gauge_day[4], 0x2E86C1)
        self.assertEqual(pal.track, {"day": 0x263238, "night": 0x1A0800})
        self.assertEqual((pal.tile_radius, pal.tile_pad, pal.tile_border_width, pal.tile_border),
                         (14, 4, 0, None))

    def test_border_colour_is_read_when_a_theme_has_one(self):
        data = copy.deepcopy(THEMES)
        data["themes"]["classic"]["shape"].update(tile_border_width=2, tile_border="#445566")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "themes.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            pal = render.load_palette("classic", path)
        self.assertEqual((pal.tile_border_width, pal.tile_border), (2, 0x445566))

    def test_load_themes_rejects_an_invalid_file(self):
        bad = copy.deepcopy(THEMES)
        bad["themes"]["classic"]["day"]["tile"] = "red"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "themes.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ValueError):
                schema.load_themes(path)


class ValidatorRuleTests(unittest.TestCase):
    def assertRejects(self, mutate, fragment):
        errors = errors_for(mutate)
        self.assertTrue(any(fragment in e for e in errors), errors)

    def test_green_stale_colour_rejected(self):
        self.assertRejects(lambda t: t["day"].update(dot_unknown="#20B040"), "day.dot_unknown")
        self.assertRejects(lambda t: t["gauge"]["day"].update(unknown="#20B040"), "gauge.day.unknown")
        self.assertRejects(lambda t: t["sev"]["day"].update(unknown=["#20B040", "#FFFFFF"]), "sev.day.unknown[0]")

    def test_grey_in_the_green_band_is_allowed(self):
        self.assertEqual(errors_for(lambda t: t["day"].update(dot_unknown="#707370")), [])

    def test_non_red_night_colour_rejected(self):
        self.assertRejects(lambda t: t["night"].update(value="#2060FF"), "night.value")
        self.assertRejects(lambda t: t["gauge"]["night"].update(ok="#00B040"), "gauge.night.ok")
        self.assertRejects(lambda t: t["sev"]["night"].update(warn=["#7A3E00", "#80F0B0"]), "sev.night.warn[1]")

    def test_day_severity_hue_bands(self):
        self.assertRejects(lambda t: t["sev"]["day"].update(ok=["#C62828", "#FFFFFF"]), "sev.day.ok[0]")
        self.assertRejects(lambda t: t["gauge"]["day"].update(alarm="#1E7B34"), "gauge.day.alarm")

    def test_low_contrast_rejected(self):
        self.assertRejects(lambda t: t["day"].update(value="#202830"), "day.value")
        self.assertRejects(lambda t: t["day"].update(label="#181F27"), "day.label")
        self.assertRejects(lambda t: t["sev"]["day"].update(ok=["#1E7B34", "#3A9A50"]), "sev.day.ok")

    def test_unknown_keys_rejected(self):
        self.assertRejects(lambda t: t.update(extra={}), "extra: unknown key")
        self.assertRejects(lambda t: t["day"].update(sparkle="#FFFFFF"), "day.sparkle: unknown key")
        self.assertRejects(lambda t: t["shape"].update(glow=1), "shape.glow: unknown key")
        data = copy.deepcopy(THEMES)
        data["other"] = 1
        self.assertTrue(any("$.other" in e for e in schema.validate_themes(data)))

    def test_bad_hex_rejected(self):
        for value in ("FFFFFF", "#FFF", "#GGGGGG", "#FFFFFFFF", 0xFFFFFF, None):
            self.assertRejects(lambda t, v=value: t["day"].update(tile=v), "day.tile")

    def test_missing_role_rejected(self):
        self.assertRejects(lambda t: t["night"].pop("track"), "night.track: required")
        self.assertRejects(lambda t: t["sev"]["day"].pop("alarm"), "sev.day.alarm: required")
        self.assertRejects(lambda t: t.pop("shape"), "shape: required")

    def test_border_width_needs_a_colour(self):
        self.assertRejects(lambda t: t["shape"].update(tile_border_width=2), "tile_border")
        self.assertEqual(errors_for(lambda t: t["shape"].update(tile_border_width=2, tile_border="#FFFFFF")), [])

    def test_classic_is_required(self):
        data = copy.deepcopy(THEMES)
        data["themes"]["other"] = data["themes"].pop("classic")
        self.assertTrue(any("classic" in e for e in schema.validate_themes(data)))


if __name__ == "__main__":
    unittest.main()
