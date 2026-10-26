#!/usr/bin/env python3
"""HaRunner / HaThread with scripted fake sockets: no network, no websockets package."""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "displays"))

import ha_client  # noqa: E402

ha_client.LOG.addHandler(logging.NullHandler())
TOKEN = "tok-very-secret-ha"
BILGE = "binary_sensor.bilge"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Source:
    """script: dicts are delivered (as JSON text), "idle" returns None, "close" raises, an Exception is raised."""

    def __init__(self, clock, script):
        self.clock, self.script, self.sent, self.closed = clock, list(script), [], False

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout):
        item = self.script.pop(0) if self.script else "close"
        if item == "idle":
            self.clock.now += timeout
            return None
        if item == "close":
            raise ConnectionError("closed by fake")
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)

    def close(self):
        self.closed = True


def run_attempts(attempts, retry=300.0):
    clock, out, stop = Clock(), queue.Queue(), threading.Event()
    waits, sources = [], []
    pending = list(attempts)

    def connect():
        if not pending:
            stop.set()
            raise ConnectionError("no more attempts")
        attempt = pending.pop(0)
        if isinstance(attempt, Exception):
            raise attempt
        sources.append(Source(clock, attempt))
        return sources[-1]

    def wait(seconds, _stop):
        waits.append(seconds)
        clock.now += seconds
        return stop.is_set()

    runner = ha_client.HaRunner(TOKEN, [BILGE], out, connect, clock=clock, wait=wait, rng=lambda: 0.5,
                                disabled_retry_s=retry)
    runner.run(stop)
    items = []
    while not out.empty():
        items.append(out.get_nowait())
    return items, waits, sources


HELLO = [{"type": "auth_required"}, {"type": "auth_ok"}, {"id": 1, "type": "result", "success": True},
         {"id": 1, "type": "event", "event": {"a": {BILGE: {"s": "on"}}}}]


