import importlib.util
import os
import stat
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("wizard", ROOT / "vesselstack-wizard.py")
wizard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wizard)

class WizardTests(unittest.TestCase):
    def test_render_is_complete_and_shell_safe(self):
        output = wizard.render({"BOAT_NAME": "A Boat (Test)", "SOCKETCAN_ENABLE": "true"})
        self.assertIn("BOAT_NAME='A Boat (Test)'", output)
        self.assertIn("SOCKETCAN_BITRATE=250000", output)
        self.assertIn("SOCKETCAN_ENABLE=true", output)

    def test_rejects_unknown_and_multiline_values(self):
        with self.assertRaises(ValueError): wizard.render({"NOT_A_SETTING": "x"})
        with self.assertRaises(ValueError): wizard.render({"BOAT_NAME": "bad\nLINE=x"})

    def test_rejects_invalid_enums(self):
        with self.assertRaises(ValueError): wizard.render({"SIGNALK_MODE": "surprise"})
        with self.assertRaises(ValueError): wizard.render({"AIS_ENABLE": "yes"})

    def test_generated_file_permissions_can_be_private(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "vesselstack.env"
            target.write_text(wizard.render({})); os.chmod(target, 0o600)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_system_report_omits_virtual_topology(self):
        report = wizard.system_report()
        self.assertTrue(all(name.startswith(("eth", "en", "wlan", "wl", "can")) for name in report["interfaces"]))

if __name__ == "__main__": unittest.main()
