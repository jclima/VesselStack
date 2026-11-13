#!/usr/bin/env python3
"""The optional Node-RED component (README "Node-RED (optional)")."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "displays"))
sys.path.insert(0, str(ROOT / "tests"))

import mqtt_auth  # noqa: E402
from common import config_registry as registry  # noqa: E402
from test_displays_mqtt_auth import FakeRunner  # noqa: E402

HASH = "$2b$10$" + "a" * 53
HASH2 = "$2b$10$" + "b" * 53
COMPOSE = (ROOT / "templates" / "compose.yml").read_text()


def load_script():
    spec = importlib.util.spec_from_file_location("configure_nodered", ROOT / "scripts" / "configure-nodered.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nr = load_script()


def service_block(name: str) -> str:
    return COMPOSE.split(f"\n  {name}:\n", 1)[1].split("\n\n", 1)[0]


class ComposeTests(unittest.TestCase):
    def test_service_definition(self):
        block = service_block("nodered")
        self.assertIn('profiles: ["nodered"]', block)
        self.assertRegex(block, r"image: nodered/node-red:5\.0\.9-debian\n")
        self.assertIn("container_name: vesselstack-nodered", block)
        self.assertIn('user: "1000:1000"', block)
        self.assertIn("mem_limit: 384m", block)
        self.assertIn("restart: unless-stopped", block)
        self.assertIn('"${NODERED_BIND_ADDRESS:-127.0.0.1}:${NODERED_PORT:-1880}:1880"', block)
        self.assertIn('"host.docker.internal:host-gateway"', block)
        self.assertIn("${VESSELSTACK_DATA}/node-red:/data", block)
        self.assertIn("required: true", block)  # compose refuses to start it unprepared
        self.assertNotIn("required: false", block)
        # Fail closed: root-owned settings mounted read-only and passed explicitly.
        self.assertIn('command: ["--settings", "/vesselstack/settings.js"]', block)
        self.assertIn("source: ${VESSELSTACK_ROOT}/config/node-red/settings.js", block)
        self.assertIn("target: /vesselstack/settings.js", block)
        self.assertIn("read_only: true", block)
        self.assertIn("create_host_path: false", block)
        self.assertIn('cap_drop: ["ALL"]', block)
        self.assertNotIn("docker.sock", block)
        self.assertNotIn("privileged", block)

    @unittest.skipUnless(shutil.which("docker"), "docker is not installed")
    def test_compose_renders_with_and_without_the_profile(self):
        env = ROOT / "vesselstack.example.env"
        base = ["docker", "compose", "--env-file", str(env), "-f", str(ROOT / "templates" / "compose.yml")]
        off = subprocess.run(base + ["config", "--services"], capture_output=True, text=True)
        on = subprocess.run(base + ["--profile", "nodered", "config", "--no-env-resolution", "--services"], capture_output=True, text=True)
        if off.returncode != 0:
            self.skipTest("docker compose is not usable here: " + off.stderr[:80])
        self.assertNotIn("nodered", off.stdout.split())
        self.assertIn("nodered", on.stdout.split())
        for core in registry.CORE_COMPONENTS:  # nothing core depends on it
            self.assertIn(core, off.stdout.split())


class RegistryTests(unittest.TestCase):
    def test_optional_compose_component_not_core(self):
        self.assertIn("nodered", registry.COMPOSE_COMPONENTS)
        self.assertNotIn("nodered", registry.CORE_COMPONENTS)
        self.assertEqual(registry.DEFAULTS["NODERED_ENABLE"], "false")
        self.assertEqual(registry.DEFAULTS["NODERED_PORT"], "1880")
        self.assertEqual(registry.DEFAULTS["NODERED_BIND_ADDRESS"], "127.0.0.1")
        for key in ("NODERED_ADMIN_PASSWORD", "NODERED_ADMIN_PASSWORD_HASH", "NODERED_DASHBOARD_PASSWORD",
                    "NODERED_DASHBOARD_PASSWORD_HASH", "NODERED_CREDENTIAL_SECRET"):
            self.assertEqual(registry.SETTINGS[key].owner, "installer")
            self.assertEqual(registry.SETTINGS[key].component, "nodered")

    def test_validation(self):
        self.assertEqual(registry.validate_value("NODERED_PORT", "1880"), "1880")
        self.assertEqual(registry.validate_value("NODERED_BIND_ADDRESS", ""), "")
        self.assertEqual(registry.validate_value("NODERED_BIND_ADDRESS", "127.0.0.1"), "127.0.0.1")
        for key, bad in (("NODERED_PORT", "0"), ("NODERED_PORT", "x"), ("NODERED_ENABLE", "yes"), ("NODERED_BIND_ADDRESS", "host")):
            with self.assertRaises(ValueError):
                registry.validate_value(key, bad)

    def test_installer_and_example_env_carry_the_keys(self):
        installer = (ROOT / "install.sh").read_text()
        example = (ROOT / "vesselstack.example.env").read_text()
        for key in ("NODERED_ENABLE", "NODERED_PORT", "NODERED_BIND_ADDRESS", "NODERED_ADMIN_PASSWORD",
                    "NODERED_ADMIN_PASSWORD_HASH", "NODERED_DASHBOARD_PASSWORD", "NODERED_DASHBOARD_PASSWORD_HASH",
                    "NODERED_CREDENTIAL_SECRET"):
            self.assertRegex(installer, rf"(?m)^{key}$")
            self.assertRegex(example, rf"(?m)^{key}=")
        self.assertIn('NODERED_ENABLE="false"', example)
        self.assertIn(': "${NODERED_BIND_ADDRESS:=127.0.0.1}"', installer)
        self.assertIn('NODERED_BIND_ADDRESS="127.0.0.1"', example)
        self.assertIn("--https=$REMOTE_NODERED_SERVE_PORT", (ROOT / "scripts" / "vesselstackctl").read_text())


class MqttAccountTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "root"
        (self.root / "config").mkdir(parents=True)
        (self.root / "compose.yml").write_text("services:\n  mosquitto:\n    image: eclipse-mosquitto:9.9.9-alpine\n")
        self.mdir = self.root / "config" / mqtt_auth.AUTH_DIR_NAME

    def sync(self, **extra):
        env = {"MQTT_USERNAME": "homeassistant", "MQTT_PASSWORD": "admin-secret", "DISPLAYS_ENABLE": "false"}
        env.update(extra)
        return mqtt_auth.sync(self.root, None, env, runner=FakeRunner(), dir_owner=None, file_owner=None)

    def test_account_only_when_enabled_with_least_privilege_acl(self):
        self.sync()
        self.assertNotIn("nodered", (self.mdir / "acl").read_text())
        summary = self.sync(NODERED_ENABLE="true")
        self.assertTrue(summary["nodered"])
        acl = (self.mdir / "acl").read_text()
        self.assertIn("user vesselstack-nodered\ntopic readwrite vesselstack/nodered/#\ntopic read vesselstack/#\n", acl)
        # It must not receive the administrator's wildcard or write access outside its prefix.
        block = acl.split("user vesselstack-nodered\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(block.splitlines(), ["topic readwrite vesselstack/nodered/#", "topic read vesselstack/#"])
        users = [line.split(":")[0] for line in (self.mdir / "passwords").read_text().splitlines()]
        self.assertEqual(users, ["homeassistant", "vesselstack-nodered"])
        secrets = json.loads((self.root / "config" / mqtt_auth.SECRETS_NAME).read_text())
        self.assertGreaterEqual(len(secrets["nodered"]["mqtt_password"]), 32)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / "config" / mqtt_auth.SECRETS_NAME).st_mode), 0o600)

    def test_password_is_stable_and_account_removed_when_disabled(self):
        self.sync(NODERED_ENABLE="true")
        first = json.loads((self.root / "config" / mqtt_auth.SECRETS_NAME).read_text())["nodered"]["mqtt_password"]
        self.sync(NODERED_ENABLE="true")
        self.assertEqual(json.loads((self.root / "config" / mqtt_auth.SECRETS_NAME).read_text())["nodered"]["mqtt_password"], first)
        self.sync(NODERED_ENABLE="false")
        self.assertNotIn("nodered", (self.mdir / "acl").read_text())

    def test_callers_that_do_not_know_the_setting_read_it_from_vesselstack_env(self):
        self.sync(NODERED_ENABLE="true")
        env_file = self.root / "config" / "vesselstack.env"
        env_file.write_text("NODERED_ENABLE='true'\n")
        self.sync()  # e.g. an older vesselstackctl display command
        self.assertIn("user vesselstack-nodered", (self.mdir / "acl").read_text())
        env_file.write_text("NODERED_ENABLE=false\n")
        self.sync()  # the stored password alone no longer keeps the account
        self.assertNotIn("user vesselstack-nodered", (self.mdir / "acl").read_text())
        env_file.unlink()
        self.sync()
        self.assertNotIn("user vesselstack-nodered", (self.mdir / "acl").read_text())

    def test_invalid_setting_is_refused(self):
        with self.assertRaises(mqtt_auth.MqttAuthError):
            self.sync(NODERED_ENABLE="maybe")


class RenderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.root, self.data = tmp / "root", tmp / "data"
        (self.root / "config").mkdir(parents=True)
        self.data.mkdir()
        self.env_file = self.root / "config" / "vesselstack.env"
        self.write_env(NODERED_ENABLE="true")
        (self.root / "config" / "displays.secrets.json").write_text(
            json.dumps({"schema": 1, "bridge": {}, "displays": {}, "nodered": {"mqtt_password": "mqtt-pass_123"}}))
        self._chown = nr.os.chown
        nr.os.chown = lambda *a, **k: None
        self._fchown = nr.os.fchown
        nr.os.fchown = lambda *a, **k: None
        self.addCleanup(self._restore)

    def _restore(self):
        nr.os.chown = self._chown
        nr.os.fchown = self._fchown

    def write_env(self, **values):
        base = {"NODERED_ADMIN_PASSWORD_HASH": f"'{HASH}'", "NODERED_DASHBOARD_PASSWORD_HASH": f"'{HASH2}'", "NODERED_CREDENTIAL_SECRET": "s" * 64,
                "INFLUXDB_ORG": "vesselstack", "INFLUXDB_RAW_BUCKET": "signalk", "INFLUXDB_HISTORY_BUCKET": "signalk_1m",
                "SIGNALK_URL": "http://127.0.0.1:3000", "HOME_ASSISTANT_URL": "http://127.0.0.1:8123"}
        base.update(values)
        self.env_file.write_text("".join(f"{k}={v}\n" for k, v in base.items()))

    def render(self):
        return nr.main(["render", "--root", str(self.root), "--data", str(self.data), "--env-file", str(self.env_file),
                        "--settings-template", str(ROOT / "templates" / "node-red" / "settings.js")])

    def test_enabled_writes_private_env_and_seeds_settings_once(self):
        self.assertEqual(self.render(), 0)
        env_path = self.root / "config" / "nodered.env"
        self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)
        values = nr.read_env_file(env_path)
        self.assertEqual(values["MQTT_USER"], "vesselstack-nodered")
        self.assertEqual(values["MQTT_PASSWORD"], "mqtt-pass_123")
        self.assertEqual(values["MQTT_HOST"], "mosquitto")
        self.assertEqual(values["INFLUX_URL"], "http://influxdb:8086")
        self.assertEqual(values["INFLUX_HISTORY_BUCKET"], "signalk_1m")
        self.assertEqual(values["SIGNALK_WS_URL"], "ws://host.docker.internal:3000/signalk/v1/stream?subscribe=self")
        self.assertEqual(values["HOMEASSISTANT_URL"], "http://host.docker.internal:8123")
        self.assertEqual(values["NODERED_ADMIN_PASSWORD_HASH"], HASH)
        self.assertEqual(values["NODERED_DASHBOARD_PASSWORD_HASH"], HASH2)
        self.assertNotIn("NODERED_DASHBOARD_PASSWORD", values)
        self.assertNotIn("NODERED_ADMIN_PASSWORD", values)  # the plaintext never reaches the container
        self.assertEqual(values["INFLUX_TOKEN"], "")
        settings = self.root / "config" / "node-red" / "settings.js"
        self.assertEqual(stat.S_IMODE(settings.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(settings.parent.stat().st_mode), 0o755)
        self.assertFalse((self.data / "node-red" / "settings.js").exists())  # nothing in the container-writable dir
        settings.write_text("// owner edit\n")
        self.assertEqual(self.render(), 0)
        self.assertEqual(settings.read_text(), "// owner edit\n")

    def test_legacy_data_dir_settings_is_left_alone_and_noted(self):
        (self.data / "node-red").mkdir()
        legacy = self.data / "node-red" / "settings.js"
        legacy.write_text("// old\n")
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.render(), 0)
        self.assertEqual(legacy.read_text(), "// old\n")
        self.assertIn("is no longer used", out.getvalue())
        self.assertTrue((self.root / "config" / "node-red" / "settings.js").exists())

    def test_external_signalk_host_is_kept(self):
        self.write_env(NODERED_ENABLE="true", SIGNALK_URL="http://192.0.2.5:3000/signalk")
        self.assertEqual(self.render(), 0)
        self.assertEqual(nr.read_env_file(self.root / "config" / "nodered.env")["SIGNALK_WS_URL"],
                         "ws://192.0.2.5:3000/signalk/v1/stream?subscribe=self")

    def test_existing_influx_token_survives_a_rerender(self):
        self.assertEqual(self.render(), 0)
        path = self.root / "config" / "nodered.env"
        path.write_text(path.read_text().replace("INFLUX_TOKEN=''", "INFLUX_TOKEN='tok'"))
        self.assertEqual(self.render(), 0)
        self.assertEqual(nr.read_env_file(path)["INFLUX_TOKEN"], "tok")

    def test_disabled_removes_env_but_keeps_data(self):
        self.assertEqual(self.render(), 0)
        self.write_env(NODERED_ENABLE="false")
        self.assertEqual(self.render(), 0)
        self.assertFalse((self.root / "config" / "nodered.env").exists())
        self.assertTrue((self.root / "config" / "node-red" / "settings.js").exists())

    def test_refuses_symlinked_settings_and_bad_hash(self):
        (self.root / "config" / "node-red").mkdir()
        (self.root / "config" / "node-red" / "settings.js").symlink_to("/etc/passwd")
        self.assertEqual(self.render(), 1)
        (self.root / "config" / "node-red" / "settings.js").unlink()
        self.write_env(NODERED_ENABLE="true", NODERED_ADMIN_PASSWORD_HASH="'plain'")
        self.assertEqual(self.render(), 1)
        self.assertFalse((self.root / "config" / "nodered.env").exists())

    def test_package_json_is_read_safely(self):
        data = self.data / "node-red"
        data.mkdir()
        pkg = data / "package.json"
        pkg.write_text(json.dumps({"dependencies": {"a": "1"}}))
        self.assertEqual(nr.read_package_deps(data), {"a": "1"})
        pkg.unlink()
        pkg.symlink_to("/etc/passwd")
        self.assertEqual(nr.read_package_deps(data), {})
        pkg.unlink()
        pkg.write_text(" " * (nr.MAX_PACKAGE_JSON + 10) + "{}")
        self.assertEqual(nr.read_package_deps(data), {})
        pkg.unlink()
        os.mkfifo(pkg)  # must not block
        self.assertEqual(nr.read_package_deps(data), {})

    def test_values_that_cannot_be_quoted_are_refused(self):
        with self.assertRaises(nr.Failure):
            nr.quote("a'b")


class TemplateTests(unittest.TestCase):
    def test_settings_template_is_secure_by_default(self):
        text = (ROOT / "templates" / "node-red" / "settings.js").read_text()
        self.assertIn("user: 'crew', pass: dashboardHash", text)
        self.assertIn("delete process.env.NODERED_CREDENTIAL_SECRET", text)
        self.assertIn("delete process.env.NODERED_ADMIN_PASSWORD_HASH", text)
        for needle in ("adminAuth", "httpNodeAuth", "process.env.NODERED_ADMIN_PASSWORD_HASH", "process.env.NODERED_CREDENTIAL_SECRET",
                       "functionExternalModules: false", "projects: { enabled: false }",
                       "telemetry: { enabled: false", "diagnostics: { enabled: false", "allowInstall: true"):
            self.assertIn(needle, text)
        self.assertNotRegex(text, r"\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}")  # no hash baked into the file
        self.assertIn("throw new Error", text)  # never starts without a sign-in

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_settings_template_loads_and_fails_closed(self):
        script = ROOT / "templates" / "node-red" / "settings.js"
        run = lambda env: subprocess.run(["node", "-e", f"console.log(Object.keys(require({json.dumps(str(script))})).length)"],
                                         env={**os.environ, **env}, capture_output=True, text=True)
        self.assertNotEqual(run({"NODERED_ADMIN_PASSWORD_HASH": "", "NODERED_DASHBOARD_PASSWORD_HASH": "", "NODERED_CREDENTIAL_SECRET": ""}).returncode, 0)
        self.assertNotEqual(run({"NODERED_ADMIN_PASSWORD_HASH": HASH, "NODERED_DASHBOARD_PASSWORD_HASH": "", "NODERED_CREDENTIAL_SECRET": "x"}).returncode, 0)
        self.assertEqual(run({"NODERED_ADMIN_PASSWORD_HASH": HASH, "NODERED_DASHBOARD_PASSWORD_HASH": HASH2, "NODERED_CREDENTIAL_SECRET": "x"}).returncode, 0)

    def test_example_flows_use_environment_variables_only(self):
        text = (ROOT / "templates" / "node-red" / "vesselstack-example-flows.json").read_text()
        flows = json.loads(text)
        types = {node["type"] for node in flows}
        self.assertTrue({"mqtt in", "debug", "http request", "websocket in", "websocket-client", "ui-gauge", "mqtt-broker"} <= types)
        broker = next(node for node in flows if node["type"] == "mqtt-broker")
        self.assertEqual(broker["credentials"], {"user": "${MQTT_USER}", "password": "${MQTT_PASSWORD}"})
        self.assertIn("ws://host.docker.internal:3000/signalk/v1/stream?subscribe=self", text)
        self.assertIn("env.get('INFLUX_TOKEN')", text)
        self.assertEqual([n["id"] for n in flows if n["id"] in {m["id"] for m in flows if m is not n}], [])  # unique ids
        ids = {node["id"] for node in flows}
        for node in flows:
            for key in ("z", "broker", "client", "group", "page", "ui", "theme"):
                if node["type"] == "mqtt-broker" and key == "broker":
                    continue  # the broker host (an environment reference), not a node id
                if key in node and node[key]:
                    self.assertIn(node[key], ids, (node["id"], key))
            for wire in node.get("wires", []):
                for target in wire:
                    self.assertIn(target, ids)
        self.assertNotRegex(text, r"(?i)token['\"]?\s*:\s*['\"][A-Za-z0-9_=-]{20,}")

    def test_pinned_palette_and_image(self):
        self.assertEqual(nr.PALETTE, ("node-red-contrib-home-assistant-websocket@0.83.0", "@flowfuse/node-red-dashboard@1.36.1"))
        self.assertEqual(nr.find_image(ROOT / "templates" / "compose.yml"), "nodered/node-red:5.0.9-debian")

    def test_palette_install_never_runs_scripts_and_skips_offline(self):
        script = (ROOT / "scripts" / "configure-nodered.py").read_text()
        self.assertIn("--ignore-scripts", script)
        self.assertIn("--save-exact", script)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "node-red").mkdir()
            self.assertEqual(nr.main(["palette", "--data", tmp, "--offline", "--compose", str(ROOT / "templates" / "compose.yml")]), 0)


class FirewallAndDocsTests(unittest.TestCase):
    def test_readme_documents_the_feature(self):
        readme = (ROOT / "README.md").read_text()
        for needle in ("Node-RED (optional)", "NODERED_ENABLE", "NODERED_ADMIN_PASSWORD", "vesselstack-nodered"):
            self.assertIn(needle, readme)


if __name__ == "__main__":
    unittest.main()
