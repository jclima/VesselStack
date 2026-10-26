#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "displays"))

import ha_protocol  # noqa: E402

TOKEN = "tok-3f9a-secret"
BILGE, WATCH = "binary_sensor.vesselstack_bilge_pump", "sensor.boat_watch_summary"


def session():
    return ha_protocol.HaSession(TOKEN, [BILGE, WATCH])


def authed():
    s = session()
    s.feed({"type": "auth_required", "ha_version": "2026.1.0"})
    s.feed({"type": "auth_ok", "ha_version": "2026.1.0"})
    return s


def result(ident, success=True, payload=None):
    return {"id": ident, "type": "result", "success": success, "result": payload}


def entities_event(**body):
    return {"id": 1, "type": "event", "event": body}


class HandshakeTests(unittest.TestCase):
    def test_auth_required_is_answered_with_the_token_once(self) -> None:
        s = session()
        frames, updates, events = s.feed({"type": "auth_required", "ha_version": "x"})
        self.assertEqual(frames, [{"type": "auth", "access_token": TOKEN}])
        self.assertEqual((updates, events), ([], []))
        self.assertEqual(s.feed({"type": "auth_required"}), ([], [], []))     # never a second auth frame

    def test_auth_ok_subscribes_to_exactly_the_configured_entities(self) -> None:
        s = session()
        s.feed({"type": "auth_required"})
        frames, updates, events = s.feed({"type": "auth_ok"})
        self.assertEqual(frames, [{"id": 1, "type": "subscribe_entities", "entity_ids": [BILGE, WATCH]}])
        self.assertEqual(events, [("auth_ok",)])
        self.assertEqual(updates, [])

    def test_auth_ok_without_an_auth_frame_is_ignored(self) -> None:
        self.assertEqual(session().feed({"type": "auth_ok"}), ([], [], []))

    def test_auth_invalid_is_a_permanent_failure_and_ignores_everything_after(self) -> None:
        s = session()
        s.feed({"type": "auth_required"})
        frames, updates, events = s.feed({"type": "auth_invalid", "message": "Invalid access token"})
        self.assertEqual((frames, updates, events), ([], [], [("auth_invalid",)]))
        self.assertEqual(s.phase, ha_protocol.PHASE_FAILED)
        self.assertEqual(s.feed({"type": "auth_ok"}), ([], [], []))
        self.assertEqual(s.feed({"type": "auth_required"}), ([], [], []))

    def test_the_token_only_ever_appears_in_the_auth_frame(self) -> None:
        s = session()
        seen = []
        for message in ({"type": "auth_required"}, {"type": "auth_ok"}, result(1),
                        entities_event(a={BILGE: {"s": "on"}}), result(1, False)):
            frames, updates, events = s.feed(message)
            seen.append(json.dumps([frames[1:] if message["type"] == "auth_required" else frames, updates, events]))
        self.assertNotIn(TOKEN, "".join(seen))

    def test_events_do_not_carry_server_text(self) -> None:
        s = session()
        s.feed({"type": "auth_required"})
        _, _, events = s.feed({"type": "auth_invalid", "message": "the token abc is bad"})
        self.assertNotIn("abc", json.dumps(events))


