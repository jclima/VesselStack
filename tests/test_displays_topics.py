#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "displays"))

import topics  # noqa: E402

CONFIG = {
    "heartbeat_s": 5,
    "instruments": {
        "sog": {"path": "navigation.speedOverGround", "convert": "m/s->kn", "unit": "kn",
                "precision": 1, "stale_after_s": 10, "min_interval_ms": 500},
        "depth": {"path": "environment.depth.belowTransducer", "convert": "m->ft", "unit": "ft",
                  "precision": 1, "stale_after_s": 10},
    },
}
SOG = "navigation.speedOverGround"
TS = "2026-01-01T00:00:00.000Z"
KN = 1852 / 3600  # 1 knot in m/s


def decode(msgs):
    return [(t, json.loads(p), r) for t, p, r in msgs]


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = topics.Engine(CONFIG)

    def test_constants(self) -> None:
        self.assertEqual(topics.instrument_topic("sog"), "vesselstack/v1/instruments/sog")
        self.assertEqual(topics.BRIDGE_STATUS_TOPIC, "vesselstack/v1/bridge/status")
        self.assertEqual(topics.BRIDGE_SIGNALK_TOPIC, "vesselstack/v1/bridge/signalk")
        self.assertEqual(topics.display_status_topic("vsdisp-helm"), "vesselstack/v1/displays/vsdisp-helm/status")

    def test_first_publish_and_payload_shape(self) -> None:
        msgs = self.engine.update(SOG, 6.4 * KN, TS, 100.0)
        self.assertEqual(len(msgs), 1)
        topic, payload, retain = msgs[0]
        self.assertEqual(topic, "vesselstack/v1/instruments/sog")
        self.assertTrue(retain)
        self.assertEqual(payload, '{"v":6.4,"u":"kn","st":"ok","ts":"%s","age":0.0}' % TS)

    def test_unchanged_value_not_republished(self) -> None:
        self.engine.update(SOG, 6.4 * KN, TS, 100.0)
        self.assertEqual(self.engine.update(SOG, 6.4 * KN, TS, 101.0), [])
        self.assertEqual(self.engine.update(SOG, 6.4004 * KN, TS, 102.0), [])  # rounds the same

    def test_min_interval_rate_limit_then_flush_on_tick(self) -> None:
        self.engine.update(SOG, 6.0 * KN, TS, 100.0)
        self.assertEqual(self.engine.update(SOG, 7.0 * KN, TS, 100.2), [])
        sog = lambda msgs: [m for m in decode(msgs) if m[0].endswith("/sog")]  # noqa: E731
        self.assertEqual(sog(self.engine.tick(100.3)), [])
        flushed = sog(self.engine.tick(100.6))
        self.assertEqual(flushed[0][1]["v"], 7.0)
        self.assertEqual(decode(self.engine.update(SOG, 8.0 * KN, TS, 101.2))[0][1]["v"], 8.0)

    def test_heartbeat_republish_with_age(self) -> None:
        self.engine.update(SOG, 6.0 * KN, TS, 100.0)
        self.assertEqual([m for m in self.engine.tick(103.0) if m[0].endswith("/sog")], [])
        beat = [m for m in decode(self.engine.tick(105.0)) if m[0].endswith("/sog")]
        self.assertEqual(beat[0][1]["st"], "ok")
        self.assertEqual(beat[0][1]["age"], 5.0)

    def test_ok_stale_ok(self) -> None:
        self.engine.update(SOG, 6.0 * KN, TS, 100.0)
        sog = lambda msgs: [m for m in decode(msgs) if m[0].endswith("/sog")]  # noqa: E731
        self.assertEqual(sog(self.engine.tick(110.0))[0][1]["st"], "ok")  # heartbeat, exactly at limit
        stale = sog(self.engine.tick(110.5))
        self.assertEqual(stale[0][1]["st"], "stale")
        self.assertIsNone(stale[0][1]["v"])
        self.assertEqual(stale[0][1]["ts"], TS)
        self.assertEqual(stale[0][1]["age"], 10.5)
        again = sog(self.engine.update(SOG, 6.0 * KN, TS, 111.0))
        self.assertEqual(again[0][1]["st"], "ok")
        self.assertEqual(again[0][1]["v"], 6.0)

    def test_missing_when_never_received(self) -> None:
        msgs = decode(self.engine.tick(0.0))
        self.assertEqual(len(msgs), 2)
        for _, payload, retain in msgs:
            self.assertTrue(retain)
            self.assertEqual((payload["st"], payload["v"], payload["ts"], payload["age"]), ("missing", None, None, None))
        self.assertEqual(self.engine.tick(1.0), [])
        self.assertEqual(len(self.engine.tick(5.0)), 2)  # heartbeat
        self.assertEqual(decode(self.engine.update(SOG, KN, TS, 6.0))[0][1]["st"], "ok")

    def test_invalid_values_ignored(self) -> None:
        for bad in (None, True, False, "6.4", float("nan"), float("inf"), {"a": 1}, [1]):
            self.assertEqual(self.engine.update(SOG, bad, TS, 1.0), [], repr(bad))
        self.assertEqual(self.engine.update("unknown.path", 1.0, TS, 1.0), [])
        self.assertEqual(decode(self.engine.tick(2.0))[0][1]["st"], "missing")

    def test_out_of_range_values_ignored(self) -> None:
        # 1e30 exceeds the magnitude limit; 1e308 only overflows after m/s->kn.
        for bad in (1e30, -1e30, 1e308):
            self.assertEqual(self.engine.update(SOG, bad, TS, 1.0), [], repr(bad))
        self.assertEqual(decode(self.engine.tick(2.0))[0][1]["st"], "missing")
        msgs = decode(self.engine.update(SOG, KN * 6.4, TS, 3.0))
        self.assertEqual(msgs[0][1]["v"], 6.4)
        self.assertEqual(msgs[0][1]["st"], "ok")
        self.assertEqual(self.engine.update(SOG, 1e30, TS, 4.0), [])

    def test_signed_conversion_rounding_in_payload(self) -> None:
        engine = topics.Engine({"instruments": {"awa": {"path": "environment.wind.angleApparent",
                                "convert": "rad->deg_signed", "unit": "deg", "precision": 0, "stale_after_s": 10}}})
        out = decode(engine.update("environment.wind.angleApparent", -3.0, None, 0.0))
        self.assertEqual(out[0][1]["v"], -172)
        self.assertIsNone(out[0][1]["ts"])


