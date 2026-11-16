import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cv_under_test", ROOT / "common" / "component_versions.py")
cv = importlib.util.module_from_spec(spec)
sys.modules["cv_under_test"] = cv
spec.loader.exec_module(cv)
from common import config_registry as registry  # noqa: E402

COMPOSE = (ROOT / "templates" / "compose.yml").read_text()
DIGEST = "sha256:19c5c09184458a41b7b5915e71463a39f36cd33540f41bdbed008791b8182d5f"

TABLE = {
    "signalk": ("SIGNALK_VERSION", "cr.signalk.io/signalk/signalk-server", "v2.27.0"),
    "ais-catcher": ("AIS_IMAGE", "ghcr.io/jvde-github/ais-catcher", "ghcr.io/jvde-github/ais-catcher:v0.70"),
    "nodered": ("NODERED_VERSION", "nodered/node-red", "5.0.9-debian"),
    "homeassistant": ("HOMEASSISTANT_VERSION", "homeassistant/home-assistant", "2026.6.4"),
    "influxdb": ("INFLUXDB_VERSION", "influxdb", "2.9.1"),
    "grafana": ("GRAFANA_VERSION", "grafana/grafana", "13.0.1"),
    "prometheus": ("PROMETHEUS_VERSION", "prom/prometheus", "v2.55.0"),
    "node-exporter": ("NODE_EXPORTER_VERSION", "quay.io/prometheus/node-exporter", "v1.10.2"),
    "mosquitto": ("MOSQUITTO_VERSION", "eclipse-mosquitto", "2.1.2-alpine"),
    "homepage": ("HOMEPAGE_VERSION", "ghcr.io/gethomepage/homepage", "v2.7.0@" + DIGEST),
}


class TableTests(unittest.TestCase):
    def test_the_component_table(self):
        self.assertEqual(cv.components(), list(TABLE))
        for cid, (setting, repository, default) in TABLE.items():
            with self.subTest(cid=cid):
                self.assertEqual(cv.BY_ID[cid].setting, setting)
                self.assertEqual(cv.BY_ID[cid].repository, repository)
                self.assertEqual(cv.default(cid), default)
                self.assertIsNone(cv.validate(cid, ""))
                self.assertIsNone(cv.validate(cid, default))
                self.assertEqual(cv.compare(cid, ""), "default")
                self.assertEqual(cv.compare(cid, default), "default")

    def test_every_component_image_in_compose_uses_the_default_form_with_the_module_default(self):
        images = re.findall(r"^\s*image:\s*(\S+)\s*$", COMPOSE, re.M)
        self.assertEqual(len(images), len(TABLE))
        for cid, comp in cv.BY_ID.items():
            if cid == "ais-catcher":
                expected = "${AIS_IMAGE:-" + comp.default + "}"
            else:
                expected = f"{comp.repository}:${{{comp.setting}:-{comp.default}}}"
            self.assertIn(expected, images, cid)
            self.assertEqual(cv.effective(cid, ""), expected.replace("${" + comp.setting + ":-", "").rstrip("}") if cid == "ais-catcher" else f"{comp.repository}:{comp.default}")

    def test_compose_images_are_unchanged_with_no_overrides(self):
        """The defaults substituted into the template give the images the template named before this change."""
        before = subprocess.run(["git", "show", "HEAD:templates/compose.yml"], cwd=ROOT, capture_output=True, text=True)
        if before.returncode != 0 or "SIGNALK_VERSION:-" in before.stdout:
            self.skipTest("the previous template is not available")
        old = re.findall(r"^\s*image:\s*(\S+)\s*$", before.stdout, re.M)
        old = [i.replace("${SIGNALK_VERSION}", "v2.27.0").replace("${AIS_IMAGE}", "ghcr.io/jvde-github/ais-catcher:v0.70") for i in old]
        new = [re.sub(r"\$\{[A-Z_]+:-([^}]*)\}", r"\1", i) for i in re.findall(r"^\s*image:\s*(\S+)\s*$", COMPOSE, re.M)]
        self.assertEqual(sorted(new), sorted(old))