class SubscribeEntitiesTests(unittest.TestCase):
    def test_result_success_makes_the_session_ready(self) -> None:
        s = authed()
        self.assertEqual(s.feed(result(1)), ([], [], [("ready", "entities")]))
        self.assertEqual(s.phase, ha_protocol.PHASE_READY)

    def test_initial_state_event(self) -> None:
        s = authed()
        s.feed(result(1))
        frames, updates, events = s.feed(entities_event(a={
            BILGE: {"s": "off", "a": {"friendly_name": "Bilge"}, "c": "ctx", "lc": 1.0, "lu": 1.0},
            WATCH: {"s": "Dock: clear, dock 4.2 ft"}, "sensor.other": {"s": "7"}}))
        self.assertEqual(frames, [])
        self.assertEqual(sorted(updates), [(BILGE, "off"), (WATCH, "Dock: clear, dock 4.2 ft")])
        self.assertEqual(events, [])

    def test_changes_removals_and_attribute_only_changes(self) -> None:
        s = authed()
        s.feed(result(1))
        _, updates, _ = s.feed(entities_event(c={BILGE: {"+": {"s": "on", "lu": 2.0, "c": {"id": "x"}}}}))
        self.assertEqual(updates, [(BILGE, "on")])
        _, updates, _ = s.feed(entities_event(c={BILGE: {"+": {"a": {"friendly_name": "Bilge pump"}}}}))
        self.assertEqual(updates, [])                       # attributes only: no state change
        _, updates, _ = s.feed(entities_event(c={BILGE: {"-": {"a": ["old"]}, "+": {"s": "off"}}}))
        self.assertEqual(updates, [(BILGE, "off")])
        _, updates, _ = s.feed(entities_event(r=[BILGE, "sensor.other"]))
        self.assertEqual(updates, [(BILGE, None)])          # gone: no value
        _, updates, _ = s.feed(entities_event(a={BILGE: {"s": "on"}}))
        self.assertEqual(updates, [(BILGE, "on")])          # and back again

    def test_a_non_string_state_is_no_value(self) -> None:
        s = authed()
        s.feed(result(1))
        _, updates, _ = s.feed(entities_event(a={BILGE: {"s": 5}, WATCH: {"x": 1}}))
        self.assertEqual(sorted(updates), [(BILGE, None), (WATCH, None)])
        _, updates, _ = s.feed(entities_event(c={BILGE: {"+": {"s": None}}}))
        self.assertEqual(updates, [(BILGE, None)])

    def test_unknown_entities_message_types_and_ids_are_ignored(self) -> None:
        s = authed()
        s.feed(result(1))
        for message in ({"type": "pong", "id": 9}, {"type": "event", "id": 7, "event": {"a": {BILGE: {"s": "on"}}}},
                        {"type": "mystery"}, {"id": 1, "type": "event"}, {"id": 1, "type": "event", "event": []},
                        {"id": 1, "type": "event", "event": {"a": []}}, {"id": 1, "type": "event", "event": {"c": 3}},
                        {"id": 1, "type": "event", "event": {"c": {BILGE: 3}}}, {"id": 1, "type": "event",
                                                                              "event": {"a": {BILGE: 5}}},
                        [], "null", "not json {", b"\xff\xfe", 5, None):
            self.assertEqual(s.feed(message)[:2], ([], []), message)

    def test_events_before_the_subscription_are_ignored(self) -> None:
        s = session()
        self.assertEqual(s.feed(entities_event(a={BILGE: {"s": "on"}})), ([], [], []))
        s.feed({"type": "auth_required"})
        self.assertEqual(s.feed(entities_event(a={BILGE: {"s": "on"}})), ([], [], []))

    def test_text_and_bytes_frames_are_decoded(self) -> None:
        s = session()
        frames, _, _ = s.feed(json.dumps({"type": "auth_required"}))
        self.assertEqual(frames[0]["type"], "auth")
        frames, _, events = s.feed(json.dumps({"type": "auth_ok"}).encode())
        self.assertEqual(frames[0]["type"], "subscribe_entities")
        self.assertEqual(events, [("auth_ok",)])

    def test_a_result_for_another_id_changes_nothing(self) -> None:
        s = authed()
        self.assertEqual(s.feed(result(5)), ([], [], []))
        self.assertEqual(s.phase, ha_protocol.PHASE_SUBSCRIBING)


