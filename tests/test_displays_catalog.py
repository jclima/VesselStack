#!/usr/bin/env python3
"""displays/catalog.json and catalog.py: entries, units, live filtering, merging and materialising."""

from __future__ import annotations

import copy
import json
import socket
import sys
import threading
import time
import unittest
from unittest import mock
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "displays"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import catalog  # noqa: E402
import conversions  # noqa: E402
import fake_signalk  # noqa: E402
import render  # noqa: E402
import schema  # noqa: E402

BOARD = "waveshare-esp32-s3-touch-lcd-2.8c"
REGISTRY = schema.load_registry()
CATALOG = catalog.load_catalog()
PRIVATE = (fake_signalk.FAKE_MMSI, fake_signalk.FAKE_NAME, fake_signalk.FAKE_CALLSIGN, fake_signalk.FAKE_UUID,
           fake_signalk.FAKE_DESTINATION, "latitude", "longitude")


def base_config(**extra):
    config = {"schema": 1, "mqtt_host": "vesselstack.local", "heartbeat_s": 5, "instruments": {}, "displays": {}}
    config.update(extra)
    return config


def with_row(config, row, units, apply_thresholds=False):
    """Materialise ``row`` into ``config`` (in place) and return its id."""
    item_id, definition = catalog.materialise(row, config, units, apply_thresholds=apply_thresholds)
    section = "notifications" if row["kind"] == "notification" else "instruments"
    config.setdefault(section, {})[item_id] = definition
    return item_id


def valid(config):
    return schema.validate_config(config, REGISTRY) + render.check_glyphs(config)


class CatalogFileTests(unittest.TestCase):
    def test_file_is_valid_and_sized_as_intended(self):
        self.assertEqual(catalog.validate_catalog(json.loads((ROOT / "displays" / "catalog.json").read_text())), [])
        entries = CATALOG["entries"]
        self.assertGreaterEqual(len(entries), 30)
        self.assertEqual(len({e["key"] for e in entries}), len(entries))

    def test_every_icon_exists_and_every_conversion_exists(self):
        icons = schema.icon_map()
        for entry in CATALOG["entries"]:
            if "icon" in entry:
                self.assertIn(entry["icon"], icons, entry["key"])
        for quantity, systems in catalog.QUANTITIES.items():
            for system, (convert, unit) in systems.items():
                self.assertIn(convert, conversions.CONVERSIONS, (quantity, system))
                self.assertLessEqual(len(unit), 8)
                self.assertEqual(render.unsupported_chars(unit), [], (quantity, system))

    def test_validator_rejects_bad_entries(self):
        def broken(mutate):
            data = copy.deepcopy(CATALOG)
            mutate(data["entries"][0])
            return catalog.validate_catalog(data)
        self.assertTrue(broken(lambda e: e.update(icon="no_such_icon")))
        self.assertTrue(broken(lambda e: e.update(quantity="furlongs")))
        self.assertTrue(broken(lambda e: e.update(path="bad path")))
        self.assertTrue(broken(lambda e: e.update(extra=1)))
        self.assertTrue(broken(lambda e: e.update(label="{1}")))
        self.assertTrue(broken(lambda e: e.update(range={"metric": [5, 1], "us_customary": [0, 1]})))
        self.assertTrue(broken(lambda e: e.update(
            suggested_thresholds={"metric": {"alarm_below": 5, "warn_below": 1}, "us_customary": {}})))

    def test_fire_and_flooding_default_to_unknown_with_a_note_and_the_others_stay_ok(self):
        by_key = {e["key"]: e for e in CATALOG["entries"]}
        for key in ("fire", "flooding"):
            self.assertEqual(by_key[key]["absent"], "unknown", key)
            self.assertIn("Shows --", by_key[key]["note"], key)
            self.assertIn("cannot be told apart from no sensor", by_key[key]["note"], key)
        for key in ("anchor", "mob"):
            self.assertEqual(by_key[key]["absent"], "ok", key)
            self.assertNotIn("note", by_key[key], key)

    def test_wildcard_entries_have_default_instances(self):
        wild = [e for e in CATALOG["entries"] if "*" in e["path"]]
        self.assertGreaterEqual(len(wild), 10)
        self.assertTrue(all(e["default_instance"] for e in wild))


