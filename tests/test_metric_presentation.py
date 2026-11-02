import importlib.util
import os
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "boat-chat"))
spec = importlib.util.spec_from_file_location("metric_app", ROOT / "boat-chat" / "app.py")
APP = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(APP)


class MetricPresentationTests(unittest.TestCase):
    def _history(self, rows_by_bucket, ha=None, message="temperature history"):
        measurements = {bucket: sorted({str(row["_measurement"]) for row in rows}) for bucket, rows in rows_by_bucket.items()}
        def list_measurements(bucket):
            return measurements[bucket]
        def query(flux):
            bucket = APP.INFLUX_HOME_ASSISTANT_BUCKET if f'from(bucket: "{APP.INFLUX_HOME_ASSISTANT_BUCKET}")' in flux else APP.INFLUX_HISTORY_BUCKET
            rows = rows_by_bucket.get(bucket, [])
            return rows
        with mock.patch.object(APP, "influx_measurements", side_effect=list_measurements), mock.patch.object(APP, "query_influx", side_effect=query):
            return APP.generic_influx_history_summary(message, ha_telemetry=ha)

    def test_model_copy_converts_values_but_source_stays_canonical(self):
        source = {"influx_history": {"buckets": {"x": {"units": {"propulsion.port.temperature": "F"}, "numeric_summary": {"propulsion.port.temperature": {"min": 212, "count": 1, "delta": 18}}}}}}
        with mock.patch.dict(os.environ, {"BOAT_UNITS": "metric"}, clear=False):
            shown = APP.compact_context(source)
        self.assertEqual(source["influx_history"]["buckets"]["x"]["numeric_summary"]["propulsion.port.temperature"]["min"], 212)
        stats = shown["influx_history"]["buckets"]["x"]["numeric_summary"]["propulsion.port.temperature"]
        self.assertAlmostEqual(stats["min"], 100)
        self.assertEqual(stats["count"], 1)

    def test_thresholds_use_metric_query_units(self):
        with mock.patch.dict(os.environ, {"BOAT_UNITS": "metric"}, clear=False):
            self.assertEqual(APP._threshold_unit_for_measurement("propulsion.port.oilPressure", "kPa", "below 100"), ("psi", "kPa"))
            self.assertEqual(APP._threshold_unit_for_measurement("environment.outside.pressure", None, "barometer below 1000"), ("inHg", "hPa"))

    def test_barometric_threshold_defaults_follow_metric_or_customary_preference(self):
        for preference, expected in (("metric", "hPa"), ("us_customary", "inHg")):
            with mock.patch.dict(os.environ, {"BOAT_UNITS": preference}, clear=False):
                self.assertEqual(
                    APP._threshold_unit_for_measurement(
                        "environment.outside.pressure", None, "barometer below 30", "Pa"
                    ),
                    ("Pa", expected),
                )
                self.assertEqual(
                    APP._threshold_unit_for_measurement(
                        "environment.outside.pressure", None, "pressure below 30", "hPa"
                    ),
                    ("hPa", expected),
                )

    def test_explicit_barometric_threshold_unit_overrides_preference(self):
        with mock.patch.dict(os.environ, {"BOAT_UNITS": "metric"}, clear=False):
            self.assertEqual(
                APP._threshold_unit_for_measurement(
                    "environment.outside.pressure", "psi", "barometer below 30psi", "kPa"
                ),
                ("kPa", "psi"),
            )

    def test_actual_signalk_barometer_threshold_uses_pa_source_and_preserves_chart_values(self):
        rows = {
            APP.INFLUX_HISTORY_BUCKET: [
                {"_time": "2026-01-01T00:00:00Z", "_measurement": "environment.outside.pressure", "_value": str(29.5 * 3386.389)},
                {"_time": "2026-01-01T00:01:00Z", "_measurement": "environment.outside.pressure", "_value": str(31 * 3386.389)},
            ],
            APP.INFLUX_HOME_ASSISTANT_BUCKET: [],
        }
        with mock.patch.dict(os.environ, {"BOAT_UNITS": "us_customary"}, clear=False):
            history = self._history(rows, message="barometer below 30")
        bucket = history["buckets"][APP.INFLUX_HISTORY_BUCKET]
        result = bucket["analysis"]["threshold_analysis"][0]
        self.assertEqual(result["matching_samples"], 1)
        self.assertEqual(result["threshold_unit"], "inHg")
        self.assertEqual(bucket["chart_series"][0]["unit"], "inHg")
        self.assertAlmostEqual(bucket["chart_series"][0]["points"][0]["value"], 29.5, places=2)

    def test_actual_ha_barometer_threshold_uses_hpa_in_metric_preference(self):
        ha = {"matched": [{"entity_id": "sensor.barometric_pressure", "unit": "hPa"}]}
        rows = {
            APP.INFLUX_HISTORY_BUCKET: [],
            APP.INFLUX_HOME_ASSISTANT_BUCKET: [
                {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.barometric_pressure", "_value": "1000"},
                {"_time": "2026-01-01T00:01:00Z", "_measurement": "sensor.barometric_pressure", "_value": "1050"},
            ],
        }
        with mock.patch.dict(os.environ, {"BOAT_UNITS": "metric"}, clear=False):
            history = self._history(rows, ha, "barometer below 1016 hPa")
        bucket = history["buckets"][APP.INFLUX_HOME_ASSISTANT_BUCKET]
        result = bucket["analysis"]["threshold_analysis"][0]
        self.assertEqual(result["matching_samples"], 1)
        self.assertEqual(result["threshold_unit"], "hPa")
        self.assertEqual(bucket["units"]["sensor.barometric_pressure"], "hPa")

    def test_actual_mixed_barometric_history_keeps_both_pressure_measurements(self):
        ha = {"matched": [
            {"entity_id": "sensor.barometric_pressure", "unit": "hPa"},
            {"entity_id": "sensor.outside_pressure", "unit": "hPa"},
        ]}
        rows = {APP.INFLUX_HISTORY_BUCKET: [], APP.INFLUX_HOME_ASSISTANT_BUCKET: [
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.barometric_pressure", "_value": "1000"},
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.outside_pressure", "_value": "1000"},
            {"_time": "2026-01-01T00:01:00Z", "_measurement": "sensor.barometric_pressure", "_value": "1050"},
            {"_time": "2026-01-01T00:01:00Z", "_measurement": "sensor.outside_pressure", "_value": "1050"},
        ]}
        for preference, threshold in (("metric", "1016"), ("us_customary", "30")):
            with mock.patch.dict(os.environ, {"BOAT_UNITS": preference}, clear=False):
                history = self._history(rows, ha, f"barometric pressure below {threshold}")
            results = history["buckets"][APP.INFLUX_HOME_ASSISTANT_BUCKET]["analysis"]["threshold_analysis"]
            self.assertEqual(
                {item["measurement"]: (item["matching_samples"], item["threshold_unit"]) for item in results},
                {
                    "sensor.barometric_pressure": (1, "hPa" if preference == "metric" else "inHg"),
                    "sensor.outside_pressure": (1, "hPa" if preference == "metric" else "inHg"),
                },
            )

    def test_actual_history_explicit_pressure_unit_overrides_both_preferences_and_engine_defaults_remain(self):
        ha = {"matched": [{"entity_id": "sensor.barometric_pressure", "unit": "hPa"}]}
        rows = {
            APP.INFLUX_HISTORY_BUCKET: [],
            APP.INFLUX_HOME_ASSISTANT_BUCKET: [
                {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.barometric_pressure", "_value": "1000"},
                {"_time": "2026-01-01T00:01:00Z", "_measurement": "sensor.barometric_pressure", "_value": "1050"},
            ],
        }
        for preference in ("metric", "us_customary"):
            with mock.patch.dict(os.environ, {"BOAT_UNITS": preference}, clear=False):
                history = self._history(rows, ha, "barometer below 14psi")
            result = history["buckets"][APP.INFLUX_HOME_ASSISTANT_BUCKET]["analysis"]["threshold_analysis"][0]
            self.assertEqual(result["threshold_unit"], "psi")
        with mock.patch.dict(os.environ, {"BOAT_UNITS": "metric"}, clear=False):
            self.assertEqual(APP._threshold_unit_for_measurement("propulsion.port.oilPressure", None, "oil pressure below 100", "kPa"), ("kPa", "kPa"))
        with mock.patch.dict(os.environ, {"BOAT_UNITS": "us_customary"}, clear=False):
            self.assertEqual(APP._threshold_unit_for_measurement("propulsion.port.oilPressure", None, "oil pressure below 14", "kPa"), ("kPa", "psi"))

    def test_percent_threshold_does_not_match_voltage_in_mixed_soc_history(self):
        rows = [
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.soc", "_value": "40"},
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.voltage", "_value": "12"},
            {"_time": "2026-01-01T00:01:00Z", "_measurement": "sensor.soc", "_value": "60"},
            {"_time": "2026-01-01T00:01:00Z", "_measurement": "sensor.voltage", "_value": "13"},
        ]
        for suffix in ("%", " %", "%,", "%).", " percent"):
            message = f"SOC and voltage below 50{suffix}"
            analysis = APP.analyze_history_rows(
                message,
                rows,
                ["sensor.soc", "sensor.voltage"],
                1,
                {"sensor.soc": "%", "sensor.voltage": "V"},
            )
            self.assertEqual(
                analysis["threshold_analysis"],
                [{
                    "measurement": "sensor.soc",
                    "direction": "below",
                    "threshold": 50.0,
                    "threshold_unit": "%",
                    "matching_samples": 1,
                    "total_samples": 2,
                    "approximate_matching_hours": 0.02,
                }],
            )

    def test_actual_ha_percent_threshold_route_preserves_soc_only_in_both_preferences(self):
        ha = {"matched": [{"entity_id": "sensor.soc", "unit": "%"}, {"entity_id": "sensor.voltage", "unit": "V"}]}
        rows = {APP.INFLUX_HISTORY_BUCKET: [], APP.INFLUX_HOME_ASSISTANT_BUCKET: [
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.soc", "_value": "40"},
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.voltage", "_value": "12"},
            {"_time": "2026-01-01T00:01:00Z", "_measurement": "sensor.soc", "_value": "60"},
            {"_time": "2026-01-01T00:01:00Z", "_measurement": "sensor.voltage", "_value": "13"},
        ]}
        for preference in ("metric", "us_customary"):
            with mock.patch.dict(os.environ, {"BOAT_UNITS": preference}, clear=False):
                history = self._history(rows, ha, "SOC and voltage below 50%,")
            bucket = history["buckets"][APP.INFLUX_HOME_ASSISTANT_BUCKET]
            self.assertEqual([(item["measurement"], item["matching_samples"], item["threshold_unit"]) for item in bucket["analysis"]["threshold_analysis"]], [("sensor.soc", 1, "%")])
            chart_units = {series["label"]: series["unit"] for series in bucket["chart_series"]}
            self.assertEqual(chart_units, {"voltage": "V", "soc": "%"})

    def test_history_thresholds_use_explicit_ha_temperature_units(self):
        ha = {"matched": [{"entity_id": "sensor.air_temperature", "unit": "°C"}]}
        rows = {APP.INFLUX_HISTORY_BUCKET: [], APP.INFLUX_HOME_ASSISTANT_BUCKET: [
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.air_temperature", "_value": "20"},
            {"_time": "2026-01-01T00:01:00Z", "_measurement": "sensor.air_temperature", "_value": "30"},
        ]}
        history = self._history(rows, ha, "temperature history above 25C")
        analysis = history["buckets"][APP.INFLUX_HOME_ASSISTANT_BUCKET]["analysis"]
        self.assertEqual(analysis["threshold_analysis"][0]["matching_samples"], 1)
        self.assertEqual(analysis["threshold_analysis"][0]["threshold_unit"], "°C")

    def test_history_implicit_ha_temperature_threshold_uses_preference(self):
        ha = {"matched": [{"entity_id": "sensor.air_temperature", "unit": "°C"}]}
        rows = {APP.INFLUX_HISTORY_BUCKET: [], APP.INFLUX_HOME_ASSISTANT_BUCKET: [
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.air_temperature", "_value": "20"},
            {"_time": "2026-01-01T00:01:00Z", "_measurement": "sensor.air_temperature", "_value": "30"},
        ]}
        for preference, query in (("metric", "temperature above 25"), ("us_customary", "temperature above 77")):
            with mock.patch.dict(os.environ, {"BOAT_UNITS": preference}, clear=False):
                history = self._history(rows, ha, query)
            bucket = history["buckets"][APP.INFLUX_HOME_ASSISTANT_BUCKET]
            self.assertEqual(bucket["units"]["sensor.air_temperature"], "°C")
            self.assertEqual(bucket["numeric_summary"]["sensor.air_temperature"]["avg"], 25.0)
            self.assertEqual(bucket["analysis"]["threshold_analysis"][0]["matching_samples"], 1)

    def test_history_thresholds_convert_fahrenheit_and_pressure_units(self):
        ha = {"matched": [
            {"entity_id": "sensor.temp_f", "unit": "°F"},
            {"entity_id": "sensor.pressure", "unit": "kPa"},
        ]}
        rows = {APP.INFLUX_HISTORY_BUCKET: [], APP.INFLUX_HOME_ASSISTANT_BUCKET: [
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.temp_f", "_value": "68"},
            {"_time": "2026-01-01T00:01:00Z", "_measurement": "sensor.temp_f", "_value": "86"},
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.pressure", "_value": "100"},
            {"_time": "2026-01-01T00:01:00Z", "_measurement": "sensor.pressure", "_value": "10"},
        ]}
        history = self._history(rows, ha, "temperature above 25C")
        results = history["buckets"][APP.INFLUX_HOME_ASSISTANT_BUCKET]["analysis"]["threshold_analysis"]
        self.assertEqual({item["measurement"]: item["matching_samples"] for item in results}, {"sensor.temp_f": 1})
        history = self._history(rows, ha, "pressure above 14psi")
        results = history["buckets"][APP.INFLUX_HOME_ASSISTANT_BUCKET]["analysis"]["threshold_analysis"]
        self.assertEqual({item["measurement"]: item["matching_samples"] for item in results}, {"sensor.pressure": 1})

    def test_history_thresholds_skip_unknown_or_incompatible_source_units(self):
        ha = {"matched": [
            {"entity_id": "sensor.opaque", "unit": "mystery"},
            {"entity_id": "sensor.pressure", "unit": "kPa"},
        ]}
        rows = {APP.INFLUX_HISTORY_BUCKET: [], APP.INFLUX_HOME_ASSISTANT_BUCKET: [
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.opaque", "_value": "1"},
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.pressure", "_value": "100"},
        ]}
        analysis = APP.analyze_history_rows(
            "temperature above 25C",
            rows[APP.INFLUX_HOME_ASSISTANT_BUCKET],
            ["sensor.opaque", "sensor.pressure"],
            1,
            {"sensor.opaque": "mystery", "sensor.pressure": "kPa"},
        )
        self.assertEqual(analysis["threshold_analysis"], [])

    def test_history_thresholds_convert_pa_to_inhg(self):
        rows = [
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.pressure", "_value": "101325"},
            {"_time": "2026-01-01T00:01:00Z", "_measurement": "sensor.pressure", "_value": "90000"},
        ]
        analysis = APP.analyze_history_rows("pressure above 29inHg", rows, ["sensor.pressure"], 1, {"sensor.pressure": "Pa"})
        self.assertEqual(analysis["threshold_analysis"][0]["matching_samples"], 1)

    def test_history_thresholds_accept_pa_explicit_spaced_and_attached(self):
        ha = {"matched": [{"entity_id": "sensor.pressure", "unit": "Pa"}]}
        rows = {APP.INFLUX_HISTORY_BUCKET: [], APP.INFLUX_HOME_ASSISTANT_BUCKET: [
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.pressure", "_value": "100000"},
            {"_time": "2026-01-01T00:01:00Z", "_measurement": "sensor.pressure", "_value": "90000"},
        ]}
        for preference, message in (("metric", "pressure above 95000 Pa"), ("us_customary", "pressure above 95000Pa")):
            with mock.patch.dict(os.environ, {"BOAT_UNITS": preference}, clear=False):
                history = self._history(rows, ha, message)
            analysis = history["buckets"][APP.INFLUX_HOME_ASSISTANT_BUCKET]["analysis"]
            self.assertEqual(analysis["threshold_analysis"][0]["matching_samples"], 1)
            self.assertEqual(analysis["threshold_analysis"][0]["threshold_unit"], "Pa")

    def test_supplied_missing_units_do_not_guess_but_none_keeps_signalk_fallback(self):
        rows = [
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.temp", "_value": "68"},
            {"_time": "2026-01-01T00:01:00Z", "_measurement": "sensor.temp", "_value": "86"},
        ]
        self.assertEqual(APP.analyze_history_rows("temperature above 25C", rows, ["sensor.temp"], 1, {})["threshold_analysis"], [])
        self.assertEqual(APP.analyze_history_rows("temperature above 25C", rows, ["sensor.temp"], 1, {"other": "°F"})["threshold_analysis"], [])
        signalk = APP.analyze_history_rows("temperature above 25C", rows, ["propulsion.port.temperature"], 1)
        self.assertEqual(signalk["threshold_analysis"], [])
        signalk_rows = [dict(row, _measurement="propulsion.port.temperature") for row in rows]
        signalk = APP.analyze_history_rows("temperature above 25C", signalk_rows, ["propulsion.port.temperature"], 1)
        self.assertEqual(signalk["threshold_analysis"][0]["matching_samples"], 1)

    def test_source_gph_alias_converts_l_per_hour_threshold(self):
        rows = [
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.fuel", "_value": "0.1"},
            {"_time": "2026-01-01T00:01:00Z", "_measurement": "sensor.fuel", "_value": "1"},
        ]
        analysis = APP.analyze_history_rows("fuel above 2L/h", rows, ["sensor.fuel"], 1, {"sensor.fuel": "gph"})
        self.assertEqual(analysis["threshold_analysis"][0]["matching_samples"], 1)

    def test_history_thresholds_keep_same_unit_canonical_signals(self):
        rows = [
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "electrical.current", "_value": "4"},
            {"_time": "2026-01-01T00:01:00Z", "_measurement": "electrical.current", "_value": "6"},
        ]
        analysis = APP.analyze_history_rows("current above 5", rows, ["electrical.current"], 1)
        self.assertEqual(analysis["threshold_analysis"][0]["matching_samples"], 1)
        unknown = APP.analyze_history_rows("current above 5", rows, ["electrical.current"], 1, {"electrical.current": "mystery"})
        self.assertEqual(unknown["threshold_analysis"], [])

    def test_chart_points_have_display_labels(self):
        with mock.patch.dict(os.environ, {"BOAT_UNITS": "metric"}, clear=False):
            points = [{"_time": "2026-01-01T00:00:00Z", "_measurement": "propulsion.port.temperature", "_value": "212"}]
            series = []
            for row in points:
                value = APP.display_units.display_value(row["_measurement"], float(row["_value"]), "metric")
                series.append({"value": value, "label": f"{value:g} {APP.unit_for_measurement(row['_measurement'], {})}"})
        self.assertEqual(series[0], {"value": 100.0, "label": "100 °C"})

    def test_actual_history_keeps_ha_source_units_across_chart_cards_and_focus(self):
        ha = {"matched": [{"entity_id": "sensor.air_temperature", "unit": "°C"}]}
        rows = {APP.INFLUX_HISTORY_BUCKET: [], APP.INFLUX_HOME_ASSISTANT_BUCKET: [
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.air_temperature", "_value": "20"},
            {"_time": "2026-01-01T00:01:00Z", "_measurement": "sensor.air_temperature", "_value": "21"},
        ]}
        with mock.patch.dict(os.environ, {"BOAT_UNITS": "metric"}, clear=False):
            history = self._history(rows, ha, "temperature history maximum")
            bucket = history["buckets"][APP.INFLUX_HOME_ASSISTANT_BUCKET]
            self.assertEqual(bucket["units"]["sensor.air_temperature"], "°C")
            self.assertEqual(bucket["chart_series"][0]["points"][0]["value"], 20.0)
            context = {"question_type": {"generic_history": True}, "influx_history": history, "match_tokens": ["temperature"]}
            experience = APP.answer_experience(context)
            self.assertEqual(experience["metrics"][0]["avg"], 20.5)
            focus = APP.complex_answer_focus("what was the maximum temperature", history)
            self.assertEqual(focus["extrema"][0]["value"], "21")

    def test_actual_history_direct_answer_preserves_ha_celsius_in_us_preference(self):
        ha = {"matched": [{"entity_id": "sensor.air_temperature", "unit": "°C"}]}
        rows = {APP.INFLUX_HISTORY_BUCKET: [], APP.INFLUX_HOME_ASSISTANT_BUCKET: [{"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.air_temperature", "_value": "20"}]}
        with mock.patch.dict(os.environ, {"BOAT_UNITS": "us_customary"}, clear=False):
            history = self._history(rows, ha)
            answer = APP.answer_from_generic_history({"question_type": {"generic_history": True}, "influx_history": history, "match_tokens": ["temperature"]})
        self.assertIn("20 °C", answer)

    def test_ha_fahrenheit_and_unknown_units_are_not_guessed(self):
        ha = {"matched": [
            {"entity_id": "sensor.temp_f", "unit": "°F"},
            {"entity_id": "sensor.opaque", "unit": "mystery"},
            {"entity_id": "sensor.no_unit", "unit": ""},
        ]}
        rows = {APP.INFLUX_HISTORY_BUCKET: [], APP.INFLUX_HOME_ASSISTANT_BUCKET: [
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.temp_f", "_value": "68"},
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.opaque", "_value": "7"},
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.no_unit", "_value": "9"},
        ]}
        with mock.patch.dict(os.environ, {"BOAT_UNITS": "metric"}, clear=False):
            history = self._history(rows, ha, "temperature history")
            summary = history["buckets"][APP.INFLUX_HOME_ASSISTANT_BUCKET]["numeric_summary"]
            self.assertEqual(summary["sensor.temp_f"]["avg"], 68.0)
            self.assertEqual(summary["sensor.opaque"]["avg"], 7.0)
            compact = APP.compact_context({"influx_history": history, "ha_telemetry": ha})
            self.assertEqual(compact["influx_history"]["buckets"][APP.INFLUX_HOME_ASSISTANT_BUCKET]["numeric_summary"]["sensor.temp_f"]["avg"], 20.0)
            self.assertEqual(compact["influx_history"]["buckets"][APP.INFLUX_HOME_ASSISTANT_BUCKET]["numeric_summary"]["sensor.opaque"]["avg"], 7.0)

    def test_ha_pressure_and_soc_use_source_units_not_signalk_heuristics(self):
        ha = {"matched": [{"entity_id": "sensor.pressure", "unit": "kPa"}, {"entity_id": "sensor.soc", "unit": "%"}]}
        rows = {APP.INFLUX_HISTORY_BUCKET: [], APP.INFLUX_HOME_ASSISTANT_BUCKET: [
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.pressure", "_value": "100"},
            {"_time": "2026-01-01T00:00:00Z", "_measurement": "sensor.soc", "_value": "80"},
        ]}
        with mock.patch.dict(os.environ, {"BOAT_UNITS": "metric"}, clear=False):
            history = self._history(rows, ha, "pressure and battery history")
            summary = history["buckets"][APP.INFLUX_HOME_ASSISTANT_BUCKET]["numeric_summary"]
            self.assertEqual(summary["sensor.pressure"]["avg"], 100.0)
            self.assertEqual(summary["sensor.soc"]["avg"], 80.0)

    def test_signalk_canonical_history_converts_once_and_source_stays_immutable(self):
        rows = {APP.INFLUX_HISTORY_BUCKET: [{"_time": "2026-01-01T00:00:00Z", "_measurement": "propulsion.port.temperature", "_value": "373.15"}], APP.INFLUX_HOME_ASSISTANT_BUCKET: []}
        with mock.patch.dict(os.environ, {"BOAT_UNITS": "metric"}, clear=False):
            history = self._history(rows, message="temperature history")
            self.assertAlmostEqual(history["buckets"][APP.INFLUX_HISTORY_BUCKET]["numeric_summary"]["propulsion.port.temperature"]["avg"], 212.0, places=3)
            shown = APP.compact_context({"influx_history": history})
            self.assertAlmostEqual(shown["influx_history"]["buckets"][APP.INFLUX_HISTORY_BUCKET]["numeric_summary"]["propulsion.port.temperature"]["avg"], 100.0, places=3)
            self.assertAlmostEqual(history["buckets"][APP.INFLUX_HISTORY_BUCKET]["numeric_summary"]["propulsion.port.temperature"]["avg"], 212.0, places=3)

    def test_fuel_balance_metric_changes_rates_and_volume_but_not_signed_rpm_or_percent(self):
        context = {"question_type": {"fuel_balance": True}, "fuel_balance_24h": {"running_minutes": 60, "confidence": "high", "averages": {"port_fuel_gph": 10.0, "starboard_fuel_gph": 12.0, "port_rpm": 2000, "starboard_rpm": 1980}, "derived": {"starboard_minus_port_fuel_gph": 2.0, "starboard_fuel_percent_vs_port": 20.0, "starboard_minus_port_rpm": -20.0, "starboard_minus_port_total_gallons": 2.0}}, "fuel_balance_7d": {"running_minutes": 0}}
        with mock.patch.dict(os.environ, {"BOAT_UNITS": "metric"}, clear=False):
            metric = APP.answer_from_fuel_balance(context)
        with mock.patch.dict(os.environ, {"BOAT_UNITS": "us_customary"}, clear=False):
            customary = APP.answer_from_fuel_balance(context)
        self.assertIn("L/h", metric); self.assertIn("7.571", metric); self.assertIn("-20.0", metric); self.assertIn("20.0%", metric); self.assertIn("L.", metric)
        self.assertIn("gal/h", customary); self.assertIn("2.000", customary); self.assertIn("-20.0", customary); self.assertIn("20.0%", customary); self.assertIn("gal.", customary)


if __name__ == "__main__":
    unittest.main()
