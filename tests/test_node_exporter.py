import json
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