class RunnerTests(unittest.TestCase):
    def test_handshake_subscribes_and_queues_state(self) -> None:
        items, _, sources = run_attempts([HELLO + ["idle", {"id": 1, "type": "event", "event": {
            "c": {BILGE: {"+": {"s": "off"}}}}}, "close"]])
        self.assertEqual(items[0], ("connected",))
        self.assertEqual([i for i in items if i[0] == "update"], [("update", BILGE, "on"), ("update", BILGE, "off")])
        self.assertEqual(items[-1], ("disconnected",))
        self.assertEqual(sources[0].sent[0], {"type": "auth", "access_token": TOKEN})
        self.assertEqual(sources[0].sent[1], {"id": 1, "type": "subscribe_entities", "entity_ids": [BILGE]})
        self.assertTrue(sources[0].closed)

    def test_token_is_sent_only_in_the_auth_frame_and_never_queued(self) -> None:
        items, _, sources = run_attempts([HELLO + ["close"]])
        self.assertNotIn(TOKEN, json.dumps([f for f in sources[0].sent[1:]]))
        self.assertNotIn(TOKEN, json.dumps(items))

    def test_auth_invalid_disables_logs_once_and_retries_every_300_s(self) -> None:
        bad = [{"type": "auth_required"}, {"type": "auth_invalid", "message": "Invalid access token"}]
        with self.assertLogs("vesselstack.displays.bridge.ha", level="INFO") as logs:
            items, waits, sources = run_attempts([bad, bad, bad])
        self.assertEqual(items, [("disabled",)])                  # announced once, not flapping
        self.assertEqual(len(sources), 3)
        self.assertEqual(waits[:3], [300.0, 300.0, 300.0])
        errors = [r for r in logs.records if r.levelno >= logging.ERROR]
        self.assertEqual(len(errors), 1)
        self.assertNotIn(TOKEN, "\n".join(logs.output))

    def test_recovers_after_disabled_when_the_token_is_fixed(self) -> None:
        bad = [{"type": "auth_required"}, {"type": "auth_invalid"}]
        items, _, _ = run_attempts([bad, HELLO + ["close"]])
        self.assertEqual([i for i in items if len(i) == 1], [("disabled",), ("connected",), ("disconnected",)])

    def test_connect_failures_back_off_with_jitter_up_to_30_s(self) -> None:
        items, waits, _ = run_attempts([ConnectionError("down")] * 8)
        self.assertEqual(waits[:6], [1.0, 2.0, 4.0, 8.0, 16.0, 30.0])
        self.assertEqual(items, [])                                # never connected: nothing to announce

    def test_a_stable_connection_resets_the_backoff(self) -> None:
        long_session = HELLO + ["idle"] * 12 + ["close"]
        _, waits, _ = run_attempts([ConnectionError("x"), ConnectionError("x"), long_session])
        self.assertEqual(waits[:3], [1.0, 2.0, 1.0])

    def test_fallback_when_subscribe_entities_fails(self) -> None:
        script = [{"type": "auth_required"}, {"type": "auth_ok"},
                  {"id": 1, "type": "result", "success": False, "error": {"code": "unknown_command"}},
                  {"id": 3, "type": "result", "success": True,
                   "result": [{"entity_id": BILGE, "state": "off"}, {"entity_id": "light.x", "state": "on"}]},
                  {"id": 2, "type": "result", "success": True},
                  {"id": 2, "type": "event", "event": {"event_type": "state_changed", "data": {
                      "entity_id": BILGE, "new_state": {"state": "on"}}}}, "close"]
        items, _, sources = run_attempts([script])
        self.assertEqual(items[0], ("update", BILGE, "off"))
        self.assertIn(("connected",), items)
        self.assertEqual([i for i in items if i[0] == "update"], [("update", BILGE, "off"), ("update", BILGE, "on")])
        self.assertEqual([f.get("type") for f in sources[0].sent],
                         ["auth", "subscribe_entities", "subscribe_events", "get_states"])

    def test_failed_subscription_reconnects(self) -> None:
        script = [{"type": "auth_required"}, {"type": "auth_ok"},
                  {"id": 1, "type": "result", "success": False}, {"id": 2, "type": "result", "success": False}]
        items, waits, sources = run_attempts([script, HELLO + ["close"]])
        self.assertEqual(len(sources), 2)
        self.assertEqual(waits[0], 1.0)

    def test_handshake_timeout(self) -> None:
        items, waits, sources = run_attempts([["idle"] * 40, HELLO + ["close"]])
        self.assertEqual(len(sources), 2)                          # gave up on the silent server, tried again
        self.assertEqual(items[0], ("connected",))

    def test_garbage_frames_are_ignored(self) -> None:
        items, _, _ = run_attempts([["not json", HELLO[0], {"type": "mystery"}] + HELLO[1:] + ["close"]])
        self.assertEqual(items[0], ("connected",))

    def test_stop_ends_the_loop_without_further_announcements(self) -> None:
        clock, out, stop = Clock(), queue.Queue(), threading.Event()
        stop.set()
        runner = ha_client.HaRunner(TOKEN, [BILGE], out, lambda: self.fail("must not connect"), clock=clock)
        runner.run(stop)
        self.assertTrue(out.empty())


class ThreadTests(unittest.TestCase):
    def test_a_crash_is_caught_logged_and_restarted(self) -> None:
        calls = []

        class Boom:
            out = queue.Queue()

            def run(self, stop):
                calls.append(1)
                if len(calls) < 3:
                    raise RuntimeError(f"boom {TOKEN}")
                stop.wait(5)

        waits = []
        thread = ha_client.HaThread(Boom(), wait=lambda s, stop: waits.append(s))
        with self.assertLogs("vesselstack.displays.bridge.ha", level="ERROR") as logs:
            thread.start()
            for _ in range(200):
                if len(calls) >= 3:
                    break
                threading.Event().wait(0.01)
            thread.stop()
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual(waits[:2], [1.0, 2.0])
        self.assertEqual(sum(1 for r in logs.records if "crashed" in r.getMessage()), 2)
        items = []
        while not thread.queue.empty():
            items.append(thread.queue.get_nowait())
        self.assertEqual(items[:2], [("disconnected",), ("disconnected",)])

    def test_real_thread_starts_and_stops_cleanly(self) -> None:
        out = queue.Queue()
        runner = ha_client.HaRunner(TOKEN, [BILGE], out, lambda: (_ for _ in ()).throw(ConnectionError("x")))
        thread = ha_client.HaThread(runner)
        with self.assertLogs("vesselstack.displays.bridge.ha", level="WARNING"):
            thread.start()
            threading.Event().wait(0.2)
            thread.stop()
        self.assertFalse(thread._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