class ValidateTests(unittest.TestCase):
    def bad(self, cid, value, text=None):
        message = cv.validate(cid, value)
        self.assertIsNotNone(message, f"{cid} {value!r} should be refused")
        self.assertIn(cv.BY_ID[cid].setting, message)
        if text:
            self.assertIn(text, message)

    def test_accepts(self):
        for cid, value in (("signalk", "v2.28.1"), ("nodered", "5.1.0-debian"), ("homeassistant", "2026.12.0"), ("influxdb", "2.10.0"),
                           ("grafana", "13.2.0"), ("prometheus", "v2.60.0"), ("node-exporter", "v1.11.0"), ("mosquitto", "2.1.3-alpine"),
                           ("mosquitto", "2.2.0-alpine"), ("homepage", "v2.8.0"), ("homepage", "v2.8.0@" + DIGEST),
                           ("grafana", "13.0.1@" + DIGEST), ("ais-catcher", "ghcr.io/jvde-github/ais-catcher:v0.71"),
                           ("ais-catcher", "registry.example.net:5000/team/ais:1.2@" + DIGEST)):
            with self.subTest(cid=cid, value=value):
                self.assertIsNone(cv.validate(cid, value))

    def test_floating_tags_are_refused(self):
        for cid in cv.components():
            for tag in ("latest", "stable", "main", "edge", "nightly"):
                value = f"ghcr.io/x/y:{tag}" if cid == "ais-catcher" else tag
                with self.subTest(cid=cid, tag=tag):
                    self.bad(cid, value)
        self.bad("ais-catcher", "ghcr.io/x/y:abc")  # no digit at all

    def test_another_repository_or_registry_or_whitespace(self):
        for cid in cv.components():
            if cid == "ais-catcher":
                continue
            for value in ("evil.example/x:13.0.1", "x/13.0.1", "13.0.1 ", " 13.0.1", "13.0.1 --privileged", "13.0.1\t", "a:b"):
                with self.subTest(cid=cid, value=value):
                    self.bad(cid, value)
        self.bad("ais-catcher", "ghcr.io/x/y:v1 --privileged")
        self.bad("ais-catcher", "ghcr.io/x/y")
        self.bad("ais-catcher", "GHCR.IO/X/y:v1")

    def test_rules_per_component(self):
        self.bad("signalk", "v3.0.0")
        self.bad("signalk", "2.28.0")
        self.bad("grafana", "12.4.0")
        self.bad("grafana", "14.0.0")
        self.bad("influxdb", "3.0.0")
        self.bad("prometheus", "v3.0.0")
        self.bad("node-exporter", "v2.0.0")
        self.bad("nodered", "5.1.0", "-debian")
        self.bad("nodered", "6.0.0-debian")
        self.bad("mosquitto", "2.1.3", "-alpine")
        self.bad("mosquitto", "2.0.9-alpine", "2.1.2-alpine")
        self.bad("mosquitto", "3.0.0-alpine")
        self.bad("homeassistant", "2026.7")
        self.bad("homeassistant", "v2026.7.0")
        self.bad("homepage", "v3.0.0")
        self.bad("grafana", "13.1.0@sha256:abc")
        self.bad("grafana", "13.1.0@" + DIGEST.upper())

    def test_messages_name_the_setting_and_an_example(self):
        message = cv.validate("grafana", "latest")
        self.assertIn("GRAFANA_VERSION", message)
        self.assertIn("13.0.2", message)
        self.assertIn("empty", message)


class EffectiveCompareTests(unittest.TestCase):
    def test_effective(self):
        self.assertEqual(cv.effective("grafana", ""), "grafana/grafana:13.0.1")
        self.assertEqual(cv.effective("grafana", "13.2.0"), "grafana/grafana:13.2.0")
        self.assertEqual(cv.effective("ais-catcher", ""), "ghcr.io/jvde-github/ais-catcher:v0.70")
        self.assertEqual(cv.effective("ais-catcher", "ghcr.io/jvde-github/ais-catcher:v0.71"), "ghcr.io/jvde-github/ais-catcher:v0.71")
        self.assertEqual(cv.effective("homepage", ""), "ghcr.io/gethomepage/homepage:v2.7.0@" + DIGEST)
        with self.assertRaises(ValueError):
            cv.effective("grafana", "latest")

    def test_compare_is_version_aware(self):
        self.assertEqual(cv.compare("grafana", "13.0.10"), "newer")
        self.assertEqual(cv.compare("grafana", "13.0.0"), "older")
        self.assertEqual(cv.compare("signalk", "v2.100.0"), "newer")
        self.assertEqual(cv.compare("signalk", "v2.9.0"), "older")
        self.assertEqual(cv.compare("homeassistant", "2026.10.0"), "newer")
        self.assertEqual(cv.compare("homeassistant", "2026.6.3"), "older")
        self.assertEqual(cv.compare("mosquitto", "2.1.10-alpine"), "newer")
        self.assertEqual(cv.compare("ais-catcher", "ghcr.io/jvde-github/ais-catcher:v0.9"), "older")
        self.assertEqual(cv.compare("ais-catcher", "ghcr.io/jvde-github/ais-catcher:v0.71"), "newer")
        self.assertEqual(cv.compare("ais-catcher", "ghcr.io/other/ais:v9"), "different")
        self.assertEqual(cv.compare("homepage", "v2.7.0"), "different")  # same version, not the pinned build
        self.assertEqual(cv.compare("grafana", "latest"), "different")


