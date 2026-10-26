#!/usr/bin/env python3

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "displays"))

import severity  # noqa: E402

sev = severity.numeric_sev
FULL = {"alarm_below": 11.0, "warn_below": 12.0, "warn_above": 14.5, "alarm_above": 15.0}


class BoundsTests(unittest.TestCase):
    def test_full_band(self) -> None:
        for value, expected in ((10.9, "alarm"), (11.5, "warn"), (13.0, "ok"), (14.8, "warn"),
                                (15.5, "alarm")):
            self.assertEqual(sev(value, FULL), expected, value)

    def test_equality_stays_on_the_safe_side(self) -> None:
        for value, expected in ((11.0, "warn"), (12.0, "ok"), (14.5, "ok"), (15.0, "warn")):
            self.assertEqual(sev(value, FULL), expected, value)

    def test_equal_alarm_and_warn_bounds(self) -> None:
        th = {"alarm_below": 5, "warn_below": 5}
        self.assertEqual(sev(4.9, th), "alarm")
        self.assertEqual(sev(5, th), "ok")

    def test_single_sided_configs(self) -> None:
        self.assertEqual(sev(9, {"warn_below": 10}), "warn")
        self.assertEqual(sev(1000, {"warn_below": 10}), "ok")
        self.assertEqual(sev(9, {"alarm_below": 10}), "alarm")
        self.assertEqual(sev(11, {"alarm_below": 10}), "ok")
        self.assertEqual(sev(51, {"warn_above": 50}), "warn")
        self.assertEqual(sev(-1000, {"warn_above": 50}), "ok")
        self.assertEqual(sev(51, {"alarm_above": 50}), "alarm")
        self.assertEqual(sev(50, {"alarm_above": 50}), "ok")

    def test_unknown_for_missing_or_non_finite(self) -> None:
        for value in (None, float("nan"), float("inf"), "12", True):
            self.assertEqual(sev(value, FULL), "unknown", value)
        self.assertEqual(sev(None, FULL, "alarm"), "unknown")


class HysteresisTests(unittest.TestCase):
    TH = {"alarm_below": 11.0, "warn_below": 12.0, "warn_above": 14.5, "alarm_above": 15.0, "hysteresis": 0.3}

    def test_low_side_recovery(self) -> None:
        th = self.TH
        self.assertEqual(sev(11.2, th, "alarm"), "alarm")  # inside the hold zone
        self.assertEqual(sev(11.3, th, "alarm"), "warn")   # cleared alarm_below + hysteresis
        self.assertEqual(sev(12.2, th, "warn"), "warn")
        self.assertEqual(sev(12.3, th, "warn"), "ok")
        self.assertEqual(sev(12.2, th, "alarm"), "warn")   # alarm -> warn zone -> still warn
        self.assertEqual(sev(12.3, th, "alarm"), "ok")

    def test_high_side_recovery(self) -> None:
        th = self.TH
        self.assertEqual(sev(14.8, th, "alarm"), "alarm")
        self.assertEqual(sev(14.7, th, "alarm"), "warn")
        self.assertEqual(sev(14.3, th, "warn"), "warn")
        self.assertEqual(sev(14.2, th, "warn"), "ok")

    def test_getting_worse_is_immediate(self) -> None:
        th = self.TH
        self.assertEqual(sev(11.9, th, "ok"), "warn")
        self.assertEqual(sev(10.9, th, "ok"), "alarm")
        self.assertEqual(sev(10.9, th, "warn"), "alarm")
        self.assertEqual(sev(15.1, th, "ok"), "alarm")

    def test_no_hysteresis_without_history(self) -> None:
        for prev in (None, "unknown", "ok"):
            self.assertEqual(sev(11.2, self.TH, prev), "warn")
            self.assertEqual(sev(12.2, self.TH, prev), "ok")

    def test_hysteresis_with_single_bounds(self) -> None:
        th = {"alarm_above": 50, "hysteresis": 2}
        self.assertEqual(sev(49, th, "alarm"), "alarm")
        self.assertEqual(sev(48, th, "alarm"), "ok")
        th = {"warn_below": 10, "hysteresis": 1}
        self.assertEqual(sev(10.5, th, "warn"), "warn")
        self.assertEqual(sev(11, th, "warn"), "ok")

    def test_decimal_boundaries_are_not_float_noise(self) -> None:
        th = {"warn_below": 12.0, "hysteresis": 0.1}
        self.assertEqual(sev(12.1, th, "warn"), "ok")  # 12.0 + 0.1 must not be 12.100000000000001

    def test_zero_hysteresis_is_plain_bounds(self) -> None:
        th = dict(FULL, hysteresis=0)
        self.assertEqual(sev(12.0, th, "warn"), "ok")


if __name__ == "__main__":
    unittest.main()
