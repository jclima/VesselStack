from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "boat-chat"))
import app  # noqa: E402
import telemetry_cache  # noqa: E402


def fuel_window() -> dict:
    start = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
    stop = start + dt.timedelta(minutes=2)
    return {"label": "test", "start": start, "stop": stop, "start_local": "start", "stop_local": "stop", "start_utc": "2025-01-01T00:00:00Z", "stop_utc": "2025-01-01T00:02:00Z", "cache_key": "test"}


class FuelCorrectnessTests(unittest.TestCase):
    def setUp(self):
        self._put_summary = mock.patch.object(telemetry_cache, "put_summary")
        self._put_summary.start()
        self.addCleanup(self._put_summary.stop)

    def rows(self, *pairs):
        return [{"_time": f"2025-01-01T00:0{i}:00Z", **values} for i, values in enumerate(pairs)]

    def test_missing_channel_is_partial_not_zero(self):
        rows = self.rows({"propulsion.port.revolutions": 5, "propulsion.port.fuel.rate": 0.001, "propulsion.starboard.revolutions": None})
        with mock.patch.object(app, "query_influx", return_value=rows):
            result = app.fuel_usage_summary(fuel_window(), use_cache=False)
        self.assertFalse(result["coverage_complete"])
        self.assertIsNone(result["totals"]["total_gallons"])
        self.assertGreater(result["observed_partial_consumption"]["total_liters"], 0)

    def test_negative_boolean_and_nonfinite_rates_are_not_consumption(self):
        rows = self.rows(
            {"propulsion.port.revolutions": 5, "propulsion.starboard.revolutions": 5, "propulsion.port.fuel.rate": -1, "propulsion.starboard.fuel.rate": True},
        )
        with mock.patch.object(app, "query_influx", return_value=rows):
            result = app.fuel_usage_summary(fuel_window(), use_cache=False)
        self.assertFalse(result["coverage_complete"])
        self.assertIsNone(result["observed_partial_consumption"]["total_liters"])
        rows = self.rows({"propulsion.port.revolutions": 5, "propulsion.starboard.revolutions": 5, "propulsion.port.fuel.rate": float("nan"), "propulsion.starboard.fuel.rate": float("inf")})
        with mock.patch.object(app, "query_influx", return_value=rows):
            result = app.fuel_usage_summary(fuel_window(), use_cache=False)
        self.assertIsNone(result["observed_partial_consumption"]["total_liters"])

    def test_genuine_zero_rate_is_complete(self):
        rows = self.rows({"propulsion.port.revolutions": 5, "propulsion.starboard.revolutions": 5, "propulsion.port.fuel.rate": 0, "propulsion.starboard.fuel.rate": 0})
        with mock.patch.object(app, "query_influx", return_value=rows):
            result = app.fuel_usage_summary(fuel_window(), use_cache=False)
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["totals"]["total_liters"], 0)

    def test_sparse_returned_rows_are_described_as_sampled_observations(self):
        rows = self.rows(
            {"propulsion.port.revolutions": 5, "propulsion.starboard.revolutions": 5, "propulsion.port.fuel.rate": 0.001, "propulsion.starboard.fuel.rate": 0.001},
            {"propulsion.port.revolutions": 5, "propulsion.starboard.revolutions": 5, "propulsion.port.fuel.rate": 0.001, "propulsion.starboard.fuel.rate": 0.001},
        )
        with mock.patch.object(app, "query_influx", return_value=rows):
            result = app.fuel_usage_summary(fuel_window(), use_cache=False)
        answer = app.answer_from_fuel_usage({"question_type": {"fuel_usage": True}, "fuel_usage": result})
        self.assertIn("sampled running minutes", answer)
        self.assertEqual(result["coverage_scope"], "returned_observation_minutes")