class CliTests(unittest.TestCase):
    def run_cli(self, *args, env=None):
        return subprocess.run([sys.executable, "-I", str(ROOT / "common" / "component_versions.py"), *args], capture_output=True, text=True, env=env)

    def test_list_json_validate_effective(self):
        out = self.run_cli("--list")
        self.assertEqual(out.returncode, 0)
        self.assertIn("grafana\tGRAFANA_VERSION\t13.0.1", out.stdout)
        data = json.loads(self.run_cli("--json").stdout)
        self.assertEqual([c["id"] for c in data["components"]], list(TABLE))
        self.assertEqual(self.run_cli("--validate", "grafana", "13.1.0").returncode, 0)
        bad = self.run_cli("--validate", "grafana", "latest")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("GRAFANA_VERSION", bad.stdout)
        self.assertEqual(self.run_cli("--effective", "influxdb", "").stdout.strip(), "influxdb:2.9.1")
        self.assertEqual(self.run_cli("--default", "signalk").stdout.strip(), "v2.27.0")
        self.assertEqual(self.run_cli("--compare", "grafana", "13.1.0").stdout.strip(), "newer")


class RegistryAndWizardTests(unittest.TestCase):
    def test_registry_settings_follow_the_module(self):
        for cid, comp in cv.BY_ID.items():
            spec = registry.SETTINGS[comp.setting]
            self.assertEqual(spec.default, "")
            self.assertFalse(spec.required)
            self.assertEqual(registry.validate_value(comp.setting, ""), "")
            good = "ghcr.io/jvde-github/ais-catcher:v0.71" if cid == "ais-catcher" else comp.example
            self.assertEqual(registry.validate_value(comp.setting, good), good)
            with self.assertRaises(ValueError):
                registry.validate_value(comp.setting, "latest")

    def test_installer_and_example_env_carry_the_keys(self):
        installer = (ROOT / "install.sh").read_text()
        example = (ROOT / "vesselstack.example.env").read_text()
        for comp in cv.COMPONENTS:
            self.assertRegex(installer, rf"(?m)^{comp.setting}$")
            self.assertRegex(example, rf'(?m)^{comp.setting}=""$')
        self.assertNotIn(': "${SIGNALK_VERSION:=', installer)
        self.assertNotIn(': "${AIS_IMAGE:=', installer)

    def test_wizard_has_a_prompt_for_each_setting(self):
        wspec = importlib.util.spec_from_file_location("wizard_cv_test", ROOT / "vesselstack-wizard.py")
        wizard = importlib.util.module_from_spec(wspec)
        wspec.loader.exec_module(wizard)
        for comp in cv.COMPONENTS:
            self.assertIn(comp.setting, wizard.FIELD_INDEX)
            with self.assertRaises(ValueError):
                wizard.validate({comp.setting: "latest"})
            self.assertEqual(wizard.validate({comp.setting: ""})[comp.setting], "")


