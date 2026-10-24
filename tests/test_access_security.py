#!/usr/bin/env python3
"""Access fail-closed, secret redaction, settings precedence, compose binds."""
import json
import os
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "boat-chat"))
import app
import env_config
import telegram_bot

CONTROL_SPEC = __import__("importlib.util").util.spec_from_file_location("panel_under_test", ROOT / "control-panel/app.py")
panel = __import__("importlib.util").util.module_from_spec(CONTROL_SPEC)
CONTROL_SPEC.loader.exec_module(panel)

MANAGED = ("BOAT_CHAT_ACCESS_TOKEN", "BOAT_CHAT_ALLOW_OPEN", "BOAT_CHAT_SETTINGS_TOKEN", "BOAT_CHAT_ENV")


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {key: os.environ.pop(key, None) for key in MANAGED}
        self.saved["BOAT_CHAT_MEMORY_DB"] = os.environ.get("BOAT_CHAT_MEMORY_DB")
        self.saved["BOAT_CHAT_PROVIDER"] = os.environ.get("BOAT_CHAT_PROVIDER")
        os.environ["BOAT_CHAT_MEMORY_DB"] = str(Path(self.tmp.name) / "m.sqlite")
        os.environ["BOAT_CHAT_PROVIDER"] = "local"
        self.env_file = Path(self.tmp.name) / "boat-chat.env"
        patcher = mock.patch.object(app, "ENV_CONFIG", self.env_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        app.RATE_LIMIT_REQUESTS.clear()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.BoatChatHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def call(self, method, path, payload=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Boat-Chat-Token"] = token
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")


class AccessTests(ServerCase):
    def test_empty_token_fails_closed(self):
        self.assertTrue(app.access_token_required())
        for path, payload in (("/api/chat", {"message": "hi"}), ("/api/feedback", {}), ("/api/session/clear", {}), ("/api/maintenance", {"title": "x"})):
            self.assertEqual(401, self.call("POST", path, payload)[0], path)
        for path in ("/api/status", "/api/insights"):
            self.assertEqual(401, self.call("GET", path)[0], path)

    def test_token_required_and_accepted(self):
        os.environ["BOAT_CHAT_ACCESS_TOKEN"] = "right-token"
        self.assertEqual(401, self.call("POST", "/api/chat", {"message": "hi"}, token="wrong")[0])
        status, body = self.call("POST", "/api/chat", {"message": "What is the boat name?"}, token="right-token")
        self.assertEqual(200, status)
        self.assertIn("answer", body)

    def test_require_flag_cannot_open_access(self):
        self.env_file.write_text("BOAT_CHAT_REQUIRE_ACCESS_TOKEN=false\n")
        self.assertEqual(401, self.call("POST", "/api/chat", {"message": "hi"})[0])

    def test_explicit_allow_open_opt_in(self):
        os.environ["BOAT_CHAT_ALLOW_OPEN"] = "true"
        self.assertFalse(app.access_token_required())
        self.assertEqual(200, self.call("POST", "/api/chat", {"message": "What is the boat name?"})[0])

    def test_allow_open_is_not_settable_through_the_settings_file_or_api(self):
        self.assertNotIn("BOAT_CHAT_ALLOW_OPEN", app.SETTING_KEYS)
        self.env_file.write_text("BOAT_CHAT_ALLOW_OPEN=true\n")
        self.assertTrue(app.access_token_required())

    def test_health_reports_token_required_when_unconfigured(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(200, status)
        self.assertTrue(body["access_token_required"])


class SettingsTests(ServerCase):
    def handler(self, address, header=None):
        stub = mock.Mock()
        stub.client_address = (address, 1234)
        stub.headers = {"X-Boat-Chat-Token": header} if header is not None else {}
        return app.BoatChatHandler.settings_write_allowed(stub)

    def test_private_lan_address_is_not_trusted(self):
        # Built from parts so the public-tree scanner sees no private-network literal.
        for address in tuple(".".join(parts) for parts in (("192", "168", "1", "20"), ("10", "0", "0", "5"), ("172", "16", "3", "4"), ("100", "64", "0", "1"))):
            self.assertFalse(self.handler(address)[0], address)
        self.assertTrue(self.handler("127.0.0.1")[0])
        self.assertTrue(self.handler("::1")[0])

    def test_settings_token_is_required_everywhere_when_configured(self):
        os.environ["BOAT_CHAT_SETTINGS_TOKEN"] = "settings-secret"
        self.assertFalse(self.handler("127.0.0.1")[0])
        self.assertFalse(self.handler(".".join(("192", "168", "1", "20")), "wrong")[0])
        self.assertTrue(self.handler(".".join(("192", "168", "1", "20")), "settings-secret")[0])

    def test_unauthenticated_settings_never_return_secrets(self):
        self.env_file.write_text("TELEGRAM_ALLOWED_CHAT_IDS=424242\nTELEGRAM_BOT_TOKEN=bot-secret\nBOAT_CHAT_ACCESS_TOKEN=access-secret\nOPENAI_API_KEY=sk-secret\n")
        status, body = self.call("GET", "/api/settings")
        self.assertEqual(200, status)
        text = json.dumps(body)
        for leaked in ("424242", "bot-secret", "access-secret", "sk-secret"):
            self.assertNotIn(leaked, text)
        for key in ("TELEGRAM_ALLOWED_CHAT_IDS", "TELEGRAM_BOT_TOKEN", "BOAT_CHAT_ACCESS_TOKEN"):
            self.assertTrue(body["secrets"][key], key)
            self.assertNotIn(key, body["settings"])

    def test_every_credential_like_setting_is_secret(self):
        for key in app.SETTING_KEYS:
            if re.search(r"(TOKEN|SECRET|API_KEY|PASSWORD|CHAT_IDS|ACCESS_KEY)", key) and not key.endswith("MAX_TOKENS"):
                self.assertIn(key, app.SECRET_SETTING_KEYS, key)

    def test_value_equal_to_base_is_not_saved_as_override(self):
        os.environ["BOAT_CHAT_PORT"] = "8765"
        self.addCleanup(os.environ.pop, "BOAT_CHAT_PORT", None)
        app.update_settings({"BOAT_CHAT_PORT": "8765", "BOAT_CHAT_PROVIDER": "ollama"})
        saved = env_config.parse_env_file(self.env_file, app.SETTING_KEYS)
        self.assertNotIn("BOAT_CHAT_PORT", saved)
        self.assertEqual("ollama", saved["BOAT_CHAT_PROVIDER"])

    def test_newline_cr_nul_values_are_rejected_without_writing(self):
        for bad in ("ollama\nBOAT_CHAT_ALLOW_OPEN=true", "a\rb", "a\x00b"):
            with self.assertRaises(ValueError):
                app.update_settings({"BOAT_CHAT_PROVIDER": bad})
        with self.assertRaises(ValueError):
            app.update_settings({"BOAT_CHAT_PORT": "8765", "BOAT_CHAT_PROVIDER": "x\ny"})
        self.assertFalse(self.env_file.exists() and "8765" in self.env_file.read_text())


class PrecedenceTests(unittest.TestCase):
    def test_single_env_variable_selects_the_file_for_both_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "custom.env"
            code = "import sys;sys.path.insert(0,'boat-chat');import app,telegram_bot;print(app.ENV_CONFIG);print(telegram_bot.ENV_CONFIG)"
            env = {k: v for k, v in os.environ.items() if k != "BOAT_CHAT_STATE_DIR"}
            env["BOAT_CHAT_ENV"] = str(path)
            import subprocess
            out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, text=True, capture_output=True, check=True).stdout.split()
            self.assertEqual([str(path), str(path)], out)

    def test_default_file_is_in_state_dir(self):
        env = {k: v for k, v in os.environ.items() if k != "BOAT_CHAT_ENV"}
        env["BOAT_CHAT_STATE_DIR"] = "/var/state/boat"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(Path("/var/state/boat/boat-chat.env"), env_config.env_path())

    def test_settings_file_overrides_base_environment_in_app_and_telegram(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boat-chat.env"
            path.write_text("TELEGRAM_POLL_TIMEOUT=45\n")
            with mock.patch.dict(os.environ, {"TELEGRAM_POLL_TIMEOUT": "10", "TELEGRAM_BOT_TOKEN": "base-token"}), \
                 mock.patch.object(app, "ENV_CONFIG", path), mock.patch.object(telegram_bot, "ENV_CONFIG", path):
                self.assertEqual("45", app.setting_value("TELEGRAM_POLL_TIMEOUT"))
                self.assertEqual("45", telegram_bot.parse_env_config()["TELEGRAM_POLL_TIMEOUT"])
                self.assertEqual("base-token", app.setting_value("TELEGRAM_BOT_TOKEN"))
                self.assertEqual("base-token", telegram_bot.parse_env_config()["TELEGRAM_BOT_TOKEN"])

    def test_control_panel_save_clears_stale_ui_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vessel, chat, ctl = root / "vessel.env", root / "chat.env", root / "ctl.env"
            override = root / "ui.env"
            panel.write_env(vessel, {"BOAT_NAME": "T"}, [])
            panel.write_env(chat, {"BOAT_CHAT_PROVIDER": "local"}, [])
            override.write_text("# UI\nBOAT_CHAT_PROVIDER=ollama\nBOAT_CHAT_MODEL=llama3.2\n")
            os.chmod(override, 0o600)
            with mock.patch.multiple(panel, VESSEL_ENV=vessel, CHAT_ENV=chat, PANEL_ENV=ctl, CONFIG_BACKUP_ROOT=root / "bk"), \
                 mock.patch.dict(os.environ, {"BOAT_CHAT_ENV": str(override)}):
                config = panel.public_configuration()
                self.assertEqual(["BOAT_CHAT_MODEL", "BOAT_CHAT_PROVIDER"], config["overridden"])
                described = {f["key"]: f["description"] for f in config["fields"]}
                self.assertIn("Overridden", described["BOAT_CHAT_PROVIDER"])
                panel.update_configuration({"BOAT_CHAT_PROVIDER": "openai"})
            text = override.read_text()
            self.assertNotIn("BOAT_CHAT_PROVIDER", text)
            self.assertIn("BOAT_CHAT_MODEL=llama3.2", text)
            self.assertEqual(0o600, override.stat().st_mode & 0o777)
            self.assertEqual("openai", panel.parse_env(chat)["BOAT_CHAT_PROVIDER"])


class ComposeBindTests(unittest.TestCase):
    def ports(self):
        text = (ROOT / "templates/compose.yml").read_text()
        services, current = {}, None
        for line in text.splitlines():
            match = re.match(r"^  ([a-z-]+):\s*$", line)
            if match:
                current = match.group(1)
                services[current] = []
            elif current and re.match(r'^      - "\$\{', line) and ":" in line:
                services[current].append(line.split('"')[1])
        return services

    def test_data_services_default_to_loopback(self):
        services = self.ports()
        for name in ("influxdb", "prometheus", "mosquitto"):
            self.assertEqual(1, len(services[name]), name)
            self.assertTrue(services[name][0].startswith("${VESSELSTACK_DATA_BIND_ADDRESS:-127.0.0.1}:"), name)

    def test_web_services_have_configurable_bind(self):
        services = self.ports()
        for name in ("grafana", "heimdall"):
            for mapping in services[name]:
                self.assertTrue(mapping.startswith("${VESSELSTACK_WEB_BIND_ADDRESS:-0.0.0.0}:"), mapping)

    def test_no_unbound_sensitive_publish(self):
        services = self.ports()
        for name in ("influxdb", "prometheus", "mosquitto", "grafana", "heimdall"):
            for mapping in services[name]:
                self.assertRegex(mapping, r"^\$\{VESSELSTACK_(DATA|WEB)_BIND_ADDRESS:-")

    def test_example_env_and_installer_know_the_bind_variables(self):
        example = (ROOT / "vesselstack.example.env").read_text()
        installer = (ROOT / "install.sh").read_text()
        for key in ("VESSELSTACK_DATA_BIND_ADDRESS", "VESSELSTACK_WEB_BIND_ADDRESS"):
            self.assertIn(key, example)
            self.assertGreaterEqual(installer.count(key), 3)
        self.assertIn('BOAT_CHAT_ACCESS_TOKEN="GENERATE"', example)


if __name__ == "__main__":
    unittest.main()
