import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = ROOT / "templates/grafana/dashboards"
PLACEHOLDER = "__VESSELSTACK_RAW_BUCKET__"
BUCKET_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# In the raw file the quotes are JSON-escaped.
BUCKET_REF = r'from\(bucket: \\"([^"\\]*)\\"\)'


def load(name):
    return json.loads((DASHBOARDS / name).read_text())


def leaf_panels(dashboard):
    for panel in dashboard["panels"]:
        yield from panel.get("panels", [])
        if panel["type"] != "row":
            yield panel


class GrafanaDashboardTests(unittest.TestCase):
    def test_every_dashboard_is_valid_json_with_a_unique_uid_and_title(self):
        files = sorted(DASHBOARDS.glob("*.json"))
        self.assertIn("boat-overview.json", [f.name for f in files])
        dashboards = [json.loads(f.read_text()) for f in files]
        uids = [d["uid"] for d in dashboards]
        self.assertEqual(len(uids), len(set(uids)))
        self.assertEqual(len({d["title"] for d in dashboards}), len(dashboards))
        self.assertIn("vesselstack-boat-overview", uids)

    def test_panel_ids_are_unique_and_the_layout_does_not_overlap(self):
        panels = load("boat-overview.json")["panels"]
        ids = [p["id"] for p in panels]
        self.assertEqual(len(ids), len(set(ids)))
        cells = set()
        for panel in panels:
            pos = panel["gridPos"]
            self.assertLessEqual(pos["x"] + pos["w"], 24)
            for x in range(pos["x"], pos["x"] + pos["w"]):
                for y in range(pos["y"], pos["y"] + pos["h"]):
                    self.assertNotIn((x, y), cells, panel["title"])
                    cells.add((x, y))

    def test_datasource_is_the_provisioned_influx_uid_and_queries_are_flux(self):
        provisioned = (ROOT / "templates/grafana/provisioning/datasources/vesselstack.yml").read_text()
        self.assertIn("uid: vesselstack-influx", provisioned)
        self.assertIn("version: Flux", provisioned)
        dashboard = load("boat-overview.json")
        panels = list(leaf_panels(dashboard))
        self.assertGreaterEqual(len(panels), 12)
        for panel in panels:
            self.assertEqual(panel["datasource"], {"type": "influxdb", "uid": "vesselstack-influx"}, panel["title"])
            for target in panel["targets"]:
                self.assertEqual(target["datasource"], panel["datasource"])
                query = target["query"]
                self.assertIn(f'from(bucket: "{PLACEHOLDER}")', query)
                self.assertIn('r.self == "true"', query)
                self.assertIn('r._field == "value"', query)
                self.assertRegex(query, r"r\._measurement =~ /\^.*\$/")
                self.assertIn("aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)", query)
                self.assertNotIn("SELECT", query.upper().split("|>")[0])

    def test_no_data_is_explained_on_every_panel(self):
        for panel in leaf_panels(load("boat-overview.json")):
            self.assertEqual(panel["fieldConfig"]["defaults"]["noValue"], "No data yet", panel["title"])

    def test_requested_rows_and_signals_are_present(self):
        dashboard = load("boat-overview.json")
        rows = [p["title"] for p in dashboard["panels"] if p["type"] == "row"]
        self.assertEqual(rows, ["Batteries", "Tanks", "Engine", "Navigation", "Environment"])
        queries = "\n".join(t["query"] for p in leaf_panels(dashboard) for t in p["targets"])
        for path in ("voltage", "current", "stateOfCharge", "currentLevel", "revolutions", "temperature", "oilPressure",
                     "speedOverGround", "courseOverGround", "belowTransducer", "speedApparent", "angleApparent", "pressure"):
            self.assertIn(path, queries)
        by_title = {p["title"]: p for p in leaf_panels(dashboard)}
        # Signal K is SI: radians and kelvin are converted for display.
        self.assertIn("57.29578", by_title["Course over ground"]["targets"][0]["query"])
        self.assertEqual(by_title["Course over ground"]["fieldConfig"]["defaults"]["unit"], "degree")
        self.assertIn("273.15", by_title["Outside temperature"]["targets"][0]["query"])
        self.assertEqual(by_title["Outside temperature"]["fieldConfig"]["defaults"]["unit"], "celsius")

    def test_position_data_is_never_shown(self):
        text = (DASHBOARDS / "boat-overview.json").read_text()
        self.assertNotIn("navigation.position", text)
        self.assertNotIn("navigation\\\\.position", text)
        self.assertNotIn("geomap", text)
        self.assertNotIn('"lat"', text)
        self.assertNotIn('"lon"', text)

    def test_placeholder_is_the_only_bucket_reference_and_substitution_is_complete(self):
        text = (DASHBOARDS / "boat-overview.json").read_text()
        self.assertEqual(len(re.findall(BUCKET_REF, text)), text.count(PLACEHOLDER))
        self.assertEqual(set(re.findall(BUCKET_REF, text)), {PLACEHOLDER})
        rendered = text.replace(PLACEHOLDER, "boat_raw-1.2")
        self.assertNotIn(PLACEHOLDER, rendered)
        self.assertEqual(set(re.findall(BUCKET_REF, rendered)), {"boat_raw-1.2"})
        json.loads(rendered)

    def test_bucket_name_pattern_rejects_anything_that_could_alter_the_query(self):
        for good in ("signalk", "vesselstack_raw", "a.b-c_1", "x" * 64):
            self.assertRegex(good, BUCKET_RE)
        for bad in ("", "x" * 65, 'a"b', "a b", "a/b", "a\\b", "a&b", "a|b", "a\nb", "a$(x)", "é"):
            self.assertNotRegex(bad, BUCKET_RE)

    def test_installer_validates_the_bucket_before_rendering_the_dashboard(self):
        installer = (ROOT / "install.sh").read_text()
        self.assertIn('[[ "$INFLUXDB_RAW_BUCKET" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]', installer)
        self.assertLess(installer.index("INFLUXDB_RAW_BUCKET must be 1 to 64"), installer.index("boat-overview.json"))
        self.assertIn("s/__VESSELSTACK_RAW_BUCKET__/$INFLUXDB_RAW_BUCKET/g", installer)
        # The same shell expression, run for real, on a hostile value.
        pattern = r'[[ "$1" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]'
        for value, ok in (("vesselstack_raw", True), ('x") |> drop() //', False), ("a/b", False)):
            result = subprocess.run(["bash", "-c", pattern, "bash", value])
            self.assertEqual(result.returncode == 0, ok, value)


if __name__ == "__main__":
    unittest.main()
