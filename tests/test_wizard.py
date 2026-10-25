import http.client
import importlib.util
import json
import threading
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

if __name__ == "__main__": unittest.main()