class UnitTests(unittest.TestCase):
    def test_normalize_units(self):
        for value in ("metric", "METRIC", " si ", "nautical", "Nautical", "meters"):
            self.assertEqual(catalog.normalize_units(value), "metric", value)
        for value in ("us_customary", "", None, "imperial", "garbage"):
            self.assertEqual(catalog.normalize_units(value), "us_customary", value)

    def test_quantity_table_matches_the_design(self):
        q = catalog.resolve_quantity
        self.assertEqual(q("depth", "metric"), ("none", "m"))
        self.assertEqual(q("depth", "us_customary"), ("m->ft", "ft"))
        self.assertEqual(q("temperature", "metric")[0], "K->C")
        self.assertEqual(q("temperature", "us_customary")[0], "K->F")
        self.assertEqual(q("pressure", "us_customary")[0], "Pa->psi")
        self.assertEqual(q("barometric", "us_customary"), ("Pa->hPa", "hPa"))
        self.assertEqual(q("volume", "us_customary")[0], "m3->gal")
        self.assertEqual(q("flow", "metric")[0], "m3/s->L/h")
        self.assertEqual(q("speed", "us_customary"), ("m/s->kn", "kn"))
        self.assertEqual(q("heading", "metric")[0], "rad->deg_360")
        self.assertEqual(q("angle", "metric")[0], "rad->deg_signed")
        self.assertEqual(q("rpm", "metric")[0], "Hz->rpm")
        self.assertEqual(q("charge", "metric")[0], "C->Ah")
        self.assertEqual(q("voltage", "nautical"), ("none", "V"))

    def test_legacy_nautical_is_metric_everywhere(self):
        cfg = base_config()
        a = catalog.merged_catalog(CATALOG, None, "nautical", cfg)
        b = catalog.merged_catalog(CATALOG, None, "metric", cfg)
        self.assertEqual(a, b)


