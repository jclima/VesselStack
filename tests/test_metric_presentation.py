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


if __name__ == "__main__":
    unittest.main()
