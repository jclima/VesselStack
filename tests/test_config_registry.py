import importlib.util
import unittest
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from common import config_registry as registry


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wizard = load("wizard_registry_test", "vesselstack-wizard.py")
panel = load("panel_registry_test", "control-panel/app.py")


class ConfigRegistryTests(unittest.TestCase):
    def test_wizard_defaults_are_registry_defaults_and_legacy_values_explicit(self):
        self.assertEqual(wizard.DEFAULTS, registry.DEFAULTS)
        self.assertEqual(registry.LEGACY_DEFAULTS["SIGNALK_MODE"], "existing")
        self.assertEqual(registry.LEGACY_DEFAULTS["INFLUXDB_RAW_BUCKET"], "vesselstack_raw")

    def test_unknown_and_shared_bad_values_rejected_by_both_surfaces(self):
        with self.assertRaises(ValueError): registry.validate_value("NO_SUCH_KEY", "x")
        for key, bad in (("MQTT_PORT", "0"), ("MQTT_PORT", "65536"), ("MQTT_USERNAME", "bad name"), ("BOAT_NAME", "bad;name"), ("VESSELSTACK_UID", "1.5"), ("VESSELSTACK_FIREWALL_ALLOW_TCP", "22,9000:8000")):
            with self.subTest(key=key, bad=bad):
                with self.assertRaises(ValueError): registry.validate_value(key, bad)
                spec = panel.FIELD_BY_KEY.get(key)
                if spec:
                    with self.assertRaises(ValueError): panel.validate_value(spec, bad)
                with self.assertRaises(ValueError): wizard.validate({key: bad})

    def test_alert_settings_are_opt_in_with_no_assumed_thresholds(self):
        d = registry.DEFAULTS
        self.assertEqual((d["ALERTS_ENABLE"], d["ALERT_TELEGRAM_CHAT_IDS"], d["ALERT_BATTERY_LOW_V"], d["ALERT_BILGE_ENTITY"],
                          d["ALERT_SHORE_POWER_ENTITY"], d["ALERT_HEARTBEAT_URL"]), ("false", "", "", "", "", ""))
        self.assertEqual((d["ALERT_REPEAT_MINUTES"], d["ALERT_STALE_DATA_MINUTES"]), ("60", "15"))
        self.assertEqual(registry.SYSTEMD_COMPONENTS["alerts"], "vesselstack-alerts.timer")
        for key in ("ALERT_BATTERY_LOW_V", "ALERT_HEARTBEAT_URL", "ALERT_TELEGRAM_CHAT_IDS"):
            self.assertEqual(registry.validate_value(key, ""), "")
        for key, good in (("ALERT_BATTERY_LOW_V", "12.2"), ("ALERT_HEARTBEAT_URL", "https://hc-ping.com/abc"), ("ALERT_TELEGRAM_CHAT_IDS", "1, -22"),
                          ("ALERT_BILGE_ENTITY", "binary_sensor.bilge"), ("ALERT_REPEAT_MINUTES", "30")):
            self.assertEqual(registry.validate_value(key, good), good)
        for key, bad in (("ALERT_BATTERY_LOW_V", "twelve"), ("ALERT_BATTERY_LOW_V", "0"), ("ALERT_HEARTBEAT_URL", "http://insecure.example"),
                         ("ALERT_TELEGRAM_CHAT_IDS", "@name"), ("ALERT_BILGE_ENTITY", "bilge"), ("ALERT_REPEAT_MINUTES", "1"), ("ALERT_STALE_DATA_MINUTES", "x")):
            with self.subTest(key=key, bad=bad):
                with self.assertRaises(ValueError): registry.validate_value(key, bad)
                with self.assertRaises(ValueError): wizard.validate({key: bad})
                spec = panel.FIELD_BY_KEY[key]
                with self.assertRaises(ValueError): panel.validate_value(spec, bad)

    def test_alert_keys_exist_on_every_surface_and_the_installer(self):
        install = (ROOT / "install.sh").read_text()
        example = (ROOT / "vesselstack.example.env").read_text()
        for key in ("ALERTS_ENABLE", "ALERT_TELEGRAM_CHAT_IDS", "ALERT_BATTERY_LOW_V", "ALERT_BILGE_ENTITY", "ALERT_SHORE_POWER_ENTITY",
                    "ALERT_HEARTBEAT_URL", "ALERT_REPEAT_MINUTES", "ALERT_STALE_DATA_MINUTES"):
            with self.subTest(key=key):
                self.assertIn(key, panel.FIELD_BY_KEY)
                self.assertIn(f'{key}=', example)
                self.assertIn(f'env_line {key} ', install)
                self.assertEqual(wizard.DEFAULTS[key], registry.DEFAULTS[key])
        self.assertIn("ALERT_HEARTBEAT_URL)", install.split("config_key_is_secret()")[1].split("}")[0])
        self.assertEqual(panel.FIELD_BY_KEY["ALERT_HEARTBEAT_URL"]["kind"], "secret")

    def test_remote_access_keys_are_off_by_default_on_every_surface_and_the_installer(self):
        self.assertEqual((registry.DEFAULTS["REMOTE_ACCESS"], registry.DEFAULTS["REMOTE_PANEL_SERVE"]), ("off", "true"))
        self.assertEqual(registry.SETTINGS["REMOTE_ACCESS"].component, "remote")
        install = (ROOT / "install.sh").read_text()
        example = (ROOT / "vesselstack.example.env").read_text()
        for key in ("REMOTE_ACCESS", "REMOTE_PANEL_SERVE"):
            with self.subTest(key=key):
                self.assertIn(key, panel.FIELD_BY_KEY)
                self.assertIn(f'{key}="', example)
                self.assertIn(f"\n{key}\n", install)
                self.assertEqual(wizard.DEFAULTS[key], registry.DEFAULTS[key])
        self.assertEqual(panel.FIELD_BY_KEY["REMOTE_ACCESS"]["kind"], "select")
        self.assertIn(': "${REMOTE_ACCESS:=off}"', install)
        self.assertIn(': "${REMOTE_PANEL_SERVE:=true}"', install)
        self.assertEqual(registry.validate_value("REMOTE_ACCESS", "tailscale"), "tailscale")
        for key, bad in (("REMOTE_ACCESS", "wireguard"), ("REMOTE_ACCESS", "TAILSCALE"), ("REMOTE_PANEL_SERVE", "yes")):
            with self.subTest(key=key, bad=bad):
                with self.assertRaises(ValueError): registry.validate_value(key, bad)
                with self.assertRaises(ValueError): wizard.validate({key: bad})
                with self.assertRaises(ValueError): panel.validate_value(panel.FIELD_BY_KEY[key], bad)

    def test_signalk_datapath_autoconfig_key_is_a_managed_boolean_on_every_surface(self):
        key = "SIGNALK_DATAPATH_AUTOCONFIG"
        self.assertEqual(registry.DEFAULTS[key], "true")
        self.assertEqual(registry.SETTINGS[key].component, "signalk")
        self.assertEqual(wizard.DEFAULTS[key], "true")
        self.assertEqual(wizard.validate({key: "false"})[key], "false")
        self.assertEqual(panel.FIELD_BY_KEY[key]["kind"], "boolean")
        self.assertEqual(panel.validate_value(panel.FIELD_BY_KEY[key], "false"), "false")
        for bad in ("yes", "1", "TRUE"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError): registry.validate_value(key, bad)
                with self.assertRaises(ValueError): wizard.validate({key: bad})
        installer = (ROOT / "install.sh").read_text()
        self.assertIn(f': "${{{key}:=true}}"', installer)
        self.assertIn(f"{key} NODE_EXPORTER_ENABLE", installer)
        self.assertIn(f"\n{key}\nSOCKETCAN_ENABLE\n", installer)
        self.assertIn(f'{key}="true"', (ROOT / "vesselstack.example.env").read_text())
        self.assertIn(key, (ROOT / "wizard/app.js").read_text())

    def test_legacy_units_normalize_before_enum_validation(self):
        self.assertEqual(wizard.validate({"BOAT_UNITS": " Nautical "})["BOAT_UNITS"], "metric")

    def test_owner_metadata_and_component_parity(self):
        self.assertEqual(registry.SETTINGS["HOME_ASSISTANT_TOKEN"].owner, "vessel")
        self.assertEqual(registry.SETTINGS["TELEGRAM_ENABLE"].owner, "vessel")
        self.assertEqual(registry.SETTINGS["TELEMETRY_INDEXER_ENABLE"].owner, "vessel")
        self.assertEqual(tuple(panel.CORE_COMPONENTS), registry.CORE_COMPONENTS)
        self.assertEqual(panel.COMPOSE_COMPONENTS, registry.COMPOSE_COMPONENTS)
        self.assertEqual(panel.SYSTEMD_COMPONENTS, registry.SYSTEMD_COMPONENTS)

    def test_unattended_operation_settings(self):
        expected = {"VESSELSTACK_WATCHDOG": "auto", "VESSELSTACK_JOURNAL_MAX_USE": "200M", "VESSELSTACK_BACKUP_SCHEDULE": "off",
                    "VESSELSTACK_BACKUP_KEEP": "8", "VESSELSTACK_BACKUP_SCHEDULE_SYSTEM_DISK": "false"}
        for key, default in expected.items():
            self.assertEqual(registry.DEFAULTS[key], default)
            self.assertEqual(wizard.DEFAULTS[key], default)
            self.assertIn(key, wizard.FIELD_INDEX)
        for key, good in (("VESSELSTACK_WATCHDOG", "true"), ("VESSELSTACK_BACKUP_SCHEDULE", "weekly"), ("VESSELSTACK_BACKUP_SCHEDULE", "daily"),
                          ("VESSELSTACK_JOURNAL_MAX_USE", "1G"), ("VESSELSTACK_JOURNAL_MAX_USE", "512K"), ("VESSELSTACK_JOURNAL_MAX_USE", "off"), ("VESSELSTACK_BACKUP_KEEP", "365"),
                          ("VESSELSTACK_BACKUP_SCHEDULE_SYSTEM_DISK", "true")):
            with self.subTest(key=key, good=good):
                self.assertEqual(registry.validate_value(key, good), good)
                self.assertEqual(wizard.validate({key: good})[key], good)
        for key, bad in (("VESSELSTACK_WATCHDOG", "yes"), ("VESSELSTACK_BACKUP_SCHEDULE", "hourly"), ("VESSELSTACK_JOURNAL_MAX_USE", "200"),
                         ("VESSELSTACK_JOURNAL_MAX_USE", "0M"), ("VESSELSTACK_JOURNAL_MAX_USE", "1.5G"), ("VESSELSTACK_JOURNAL_MAX_USE", "200MB"),
                         ("VESSELSTACK_BACKUP_KEEP", "0"), ("VESSELSTACK_BACKUP_KEEP", "00"), ("VESSELSTACK_BACKUP_KEEP", "08"), ("VESSELSTACK_BACKUP_KEEP", "010"), ("VESSELSTACK_BACKUP_KEEP", "366"), ("VESSELSTACK_BACKUP_KEEP", "2.5"),
                         ("VESSELSTACK_BACKUP_SCHEDULE_SYSTEM_DISK", "maybe")):
            with self.subTest(key=key, bad=bad):
                with self.assertRaises(ValueError): registry.validate_value(key, bad)
                with self.assertRaises(ValueError): wizard.validate({key: bad})
                spec = panel.FIELD_BY_KEY[key]
                with self.assertRaises(ValueError): panel.validate_value(spec, bad)

    def test_installer_and_example_env_carry_the_unattended_keys(self):
        installer = (ROOT / "install.sh").read_text()
        example = (ROOT / "vesselstack.example.env").read_text()
        for key, default in (("VESSELSTACK_WATCHDOG", "auto"), ("VESSELSTACK_JOURNAL_MAX_USE", "200M"), ("VESSELSTACK_BACKUP_SCHEDULE", "off"),
                             ("VESSELSTACK_BACKUP_KEEP", "8"), ("VESSELSTACK_BACKUP_SCHEDULE_SYSTEM_DISK", "false")):
            with self.subTest(key=key):
                self.assertIn(f': "${{{key}:={default}}}"', installer)
                self.assertRegex(installer, rf"(?m)^{key}$")
                self.assertIn(f'{key}="{default}"', example)

    def test_chat_port_override_is_preferred_and_invalid_is_not_hidden(self):
        self.assertEqual(registry.effective_chat_port({"BOAT_CHAT_PORT": "8000"}, {"BOAT_CHAT_PORT": "9000"}), 9000)
        self.assertEqual(registry.effective_chat_port({"BOAT_CHAT_PORT": "8000"}, {"BOAT_CHAT_PORT": ""}), 8765)
        self.assertEqual(registry.effective_chat_port({}, {}), 8765)
        with self.assertRaises(ValueError): registry.effective_chat_port({"BOAT_CHAT_PORT": "bad"}, {})

    def test_endpoint_precedence_and_irrelevant_malformed_values(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "config").mkdir()
            (root / "config" / "vesselstack.env").write_text("VESSELSTACK_DATA='" + str(root / "data") + "'\nUNRELATED='unterminated\n")
            (root / "config" / "boat-chat.env").write_text("BOAT_CHAT_HOST=127.0.0.1\nBOAT_CHAT_PORT=8123\n")
            data = root / "data" / "boat-chat"; data.mkdir(parents=True)
            (data / "boat-chat.env").write_text("BOAT_CHAT_PORT=8456\n")
            self.assertEqual(registry.load_effective_chat_endpoint(root)["port"], 8456)
            explicit = root / "explicit.env"; explicit.write_text("BOAT_CHAT_PORT=\nBOAT_CHAT_HOST=127.0.0.2\n")
            self.assertEqual(registry.load_effective_chat_endpoint(root, env={"BOAT_CHAT_ENV": str(explicit)})["port"], 8765)
            self.assertEqual(registry.load_effective_chat_endpoint(root, env={"BOAT_CHAT_ENV": str(explicit)})["host"], "127.0.0.2")
            bad = root / "bad.env"; bad.write_text("BOAT_CHAT_PORT=70000\n")
            with self.assertRaises(ValueError): registry.load_effective_chat_endpoint(root, env={"BOAT_CHAT_ENV": str(bad)})


if __name__ == "__main__":
    unittest.main()