class SignalKHelperTests(unittest.TestCase):
    def test_subscribe_frame(self) -> None:
        frame = topics.subscribe_frame(CONFIG)
        self.assertEqual(frame["context"], "vessels.self")
        self.assertEqual(frame["subscribe"][0], {"path": SOG, "period": 500, "minPeriod": 500})
        self.assertEqual(frame["subscribe"][1]["period"], 1000)
        self.assertEqual(len(frame["subscribe"]), 2)

    def test_url_mapping(self) -> None:
        self.assertEqual(topics.signalk_stream_url("http://signalk.local:3000"),
                         "ws://signalk.local:3000/signalk/v1/stream?subscribe=none")
        self.assertEqual(topics.signalk_stream_url("https://signalk.local/"),
                         "wss://signalk.local/signalk/v1/stream?subscribe=none")
        self.assertEqual(topics.signalk_stream_url("http://h:3000/prefix/"),
                         "ws://h:3000/prefix/signalk/v1/stream?subscribe=none")
        with self.assertRaises(ValueError):
            topics.signalk_stream_url("ftp://h")

    def test_parse_delta(self) -> None:
        msg = {"context": "vessels.self", "updates": [{"timestamp": TS, "values": [
            {"path": SOG, "value": 3.2}, {"path": "x.y", "value": None}, {"nopath": 1}]}]}
        self.assertEqual(topics.parse_delta(msg), [(SOG, 3.2, TS), ("x.y", None, TS)])
        self.assertEqual(topics.parse_delta(json.dumps(msg)), topics.parse_delta(msg))
        no_ctx = {"updates": msg["updates"]}
        self.assertEqual(len(topics.parse_delta(no_ctx)), 2)

    def test_parse_delta_context_filtering(self) -> None:
        updates = [{"timestamp": TS, "values": [{"path": SOG, "value": 1}]}]
        other = {"context": "vessels.urn:mrn:imo:mmsi:000000000", "updates": updates}
        self.assertEqual(topics.parse_delta(other), [])
        self.assertEqual(len(topics.parse_delta(other, self_urn="urn:mrn:imo:mmsi:000000000")), 1)
        self.assertEqual(len(topics.parse_delta({"context": "urn:x", "updates": updates}, self_urn="urn:x")), 1)
        self.assertEqual(topics.parse_delta({"context": "vessels.self"}), [])
        self.assertEqual(topics.parse_delta("not json"), [])
        self.assertEqual(topics.parse_delta({"name": "hello", "version": "1"}), [])


