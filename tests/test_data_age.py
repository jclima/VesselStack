from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "boat-chat"))
import data_age  # noqa: E402

NOW = dt.datetime(2026, 11, 14, 12, 0, 0, tzinfo=dt.timezone.utc)


def stamp(seconds_ago: float) -> str:
    return (NOW - dt.timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


class ClassifyTests(unittest.TestCase):
    def test_boundaries(self):
        for ago, state in ((0, "fresh"), (600, "fresh"), (601, "stale"), (-60, "fresh"), (-61, "future")):
            with self.subTest(ago=ago):
                self.assertEqual(data_age.classify(stamp(ago), NOW).state, state)

    def test_age_and_formatting(self):
        result = data_age.classify(stamp(172800), NOW, ZoneInfo("Europe/Oslo"))
        self.assertEqual(result.age_seconds, 172800)
        self.assertEqual(result.observed_utc, "2026-11-12T12:00:00Z")
        self.assertIn("13:00", result.observed_local)
        self.assertTrue(data_age.classify(stamp(5), NOW).observed_local.endswith("UTC"))

    def test_missing_and_invalid(self):
        for value in (None, ""):
            self.assertEqual(data_age.classify(value, NOW), ("missing", None, None, None))
        for value in ("2026-11-14T12:00:00", "garbage", "1970-01-01T00:00:00Z", "2019-12-31T23:59:59Z", 12, True):
            with self.subTest(value=value):
                self.assertEqual(data_age.classify(value, NOW).state, "invalid")

    def test_earliest_plausible_is_valid(self):
        self.assertEqual(data_age.classify("2020-01-01T00:00:00Z", NOW).state, "stale")

    def test_offsets_are_respected(self):
        self.assertEqual(data_age.classify("2026-11-14T13:59:00+02:00", NOW).state, "fresh")


class DescribeAgeTests(unittest.TestCase):
    def test_boundaries(self):
        cases = {0: "under 1 min", 59.9: "under 1 min", 60: "1 min", 240: "4 min", 3599: "59 min",
                 3600: "1 h", 7800: "2 h 10 min", 86399: "23 h 59 min", 86400: "1 day",
                 172799: "1 day", 172800: "2 days", -5: "under 1 min"}
        for seconds, text in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(data_age.describe_age(seconds), text)


class ClockStatusTests(unittest.TestCase):
    def test_ok_unverified(self):
        stamps = [stamp(5), stamp(30), stamp(900)]
        self.assertEqual(data_age.clock_status(NOW, stamps, NOW.isoformat()), "ok")
        self.assertEqual(data_age.clock_status(NOW, stamps), "unverified")
        self.assertEqual(data_age.clock_status(NOW, stamps, "garbage"), "unverified")

    def test_floor_date_is_suspect(self):
        early = dt.datetime(2025, 12, 31, tzinfo=dt.timezone.utc)
        self.assertEqual(data_age.clock_status(early, [], early.isoformat()), "suspect")

    def test_gps_mismatch(self):
        for offset, expected in ((300, "ok"), (301, "suspect"), (-301, "suspect")):
            gps = (NOW + dt.timedelta(seconds=offset)).isoformat()
            self.assertEqual(data_age.clock_status(NOW, [], gps), expected)

    def test_mostly_future_timestamps(self):
        self.assertEqual(data_age.clock_status(NOW, [stamp(-300)] * 3 + [stamp(1)], NOW.isoformat()), "suspect")
        self.assertEqual(data_age.clock_status(NOW, [stamp(-300)] * 2 + [stamp(1)] * 2, NOW.isoformat()), "ok")
        # Fewer than three parseable timestamps never trigger the rule.
        self.assertEqual(data_age.clock_status(NOW, [stamp(-300)] * 2, NOW.isoformat()), "ok")


if __name__ == "__main__":
    unittest.main()
