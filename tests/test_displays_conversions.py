#!/usr/bin/env python3

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "displays"))

import conversions  # noqa: E402

KNOWN = {
    "none": (3.5, 3.5),
    "m/s->kn": (1852 / 3600, 1.0),
    "m/s->km/h": (10, 36),
    "m/s->mph": (1609.344 / 3600, 1.0),
    "K->C": (273.15, 0.0),
    "K->F": (273.15, 32.0),
    "rad->deg": (math.pi, 180.0),
    "rad->deg_signed": (math.pi / 2, 90.0),
    "rad->deg_360": (math.pi / 2, 90.0),
    "m->ft": (0.3048, 1.0),
    "m->fathom": (1.8288, 1.0),
    "m->nmi": (1852, 1.0),
    "ratio->%": (0.5, 50.0),
    "Pa->hPa": (101325, 1013.25),
    "Pa->bar": (100000, 1.0),
    "Pa->psi": (6894.757293168, 1.0),
    "m3->L": (0.5, 500.0),
    "m3->gal": (0.003785411784, 1.0),
    "m3/s->L/h": (0.001, 3600.0),
    "m3/s->gal/h": (0.003785411784, 3600.0),
    "Hz->rpm": (50, 3000),
    "C->Ah": (3600, 1.0),
    "s->min": (120, 2.0),
    "s->h": (7200, 2.0),
    "negate": (12.5, -12.5),
}


class ConversionTests(unittest.TestCase):
    def test_every_conversion_has_a_known_value(self) -> None:
        self.assertEqual(set(KNOWN), set(conversions.CONVERSIONS))
        for name, (src, expected) in KNOWN.items():
            with self.subTest(name):
                self.assertAlmostEqual(conversions.convert(name, src), expected, places=6)

    def test_negate_flips_the_sign_and_never_yields_negative_zero(self) -> None:
        self.assertEqual(conversions.convert("negate", -3.2), 3.2)
        self.assertEqual(conversions.convert_and_round("negate", 0.0, 1), 0.0)
        self.assertEqual(str(conversions.convert_and_round("negate", 0.0, 1)), "0.0")
        self.assertEqual(str(conversions.convert_and_round("negate", 0.04, 1)), "0.0")   # -0.04 rounds to zero
        self.assertEqual(conversions.convert_and_round("negate", 12.34, 1), -12.3)
        self.assertEqual(conversions.convert_and_round("negate", -12.34, 0), 12)
        self.assertIsNone(conversions.convert_and_round("negate", float("inf"), 1))

    def test_signed_angle_wrap(self) -> None:
        f = lambda deg: conversions.convert("rad->deg_signed", math.radians(deg))  # noqa: E731
        self.assertAlmostEqual(f(180), 180.0)
        self.assertAlmostEqual(f(-180), 180.0)
        self.assertAlmostEqual(f(181), -179.0)
        self.assertAlmostEqual(f(-181), 179.0)
        self.assertAlmostEqual(f(360), 0.0)
        self.assertAlmostEqual(f(540), 180.0)
        self.assertAlmostEqual(f(-90), -90.0)

    def test_360_angle_wrap(self) -> None:
        f = lambda deg: conversions.convert("rad->deg_360", math.radians(deg))  # noqa: E731
        self.assertAlmostEqual(f(0), 0.0)
        self.assertAlmostEqual(f(-1), 359.0)
        self.assertAlmostEqual(f(360), 0.0, places=6)
        self.assertAlmostEqual(f(725), 5.0)
        self.assertLess(conversions.convert("rad->deg_360", -1e-18), 360.0)

    def test_rounding(self) -> None:
        self.assertEqual(conversions.round_value(2.5, 0), 3)
        self.assertEqual(conversions.round_value(-2.5, 0), -3)
        self.assertEqual(conversions.round_value(6.45, 1), 6.5)
        self.assertEqual(conversions.round_value(1.23456, 3), 1.235)
        self.assertIsInstance(conversions.round_value(3.2, 0), int)
        self.assertEqual(str(conversions.round_value(-0.04, 1)), "0.0")
        self.assertEqual(conversions.convert_and_round("m/s->kn", 3.2918, 1), 6.4)

    def test_round_value_rejects_out_of_range(self) -> None:
        for bad in (1e30, 1e300, -1e300, float("inf"), float("-inf"), float("nan"), 1e15 * 1.0001):
            self.assertIsNone(conversions.round_value(bad, 1), repr(bad))
            self.assertIsNone(conversions.round_value(bad, 0), repr(bad))
        self.assertEqual(conversions.round_value(1e15, 1), 1e15)
        self.assertEqual(conversions.round_value(-1e15, 0), -10**15)
        self.assertIsNone(conversions.convert_and_round("m/s->kn", 1e308, 1))
        self.assertIsNone(conversions.convert_and_round("rad->deg_signed", 1e308, 1))


if __name__ == "__main__":
    unittest.main()