SEV_CONFIG = {
    "heartbeat_s": 5,
    "instruments": {
        "house": {"path": "electrical.batteries.house.voltage", "convert": "none", "unit": "V", "precision": 1,
                  "stale_after_s": 10, "min_interval_ms": 1000,
                  "thresholds": {"alarm_below": 11.0, "warn_below": 12.0, "hysteresis": 0.5}},
        "plain": {"path": "x.y", "convert": "none", "unit": "V", "precision": 1, "stale_after_s": 10},
    },
}
HOUSE = "electrical.batteries.house.voltage"


class SeverityPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = topics.Engine(SEV_CONFIG)

    def test_build_payload_field_order(self) -> None:
        self.assertEqual(topics.build_payload(1.5, "V", "ok", TS, 0.0),
                         '{"v":1.5,"u":"V","st":"ok","ts":"%s","age":0.0}' % TS)
        self.assertEqual(topics.build_payload(1.5, "V", "ok", TS, 0.0, "warn"),
                         '{"v":1.5,"u":"V","st":"ok","sev":"warn","ts":"%s","age":0.0}' % TS)

    def test_sev_only_for_instruments_with_thresholds(self) -> None:
        msgs = decode(self.engine.update(HOUSE, 13.0, TS, 100.0) + self.engine.update("x.y", 13.0, TS, 100.0))
        self.assertEqual(msgs[0][1]["sev"], "ok")
        self.assertNotIn("sev", msgs[1][1])
        self.assertEqual(list(msgs[0][1]), ["v", "u", "st", "sev", "ts", "age"])

    def test_sev_uses_the_rounded_value(self) -> None:
        # 11.96 rounds to 12.0 (precision 1): not below warn_below 12.0, so ok.
        msgs = decode(self.engine.update(HOUSE, 11.96, TS, 100.0))
        self.assertEqual((msgs[0][1]["v"], msgs[0][1]["sev"]), (12.0, "ok"))

    def test_sev_change_alone_publishes_respecting_the_rate_limit(self) -> None:
        self.engine.update(HOUSE, 12.0, TS, 100.0)               # ok, 12.0
        self.engine.update(HOUSE, 11.9, TS, 101.0)               # warn
        self.assertEqual(self.engine.update(HOUSE, 11.4, TS, 102.0)[0][1].count('"warn"'), 1)  # still warn
        msgs = decode(self.engine.update(HOUSE, 10.9, TS, 103.0))
        self.assertEqual(msgs[0][1]["sev"], "alarm")
        # Rate limited: a sev change inside min_interval is deferred to tick.
        self.assertEqual(self.engine.update(HOUSE, 12.9, TS, 103.2), [])
        house = lambda msgs: [m for m in decode(msgs) if m[0].endswith("/house")]  # noqa: E731
        self.assertEqual(house(self.engine.tick(103.5)), [])
        out = house(self.engine.tick(104.1))
        self.assertEqual((out[0][1]["v"], out[0][1]["sev"]), (12.9, "ok"))

    def test_hysteresis_applies_across_updates(self) -> None:
        self.engine.update(HOUSE, 10.9, TS, 100.0)               # alarm
        msgs = decode(self.engine.update(HOUSE, 11.3, TS, 101.5))  # inside hold zone (< 11.5)
        self.assertEqual(msgs[0][1]["sev"], "alarm")
        msgs = decode(self.engine.update(HOUSE, 11.6, TS, 103.0))
        self.assertEqual(msgs[0][1]["sev"], "warn")

    def test_stale_and_missing_are_unknown(self) -> None:
        first = decode(self.engine.tick(100.0))
        self.assertEqual(first[0][1]["st"], "missing")
        self.assertEqual(first[0][1]["sev"], "unknown")
        self.assertNotIn("sev", first[1][1])
        self.engine.update(HOUSE, 13.0, TS, 101.0)
        stale = decode(self.engine.tick(120.0))
        house = [m for m in stale if m[0].endswith("/house")][0][1]
        self.assertEqual((house["st"], house["v"], house["sev"]), ("stale", None, "unknown"))

    def test_recovery_after_stale_has_no_hysteresis_memory(self) -> None:
        self.engine.update(HOUSE, 10.9, TS, 100.0)               # alarm
        self.engine.tick(120.0)                                  # stale
        msgs = decode(self.engine.update(HOUSE, 11.3, TS, 121.0))
        self.assertEqual(msgs[0][1]["sev"], "warn")

    def test_existing_payloads_are_unchanged_without_thresholds(self) -> None:
        engine = topics.Engine(CONFIG)
        topic, payload, _ = engine.update(SOG, 6.4 * KN, TS, 100.0)[0]
        self.assertNotIn("sev", payload)



