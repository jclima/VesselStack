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


if __name__ == "__main__":
    unittest.main()
