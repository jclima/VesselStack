import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vesselstack-firewall"


def render(**environment):
    env = {"PATH": os.environ["PATH"], "VESSELSTACK_ENV": "/nonexistent"}
    env.update(environment)
    return subprocess.run([str(SCRIPT), "print"], env=env, capture_output=True, text=True)


class FirewallRenderTests(unittest.TestCase):
    def test_default_policy_drops_admin_ports_only(self):
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("-A VESSELSTACK-INPUT -i wlan0 -p tcp -m multiport --dports 1883,8086,9090,43000 -j DROP", out)
        self.assertIn("-A VESSELSTACK-DOCKER -i wlan0 -p tcp -m multiport --dports 1883,3000,8086,9090 -j DROP", out)
        self.assertNotIn("ESTABLISHED", out)

    def test_payload_is_atomic_restore_input(self):
        out = render().stdout
        self.assertIn("*filter\n:VESSELSTACK-INPUT - [0:0]\n-F VESSELSTACK-INPUT\n", out)
        self.assertEqual(out.count("COMMIT"), 3)
        self.assertIn("-A VESSELSTACK-INPUT -j RETURN\nCOMMIT", out)

    def test_strict_defaults_allow_only_ssh(self):
        out = render(VESSELSTACK_FIREWALL_STRICT="true").stdout
        self.assertIn("-A VESSELSTACK-INPUT -i wlan0 -p tcp --dport 22 -j RETURN", out)
        self.assertIn("-A VESSELSTACK-INPUT -i wlan0 -j DROP", out)
        self.assertIn("-A VESSELSTACK-INPUT -i wlan0 -p ipv6-icmp -j RETURN", out)
        self.assertIn("-A VESSELSTACK-INPUT -i wlan0 -p udp --dport 68 -j RETURN", out)
        self.assertIn("-A VESSELSTACK-DOCKER -i wlan0 -j DROP", out)
        for exposed in ("3000", "8765", "80", "443"):
            self.assertNotIn(f"--dport {exposed} ", out)
        self.assertIn("-i wlan0 -j DROP\n-A VESSELSTACK-INPUT -j RETURN\nCOMMIT", out)

    def test_strict_allow_lists_are_configurable(self):
        out = render(
            VESSELSTACK_FIREWALL_STRICT="true",
            VESSELSTACK_FIREWALL_ALLOW_TCP="22,8765,5000:5100",
            VESSELSTACK_FIREWALL_ALLOW_UDP="5353",
            VESSELSTACK_UNTRUSTED_INTERFACE="wlan1",
        ).stdout
        self.assertIn("-A VESSELSTACK-INPUT -i wlan1 -p tcp --dport 8765 -j RETURN", out)
        self.assertIn("-A VESSELSTACK-INPUT -i wlan1 -p tcp --dport 5000:5100 -j RETURN", out)
        self.assertIn("-A VESSELSTACK-INPUT -i wlan1 -p udp --dport 5353 -j RETURN", out)
        self.assertIn("--ctorigdstport 8765 -j RETURN", out)

    def test_empty_allow_list_allows_nothing(self):
        out = render(VESSELSTACK_FIREWALL_STRICT="true", VESSELSTACK_FIREWALL_ALLOW_TCP="").stdout
        self.assertNotIn("--dport 22", out)

    def test_invalid_input_is_rejected(self):
        for bad in (
            {"VESSELSTACK_FIREWALL_STRICT": "yes"},
            {"VESSELSTACK_FIREWALL_ALLOW_TCP": "22;reboot"},
            {"VESSELSTACK_FIREWALL_ALLOW_TCP": "70000"},
            {"VESSELSTACK_FIREWALL_ALLOW_UDP": "0"},
            {"VESSELSTACK_UNTRUSTED_INTERFACE": "wlan0 -j ACCEPT"},
        ):
            self.assertNotEqual(render(**bad).returncode, 0, bad)

    def test_print_mode_reads_env_file_and_needs_no_root(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "vesselstack.env"
            env.write_text("VESSELSTACK_FIREWALL_STRICT=true\nVESSELSTACK_FIREWALL_ALLOW_TCP='22,80'\nMQTT_PORT=1999\n")
            out = render(VESSELSTACK_ENV=str(env)).stdout
        self.assertIn("--dport 80 -j RETURN", out)

    def test_unknown_action_is_a_usage_error(self):
        result = subprocess.run([str(SCRIPT), "bogus"], capture_output=True, text=True, env={"PATH": os.environ["PATH"], "VESSELSTACK_ENV": "/nonexistent"})
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
