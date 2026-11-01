import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_indexer():
    sys.path.insert(0, str(ROOT / "boat-chat"))
    spec = importlib.util.spec_from_file_location("telemetry_indexer_test", ROOT / "boat-chat/telemetry_indexer.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class TelemetryIndexerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.indexer = load_indexer()

    def test_distance_units_are_explicit_and_unknown_is_not_guessed(self):
        u = self.indexer
        self.assertAlmostEqual(u.distance_ft_from_ha({"state": "1", "attributes": {"unit_of_measurement": "m"}}), 3.280839895)
        self.assertAlmostEqual(u.distance_ft_from_ha({"state": "1", "attributes": {"unit_of_measurement": "km"}}), 3280.839895)
        self.assertIsNone(u.distance_ft_from_ha({"state": "1", "attributes": {"unit_of_measurement": "yd"}}))
        self.assertIsNone(u.distance_ft_from_ha({"state": "nan", "attributes": {"unit_of_measurement": "ft"}}))

    def test_quiet_result_contains_no_payload(self):
        self.assertEqual(self.indexer._safe_result(False, 0, 1, "RuntimeError"),
                         {"ok": False, "count": 0, "failed": 1, "error_class": "RuntimeError"})


if __name__ == "__main__":
    unittest.main()