class ParserTests(unittest.TestCase):
    def load(self, name, rel):
        s = importlib.util.spec_from_file_location(name, ROOT / rel)
        m = importlib.util.module_from_spec(s)
        sys.modules[name] = m
        s.loader.exec_module(m)
        return m

    def setUp(self):
        sys.path.insert(0, str(ROOT / "displays"))
        self.addCleanup(lambda: sys.path.remove(str(ROOT / "displays")))
        self.mqtt = self.load("mqtt_auth_cv_test", "displays/mqtt_auth.py")
        self.nr = self.load("nodered_cv_test", "scripts/configure-nodered.py")
        self.compose = ROOT / "templates" / "compose.yml"
        self._saved = {k: os.environ.pop(k, None) for k in ("MOSQUITTO_VERSION", "NODERED_VERSION")}
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_no_override_gives_todays_images(self):
        self.assertEqual(self.mqtt.find_image(ROOT, self.compose, {}), "eclipse-mosquitto:2.1.2-alpine")
        self.assertEqual(self.nr.find_image(self.compose), "nodered/node-red:5.0.9-debian")
        self.assertEqual(self.mqtt.find_image(ROOT, self.compose, {"MOSQUITTO_VERSION": ""}), "eclipse-mosquitto:2.1.2-alpine")

    def test_a_valid_override_is_used(self):
        self.assertEqual(self.mqtt.find_image(ROOT, self.compose, {"MOSQUITTO_VERSION": "2.1.3-alpine"}), "eclipse-mosquitto:2.1.3-alpine")
        os.environ["NODERED_VERSION"] = "5.0.10-debian"
        self.assertEqual(self.nr.find_image(self.compose), "nodered/node-red:5.0.10-debian")

    def test_override_is_read_from_the_stack_settings_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "vesselstack.env").write_text('MOSQUITTO_VERSION="2.1.4-alpine"\n')
            self.assertEqual(self.mqtt.find_image(root, self.compose), "eclipse-mosquitto:2.1.4-alpine")

    def test_an_invalid_value_is_refused_and_never_becomes_an_image_name(self):
        for bad in ("latest", "2.1.3-alpine --privileged", "evil/x:1", "2.0.1-alpine"):
            with self.subTest(bad=bad):
                with self.assertRaises(self.mqtt.MqttAuthError) as ctx:
                    self.mqtt.find_image(ROOT, self.compose, {"MOSQUITTO_VERSION": bad})
                self.assertIn("MOSQUITTO_VERSION", str(ctx.exception))
                os.environ["NODERED_VERSION"] = bad if "alpine" not in bad else "latest"
                with self.assertRaises(self.nr.Failure) as nctx:
                    self.nr.find_image(self.compose)
                self.assertIn("NODERED_VERSION", str(nctx.exception))


def shell_function(name):
    source = (ROOT / "scripts" / "vesselstackctl").read_text()
    start = source.index(f"{name}() {{")
    return source[start:source.index("\n}\n", start) + 3]


class SignalkConsumersTests(unittest.TestCase):
    def test_datapath_script_uses_the_release_default_for_an_empty_setting(self):
        s = importlib.util.spec_from_file_location("datapath_cv_test", ROOT / "scripts" / "configure-signalk-datapath.py")
        m = importlib.util.module_from_spec(s)
        sys.modules["datapath_cv_test"] = m
        s.loader.exec_module(m)
        self.assertEqual(m.signalk_tag(""), "v2.27.0")
        self.assertEqual(m.signalk_tag("v2.30.1"), "v2.30.1")
        with self.assertRaises(m.StepFailed):
            m.signalk_tag("latest")

    def test_native_install_script_resolves_the_effective_version(self):
        text = (ROOT / "scripts" / "install-signalk-native.sh").read_text()
        self.assertIn('--default signalk', text)
        self.assertIn('signalk-server@${signalk_version#v}', text)
        self.assertNotIn('${SIGNALK_VERSION#v}', text)


