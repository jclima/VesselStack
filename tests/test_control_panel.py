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


class TokenRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.vessel, self.chat, self.control = root / "vessel.env", root / "chat.env", root / "control.env"
        panel.write_env(self.vessel, {"BOAT_NAME": "Test"}, [])
        panel.write_env(self.chat, {"BOAT_CHAT_ACCESS_TOKEN": "old-access-code"}, [])
        panel.write_env(self.control, {"CONTROL_PANEL_TOKEN": "panel-token"}, [])
        patch = mock.patch.multiple(panel, VESSEL_ENV=self.vessel, CHAT_ENV=self.chat, PANEL_ENV=self.control, CONFIG_BACKUP_ROOT=root / "backups")
        patch.start()
        self.addCleanup(patch.stop)
        self.store = mock.Mock()
        self.store.revoke_all.return_value = 3
        chat_store = mock.patch.object(panel, "chat_auth_store", return_value=self.store)
        chat_store.start()
        self.addCleanup(chat_store.stop)

    def test_new_access_code_signs_out_all_boat_chat_devices_through_the_helper_store(self) -> None:
        notes: list[str] = []
        panel.update_configuration({"BOAT_CHAT_ACCESS_TOKEN": "new-access-code", "BOAT_NAME": "Renamed"}, notes)
        self.store.revoke_all.assert_called_once_with()
        self.assertEqual(panel.parse_env(self.chat)["BOAT_CHAT_ACCESS_TOKEN"], "new-access-code")
        self.assertTrue(any("3 signed-in device" in note for note in notes), notes)

    def test_unchanged_blank_or_unrelated_saves_keep_devices(self) -> None:
        panel.update_configuration({"BOAT_CHAT_ACCESS_TOKEN": "old-access-code"})
        panel.update_configuration({"BOAT_CHAT_ACCESS_TOKEN": ""})
        panel.update_configuration({"BOAT_NAME": "Other"})
        self.store.revoke_all.assert_not_called()

    def test_override_file_shadowing_counts_as_the_effective_old_value(self) -> None:
        data = Path(self._tmp.name) / "data"
        (data / "boat-chat").mkdir(parents=True)
        panel.write_env(self.vessel, {"BOAT_NAME": "Test", "VESSELSTACK_DATA": str(data)}, [])
        (data / "boat-chat" / "boat-chat.env").write_text("BOAT_CHAT_ACCESS_TOKEN=page-code\n")
        panel.update_configuration({"BOAT_CHAT_ACCESS_TOKEN": "old-access-code"})  # equals the file value, but not what was in force
        self.store.revoke_all.assert_called_once_with()

    def test_failed_revocation_is_reported_not_hidden(self) -> None:
        self.store.revoke_all.side_effect = panel.vsauth.StoreError("boom")
        notes: list[str] = []
        panel.update_configuration({"BOAT_CHAT_ACCESS_TOKEN": "another-access-code"}, notes)
        self.assertEqual(panel.parse_env(self.chat)["BOAT_CHAT_ACCESS_TOKEN"], "another-access-code")
        self.assertTrue(any("revoke-all chat" in note for note in notes), notes)

    def test_panel_never_writes_the_chat_store_in_process_as_root(self) -> None:
        source = (Path(panel.__file__)).read_text()
        self.assertIn("inherit_owner=True", source[source.index("def chat_auth_store"):source.index("def auth_store")])


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
                    "telegram": "Telegram alerts", "prometheus": "Prometheus", "homepage": "Home page", "displays-bridge": "Displays bridge"}
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

    # ---- backup preflight and one-shot backup choices

    def _preflight(self, mount, listing=None, env_text="", code=0):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "vesselstack.env"
            env.write_text(env_text)
            status = {"path": "/media/backup-drive/backups", "mount": mount}
            output = (listing if isinstance(listing, str) else json.dumps(listing)) if listing is not None else ""
            with mock.patch.object(panel, "VESSEL_ENV", env), mock.patch.object(panel, "backup_status", return_value=status), \
                    mock.patch.object(panel, "command_stdout", return_value=(code, output)) as listed:
                return panel.backup_preflight(), listed

    def test_preflight_mounted_backup_is_the_default_and_lists_nothing(self) -> None:
        result, listed = self._preflight("mounted")
        self.assertTrue(result["default_ok"])
        self.assertEqual((result["drives"], result["refused"], result["mounted_elsewhere"]), ([], [], []))
        self.assertFalse(result["system_disk_once"]["offered"])
        listed.assert_not_called()

    def test_preflight_unmounted_backup_lists_root_side_drives(self) -> None:
        listing = {"fstab_mount": True,
                   "drives": [{"uuid": "AB12-CD34", "label": "BACKUP", "size": "58G", "fstype": "ext4", "device": "/dev/sda1"},
                              {"uuid": "../bad", "label": "X", "size": "1G", "fstype": "ext4", "device": "/dev/sdz1"}],
                   "refused": [{"device": "/dev/sdb1", "label": "CAMERA", "size": "29G", "fstype": "vfat", "reason": "vfat is not supported for backups"}],
                   "mounted_elsewhere": [{"device": "/dev/sde1", "label": "", "size": "16G", "fstype": "ext4", "mountpoint": "/media/pi/USBSTICK"}]}
        result, listed = self._preflight("not_mounted", listing)
        self.assertEqual(listed.call_args.args[0][1:], ["backup-drive", "list", "--json"])
        self.assertFalse(result["default_ok"])
        self.assertTrue(result["fstab_mount"])
        self.assertEqual([d["uuid"] for d in result["drives"]], ["AB12-CD34"])
        self.assertEqual(set(result["drives"][0]), {"uuid", "device", "label", "size", "fstype"})
        self.assertIn("not supported", result["refused"][0]["reason"])
        self.assertEqual(result["mounted_elsewhere"][0]["mountpoint"], "/media/pi/USBSTICK")
        self.assertEqual(result["system_disk_once"], {"offered": True, "reason": "", "target": panel.DEFAULT_BACKUP_DIR})
        self.assertEqual(result["skip"], {"offered": True})
        self.assertEqual(set(result), {"path", "mount", "default_ok", "fstab_mount", "drives", "refused", "mounted_elsewhere", "system_disk_once", "skip"})

    def test_preflight_degrades_to_no_drives_when_listing_fails(self) -> None:
        for kwargs in ({"code": 1, "listing": ""}, {"listing": "not json"}, {"listing": "[1, 2]"}):
            with self.subTest(kwargs=kwargs):
                result, _ = self._preflight("not_mounted", **kwargs)
                self.assertEqual((result["drives"], result["refused"], result["fstab_mount"]), ([], [], False))
                self.assertTrue(result["system_disk_once"]["offered"])

    def test_preflight_lock_turns_off_system_disk_and_skip(self) -> None:
        result, _ = self._preflight("not_mounted", {"drives": []}, env_text="VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=false\n")
        self.assertFalse(result["system_disk_once"]["offered"])
        self.assertIn("VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=false", result["system_disk_once"]["reason"])
        self.assertEqual(result["skip"], {"offered": False})

    def test_command_stdout_drops_stderr(self) -> None:
        with mock.patch.object(panel.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=" {} \n")) as run:
            self.assertEqual(panel.command_stdout(["ctl"]), (0, "{}"))
        self.assertEqual(run.call_args.kwargs["stderr"], panel.subprocess.DEVNULL)

    def _folder_fixture(self, directory):
        reviewed = Path(directory) / "releases"
        release = reviewed / "vesselstack-0.9.0"
        release.mkdir(parents=True)
        (release / "install.sh").write_text("#!/bin/sh\n"); (release / "VERSION").write_text("0.9.0\n")
        return reviewed, release

    def test_update_from_folder_default_argv_is_unchanged_and_choices_append_after_the_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reviewed, release = self._folder_fixture(directory)
            with mock.patch.object(panel, "REVIEWED_RELEASE_ROOT", reviewed), mock.patch.object(panel, "verify_release_path"), mock.patch.object(panel, "verify_release_tree"):
                default = panel.action_command("update", {"source_directory": str(release)})
                self.assertEqual(default, [str(panel.CTL), "update", str(release.resolve())])
                for mode, flag in (("system_disk_once", "--backup-choice=system-disk-once"), ("skip", "--backup-choice=skip")):
                    command = panel.action_command("update", {"source_directory": str(release)}, {"mode": mode})
                    self.assertEqual(command, [str(panel.CTL), "update", str(release.resolve()), flag])
                # Mount choices change nothing in the update command; the mount is a separate step.
                self.assertEqual(panel.action_command("update", {"source_directory": str(release)}, {"mode": "mount_fstab"}), default)
                # A browser-supplied field can never select a choice.
                forged = panel.action_command("update", {"source_directory": str(release), "_backup_choice": {"mode": "skip"}, "backup_choice": {"mode": "skip"}})
                self.assertEqual(forged, default)

    def test_folder_update_mounts_in_a_transient_unit_outside_the_panel_sandbox(self) -> None:
        self.assertEqual(panel.folder_update_steps(None), [])
        self.assertEqual(panel.folder_update_steps({"mode": "skip", "confirm": "install-without-backup"}), [])
        fstab = panel.folder_update_steps({"mode": "mount_fstab"})[0]
        self.assertEqual(fstab[:5], ["systemd-run", "--wait", "--pipe", "--collect", "--quiet"])
        self.assertIn("--property=RuntimeMaxSec=300", fstab)
        self.assertEqual(fstab[-4:], [str(panel.CTL), "backup-drive", "mount", "--fstab"])
        drive = panel.folder_update_steps({"mode": "mount_drive", "uuid": "AB12-CD34"})[0]
        self.assertEqual(drive[-5:], [str(panel.CTL), "backup-drive", "mount", "--uuid", "AB12-CD34"])

    def test_process_environment_lock_applies_to_choice_and_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "vesselstack.env"
            env.write_text("")
            skip = {"backup": {"mode": "skip", "confirm": "install-without-backup"}}
            with mock.patch.object(panel, "VESSEL_ENV", env), mock.patch.dict(os.environ, {"VESSELSTACK_BACKUP_ALLOW_UNMOUNTED": "false"}):
                with self.assertRaisesRegex(ValueError, "turned off"):
                    panel.request_backup_choice(skip)
                result, _ = self._preflight("not_mounted", {"drives": []})
                self.assertFalse(result["skip"]["offered"])
                self.assertFalse(result["system_disk_once"]["offered"])

    def test_request_backup_choice_validates_locks_and_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "vesselstack.env"
            env.write_text("")
            skip = {"backup": {"mode": "skip", "confirm": "install-without-backup"}}
            with mock.patch.object(panel, "VESSEL_ENV", env), mock.patch.object(panel, "effective_backup_choice", side_effect=lambda c, _: (c, "")):
                self.assertIsNone(panel.request_backup_choice({}))
                self.assertIsNone(panel.request_backup_choice({"backup": {"mode": "require_mounted"}}))
                self.assertEqual(panel.request_backup_choice(skip)["mode"], "skip")
                with self.assertRaises(ValueError):
                    panel.request_backup_choice({"backup": {"mode": "skip"}})
                with self.assertRaises(ValueError):
                    panel.request_backup_choice({"backup": {"mode": "mount_drive", "uuid": "/dev/sda1"}})
                env.write_text("VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=false\n")
                with self.assertRaisesRegex(ValueError, "turned off"):
                    panel.request_backup_choice(skip)
                env.write_text("")
                with mock.patch.object(panel, "ctl_supports", return_value=False):
                    with self.assertRaisesRegex(ValueError, "older than this Control Panel"):
                        panel.request_backup_choice({"backup": {"mode": "mount_fstab"}}, folder=True)
                    self.assertEqual(panel.request_backup_choice(skip)["mode"], "skip")  # the release worker checks for itself
            with mock.patch.object(panel, "VESSEL_ENV", env), mock.patch.object(panel.os.path, "ismount", return_value=True):
                env.write_text("VESSELSTACK_BACKUP=/media/backup-drive/backups\n")
                self.assertIsNone(panel.request_backup_choice({"backup": {"mode": "mount_fstab"}}))

    def test_operation_store_runs_steps_first_and_stops_on_failure(self) -> None:
        ran: list = []

        class FakeProcess:
            def __init__(self, code):
                self.stdout = iter(["line\n"]); self.code = code
            def wait(self):
                return self.code

        def popen(command, **kwargs):
            ran.append(command)
            return FakeProcess(1 if command[0] == "mount-step" else 0)

        for failing, expected in ((False, [["ok-step"], ["main"]]), (True, [["mount-step"]])):
            ran.clear()
            store = panel.OperationStore()
            operation = {"label": "update", "state": "running", "output": panel.deque(maxlen=10), "exit_code": None, "finished_at": None}
            with mock.patch.object(panel.subprocess, "Popen", side_effect=popen), mock.patch.object(panel, "release_lifecycle_slot"):
                store._run(operation, ["main"], None, [["mount-step"] if failing else ["ok-step"]])
            self.assertEqual(ran, expected)
            self.assertEqual(operation["state"], "failed" if failing else "succeeded")


