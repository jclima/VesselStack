#!/usr/bin/env python3

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "displays"))

import severity  # noqa: E402

sys.path.insert(0, str(ROOT / "boat-chat"))
import status_model  # noqa: E402

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


def ok(text):
    return {"sev": "ok", "text": text}


BINARY = {"kind": "binary", "states": [{"match": "on", "sev": "warn", "text": "RUNNING"},
                                       {"match": "off", "sev": "ok", "text": "OFF"}]}
NUMERIC = {"kind": "numeric", "unit": "C", "precision": 1, "thresholds": {"warn_above": 30, "alarm_above": 40}}


class HaStateTests(unittest.TestCase):
    def test_no_value_states_are_missing_and_unknown_never_ok(self) -> None:
        for kind, cfg in (("binary", BINARY), ("state", BINARY), ("numeric", NUMERIC)):
            for state in ("unknown", "unavailable", "", "  ", "UNKNOWN", " Unavailable ", "none", "None", None):
                self.assertEqual(severity.ha_state(state, kind, cfg), ("missing", "unknown", None, None),
                                 (kind, state))
        for bad in (1, 1.5, True, [], {}, b"on"):
            self.assertEqual(severity.ha_state(bad, "binary", BINARY), ("missing", "unknown", None, None))

    def test_binary_matches_by_index_case_insensitively_after_trimming(self) -> None:
        self.assertEqual(severity.ha_state("on", "binary", BINARY), ("ok", "warn", 0, None))
        self.assertEqual(severity.ha_state("  OFF\n", "binary", BINARY), ("ok", "ok", 1, None))
        self.assertEqual(severity.ha_state("On", "binary", BINARY), ("ok", "warn", 0, None))

    def test_unmatched_takes_the_default_and_a_binary_default_is_warn(self) -> None:
        self.assertEqual(severity.ha_state("maybe", "binary", BINARY), ("ok", "warn", -1, None))
        cfg = dict(BINARY, default={"sev": "alarm", "text": "BAD"})
        self.assertEqual(severity.ha_state("maybe", "binary", cfg), ("ok", "alarm", -1, None))
        cfg = dict(BINARY, default={"sev": "ok", "text": "fine"})   # an explicit choice is honoured
        self.assertEqual(severity.ha_state("maybe", "binary", cfg), ("ok", "ok", -1, None))
        self.assertEqual(severity.ha_state("onn", "binary", BINARY)[:2], ("ok", "warn"))   # no substring match

    def test_first_match_wins_and_prefix_matches_the_start_only(self) -> None:
        cfg = {"kind": "state", "states": [{"prefix": "dock: clear", "sev": "ok", "text": "A"},
                                           {"prefix": "dock", "sev": "warn", "text": "B"},
                                           {"match": "dock: clear", "sev": "alarm", "text": "C"}],
               "default": {"sev": "warn", "text": "?"}}
        self.assertEqual(severity.ha_state("Dock: clear, dock 5.2 ft", "state", cfg)[1:3], ("ok", 0))
        self.assertEqual(severity.ha_state("dock: drifting", "state", cfg)[1:3], ("warn", 1))
        self.assertEqual(severity.ha_state("at dock", "state", cfg)[1:3], ("warn", -1))
        self.assertEqual(severity.ha_state("dock: clear", "state", cfg)[1:3], ("ok", 0))   # not the later match

    def test_numeric_parses_finite_numbers_only(self) -> None:
        self.assertEqual(severity.ha_state("21.46", "numeric", NUMERIC), ("ok", "ok", None, 21.5))
        self.assertEqual(severity.ha_state(" 35 ", "numeric", NUMERIC), ("ok", "warn", None, 35.0))
        self.assertEqual(severity.ha_state("41", "numeric", NUMERIC)[1], "alarm")
        self.assertEqual(severity.ha_state("1e1", "numeric", NUMERIC)[3], 10.0)
        self.assertEqual(severity.ha_state("-5", "numeric", NUMERIC)[3], -5.0)
        for bad in ("nan", "NaN", "inf", "-inf", "Infinity", "1e999", "1_0", "on", "12 C", "0x10", "--1", "1,5"):
            self.assertEqual(severity.ha_state(bad, "numeric", NUMERIC), ("missing", "unknown", None, None), bad)

    def test_numeric_without_thresholds_is_ok_and_hysteresis_follows_prev(self) -> None:
        plain = {"kind": "numeric", "unit": "V", "precision": 2}
        self.assertEqual(severity.ha_state("12.345", "numeric", plain), ("ok", "ok", None, 12.35))
        cfg = {"kind": "numeric", "unit": "V", "precision": 1,
               "thresholds": {"warn_below": 12.0, "hysteresis": 0.5}}
        self.assertEqual(severity.ha_state("12.2", "numeric", cfg, prev="warn")[1], "warn")
        self.assertEqual(severity.ha_state("12.2", "numeric", cfg)[1], "ok")

    def test_reproduces_boat_chat_classify_on_its_vocabulary(self) -> None:
        """An equivalent ``states`` config gives the same verdict as boat-chat's status_model.classify."""
        configs = {
            "Boat health": {"states": [{"match": "ok", "sev": "ok", "text": "OK"}]},
            "Engine alarms": {"states": [{"match": "all clear", "sev": "ok", "text": "CLEAR"},
                                         {"match": "normal", "sev": "ok", "text": "OK"}]},
            "Weather risk": {"states": [{"match": "normal", "sev": "ok", "text": "OK"},
                                        {"match": "low", "sev": "ok", "text": "LOW"}]},
            "Boat watch": {"states": [{"match": "clear", "sev": "ok", "text": "CLEAR"},
                                      {"prefix": "dock: clear", "sev": "ok", "text": "DOCK"},
                                      {"prefix": "away: clear", "sev": "ok", "text": "AWAY"},
                                      {"prefix": "anchor: clear", "sev": "ok", "text": "ANCHOR"}]},
        }
        vocabulary = ["ok", "OK", " Ok ", "healthy", "all clear", "All Clear", "normal", "Normal", "low", "LOW",
                      "clear", "Clear", "dock: clear", "Dock: Clear", "away: clear", "anchor: clear",
                      "anchor: clear, drift 12.5 ft", "dock: clear, dock 3.1 ft", "away: clear",
                      "high", "elevated", "alarm", "warning", "problem", "dock: drifting", "anchor: dragging",
                      "unknown", "unavailable", "none", "None", "", "   ", "on", "off", "degraded",
                      "Unknown", "UNAVAILABLE"]
        verdict = {"ok": "ok", "warning": "warn", "unknown": "unknown"}
        for label, cfg in configs.items():
            cfg = dict(cfg, default={"sev": "warn", "text": "CHECK"})
            for value in vocabulary:
                expected = verdict[status_model.classify(label, value)]
                got = severity.ha_state(value, "state", cfg)[1]
                self.assertEqual(got, expected, (label, value))


if __name__ == "__main__":
    unittest.main()