class MaterialiseEveryEntryTests(unittest.TestCase):
    def test_every_entry_in_both_unit_systems_validates(self):
        for units in catalog.UNIT_SYSTEMS:
            for apply in (False, True):
                config = base_config()
                rows = catalog.merged_catalog(CATALOG, None, units, config, REGISTRY)
                self.assertEqual(len(rows), len(CATALOG["entries"]))
                for row in rows:
                    with_row(config, row, units, apply_thresholds=apply)
                self.assertEqual(valid(config), [], (units, apply))
                self.assertEqual(len(config["instruments"]) + len(config["notifications"]), len(rows))

    def test_definition_in_rows_matches_materialise_and_has_no_thresholds(self):
        for units in catalog.UNIT_SYSTEMS:
            for row in catalog.merged_catalog(CATALOG, None, units, base_config(), REGISTRY):
                self.assertNotIn("thresholds", row["definition"], row["row_id"])
                self.assertEqual(row["proposed_id"], row["proposed_id"].lower())
                self.assertRegex(row["proposed_id"], schema.INSTRUMENT_ID.pattern)

    def test_thresholds_are_off_unless_asked_for(self):
        rows = {r["row_id"]: r for r in catalog.merged_catalog(CATALOG, None, "metric", base_config())}
        row = rows["depth"]
        self.assertTrue(row["suggested_thresholds"])
        _, plain = catalog.materialise(row, base_config(), "metric")
        self.assertNotIn("thresholds", plain)
        _, with_th = catalog.materialise(row, base_config(), "metric", apply_thresholds=True)
        self.assertEqual(with_th["thresholds"], row["suggested_thresholds"])
        # Default config built from every row: nothing counts in the alarm summary.
        config = base_config()
        for r in rows.values():
            with_row(config, r, "metric")
        self.assertFalse(schema.counts_for_alarm_summary(config))
        self.assertTrue(all(n["alarm"] is False for n in config["notifications"].values()))
        config = base_config()
        for r in rows.values():
            with_row(config, r, "metric", apply_thresholds=True)
        self.assertTrue(schema.counts_for_alarm_summary(config))

    def test_every_claimed_layout_accepts_the_row(self):
        for units in catalog.UNIT_SYSTEMS:
            config = base_config()
            rows = catalog.merged_catalog(CATALOG, None, units, config, REGISTRY)
            for row in rows:
                for layout in row["layouts"]:
                    if layout == "electrical":
                        continue
                    cfg = base_config()
                    item = with_row(cfg, row, units)
                    cfg["displays"]["t"] = {"name": "T", "board": BOARD,
                                            "pages": [{"title": "T", "layout": layout, "items": [item]}]}
                    self.assertEqual(valid(cfg), [], (units, row["row_id"], layout))

    def test_gauge_rows_without_a_range_do_not_claim_range_layouts(self):
        rows = {r["row_id"]: r for r in catalog.merged_catalog(CATALOG, None, "metric", base_config(), REGISTRY)}
        self.assertNotIn("tank-bars", rows["log"]["layouts"])
        self.assertIn("tank-bars", rows["tank_fuel:0"]["layouts"])
        self.assertIn("gauge-arc", rows["depth"]["layouts"])
        self.assertNotIn("gauge-arc", rows["anchor"]["layouts"])

    def test_electrical_page_from_catalog_roles(self):
        for units in catalog.UNIT_SYSTEMS:
            config = base_config()
            rows = {r["catalog_key"]: r for r in catalog.merged_catalog(CATALOG, None, units, config, REGISTRY)}
            ids = [with_row(config, rows[k], units)
                   for k in ("batt_voltage", "batt_soc", "batt_current", "anchor", "solar_current")]
            config["displays"]["t"] = {"name": "T", "board": BOARD,
                                       "pages": [{"title": "E", "layout": "electrical", "items": ids}]}
            self.assertEqual(valid(config), [], units)
            self.assertIn("electrical", rows["batt_voltage"]["layouts"])
            self.assertEqual(rows["batt_soc"]["roles"], ["soc"])

    def test_stale_follows_the_heartbeat(self):
        config = base_config(heartbeat_s=30)
        for row in catalog.merged_catalog(CATALOG, None, "metric", config):
            if row["kind"] == "instrument":
                self.assertGreaterEqual(row["stale_after_s"], 60, row["row_id"])
        for row in catalog.merged_catalog(CATALOG, None, "metric", config):
            with_row(config, row, "metric")
        self.assertEqual(valid(config), [])