class DisplaysApiBase(unittest.TestCase):
    """Shared server and subprocess fakes for the Displays API tests (subprocess is mocked)."""

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
        self.kwargs: list[dict] = []
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
        self.kwargs.append(kwargs)
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

    def post(self, path, body=None, token="default", headers=None, raw=None, content_type="application/json"):
        data = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
        all_headers = {"Content-Type": content_type} if content_type else {}
        if token is not None:
            all_headers["X-VesselStack-Token"] = self.TOKEN if token == "default" else token
        all_headers.update(headers or {})
        request = urllib.request.Request(f"http://127.0.0.1:{self.server.server_address[1]}{path}", data=data, headers=all_headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()


class DisplaysApiTests(DisplaysApiBase):
    """GET /api/displays*: fixed argv, auth, cache, error mapping and no secrets (subprocess is mocked)."""

    def test_every_endpoint_needs_the_token(self) -> None:
        for path in ("/api/displays", "/api/displays?id=helm", "/api/displays/themes", "/api/displays/catalog",
                     "/api/displays/catalog?live=1", "/api/displays/serial-ports", "/api/displays/history"):
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



REV = "a" * 64


class DisplaysWriteApiTests(DisplaysApiBase):
    """POST /api/displays/{save,restore,deploy,rollback} and GET history: auth, origin, validation, exact argv."""

    def setUp(self) -> None:
        super().setUp()
        self.slot_calls: list[str] = []
        self.busy = False

        def take():
            if self.busy:
                raise ValueError("Another operation is already running")
            self.slot_calls.append("take")
            return 99

        def release(fd):
            self.slot_calls.append("release")
        for patch in (mock.patch.object(panel, "take_lifecycle_slot", side_effect=take),
                      mock.patch.object(panel, "release_lifecycle_slot", side_effect=release)):
            patch.start()
            self.addCleanup(patch.stop)
        self.operations = mock.Mock()
        self.operations.start.return_value = {"state": "running", "id": 7}
        patch = mock.patch.object(panel, "OPERATIONS", self.operations)
        patch.start()
        self.addCleanup(patch.stop)

    SAVE = {"id": "helm", "revision": REV, "pages": [{"title": "Nav", "layout": "grid-2x2", "items": ["sog"]}], "theme": "ocean"}
    ENDPOINTS = {"/api/displays/save": SAVE, "/api/displays/restore": {"stamp": "20260101T120000Z", "revision": REV},
                 "/api/displays/deploy": {"id": "helm", "method": "ota", "target": "192.0.2.50"},
                 "/api/displays/rollback": {"id": "helm", "method": "ota", "target": "192.0.2.50"}}

    def test_history_is_a_fixed_get(self) -> None:
        self.answer(["display", "history", "--json"], {"schema": 1, "backups": []})
        self.assertEqual(self.get("/api/displays/history")[0], 200)
        self.assertEqual(self.calls, [[self.CTL, "display", "history", "--json"]])

    def test_every_post_needs_the_token_and_a_matching_origin(self) -> None:
        for path, body in self.ENDPOINTS.items():
            self.assertEqual(self.post(path, body, token=None)[0], 401, path)
            self.assertEqual(self.post(path, body, token="wrong")[0], 401, path)
            self.assertEqual(self.post(path, body, headers={"Origin": "http://evil.example"})[0], 403, path)
            self.assertEqual(self.post(path, body, headers={"Origin": "http://127.0.0.1:1"})[0], 403, path)
        self.assertEqual(self.calls, [])
        self.operations.start.assert_not_called()

    def test_content_type_size_and_shape(self) -> None:
        for path in self.ENDPOINTS:
            self.assertEqual(self.post(path, raw=b"{}", content_type="text/plain")[0], 400, path)
            self.assertEqual(self.post(path, raw=b"{}", content_type=None)[0], 400, path)
            self.assertEqual(self.post(path, raw=b"[1]")[0], 400, path)
            self.assertEqual(self.post(path, raw=b"not json")[0], 400, path)
            import http.client
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
            connection.putrequest("POST", path)  # declare a body over 1 MiB; the server refuses before reading it
            for name, value in (("Content-Type", "application/json"), ("X-VesselStack-Token", self.TOKEN), ("Content-Length", str(1024 * 1024 + 1))):
                connection.putheader(name, value)
            connection.endheaders()
            self.assertEqual(connection.getresponse().status, 400, path)
            connection.close()
        self.assertEqual(self.post("/api/displays/unknown", {})[0], 404)
        self.assertEqual(self.calls, [])

    def test_save_runs_set_pages_with_the_body_on_stdin_only(self) -> None:
        reply = {"ok": True, "changed": True, "revision": "b" * 64, "rebuild_needed": ["helm"]}
        self.answer(["display", "set-pages", "helm", "--if-revision", REV, "--stdin"], reply)
        status, body = self.post("/api/displays/save", {**self.SAVE, "new_instruments": {"x": {"path": "a.b"}}})
        self.assertEqual((status, json.loads(body)), (200, reply))
        self.assertEqual(self.calls, [[self.CTL, "display", "set-pages", "helm", "--if-revision", REV, "--stdin"]])
        sent = json.loads(self.kwargs[0]["input"])
        self.assertEqual(set(sent), {"pages", "theme", "new_instruments"}, "id and revision are not part of the request body")
        self.assertEqual(sent["pages"], self.SAVE["pages"])
        self.assertEqual(self.kwargs[0]["timeout"], 40)
        self.assertNotIn("sog", " ".join(self.calls[0]), "page content never reaches the argument list")
        self.assertEqual(self.slot_calls, ["take", "release"], "the global operation slot is held while saving")

    def test_save_rejects_bad_input_before_running_anything(self) -> None:
        bad = [{**self.SAVE, "id": "--evil"}, {**self.SAVE, "id": "A"}, {**self.SAVE, "id": 7}, {k: v for k, v in self.SAVE.items() if k != "id"},
               {**self.SAVE, "revision": "abc"}, {k: v for k, v in self.SAVE.items() if k != "revision"}, {**self.SAVE, "revision": REV + "0"},
               {**self.SAVE, "pages": "x"}, {k: v for k, v in self.SAVE.items() if k != "pages"}, {**self.SAVE, "argv": ["--force"]}]
        for body in bad:
            self.assertEqual(self.post("/api/displays/save", body)[0], 400, body)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.slot_calls, [])

    def test_save_is_409_while_an_operation_runs(self) -> None:
        self.busy = True
        status, body = self.post("/api/displays/save", self.SAVE)
        self.assertEqual(status, 409)
        self.assertIn("running", json.loads(body)["error"])
        self.assertEqual(self.calls, [])

    def test_save_error_mapping_and_cache_invalidation(self) -> None:
        args = ["display", "set-pages", "helm", "--if-revision", REV, "--stdin"]
        self.answer(["display", "catalog", "--json"], {"schema": 1, "rows": []})
        self.get("/api/displays/catalog")
        self.get("/api/displays/catalog")
        self.assertEqual(len(self.calls), 1)
        cases = [(2, 400, ["page 1: too many items", "second problem"]), (3, 409, ["the configuration changed since it was read"]),
                 (4, 409, ["a build or flash for helm is running"]), (1, 502, ["cannot read"])]
        for code, status, errors in cases:
            self.answer(args, {"ok": False, "code": code, "errors": errors}, code=code)
            got, body = self.post("/api/displays/save", self.SAVE)
            self.assertEqual(got, status, code)
            data = json.loads(body)
            self.assertEqual(data["errors"], errors)
            self.assertEqual(data["error"], errors[0])
        self.assertEqual(self.slot_calls, ["take", "release"] * 4, "the slot is released after a failure too")
        self.calls.clear()
        self.get("/api/displays/catalog")
        self.assertEqual(len(self.calls), 1, "a save forgets the cached catalog")

    def test_restore_argv_and_validation(self) -> None:
        self.answer(["display", "restore-config", "20260101T120000Z", "--if-revision", REV], {"ok": True, "revision": "c" * 64})
        status, body = self.post("/api/displays/restore", {"stamp": "20260101T120000Z", "revision": REV})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.calls, [[self.CTL, "display", "restore-config", "20260101T120000Z", "--if-revision", REV]])
        self.calls.clear()
        self.answer(["display", "catalog", "--json"], {"schema": 1, "rows": []})
        self.get("/api/displays/catalog")
        self.get("/api/displays/catalog")
        self.assertEqual(len(self.calls), 1)
        self.post("/api/displays/restore", {"stamp": "20260101T120000Z", "revision": REV})
        self.calls.clear()
        self.get("/api/displays/catalog")
        self.assertEqual(len(self.calls), 1, "a restore forgets the cached catalog")
        self.calls.clear()
        for body in ({"stamp": "--help", "revision": REV}, {"stamp": "20260101T120000Z"}, {"stamp": "../x", "revision": REV},
                     {"stamp": "20260101T120000Z", "revision": "nope"}, {"stamp": 5, "revision": REV}):
            self.assertEqual(self.post("/api/displays/restore", body)[0], 400, body)
        self.assertEqual(self.calls, [])
        self.busy = True
        self.assertEqual(self.post("/api/displays/restore", {"stamp": "20260101T120000Z", "revision": REV})[0], 409)

    def test_deploy_ota_and_serial_argv(self) -> None:
        status, body = self.post("/api/displays/deploy", {"id": "helm", "method": "ota", "target": "192.0.2.50", "confirm_new_target": True})
        self.assertEqual((status, json.loads(body)["state"]), (202, "running"))
        self.operations.start.assert_called_with("display deploy helm", [self.CTL, "display", "deploy", "helm", "--ota", "192.0.2.50", "--progress"])
        self.assertEqual(self.calls, [], "no serial-ports lookup for Wi-Fi")
        self.answer(["display", "serial-ports", "--json"], {"schema": 1, "ports": [{"path": "/dev/serial/by-id/usb-X-if00", "device": "/dev/ttyACM0"}]})
        for target in ("/dev/serial/by-id/usb-X-if00", "/dev/ttyACM0"):
            self.assertEqual(self.post("/api/displays/deploy", {"id": "helm", "method": "serial", "target": target})[0], 202, target)
            self.operations.start.assert_called_with("display deploy helm", [self.CTL, "display", "deploy", "helm", "--serial", target, "--progress"])

    def test_deploy_target_validation(self) -> None:
        self.answer(["display", "serial-ports", "--json"], {"schema": 1, "ports": [{"path": "/dev/serial/by-id/usb-X-if00", "device": "/dev/ttyACM0"}]})
        for target in ("-x", "--force", "a b", " 192.0.2.50", "192.0.2.50 ", "http://192.0.2.50", "192.0.2.50:80", "a/b", "../x", "..", "x..y", "a_b",
                       "fe80::1%eth0", "", None, 5, ["a"], "bootsel", "MQTT", "OTA", "Serial", "fe80::1%a\nb", "fe80::1%$(id)", "COM3", "a" * 300, "host\nname", "-host.local", "12345"):
            self.assertEqual(self.post("/api/displays/deploy", {"id": "helm", "method": "ota", "target": target, "confirm_new_target": True})[0], 400, repr(target))
        for target in ("/dev/ttyUSB0", "/dev/serial/by-id/../../sda", "/dev/serial/by-id/usb-X-if00/", "-x", "", None, "/dev/ttyACM0 --force"):
            self.assertEqual(self.post("/api/displays/deploy", {"id": "helm", "method": "serial", "target": target})[0], 400, repr(target))
        for body in ({"id": "--evil", "method": "ota", "target": "192.0.2.50", "confirm_new_target": True}, {"id": "helm", "method": "telnet", "target": "x"},
                     {"id": "helm", "target": "192.0.2.50"}, {"method": "ota", "target": "192.0.2.50", "confirm_new_target": True}):
            self.assertEqual(self.post("/api/displays/deploy", body)[0], 400, body)
        self.operations.start.assert_not_called()
        for target in ("192.0.2.50", "2001:db8::1", "helm.local", "boat-helm", "helm.boat.example."):
            self.assertEqual(self.post("/api/displays/deploy", {"id": "helm", "method": "ota", "target": target, "confirm_new_target": True})[0], 202, target)

    def test_host_rules_match_the_command_line_tool(self) -> None:
        import firmware
        import render
        for text in ("192.0.2.50", "helm.local", "-x", "a b", "http://x", "..", "x..y", "bootsel", "ota", "SERIAL", "fe80::1%eth0", "COM3", "12345", "a_b", "2001:db8::1",
                     "", "a" * 300, "host-", "-host", "ok-host.example.com", "1.2.3.4.5", "[::1]", "a.b.9"):
            try:
                firmware.validate_host(text)
                cli = True
            except render.RenderError:
                cli = False
            try:
                panel.valid_ota_host(text)
                web = True
            except ValueError:
                web = False
            self.assertLessEqual(web, cli, f"the panel must never accept what the tool refuses: {text!r}")
            if cli:
                self.assertTrue(web, text)

    def test_deploy_is_409_while_an_operation_runs(self) -> None:
        self.operations.start.side_effect = ValueError("Another operation is already running")
        status, body = self.post("/api/displays/deploy", {"id": "helm", "method": "ota", "target": "192.0.2.50", "confirm_new_target": True})
        self.assertEqual(status, 409)
        self.assertIn("running", json.loads(body)["error"])

    def test_rollback_runs_flash_previous_without_force(self) -> None:
        status, _ = self.post("/api/displays/rollback", {"id": "helm", "method": "ota", "target": "helm.local", "confirm_new_target": True})
        self.assertEqual(status, 202)
        label, argv = self.operations.start.call_args[0]
        self.assertEqual(label, "display rollback helm")
        self.assertEqual(argv, [self.CTL, "display", "flash", "helm", "--previous", "--ota", "helm.local", "--progress"])
        self.assertNotIn("--force", argv)
        self.assertEqual(self.post("/api/displays/rollback", {"id": "helm", "method": "ota", "target": "-x", "confirm_new_target": True})[0], 400)

    def test_ota_target_needs_confirmation_unless_it_is_the_last_successful_one(self) -> None:
        show = ["display", "show", "helm", "--json"]
        for route in ("deploy", "rollback"):
            path = f"/api/displays/{route}"
            self.operations.start.reset_mock()
            self.answer(show, {"readiness": {"last_flash": {"ota_target": "192.0.2.50"}}})
            self.assertEqual(self.post(path, {"id": "helm", "method": "ota", "target": "192.0.2.50"})[0], 202, route)
            self.assertEqual(self.post(path, {"id": "helm", "method": "ota", "target": "192.0.2.77"})[0], 400, route)
            self.assertIn("confirm", json.loads(self.post(path, {"id": "helm", "method": "ota", "target": "192.0.2.77"})[1])["error"])
            self.assertEqual(self.post(path, {"id": "helm", "method": "ota", "target": "192.0.2.77", "confirm_new_target": "yes"})[0], 400)
            self.assertEqual(self.operations.start.call_count, 1, "only the matching target started")
            self.assertEqual(self.post(path, {"id": "helm", "method": "ota", "target": "192.0.2.77", "confirm_new_target": True})[0], 202, route)
            self.answer(show, {"readiness": {"last_flash": None}})  # nothing recorded: every Wi-Fi target is new
            self.assertEqual(self.post(path, {"id": "helm", "method": "ota", "target": "192.0.2.50"})[0], 400, route)

    def test_rollback_is_409_while_an_operation_runs(self) -> None:
        self.operations.start.side_effect = ValueError("Another operation is already running")
        status, body = self.post("/api/displays/rollback", {"id": "helm", "method": "ota", "target": "192.0.2.50", "confirm_new_target": True})
        self.assertEqual(status, 409)
        self.assertIn("running", json.loads(body)["error"])

    def test_operation_output_cannot_leak_the_command_arguments(self) -> None:
        store = panel.OperationStore()
        with mock.patch.object(panel, "take_lifecycle_slot", return_value=1), mock.patch.object(panel, "release_lifecycle_slot"), \
                mock.patch.object(panel.threading, "Thread"):
            started = store.start("display deploy helm", [self.CTL, "display", "deploy", "helm", "--ota", "192.0.2.50"])
        self.assertEqual(started["command"], [self.CTL, "[arguments hidden]"])

    def test_deploy_failure_summary_extracts_the_remedy(self) -> None:
        operation = {"state": "failed", "label": "display deploy helm", "output": [
            "STAGE: render", "STAGE: upload", "ERROR Timed out", "error: the upload failed (exit status 1).",
            "To fix: Check that the display is powered on."]}
        summary = panel.summarize_operation(operation)
        self.assertIn("upload failed", summary["reason"])
        self.assertEqual(summary["fix"], "Check that the display is powered on.")


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

