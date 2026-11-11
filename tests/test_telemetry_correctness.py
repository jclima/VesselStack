from __future__ import annotations

import datetime as dt
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
        self.assertIn("port unavailable", answer)
        self.assertIn("starboard unavailable", answer)


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


if __name__ == "__main__":
    unittest.main()
