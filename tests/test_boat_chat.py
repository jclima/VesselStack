#!/usr/bin/env python3

from __future__ import annotations

import datetime as dt
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "boat-chat"))

import app  # noqa: E402
import telemetry_cache  # noqa: E402


class RuntimeConfigurationTests(unittest.TestCase):
    def test_signalk_url_comes_from_environment(self) -> None:
        with mock.patch.dict(os.environ, {"SIGNALK_URL": "http://signalk.test:3000"}):
            with mock.patch.object(app, "http_get_json", return_value={}) as request:
                app.get_signalk_self()
        self.assertEqual(
            request.call_args.args[0],
            "http://signalk.test:3000/signalk/v1/api/vessels/self",
        )

    def test_home_assistant_url_and_token_come_from_environment(self) -> None:
        environment = {
            "HOME_ASSISTANT_URL": "http://ha.test:8123",
            "HOME_ASSISTANT_TOKEN": "test-token",
        }
        with mock.patch.dict(os.environ, environment):
            with mock.patch.object(app, "http_get_json", return_value=[]) as request:
                app.get_ha_all_states()
        self.assertEqual(request.call_args.args[0], "http://ha.test:8123/api/states")
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer test-token")

    def test_influx_credentials_come_from_environment(self) -> None:
        environment = {
            "INFLUXDB_URL": "http://influx.test:8086",
            "INFLUXDB_ORG": "test-org",
            "INFLUXDB_TOKEN": "test-token",
        }
        with mock.patch.dict(os.environ, environment):
            settings = app.load_influx_env()
        self.assertEqual(settings["INFLUXDB_URL"], environment["INFLUXDB_URL"])
        self.assertEqual(settings["INFLUXDB_ORG"], environment["INFLUXDB_ORG"])
        self.assertEqual(settings["INFLUXDB_TOKEN"], environment["INFLUXDB_TOKEN"])


class QuestionClassificationTests(unittest.TestCase):
    def test_current_engine_diagnostic_is_not_fuel_usage(self) -> None:
        question = (
            "Is anything wrong with the port engine right now? Use RPM, oil pressure, "
            "coolant temperature, alarms, alternator voltage, load, boost, and fuel rate."
        )
        kind = app.classify_question(question)
        self.assertFalse(kind["fuel_usage"])
        self.assertTrue(kind["generic_telemetry"])
        self.assertIn("oil_pressure", kind["concepts"])
        self.assertIn("fuel_rate", kind["concepts"])

    def test_overview_and_simple_history_keep_ha_fallback(self) -> None:
        self.assertTrue(app.should_collect_ha_telemetry(app.classify_question("What telemetry history can you see?")))
        self.assertTrue(app.should_collect_ha_telemetry(app.classify_question("Depth trend over the last 24 hours")))

    def test_multi_signal_extrema_question_is_complex_history(self) -> None:
        question = (
            "When did the port engine oil pressure reach its minimum in the last 30 days, "
            "and what were RPM, coolant temperature, and load at that time?"
        )
        kind = app.classify_question(question)
        self.assertTrue(kind["generic_history"])
        self.assertTrue(kind["complex_history"])
        self.assertEqual(
            {"engine", "load", "oil_pressure", "rpm", "temperature"},
            set(kind["concepts"]),
        )

    def test_explicit_fuel_usage_still_routes_deterministically(self) -> None:
        kind = app.classify_question("How much fuel did I use last weekend?")
        self.assertTrue(kind["fuel_usage"])
        self.assertFalse(kind["fuel_usage_needs_window"])

    def test_fuel_remaining_is_not_fuel_usage(self) -> None:
        kind = app.classify_question("How much fuel remains and what range does that imply?")
        self.assertFalse(kind["fuel_usage"])
        self.assertIn("fuel_level", kind["concepts"])

    def test_boat_sensor_question_is_not_identity(self) -> None:
        kind = app.classify_question("Which boat sensors have stopped updating?")
        self.assertFalse(kind["identity"])
        self.assertIn("freshness", kind["concepts"])

    def test_stopped_reporting_is_sensor_freshness(self) -> None:
        question = (
            "Which boat sensors have stopped reporting, and distinguish confirmed unavailable "
            "sensors from values that merely have old Home Assistant timestamps."
        )
        kind = app.classify_question(question)
        self.assertIn("freshness", kind["concepts"])
        self.assertNotIn("ais", kind["concepts"])

    def test_solar_contribution_routes_to_inference(self) -> None:
        kind = app.classify_question("How much did the solar panel contribute while shore power was off last week?")
        self.assertTrue(kind["solar"])
        self.assertFalse(kind["solar_hardware"])
        self.assertIn("solar", kind["concepts"])
        self.assertEqual(app.resolve_solar_history_days("last week"), 7)

    def test_solar_hardware_question_searches_monitoring_docs(self) -> None:
        kind = app.classify_question("What should I add to measure solar panel output directly?")
        self.assertTrue(kind["solar_hardware"])
        self.assertTrue(app.should_search_docs(kind, "What should I add to measure solar panel output directly?"))