class AlarmSummaryTests(unittest.TestCase):
    def summary(self, items):
        payload = topics.alarm_summary(items, "T")
        return None if payload is None else json.loads(payload)

    def test_nothing_counted_publishes_nothing(self) -> None:
        self.assertIsNone(topics.alarm_summary([], "T"))

    def test_max_ranks_alarm_warn_unknown_ok(self) -> None:
        self.assertEqual(self.summary([("a", "ok"), ("b", "ok")])["max"], "ok")
        self.assertEqual(self.summary([("a", "ok"), ("b", "unknown")])["max"], "unknown")
        self.assertEqual(self.summary([("a", "unknown"), ("b", "warn")])["max"], "warn")
        self.assertEqual(self.summary([("a", "warn"), ("b", "alarm"), ("c", "unknown")])["max"], "alarm")

    def test_counts_and_id_order(self) -> None:
        got = self.summary([("w1", "warn"), ("a1", "alarm"), ("u", "unknown"), ("ok", "ok"),
                            ("a2", "alarm"), ("w2", "warn")])
        self.assertEqual(got, {"max": "alarm", "na": 2, "nw": 2, "nu": 1,
                               "ids": ["a1", "a2", "w1", "w2"], "ts": "T"})

    def test_unrecognised_severity_is_unknown_and_ids_are_capped(self) -> None:
        self.assertEqual(self.summary([("a", None)])["nu"], 1)
        got = self.summary([(f"a{i}", "alarm") for i in range(20)] + [("w", "warn")])
        self.assertEqual(got["na"], 20)
        self.assertEqual(got["ids"], [f"a{i}" for i in range(16)])

    def test_payload_is_compact_and_id_only(self) -> None:
        payload = topics.alarm_summary([("a", "alarm")], "2026-01-01T00:00:00Z")
        self.assertEqual(payload, '{"max":"alarm","na":1,"nw":0,"nu":0,"ids":["a"],'
                                  '"ts":"2026-01-01T00:00:00Z"}')

    def test_instrument_items_follow_alarm_flag_and_freshness(self) -> None:
        cfg = {"heartbeat_s": 5, "instruments": {
            "depth": {"path": "d", "convert": "none", "unit": "m", "precision": 1, "stale_after_s": 10,
                      "thresholds": {"alarm_below": 4}},
            "muted": {"path": "m", "convert": "none", "unit": "m", "precision": 1, "stale_after_s": 10,
                      "thresholds": {"alarm_below": 4}, "alarm": False},
            "plain": {"path": "p", "convert": "none", "unit": "m", "precision": 1, "stale_after_s": 10},
            "forced": {"path": "f", "convert": "none", "unit": "m", "precision": 1, "stale_after_s": 10,
                       "alarm": True}}}
        engine = topics.Engine(cfg)
        self.assertEqual(engine.summary_items(), [("depth", "unknown"), ("forced", "unknown")])
        for path in "dmpf":
            engine.update(path, 2.0, TS, 0.0)
        self.assertEqual(engine.summary_items(), [("depth", "alarm"), ("forced", "ok")])
        engine.tick(11.0)
        self.assertEqual(engine.summary_items(), [("depth", "unknown"), ("forced", "unknown")])

    def test_notification_items_default_to_counted(self) -> None:
        cfg = {"notifications": {"a": {"path": "notifications.a", "label": "A"},
                                 "b": {"path": "notifications.b", "label": "B", "alarm": False}}}
        events = topics.EventEngine(cfg)
        self.assertEqual(events.summary_items(0.0), [("a", "unknown")])
        events.source_state("signalk", True, 0.0)
        events.set("a", {"state": "alarm"}, TS, 0.5)
        self.assertEqual(events.summary_items(0.5), [("a", "alarm")])

