import http.client
import importlib.util
import itertools
import json
import re
import select
import sys
import threading
import time
from http.server import ThreadingHTTPServer
import os
import stat
import subprocess
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

    def test_raw_retention_defaults_empty_and_is_validated(self):
        self.assertIn("INFLUXDB_RAW_RETENTION=''", wizard.render({}))
        self.assertIn("INFLUXDB_RAW_RETENTION=30d", wizard.render({"INFLUXDB_RAW_RETENTION": "30d"}))
        for bad in ("30", "1y", "0d", "-5h"):
            with self.assertRaises(ValueError): wizard.render({"INFLUXDB_RAW_RETENTION": bad})

    def test_generated_file_permissions_can_be_private(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "vesselstack.env"
            target.write_text(wizard.render({})); os.chmod(target, 0o600)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_system_report_omits_virtual_topology(self):
        report = wizard.system_report()
        self.assertTrue(all(name.startswith(("eth", "en", "wlan", "wl", "can")) for name in report["interfaces"]))

class WizardHttpTests(unittest.TestCase):
    TOKEN = "test-token-123"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "vesselstack.env"
        wizard.OUTPUT = self.output
        wizard.Handler.token = self.TOKEN
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), wizard.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None, host="default"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        merged = {} if headers is None else dict(headers)
        if host == "default":
            host = f"127.0.0.1:{self.port}"
        if host is not None:
            merged["Host"] = host
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for key, value in merged.items():
            connection.putheader(key, value)
        payload = body if body is None or isinstance(body, bytes) else json.dumps(body).encode()
        if payload is not None:
            connection.putheader("Content-Length", str(len(payload)))
        connection.endheaders(payload)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def good(self, **extra):
        headers = {"Content-Type": "application/json", "X-Wizard-Token": self.TOKEN}
        headers.update(extra)
        return headers

    def test_save_succeeds_with_token_json_and_local_host(self):
        status, _ = self.request("POST", "/api/save", {"values": {"BOAT_NAME": "Test"}}, self.good())
        self.assertEqual(status, 200)
        self.assertIn("BOAT_NAME=Test", self.output.read_text())
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)

    def test_missing_or_wrong_token_is_rejected(self):
        for token in (None, "wrong"):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["X-Wizard-Token"] = token
            self.assertEqual(self.request("POST", "/api/save", {"values": {}}, headers)[0], 403)
        self.assertFalse(self.output.exists())

    def test_non_json_content_type_is_rejected(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
            headers = self.good(**{"Content-Type": content_type})
            self.assertEqual(self.request("POST", "/api/save", b'{"values":{}}', headers)[0], 415)
        self.assertFalse(self.output.exists())

    def test_dns_rebinding_host_is_rejected(self):
        self.assertEqual(self.request("POST", "/api/save", {"values": {}}, self.good(), host=f"evil.example:{self.port}")[0], 403)
        self.assertEqual(self.request("GET", "/api/system", None, {"X-Wizard-Token": self.TOKEN}, host="evil.example")[0], 403)
        self.assertEqual(self.request("GET", "/", None, {}, host=f"rebind.test:{self.port}")[0], 403)
        self.assertEqual(self.request("GET", "/", None, {}, host="127.0.0.1:1")[0], 403)

    def test_cross_origin_post_is_rejected_and_same_origin_allowed(self):
        self.assertEqual(self.request("POST", "/api/save", {"values": {}}, self.good(Origin="https://evil.example"))[0], 403)
        self.assertEqual(self.request("POST", "/api/save", {"values": {}}, self.good(Origin="null"))[0], 403)
        self.assertEqual(self.request("POST", "/api/save", {"values": {}}, self.good(Origin=f"http://localhost:{self.port}"), host=f"localhost:{self.port}")[0], 200)

    def test_served_files_never_contain_the_token_and_api_requires_it(self):
        for path in ("/", "/index.html", "/app.js", "/styles.css"):
            status, body = self.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertNotIn(self.TOKEN.encode(), body, path)
            self.assertNotIn(b"wizard-token", body, path)
        self.assertEqual(self.request("GET", "/api/system")[0], 403)
        self.assertEqual(self.request("GET", "/api/system", None, {"X-Wizard-Token": "wrong"})[0], 403)
        self.assertEqual(self.request("GET", "/api/system", None, {"X-Wizard-Token": self.TOKEN})[0], 200)

    def test_full_flow_with_token_from_fragment(self):
        # The browser sends the fragment token only as a header, never in a URL.
        status, _ = self.request("GET", f"/?token={self.TOKEN}")
        self.assertEqual(status, 200)
        self.assertEqual(self.request("POST", "/api/save", {"values": {"BOAT_NAME": "Frag"}}, {"Content-Type": "application/json"})[0], 403)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.request("POST", "/api/save", {"values": {"BOAT_NAME": "Frag"}}, self.good())[0], 200)
        self.assertIn("BOAT_NAME=Frag", self.output.read_text())

    def test_exit_after_save_stops_the_server(self):
        wizard.Handler.exit_after_save = True
        self.addCleanup(setattr, wizard.Handler, "exit_after_save", False)
        status, _ = self.request("POST", "/api/save", {"values": {"BOAT_NAME": "Test"}}, self.good())
        self.assertEqual(status, 200)
        for _ in range(50):
            try:
                self.request("GET", "/api/system", None, self.good())
            except OSError:
                break
            threading.Event().wait(0.1)
        else:
            self.fail("server still accepting connections after save")

    def test_failed_save_does_not_stop_the_server(self):
        wizard.Handler.exit_after_save = True
        self.addCleanup(setattr, wizard.Handler, "exit_after_save", False)
        self.assertEqual(self.request("POST", "/api/save", {"values": {"NOT_A_SETTING": "x"}}, self.good())[0], 400)
        self.assertEqual(self.request("GET", "/api/system", None, self.good())[0], 200)

    def test_save_does_not_follow_a_planted_temp_symlink(self):
        victim = Path(self.directory.name) / "victim"
        victim.write_text("keep")
        os.symlink(victim, str(self.output) + ".tmp")
        status, _ = self.request("POST", "/api/save", {"values": {"BOAT_NAME": "Test"}}, self.good())
        self.assertEqual(status, 200)
        self.assertEqual(victim.read_text(), "keep")
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)

    def test_unset_token_refuses_everything(self):
        wizard.Handler.token = None
        self.assertEqual(self.request("POST", "/api/save", {"values": {}}, self.good())[0], 403)

    def test_app_js_reads_token_from_fragment_and_clears_it(self):
        source = (ROOT / "wizard" / "app.js").read_text()
        self.assertEqual(source.count("X-Wizard-Token"), 2)
        self.assertIn("location.hash", source)
        self.assertIn("history.replaceState", source)
        self.assertNotIn("wizard-token", (ROOT / "wizard" / "index.html").read_text())

    def test_firewall_strict_settings_validated(self):
        self.assertIn("VESSELSTACK_FIREWALL_ALLOW_TCP=22", wizard.render({}))
        with self.assertRaises(ValueError): wizard.render({"VESSELSTACK_FIREWALL_STRICT": "maybe"})
        with self.assertRaises(ValueError): wizard.render({"VESSELSTACK_FIREWALL_ALLOW_TCP": "22,abc"})

