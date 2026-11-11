import tempfile
import time
import itertools
import unittest
from unittest import mock
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "boat-chat"))
import telemetry_cache
import telemetry_indexer
import status_model


class SolarProfileTests(unittest.TestCase):
    def test_refresh_invalid_profile_writes_barrier_for_following_valid_profile(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "power.sqlite"
            now = int(time.time())
            stamps = [now - 120, now - 60, now, now + 60]
            original_put = telemetry_cache.put_power_observation
            put_index = iter(range(4))
            def put_at_stamp(**kwargs):
                stamp = stamps[next(put_index)]
                kwargs["observed_at"] = stamp
                kwargs["source_observed_at"] = stamp
                return original_put(**kwargs)
            with mock.patch.object(telemetry_cache, "power_db_path", return_value=path), mock.patch.object(telemetry_cache, "prune_power_observations"), mock.patch.object(telemetry_cache, "put_power_observation", side_effect=put_at_stamp), mock.patch.object(telemetry_cache, "put_summary"), mock.patch.object(telemetry_cache, "prune_category"):
                valid = {"profile": {"engines": [], "battery_banks": [{"id": "house", "path": "electrical.batteries.house", "role": "house"}], "fuel_tanks": [], "primary_battery_bank": "house"}, "fingerprint": "A", "legacy": False}
                invalid = {"profile": {"engines": [], "battery_banks": [], "fuel_tanks": [], "primary_battery_bank": None}, "fingerprint": "B", "legacy": False}
                def sample(stamp):
                    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))
                    return ({"values": {"electrical.batteries.house.power": 100, "electrical.batteries.house.current": 8, "electrical.batteries.house.voltage": 12.4}, "timestamps": {"electrical.batteries.house.power": iso}}, {"binary_sensor.shore_power_connected": {"state": "off", "last_reported": iso}, "sensor.distance_from_dock": {"state": "1000", "last_reported": iso, "attributes": {"unit_of_measurement": "ft"}}, "input_number.dock_radius": {"state": "100", "last_reported": iso, "attributes": {"unit_of_measurement": "ft"}}})
                telemetry_indexer.refresh_solar_inference(*sample(stamps[0]), runtime=valid)
                telemetry_indexer.refresh_solar_inference({"values": {}, "timestamps": {}}, {}, runtime=invalid)
                telemetry_indexer.refresh_solar_inference(*sample(stamps[2]), runtime=valid)
                conn = telemetry_cache.connect_power(path)
                rows = [dict(row) for row in conn.execute("select profile_fingerprint, source_timestamp_quality, battery_power_w from power_observations order by observed_at").fetchall()]
                conn.close()
                self.assertEqual([row["profile_fingerprint"] for row in rows], ["A", "B", "A"])
                self.assertEqual(rows[0]["source_timestamp_quality"], "verified")
                self.assertNotEqual(rows[1]["source_timestamp_quality"], "verified")
                self.assertEqual(rows[2]["source_timestamp_quality"], "verified")
                summary = telemetry_cache.power_tracking_summary(days=1, now=stamps[2] + 1, path=path, profile_fingerprint="A")
                self.assertEqual(summary["inferred_net_charge_wh"], 0)
                telemetry_indexer.refresh_solar_inference(*sample(stamps[3]), runtime=valid)
                summary = telemetry_cache.power_tracking_summary(days=1, now=stamps[3] + 1, path=path, profile_fingerprint="A")
            self.assertGreater(summary["inferred_net_charge_wh"], 0)

    def test_refresh_uses_custom_primary_house_bank_and_status_warns_at_five_percent(self):
        profile = {"engines": [], "battery_banks": [{"id": "starter", "path": "electrical.batteries.starter", "role": "starter"}, {"id": "house", "path": "electrical.batteries.house", "role": "house"}], "fuel_tanks": [], "primary_battery_bank": "house"}
        runtime = {"profile": profile, "fingerprint": "custom", "legacy": False}
        with mock.patch.object(telemetry_cache, "put_power_observation", return_value={}) as put, mock.patch.object(telemetry_cache, "prune_power_observations"), mock.patch.object(telemetry_cache, "power_tracking_summary", return_value={}), mock.patch.object(telemetry_cache, "put_summary"), mock.patch.object(telemetry_cache, "prune_category"):
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            telemetry_indexer.refresh_solar_inference({"values": {"electrical.batteries.house.power": 100, "electrical.batteries.starter.power": 900}, "timestamps": {"electrical.batteries.house.power": stamp}}, {}, runtime=runtime)
        self.assertEqual(put.call_args.kwargs["battery_power_w"], 100)
        self.assertEqual(put.call_args.kwargs["source_timestamp_quality"], "verified")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cached = {"cache": {"age_seconds": 0}, "current_telemetry": {"values": {"electrical.batteries.house.capacity.stateOfCharge": 5, "electrical.batteries.house.voltage": 12.1, "electrical.batteries.starter.capacity.stateOfCharge": 80, "electrical.batteries.starter.voltage": 12.6}, "timestamps": {key: stamp for key in ("electrical.batteries.house.capacity.stateOfCharge", "electrical.batteries.house.voltage", "electrical.batteries.starter.capacity.stateOfCharge", "electrical.batteries.starter.voltage")}}, "ha_states": {}}
        status = status_model.normalize(cached, profile=profile)
        self.assertEqual(status["warning_count"], 1)
    def test_third_configured_engine_running_prevents_engines_off(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "power.sqlite"
            now = int(time.time())
            for stamp in (now - 60, now):
                row = telemetry_cache.put_power_observation(
                shore_power_state="off", distance_from_dock_ft=1000, dock_radius_ft=100,
                port_rpm=0, starboard_rpm=0, engine_rpms={"port": 0, "starboard": 0, "aux": 1200},
                battery_power_w=250, battery_current_a=10, battery_voltage_v=12, battery_soc_pct=50,
                observed_at=stamp, source_observed_at=stamp,
                source_timestamp_quality="verified", profile_fingerprint="profile-a", path=path,
            )
            self.assertEqual(row["classification"], "engine_running")
            summary = telemetry_cache.power_tracking_summary(days=1, now=now + 1, path=path, profile_fingerprint="profile-a")
            self.assertEqual(summary["qualifying_sample_count"], 0)

    def test_legacy_missing_dynamic_rpm_remains_unknown_after_readback(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "power.sqlite"; now = int(time.time())
            telemetry_cache.put_power_observation(shore_power_state="off", distance_from_dock_ft=1000, dock_radius_ft=100, port_rpm=None, starboard_rpm=None, battery_power_w=250, battery_current_a=10, battery_voltage_v=12, battery_soc_pct=50, observed_at=now, source_observed_at=now, source_timestamp_quality="verified", path=path)
            summary = telemetry_cache.power_tracking_summary(days=1, now=now + 1, path=path)
            self.assertEqual(summary["qualifying_sample_count"], 0)

    def test_zero_engines_is_explicitly_off_only_when_profile_state_is_present(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "power.sqlite"
            row = telemetry_cache.put_power_observation(
                shore_power_state="off", distance_from_dock_ft=1000, dock_radius_ft=100,
                port_rpm=None, starboard_rpm=None, engine_rpms={}, battery_power_w=250,
                battery_current_a=10, battery_voltage_v=12, battery_soc_pct=50,
                observed_at=int(time.time()), source_observed_at=int(time.time()),
                source_timestamp_quality="verified", profile_fingerprint="profile-a", path=path,
            )
            self.assertEqual(row["classification"], "off_shore_engines_off")


if __name__ == "__main__":
    unittest.main()
