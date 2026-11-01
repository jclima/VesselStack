"""Focused regressions for settings and telemetry review fixes."""

import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "boat-chat"))
import app
import env_config
import telemetry_cache


class ReviewRegressionTests(unittest.TestCase):
    def test_explicit_clear_masks_inherited_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boat-chat.env"
            with mock.patch.object(app, "ENV_CONFIG", path), mock.patch.dict(os.environ, {"BOAT_CHAT_PORT": "8765"}, clear=False):
                app.update_settings({"BOAT_CHAT_PORT": None})
                self.assertEqual("", env_config.parse_env_file(path, app.SETTING_KEYS)["BOAT_CHAT_PORT"])

    def test_finite_optional_float_rejects_non_finite_values(self):
        self.assertIsNone(telemetry_cache._optional_float("nan"))
        self.assertIsNone(telemetry_cache._optional_float("inf"))
        self.assertTrue(math.isfinite(telemetry_cache._optional_float("1.5")))


if __name__ == "__main__":
    unittest.main()