class SignInApiTests(DisplaysApiBase):
    PASSPHRASE = "correct horse battery"

    def setUp(self) -> None:
        super().setUp()
        root = tempfile.mkdtemp(prefix="vesselstack-signin-")
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        for patch in (mock.patch.object(panel, "ROOT", Path(root)), mock.patch.object(panel, "LOGIN_THROTTLE", panel.vsauth.LoginThrottle())):
            patch.start()
            self.addCleanup(patch.stop)
        self.root = Path(root)

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body if body is not None else {}).encode() if method == "POST" else None
        all_headers = {"Content-Type": "application/json"} if data is not None else {}
        if path in {"/api/auth/login", "/api/auth/passphrase"} and method == "POST":
            all_headers.update(self.origin)  # these two endpoints require an Origin; pass {"Origin": None} to omit it
        all_headers.update(headers or {})
        all_headers = {k: v for k, v in all_headers.items() if v is not None}
        request = urllib.request.Request(f"http://127.0.0.1:{self.server.server_address[1]}{path}", data=data, headers=all_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode() or "{}"), dict(response.headers)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode() or "{}"), dict(exc.headers)
            finally:
                exc.close()

    @property
    def origin(self):
        return {"Origin": f"http://127.0.0.1:{self.server.server_address[1]}"}

    def tokenh(self):
        return {"X-VesselStack-Token": self.TOKEN}

    def set_passphrase(self):
        status, payload, _ = self.call("POST", "/api/auth/passphrase", {"new": self.PASSPHRASE}, self.tokenh())
        self.assertEqual(status, 200, payload)

    def login(self, remember=True, passphrase=None, label="Safari on Mac"):
        return self.call("POST", "/api/auth/login", {"passphrase": passphrase or self.PASSPHRASE, "remember": remember, "label": label})

    def device(self, token):
        return {"X-VesselStack-Device": token, **self.origin}

    # -- pairing links ------------------------------------------------------
    def make_pairing(self, target="panel", admin=False, headers=None):
        body = {"for": target, "admin": admin}
        return self.call("POST", "/api/auth/pairing", body, headers or {**self.tokenh(), **self.origin})

    def redeem(self, code, **extra):
        return self.call("POST", "/api/auth/pair", {"code": code, **extra}, self.origin)

    def test_pairing_matrix_requires_auth_and_origin(self) -> None:
        self.assertEqual(self.call("POST", "/api/auth/pairing", {"for": "panel"}, self.origin)[0], 401)
        self.assertEqual(self.call("POST", "/api/auth/pairing", {"for": "panel"}, self.tokenh())[0], 403, "Origin is mandatory")
        self.assertEqual(self.call("POST", "/api/auth/pair", {"code": "ABCDE-FGHJK"})[0], 403, "redeeming needs an Origin too")
        self.assertEqual(self.call("POST", "/api/auth/pair", {"code": "ABCDE-FGHJK"}, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.make_pairing("nope")[0], 400)
        self.assertEqual(self.make_pairing("panel", admin=True)[0], 400, "admin pairing is Boat Chat only")

    def test_panel_pairing_round_trip_is_single_use(self) -> None:
        status, link, _ = self.make_pairing()
        self.assertEqual(status, 200, link)
        self.assertRegex(link["code"], r"^[0-9A-Z]{5}-[0-9A-Z]{5}$")
        self.assertTrue(link["tunnel"])
        self.assertIn("#pair=" + link["code"].replace("-", ""), link["url"])
        size = len(link["modules"])
        self.assertTrue(all(len(row) == size for row in link["modules"]))
        self.assertNotIn(link["code"].replace("-", ""), (self.root / "config" / "control-panel-auth.json").read_text())
        status, paired, headers = self.redeem(link["code"].lower(), label="Pixel", remember=False)
        self.assertEqual(status, 200, paired)
        self.assertEqual(set(paired), {"device_token", "device_id", "expires_at", "remember"})
        self.assertTrue(paired["remember"], "remember is ignored; pair links always remember")
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(self.call("GET", "/api/config", headers=self.device(paired["device_token"]))[0], 200)
        self.assertEqual(self.redeem(link["code"])[0], 401, "single use")

    def test_bad_pairing_codes_are_throttled(self) -> None:
        results = [self.redeem("ZZZZZ-ZZZZZ")[0] for _ in range(8)]
        self.assertEqual(results.count(401), panel.vsauth.LoginThrottle.IP_FREE_FAILURES)
        self.assertEqual(results.count(429), 8 - panel.vsauth.LoginThrottle.IP_FREE_FAILURES)

    def test_chat_pairing_is_written_to_the_chat_store_with_role(self) -> None:
        data = self.root / "data"
        (data / "boat-chat").mkdir(parents=True)
        env = self.root / "vesselstack.env"
        env.write_text(f'VESSELSTACK_DATA="{data}"\n')
        chat_env = self.root / "boat-chat.env"
        chat_env.write_text('BOAT_CHAT_HOST="198.51.100.9"\nBOAT_CHAT_PORT="8765"\n')
        with mock.patch.object(panel, "VESSEL_ENV", env), mock.patch.object(panel, "CHAT_ENV", chat_env):
            self.assertEqual(self.make_pairing("chat", admin=True)[0], 400, "admin pairing needs BOAT_CHAT_SETTINGS_TOKEN")
        self.assertFalse((data / "boat-chat" / "auth.json").exists())
        chat_env.write_text('BOAT_CHAT_HOST="198.51.100.9"\nBOAT_CHAT_PORT="8765"\nBOAT_CHAT_SETTINGS_TOKEN="admin-code"\n')
        with mock.patch.object(panel, "VESSEL_ENV", env), mock.patch.object(panel, "CHAT_ENV", chat_env):
            status, link, _ = self.make_pairing("chat", admin=True)
        self.assertEqual(status, 200, link)
        self.assertTrue(link["url"].startswith("http://198.51.100.9:8765/#pair="))
        self.assertFalse(link["tunnel"])
        stored = json.loads((data / "boat-chat" / "auth.json").read_text())
        self.assertEqual([p["role"] for p in stored["pairings"]], ["admin"])
        self.assertNotIn(link["code"].replace("-", ""), json.dumps(stored))
        for _ in range(2):
            with mock.patch.object(panel, "VESSEL_ENV", env), mock.patch.object(panel, "CHAT_ENV", chat_env):
                self.assertEqual(self.make_pairing("chat")[0], 200)
            pass
        with mock.patch.object(panel, "VESSEL_ENV", env), mock.patch.object(panel, "CHAT_ENV", chat_env):
            status, body, _ = self.make_pairing("chat")
        self.assertEqual(status, 400, "at most three outstanding")
        self.assertIn("already waiting", body["error"])

    def chat_dirs(self):
        data = self.root / "data"
        (data / "boat-chat").mkdir(parents=True, exist_ok=True)
        env = self.root / "vesselstack.env"
        env.write_text(f'VESSELSTACK_DATA="{data}"\n')
        chat_env = self.root / "boat-chat.env"
        return data, env, chat_env

    def test_admin_pairing_binds_to_the_effective_settings_token(self) -> None:
        data, env, chat_env = self.chat_dirs()
        chat_env.write_text('BOAT_CHAT_HOST="198.51.100.9"\nBOAT_CHAT_SETTINGS_TOKEN="env-code"\n')
        override = data / "boat-chat" / "boat-chat.env"
        bindings = []
        for text in ("", "BOAT_CHAT_SETTINGS_TOKEN='page-code'\n"):
            override.write_text(text)
            with mock.patch.object(panel, "VESSEL_ENV", env), mock.patch.object(panel, "CHAT_ENV", chat_env):
                self.assertEqual(self.make_pairing("chat", admin=True)[0], 200)
            stored = json.loads((data / "boat-chat" / "auth.json").read_text())
            bindings.append((stored["pairings"][-1]["binding"], stored["binding_key"]))
        self.assertEqual(bindings[0][0], panel.vsauth.admin_binding(bindings[0][1], "env-code"), "base file when no override")
        self.assertEqual(bindings[1][0], panel.vsauth.admin_binding(bindings[1][1], "page-code"), "Settings-page override wins, as in Boat Chat")
        self.assertEqual(bindings[0][1], bindings[1][1], "one key per store")

    def test_auth_context_is_evaluated_once_per_request(self) -> None:
        self.set_passphrase()
        token = self.login()[1]["device_token"]
        store = panel.auth_store()
        handler = object.__new__(panel.ControlPanelHandler)
        handler.headers = {"X-VesselStack-Device": token}
        handler.send_json = lambda *a, **k: self.fail("unexpected rejection")
        self.assertTrue(handler.require_auth())
        device_id = handler.current_device_id()
        self.assertTrue(store.revoke(device_id), "revoked while the request is still being handled")
        self.assertEqual(handler.auth_context()["kind"], "device")
        self.assertEqual(handler.current_device_id(), device_id, "the same identity for the whole request")
        self.assertFalse(handler.authenticated(), "a fresh evaluation does see the revocation")
        fresh = object.__new__(panel.ControlPanelHandler)
        fresh.headers = {"X-VesselStack-Device": token}
        sent = []
        fresh.send_json = lambda status, payload, headers=None: sent.append(status)
        self.assertFalse(fresh.require_auth())
        self.assertEqual(sent, [401])

    def test_device_revoked_mid_request_never_crashes_the_handler(self) -> None:
        self.set_passphrase()
        token = self.login()[1]["device_token"]
        real = panel.vsauth.AuthStore.list_devices

        def revoking(store, current_id=None):
            store.revoke_all()
            return real(store, current_id)
        with mock.patch.object(panel.vsauth.AuthStore, "list_devices", revoking):
            status, body, _ = self.call("GET", "/api/auth/devices", {}, self.device(token))
        self.assertEqual((status, body["via"]), (200, "device"))
        self.assertEqual(self.call("GET", "/api/auth/devices", {}, self.device(token))[0], 401, "the next request is refused")
        # same on POST: the Origin gate and the handler both use the context fixed at authentication
        token = self.login()[1]["device_token"]
        original = panel.ControlPanelHandler.read_payload

        def revoking_payload(handler):
            panel.auth_store().revoke_all()
            return original(handler)
        with mock.patch.object(panel.ControlPanelHandler, "read_payload", revoking_payload):
            status, body, _ = self.call("POST", "/api/auth/devices/revoke", {"id": "all"}, self.device(token))
        self.assertEqual(status, 200, body)

    def test_corrupt_chat_store_degrades_in_the_panel(self) -> None:
        data, env, _ = self.chat_dirs()
        (data / "boat-chat" / "auth.json").write_text(json.dumps({"schema": 1, "devices": 5, "pairings": [{"created": []}]}))
        with mock.patch.object(panel, "VESSEL_ENV", env):
            self.assertIsNone(panel.chat_passphrase_hash())
            self.set_passphrase()  # the "must differ from Boat Chat" rule just does not apply
            self.assertEqual(self.login()[0], 200)
            self.assertEqual(self.make_pairing("chat")[0], 500, "reported, not crashed, and the unreadable store is left alone")
        self.assertEqual(json.loads((data / "boat-chat" / "auth.json").read_text())["devices"], 5)

    def test_success_does_not_wipe_address_backoff_over_http(self) -> None:
        self.set_passphrase()
        for _ in range(3):
            self.assertEqual(self.login(passphrase="wrong passphrase!")[0], 401)
        self.assertEqual(self.login()[0], 200)
        self.assertEqual(panel.LOGIN_THROTTLE._ips["127.0.0.1"]["count"], 3, "the earlier failures still count after a success")
        self.assertEqual(self.login(passphrase="wrong passphrase!")[0], 401)
        self.assertEqual(self.login(passphrase="wrong passphrase!")[0], 401)
        self.assertEqual(self.login()[0], 429, "5 failures in a row still trip the backoff even with a success between")

    def test_status_leaks_only_whether_a_passphrase_is_set(self) -> None:
        status, payload, _ = self.call("GET", "/api/auth/status")
        self.assertEqual((status, payload), (200, {"passphrase_set": False}))
        self.set_passphrase()
        self.assertEqual(self.call("GET", "/api/auth/status")[1], {"passphrase_set": True})

    def test_login_is_off_until_a_passphrase_is_set(self) -> None:
        status, payload, _ = self.login()
        self.assertEqual(status, 409)
        self.assertIn("off until a passphrase is set", payload["error"])

    def test_login_returns_a_device_token_that_works_for_existing_apis(self) -> None:
        self.set_passphrase()
        status, payload, headers = self.login(remember=True)
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"device_token", "device_id", "expires_at", "remember"})
        self.assertNotIn("Set-Cookie", headers)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        token = payload["device_token"]
        self.assertEqual(self.call("GET", "/api/config", headers=self.device(token))[0], 200)
        self.assertEqual(self.call("GET", "/api/config")[0], 401)
        self.assertEqual(self.call("GET", "/api/config", headers={"X-VesselStack-Device": "nope" * 10})[0], 401)
        self.assertEqual(self.call("GET", "/api/config", headers=self.tokenh())[0], 200, "long token unchanged")
        raw = (self.root / "config/control-panel-auth.json").read_text()
        self.assertNotIn(token, raw)

    def test_non_remembered_login_lasts_twelve_hours(self) -> None:
        self.set_passphrase()
        _, short, _ = self.login(remember=False)
        _, long, _ = self.login(remember=True)
        now = time.time()
        self.assertAlmostEqual(short["expires_at"] - now, 12 * 3600, delta=60)
        self.assertAlmostEqual(long["expires_at"] - now, 30 * 86400, delta=60)

    def test_wrong_passphrase_is_a_generic_401_and_bad_payloads_are_400(self) -> None:
        self.set_passphrase()
        status, payload, _ = self.login(passphrase="not the passphrase")
        self.assertEqual(status, 401)
        self.assertEqual(self.call("POST", "/api/auth/login", {"passphrase": "x"})[0], 401)
        self.assertEqual(self.call("POST", "/api/auth/login", {"passphrase": 5, "remember": True})[0], 400)
        self.assertEqual(self.call("POST", "/api/auth/login", {"passphrase": self.PASSPHRASE, "remember": "yes"})[0], 400)

    def test_lockout_returns_429_but_the_token_still_works(self) -> None:
        self.set_passphrase()
        for _ in range(5):
            self.assertEqual(self.login(passphrase="wrong wrong wrong")[0], 401)
        status, payload, headers = self.login()
        self.assertEqual(status, 429)
        self.assertGreater(payload["retry_after"], 0)
        self.assertEqual(headers["Retry-After"], str(payload["retry_after"]))
        self.assertEqual(self.call("GET", "/api/config", headers=self.tokenh())[0], 200)

    def test_login_and_passphrase_require_an_origin_whatever_the_credential(self) -> None:
        self.set_passphrase()
        none = {"Origin": None}
        self.assertEqual(self.call("POST", "/api/auth/login", {"passphrase": self.PASSPHRASE, "remember": True}, none)[0], 403)
        self.assertEqual(self.call("POST", "/api/auth/passphrase", {"new": "another long passphrase"}, {**self.tokenh(), **none})[0], 403)
        self.assertEqual(self.call("POST", "/api/auth/passphrase", {"new": "another long passphrase"}, self.tokenh())[0], 200)

    def test_concurrent_wrong_attempts_are_limited_before_any_is_evaluated(self) -> None:
        self.set_passphrase()
        evaluated = []
        real = panel.vsauth.AuthStore.check_passphrase

        def counting(store, passphrase):
            evaluated.append(passphrase)
            time.sleep(0.05)
            return real(store, passphrase)
        results = []
        with mock.patch.object(panel.vsauth.AuthStore, "check_passphrase", counting):
            threads = [threading.Thread(target=lambda: results.append(self.login(passphrase="wrong wrong wrong")[0])) for _ in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(sorted(results).count(401), panel.vsauth.LoginThrottle.IP_FREE_FAILURES)
        self.assertEqual(sorted(results).count(429), 12 - panel.vsauth.LoginThrottle.IP_FREE_FAILURES)
        self.assertEqual(len(evaluated), panel.vsauth.LoginThrottle.IP_FREE_FAILURES)

    def test_global_lockout_applies_to_other_addresses(self) -> None:
        self.set_passphrase()
        throttle = panel.LOGIN_THROTTLE
        for index in range(21):
            throttle.failure(f"192.0.2.{index + 10}")
        self.assertEqual(self.login()[0], 429)

    def test_device_posts_need_a_matching_origin_but_token_posts_do_not(self) -> None:
        self.set_passphrase()
        token = self.login()[1]["device_token"]
        body = {"id": "all"}
        self.assertEqual(self.call("POST", "/api/auth/devices/revoke", body, {"X-VesselStack-Device": token})[0], 403)
        self.assertEqual(self.call("POST", "/api/auth/devices/revoke", body, {"X-VesselStack-Device": token, "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.call("POST", "/api/auth/devices/revoke", body, self.tokenh())[0], 200)
        self.assertEqual(self.call("GET", "/api/config", headers=self.device(token))[0], 401, "revoked")

    def test_devices_list_marks_current_and_hides_hashes(self) -> None:
        self.set_passphrase()
        first = self.login(label="Phone")[1]
        second = self.login(label="Laptop")[1]
        status, payload, _ = self.call("GET", "/api/auth/devices", headers=self.device(first["device_token"]))
        self.assertEqual(status, 200)
        self.assertEqual({d["label"]: d["current"] for d in payload["devices"]}, {"Phone": True, "Laptop": False})
        for entry in payload["devices"]:
            self.assertEqual(set(entry), {"id", "label", "created", "last_seen", "expires", "current"})
        self.assertEqual(self.call("GET", "/api/auth/devices")[0], 401)
        self.assertNotIn("token_sha256", json.dumps(payload))
        self.assertEqual(self.call("POST", "/api/auth/devices/revoke", {"id": second["device_id"]}, self.device(first["device_token"]))[0], 200)
        self.assertEqual(len(self.call("GET", "/api/auth/devices", headers=self.tokenh())[1]["devices"]), 1)
        self.assertEqual(self.call("POST", "/api/auth/devices/revoke", {"id": "missing"}, self.tokenh())[0], 404)
        self.assertEqual(self.call("POST", "/api/auth/devices/revoke", {}, self.tokenh())[0], 400)

    def test_logout_revokes_only_the_current_device(self) -> None:
        self.set_passphrase()
        mine = self.login()[1]["device_token"]
        other = self.login()[1]["device_token"]
        self.assertEqual(self.call("POST", "/api/auth/logout", {}, self.device(mine))[0], 200)
        self.assertEqual(self.call("GET", "/api/config", headers=self.device(mine))[0], 401)
        self.assertEqual(self.call("GET", "/api/config", headers=self.device(other))[0], 200)
        self.assertEqual(self.call("POST", "/api/auth/logout", {}, self.tokenh())[1]["revoked"], False)
        self.assertEqual(self.call("POST", "/api/auth/logout", {})[0], 401)

    def test_changing_the_passphrase(self) -> None:
        self.set_passphrase()
        mine = self.login()[1]["device_token"]
        other = self.login()[1]["device_token"]
        new = "a different long phrase"
        self.assertEqual(self.call("POST", "/api/auth/passphrase", {"new": new}, self.device(mine))[0], 401, "needs current")
        self.assertEqual(self.call("POST", "/api/auth/passphrase", {"current": "wrong wrong wrong", "new": new}, self.device(mine))[0], 401)
        self.assertEqual(self.call("POST", "/api/auth/passphrase", {"current": self.PASSPHRASE, "new": "short"}, self.device(mine))[0], 400)
        status, payload, _ = self.call("POST", "/api/auth/passphrase", {"current": self.PASSPHRASE, "new": new}, self.device(mine))
        self.assertEqual((status, payload["revoked_others"]), (200, 1))
        self.assertEqual(self.call("GET", "/api/config", headers=self.device(mine))[0], 200)
        self.assertEqual(self.call("GET", "/api/config", headers=self.device(other))[0], 401)
        self.assertEqual(self.login(passphrase=self.PASSPHRASE)[0], 401)
        self.assertEqual(self.login(passphrase=new)[0], 200)

    def test_passphrase_change_loses_to_a_concurrent_change_with_409(self) -> None:
        self.set_passphrase()
        mine = self.login()[1]["device_token"]
        real = panel.vsauth.AuthStore.check_passphrase
        fired = []

        def racing(store, *args):
            value = real(store, *args)
            if not fired:
                fired.append(1)
                panel.vsauth.AuthStore(self.root / "config" / panel.vsauth.STORE_NAME).set_passphrase("rival long passphrase")
            return value
        with mock.patch.object(panel.vsauth.AuthStore, "check_passphrase", racing):
            status, payload, _ = self.call("POST", "/api/auth/passphrase", {"current": self.PASSPHRASE, "new": "a different long phrase"}, self.device(mine))
        self.assertEqual((status, payload["error"]), (409, "passphrase changed, try again"))
        self.assertEqual(self.login(passphrase="rival long passphrase")[0], 200)

    def test_long_token_may_change_the_passphrase_without_the_current_one(self) -> None:
        self.set_passphrase()
        device = self.login()[1]["device_token"]
        status, payload, _ = self.call("POST", "/api/auth/passphrase", {"new": "brand new long phrase"}, self.tokenh())
        self.assertEqual((status, payload["revoked_others"]), (200, 1))
        self.assertEqual(self.call("GET", "/api/config", headers=self.device(device))[0], 401)

    def test_password_endpoints_require_authentication(self) -> None:
        for path in ("/api/auth/logout", "/api/auth/devices/revoke", "/api/auth/passphrase"):
            self.assertEqual(self.call("POST", path, {"new": self.PASSPHRASE})[0], 401, path)


if __name__ == "__main__":
    unittest.main()

class HostAllowlistApiTests(SignInApiTests):
    """DNS-rebinding guard: every request needs a Host naming this machine."""

    def raw(self, method, path, host, headers=None, body=None):
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            connection.putrequest(method, path, skip_host=True)
            if host is not None:
                connection.putheader("Host", host)
            for name, value in (headers or {}).items():
                connection.putheader(name, value)
            data = json.dumps(body).encode() if body is not None else None
            if data is not None:
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", str(len(data)))
            connection.endheaders(data)
            response = connection.getresponse()
            return response.status, response.read().decode()
        finally:
            connection.close()

    def test_wrong_host_is_refused_for_static_files_and_api(self) -> None:
        port = self.server.server_address[1]
        self.set_passphrase()
        for host in ("evil.example", f"evil.example:{port}", f"127.0.0.1:{port + 1}", "127.0.0.1", "", None, f"127.0.0.1:{port}.evil.example"):
            for method, path in (("GET", "/"), ("GET", "/health"), ("GET", "/api/auth/status"), ("GET", "/api/config")):
                status, text = self.raw(method, path, host, self.tokenh())
                self.assertIn(status, (400, 421), (host, path))
                self.assertNotIn("passphrase", text.lower())
            status, _ = self.raw("POST", "/api/auth/login", host, {"Origin": f"http://{host}"} if host else {}, {"passphrase": self.PASSPHRASE, "remember": True})
            self.assertIn(status, (400, 421), host)
        self.assertEqual(self.call("GET", "/api/auth/status")[1], {"passphrase_set": True}, "no attempt was counted")
        self.assertEqual(panel.LOGIN_THROTTLE.retry_after("127.0.0.1"), 0)

    def test_allowed_host_forms_are_accepted(self) -> None:
        port = self.server.server_address[1]
        for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"LocalHost:{port}", f"[::1]:{port}"):
            self.assertEqual(self.raw("GET", "/api/config", host, self.tokenh())[0], 200, host)
            self.assertEqual(self.raw("GET", "/", host)[0], 200, host)

    def test_extra_hosts_come_from_the_environment(self) -> None:
        port = self.server.server_address[1]
        self.assertEqual(self.raw("GET", "/health", f"boat.example:{port}")[0], 421)
        panel._HOST_ALLOWLISTS.clear()
        with mock.patch.dict(panel.os.environ, {"CONTROL_PANEL_ALLOWED_HOSTS": "boat.example, other.example:9"}):
            panel._HOST_ALLOWLISTS.clear()
            allowlist = panel.host_allowlist(self.server.server_address)
            allowlist._expires = float("-inf")
            self.assertEqual(self.raw("GET", "/health", f"boat.example:{port}")[0], 200)
            self.assertEqual(self.raw("GET", "/health", "boat.example")[0], 421, "port is required unless it is 80")
            self.assertEqual(self.raw("GET", "/health", "other.example:9")[0], 200)
        panel._HOST_ALLOWLISTS.clear()


class HostAllowlistUnitTests(unittest.TestCase):
    def test_lan_mode_adds_machine_names_and_refreshes(self) -> None:
        clock = [0.0]
        names = [{"boat", "boat.local", "203.0.113.20"}]
        allow = panel.vsauth.HostAllowlist("0.0.0.0", 8780, clock=lambda: clock[0], discover=lambda: names[0])
        self.assertTrue(allow.allowed("boat.local:8780"))
        self.assertTrue(allow.allowed("203.0.113.20:8780"))
        self.assertFalse(allow.allowed("203.0.113.20"), "port required")
        self.assertFalse(allow.allowed("203.0.113.21:8780"))
        names[0] = {"boat", "boat.local", "203.0.113.21"}
        self.assertFalse(allow.allowed("203.0.113.21:8780"), "cached until the refresh interval")
        clock[0] = 61
        self.assertTrue(allow.allowed("203.0.113.21:8780"))
        self.assertFalse(allow.allowed("203.0.113.20:8780"))
        self.assertFalse(allow.allowed("attacker.example:8780"))

    def test_loopback_mode_does_not_trust_machine_names(self) -> None:
        allow = panel.vsauth.HostAllowlist("127.0.0.1", 8780, discover=lambda: {"boat.local"})
        self.assertFalse(allow.allowed("boat.local:8780"))
        self.assertTrue(allow.allowed("localhost:8780"))

    def test_port_80_may_omit_the_port(self) -> None:
        allow = panel.vsauth.HostAllowlist("127.0.0.1", 80)
        self.assertTrue(allow.allowed("localhost"))
        self.assertTrue(allow.allowed("localhost:80"))