class FallbackTests(unittest.TestCase):
    def failed_subscribe(self):
        s = authed()
        frames, updates, events = s.feed({"id": 1, "type": "result", "success": False,
                                          "error": {"code": "unknown_command", "message": "x"}})
        return s, frames, updates, events

    def test_failed_subscribe_entities_falls_back_to_events_plus_get_states(self) -> None:
        s, frames, updates, events = self.failed_subscribe()
        self.assertEqual(frames, [{"id": 2, "type": "subscribe_events", "event_type": "state_changed"},
                                  {"id": 3, "type": "get_states"}])
        self.assertEqual(events, [("fallback",)])
        self.assertEqual(updates, [])
        self.assertEqual([f["id"] for f in frames], sorted(f["id"] for f in frames))   # ids only increase

    def test_ready_after_both_results_and_the_snapshot_is_filtered(self) -> None:
        s, *_ = self.failed_subscribe()
        self.assertEqual(s.feed(result(2)), ([], [], []))
        snapshot = [{"entity_id": BILGE, "state": "on", "attributes": {"secret": "x"}},
                    {"entity_id": "light.cabin", "state": "on"}, {"entity_id": WATCH, "state": 5}, "junk", {}]
        frames, updates, events = s.feed(result(3, payload=snapshot))
        self.assertEqual(sorted(updates, key=str), [(BILGE, "on"), (WATCH, None)])
        self.assertEqual(events, [("ready", "events")])
        self.assertEqual(s.phase, ha_protocol.PHASE_READY)

    def test_state_changed_events_are_filtered_client_side(self) -> None:
        s, *_ = self.failed_subscribe()
        s.feed(result(3, payload=[]))
        s.feed(result(2))

        def changed(eid, new):
            return {"id": 2, "type": "event", "event": {"event_type": "state_changed", "data": {
                "entity_id": eid, "new_state": new, "old_state": None}}}
        self.assertEqual(s.feed(changed(BILGE, {"state": "off", "attributes": {}}))[1], [(BILGE, "off")])
        self.assertEqual(s.feed(changed("light.cabin", {"state": "on"}))[1], [])
        self.assertEqual(s.feed(changed(WATCH, None))[1], [(WATCH, None)])          # removed
        self.assertEqual(s.feed(changed(BILGE, {"state": 3}))[1], [(BILGE, None)])
        self.assertEqual(s.feed({"id": 2, "type": "event", "event": {"event_type": "call_service",
                                                                       "data": {"entity_id": BILGE}}})[1], [])
        self.assertEqual(s.feed({"id": 99, "type": "event", "event": {"event_type": "state_changed", "data": {
            "entity_id": BILGE, "new_state": {"state": "on"}}}})[1], [])
        self.assertEqual(s.feed({"id": 2, "type": "event", "event": {"event_type": "state_changed",
                                                                       "data": 5}})[1], [])

    def test_snapshot_applied_after_an_earlier_event_wins_by_order(self) -> None:
        s, *_ = self.failed_subscribe()
        _, early, _ = s.feed({"id": 2, "type": "event", "event": {"event_type": "state_changed", "data": {
            "entity_id": BILGE, "new_state": {"state": "off"}}}})
        _, snap, _ = s.feed(result(3, payload=[{"entity_id": BILGE, "state": "on"}]))
        self.assertEqual((early, snap), ([(BILGE, "off")], [(BILGE, "on")]))   # the later update is the snapshot

    def test_a_failed_fallback_is_a_permanent_failure(self) -> None:
        s, *_ = self.failed_subscribe()
        frames, updates, events = s.feed(result(2, success=False))
        self.assertEqual(events, [("failed", "subscribe")])
        self.assertEqual(s.feed(result(3)), ([], [], []))

    def test_a_failed_get_states_is_a_permanent_failure(self) -> None:
        s, *_ = self.failed_subscribe()
        s.feed(result(2))
        self.assertEqual(s.feed(result(3, success=False))[2], [("failed", "subscribe")])

    def test_events_mode_ignores_compressed_events(self) -> None:
        s, *_ = self.failed_subscribe()
        self.assertEqual(s.feed(entities_event(a={BILGE: {"s": "on"}}))[1], [])


class NoRetentionTests(unittest.TestCase):
    def test_the_session_keeps_no_message_bodies(self) -> None:
        s = authed()
        s.feed(result(1))
        marker = "ZZ-marker-attr"
        s.feed(entities_event(a={BILGE: {"s": "on", "a": {"friendly_name": marker}}, "sensor.x": {"s": marker}}))
        self.assertNotIn(marker, repr(vars(s)))


if __name__ == "__main__":
    unittest.main()