class SemanticMatchingTests(unittest.TestCase):
    def test_multi_signal_query_matches_each_requested_measurement(self) -> None:
        question = "Compare port oil pressure, coolant temperature, load, boost, and RPM over the last week."
        for measurement in [
            "propulsion.port.oilPressure",
            "propulsion.port.temperature",
            "propulsion.port.engineLoad",
            "propulsion.port.boostPressure",
            "propulsion.port.revolutions",
        ]:
            self.assertGreater(app.semantic_match_score(measurement, question), 0, measurement)
        self.assertEqual(app.semantic_match_score("propulsion.starboard.oilPressure", question), 0)

    def test_engine_temperature_does_not_pull_cabin_temperature(self) -> None:
        question = "What is the port engine coolant temperature?"
        self.assertGreater(app.semantic_match_score("sensor.engine_port_coolant_temp", question), 0)
        self.assertEqual(app.semantic_match_score("sensor.h5100_1625_temperature", question), 0)

    def test_personal_device_is_not_a_freshness_candidate(self) -> None:
        state = {
            "entity_id": "sensor.example_phone_battery_level",
            "state": "unavailable",
            "attributes": {"unit_of_measurement": "%", "device_class": "battery"},
        }
        self.assertFalse(app.freshness_candidate(state))


class HistoryAnalysisTests(unittest.TestCase):
    def test_summary_preserves_extrema_times(self) -> None:
        rows = [
            {"_measurement": "propulsion.port.oilPressure", "_time": "2026-07-01T10:00:00Z", "_value": "50"},
            {"_measurement": "propulsion.port.oilPressure", "_time": "2026-07-01T11:00:00Z", "_value": "20"},
            {"_measurement": "propulsion.port.oilPressure", "_time": "2026-07-01T12:00:00Z", "_value": "70"},
        ]
        summary = app.summarize_numeric_rows(rows)["propulsion.port.oilPressure"]
        self.assertEqual(summary["min"], 20.0)
        self.assertEqual(summary["min_time_utc"], "2026-07-01T11:00:00Z")
        self.assertEqual(summary["max_time_utc"], "2026-07-01T12:00:00Z")

    def test_running_filter_keeps_aligned_rows(self) -> None:
        rows = [
            {"_measurement": "propulsion.port.revolutions", "_time": "a", "_value": "0"},
            {"_measurement": "propulsion.port.oilPressure", "_time": "a", "_value": "0"},
            {"_measurement": "propulsion.port.revolutions", "_time": "b", "_value": "1200"},
            {"_measurement": "propulsion.port.oilPressure", "_time": "b", "_value": "40"},
        ]
        filtered = app.engine_running_history_rows(rows)
        self.assertEqual({row["_time"] for row in filtered}, {"b"})
        self.assertEqual(len(filtered), 2)

    def test_side_comparison_includes_rpm_matched_samples(self) -> None:
        rows = [
            {"_measurement": "propulsion.port.revolutions", "_time": "a", "_value": "1800"},
            {"_measurement": "propulsion.starboard.revolutions", "_time": "a", "_value": "1820"},
            {"_measurement": "propulsion.port.fuel.rate", "_time": "a", "_value": "4"},
            {"_measurement": "propulsion.starboard.fuel.rate", "_time": "a", "_value": "4.4"},
        ]
        selected = [
            "propulsion.port.fuel.rate",
            "propulsion.starboard.fuel.rate",
            "propulsion.port.revolutions",
            "propulsion.starboard.revolutions",
        ]
        analysis = app.analyze_history_rows("compare fuel at comparable RPM", rows, selected, 1)
        fuel = next(
            item
            for item in analysis["side_comparisons"]
            if item["port_measurement"] == "propulsion.port.fuel.rate"
        )
        self.assertEqual(fuel["rpm_matched_within_50"]["samples"], 1)
        self.assertEqual(fuel["rpm_matched_within_50"]["starboard_percent_vs_port"], 10.0)

    def test_unit_conversions_use_boat_display_units(self) -> None:
        self.assertAlmostEqual(app.convert_generic_value("environment.depth.belowTransducer", 3.0), 9.8425, places=3)
        self.assertEqual(app.convert_generic_value("propulsion.port.engineLoad", 0.5), 50.0)
        self.assertEqual(app.convert_generic_value("electrical.batteries.shunt.capacity.stateOfCharge", 0.8), 80.0)
        self.assertEqual(app.unit_for_measurement("propulsion.port.temperature", {}), "F")
        self.assertEqual(app.unit_for_measurement("environment.depth.belowTransducer", {}), "ft")

    def test_complex_focus_leads_with_requested_extremum(self) -> None:
        history = {
            "label": "last 30 days",
            "buckets": {
                "vesselstack_1m": {
                    "numeric_summary": {
                        "propulsion.port.oilPressure": {"min": 0.0, "min_time_local": "off"}
                    },
                    "engine_running_numeric_summary": {
                        "propulsion.port.oilPressure": {
                            "min": 10.1,
                            "min_time_local": "2025-01-17 19:30 UTC",
                        }
                    },
                    "engine_running_analysis": {
                        "notable_snapshots": [
                            {
                                "reason": "propulsion.port.oilPressure minimum",
                                "values": {"propulsion.port.revolutions": 205.0},
                            }
                        ]
                    },
                    "units": {"propulsion.port.oilPressure": "psi"},
                }
            },
        }
        focus = app.complex_answer_focus("When was port oil pressure at its minimum?", history)
        self.assertEqual(focus["measurement"], "propulsion.port.oilPressure")
        self.assertEqual(focus["extrema"][0]["value"], "10.1")
        self.assertTrue(focus["extrema"][0]["engine_running_only"])