if __name__ == "__main__":
    unittest.main()


NOTE_CONFIG = {
    "heartbeat_s": 5,
    "instruments": CONFIG["instruments"],
    "notifications": {
        "anchor": {"path": "notifications.navigation.anchor", "label": "Anchor"},
        "bilge": {"path": "notifications.bilge", "label": "Bilge", "absent": "unknown"},
    },
}
ANCHOR = "notifications.navigation.anchor"
ANCHOR_TOPIC = "vesselstack/v1/notifications/anchor"
BILGE_TOPIC = "vesselstack/v1/notifications/bilge"


def by_topic(msgs):
    out = {}
    for topic, body, retain in decode(msgs):
        assert retain is True
        out.setdefault(topic, []).append(body)
    return out


class EventEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events = topics.EventEngine(NOTE_CONFIG)

    def connect(self, now=0.0):
        self.events.source_state("signalk", True, now)
        return now

    def test_topic_and_payload_shape(self) -> None:
        self.assertEqual(topics.notification_topic("anchor"), ANCHOR_TOPIC)
        self.assertEqual(self.events.paths(), [ANCHOR, "notifications.bilge"])
        raw = self.events.tick(0.0)
        self.assertEqual(raw[0][0], ANCHOR_TOPIC)
        self.assertEqual(raw[0][1], '{"v":null,"u":"","st":"missing","sev":"unknown","ts":null,"age":null}')
        self.assertTrue(all(retain for _, _, retain in raw))

    def test_never_connected_is_missing_then_absent_mapping_once_connected(self) -> None:
        self.assertEqual({b["st"] for bodies in by_topic(self.events.tick(0.0)).values() for b in bodies},
                         {"missing"})
        sent = by_topic(self.events.source_state("signalk", True, 1.0))
        self.assertEqual(sent[ANCHOR_TOPIC][0]["st"], "ok")
        # settling: nothing is claimed healthy until SignalK had a chance to resend active alarms
        self.assertEqual(sent[ANCHOR_TOPIC][0]["sev"], "unknown")
        later = by_topic(self.events.tick(1.0 + topics.EVENT_SETTLE_S))
        self.assertEqual(later[ANCHOR_TOPIC][0]["sev"], "ok")          # absent defaults to ok
        self.assertNotIn(BILGE_TOPIC, later)                            # unchanged: still ("ok", "unknown")
        beat = by_topic(self.events.tick(1.0 + 5.0))
        self.assertEqual((beat[BILGE_TOPIC][0]["st"], beat[BILGE_TOPIC][0]["sev"]), ("ok", "unknown"))  # absent: unknown

    def test_raise_publishes_immediately_and_never_reads_the_message(self) -> None:
        self.connect()
        out = by_topic(self.events.update(ANCHOR, {"state": "alarm", "message": "SECRET-TEXT", "method": ["sound"]},
                                          TS, 2.0))
        self.assertEqual(list(out), [ANCHOR_TOPIC])
        body = out[ANCHOR_TOPIC][0]
        self.assertEqual((body["st"], body["sev"], body["ts"], body["v"], body["u"]), ("ok", "alarm", TS, None, ""))
        self.assertNotIn("SECRET", json.dumps(out))
        self.assertEqual(list(body), ["v", "u", "st", "sev", "ts", "age"])
        # the same state again is not republished before the heartbeat
        self.assertEqual(self.events.update(ANCHOR, {"state": "alarm"}, TS, 2.5), [])

    def test_state_changes_map_through_severity(self) -> None:
        self.connect()
        seen = []
        for t, state in enumerate(("warn", "emergency", "normal", "alert", "bogus"), start=1):
            out = by_topic(self.events.set("anchor", {"state": state}, TS, float(t)))
            seen.append(out[ANCHOR_TOPIC][0]["sev"])
        self.assertEqual(seen, ["warn", "alarm", "ok", "warn", "unknown"])

    def test_clear_follows_absent_mapping(self) -> None:
        self.connect()
        self.events.set("anchor", {"state": "alarm"}, TS, 1.0)
        self.events.set("bilge", {"state": "alarm"}, TS, 1.0)
        cleared = by_topic(self.events.update(ANCHOR, None, TS, 2.0) + self.events.update("notifications.bilge", None, TS, 2.0))
        self.assertEqual(cleared[ANCHOR_TOPIC][0]["sev"], "ok")
        self.assertEqual(cleared[BILGE_TOPIC][0]["sev"], "unknown")
        self.assertEqual(cleared[ANCHOR_TOPIC][0]["st"], "ok")

    def test_disconnect_makes_everything_stale_and_forgets_alarms(self) -> None:
        self.connect()
        self.events.set("anchor", {"state": "alarm"}, TS, 1.0)
        out = by_topic(self.events.source_state("signalk", False, 5.0))
        for topic in (ANCHOR_TOPIC, BILGE_TOPIC):
            self.assertEqual((out[topic][0]["st"], out[topic][0]["sev"]), ("stale", "unknown"))
            self.assertIsNone(out[topic][0]["age"])
        # a repeat of the same state changes nothing
        self.assertEqual(self.events.source_state("signalk", False, 6.0), [])
        # reconnect: the alarm raised before the outage is not assumed; SignalK re-sends it
        back = by_topic(self.events.source_state("signalk", True, 20.0))
        self.assertEqual((back[ANCHOR_TOPIC][0]["st"], back[ANCHOR_TOPIC][0]["sev"]), ("ok", "unknown"))
        resent = by_topic(self.events.update(ANCHOR, {"state": "alarm"}, TS, 20.5))
        self.assertEqual(resent[ANCHOR_TOPIC][0]["sev"], "alarm")

    def test_stale_message_while_disconnected_never_shows_ok(self) -> None:
        out = by_topic(self.events.set("anchor", {"state": "normal"}, TS, 1.0))
        self.assertEqual(out[ANCHOR_TOPIC][0]["st"], "missing")
        self.assertEqual(out[ANCHOR_TOPIC][0]["sev"], "unknown")

    def test_heartbeat_republishes_every_heartbeat_s(self) -> None:
        self.connect()
        self.events.set("anchor", {"state": "alarm"}, TS, 1.0)
        self.assertEqual(self.events.tick(1.0), [])
        self.assertEqual(self.events.tick(4.9), [])
        self.assertEqual(list(by_topic(self.events.tick(5.0))), [BILGE_TOPIC])  # published at connect, t=0
        beat = by_topic(self.events.tick(6.0))                                # anchor: published at t=1
        self.assertEqual(beat[ANCHOR_TOPIC][0]["sev"], "alarm")
        self.assertEqual(beat[ANCHOR_TOPIC][0]["age"], 5.0)
        self.assertEqual(self.events.tick(6.5), [])
        self.assertEqual(len(self.events.tick(11.0)), 2)

    def test_unknown_ids_and_other_sources_are_ignored(self) -> None:
        self.assertEqual(self.events.set("nope", {"state": "alarm"}, TS, 0.0), [])
        self.assertEqual(self.events.update("notifications.other", {"state": "alarm"}, TS, 0.0), [])
        self.assertEqual(self.events.source_state("ha", True, 0.0), [])
        self.assertFalse(self.events.connected)

    def test_no_notifications_means_no_output(self) -> None:
        engine = topics.EventEngine(CONFIG)
        self.assertEqual(engine.paths(), [])
        self.assertEqual(engine.source_state("signalk", True, 0.0), [])
        self.assertEqual(engine.tick(100.0), [])

    def test_subscribe_frame_includes_notification_paths(self) -> None:
        frame = topics.subscribe_frame(NOTE_CONFIG)
        entries = {e["path"]: e for e in frame["subscribe"]}
        self.assertEqual(entries[ANCHOR]["period"], 1000)
        self.assertEqual(entries["notifications.bilge"]["period"], 1000)
        self.assertEqual(entries["navigation.speedOverGround"]["period"], 500)
        self.assertEqual(topics.subscribe_frame(CONFIG)["subscribe"],
                         [e for e in frame["subscribe"] if not e["path"].startswith("notifications.")])

    def test_instrument_engine_still_drops_non_numeric_values(self) -> None:
        engine = topics.Engine({"instruments": {"n": {"path": ANCHOR, "convert": "none", "unit": "",
                                                       "precision": 0, "stale_after_s": 10}}})
        self.assertEqual(engine.update(ANCHOR, {"state": "alarm"}, TS, 0.0), [])


