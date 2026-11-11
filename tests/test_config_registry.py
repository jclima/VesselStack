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

    def test_legacy_units_normalize_before_enum_validation(self):
        self.assertEqual(wizard.validate({"BOAT_UNITS": " Nautical "})["BOAT_UNITS"], "metric")

    def test_owner_metadata_and_component_parity(self):
        self.assertEqual(registry.SETTINGS["HOME_ASSISTANT_TOKEN"].owner, "vessel")
        self.assertEqual(registry.SETTINGS["TELEGRAM_ENABLE"].owner, "vessel")
        self.assertEqual(registry.SETTINGS["TELEMETRY_INDEXER_ENABLE"].owner, "vessel")
        self.assertEqual(tuple(panel.CORE_COMPONENTS), registry.CORE_COMPONENTS)
        self.assertEqual(panel.COMPOSE_COMPONENTS, registry.COMPOSE_COMPONENTS)
        self.assertEqual(panel.SYSTEMD_COMPONENTS, registry.SYSTEMD_COMPONENTS)

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