class BriefingCorrectnessTests(unittest.TestCase):
    def test_missing_stale_future_and_error_values_are_unavailable(self):
        now = dt.datetime.now(dt.timezone.utc)
        context = {"current_telemetry": {"values": {"propulsion.port.revolutions": 0, "propulsion.starboard.revolutions": 0, "electrical.batteries.shunt.voltage": 12}, "timestamps": {"propulsion.port.revolutions": (now - dt.timedelta(seconds=700)).isoformat(), "propulsion.starboard.revolutions": (now + dt.timedelta(seconds=90)).isoformat()}}, "ha_states": {}, "question_type": {}}
        answer = app.answer_from_briefing("brief", context)
        # A stale reading is no longer "unavailable": it says it is old (see DataAgeBriefingTests).
        self.assertIn("Engine data is not reporting", answer)
        self.assertNotIn("Engines: port 0 RPM", answer)
        self.assertIn("starboard unavailable", answer)


NOW = dt.datetime(2026, 11, 14, 12, 0, 0, tzinfo=dt.timezone.utc)


def iso(seconds_ago: float) -> str:
    return (NOW - dt.timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


class DataAgeBriefingTests(unittest.TestCase):
    SOC = "electrical.batteries.shunt.capacity.stateOfCharge"
    VOLT = "electrical.batteries.shunt.voltage"

    def brief(self, values, ages):
        context = {
            "now_utc": iso(0),
            "current_telemetry": {"values": values, "timestamps": {k: iso(v) for k, v in ages.items()}},
            "ha_states": {}, "question_type": {},
        }
        return app.answer_from_briefing("brief", context)

    def test_fresh_soc_prints_as_before(self):
        answer = self.brief({self.SOC: 85, self.VOLT: 12.8}, {self.SOC: 30, self.VOLT: 30})
        self.assertIn(": 85%, 12.80 V.", answer)

    def test_two_day_old_soc_says_how_old(self):
        answer = self.brief({self.SOC: 85, self.VOLT: 12.8}, {self.SOC: 2 * 86400 + 60, self.VOLT: 30})
        self.assertIn("SOC: no recent data (last 85%, 2 days ago)", answer)
        self.assertIn("12.80 V", answer)

    def test_no_reading_is_unavailable(self):
        answer = self.brief({}, {})
        self.assertIn("SOC unavailable", answer)
        self.assertIn("voltage unavailable", answer)
        self.assertNotIn("no recent data", answer)

    def test_stale_voltage_says_how_old(self):
        answer = self.brief({self.SOC: 85, self.VOLT: 12.8}, {self.SOC: 30, self.VOLT: 3 * 3600})
        self.assertIn("voltage: no recent data (last 12.80 V, 3 h ago)", answer)

    def test_stale_rpm_is_not_current_and_never_zero(self):
        values = {"propulsion.port.revolutions": 1800, "propulsion.starboard.revolutions": 0}
        answer = self.brief(values, {"propulsion.port.revolutions": 86400 * 2, "propulsion.starboard.revolutions": 86400 * 2})
        self.assertIn("Engine data is not reporting (normal when engines and their gateway are off); last reading", answer)
        self.assertIn("2 days ago", answer)
        self.assertNotIn("Engines: port 1800", answer)
        self.assertNotIn("port 1800 RPM,", answer.split("last reading")[0])
        self.assertNotIn("Engines: port 0 RPM", answer)

    def test_fresh_rpm_prints_current(self):
        answer = self.brief({"propulsion.port.revolutions": 1200, "propulsion.starboard.revolutions": 1200},
                            {"propulsion.port.revolutions": 5, "propulsion.starboard.revolutions": 5})
        self.assertIn("port 1200 RPM, starboard 1200 RPM.", answer)
        self.assertNotIn("not reporting", answer)


class DataAgeBatteryVoltageTests(unittest.TestCase):
    def summary(self, vmin, vmax, last_age_seconds=1800, with_utc=True, soc=True):
        data = {
            "samples": 48, "label": "last 14 days", "battery_label": "House",
            "last_sample_local": "Wed 2026-11-11 09:00 UTC",
            "voltage": {"count": 48, "min": vmin, "avg": (vmin + vmax) / 2, "max": vmax, "latest": vmax},
            "smartshunt_soc": {"count": 48, "min": 60.0, "max": 95.0, "latest": 90.0} if soc else {"count": 0},
            "thresholds": {"below_11_8_samples": 0, "below_12_05_samples": 0, "charging_or_float_samples": 0},
            "agm_voltage_soc_estimate": {"latest_voltage_percent": 80.0},
        }
        if with_utc:
            data["last_sample_utc"] = iso(last_age_seconds)
        return data

    def answer(self, summary):
        return app.answer_from_battery_voltage({"question_type": {"battery_voltage_history": True}, "battery_voltage": summary, "now_utc": iso(0)})

    def test_latest_values_carry_sample_time(self):
        answer = self.answer(self.summary(12.2, 12.9))
        self.assertIn("latest 12.9 V (hourly mean, Wed 2026-11-11 09:00 UTC)", answer)
        self.assertIn("latest 90% (hourly mean, Wed 2026-11-11 09:00 UTC)", answer)
        self.assertIn("No hourly samples were below the configured voltage thresholds.", answer)
        self.assertIn("Resting-voltage estimate", answer)
        self.assertNotIn("No samples since", answer)
        self.assertNotIn("only for 12 V banks", answer)

    def test_old_last_sample_adds_not_current_sentence(self):
        answer = self.answer(self.summary(12.2, 12.9, last_age_seconds=3 * 86400))
        self.assertIn("No samples since Wed 2026-11-11 09:00 UTC; the readings above are not current.", answer)

    def test_cached_summary_without_timestamp_omits_sentence(self):
        answer = self.answer(self.summary(12.2, 12.9, with_utc=False))
        self.assertNotIn("No samples since", answer)

    def test_24v_bank_gets_numbers_without_12v_commentary(self):
        answer = self.answer(self.summary(25.2, 27.6))
        self.assertIn("min 25.2 V", answer)
        self.assertIn("Voltage guidance is shown only for 12 V banks; this bank's readings (25.2-27.6 V) are outside that range.", answer)
        for phrase in ("thresholds", "charging/float", "Resting-voltage estimate", "Latest voltage is high", "Low-voltage flags"):
            self.assertNotIn(phrase, answer)
        self.assertIn("SOC: 60-95%", answer)


class DataAgeGenericHistoryTests(unittest.TestCase):
    def test_latest_carries_its_time(self):
        history = {"label": "last 7 days", "start_local": "a", "stop_local": "b", "buckets": {app.INFLUX_HISTORY_BUCKET: {
            "numeric_summary": {"environment.outside.temperature": {"count": 3, "min": 280.0, "avg": 281.0, "max": 282.0, "last": 281.5,
                                "min_time_local": "t1", "max_time_local": "t2", "last_time_local": "Fri 2026-11-13 18:00 UTC"}}}}}
        answer = app.answer_from_generic_history({"question_type": {"generic_history": True}, "influx_history": history, "match_tokens": ["temperature"]})
        self.assertRegex(answer, r"latest [^,;(]+ at Fri 2026-11-13 18:00 UTC \(3 samples")

    def test_missing_time_keeps_old_shape(self):
        history = {"label": "x", "start_local": "a", "stop_local": "b", "buckets": {app.INFLUX_HISTORY_BUCKET: {
            "numeric_summary": {"environment.outside.temperature": {"count": 3, "min": 1.0, "avg": 2.0, "max": 3.0, "last": 2.5}}}}}
        answer = app.answer_from_generic_history({"question_type": {"generic_history": True}, "influx_history": history, "match_tokens": ["temperature"]})
        self.assertIn("latest 2.5 F (3 samples", answer)


class FuelNoDataTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(telemetry_cache, "put_summary")
        patcher.start(); self.addCleanup(patcher.stop)

    def run_summary(self, rows, minutes=60):
        window = fuel_window()
        window["stop"] = window["start"] + dt.timedelta(minutes=minutes)
        with mock.patch.object(app, "query_influx", return_value=rows):
            return app.fuel_usage_summary(window, use_cache=False)

    def answer(self, summary):
        return app.answer_from_fuel_usage({"question_type": {"fuel_usage": True}, "fuel_usage": summary})

    @staticmethod
    def row(minute, rpm):
        return {"_time": f"2025-01-01T00:{minute:02d}:00Z", "propulsion.port.revolutions": rpm / 60.0, "propulsion.starboard.revolutions": rpm / 60.0,
                "propulsion.port.fuel.rate": 0.001, "propulsion.starboard.fuel.rate": 0.001}

    def test_zero_rows_is_no_data_not_not_running(self):
        summary = self.run_summary([])
        self.assertEqual(summary["sample_counts"]["observed_minutes"], 0)
        answer = self.answer(summary)
        self.assertIn("No engine or fuel data was recorded for test", answer)
        self.assertIn("telemetry may not have been logging), so fuel use cannot be determined.", answer)
        self.assertNotIn("Engines were not running", answer)

    def test_rows_with_only_fuel_rate_count_as_no_engine_data(self):
        summary = self.run_summary([{"_time": "2025-01-01T00:00:00Z", "propulsion.port.fuel.rate": 0.001}])
        self.assertEqual(summary["sample_counts"]["observed_minutes"], 0)

    def test_rpm_below_threshold_means_engines_not_running(self):
        summary = self.run_summary([self.row(0, 0), self.row(1, 0), self.row(2, 0)])
        self.assertEqual(summary["sample_counts"]["observed_minutes"], 3)
        answer = self.answer(summary)
        self.assertIn("Engines were not running during test", answer)
        self.assertIn("3 minutes of engine data recorded", answer)
        self.assertNotIn("No engine or fuel data", answer)

    def test_normal_running_window_unchanged(self):
        summary = self.run_summary([self.row(0, 1500), self.row(1, 1500), self.row(2, 1500)])
        self.assertEqual(summary["gap_minutes"], 0)
        answer = self.answer(summary)
        self.assertIn("across 3 sampled running minutes", answer)
        self.assertNotIn("missing inside the running period", answer)

    def test_cached_summary_without_new_field_keeps_old_wording(self):
        summary = {"label": "test", "start_local": "s", "stop_local": "e", "running_minutes": 0, "rpm_threshold": 400, "sample_counts": {"running_minutes": 0}}
        self.assertIn("I found no engine-running fuel samples for test", self.answer(summary))
        summary.pop("sample_counts")
        self.assertIn("I found no engine-running fuel samples for test", self.answer(summary))

    def test_gap_inside_running_span_is_reported_as_minimum(self):
        rows = [self.row(0, 1500), self.row(1, 1500), self.row(12, 1500), self.row(13, 1500)]
        summary = self.run_summary(rows)
        self.assertEqual(summary["gap_minutes"], 10)
        self.assertEqual(summary["largest_gap_minutes"], 10)
        self.assertIn("About 10 minutes of data are missing inside the running period, so the total is a minimum.", self.answer(summary))

    def test_observed_idle_minutes_inside_span_are_not_gaps(self):
        rows = [self.row(0, 1500)] + [self.row(m, 0) for m in range(1, 11)] + [self.row(11, 1500)]
        self.assertEqual(self.run_summary(rows)["gap_minutes"], 0)

    def test_short_gap_is_ignored(self):
        rows = [self.row(0, 1500), self.row(4, 1500)]
        self.assertEqual(self.run_summary(rows)["gap_minutes"], 0)


class PowerCorrectnessTests(unittest.TestCase):
    def put(self, path, poll, source, quality="verified"):
        telemetry_cache.put_power_observation(shore_power_state="off", port_rpm=0, starboard_rpm=0, battery_power_w=100, battery_current_a=8, battery_voltage_v=13, battery_soc_pct=80, observed_at=poll, source_observed_at=source, source_timestamp_quality=quality, path=path)

    def test_repeated_out_of_order_and_legacy_samples_do_not_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "power.sqlite"
            self.put(path, 100, 100); self.put(path, 200, 100); self.put(path, 300, 90); self.put(path, 400, 110); self.put(path, 500, 120, "legacy_unverified")
            summary = telemetry_cache.power_tracking_summary(days=1, now=500, path=path)
            self.assertEqual(summary["valid_interval_count"], 0)
            self.assertEqual(summary["unverified_legacy_observation_count"], 1)

    def test_boolean_power_and_negative_rpm_are_not_qualifying(self):
        observation = {"shore_power_state": "off", "port_rpm": -1, "starboard_rpm": 0, "battery_power_w": True}
        self.assertEqual(telemetry_cache.classify_power_observation(observation), "engine_state_unknown")
        self.assertIsNone(telemetry_cache._optional_float(True))

    def test_refresh_rejects_stale_shore_and_distance(self):
        now = dt.datetime.now(dt.timezone.utc)
        telemetry = {"values": {"propulsion.port.revolutions": 0, "propulsion.starboard.revolutions": 0, "electrical.batteries.shunt.power": 100}, "timestamps": {key: now.isoformat() for key in ("propulsion.port.revolutions", "propulsion.starboard.revolutions", "electrical.batteries.shunt.power")}}
        old = (now - dt.timedelta(seconds=700)).isoformat()
        ha = {"binary_sensor.shore_power_connected": {"state": "off", "last_updated": old}, "sensor.distance_from_dock": {"state": "2000", "last_updated": old, "attributes": {"unit_of_measurement": "ft"}}}
        with mock.patch.object(telemetry_cache, "put_power_observation", return_value={"classification": "shore_connection_unknown"}) as put, mock.patch.object(telemetry_cache, "prune_power_observations"), mock.patch.object(telemetry_cache, "power_tracking_summary", return_value={}), mock.patch.object(telemetry_cache, "put_summary"), mock.patch.object(telemetry_cache, "prune_category"):
            import telemetry_indexer
            telemetry_indexer.refresh_solar_inference(telemetry, ha)
        self.assertEqual(put.call_args.kwargs["shore_power_state"], "unknown")

    def test_refresh_allows_fresh_power_and_rpm_without_optional_fields(self):
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        telemetry = {"values": {"propulsion.port.revolutions": 0, "propulsion.starboard.revolutions": 0, "electrical.batteries.shunt.power": 100}, "timestamps": {"propulsion.port.revolutions": now, "propulsion.starboard.revolutions": now, "electrical.batteries.shunt.power": now}}
        ha = {"binary_sensor.shore_power_connected": {"state": "off", "last_updated": now}}
        import telemetry_indexer
        with mock.patch.object(telemetry_cache, "put_power_observation", return_value={}), mock.patch.object(telemetry_cache, "prune_power_observations"), mock.patch.object(telemetry_cache, "power_tracking_summary", return_value={}), mock.patch.object(telemetry_cache, "put_summary"), mock.patch.object(telemetry_cache, "prune_category"):
            telemetry_indexer.refresh_solar_inference(telemetry, ha)

    def test_explicit_profile_excludes_unfingerprinted_power_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "power.sqlite"
            self.put(path, 100, 100)
            conn = telemetry_cache.connect_power(path)
            conn.execute("update power_observations set profile_fingerprint = NULL")
            conn.commit(); conn.close()
            summary = telemetry_cache.power_tracking_summary(days=1, now=200, path=path, profile_fingerprint="explicit-profile")
            self.assertEqual(summary["valid_interval_count"], 0)
            self.assertEqual(summary["qualifying_sample_count"], 0)




class DataAgeReachesModelTests(unittest.TestCase):
    NOW = dt.datetime(2026, 11, 14, 12, 0, 0, tzinfo=dt.timezone.utc)

    @staticmethod
    def node(value, seconds_ago=None, now=NOW):
        node = {"value": value}
        if seconds_ago is not None:
            node["timestamp"] = (now - dt.timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")
        return node

    def tree(self):
        n = self.node
        return {
            "navigation": {"datetime": n(self.NOW.isoformat().replace("+00:00", "Z"), 1)},
            "environment": {"depth": {"belowTransducer": n(3.2, 2)}},
            "electrical": {"batteries": {"shunt": {
                "capacity": {"stateOfCharge": n(0.85, 172800)},
                "voltage": n(13.1, 172800),
            }}},
            "propulsion": {"port": {"revolutions": n(30.0, 172800)}},
        }

    def snapshot(self, tree=None):
        profile = {"profile": {"battery_banks": [{"id": "shunt", "path": "electrical.batteries.shunt"}],
                               "engines": [{"id": "port", "path": "propulsion.port"}], "fuel_tanks": []},
                   "diagnostic": None, "fingerprint": "x"}
        with mock.patch.object(app, "get_signalk_self", return_value=tree or self.tree()), \
                mock.patch.object(app.profile_runtime, "current", return_value=profile):
            return app.current_telemetry_snapshot("battery voltage", now=self.NOW)

    def model_copy(self, snap):
        return app.compact_context({"current_telemetry": snap, "question_type": {"current_request": True}})["current_telemetry"]

    def test_battery_shunt_two_days_old(self):
        snap = self.snapshot()
        soc = "electrical.batteries.shunt.capacity.stateOfCharge"
        volts = "electrical.batteries.shunt.voltage"
        self.assertEqual(snap["values"][soc], 85.0)
        self.assertEqual(snap["freshness"][soc], "stale")
        self.assertAlmostEqual(snap["ages_seconds"][volts], 172800, delta=1)
        self.assertEqual(snap["observed_at_utc"], "2026-11-14T12:00:00Z")
        self.assertEqual(snap["fresh_threshold_seconds"], 600)
        self.assertEqual(snap["clock_status"], "ok")
        shown = self.model_copy(snap)
        for path in (soc, volts):
            self.assertNotIn(path, shown["values"])
            self.assertIn(path, shown["stale_values"])
            self.assertEqual(shown["stale_age"][path], "2 days")
            self.assertTrue(shown["last_update_local"][path])
        self.assertIn("environment.depth.belowTransducer", shown["values"])
        for dropped in ("timestamps", "ages_seconds", "freshness"):
            self.assertNotIn(dropped, shown)
        self.assertIn("units", shown)
        # The stored snapshot is untouched by the model copy.
        self.assertIn(soc, snap["values"])
        self.assertIn("freshness", snap)

    def test_stale_gps_time_leaves_clock_unverified(self):
        tree = self.tree()
        tree["navigation"]["datetime"] = self.node("2026-11-12T12:00:00Z", 172800)
        self.assertEqual(self.snapshot(tree)["clock_status"], "unverified")

    def test_stale_engine_rpm_is_not_current(self):
        tree = self.tree()
        tree["propulsion"]["port"]["revolutions"] = self.node(30.0, 172800)  # 1800 rpm
        shown = self.model_copy(self.snapshot(tree))
        self.assertNotIn("propulsion.port.revolutions", shown["values"])
        self.assertEqual(shown["stale_values"]["propulsion.port.revolutions"], 1800.0)

    def test_missing_and_future_timestamps(self):
        tree = self.tree()
        tree["environment"]["depth"]["belowTransducer"] = self.node(3.2)
        tree["navigation"]["speedOverGround"] = self.node(2.0, -300)
        snap = self.snapshot(tree)
        self.assertEqual(snap["freshness"]["environment.depth.belowTransducer"], "missing")
        self.assertEqual(snap["freshness"]["navigation.speedOverGround"], "future")
        shown = self.model_copy(snap)
        self.assertEqual(shown["stale_age"]["environment.depth.belowTransducer"], "unknown (no source timestamp)")
        self.assertEqual(shown["stale_age"]["navigation.speedOverGround"], "unverifiable (timestamp in the future)")
        self.assertNotIn("environment.depth.belowTransducer", shown["values"])
        self.assertNotIn("navigation.speedOverGround", shown["values"])

    def test_trimmed_context_never_merges_stale_into_values(self):
        shown = self.model_copy(self.snapshot())
        soc = "electrical.batteries.shunt.capacity.stateOfCharge"
        small = json.loads(app.serialize_context_with_budget({"current_telemetry": shown}, 400))
        telemetry = small.get("current_telemetry")
        self.assertTrue(telemetry is None or soc not in telemetry.get("values", {}))
        self.assertTrue(small["context_trimming"]["incomplete"])

if __name__ == "__main__":
    unittest.main()
