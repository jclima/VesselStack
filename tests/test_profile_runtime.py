from __future__ import annotations

import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "boat-chat"))
import profile_runtime  # noqa: E402
import app  # noqa: E402
from cruise_efficiency import matched_sample  # noqa: E402


class ProfileRuntimeTests(unittest.TestCase):
    def test_missing_profile_is_legacy_and_invalid_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snap = profile_runtime.snapshot(root)
            self.assertTrue(snap["legacy"])
            (root / "config").mkdir()
            (root / "config" / "boat-profile.json").write_text("{bad")
            snap = profile_runtime.snapshot(root)
            self.assertFalse(snap["legacy"])
            self.assertEqual(snap["profile"]["engines"], [])
            self.assertIn("malformed", snap["diagnostic"])

    def test_single_engine_and_three_engine_matching_is_dynamic(self):
        profile = {"engines": [{"id": "main", "path": "propulsion.main", "fuel_rate_supported": True}]}
        row = {"propulsion.main.revolutions": 20, "propulsion.main.fuel.rate": .00001, "navigation.speedOverGround": 5}
        sample = matched_sample(row, engines=profile["engines"])
        self.assertEqual(sample["engine_count"], 1)
        self.assertNotIn("port_rpm", sample)
        profile["engines"].extend({"id": x, "path": f"propulsion.{x}", "fuel_rate_supported": True} for x in ("aux", "gen"))
        for x in ("aux", "gen"):
            row[f"propulsion.{x}.revolutions"] = 20
            row[f"propulsion.{x}.fuel.rate"] = .00001
        self.assertEqual(matched_sample(row, engines=profile["engines"])["engine_count"], 3)

    def test_legacy_pair_requires_exact_topology(self):
        self.assertTrue(profile_runtime.legacy_pair(profile_runtime.legacy_default()))
        altered = profile_runtime.legacy_default()
        altered["engines"][0]["label"] = "Main"
        altered["engines"].reverse()
        self.assertTrue(profile_runtime.legacy_pair(altered))
        altered = profile_runtime.legacy_default()
        altered["engines"][0]["path"] = "propulsion.main"
        self.assertFalse(profile_runtime.legacy_pair(altered))
        altered = profile_runtime.legacy_default()
        altered["engines"].append({"id": "aux", "label": "Aux", "path": "propulsion.aux", "kind": "combustion", "fuel_rate_supported": True})
        self.assertFalse(profile_runtime.legacy_pair(altered))

    def test_invalid_collect_context_performs_zero_source_io(self):
        invalid = {"profile": {"engines": [], "battery_banks": [], "fuel_tanks": [], "primary_battery_bank": None}, "legacy": False, "fingerprint": "x", "diagnostic": "invalid profile"}
        with mock.patch.object(profile_runtime, "snapshot", return_value=invalid), \
             mock.patch.object(app, "get_signalk_self", side_effect=AssertionError), \
             mock.patch.object(app, "get_ha_states", side_effect=AssertionError), \
             mock.patch.object(app, "load_boat_facts", side_effect=AssertionError), \
             mock.patch.object(app, "search_local_docs", side_effect=AssertionError):
            context = app.collect_context("show engine history")
        self.assertEqual(context["profile_diagnostic"], "invalid profile")

    def test_scope_keeps_snapshot_for_query_cache_and_formatting(self):
        first = profile_runtime.snapshot()
        second = dict(first, profile={**first["profile"], "engines": []}, fingerprint="different")
        with profile_runtime.scope(runtime=first):
            self.assertIs(profile_runtime.current(), first)
            self.assertEqual(app.engine_run_cache_key("first", 1, 200).split("profile-")[-1], first["fingerprint"][:16])
            answer = app.generate_answer("fuel", {"profile": first["profile"], "profile_fingerprint": first["fingerprint"], "_profile_runtime": first, "question_type": {"fuel_usage": True}, "fuel_usage": {"available": False, "diagnostic": "unsupported"}, "query_plan": {}, "boat_facts": {}})
            self.assertIsInstance(answer, str)
            self.assertIs(profile_runtime.current(), first)
        self.assertIsNot(profile_runtime.current(), first)

    def test_generic_history_uses_configured_engine_paths_only(self):
        profile = {"schema_version": 1, "engines": [{"id": "main", "label": "Main", "path": "propulsion.main", "kind": "combustion", "fuel_rate_supported": True}], "battery_banks": [], "fuel_tanks": [], "primary_battery_bank": None}
        runtime = {"profile": profile, "legacy": False, "fingerprint": "main", "diagnostic": None}
        with profile_runtime.scope(runtime=runtime), \
             mock.patch.object(app, "influx_measurements", return_value=["propulsion.main.revolutions", "propulsion.port.revolutions", "propulsion.starboard.revolutions", "navigation.speedOverGround"]), \
             mock.patch.object(app, "query_influx", return_value=[]):
            result = app.generic_influx_history_summary("show engine rpm", include_numeric=False)
        self.assertIn("propulsion.main.revolutions", result["buckets"][app.INFLUX_HISTORY_BUCKET]["matched_measurements"])
        self.assertNotIn("propulsion.port.revolutions", result["buckets"][app.INFLUX_HISTORY_BUCKET]["matched_measurements"])

    def test_rich_unsupported_diagnostic_does_not_call_model(self):
        context = {"_profile_runtime": profile_runtime.snapshot(), "profile": {}, "profile_fingerprint": "x", "question_type": {"fuel_economy": True}, "fuel_economy": {"available": False, "diagnostic": "unsupported economy"}, "query_plan": {"complex": True}}
        with mock.patch.object(app, "call_model", side_effect=AssertionError):
            self.assertEqual(app.generate_answer("explain fuel economy", context), "unsupported economy")

    def test_readiness_uses_indexer_payload_fingerprint_and_age(self):
        runtime = profile_runtime.snapshot()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cache.sqlite"
            import telemetry_cache
            telemetry_cache.put_summary("indexer", "last_run", {"profile_fingerprint": runtime["fingerprint"]}, path=path)
            with mock.patch.object(app.memory_index, "db_path", return_value=path), mock.patch.object(app.memory_index, "status", return_value={"exists": True, "chunks": 1}), mock.patch.object(app, "configured_provider", return_value="local"):
                self.assertTrue(app.readiness_status()["layers"]["telemetry_cache"]["ready"])


if __name__ == "__main__":
    unittest.main()