class ControlCommandTests(unittest.TestCase):
    def test_component_versions_is_allowed_without_the_lifecycle_lock(self):
        func = shell_function("command_needs_lifecycle_lock")
        script = func + '\nfor args in "component versions" "component versions --json" "component add grafana" "component remove grafana"; do\n' \
                        '  command_needs_lifecycle_lock $args && echo "lock $args" || echo "free $args"\ndone\n'
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(out, ["free component versions", "free component versions --json", "lock component add grafana", "lock component remove grafana"])

    def fake_docker(self, tmp, table):
        bin_dir = Path(tmp) / "bin"
        bin_dir.mkdir()
        lines = "\n".join(table)
        (bin_dir / "docker").write_text(
            "#!/bin/bash\nname=${@: -1}\nwhile IFS='|' read -r n image iid running; do\n"
            "  if [ \"$n\" = \"$name\" ]; then echo \"$image|$iid|$running\"; exit 0; fi\n"
            f"done <<'T'\n{lines}\nT\n"
            "echo \"Error: No such object: $name\" >&2; exit 1\n")
        (bin_dir / "docker").chmod(0o755)
        return bin_dir

    def run_report(self, env_text, table, fmt="table", docker=True):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "vesselstack.env"
            env_file.write_text(env_text)
            env = dict(os.environ)
            env["PATH"] = (str(self.fake_docker(tmp, table)) + ":" if docker else "") + ("/usr/bin:/bin" if not docker else env["PATH"])
            if not docker:
                env["PATH"] = str(Path(tmp) / "empty")
            result = subprocess.run([sys.executable, "-I", str(ROOT / "common" / "component_versions.py"), "--report", "--format", fmt, "--env-file", str(env_file)],
                                    capture_output=True, text=True, env=env)
            return result

    BASE = 'SIGNALK_MODE="docker"\nGRAFANA_VERSION=""\n'

    def test_no_override_means_no_doctor_or_status_line(self):
        table = ["vesselstack-grafana|grafana/grafana:13.0.1|sha256:a|true"]
        for fmt in ("doctor", "status"):
            r = self.run_report(self.BASE, table, fmt)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_override_newer_and_older(self):
        table = ["vesselstack-grafana|grafana/grafana:13.1.0|sha256:a|true", "vesselstack-influxdb|influxdb:2.8.0|sha256:b|true"]
        env = self.BASE + 'GRAFANA_VERSION="13.1.0"\nINFLUXDB_VERSION="2.8.0"\n'
        rows = {r["id"]: r for r in json.loads(self.run_report(env, table, "json").stdout)["components"]}
        self.assertEqual(rows["grafana"]["state"], "override (newer)")
        self.assertEqual(rows["influxdb"]["state"], "override (older)")
        self.assertEqual(rows["grafana"]["running_image"], "grafana/grafana:13.1.0")
        self.assertEqual(rows["prometheus"]["state"], "release default")
        doctor = self.run_report(env, table, "doctor")
        self.assertEqual(doctor.returncode, 0)
        self.assertIn("WARN Grafana 13.1.0 is set; the release default is 13.0.1. This version is not tested with this release.", doctor.stdout)
        status = self.run_report(env, table, "status")
        self.assertIn("2 not at the release default; see: sudo vesselstackctl component versions", status.stdout)
        text = self.run_report(env, table).stdout
        self.assertIn("override (newer)", text)
        self.assertIn("upgrade one way", text)

    def test_drift_is_a_failure_when_running_and_info_when_stopped(self):
        env = self.BASE
        running = self.run_report(env, ["vesselstack-grafana|grafana/grafana:13.0.0|sha256:a|true"], "doctor")
        self.assertEqual(running.returncode, 1)
        self.assertIn("FAIL Grafana is running grafana/grafana:13.0.0 but grafana/grafana:13.0.1 is configured", running.stderr)
        stopped = self.run_report(env, ["vesselstack-grafana|grafana/grafana:13.0.0|sha256:a|false"], "doctor")
        self.assertEqual(stopped.returncode, 0)
        self.assertIn("INFO Grafana is running", stopped.stdout)
        data = json.loads(self.run_report(env, ["vesselstack-grafana|grafana/grafana:13.0.0|sha256:a|true"], "json").stdout)
        self.assertEqual([r["state"] for r in data["components"] if r["id"] == "grafana"], ["drift"])

    def test_docker_absent_and_component_disabled(self):
        env = self.BASE + 'VESSELSTACK_DISABLED_COMPONENTS="grafana"\nGRAFANA_VERSION="13.1.0"\n'
        r = self.run_report(env, [], "json", docker=False)
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        self.assertEqual(data["docker"], "unavailable")
        rows = {x["id"]: x for x in data["components"]}
        self.assertEqual(rows["grafana"]["running_state"], "not enabled")
        self.assertEqual(rows["grafana"]["state"], "override (newer)")
        self.assertEqual(rows["influxdb"]["running_state"], "unknown")
        self.assertIn("what is running is unknown", self.run_report(env, [], "table", docker=False).stdout)


if __name__ == "__main__":
    unittest.main()
