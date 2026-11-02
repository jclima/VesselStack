import json
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


class NodeExporterTemplateTests(unittest.TestCase):
    def test_system_health_host_panels_use_prometheus_datasource(self):
        dashboard = json.loads((ROOT / "templates/grafana/dashboards/system-health.json").read_text())
        panels = {panel["id"]: panel for panel in dashboard["panels"]}
        expected = {"type": "prometheus", "uid": "vesselstack-prometheus"}
        for panel_id in range(2, 6):
            self.assertEqual(panels[panel_id]["datasource"], expected)

    def test_exporter_is_optional_hardened_and_internal_only(self):
        compose = (ROOT / "templates/compose.yml").read_text()
        service = compose[compose.index("  node-exporter:"):compose.index("  mosquitto:")]
        self.assertIn('profiles: ["metrics"]', service)
        self.assertIn("quay.io/prometheus/node-exporter:v1.10.2", service)
        self.assertNotIn("ports:", service)
        self.assertIn("pid: host", service)
        self.assertIn("read_only: true", service)
        self.assertIn("cap_drop: [\"ALL\"]", service)
        self.assertIn("no-new-privileges:true", service)
        self.assertIn("--collector.disable-defaults", service)

    def test_prometheus_switches_scrape_config(self):
        self.assertNotIn("host.docker.internal", (ROOT / "templates/prometheus.yml").read_text())
        self.assertIn("node-exporter:9100", (ROOT / "templates/prometheus-node-exporter.yml").read_text())

    def test_production_sync_applies_rendered_config_by_stamp(self):
        """Exercise the real shell function, with Docker isolated behind a fake binary."""
        source = (ROOT / "scripts/vesselstackctl").read_text()
        start = source.index("write_prometheus_stamp() {")
        end = source.index("persist_disabled_components() {")
        sync_function = source[start:end]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "app"
            templates = root / "installer/templates"
            config = root / "config"
            templates.mkdir(parents=True)
            config.mkdir()
            base = ROOT / "templates/prometheus.yml"
            node = ROOT / "templates/prometheus-node-exporter.yml"
            shutil.copy2(base, templates / "prometheus.yml")
            shutil.copy2(node, templates / "prometheus-node-exporter.yml")
            docker = Path(temp) / "docker"
            docker.write_text(
                "#!/bin/bash\n"
                "printf '%s\\n' \"$*\" >> \"$FAKE_DOCKER_LOG\"\n"
                "if [ \"${FAKE_DOCKER_FAIL:-0}\" = 1 ]; then\n"
                "  exit 42\n"
                "fi\n"
                "exit 0\n"
            )
            docker.chmod(0o755)
            stamp = config / ".prometheus-conf.sha256"

            def digest(path):
                return hashlib.sha256(path.read_bytes()).hexdigest()

            def run(flag, disabled="", fail=False):
                script = f'''{sync_function}
'''
                env = os.environ.copy()
                env.update(
                    VESSELSTACK_ROOT=str(root),
                    NODE_EXPORTER_ENABLE=flag,
                    VESSELSTACK_DISABLED_COMPONENTS=disabled,
                    FAKE_DOCKER_LOG=str(Path(temp) / "docker.log"),
                    FAKE_DOCKER_FAIL="1" if fail else "0",
                    COMPOSE="",  # assigned by the shell prelude below
                )
                return subprocess.run(
                    ["bash", "-c", "set -euo pipefail\nCOMPOSE=(docker compose)\n" + script + "sync_prometheus_config || exit 1\n"],
                    env={**env, "PATH": f"{temp}:{env['PATH']}"},
                    text=True,
                    capture_output=True,
                )

            # A render-only change (simulated by copying the new rendered file)
            # forces the running Prometheus container and stamps the actual file.
            rendered = config / "prometheus.yml"
            shutil.copy2(base, rendered)
            stamp.write_text(digest(rendered) + "\n")
            shutil.copy2(node, rendered)
            result = run("true")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--force-recreate prometheus", (Path(temp) / "docker.log").read_text())
            self.assertEqual(stamp.read_text().strip(), digest(rendered))
            self.assertFalse(list(config.glob(".prometheus-conf.sha256.tmp.*")))

            # Switching back is another real config transition; unchanged starts are idempotent.
            shutil.copy2(base, rendered)
            result = run("false")
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (Path(temp) / "docker.log").read_text().splitlines()
            self.assertEqual(len(calls), 2)
            before = len(calls)
            self.assertEqual(run("false").returncode, 0)
            self.assertEqual(len((Path(temp) / "docker.log").read_text().splitlines()), before)

            # A missing legacy stamp forces exactly one apply, then becomes stable.
            stamp.unlink()
            self.assertEqual(run("false").returncode, 0)
            self.assertEqual(len((Path(temp) / "docker.log").read_text().splitlines()), before + 1)
            self.assertEqual(run("false").returncode, 0)
            self.assertEqual(len((Path(temp) / "docker.log").read_text().splitlines()), before + 1)

            # Disabled Prometheus neither recreates nor writes a stamp.
            stamp.unlink()
            self.assertEqual(run("true", "prometheus").returncode, 0)
            self.assertFalse(stamp.exists())
            self.assertEqual(len((Path(temp) / "docker.log").read_text().splitlines()), before + 1)

            # A failed targeted recreate leaves the old stamp; retry applies it.
            stamp.write_text("old\n")
            failure = run("true", fail=True)
            self.assertNotEqual(failure.returncode, 0, failure.stderr + (Path(temp) / "docker.log").read_text())
            self.assertEqual(stamp.read_text(), "old\n")
            self.assertEqual(run("true").returncode, 0)
            self.assertEqual(stamp.read_text().strip(), digest(rendered))


if __name__ == "__main__":
    unittest.main()
