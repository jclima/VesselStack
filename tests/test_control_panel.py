#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
import unittest
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


if __name__ == "__main__":
    unittest.main()