class IdTests(unittest.TestCase):
    def test_ids_are_unique_across_instruments_notifications_and_ha(self):
        row = {k: v for k, v in catalog.merged_catalog(CATALOG, None, "metric", base_config())[0].items()}
        config = base_config(notifications={"sog": {"path": "notifications.x.y", "label": "x"}},
                             ha={"sog_2": {"entity_id": "binary_sensor.x", "label": "x", "kind": "binary"}})
        item_id, _ = catalog.materialise(row, config, "metric")
        self.assertEqual(row["id_stem"], "sog")
        self.assertEqual(item_id, "sog_3")
        config["instruments"]["sog_3"] = {"path": "other.path", "label": "x", "convert": "none", "unit": "",
                                          "precision": 0, "stale_after_s": 10}
        self.assertEqual(catalog.materialise(row, config, "metric")[0], "sog_4")

    def test_existing_instrument_with_same_path_and_convert_is_reused_untouched(self):
        rows = {r["row_id"]: r for r in catalog.merged_catalog(CATALOG, None, "metric", base_config())}
        existing = {"path": "navigation.speedOverGround", "label": "Mine", "convert": "m/s->kn", "unit": "kn",
                    "precision": 2, "stale_after_s": 20}
        config = base_config(instruments={"my_sog": existing})
        item_id, definition = catalog.materialise(rows["sog"], config, "metric", apply_thresholds=True)
        self.assertEqual((item_id, definition), ("my_sog", existing))
        self.assertNotIn("thresholds", config["instruments"]["my_sog"])
        # A different conversion is a different row: it gets its own id.
        config["instruments"]["my_sog"]["convert"] = "m/s->km/h"
        self.assertNotEqual(catalog.materialise(rows["sog"], config, "metric")[0], "my_sog")

    def test_slug_and_id_rules(self):
        self.assertEqual(catalog.slug("speedOverGround"), "speed_over_ground")
        self.assertEqual(catalog.id_stem("123abc"), "i_123abc")
        self.assertEqual(catalog.id_stem("!!!"), "item")
        long_id = catalog.allocate_id("x" * 60, {"x" * 32})
        self.assertRegex(long_id, schema.INSTRUMENT_ID.pattern)
        self.assertTrue(long_id.endswith("_2"))
        self.assertEqual(len(long_id), 32)

    def test_label_rules(self):
        self.assertEqual(catalog.entry_label("Fuel {0}", "0", True), "Fuel")
        self.assertEqual(catalog.entry_label("Fuel {0}", "0", False), "Fuel 0")
        self.assertEqual(catalog.entry_label("Batt {0} V", "house", False), "Batt House V")
        self.assertLessEqual(len(catalog.entry_label("Coolant {0}", "starboardEngineLong", False)), 16)
        self.assertEqual(catalog.clean_label("Temp✓!"), "Temp!")
        self.assertEqual(catalog.clean_label("✓✓"), catalog.GENERIC_LABEL)
        self.assertEqual(len(catalog.clean_label("a" * 40)), 16)

    def test_match_path(self):
        self.assertEqual(catalog.match_path("a.*.c", "a.b.c"), ("b",))
        self.assertEqual(catalog.match_path("a.b", "a.b"), ())
        self.assertIsNone(catalog.match_path("a.*.c", "a.b.c.d"))
        self.assertIsNone(catalog.match_path("a.*.c", "a.b.d"))


class ServerCase(unittest.TestCase):
    def serve(self, **kw):
        server, _thread = fake_signalk.serve_rest(0, **kw)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, f"http://127.0.0.1:{server.server_port}"