class WizardCommandLineTests(unittest.TestCase):
    def test_output_and_exit_after_save_flags(self):
        import subprocess, urllib.request, re as regex
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "custom.env"
            process = subprocess.Popen(
                ["python3", "-I", str(ROOT / "vesselstack-wizard.py"), "--port", "0", "--output", str(target), "--exit-after-save"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            self.addCleanup(process.kill)
            banner = [process.stdout.readline() for _ in range(4)]
            url = regex.search(r"http://127\.0\.0\.1:(\d+)", banner[0])
            token = regex.search(r"http://127\.0\.0\.1:\d+/#token=([A-Za-z0-9_-]+)", banner[1])
            self.assertTrue(url and token, banner)
            self.assertNotIn("token", banner[0].lower())
            request = urllib.request.Request(
                f"http://127.0.0.1:{url.group(1)}/api/save", data=json.dumps({"values": {"BOAT_NAME": "Cli"}}).encode(),
                headers={"Content-Type": "application/json", "X-Wizard-Token": token.group(1)})
            self.assertEqual(urllib.request.urlopen(request, timeout=5).status, 200)
            self.assertEqual(process.wait(timeout=10), 0)
            self.assertIn("BOAT_NAME=Cli", target.read_text())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

class ValidateCliTests(unittest.TestCase):
    def check(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "in.env"; path.write_text(text)
            return subprocess.run(["python3", "-I", str(ROOT / "vesselstack-wizard.py"), "--validate", str(path)],
                                  capture_output=True, text=True, timeout=20)

    def test_wizard_output_and_example_env_are_valid(self):
        self.assertEqual(self.check(wizard.render({"BOAT_NAME": "A Boat (Test)"})).returncode, 0)
        self.assertEqual(self.check((ROOT / "vesselstack.example.env").read_text()).returncode, 0)

    def test_refuses_unknown_key_bad_characters_enums_and_syntax(self):
        for text in ("NOT_A_SETTING=x\n", "BOAT_NAME='a;b'\n", "BOAT_NAME='$(id)'\n", "SIGNALK_MODE=surprise\n",
                     "AIS_ENABLE=yes\n", "VESSELSTACK_CAN_CREATE_VCAN=maybe\n", "BOAT_NAME=a b\n", "garbage\n"):
            result = self.check(text)
            self.assertEqual(result.returncode, 1, text)
            self.assertNotIn("Traceback", result.stderr)

    def test_missing_file_is_refused(self):
        result = subprocess.run(["python3", "-I", str(ROOT / "vesselstack-wizard.py"), "--validate", "/nonexistent/x.env"],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1)


class Scripted:
    """Drives run_cli with a list of answers, like the bootstrap's scripted-answers file.

    After the list runs out the answer is Enter (the default) unless strict, which ends the input."""
    def __init__(self, answers, strict=False):
        self.answers = iter(answers); self.strict = strict; self.output = []; self.asked = []
    def _next(self):
        value = next(self.answers, None if self.strict else "")
        return value
    def console(self):
        def write(text): self.output.append(text)
        def read_line(): return self._next()
        def read_secret(): return self._next()
        return wizard.Console(read_line, read_secret, write)
    @property
    def text(self): return "".join(self.output)

def drive(answers, values=None, facts=None, strict=False):
    facts = facts if facts is not None else {"architecture": "aarch64", "memory_gib": 8.0, "docker": True, "compose": True,
                                              "interfaces": ["eth0", "wlan0"], "can0": False, "timezone": "Europe/Oslo", "wifi": "wlan0"}
    values = values if values is not None else wizard.initial_values(facts)
    script = Scripted(answers, strict)
    try: result = wizard.run_cli(script.console(), values, facts)
    except wizard.Aborted: result = "aborted"
    return result, script.text

# One Enter for each question of an all-default walk (see SECTIONS), the advanced gate, then the review choice.
class CliWizardTests(unittest.TestCase):
    def test_every_setting_has_a_prompt_and_every_default_passes_its_own_check(self):
        self.assertEqual(set(wizard.FIELD_INDEX) - {"LAN_ACCESS"}, set(wizard.DEFAULTS))  # LAN_ACCESS is a pseudo-setting
        values = wizard.DEFAULTS.copy()
        for key, field in wizard.FIELD_INDEX.items():
            if key == "LAN_ACCESS" or field["kind"] in ("bool", "choice") or values[key] in ("", "GENERATE"): continue
            self.assertEqual(wizard.normalize(field, values[key], values)[1], None, key)
        # The browser wizard's fields are all reachable from the terminal.
        app = (ROOT / "wizard" / "app.js").read_text()
        synced = set(re.findall(r"'([A-Z][A-Z0-9_]+)'", app[app.index("function sync()"):app.index("function draw()")]))
        self.assertTrue(synced and synced <= set(wizard.FIELD_INDEX))
        example = wizard.load_env(ROOT / "vesselstack.example.env")
        for key, value in example.items():
            field = wizard.FIELD_INDEX[key]
            if field["kind"] in ("bool", "choice") or value in ("", "GENERATE"): continue
            self.assertIsNone(wizard.normalize(field, value, wizard.DEFAULTS.copy())[1], key)

    def test_defaults_walk_returns_the_defaults_and_renders(self):
        result, output = drive([], strict=False)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["BOAT_NAME"], "My Boat")
        self.assertEqual(result["BOAT_TIMEZONE"], "Europe/Oslo")  # detected, shown as the default
        self.assertEqual(result["SOCKETCAN_ENABLE"], "false")
        self.assertEqual(result["INFLUXDB_RAW_RETENTION"], "")
        self.assertEqual(result["INFLUXDB_PASSWORD"], "GENERATE")
        wizard.render(result)
        self.assertIn("Boat name [My Boat]", output)
        self.assertIn("Timezone [Europe/Oslo]", output)
        self.assertIn("Review (secrets are never shown)", output)
        self.assertIn("installer default (30d on a new install)", output)

    def test_edits_invalid_input_help_and_back(self):
        answers = ["Bad;Name", "Sea Wolf", "Sailing yacht", "3", "Atlantis/Nowhere", "Europe/Oslo",
                   "y", "12345", "123456789", "b", "", "ABCD1", "11.5", "3.8",
                   "2", "http://192.0.2.10:3000",
                   "y", "y", "", "",   # CAN on, vcan on (interface becomes vcan0), interface, bitrate
                   "n", "", "?", "", "1", "y", "y", "", "n", "22,8080", "-", "14d", "", "n", "s"]
        result, output = drive(answers, strict=True)
        self.assertIsInstance(result, dict, output)
        self.assertEqual(result["BOAT_NAME"], "Sea Wolf")
        self.assertEqual(result["BOAT_UNITS"], "nautical")
        self.assertEqual(result["BOAT_TIMEZONE"], "Europe/Oslo")
        self.assertEqual((result["BOAT_MMSI"], result["BOAT_CALLSIGN"], result["BOAT_LOA_M"], result["BOAT_BEAM_M"]), ("123456789", "ABCD1", "11.5", "3.8"))
        self.assertEqual((result["SIGNALK_MODE"], result["SIGNALK_URL"]), ("existing", "http://192.0.2.10:3000"))
        self.assertEqual((result["SOCKETCAN_ENABLE"], result["VESSELSTACK_CAN_CREATE_VCAN"], result["SOCKETCAN_INTERFACE"]), ("true", "true", "vcan0"))
        self.assertEqual(result["VESSELSTACK_FIREWALL_ENABLE"], "true")
        self.assertEqual((result["VESSELSTACK_FIREWALL_ALLOW_TCP"], result["VESSELSTACK_FIREWALL_ALLOW_UDP"]), ("22,8080", ""))
        self.assertEqual(result["INFLUXDB_RAW_RETENTION"], "14d")
        for message in ("Use only letters, digits, spaces", "Unknown timezone", "An MMSI has exactly 9 digits", "A long-lived access token"):
            self.assertIn(message, output)
        wizard.render(result)

    def test_first_question_back_and_unclearable_setting(self):
        result, output = drive(["b", "-", "Boat"] , strict=False)
        self.assertIn("That is the first question here.", output)
        self.assertIn("This setting cannot be cleared", output)
        self.assertEqual(result["BOAT_NAME"], "Boat")

    def test_secrets_are_never_printed(self):
        token = "sekrit-ha-token-123.abc"
        custom = "my-own-password-98765"
        def aware(prompt_log):
            def write(text): prompt_log.append(text)
            def line():
                last = prompt_log[-1]
                if "access token (optional)" in last: return token
                if last.startswith("Advanced settings"): return "y"
                if "InfluxDB administrator password" in last: return custom
                if "[s] save" in last: return "s"
                return ""
            return wizard.Console(line, line, write)
        log = []
        facts = {"architecture": "aarch64", "memory_gib": 8.0, "docker": True, "compose": True, "interfaces": ["eth0"], "can0": False, "timezone": "UTC", "wifi": ""}
        result = wizard.run_cli(aware(log), wizard.initial_values(facts), facts)
        output = "".join(log)
        self.assertEqual(result["HOME_ASSISTANT_TOKEN"], token)
        self.assertEqual(result["INFLUXDB_PASSWORD"], custom)
        self.assertEqual(result["INFLUXDB_TOKEN"], "GENERATE")
        self.assertNotIn(token, output); self.assertNotIn(custom, output)
        self.assertIn("set (hidden)", output); self.assertIn("generated during installation", output)
        # A rejected secret is not echoed in the message either.
        field = wizard.FIELD_INDEX["INFLUXDB_PASSWORD"]
        value, message = wizard.normalize(field, "short", wizard.DEFAULTS.copy())
        self.assertIsNone(value); self.assertNotIn("short", message)

    def test_private_identifiers_are_hidden_in_the_review(self):
        result, output = drive(["", "", "", "", "y", "123456789", "ABCD1"], strict=False)
        self.assertEqual(result["BOAT_MMSI"], "123456789")
        self.assertNotIn("123456789", output); self.assertNotIn("ABCD1", output)

    def test_quit_and_ended_input(self):
        self.assertIsNone(drive([""] * 16 + ["q"], strict=True)[0])
        self.assertEqual(drive([""] * 3, strict=True)[0], "aborted")
        self.assertEqual(drive([""] * 16, strict=True)[0], "aborted")   # no review answer

    def test_edit_a_section_from_the_review(self):
        result, _ = drive([""] * 16 + ["e", "1", "Edited"] + [""] * 5 + ["s"], strict=True)
        self.assertEqual(result["BOAT_NAME"], "Edited")
        result, output = drive([""] * 16 + ["e", "99", "x", "s"], strict=True)
        self.assertIn("Please enter a section number", output)

    def test_set_option_uses_the_same_rules(self):
        values = wizard.DEFAULTS.copy()
        wizard.apply_settings(values, ["BOAT_NAME=Foo", "SOCKETCAN_ENABLE=yes", "SOCKETCAN_BITRATE=500000", "BOAT_UNITS=2"])
        self.assertEqual((values["BOAT_NAME"], values["SOCKETCAN_ENABLE"], values["SOCKETCAN_BITRATE"], values["BOAT_UNITS"]),
                         ("Foo", "true", "500000", "us_customary"))
        for bad in ("NOT_A_KEY=1", "SIGNALK_MODE=bogus", "BOAT_MMSI=12", "SOCKETCAN_ENABLE=maybe", "BOAT_NAME=", "BOAT_NAME=a;b", "NOEQUALS",
                    "INFLUXDB_RAW_RETENTION=1y", "VESSELSTACK_ROOT=/home/boat/vs", "VESSELSTACK_USER=root", "VESSELSTACK_WEB_BIND_ADDRESS=lan"):
            with self.assertRaises(ValueError, msg=bad): wizard.apply_settings(wizard.DEFAULTS.copy(), [bad])
        with self.assertRaises(ValueError) as caught: wizard.apply_settings(wizard.DEFAULTS.copy(), ["HOME_ASSISTANT_TOKEN=bad;tok"])
        self.assertNotIn("bad;tok", str(caught.exception))
        with self.assertRaises(ValueError) as caught: wizard.apply_settings(wizard.DEFAULTS.copy(), ["INFLUXDB_PASSWORD=short"])
        self.assertNotIn("short", str(caught.exception))
        values = wizard.DEFAULTS.copy(); wizard.apply_settings(values, ["INFLUXDB_RAW_RETENTION=default", "BOAT_MMSI=123456789", "BOAT_MMSI="])
        self.assertEqual((values["INFLUXDB_RAW_RETENTION"], values["BOAT_MMSI"]), ("", ""))

    def test_lan_access_presets_map_to_the_real_settings(self):
        keys = ("VESSELSTACK_WEB_BIND_ADDRESS", "VESSELSTACK_DATA_BIND_ADDRESS", "BOAT_CHAT_HOST")
        expected = {"local": ("127.0.0.1", "127.0.0.1", "127.0.0.1"),
                    "dashboards": ("0.0.0.0", "127.0.0.1", "0.0.0.0"),
                    "all": ("0.0.0.0", "0.0.0.0", "0.0.0.0")}
        for name, want in expected.items():
            values = wizard.DEFAULTS.copy()
            wizard.apply_settings(values, [f"LAN_ACCESS={name}"])
            self.assertEqual(tuple(values[k] for k in keys), want, name)
            self.assertEqual(wizard.lan_preset_of(values), name)
            self.assertNotIn("LAN_ACCESS", values)
            self.assertNotIn("LAN_ACCESS", wizard.render(values))
            # The control panel is in no preset: it stays on loopback, port untouched.
            self.assertEqual((values["CONTROL_PANEL_HOST"], values["CONTROL_PANEL_PORT"]), ("127.0.0.1", "8780"))
        # The default answer is the recommended preset and equals the plain defaults.
        self.assertEqual(wizard.lan_preset_of(wizard.DEFAULTS), wizard.LAN_DEFAULT)
        self.assertEqual(wizard.LAN_DEFAULT, "dashboards")
        for preset in wizard.LAN_PRESETS.values(): self.assertNotIn("CONTROL_PANEL_HOST", preset)
        # Later --set wins, in order; a mixed combination is reported as custom.
        values = wizard.DEFAULTS.copy()
        wizard.apply_settings(values, ["LAN_ACCESS=all", "VESSELSTACK_DATA_BIND_ADDRESS=127.0.0.1", "LAN_ACCESS=2"])
        self.assertEqual(wizard.lan_preset_of(values), "dashboards")  # 2 = second choice
        wizard.apply_settings(values, ["LAN_ACCESS=all", "BOAT_CHAT_HOST=127.0.0.1"])
        self.assertEqual(wizard.lan_preset_of(values), "custom")

    def test_lan_access_refusals(self):
        for bad in ("LAN_ACCESS=everyone", "LAN_ACCESS=", "LAN_ACCESS=4", "LAN_ACCESS=0.0.0.0"):
            with self.assertRaises(ValueError, msg=bad): wizard.apply_settings(wizard.DEFAULTS.copy(), [bad])
        # An env file cannot carry the pseudo-setting: install.sh would ignore it silently.
        with self.assertRaises(ValueError): wizard.validate({"LAN_ACCESS": "local"})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.env"; path.write_text("LAN_ACCESS=local\n")
            result = subprocess.run([sys.executable, "-I", str(ROOT / "vesselstack-wizard.py"), "--validate", str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)

    def test_lan_question_walk_and_firewall_warning(self):
        # Without the firewall: the three choices, the recommended default is kept on Enter.
        result, output = drive([], strict=False)
        self.assertIn("Who should be able to reach the dashboards? - choose 1-3 [dashboards]", output)
        self.assertNotIn("WARNING: The firewall is ON", output)
        self.assertEqual(wizard.lan_preset_of(result), "dashboards")
        # Choosing "local" and "all" through the prompt; the firewall interplay is explicit.
        values = wizard.DEFAULTS.copy(); wizard.apply_settings(values, ["VESSELSTACK_FIREWALL_ENABLE=true"])
        result, output = drive([], values=values, strict=False)
        self.assertIn("WARNING: The firewall is ON and treats wlan0 as untrusted", output)
        self.assertIn("will NOT be reachable from other devices", output)
        values = wizard.DEFAULTS.copy()
        field = wizard.FIELD_INDEX["LAN_ACCESS"]
        session = wizard.Session(Scripted(["3"]).console(), values, {})
        self.assertIsNone(wizard.ask_field(session, field))
        self.assertEqual(wizard.lan_preset_of(values), "all")
        self.assertTrue(any("NO login" in n for n in wizard.lan_notes(values)))
        session = wizard.Session(Scripted(["local"]).console(), values, {})
        wizard.ask_field(session, field)
        self.assertEqual(wizard.lan_preset_of(values), "local")
        self.assertTrue(any("host-network" in n for n in wizard.lan_notes(values)))
        # Strict mode: the note names the allow-list.
        wizard.apply_settings(values, ["LAN_ACCESS=dashboards", "VESSELSTACK_FIREWALL_ENABLE=true", "VESSELSTACK_FIREWALL_STRICT=true", "VESSELSTACK_FIREWALL_ALLOW_TCP=22"])
        self.assertIn("allow-listed ports (22)", wizard.firewall_note(values))
        # The review lists the choice only when it differs from the default.
        values = wizard.DEFAULTS.copy(); wizard.apply_settings(values, ["LAN_ACCESS=local"])
        _, output = drive([], values=values, strict=False)
        self.assertRegex(output, r"Who should be able to reach the dashboards\?\s+local")

    def test_set_lan_access_on_the_command_line_prints_the_notes_and_saves_real_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "vesselstack.env"
            result = subprocess.run([sys.executable, "-I", str(ROOT / "vesselstack-wizard.py"), "--cli", "--output", str(out),
                                     "--set", "LAN_ACCESS=all", "--set", "VESSELSTACK_FIREWALL_ENABLE=true"],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("NO login", result.stdout)
            self.assertIn("The firewall is ON and treats wlan0 as untrusted", result.stdout)
            saved = wizard.load_env(out)
            self.assertEqual((saved["VESSELSTACK_WEB_BIND_ADDRESS"], saved["VESSELSTACK_DATA_BIND_ADDRESS"], saved["BOAT_CHAT_HOST"], saved["CONTROL_PANEL_HOST"]),
                             ("0.0.0.0", "0.0.0.0", "0.0.0.0", "127.0.0.1"))
            self.assertNotIn("LAN_ACCESS", out.read_text())

    def test_timezone_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            saved = (wizard.TIMEZONE_FILE, wizard.LOCALTIME_LINK)
            self.addCleanup(lambda: (setattr(wizard, "TIMEZONE_FILE", saved[0]), setattr(wizard, "LOCALTIME_LINK", saved[1])))
            wizard.TIMEZONE_FILE = base / "timezone"; wizard.LOCALTIME_LINK = base / "localtime"
            self.assertEqual(wizard.detect_timezone(), "UTC")
            (base / "timezone").write_text("Europe/Oslo\n")
            self.assertEqual(wizard.detect_timezone(), "Europe/Oslo")
            (base / "timezone").write_text("not a zone; rm -rf\n")
            self.assertEqual(wizard.detect_timezone(), "UTC")
            (base / "zoneinfo" / "America").mkdir(parents=True); (base / "zoneinfo" / "America" / "Halifax").write_text("x")
            os.symlink(base / "zoneinfo" / "America" / "Halifax", base / "localtime")
            self.assertEqual(wizard.detect_timezone(), "America/Halifax")

    def test_detected_wifi_name_becomes_the_firewall_default(self):
        facts = {"interfaces": ["eth0", "wlp2s0"], "wifi": "wlp2s0", "timezone": "UTC"}
        self.assertEqual(wizard.initial_values(facts)["VESSELSTACK_UNTRUSTED_INTERFACE"], "wlp2s0")
        facts = {"interfaces": ["eth0", "wlan0"], "wifi": "wlan0", "timezone": "UTC"}
        self.assertEqual(wizard.initial_values(facts)["VESSELSTACK_UNTRUSTED_INTERFACE"], "wlan0")

    def test_fd_input_reads_one_line_and_leaves_the_rest(self):
        read_end, write_end = os.pipe()
        self.addCleanup(lambda: [os.close(fd) for fd in (read_end,) if fd >= 0])
        os.write(write_end, b"first\r\nsecond\nthird"); os.close(write_end)
        output = []
        reader = wizard.FdInput(read_end, output.append)
        self.assertEqual(reader.line(), "first")
        self.assertEqual(os.read(read_end, 100), b"second\nthird")   # nothing was buffered away

    def test_fd_input_end_of_file(self):
        read_end, write_end = os.pipe(); os.write(write_end, b"x"); os.close(write_end)
        reader = wizard.FdInput(read_end, lambda text: None)
        self.assertEqual(reader.line(), "x"); self.assertIsNone(reader.line()); os.close(read_end)

class CliCommandTests(unittest.TestCase):
    def run_wizard(self, *args, stdin=None, timeout=30):
        return subprocess.run([sys.executable, "-I", str(ROOT / "vesselstack-wizard.py"), *args], input=stdin, capture_output=True, text=True, timeout=timeout)

    def test_defaults_set_and_base_write_a_valid_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "vesselstack.env"
            result = self.run_wizard("--defaults", "--set", "BOAT_NAME=Foo", "--set", "SOCKETCAN_ENABLE=true", "--output", str(out))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
            values = wizard.load_env(out)
            self.assertEqual((values["BOAT_NAME"], values["SOCKETCAN_ENABLE"], values["INFLUXDB_TOKEN"]), ("Foo", "true", "GENERATE"))
            self.assertEqual(self.run_wizard("--validate", str(out)).returncode, 0)
            base = Path(directory) / "base.env"; base.write_text("BOAT_NAME='From Base'\nVESSELSTACK_USER=daemon\n")
            out2 = Path(directory) / "merged.env"
            result = self.run_wizard("--base", str(base), "--set", "BOAT_TYPE=Yacht", "--output", str(out2))
            self.assertEqual(result.returncode, 0, result.stderr)
            values = wizard.load_env(out2)
            self.assertEqual((values["BOAT_NAME"], values["VESSELSTACK_USER"], values["BOAT_TYPE"]), ("From Base", "daemon", "Yacht"))

    def test_invalid_set_writes_nothing_and_does_not_echo_the_value(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "vesselstack.env"
            for bad in ("NOT_A_KEY=1", "SIGNALK_MODE=bogus", "HOME_ASSISTANT_TOKEN=bad;tok"):
                result = self.run_wizard("--defaults", "--set", bad, "--output", str(out))
                self.assertEqual(result.returncode, 1, bad)
                self.assertFalse(out.exists())
                self.assertNotIn("bad;tok", result.stdout + result.stderr)
            result = self.run_wizard("--base", "/nonexistent/base.env", "--output", str(out))
            self.assertEqual(result.returncode, 1)

    def test_answers_from_a_descriptor_and_early_end(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "vesselstack.env"
            answers = Path(directory) / "answers"; answers.write_text("Piped Boat\n" + "\n" * 16 + "s\n")
            with open(answers, "rb") as handle:
                result = subprocess.run([sys.executable, "-I", str(ROOT / "vesselstack-wizard.py"), "--cli", "--input-fd", str(handle.fileno()), "--output", str(out)],
                                        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, pass_fds=(handle.fileno(),))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(wizard.load_env(out)["BOAT_NAME"], "Piped Boat")
            out.unlink()
            answers.write_text("Short\n")
            with open(answers, "rb") as handle:
                result = subprocess.run([sys.executable, "-I", str(ROOT / "vesselstack-wizard.py"), "--cli", "--input-fd", str(handle.fileno()), "--output", str(out)],
                                        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, pass_fds=(handle.fileno(),))
            self.assertEqual(result.returncode, 1)
            self.assertFalse(out.exists())
            self.assertIn("answers ended", result.stdout)

@unittest.skipUnless(hasattr(os, "fork") and sys.platform != "win32", "needs a pseudo-terminal")
class CliPtyTests(unittest.TestCase):
    """Runs the real wizard on a real pseudo-terminal, as it runs over SSH."""
    SECRET = "S3cr3tToken-abcdef.123"

    def test_typed_secret_is_not_echoed_and_echo_is_restored(self):
        import pty, termios
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "vesselstack.env"
            pid, master = pty.fork()
            if pid == 0:
                os.execv(sys.executable, [sys.executable, "-I", str(ROOT / "vesselstack-wizard.py"), "--cli", "--output", str(out)])
            captured = bytearray(); echo_after = None; sent_secret = False; deadline = time.time() + 60
            try:
                while time.time() < deadline:
                    ready, _, _ = select.select([master], [], [], 5)
                    if not ready: continue
                    try: chunk = os.read(master, 4096)
                    except OSError: break
                    if not chunk: break
                    captured += chunk
                    tail = captured[-300:].decode("utf-8", "replace")
                    if not tail.endswith((": ", "? ")): continue
                    last = tail.splitlines()[-1] if tail.splitlines() else tail
                    if "access token (optional)" in last and not sent_secret:
                        os.write(master, (self.SECRET + "\n").encode()); sent_secret = True; continue
                    if sent_secret and echo_after is None and "Boat Chat language model" in last:
                        try: echo_after = bool(termios.tcgetattr(master)[3] & termios.ECHO)
                        except termios.error: echo_after = None
                        if echo_after is None: echo_after = "unknown"
                    if "[s] save" in last: os.write(master, b"s\n")
                    else: os.write(master, b"\n")
            finally:
                try: os.close(master)
                except OSError: pass
                _, status = os.waitpid(pid, 0)
            text = captured.decode("utf-8", "replace")
            self.assertTrue(sent_secret, text[-500:])
            self.assertEqual(os.waitstatus_to_exitcode(status), 0, text[-800:])
            self.assertNotIn(self.SECRET, text)
            self.assertIn(echo_after, (True, "unknown"))
            self.assertEqual(wizard.load_env(out)["HOME_ASSISTANT_TOKEN"], self.SECRET)
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
            self.assertIn("set (hidden)", text)

if __name__ == "__main__": unittest.main()
