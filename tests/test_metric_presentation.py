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