class WindowAndConversationTests(unittest.TestCase):
    def test_numeric_history_window(self) -> None:
        now = dt.datetime(2026, 7, 24, 12, 0, tzinfo=app.LOCAL_TZ)
        window = app.resolve_fuel_usage_window("last 12 hours", now=now)
        self.assertEqual(window["label"], "last 12 hours")
        self.assertEqual(window["stop"] - window["start"], dt.timedelta(hours=12))

    def test_followup_time_window_keeps_prior_intent(self) -> None:
        history = [
            {"role": "user", "content": "How much fuel did I use?"},
            {"role": "assistant", "content": "What time window should I use?"},
        ]
        interpreted = app.effective_question("last 30 days", history)
        kind = app.classify_question(interpreted)
        self.assertTrue(kind["fuel_usage"])
        self.assertFalse(kind["fuel_usage_needs_window"])

    def test_rpm_band(self) -> None:
        self.assertEqual(
            app.resolve_rpm_band("Fuel economy with both engines between 1800 and 2200 RPM"),
            (1800.0, 2200.0),
        )

    def test_complex_history_gets_minimum_context_budget(self) -> None:
        context = {"question_type": {"complex_history": True}}
        self.assertGreaterEqual(app.context_char_budget(context), 12000)

    def test_freshness_answer_does_not_call_stale_values_failed(self) -> None:
        context = {
            "question_type": {"concepts": ["freshness"]},
            "ha_telemetry": {
                "unavailable": [
                    {
                        "friendly_name": "Bathroom Temperature",
                        "state": "unavailable",
                        "last_updated_local": "2025-01-21 16:26 UTC",
                    }
                ],
                "stale_over_6h_count": 12,
            },
            "current_telemetry": {"available_path_count": 216, "signalk_error": None},
        }
        answer = app.answer_from_freshness(context)
        self.assertIn("Confirmed unavailable sensors", answer)
        self.assertIn("not proof they stopped updating", answer)
        self.assertIn("Live SignalK is reachable", answer)

    def test_ais_freshness_does_not_use_sensor_freshness_answer(self) -> None:
        context = {
            "question_type": {"concepts": ["ais", "freshness"]},
            "current_telemetry": {
                "ais": {
                    "targets": [
                        {
                            "name": "TEST TARGET A",
                            "mmsi": "000000001",
                            "distance_nm": 1.27,
                            "position_age_minutes": 57.0,
                            "position_stale": True,
                        },
                        {
                            "name": "TEST TARGET B",
                            "mmsi": "000000002",
                            "distance_nm": 0.07,
                            "position_age_minutes": 0.2,
                            "position_stale": False,
                        },
                    ]
                }
            },
            "ha_telemetry": {"unavailable": [{"friendly_name": "Bathroom Temperature"}]},
        }
        answer = app.answer_from_ais_freshness(context)
        self.assertIn("TEST TARGET A", answer)
        self.assertIn("1.27 nm", answer)
        self.assertIn("57.0 minutes", answer)
        self.assertIsNone(app.answer_from_freshness(context))