class FetchLiveTests(ServerCase):
    def test_only_safe_numeric_leaves_come_back(self):
        _server, url = self.serve()
        live = catalog.fetch_live(url)
        self.assertTrue(live["ok"], live)
        paths = {leaf["path"] for leaf in live["leaves"]}
        self.assertIn("navigation.speedOverGround", paths)
        self.assertIn("electrical.batteries.house.voltage", paths)
        self.assertIn("sensors.weird.mystery", paths)
        for leaf in live["leaves"]:
            self.assertEqual(set(leaf), {"path", "si_units", "age_s", "value"})
            self.assertFalse(isinstance(leaf["value"], bool))
        for denied in ("navigation.position", "navigation.destination.commonName", "navigation.destination.eta",
                       "navigation.anchor.position", "navigation.courseRhumbline.nextPoint.position",
                       "sensors.weird.flag", "sensors.weird.nan", "sensors.weird.text", "navigation.state"):
            self.assertNotIn(denied, paths)
        # navigation.anchor.maxRadius is a harmless number and stays; so do "port" instances (twin engines).
        self.assertIn("navigation.anchor.maxRadius", paths)
        self.assertIn("propulsion.port.revolutions", paths)

    def test_private_values_never_appear_in_any_output(self):
        _server, url = self.serve()
        live = catalog.fetch_live(url)
        rows = catalog.merged_catalog(CATALOG, live, "metric", base_config(), REGISTRY)
        for blob in (json.dumps(live), json.dumps(rows)):
            for secret in PRIVATE:
                self.assertNotIn(secret, blob)
        self.assertNotIn("mmsi", json.dumps(live).lower())
        self.assertNotIn("callsign", json.dumps(live).lower())

    def test_notifications_are_names_only(self):
        _server, url = self.serve()
        live = catalog.fetch_live(url)
        self.assertEqual(live["notifications"], ["notifications.electrical.batteries.house.voltage",
                                                 "notifications.navigation.anchor"])
        self.assertNotIn("dragging", json.dumps(live))

    def test_age_is_computed_from_the_leaf_timestamp(self):
        import datetime
        _server, url = self.serve()
        now = datetime.datetime(2026, 1, 1, 0, 0, 30, tzinfo=datetime.timezone.utc)
        live = catalog.fetch_live(url, now=now)
        sog = next(leaf for leaf in live["leaves"] if leaf["path"] == "navigation.speedOverGround")
        self.assertEqual(sog["age_s"], 30.0)
        self.assertEqual(sog["si_units"], "m/s")

    def test_bearer_only_when_a_token_is_set(self):
        server, url = self.serve(token="tok-123")
        self.assertTrue(catalog.fetch_live(url, "tok-123")["ok"])
        self.assertEqual(server.handler_class.seen[-1], (fake_signalk.REST_PATH, "Bearer tok-123"))
        denied = catalog.fetch_live(url, "")
        self.assertFalse(denied["ok"])
        self.assertIn("DISPLAYS_SIGNALK_TOKEN", denied["error"])
        self.assertIsNone(server.handler_class.seen[-1][1])
        wrong = catalog.fetch_live(url, "other-token")
        self.assertNotIn("other-token", json.dumps(wrong))
        _server2, url2 = self.serve()
        catalog.fetch_live(url2)
        self.assertIsNone(_server2.handler_class.seen[-1][1])

    def test_redirects_are_refused(self):
        server, url = self.serve(redirect=True)
        result = catalog.fetch_live(url, "tok-123")
        self.assertFalse(result["ok"])
        self.assertIn("redirect", result["error"])
        self.assertEqual(len(server.handler_class.seen), 1)    # the redirect target was never contacted

    def test_oversized_response_is_refused(self):
        _server, url = self.serve(big=catalog.LIVE_MAX_BYTES + 10)
        result = catalog.fetch_live(url)
        self.assertFalse(result["ok"])
        self.assertIn("4 MiB", result["error"])

    def test_unreachable_server_is_an_error_state_not_a_crash(self):
        server, url = self.serve()
        server.shutdown()
        server.server_close()
        result = catalog.fetch_live(url)
        self.assertEqual((result["ok"], result["leaves"], result["notifications"]), (False, [], []))
        self.assertTrue(result["error"])
        rows = catalog.merged_catalog(CATALOG, result, "metric", base_config())
        self.assertTrue(all(row["live_state"] == "unknown" for row in rows))

    def test_bad_urls_are_errors(self):
        for url in ("", "ftp://x", "http://operator@192.0.2.1:3000", "nonsense"):
            result = catalog.fetch_live(url, "t")
            self.assertFalse(result["ok"], url)
            self.assertNotIn("operator", result["error"])

    def test_not_json_and_wrong_shape(self):
        _server, url = self.serve(tree={"x": 1})
        self.assertTrue(catalog.fetch_live(url)["ok"])
        self.assertEqual(catalog.extract_leaves([1, 2])["leaves"], [])

    def _raw_server(self, handler):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        stop = threading.Event()
        conns = []

        def run():
            listener.settimeout(0.2)
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except OSError:
                    continue
                conns.append(conn)
                threading.Thread(target=handler, args=(conn, stop), daemon=True).start()

        threading.Thread(target=run, daemon=True).start()

        def close():
            stop.set()
            listener.close()
            for conn in conns:
                conn.close()
        self.addCleanup(close)
        return f"http://127.0.0.1:{listener.getsockname()[1]}"

    def _within_budget(self, url, budget=1.5):
        started = time.monotonic()
        with mock.patch.object(catalog, "LIVE_TOTAL_S", budget):
            result = catalog.fetch_live(url)
        elapsed = time.monotonic() - started
        self.assertFalse(result["ok"])
        self.assertIn("answered too slowly", result["error"])
        self.assertLess(elapsed, budget + 0.8)

    def test_a_body_that_drips_one_byte_at_a_time_hits_the_total_deadline(self):
        def drip(conn, stop):
            try:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{")
                while not stop.is_set():
                    time.sleep(0.3)
                    conn.sendall(b" ")
            except OSError:
                pass
        self._within_budget(self._raw_server(drip))

    def test_a_chunked_body_that_drips_hits_the_total_deadline(self):
        def drip(conn, stop):
            try:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
                while not stop.is_set():
                    time.sleep(0.3)
                    conn.sendall(b"1\r\n \r\n")
            except OSError:
                pass
        self._within_budget(self._raw_server(drip))

    def test_a_server_that_never_sends_headers_hits_the_total_deadline(self):
        def silent(conn, stop):
            conn.recv(4096)
            stop.wait(10)
        self._within_budget(self._raw_server(silent))

    def test_headers_that_drip_hit_the_total_deadline(self):
        def drip(conn, stop):
            try:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 200 OK\r\n")
                while not stop.is_set():
                    time.sleep(0.3)
                    conn.sendall(b"X-A: b\r\n")
            except OSError:
                pass
        self._within_budget(self._raw_server(drip))

    def test_no_proxy_and_no_redirect_handler_in_use(self):
        # The opener must not honour proxy variables: the token goes only to SIGNALK_URL.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), catalog._NoRedirect)
        self.assertTrue(any(isinstance(h, catalog._NoRedirect) for h in opener.handlers))


class MergedCatalogTests(ServerCase):
    def setUp(self):
        _server, url = self.serve()
        self.live = catalog.fetch_live(url)
        self.config = base_config()
        self.rows = catalog.merged_catalog(CATALOG, self.live, "metric", self.config, REGISTRY)
        self.by_id = {r["row_id"]: r for r in self.rows}

    def test_concrete_entries_are_marked_live_or_no_live_data(self):
        self.assertEqual(self.by_id["sog"]["live_state"], "live")
        self.assertEqual(self.by_id["sog"]["value"], 5.8)    # 3 m/s in knots
        self.assertEqual(self.by_id["depth"]["live_state"], "live")
        self.assertEqual(self.by_id["baro"]["live_state"], "no_live_data")
        self.assertEqual(self.by_id["anchor"]["live_state"], "live")
        self.assertEqual(self.by_id["mob"]["live_state"], "no_live_data")

    def test_twin_engine_instances_named_port_are_kept(self):
        self.assertEqual(self.by_id["engine_rpm:port"]["path"], "propulsion.port.revolutions")
        self.assertEqual(self.by_id["engine_rpm:port"]["value"], 1200)
        self.assertEqual(self.by_id["engine_rpm:main"]["live_state"], "live")
        self.assertFalse(catalog.denied_path("propulsion.port.revolutions"))
        self.assertFalse(catalog.denied_path("tanks.fuel.starboard.currentLevel"))
        for path in ("navigation.position", "navigation.position.latitude", "navigation.anchor.position",
                     "navigation.destination.commonName", "navigation.destinationX.y", "a.mmsi", "a.UUID", "a.name",
                     "a.callsign", "a.b.position"):
            self.assertTrue(catalog.denied_path(path), path)

    def test_wildcards_expand_per_live_match_else_stay_a_template(self):
        self.assertIn("batt_voltage:house", self.by_id)
        self.assertIn("batt_voltage:start", self.by_id)
        self.assertEqual(self.by_id["batt_voltage:house"]["path"], "electrical.batteries.house.voltage")
        self.assertFalse(self.by_id["batt_voltage:house"]["template"])
        self.assertEqual(self.by_id["batt_voltage:house"]["label"], "Batt House V")
        self.assertEqual(self.by_id["tank_fuel:0"]["label"], "Fuel")       # one numeric instance: no suffix
        self.assertEqual(self.by_id["tank_water:main"]["path"], "tanks.freshWater.main.currentLevel")
        template = self.by_id["engine_coolant:main"]
        self.assertTrue(template["template"])
        self.assertEqual(template["live_state"], "no_live_data")
        self.assertEqual(template["path_template"], "propulsion.*.coolantTemperature")
        self.assertEqual(template["instance"], "main")
        self.assertEqual(self.by_id["engine_rpm:main"]["value"], 1500)

    def test_uncatalogued_rows_follow_the_defaults_and_validate(self):
        unc = {r["path"]: r for r in self.rows if r["group"] == "Uncatalogued"}
        self.assertIn("sensors.weird.mystery", unc)
        mystery = unc["sensors.weird.mystery"]
        self.assertEqual((mystery["convert"], mystery["unit"], mystery["precision"]), ("none", "", 1))
        self.assertNotIn("min", mystery)
        self.assertNotIn("icon", mystery)
        self.assertNotIn("suggested_thresholds", mystery)
        self.assertEqual(mystery["label"], "mystery")
        self.assertEqual(mystery["stale_after_s"], 10)
        volts = unc["propulsion.main.alternatorVoltage"]
        self.assertEqual((volts["convert"], volts["unit"], volts["precision"]), ("none", "V", 2))
        self.assertEqual(volts["label"], "alternatorVoltag")    # 16 characters at most
        self.assertEqual(unc["propulsion.main.temperature"]["convert"], "K->C")
        self.assertEqual(unc["navigation.courseRhumbline.nextPoint.bearingTrue"]["convert"], "rad->deg_360")
        self.assertEqual(unc["navigation.courseRhumbline.nextPoint.bearingTrue"]["precision"], 0)
        self.assertEqual(unc["navigation.anchor.maxRadius"]["convert"], "none")   # metric: metres
        self.assertEqual(unc["navigation.trip.log"]["convert"], "none")
        self.assertEqual(unc["electrical.inverter.acout.power"]["unit"], "W")
        config = base_config()
        for row in unc.values():
            with_row(config, row, "metric")
        self.assertEqual(valid(config), [])
        self.assertTrue(all("thresholds" not in i for i in config["instruments"].values()))

    def test_uncatalogued_defaults_function(self):
        d = catalog.uncatalogued_defaults("a.b.windDirection", "rad", 5)
        self.assertEqual(d["quantity"], "heading")
        self.assertEqual(catalog.uncatalogued_defaults("a.b.roll", "rad", 5)["quantity"], "angle")
        self.assertIsNone(catalog.uncatalogued_defaults("a.b.c", "kg", 5)["quantity"])
        self.assertEqual(catalog.uncatalogued_defaults("a.b.c", "m/s", 30)["stale_after_s"], 60)
        self.assertEqual(catalog.uncatalogued_defaults("a.b.c", None, 1)["stale_after_s"], 10)
        self.assertEqual(catalog.uncatalogued_defaults("a.b.revolutionsX", "Hz", 5)["quantity"], "rpm")
        self.assertIsNone(catalog.uncatalogued_defaults("a.b.mains", "Hz", 5)["quantity"])
        self.assertEqual(catalog.uncatalogued_defaults("environment.long.name.x" + "y" * 40, None, 5)["label"],
                         ("x" + "y" * 40)[:16])

    def test_fire_and_flooding_rows_carry_the_unknown_default_and_the_note(self):
        for key in ("fire", "flooding"):
            row = self.by_id[key]
            self.assertEqual((row["absent"], row["definition"]["absent"]), ("unknown", "unknown"), key)
            self.assertIn("Shows --", row["note"], key)
        self.assertEqual(self.by_id["mob"]["definition"]["absent"], "ok")
        self.assertNotIn("note", self.by_id["mob"])
        item_id, note = catalog.materialise(self.by_id["fire"], base_config(), "metric")
        self.assertEqual((item_id, note["absent"]), ("fire", "unknown"))

    def test_live_notifications_are_offered_only_where_they_are_accepted(self):
        extra = self.by_id["note:notifications.electrical.batteries.house.voltage"]
        self.assertEqual(extra["kind"], "notification")
        self.assertEqual(extra["group"], "Notifications")
        self.assertEqual(extra["layouts"], ["status-grid", "electrical"])
        self.assertNotIn("grid-2x2", extra["layouts"])
        self.assertEqual(extra["definition"]["absent"], "ok")
        self.assertIs(extra["definition"]["alarm"], False)
        self.assertNotIn("note:notifications.mob", self.by_id)    # null value: not active
        self.assertEqual(self.by_id["sog"]["layouts"].count("status-grid"), 1)

    def test_groups_are_ordered_and_uncatalogued_comes_last(self):
        groups = list(dict.fromkeys(r["group"] for r in self.rows))
        self.assertEqual(groups[0], "Navigation")
        self.assertEqual(groups[-1], "Uncatalogued")
        self.assertLess(groups.index("Electrical"), groups.index("Tanks"))

    def test_configured_rows_are_annotated_and_not_duplicated(self):
        config = base_config(instruments={
            "boat_speed": {"path": "navigation.speedOverGround", "label": "SOG", "convert": "m/s->kn", "unit": "kn",
                           "precision": 1, "stale_after_s": 10}},
            displays={"helm": {"name": "H", "board": BOARD,
                               "pages": [{"title": "N", "layout": "single", "items": ["boat_speed"]}]},
                      "mast": {"name": "M", "board": BOARD,
                               "pages": [{"title": "N", "layout": "single", "items": ["boat_speed"]}]}})
        rows = {r["row_id"]: r for r in catalog.merged_catalog(CATALOG, self.live, "metric", config, REGISTRY)}
        row = rows["sog"]
        self.assertEqual((row["configured_as"], row["used_on"], row["proposed_id"]), ("boat_speed", ["helm", "mast"],
                                                                                      "boat_speed"))
        self.assertNotIn("definition", row)
        self.assertIsNone(rows["stw"]["configured_as"])
        # With US units the same path converts differently, so the row is not "already configured".
        us = {r["row_id"]: r for r in catalog.merged_catalog(CATALOG, self.live, "us_customary", config, REGISTRY)}
        self.assertIsNone(us["depth"]["configured_as"])
        config["instruments"]["d"] = {"path": "environment.depth.belowTransducer", "label": "D", "convert": "m->ft",
                                      "unit": "ft", "precision": 1, "stale_after_s": 10}
        us = {r["row_id"]: r for r in catalog.merged_catalog(CATALOG, self.live, "us_customary", config, REGISTRY)}
        self.assertEqual(us["depth"]["configured_as"], "d")

    def test_proposed_ids_are_unique_within_one_listing(self):
        ids = [r["proposed_id"] for r in self.rows]
        self.assertEqual(len(ids), len(set(ids)))

    def test_configured_values_stay_selectable_offline_with_their_saved_ranges(self):
        config = base_config(instruments={
            "port_engine": {"path": "propulsion.port.revolutions", "label": "Port engine", "convert": "Hz->rpm",
                            "unit": "rpm", "min": 0, "max": 2700, "precision": 0, "stale_after_s": 30},
            "depth": {"path": "environment.depth.belowTransducer", "label": "Depth", "convert": "none",
                      "unit": "m", "precision": 1, "stale_after_s": 30},
            "private": {"path": "navigation.position.latitude", "label": "Position", "convert": "none",
                        "unit": "", "precision": 0, "stale_after_s": 30},
        })
        rows = {row["row_id"]: row for row in catalog.merged_catalog(CATALOG, None, "metric", config, REGISTRY)}
        engine = rows["configured:port_engine"]
        self.assertEqual((engine["label"], engine["configured_as"], engine["max"]), ("Port engine", "port_engine", 2700))
        self.assertIn("gauge-arc", engine["layouts"])
        self.assertNotIn("gauge-arc", rows["depth"]["layouts"])
        self.assertNotIn("configured:private", rows)

    def test_live_failure_leaves_rows_unknown(self):
        rows = catalog.merged_catalog(CATALOG, catalog.live_error("down"), "metric", base_config())
        self.assertTrue(all(r["live_state"] == "unknown" for r in rows))
        self.assertFalse(any(r["group"] == "Uncatalogued" for r in rows))

    def test_wide_heartbeat_raises_stale_threshold_in_definitions(self):
        config = base_config(heartbeat_s=20)
        for row in catalog.merged_catalog(CATALOG, self.live, "metric", config):
            if row["kind"] == "instrument":
                self.assertGreaterEqual(row["definition"]["stale_after_s"], 40, row["row_id"])
                with_row(config, row, "metric")
        self.assertEqual(valid(config), [])


if __name__ == "__main__":
    unittest.main()
