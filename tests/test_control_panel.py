#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import os
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
            panel.write_env(vessel, {"MQTT_PASSWORD": "remove-me"}, [])
            panel.write_env(chat, {}, [])
            panel.write_env(control, {"CONTROL_PANEL_TOKEN": "panel-token"}, [])
            with mock.patch.multiple(panel, VESSEL_ENV=vessel, CHAT_ENV=chat, PANEL_ENV=control, CONFIG_BACKUP_ROOT=root / "backups"):
                panel.update_configuration({"MQTT_PASSWORD": None})
            self.assertEqual(panel.parse_env(vessel)["MQTT_PASSWORD"], "")

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


class OverrideFileSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.override = self.root / "boat-chat.env"
        patcher = mock.patch.dict(os.environ, {"BOAT_CHAT_ENV": str(self.override)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_clears_only_stale_keys_and_preserves_mode(self) -> None:
        self.override.write_text("KEEP=1\nDROP=2\n")
        os.chmod(self.override, 0o640)
        panel.clear_chat_overrides({"DROP", "ABSENT"}, {})
        self.assertEqual(self.override.read_text(), "KEEP=1\n")
        self.assertEqual(self.override.stat().st_mode & 0o777, 0o640)
        self.assertEqual([p.name for p in self.root.iterdir()], ["boat-chat.env"])

    def test_symlinked_override_is_neither_read_nor_replaced(self) -> None:
        target = self.root / "victim"
        target.write_text("DROP=secret\n")
        self.override.symlink_to(target)
        panel.clear_chat_overrides({"DROP"}, {})
        self.assertTrue(self.override.is_symlink())
        self.assertEqual(target.read_text(), "DROP=secret\n")
        self.assertEqual(panel.chat_overrides({}), set())

    def test_replace_refuses_symlink_and_cleans_temp_on_failure(self) -> None:
        target = self.root / "victim"
        target.write_text("x")
        self.override.symlink_to(target)
        with self.assertRaises(OSError):
            panel.replace_in_service_dir(self.override, "y\n")
        self.assertEqual(target.read_text(), "x")
        self.override.unlink()
        self.override.write_text("a=1\n")
        with mock.patch.object(panel.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                panel.replace_in_service_dir(self.override, "b=2\n")
        self.assertEqual(self.override.read_text(), "a=1\n")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["boat-chat.env", "victim"])

    def test_predictable_tmp_symlink_is_not_followed(self) -> None:
        victim = self.root / "victim"
        victim.write_text("keep")
        (self.root / "boat-chat.env.tmp").symlink_to(victim)
        self.override.write_text("DROP=1\nKEEP=1\n")
        panel.clear_chat_overrides({"DROP"}, {})
        self.assertEqual(victim.read_text(), "keep")
        self.assertEqual(self.override.read_text(), "KEEP=1\n")


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

    def test_disabled_optional_workers_cannot_be_started(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vessel = Path(directory) / "vessel.env"
            panel.write_env(vessel, {"TELEGRAM_ENABLE": "false", "TELEMETRY_INDEXER_ENABLE": "false"}, [])
            with mock.patch.object(panel, "VESSEL_ENV", vessel):
                with self.assertRaisesRegex(ValueError, "disabled"):
                    panel.component_command("telegram", "start")
                with self.assertRaisesRegex(ValueError, "disabled"):
                    panel.component_command("telemetry-indexer", "restart")

    def test_enabled_optional_workers_use_fixed_units(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vessel = Path(directory) / "vessel.env"
            panel.write_env(vessel, {"TELEGRAM_ENABLE": "true", "TELEMETRY_INDEXER_ENABLE": "true"}, [])
            with mock.patch.object(panel, "VESSEL_ENV", vessel):
                self.assertEqual(panel.component_command("telegram", "start"), ["systemctl", "start", "vesselstack-chat-telegram.service"])
                self.assertEqual(panel.component_command("telemetry-indexer", "restart"), ["systemctl", "restart", "vesselstack-telemetry-indexer.timer"])

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
            self.assertEqual(command[-2:], ["update", str(release.resolve())])
            (release / "install.sh").chmod(0o777)
            with mock.patch.object(panel, "REVIEWED_RELEASE_ROOT", root), self.assertRaises(ValueError):
                panel.action_command("update", {"source_directory": release})

    def test_update_rejects_unsafe_entries_anywhere_in_release_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "vesselstack-test"
            for name in ("scripts", "boat-chat", "templates", "systemd"):
                (release / name).mkdir(parents=True)
            (release / "install.sh").write_text("#!/bin/bash\n")
            (release / "VERSION").write_text("1.0.1\n")
            (release / "systemd/unit.in").write_text("x\n")
            with mock.patch.object(panel, "REVIEWED_RELEASE_ROOT", root):
                panel.action_command("update", {"source_directory": release})
                (release / "systemd/unit.in").chmod(0o666)
                with self.assertRaises(ValueError):
                    panel.action_command("update", {"source_directory": release})
                (release / "systemd/unit.in").chmod(0o644)
                (release / "scripts").chmod(0o775)
                with self.assertRaises(ValueError):
                    panel.action_command("update", {"source_directory": release})
                (release / "scripts").chmod(0o755)
                (release / "templates/link").symlink_to("/etc/passwd")
                with self.assertRaises(ValueError):
                    panel.action_command("update", {"source_directory": release})
                (release / "templates/link").unlink()
                panel.action_command("update", {"source_directory": release})

    @unittest.skipIf(os.getuid() == 0, "root-owned files are always accepted")
    def test_update_rejects_foreign_owner_in_release_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "vesselstack-test"
            (release / "scripts").mkdir(parents=True)
            (release / "install.sh").write_text("#!/bin/bash\n")
            (release / "VERSION").write_text("1.0.1\n")
            with mock.patch.object(panel, "REVIEWED_RELEASE_ROOT", root), mock.patch.object(panel.os, "geteuid", return_value=os.getuid() + 12345):
                with self.assertRaises(ValueError):
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


class InitOnlySecretTests(unittest.TestCase):
    KEYS = ["INFLUXDB_PASSWORD", "INFLUXDB_TOKEN", "GRAFANA_ADMIN_PASSWORD"]

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.vessel, self.chat, self.control = root / "vessel.env", root / "chat.env", root / "control.env"
        panel.write_env(self.vessel, {key: "original" for key in self.KEYS}, [])
        panel.write_env(self.chat, {}, [])
        panel.write_env(self.control, {"CONTROL_PANEL_TOKEN": "t"}, [])
        patcher = mock.patch.multiple(panel, VESSEL_ENV=self.vessel, CHAT_ENV=self.chat, PANEL_ENV=self.control, CONFIG_BACKUP_ROOT=root / "backups")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.directory.cleanup)

    def test_init_only_secrets_are_read_only_with_a_clear_message(self) -> None:
        for key in self.KEYS:
            spec = panel.FIELD_BY_KEY[key]
            self.assertTrue(spec["read_only"])
            self.assertIn("Read-only", spec["description"])
            self.assertIn("first created", spec["description"])
        self.assertFalse(panel.FIELD_BY_KEY["MQTT_PASSWORD"]["read_only"])

    def test_changing_or_clearing_them_is_rejected_and_nothing_is_written(self) -> None:
        for key in self.KEYS:
            for value in ("new-secret", None):
                with self.assertRaisesRegex(ValueError, "read-only"):
                    panel.update_configuration({key: value})
        self.assertEqual({key: panel.parse_env(self.vessel)[key] for key in self.KEYS}, {key: "original" for key in self.KEYS})

    def test_blank_value_is_a_no_op_not_an_error(self) -> None:
        panel.update_configuration({"INFLUXDB_TOKEN": "", "BOAT_NAME": "Renamed"})
        self.assertEqual(panel.parse_env(self.vessel)["INFLUXDB_TOKEN"], "original")
        self.assertEqual(panel.parse_env(self.vessel)["BOAT_NAME"], "Renamed")


class RetentionSettingTests(unittest.TestCase):
    def test_valid_and_invalid_raw_retention(self) -> None:
        spec = panel.FIELD_BY_KEY["INFLUXDB_RAW_RETENTION"]
        for good in ("", "0", "720h", "30d", "8w"):
            self.assertEqual(panel.validate_value(spec, good), good)
        for bad in ("30", "-1d", "30 days", "0d", "1y", "30d;rm", "100000d"):
            with self.assertRaises(ValueError):
                panel.validate_value(spec, bad)

    def test_description_warns_about_deletion(self) -> None:
        self.assertIn("deletes", panel.FIELD_BY_KEY["INFLUXDB_RAW_RETENTION"]["description"])


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