class PowerTrackingTests(unittest.TestCase):
    def test_integrates_only_off_shore_engines_off_intervals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "power.sqlite"
            start = 1_700_000_000
            for offset, power in [(0, 100), (300, 200), (600, 100)]:
                telemetry_cache.put_power_observation(
                    shore_power_state="off",
                    port_rpm=0,
                    starboard_rpm=0,
                    battery_power_w=power,
                    battery_current_a=power / 13.0,
                    battery_voltage_v=13.0,
                    battery_soc_pct=80,
                    observed_at=start + offset,
                    path=path,
                )
            summary = telemetry_cache.power_tracking_summary(days=1, now=start + 600, path=path)
            self.assertAlmostEqual(summary["inferred_net_charge_wh"], 25.0)
            self.assertAlmostEqual(summary["qualifying_coverage_hours"], 1 / 6, places=3)
            self.assertEqual(summary["valid_interval_count"], 2)
            self.assertEqual(summary["peak_inferred_charge_w"], 200.0)

    def test_rejects_long_gaps_and_non_solar_conditions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "power.sqlite"
            start = 1_700_000_000
            observations = [
                (0, "off", 0, 100),
                (1200, "off", 0, 100),
                (1500, "on", 0, 100),
                (1800, "off", 1000, 100),
            ]
            for offset, shore, rpm, power in observations:
                telemetry_cache.put_power_observation(
                    shore_power_state=shore,
                    port_rpm=rpm,
                    starboard_rpm=rpm,
                    battery_power_w=power,
                    battery_current_a=power / 13.0,
                    battery_voltage_v=13.0,
                    battery_soc_pct=80,
                    observed_at=start + offset,
                    path=path,
                )
            summary = telemetry_cache.power_tracking_summary(days=1, now=start + 1800, path=path)
            self.assertEqual(summary["inferred_net_charge_wh"], 0.0)
            self.assertEqual(summary["valid_interval_count"], 0)
            self.assertEqual(summary["skipped_gap_count"], 1)

    def test_distance_from_dock_overrides_charging_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "power.sqlite"
            start = 1_700_000_000
            for offset in (0, 300):
                telemetry_cache.put_power_observation(
                    shore_power_state="on",
                    underway_state="off",
                    distance_from_dock_ft=2000,
                    dock_radius_ft=500,
                    port_rpm=0,
                    starboard_rpm=0,
                    battery_power_w=120,
                    battery_current_a=9.0,
                    battery_voltage_v=13.3,
                    battery_soc_pct=80,
                    observed_at=start + offset,
                    path=path,
                )
            summary = telemetry_cache.power_tracking_summary(days=1, now=start + 300, path=path)
            self.assertAlmostEqual(summary["inferred_net_charge_wh"], 10.0)
            self.assertEqual(summary["valid_interval_count"], 1)
            self.assertEqual(summary["latest_observation"]["classification"], "off_shore_engines_off")

    def test_answer_labels_solar_as_inferred_net_charge(self) -> None:
        context = {
            "question_type": {"solar": True},
            "solar_inference": {
                "label": "last 7 days",
                "tracking_started_local": "2025-01-24 16:00 UTC",
                "observation_count": 10,
                "valid_interval_count": 4,
                "qualifying_coverage_hours": 0.333,
                "inferred_net_charge_wh": 25.0,
                "observed_net_discharge_wh": 5.0,
                "peak_inferred_charge_w": 200.0,
                "peak_observed_discharge_w": 20.0,
                "confidence": "low",
            },
        }
        answer = app.answer_from_solar_inference(context)
        self.assertIn("Inferred net charge", answer)
        self.assertIn("not gross panel production", answer)

    def test_solar_hardware_answer_uses_existing_victron_path_first(self) -> None:
        answer = app.answer_from_solar_hardware(
            {"question_type": {"solar_hardware": True}}
        )
        self.assertIn("installed SignalK Victron BLE plugin", answer)
        self.assertIn("Simarine PICO", answer)
        self.assertIn("Voc", answer)


class RequestBodyTests(unittest.TestCase):
    def test_json_body_is_bounded_and_must_be_an_object(self) -> None:
        handler = object.__new__(app.BoatChatHandler)
        handler.headers = {"Content-Length": str(app.MAX_REQUEST_BODY + 1)}
        handler.rfile = io.BytesIO(b"")
        with self.assertRaisesRegex(ValueError, "at most"):
            handler.read_json_body()
        payload = b'["not", "an", "object"]'
        handler.headers = {"Content-Length": str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        with self.assertRaisesRegex(ValueError, "JSON object"):
            handler.read_json_body()


if __name__ == "__main__":
    unittest.main()
