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


if __name__ == "__main__":
    unittest.main()
