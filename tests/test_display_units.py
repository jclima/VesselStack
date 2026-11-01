import copy
import importlib.util
import pathlib
import unittest


def load_units():
    path = pathlib.Path(__file__).parents[1] / "boat-chat" / "display_units.py"
    spec = importlib.util.spec_from_file_location("display_units_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class DisplayUnitsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.units = load_units()

    def test_known_conversions(self):
        u = self.units
        self.assertAlmostEqual(u.convert(32, "F", "C"), 0.0, places=6)
        self.assertAlmostEqual(u.convert(1, "psi", "kPa"), 6.894757, places=5)
        self.assertAlmostEqual(u.convert(3.28084, "ft", "m"), 1.0, places=4)
        self.assertAlmostEqual(u.convert(1, "US gal", "L"), 3.785411, places=5)
        self.assertAlmostEqual(u.convert_delta(18, "F", "C"), 10.0, places=6)

    def test_metric_context_is_deep_copy_and_renames_fuel_fields(self):
        u = self.units
        source = {"totals": {"fuel_gallons": 1.0}, "economy": {"nm_per_gallon": 2.0}}
        before = copy.deepcopy(source)
        normalized = u.normalize_context(source, "metric")
        self.assertEqual(source, before)
        self.assertAlmostEqual(normalized["totals"]["fuel_liters"], 3.785411, places=5)
        self.assertAlmostEqual(normalized["economy"]["nm_per_liter"], 0.528344, places=5)
        self.assertEqual(normalized["display_units"], "metric")

    def test_us_preference_preserves_payload(self):
        u = self.units
        source = {"totals": {"fuel_gallons": 1.0}}
        normalized = u.normalize_context(source, "us_customary")
        self.assertEqual(normalized["totals"]["fuel_gallons"], 1.0)
        self.assertEqual(normalized["display_units"], "us_customary")

    def test_metric_preserves_counts_and_temperature_deltas(self):
        normalized = self.units.normalize_context({"propulsion.port.temperature": {"min": 212.0, "count": 1, "delta": 18.0}}, "metric")
        stats = normalized["propulsion.port.temperature"]
        self.assertEqual(stats["count"], 1)
        self.assertAlmostEqual(stats["min"], 100.0)
        self.assertAlmostEqual(stats["delta"], 10.0)

    def test_reverse_pressure_and_canonical_labels(self):
        self.assertAlmostEqual(self.units.convert(1000, "hPa", "inHg"), 29.52998, places=3)

    def test_metric_presentation_converts_known_dock_fields_and_notes_only_in_copy(self):
        source = {"latest_observation": {"distance_from_dock_ft": 10.0, "dock_radius_ft": 20.0}, "notes": ["Fuel rate is integrated by minute and reported in US gallons (gal, gal/h)."]}
        normalized = self.units.normalize_context(source, "metric")
        self.assertEqual(source["latest_observation"]["distance_from_dock_ft"], 10.0)
        self.assertAlmostEqual(normalized["latest_observation"]["distance_from_dock_m"], 3.048)
        self.assertNotIn("US gallons", normalized["notes"][0])

    def test_source_dimensions_override_abbreviated_measurement_names(self):
        u = self.units
        self.assertAlmostEqual(u.display_value("sensor.coolant_temp", 68, "metric", "°F"), 20.0)
        self.assertAlmostEqual(u.display_value("propulsion.port.fuel.rate", 1, "metric", "gal/h"), 3.785411, places=5)
        self.assertEqual(u.unit_for_measurement("propulsion.port.fuel.rate", "metric", "gal/h"), "L/h")
        self.assertAlmostEqual(u.display_value("sensor.pressure", 1000, "metric", "hPa"), 100.0)

    def test_unknown_and_missing_source_units_are_preserved(self):
        u = self.units
        self.assertEqual(u.display_value("sensor.coolant_temp", 68, "metric", "mystery"), 68)
        self.assertEqual(u.unit_for_measurement("sensor.coolant_temp", "metric", "mystery"), "mystery")
        self.assertEqual(u.display_value("sensor.coolant_temp", 68, "metric", ""), 68)
        self.assertEqual(u.unit_for_measurement("sensor.coolant_temp", "metric", ""), "")

    def test_non_temperature_deltas_are_not_scaled(self):
        normalized = self.units.normalize_context({"propulsion.port.revolutions": {"delta": 18.0}, "propulsion.port.temperature": {"delta": 18.0}}, "metric")
        self.assertEqual(normalized["propulsion.port.revolutions"]["delta"], 18.0)
        self.assertAlmostEqual(normalized["propulsion.port.temperature"]["delta"], 10.0)

    def test_history_bucket_units_are_isolated_and_aligned_snapshots_follow_them(self):
        source = {"influx_history": {"buckets": {
            "a": {"units": {"sensor.cabin_temperature": "F"}, "numeric_summary": {"sensor.cabin_temperature": {"min": 68}}, "analysis": {"notable_snapshots": [{"values": {"sensor.cabin_temperature": 68}}]}},
            "b": {"units": {"sensor.cabin_temperature": "°C"}, "numeric_summary": {"sensor.cabin_temperature": {"min": 20}}, "analysis": {"notable_snapshots": [{"values": {"sensor.cabin_temperature": 20}}]}},
        }}}
        normalized = self.units.normalize_context(source, "metric")
        self.assertAlmostEqual(normalized["influx_history"]["buckets"]["a"]["numeric_summary"]["sensor.cabin_temperature"]["min"], 20.0)
        self.assertAlmostEqual(normalized["influx_history"]["buckets"]["b"]["numeric_summary"]["sensor.cabin_temperature"]["min"], 20.0)
        self.assertAlmostEqual(normalized["influx_history"]["buckets"]["a"]["analysis"]["notable_snapshots"][0]["values"]["sensor.cabin_temperature"], 20.0)

    def test_abbreviated_fuel_rate_units_match_propulsion_values(self):
        source = {"propulsion_24h": {"units": {"fuel.rate": "gal/h"}, "summary": {"propulsion.port.fuel.rate": {"avg": 1.0}}}}
        normalized = self.units.normalize_context(source, "metric")
        self.assertEqual(normalized["propulsion_24h"]["units"]["fuel.rate"], "L/h")
        self.assertAlmostEqual(normalized["propulsion_24h"]["summary"]["propulsion.port.fuel.rate"]["avg"], 3.785411, places=5)
        self.assertEqual(source["propulsion_24h"]["summary"]["propulsion.port.fuel.rate"]["avg"], 1.0)


if __name__ == "__main__":
    unittest.main()
