#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vesselstack_control_panel", ROOT / "control-panel/app.py")
assert SPEC and SPEC.loader
panel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(panel)


class EnvironmentFileTests(unittest.TestCase):
    def test_round_trip_supports_spaces_and_empty_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.env"
            panel.write_env(path, {"BOAT_NAME": "Test Boat", "EMPTY": "", "ENABLED": "true"}, ["BOAT_NAME", "EMPTY"])
            self.assertEqual(
                panel.parse_env(path),
                {"BOAT_NAME": "Test Boat", "EMPTY": "", "ENABLED": "true"},
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_public_configuration_redacts_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vessel = root / "vessel.env"
            chat = root / "chat.env"
            control = root / "control.env"
            panel.write_env(vessel, {"BOAT_NAME": "Test Boat", "INFLUXDB_TOKEN": "secret"}, [])
            panel.write_env(chat, {"OPENAI_API_KEY": "also-secret"}, [])
            panel.write_env(control, {"CONTROL_PANEL_HOST": "127.0.0.1"}, [])
            with mock.patch.multiple(panel, VESSEL_ENV=vessel, CHAT_ENV=chat, PANEL_ENV=control):
                result = panel.public_configuration()["values"]
            self.assertEqual(result["BOAT_NAME"], "Test Boat")
            self.assertEqual(result["INFLUXDB_TOKEN"], {"configured": True})
            self.assertEqual(result["OPENAI_API_KEY"], {"configured": True})
            self.assertNotIn("secret", str(result))

    def test_blank_secret_update_preserves_existing_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vessel = root / "vessel.env"
            chat = root / "chat.env"
            control = root / "control.env"
            panel.write_env(vessel, {"BOAT_NAME": "Old", "INFLUXDB_TOKEN": "keep-me"}, [])
            panel.write_env(chat, {}, [])
            panel.write_env(control, {"CONTROL_PANEL_TOKEN": "panel-token"}, [])
            with mock.patch.multiple(panel, VESSEL_ENV=vessel, CHAT_ENV=chat, PANEL_ENV=control, CONFIG_BACKUP_ROOT=root / "backups"):
                backup = panel.update_configuration({"BOAT_NAME": "New Boat", "INFLUXDB_TOKEN": ""})
            values = panel.parse_env(vessel)
            self.assertEqual(values["BOAT_NAME"], "New Boat")
            self.assertEqual(values["INFLUXDB_TOKEN"], "keep-me")
            self.assertEqual((Path(backup) / "vessel.env").stat().st_mode & 0o777, 0o600)

    def test_null_secret_update_clears_existing_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vessel = root / "vessel.env"
            chat = root / "chat.env"
            control = root / "control.env"
            panel.write_env(vessel, {"INFLUXDB_TOKEN": "remove-me"}, [])
            panel.write_env(chat, {}, [])
            panel.write_env(control, {"CONTROL_PANEL_TOKEN": "panel-token"}, [])
            with mock.patch.multiple(panel, VESSEL_ENV=vessel, CHAT_ENV=chat, PANEL_ENV=control, CONFIG_BACKUP_ROOT=root / "backups"):
                panel.update_configuration({"INFLUXDB_TOKEN": None})
            self.assertEqual(panel.parse_env(vessel)["INFLUXDB_TOKEN"], "")

    def test_partial_write_failure_restores_all_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vessel = root / "vessel.env"
            chat = root / "chat.env"
            control = root / "control.env"
            panel.write_env(vessel, {"BOAT_NAME": "Original"}, [])
            panel.write_env(chat, {"BOAT_CHAT_MODEL": "original-model"}, [])
            panel.write_env(control, {"CONTROL_PANEL_TOKEN": "panel-token"}, [])
            original_write = panel.write_env
            calls = 0

            def fail_second_write(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated write failure")
                return original_write(*args, **kwargs)

            with mock.patch.multiple(panel, VESSEL_ENV=vessel, CHAT_ENV=chat, PANEL_ENV=control, CONFIG_BACKUP_ROOT=root / "backups"), mock.patch.object(panel, "write_env", side_effect=fail_second_write):
                with self.assertRaisesRegex(OSError, "simulated"):
                    panel.update_configuration({"BOAT_NAME": "Changed", "BOAT_CHAT_MODEL": "changed-model"})
            self.assertEqual(panel.parse_env(vessel)["BOAT_NAME"], "Original")
            self.assertEqual(panel.parse_env(chat)["BOAT_CHAT_MODEL"], "original-model")


class CommandAllowlistTests(unittest.TestCase):
    def test_component_commands_are_argument_arrays(self) -> None:
        command = panel.component_command("grafana", "start")
        self.assertEqual(command[-3:], ["up", "-d", "grafana"])
        self.assertNotIn("sh", command[:1])

    def test_unknown_component_and_action_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            panel.component_command("arbitrary-service", "start")
        with self.assertRaises(ValueError):
            panel.component_command("grafana", "delete")

    def test_update_requires_reviewed_release_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "vesselstack-test"
            release.mkdir()
            with self.assertRaises(ValueError):
                panel.action_command("update", {"source_directory": release})
            (release / "install.sh").write_text("#!/bin/bash\n")
            (release / "VERSION").write_text("1.0.1\n")
            with mock.patch.object(panel, "REVIEWED_RELEASE_ROOT", root):
                command = panel.action_command("update", {"source_directory": release})
            self.assertEqual(command[-2:], ["update", str(release)])
            (release / "install.sh").chmod(0o777)
            with mock.patch.object(panel, "REVIEWED_RELEASE_ROOT", root), self.assertRaises(ValueError):
                panel.action_command("update", {"source_directory": release})


class ValidationTests(unittest.TestCase):
    def test_paths_must_be_absolute(self) -> None:
        spec = panel.FIELD_BY_KEY["VESSELSTACK_DATA"]
        with self.assertRaises(ValueError):
            panel.validate_value(spec, "relative/path")

    def test_urls_must_be_http_or_https(self) -> None:
        spec = panel.FIELD_BY_KEY["SIGNALK_URL"]
        with self.assertRaises(ValueError):
            panel.validate_value(spec, "file:///etc/passwd")

    def test_listener_port_and_host_are_bounded(self) -> None:
        with self.assertRaises(ValueError):
            panel.validate_value(panel.FIELD_BY_KEY["CONTROL_PANEL_PORT"], "70000")
        with self.assertRaises(ValueError):
            panel.validate_value(panel.FIELD_BY_KEY["CONTROL_PANEL_HOST"], "public.example")
        self.assertEqual(panel.validate_value(panel.FIELD_BY_KEY["CONTROL_PANEL_HOST"], "127.0.0.1"), "127.0.0.1")
        with self.assertRaises(ValueError):
            panel.validate_value(panel.FIELD_BY_KEY["INFLUXDB_PORT"], "0")
        self.assertEqual(panel.validate_value(panel.FIELD_BY_KEY["INFLUXDB_PORT"], "8086"), "8086")


class HttpApiTests(unittest.TestCase):
    def test_handler_authentication_requires_exact_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = root / "control.env"
            panel.write_env(control, {"CONTROL_PANEL_TOKEN": "test-panel-token"}, [])
            handler = object.__new__(panel.ControlPanelHandler)
            with mock.patch.object(panel, "PANEL_ENV", control), mock.patch.dict(panel.os.environ, {}, clear=True):
                handler.headers = {"X-VesselStack-Token": "wrong"}
                self.assertFalse(handler.authenticated())
                handler.headers = {"X-VesselStack-Token": "test-panel-token"}
                self.assertTrue(handler.authenticated())


if __name__ == "__main__":
    unittest.main()