class ClockTests(unittest.TestCase):
    SCHEDULE = {"start": "21:30", "end": "06:15"}   # crosses midnight

    @staticmethod
    def minute(text):
        return topics.hhmm_minutes(text)

    def test_hhmm_parsing(self) -> None:
        self.assertEqual(topics.hhmm_minutes("00:00"), 0)
        self.assertEqual(topics.hhmm_minutes("23:59"), 1439)
        for bad in ("24:00", "12:60", "1:00", "12:0", "12-00", "ab:cd", "", None, 1200, "12:00\n", "١٢:٠٠"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                topics.hhmm_minutes(bad)

    def test_window_crossing_midnight(self) -> None:
        night = lambda hm: topics.in_night_window(self.minute(hm), self.SCHEDULE)  # noqa: E731
        self.assertTrue(night("21:30"))      # start is inclusive
        self.assertTrue(night("23:59"))
        self.assertTrue(night("00:00"))
        self.assertTrue(night("06:14"))
        self.assertFalse(night("06:15"))     # end is exclusive
        self.assertFalse(night("12:00"))
        self.assertFalse(night("21:29"))

    def test_window_inside_one_day(self) -> None:
        schedule = {"start": "13:00", "end": "14:30"}
        night = lambda hm: topics.in_night_window(self.minute(hm), schedule)  # noqa: E731
        self.assertEqual([night(t) for t in ("12:59", "13:00", "14:29", "14:30", "23:00", "00:00")],
                         [False, True, True, False, False, False])

    def test_no_schedule_is_never_night(self) -> None:
        for schedule in (None, {}):
            self.assertFalse(topics.in_night_window(0, schedule))
            self.assertFalse(topics.in_night_window(720, schedule))

    def test_wall_clock_only_so_dst_changes_cannot_shift_the_window(self) -> None:
        # The flag depends on the local hour and minute alone; a clock that jumps (spring
        # forward 01:59 -> 03:00) or repeats (fall back 02:59 -> 02:00) moves with the window.
        from datetime import datetime
        schedule = {"start": "02:30", "end": "03:30"}
        for hm, expected in (("01:59", False), ("03:00", True), ("02:59", True), ("02:00", False)):
            hour, minute = map(int, hm.split(":"))
            state = topics.clock_state(datetime(2026, 3, 29, hour, minute), schedule)
            self.assertEqual(state, (hm, expected), hm)

    def test_clock_state_and_payload_shape(self) -> None:
        from datetime import datetime, timedelta, timezone
        local = datetime(2026, 10, 25, 14, 5, 59, tzinfo=timezone(timedelta(hours=-7)))
        self.assertEqual(topics.clock_state(local, self.SCHEDULE), ("14:05", False))
        payload = topics.clock_payload(("14:05", False), "2026-10-25T21:05:59Z")
        self.assertEqual(payload, '{"hm":"14:05","night":false,"ts":"2026-10-25T21:05:59Z"}')
        self.assertEqual(json.loads(topics.clock_payload(("23:00", True), "t"))["night"], True)

    def test_clock_is_only_wanted_for_header_or_schedule_displays(self) -> None:
        def cfg(**ui):
            return {"displays": {"helm": {"ui": ui}}}
        self.assertFalse(topics.clock_wanted({"displays": {}}))
        self.assertFalse(topics.clock_wanted(cfg()))
        self.assertFalse(topics.clock_wanted(cfg(night="manual", swipe=True, header=False)))
        self.assertTrue(topics.clock_wanted(cfg(header=True)))
        self.assertTrue(topics.clock_wanted(cfg(night="schedule")))
        self.assertTrue(topics.clock_wanted({"displays": {"a": {}, "b": {"ui": {"header": True}}}}))


