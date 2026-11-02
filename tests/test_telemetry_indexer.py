import importlib.util
import contextlib
import io
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

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

    def test_history_quiet_cli_hides_success_payload_and_uses_temp_lock(self):
        sensitive = "private telemetry payload"
        payload = {"ok": True, "items": [{"ok": True, "summary": {"secret": sensitive}}]}
        with tempfile.TemporaryDirectory() as state_dir, \
                mock.patch.object(self.indexer, "LOCK_DIR", pathlib.Path(state_dir)), \
                mock.patch.object(self.indexer, "refresh_all", return_value=payload), \
                mock.patch.object(sys, "argv", ["telemetry_indexer.py", "history", "--quiet"]), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.indexer.main()
        output = stdout.getvalue()
        self.assertEqual(output, '{"ok":true,"count":1,"failed":0}\n')
        self.assertNotIn(sensitive, output)

    def test_history_quiet_cli_hides_failure_payload_and_reports_error_class(self):
        sensitive = "private failure payload"
        payload = {"ok": False, "items": [{"ok": False, "error": sensitive, "error_class": "TimeoutError"}]}
        with tempfile.TemporaryDirectory() as state_dir, \
                mock.patch.object(self.indexer, "LOCK_DIR", pathlib.Path(state_dir)), \
                mock.patch.object(self.indexer, "refresh_all", return_value=payload), \
                mock.patch.object(sys, "argv", ["telemetry_indexer.py", "history", "--quiet"]), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            with self.assertRaises(SystemExit) as raised:
                self.indexer.main()
        output = stdout.getvalue()
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(output, '{"ok":false,"count":1,"failed":1,"error_class":"refresh_failed"}\n')
        self.assertNotIn(sensitive, output)

    def test_history_service_execstart_requests_quiet_output(self):
        service = (ROOT / "systemd/vesselstack-telemetry-indexer.service.in").read_text()
        self.assertIn("telemetry_indexer.py run --quiet", service)


if __name__ == "__main__":
    unittest.main()
