#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vesselstack_control_panel", ROOT / "control-panel/app.py")
assert SPEC and SPEC.loader
panel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(panel)
# Keep the durable lifecycle lock inside the test fixture rather than the
# privileged production default. Individual tests still patch their env files.
panel.ROOT = Path(tempfile.mkdtemp(prefix="vesselstack-panel-test-"))


sys.path[:0] = [str(ROOT / "displays"), str(ROOT / "tests"), str(ROOT / "tests" / "fixtures")]


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

    def test_legacy_nautical_units_display_as_metric_without_rewriting_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vessel = root / "vessel.env"
            vessel.write_text("BOAT_NAME=Old Boat\nBOAT_UNITS=nautical\n")
            before = vessel.read_bytes()
            with mock.patch.multiple(panel, VESSEL_ENV=vessel, CHAT_ENV=root / "chat.env", PANEL_ENV=root / "control.env"):
                values = panel.public_configuration()["values"]
            self.assertEqual(values["BOAT_UNITS"], "metric")
            self.assertEqual(vessel.read_bytes(), before)
            self.assertIn(values["BOAT_UNITS"], panel.FIELD_BY_KEY["BOAT_UNITS"]["choices"])
        with self.assertRaises(ValueError):
            panel.validate_value(panel.FIELD_BY_KEY["BOAT_UNITS"], "imperial")

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
    def test_component_membership_commands_are_allowlisted(self) -> None:
        self.assertEqual(panel.component_command("grafana", "remove")[-3:], ["component", "remove", "grafana"])
        with self.assertRaises(ValueError):
            panel.component_command("arbitrary", "remove")

    def test_disabled_component_value_is_canonical_and_validated(self) -> None:
        spec = panel.FIELD_BY_KEY["VESSELSTACK_DISABLED_COMPONENTS"]
        self.assertEqual(panel.validate_value(spec, "grafana, influxdb"), "grafana,influxdb")
        with self.assertRaises(ValueError):
            panel.validate_value(spec, "grafana,grafana")
        with self.assertRaises(ValueError):
            panel.validate_value(spec, "unknown")

    def test_excluded_component_cannot_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "vessel.env"
            panel.write_env(env, {"VESSELSTACK_DISABLED_COMPONENTS": "grafana"}, [])
            with mock.patch.object(panel, "VESSEL_ENV", env):
                with self.assertRaises(ValueError):
                    panel.component_command("grafana", "start")

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

    def test_metrics_and_sample_workers_follow_enable_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "vessel.env"
            env.write_text("NODE_EXPORTER_ENABLE=false\nTELEMETRY_INDEXER_ENABLE=false\n")
            with mock.patch.object(panel, "VESSEL_ENV", env):
                for component in ("node-exporter", "telemetry-sample"):
                    with self.assertRaises(ValueError):
                        panel.component_command(component, "start")
                env.write_text("NODE_EXPORTER_ENABLE=true\nTELEMETRY_INDEXER_ENABLE=true\n")
                self.assertIn("node-exporter", panel.component_command("node-exporter", "start"))
                self.assertEqual(panel.component_command("telemetry-sample", "restart"), ["systemctl", "restart", "vesselstack-telemetry-sample.timer"])

    def test_displays_bridge_is_opt_in_and_token_is_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vessel = Path(directory) / "vessel.env"
            with mock.patch.object(panel, "VESSEL_ENV", vessel):
                panel.write_env(vessel, {"DISPLAYS_ENABLE": "false"}, [])
                with self.assertRaisesRegex(ValueError, "disabled"):
                    panel.component_command("displays-bridge", "start")
                panel.write_env(vessel, {"DISPLAYS_ENABLE": "true"}, [])
                self.assertEqual(panel.component_command("displays-bridge", "restart"),
                                 ["systemctl", "restart", "vesselstack-displays-bridge.service"])
        self.assertEqual(panel.FIELD_BY_KEY["DISPLAYS_SIGNALK_TOKEN"]["kind"], "secret")
        self.assertEqual(panel.FIELD_BY_KEY["DISPLAYS_HA_TOKEN"]["kind"], "secret")
        self.assertEqual(panel.FIELD_BY_KEY["DISPLAYS_HA_TOKEN"]["section"], "Displays")

    def test_signal_k_naming_units_choices_and_help_text(self) -> None:
        sections = {item["section"] for item in panel.FIELDS}
        self.assertIn("Signal K & NMEA 2000", sections)
        self.assertNotIn("SignalK", sections)
        for item in panel.FIELDS:
            self.assertNotIn("SignalK", item["label"], item["key"])
            self.assertNotIn("SignalK", item["description"], item["key"])
        self.assertEqual(panel.FIELD_BY_KEY["BOAT_UNITS"]["choices"], ["us_customary", "metric"])
        self.assertEqual(panel.FIELD_BY_KEY["SIGNALK_MODE"]["choices"], ["existing", "docker", "native"])
        for key in ("SIGNALK_MODE", "SOCKETCAN_BITRATE", "AIS_DEVICE", "BOAT_TYPE", "BOAT_TIMEZONE"):
            self.assertTrue(panel.FIELD_BY_KEY[key]["description"], key)
        self.assertEqual(panel.FIELD_BY_KEY["BOAT_TIMEZONE"]["label"], "Timezone")

    def test_displays_values_mirror_the_installer_checks(self) -> None:
        user = panel.FIELD_BY_KEY["DISPLAYS_BRIDGE_MQTT_USERNAME"]
        for good in ("vesselstack-bridge", "a", "A_b.c-9", "x" * 64, ""):
            self.assertEqual(panel.validate_value(user, good), good)
        for bad in ("bad name", "x" * 65, "bad:name", "bad/name", "b\u00e9", "bad;", "a b"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                panel.validate_value(user, bad)
        # Surrounding whitespace is stripped by validate_value; the pattern itself never accepts a newline.
        self.assertFalse(panel.MQTT_ACCOUNT_NAME.fullmatch("bridge\n"))
        with self.assertRaises(ValueError):
            panel.validate_value(panel.FIELD_BY_KEY["MQTT_USERNAME"], "bad name")
        token = panel.FIELD_BY_KEY["DISPLAYS_SIGNALK_TOKEN"]
        self.assertEqual(panel.validate_value(token, 'a$b`c"d\\e\'f#g;h%i j'), 'a$b`c"d\\e\'f#g;h%i j')
        for bad in ("a\tb", "a\x01b", "a\x7fb", "a\x00b", "a\nb", "a\rb", "a\x1bb"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                panel.validate_value(token, bad)

    def test_ha_token_rejects_control_characters_and_is_never_echoed(self) -> None:
        token = panel.FIELD_BY_KEY["DISPLAYS_HA_TOKEN"]
        self.assertEqual(panel.validate_value(token, "eyJ.abc_-d"), "eyJ.abc_-d")
        self.assertEqual(panel.validate_value(token, ""), "")
        for bad in ("a\tb", "a\nb", "a\x7fb", "a\x00b", "a\rb"):
            with self.assertRaises(ValueError, msg=repr(bad)) as caught:
                panel.validate_value(token, bad)
            self.assertTrue(str(caught.exception).startswith("Displays bridge Home Assistant token "))
            self.assertNotIn(bad, str(caught.exception))

    def test_bridge_username_must_differ_from_the_mqtt_username(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vessel, chat, control = root / "vessel.env", root / "chat.env", root / "control.env"
            panel.write_env(vessel, {"MQTT_USERNAME": "homeassistant", "DISPLAYS_ENABLE": "false"}, [])
            panel.write_env(chat, {}, [])
            panel.write_env(control, {"CONTROL_PANEL_TOKEN": "t"}, [])
            before = vessel.read_text()
            with mock.patch.multiple(panel, VESSEL_ENV=vessel, CHAT_ENV=chat, PANEL_ENV=control,
                                     CONFIG_BACKUP_ROOT=root / "backups"):
                # Not enabled: the shared name is harmless until displays are switched on.
                panel.update_configuration({"DISPLAYS_BRIDGE_MQTT_USERNAME": "homeassistant"})
                with self.assertRaisesRegex(ValueError, "must differ"):
                    panel.update_configuration({"DISPLAYS_ENABLE": "true"})
                with self.assertRaisesRegex(ValueError, "must differ"):
                    panel.update_configuration({"DISPLAYS_ENABLE": "true",
                                                "DISPLAYS_BRIDGE_MQTT_USERNAME": "homeassistant"})
                self.assertEqual(panel.parse_env(vessel)["DISPLAYS_ENABLE"], "false")
                # The default bridge name collides with a MQTT user of the same name.
                panel.update_configuration({"MQTT_USERNAME": "vesselstack-bridge",
                                            "DISPLAYS_BRIDGE_MQTT_USERNAME": ""})
                with self.assertRaisesRegex(ValueError, "must differ"):
                    panel.update_configuration({"DISPLAYS_ENABLE": "true"})
                panel.update_configuration({"DISPLAYS_ENABLE": "true", "MQTT_USERNAME": "homeassistant"})
                self.assertEqual(panel.parse_env(vessel)["DISPLAYS_ENABLE"], "true")
            self.assertNotEqual(before, vessel.read_text())

    def test_update_requires_reviewed_release_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "vesselstack-test"
            release.mkdir()
            with self.assertRaises(ValueError):
                panel.action_command("update", {"source_directory": release})
            (release / "install.sh").write_text("#!/bin/bash\n")
            (release / "VERSION").write_text("0.0.2\n")
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
            (release / "VERSION").write_text("0.0.2\n")
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
            (release / "VERSION").write_text("0.0.2\n")
            with mock.patch.object(panel, "REVIEWED_RELEASE_ROOT", root), mock.patch.object(panel.os, "geteuid", return_value=os.getuid() + 12345):
                with self.assertRaises(ValueError):
                    panel.action_command("update", {"source_directory": release})


class LifecycleStateTests(unittest.TestCase):
    def test_status_reports_unknown_for_malformed_docker_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "vessel.env"
            panel.write_env(env, {"VESSELSTACK_DISABLED_COMPONENTS": "grafana"}, [])
            def output(command: list[str], timeout: int = 8) -> tuple[int, str]:
                if command[:2] == ["docker", "compose"]:
                    return (0, "not-json")
                return (3, "inactive")
            with mock.patch.object(panel, "VESSEL_ENV", env), mock.patch.object(panel, "command_output", side_effect=output):
                statuses = panel.component_status()
            self.assertEqual(statuses[0]["state"], "unknown")
            self.assertEqual(statuses[2]["state"], "unknown")
            self.assertEqual(statuses[-1]["state"], "inactive")

    def test_status_reports_stopped_for_valid_empty_docker_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "vessel.env"
            panel.write_env(env, {}, [])
            def output(command: list[str], timeout: int = 8) -> tuple[int, str]:
                return (0, "[]") if command[:2] == ["docker", "compose"] else (3, "inactive")
            with mock.patch.object(panel, "VESSEL_ENV", env), mock.patch.object(panel, "command_output", side_effect=output):
                statuses = panel.component_status()
            self.assertEqual(statuses[0]["state"], "stopped")

    def test_status_reports_stopped_for_valid_rows_missing_a_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "vessel.env"
            panel.write_env(env, {}, [])
            def output(command: list[str], timeout: int = 8) -> tuple[int, str]:
                return (0, '[{"Service":"grafana","State":"running"}]') if command[:2] == ["docker", "compose"] else (3, "inactive")
            with mock.patch.object(panel, "VESSEL_ENV", env), mock.patch.object(panel, "command_output", side_effect=output):
                statuses = panel.component_status()
            self.assertEqual(statuses[0]["state"], "stopped")
            self.assertEqual(statuses[2]["state"], "running")

    def test_status_reports_unknown_for_invalid_compose_json_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "vessel.env"
            panel.write_env(env, {}, [])
            def output(command: list[str], timeout: int = 8) -> tuple[int, str]:
                return (0, "null") if command[:2] == ["docker", "compose"] else (3, "inactive")
            with mock.patch.object(panel, "VESSEL_ENV", env), mock.patch.object(panel, "command_output", side_effect=output):
                statuses = panel.component_status()
            self.assertEqual(statuses[0]["state"], "unknown")

    def test_operation_lock_rejects_busy_configuration_and_releases_after_failure(self) -> None:
        self.assertTrue(panel.LIFECYCLE_LOCK.acquire(blocking=False))
        try:
            with self.assertRaises(ValueError):
                panel.update_configuration({})
        finally:
            panel.LIFECYCLE_LOCK.release()

        # Poll only this short-lived local operation through its store.
        local_store = panel.OperationStore()
        local_store.start("test", ["/path/that/does/not/exist"])
        for _ in range(50):
            if local_store.public()["state"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual(local_store.public()["state"], "failed")
        local_store.start("test", ["/usr/bin/true"])
        for _ in range(50):
            if local_store.public()["state"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual(local_store.public()["state"], "succeeded")

    def test_operation_start_failure_releases_admission_lock(self) -> None:
        local_store = panel.OperationStore()
        with mock.patch.object(panel.subprocess, "Popen", side_effect=OSError("spawn failed")):
            local_store.start("test", ["mock-command"])
        for _ in range(50):
            if local_store.public()["state"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual(local_store.public()["state"], "failed")
        self.assertTrue(panel.LIFECYCLE_LOCK.acquire(blocking=False))
        panel.LIFECYCLE_LOCK.release()


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
                # A non-ASCII header value is a plain mismatch, not a TypeError.
                handler.headers = {"X-VesselStack-Token": "t\u00f6ken"}
                self.assertFalse(handler.authenticated())
                # A pasted CONTROL_PANEL_TOKEN=... line, with or without quotes, is the same token.
                for accepted in ("CONTROL_PANEL_TOKEN=test-panel-token", "  CONTROL_PANEL_TOKEN = test-panel-token \n",
                                 "CONTROL_PANEL_TOKEN='test-panel-token'", "export CONTROL_PANEL_TOKEN=\"test-panel-token\"", " test-panel-token "):
                    handler.headers = {"X-VesselStack-Token": accepted}
                    self.assertTrue(handler.authenticated(), accepted)
                for refused in ("CONTROL_PANEL_TOKEN=", "CONTROL_PANEL_TOKEN=wrong", "CONTROL_PANEL_TOKEN=test-panel-token-x", ""):
                    handler.headers = {"X-VesselStack-Token": refused}
                    self.assertFalse(handler.authenticated(), refused)

    def test_normalize_token_strips_prefix_whitespace_and_quotes(self) -> None:
        self.assertEqual(panel.normalize_token("CONTROL_PANEL_TOKEN=abc"), "abc")
        self.assertEqual(panel.normalize_token("  abc  "), "abc")
        self.assertEqual(panel.normalize_token("CONTROL_PANEL_TOKEN='a b'"), "a b")
        self.assertEqual(panel.normalize_token("abc=def"), "abc=def")


class SettingsValidationTests(unittest.TestCase):
    def check(self, key: str, value: str) -> str:
        return panel.validate_value(panel.FIELD_BY_KEY[key], value)

    def test_timezone_must_exist(self) -> None:
        for good in ("Europe/Lisbon", "America/Los_Angeles", "UTC", ""):
            self.assertEqual(self.check("BOAT_TIMEZONE", good), good)
        for bad in ("Mars/Olympus", "europe/lisbon", "Europe/Lisbn", "../etc/passwd", "Lisbon"):
            with self.assertRaisesRegex(ValueError, "Timezone not recognised"):
                self.check("BOAT_TIMEZONE", bad)

    def test_timezone_is_not_blocked_without_a_timezone_database(self) -> None:
        with mock.patch.object(panel.zoneinfo, "available_timezones", return_value=set()):
            self.assertEqual(self.check("BOAT_TIMEZONE", "Mars/Olympus"), "Mars/Olympus")

    def test_boat_size_ranges(self) -> None:
        for key, good, bad in (
            ("BOAT_LOA_M", ("13.2", "0.5", "500", ""), ("0", "-5", "500.1", "abc", "nan", "inf", "1e3", "1_0", "12 m", "13,2")),
            ("BOAT_BEAM_M", ("4.1", "100", ""), ("0", "-1", "100.5", "1e400", "-0")),
        ):
            for value in good:
                self.assertEqual(self.check(key, value), value)
            for value in bad:
                with self.assertRaises(ValueError, msg=f"{key}={value}"):
                    self.check(key, value)

    def test_can_bitrate_is_a_sensible_whole_number(self) -> None:
        for good in ("250000", "10000", "1000000", ""):
            self.assertEqual(self.check("SOCKETCAN_BITRATE", good), good)
        for bad in ("0", "9999", "1000001", "250000.5", "-250000", "fast"):
            with self.assertRaises(ValueError):
                self.check("SOCKETCAN_BITRATE", bad)

    def test_errors_are_reported_per_field_and_nothing_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vessel, chat, control = root / "vessel.env", root / "chat.env", root / "control.env"
            panel.write_env(vessel, {"BOAT_NAME": "Old", "BOAT_TIMEZONE": "Mars/Olympus"}, [])
            panel.write_env(chat, {}, [])
            panel.write_env(control, {"CONTROL_PANEL_TOKEN": "t"}, [])
            before = vessel.read_text()
            with mock.patch.multiple(panel, VESSEL_ENV=vessel, CHAT_ENV=chat, PANEL_ENV=control, CONFIG_BACKUP_ROOT=root / "backups"):
                with self.assertRaises(panel.FieldErrors) as caught:
                    panel._update_configuration({"BOAT_NAME": "New", "BOAT_TIMEZONE": "Mars/Phobos", "BOAT_LOA_M": "-5"})
                self.assertEqual(set(caught.exception.fields), {"BOAT_TIMEZONE", "BOAT_LOA_M"})
                self.assertEqual(vessel.read_text(), before)
                # A stale invalid stored value never blocks saving an unrelated setting.
                panel._update_configuration({"BOAT_NAME": "New"})
                self.assertEqual(panel.parse_env(vessel)["BOAT_NAME"], "New")

    def test_every_setting_has_plain_help_and_no_markdown(self) -> None:
        for item in panel.FIELDS:
            self.assertTrue(item["description"], item["key"])
            self.assertNotIn("`", item["description"], item["key"])
            self.assertNotIn("`", item["label"], item["key"])
        names = {section["name"] for section in panel.SECTIONS}
        self.assertEqual({item["section"] for item in panel.FIELDS}, names)


class StatusFieldTests(unittest.TestCase):
    def test_components_use_real_names_and_one_plain_status(self) -> None:
        expected = {"signalk": "Signal K", "ais-catcher": "AIS-catcher", "socketcan": "SocketCAN", "node-exporter": "Node Exporter",
                    "homeassistant": "Home Assistant", "influxdb": "InfluxDB", "mosquitto": "Mosquitto", "boat-chat": "Boat Chat",
                    "telegram": "Telegram alerts", "prometheus": "Prometheus", "heimdall": "Heimdall", "displays-bridge": "Displays bridge"}
        for name, label in expected.items():
            self.assertEqual(panel.describe_component(name, "container", True, "running")["label"], label)
        self.assertEqual(panel.describe_component("grafana", "container", True, "running")["status"], "running")
        self.assertEqual(panel.describe_component("grafana", "container", True, "exited")["status"], "stopped")
        self.assertEqual(panel.describe_component("grafana", "container", True, "unknown")["status"], "unknown")
        failed = panel.describe_component("boat-chat", "systemd", True, "failed")
        self.assertEqual((failed["status"], failed["detail"]), ("stopped", "failed"))
        # Turned off in settings but still running: Disabled, never "Active and Available".
        leftover = panel.describe_component("telegram", "systemd", False, "active")
        self.assertEqual((leftover["status"], leftover["still_running"]), ("disabled", True))
        self.assertEqual(panel.describe_component("telegram", "systemd", False, "inactive")["status"], "disabled")
        for name in list(panel.COMPOSE_COMPONENTS) + list(panel.SYSTEMD_COMPONENTS):
            info = panel.describe_component(name, "container", True, "running")
            self.assertTrue(info["label"] and info["label"] == info["label"].strip(), name)
            self.assertNotIn(info["label"], {"Signalk", "Ais Catcher", "Socketcan", "Node Exporter ", "Boat Chat ", "Displays Bridge"}, name)

    def test_backup_status_reads_newest_checksummed_archive_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / "backups"
            backup.mkdir()
            old, new, orphan = backup / "vesselstack-20260101T000000Z.tar.gz", backup / "vesselstack-20260201T000000Z.tar.gz", backup / "vesselstack-20260301T000000Z.tar.gz"
            for archive, when in ((old, 1_700_000_000), (new, 1_750_000_000), (orphan, 1_790_000_000)):
                archive.write_bytes(b"x")
                os.utime(archive, (when, when))
            (backup / (old.name + ".sha256")).write_text("x")
            (backup / (new.name + ".sha256")).write_text("x")
            status = panel.backup_status({"VESSELSTACK_BACKUP": str(backup)})
            self.assertEqual(status["last_backup_at"], 1_750_000_000)
            self.assertEqual(status["mount"], "system_disk")
            self.assertEqual(status["state"], "ok")
            self.assertEqual({path.name for path in backup.iterdir()}, {old.name, new.name, orphan.name, old.name + ".sha256", new.name + ".sha256"})

    def test_backup_status_unknown_or_missing_cases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(panel.backup_status({"VESSELSTACK_BACKUP": str(Path(directory) / "none")})["state"], "none")
            empty = Path(directory) / "empty"
            empty.mkdir()
            self.assertIsNone(panel.backup_status({"VESSELSTACK_BACKUP": str(empty)})["last_backup_at"])
        with mock.patch.object(panel.os.path, "ismount", return_value=False):
            unmounted = panel.backup_status({"VESSELSTACK_BACKUP": "/media/backup-drive/vesselstack"})
        self.assertEqual((unmounted["mount"], unmounted["state"], unmounted["last_backup_at"]), ("not_mounted", "unknown", None))
        self.assertIn("not mounted", unmounted["message"])
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(panel.os, "scandir", side_effect=PermissionError("denied")):
                denied = panel.backup_status({"VESSELSTACK_BACKUP": directory})
        self.assertEqual((denied["state"], denied["last_backup_at"]), ("unknown", None))

    def test_operation_summary_names_the_saved_file_or_the_reason(self) -> None:
        ok = panel.summarize_operation({"label": "backup", "state": "succeeded", "output": ["copying", "/var/backups/vesselstack/vesselstack-20260101T000000Z.tar.gz"]})
        self.assertEqual(ok["saved_path"], "/var/backups/vesselstack/vesselstack-20260101T000000Z.tar.gz")
        bad = panel.summarize_operation({"label": "backup", "state": "failed", "output": ["ctl: running backup", "ERROR: backup mount /media/backup is not mounted"]})
        self.assertEqual(bad["reason"], "backup mount /media/backup is not mounted")
        self.assertEqual(panel.with_summary({"state": "running"}), {"state": "running"})

    def test_operations_run_non_interactively(self) -> None:
        seen: dict = {}

        class FakeProcess:
            stdout = iter(["done\n"])

            def wait(self) -> int:
                return 0

        def fake_popen(command, **kwargs):
            seen.update(kwargs["env"])
            return FakeProcess()

        local_store = panel.OperationStore()
        operation = {"label": "backup", "state": "running", "output": panel.deque(maxlen=10), "exit_code": None, "finished_at": None}
        with mock.patch.object(panel.subprocess, "Popen", side_effect=fake_popen), mock.patch.object(panel, "release_lifecycle_slot"):
            local_store._run(operation, ["true"], None)
        self.assertEqual(seen.get("VESSELSTACK_NONINTERACTIVE"), "true")

    def test_operation_summary_carries_the_to_fix_line(self) -> None:
        def failed(*lines: str) -> dict:
            return panel.summarize_operation({"label": "update", "state": "failed", "output": list(lines)})

        update = failed(
            "Refusing to back up: /media/backup is not mounted, so the archive would land on the system disk (SD card). Nothing was changed.",
            "To fix: mount the backup drive at /media/backup and run again, or back up to the system disk this once: sudo env VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=true vesselstackctl update /opt/vesselstack-0.6.1",
        )
        self.assertTrue(update["reason"].startswith("/media/backup is not mounted"))
        self.assertTrue(update["fix"].startswith("mount the backup drive at /media/backup and run again, or back up to the system disk this once: sudo env "))
        backup = failed(
            "Refusing to back up: /media/backup is not mounted, so the archive would land on the system disk (SD card). Nothing was changed.",
            "To fix: mount the backup drive at /media/backup and run again, or pass --allow-unmounted.",
        )
        self.assertEqual(backup["fix"], "mount the backup drive at /media/backup and run again, or pass --allow-unmounted.")
        units = failed(
            "Refusing to continue: BOAT_UNITS=imperial in /etc/vesselstack/vesselstack.env is not supported.",
            "To fix: set it to us_customary or metric, for example: sudo sed -i 's/^BOAT_UNITS=.*/BOAT_UNITS=metric/' /etc/vesselstack/vesselstack.env",
        )
        self.assertEqual(units["reason"], "BOAT_UNITS=imperial in /etc/vesselstack/vesselstack.env is not supported.")
        self.assertTrue(units["fix"].startswith("set it to us_customary or metric, for example: sudo sed -i "))
        unsafe = failed(
            "  MQTT_PASSWORD: a multi-line value",
            "Refusing to continue: the settings above cannot be written safely to the environment files.",
            "To fix: edit them in /etc/vesselstack/vesselstack.env (values cannot span lines), then run again. Nothing was changed.",
        )
        self.assertEqual(unsafe["reason"], "the settings above cannot be written safely to the environment files.")
        self.assertTrue(unsafe["fix"].startswith("edit them in /etc/vesselstack/vesselstack.env"))
        self.assertNotIn("fix", failed("ERROR: boom"))
        # A To fix line printed before the refusal belongs to something else.
        self.assertNotIn("fix", failed("To fix: earlier", "ERROR: boom"))

    def test_restart_note_only_when_a_newer_panel_is_installed(self) -> None:
        with mock.patch.object(panel, "installed_version", return_value="0.6.1"), mock.patch.object(panel, "RUNNING_IDENTITY", {"running_version": "0.6.0"}):
            self.assertTrue(panel.restart_needed())
        with mock.patch.object(panel, "installed_version", return_value="0.6.0"), mock.patch.object(panel, "RUNNING_IDENTITY", {"running_version": "0.6.0"}):
            self.assertFalse(panel.restart_needed())


class DisplaysApiTests(unittest.TestCase):
    """GET /api/displays*: fixed argv, auth, cache, error mapping and no secrets (subprocess is mocked)."""

    TOKEN = "panel-token-for-displays-tests"
    CTL = "/usr/local/sbin/vesselstackctl"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        control = Path(self._tmp.name) / "control.env"
        panel.write_env(control, {"CONTROL_PANEL_TOKEN": self.TOKEN}, [])
        patches = [mock.patch.object(panel, "PANEL_ENV", control), mock.patch.object(panel, "CTL", Path(self.CTL)),
                   mock.patch.dict(panel.os.environ, {}, clear=True)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        quiet = mock.patch.object(panel.ControlPanelHandler, "log_message", lambda *a, **k: None)
        quiet.start()
        self.addCleanup(quiet.stop)
        panel.invalidate_display_caches()
        self.addCleanup(panel.invalidate_display_caches)
        self.calls: list[list[str]] = []
        self.responses: dict[tuple, subprocess.CompletedProcess] = {}
        run = mock.patch.object(panel.subprocess, "run", side_effect=self.fake_run)
        run.start()
        self.addCleanup(run.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), panel.ControlPanelHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def fake_run(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.assertEqual(kwargs.get("stderr"), subprocess.PIPE, "stderr must never be merged into the JSON")
        self.assertIn("timeout", kwargs)
        answer = self.responses.get(tuple(argv[1:]))
        if isinstance(answer, BaseException):
            raise answer
        return answer or subprocess.CompletedProcess(argv, 1, "", "unexpected call")

    def answer(self, args, payload, code=0, stderr="") -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        self.responses[tuple(args)] = subprocess.CompletedProcess([], code, text, stderr)

    def get(self, path, token="default"):
        headers = {} if token is None else {"X-VesselStack-Token": self.TOKEN if token == "default" else token}
        request = urllib.request.Request(f"http://127.0.0.1:{self.server.server_address[1]}{path}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_every_endpoint_needs_the_token(self) -> None:
        for path in ("/api/displays", "/api/displays?id=helm", "/api/displays/themes", "/api/displays/catalog",
                     "/api/displays/catalog?live=1", "/api/displays/serial-ports"):
            self.assertEqual(self.get(path, None)[0], 401, path)
            self.assertEqual(self.get(path, "wrong")[0], 401, path)
        self.assertEqual(self.calls, [], "nothing runs before authentication")

    def test_display_id_regex_matches_the_cli(self) -> None:
        import schema
        self.assertEqual(panel.DISPLAY_ID_RE.pattern, schema.DISPLAY_ID.pattern)

    def test_themes_and_serial_ports_argv(self) -> None:
        self.answer(["display", "themes", "--json"], {"schema": 1, "themes": {"classic": {}}})
        self.answer(["display", "serial-ports", "--json"], {"schema": 1, "ports": []})
        self.assertEqual(self.get("/api/displays/themes")[0], 200)
        self.assertEqual(self.get("/api/displays/serial-ports")[0], 200)
        self.assertEqual(self.calls, [[self.CTL, "display", "themes", "--json"], [self.CTL, "display", "serial-ports", "--json"]])

    def test_displays_lists_then_shows_each_display_with_fixed_argv(self) -> None:
        self.answer(["display", "list", "--json"], {"schema": 1, "displays": [{"id": "helm", "name": "Helm", "board": "b", "pages": 1}, {"id": "aux", "name": "Aux", "board": "b", "pages": 1}]})
        self.answer(["display", "show", "helm", "--json"], {"schema": 1, "display": {"id": "helm"}})
        self.answer(["display", "show", "aux", "--json"], {"ok": False, "code": 2, "errors": ["boom in aux"]}, code=2)
        status, body = self.get("/api/displays")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["displays"][0], {"schema": 1, "display": {"id": "helm"}})
        self.assertEqual(data["displays"][1], {"id": "aux", "name": "Aux", "board": "b", "error": "boom in aux"}, "one failing display does not fail the rest")
        self.assertEqual(sorted(self.calls), sorted([[self.CTL, "display", "list", "--json"], [self.CTL, "display", "show", "helm", "--json"], [self.CTL, "display", "show", "aux", "--json"]]))

    def test_single_display_and_invalid_ids(self) -> None:
        self.answer(["display", "list", "--json"], {"schema": 1, "displays": [{"id": "helm", "name": "Helm", "board": "b", "pages": 1}]})
        self.answer(["display", "show", "helm", "--json"], {"schema": 1, "display": {"id": "helm"}})
        self.assertEqual(len(json.loads(self.get("/api/displays?id=helm")[1])["displays"]), 1)
        self.assertEqual(self.get("/api/displays?id=other")[0], 404)
        self.calls.clear()
        for bad in ("Helm", "-x", "a b", "a;rm", "../x", "x" * 40, "a%00b"):
            self.assertEqual(self.get("/api/displays?id=" + urllib.request.quote(bad))[0], 400, bad)
        self.assertEqual(self.calls, [], "an invalid id never reaches a command line")
        for bad in ("Helm", "--live", "a b", None, 5):
            with self.assertRaises(ValueError):
                panel.valid_display_id(bad)
        self.assertEqual(panel.valid_display_id("helm-2"), "helm-2")

    def test_listed_ids_are_validated_too(self) -> None:
        self.answer(["display", "list", "--json"], {"schema": 1, "displays": [{"id": "--evil", "name": "x", "board": "b"}, {"id": "ok", "name": "Ok", "board": "b"}]})
        self.answer(["display", "show", "ok", "--json"], {"schema": 1, "display": {"id": "ok"}})
        self.assertEqual(self.get("/api/displays")[0], 200)
        self.assertNotIn([self.CTL, "display", "show", "--evil", "--json"], self.calls)

    def test_catalog_argv_live_flag_and_validation(self) -> None:
        self.answer(["display", "catalog", "--json"], {"schema": 1, "rows": [], "live": {"requested": False}})
        self.answer(["display", "catalog", "--live", "--json"], {"schema": 1, "rows": [], "live": {"requested": True}})
        self.assertEqual(self.get("/api/displays/catalog")[0], 200)
        self.assertEqual(self.get("/api/displays/catalog?live=0")[0], 200)
        self.assertEqual(self.get("/api/displays/catalog?live=1")[0], 200)
        self.assertEqual(self.calls, [[self.CTL, "display", "catalog", "--json"], [self.CTL, "display", "catalog", "--live", "--json"]])
        self.calls.clear()
        for bad in ("yes", "true", "1;x", "--json", "2"):
            self.assertEqual(self.get("/api/displays/catalog?live=" + urllib.request.quote(bad))[0], 400, bad)
        self.assertEqual(self.calls, [])

    def test_catalog_cache_keyed_by_live_expires_and_skips_errors(self) -> None:
        self.answer(["display", "catalog", "--json"], {"schema": 1, "rows": []})
        self.answer(["display", "catalog", "--live", "--json"], {"schema": 1, "rows": []})
        clock = [1000.0]
        with mock.patch.object(panel.time, "monotonic", side_effect=lambda: clock[0]):
            self.get("/api/displays/catalog"); self.get("/api/displays/catalog")
            self.assertEqual(len(self.calls), 1, "second call within 30 s is served from memory")
            self.get("/api/displays/catalog?live=1")
            self.assertEqual(len(self.calls), 2, "the live flag is part of the cache key")
            clock[0] += 29
            self.get("/api/displays/catalog"); self.assertEqual(len(self.calls), 2)
            clock[0] += 2
            self.get("/api/displays/catalog"); self.assertEqual(len(self.calls), 3, "expires after 30 s")
            panel.invalidate_display_caches()
            self.get("/api/displays/catalog"); self.assertEqual(len(self.calls), 4, "invalidate_display_caches forgets it")
            # A failure is not cached.
            panel.invalidate_display_caches(); self.calls.clear()
            self.answer(["display", "catalog", "--json"], {"ok": False, "code": 1, "errors": ["x"]}, code=1)
            self.assertEqual(self.get("/api/displays/catalog")[0], 502)
            self.answer(["display", "catalog", "--json"], {"schema": 1, "rows": []})
            self.assertEqual(self.get("/api/displays/catalog")[0], 200)
            self.assertEqual(len(self.calls), 2)

    def test_catalog_uses_a_short_timeout(self) -> None:
        self.answer(["display", "catalog", "--live", "--json"], {"schema": 1, "rows": []})
        seen = {}
        original = self.fake_run
        def spy(argv, **kwargs):
            seen.update(kwargs)
            return original(argv, **kwargs)
        with mock.patch.object(panel.subprocess, "run", side_effect=spy):
            self.get("/api/displays/catalog?live=1")
        self.assertEqual(seen["timeout"], 12)

    def test_cli_failures_become_structured_errors_without_leaking(self) -> None:
        trace = "Traceback (most recent call last):\n  File \"/opt/vesselstack/displays/x.py\", line 1\nSECRET-IN-STDERR"
        cases = [
            (subprocess.CompletedProcess([], 2, json.dumps({"ok": False, "code": 2, "errors": ["displays.json is invalid"]}), trace), 400, "displays.json is invalid"),
            (subprocess.CompletedProcess([], 3, json.dumps({"ok": False, "code": 3, "errors": ["changed"]}), trace), 409, "changed"),
            (subprocess.CompletedProcess([], 1, json.dumps({"ok": False, "code": 1, "errors": ["cannot read"]}), trace), 502, "cannot read"),
            (subprocess.CompletedProcess([], 1, "", trace), 502, "The display tool failed"),
            (subprocess.CompletedProcess([], 0, "not json", ""), 502, "could not read"),
            (subprocess.CompletedProcess([], 0, "[1, 2]", ""), 502, "could not read"),
            (subprocess.TimeoutExpired(["x"], 12), 504, "too long"),
            (FileNotFoundError("/usr/local/sbin/vesselstackctl"), 502, "not available"),
        ]
        for answer, status, text in cases:
            self.responses[("display", "serial-ports", "--json")] = answer
            code, body = self.get("/api/displays/serial-ports")
            self.assertEqual(code, status, body)
            data = json.loads(body)
            self.assertIn(text, data["error"])
            self.assertNotIn("Traceback", body); self.assertNotIn("SECRET-IN-STDERR", body); self.assertNotIn("/usr/local/sbin", body)

    def test_responses_from_real_cli_output_carry_no_secret(self) -> None:
        import test_displays_cli as cli
        from contextlib import redirect_stdout
        import editor
        case = type("Fixture", (cli.EditorTests,), {"runTest": lambda self: None})()
        case.setUp()
        self.addCleanup(case.doCleanups)
        ctx = case.ctx()
        ctx.env["DISPLAYS_SIGNALK_TOKEN"] = cli.SK_TOKEN
        case.write_config(case.cfg()["instruments"], case.cfg()["displays"])
        secrets = [cli.ADMIN_PASSWORD, cli.WIFI_PASSWORD, cli.SK_TOKEN, "boat-net-ssid", case.sec()["displays"]["helm"]["mqtt_password"], case.sec()["bridge"]["mqtt_password"], self.TOKEN]
        self.answer(["display", "list", "--json"], editor.list_displays(ctx))
        self.answer(["display", "show", "helm", "--json"], editor.show(ctx, "helm"))
        self.answer(["display", "catalog", "--json"], editor.catalog(ctx))
        self.answer(["display", "serial-ports", "--json"], editor.serial_ports(ctx))
        for path in ("/api/displays", "/api/displays/catalog", "/api/displays/serial-ports"):
            status, body = self.get(path)
            self.assertEqual(status, 200, body)
            for value in secrets:
                self.assertNotIn(value, body, path)
        self.assertEqual(json.loads(self.get("/api/displays")[1])["displays"][0]["display"]["id"], "helm")



class DisplaysPreviewFixtureTests(unittest.TestCase):
    """tests/fixtures/displays/panel_show.json feeds the JS preview tests; it must be real `show` output."""

    FIXTURE = ROOT / "tests" / "fixtures" / "displays" / "panel_show.json"
    KEYS = ("display", "theme", "themes", "board", "layouts", "preview", "instruments", "notifications", "ha")

    def current(self) -> dict:
        import test_displays_cli as cli
        import editor
        case = type("Fixture", (cli.EditorTests,), {"runTest": lambda self: None})()
        case.setUp()
        self.addCleanup(case.doCleanups)
        config = json.loads((ROOT / "displays" / "displays.dashboard.example.json").read_text())
        config["displays"]["helm"]["board"] = cli.BOARD
        case.config.write_text(json.dumps(config))
        shown = editor.show(case.ctx(), "helm")
        return {key: shown[key] for key in self.KEYS}

    def test_fixture_matches_the_renderer(self) -> None:
        current = self.current()
        if os.environ.get("UPDATE_PANEL_FIXTURE") == "1":
            self.FIXTURE.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n")
        self.assertEqual(json.loads(self.FIXTURE.read_text()), json.loads(json.dumps(current, sort_keys=True)),
                         "regenerate with UPDATE_PANEL_FIXTURE=1 python3 -B -m unittest tests/test_control_panel.py")

if __name__ == "__main__":
    unittest.main()