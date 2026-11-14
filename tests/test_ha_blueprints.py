import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BLUEPRINTS = sorted((ROOT / "templates/homeassistant/blueprints/automation/vesselstack").glob("*.yaml"))


class BlueprintTests(unittest.TestCase):
    def test_three_blueprints_exist(self):
        self.assertEqual([p.name for p in BLUEPRINTS], ["bilge-active.yaml", "low-battery.yaml", "shore-power-lost.yaml"])

    def test_notification_actions_default_to_a_persistent_notification(self):
        for path in BLUEPRINTS:
            text = path.read_text()
            with self.subTest(blueprint=path.name):
                block = text.split("notification_actions:", 1)[1].split("\n\n", 1)[0]
                self.assertRegex(block, r"(?m)^\s+default:\s*$")
                self.assertRegex(block, r"(?m)^\s+- action: persistent_notification\.create\s*$")
                self.assertRegex(block, r"(?m)^\s+#\s+- action: notify\.mobile_app_YOUR_PHONE")
                self.assertIn("not a sole alarm system", block)
                self.assertIn("action: !input notification_actions", text)

    def test_yaml_loads_with_the_input_tag_when_pyyaml_is_available(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed")

        class Loader(yaml.SafeLoader):
            pass

        Loader.add_constructor("!input", lambda loader, node: ("input", loader.construct_scalar(node)))
        for path in BLUEPRINTS:
            with self.subTest(blueprint=path.name):
                data = yaml.load(path.read_text(), Loader)
                default = data["blueprint"]["input"]["notification_actions"]["default"]
                self.assertEqual(default[0]["action"], "persistent_notification.create")
                self.assertEqual(data["action"], ("input", "notification_actions"))


if __name__ == "__main__":
    unittest.main()
