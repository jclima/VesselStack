#!/usr/bin/env python3
"""Bridge loop tests with fakes: no network, no paho-mqtt, no websockets."""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "displays"))

import bridge  # noqa: E402
import displayctl  # noqa: E402
import topics  # noqa: E402

bridge.LOG.addHandler(logging.NullHandler())  # keep expected warnings off stderr

KN = 1852 / 3600
SOG = "navigation.speedOverGround"
CONFIG = {
    "heartbeat_s": 5,
    "instruments": {
        "sog": {"path": SOG, "convert": "m/s->kn", "unit": "kn",
                "precision": 1, "stale_after_s": 10, "min_interval_ms": 500},
    },
}
SOG_TOPIC = "vesselstack/v1/instruments/sog"
STATUS = topics.BRIDGE_STATUS_TOPIC
SIGNALK = topics.BRIDGE_SIGNALK_TOPIC


def delta(value, urn=None, ts="2026-01-01T00:00:00.000Z"):
    msg = {"updates": [{"timestamp": ts, "values": [{"path": SOG, "value": value}]}]}
    if urn:
        msg["context"] = f"vessels.{urn}"
    return json.dumps(msg)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakePublisher:
    def __init__(self):
        self.sent = []
        self.started = False
        self.stopped = False

    def start(self, on_connect):
        self.started = True
        on_connect()

    def publish(self, topic, payload, retain, qos=0):
        self.sent.append((topic, payload, retain, qos))

    def stop(self):
        self.stopped = True

    def on(self, topic):
        return [(p, r) for t, p, r, _ in self.sent if t == topic]

    def states(self, topic):
        return [json.loads(p)["st"] for p, _ in self.on(topic)]


class FakeSource:
    """script items: ("msg", text) | ("idle",) | ("close",) | ("call", fn)"""

    def __init__(self, clock, script):
        self.clock, self.script, self.sent, self.closed = clock, list(script), [], False

    def send(self, text):
        self.sent.append(text)

    def recv(self, timeout):
        item = self.script.pop(0) if self.script else ("close",)
        if item[0] == "msg":
            return item[1]
        if item[0] == "idle":
            self.clock.now += timeout
            return None
        if item[0] == "call":
            item[1]()
            return None
        raise bridge.SourceClosed("closed by fake")

    def close(self):
        self.closed = True


class Harness:
    """Runs a Bridge against scripted connection attempts; stops when they run out."""

    def __init__(self, attempts, config=CONFIG):
        self.clock = FakeClock()
        self.publisher = FakePublisher()
        self.stop = threading.Event()
        self.waits = []
        self.attempts = list(attempts)
        self.sources = []
        self.attempt_times = []

        def connect():
            self.attempt_times.append(self.clock.now)
            if not self.attempts:
                self.stop.set()
                raise ConnectionError("no more attempts")
            attempt = self.attempts.pop(0)
            if isinstance(attempt, Exception):
                raise attempt
            source = FakeSource(self.clock, attempt)
            self.sources.append(source)
            return source

        def wait(seconds, stop):
            self.waits.append(seconds)
            self.clock.now += seconds
            return stop.is_set()

        self.bridge = bridge.Bridge(config, self.publisher, connect, clock=self.clock,
                                    wait=wait, rng=lambda: 0.5)

    def run(self):
        self.bridge.run(self.stop)
        return self.publisher


class BridgeLoopTests(unittest.TestCase):
    def test_startup_publishes_status_and_missing_instruments(self):
        pub = Harness([]).run()
        self.assertTrue(pub.started)
        self.assertEqual(pub.on(STATUS)[0], ("online", True))
        self.assertEqual(pub.on(SIGNALK)[0], ("disconnected", True))
        self.assertEqual(pub.states(SOG_TOPIC)[0], "missing")
        self.assertTrue(all(retain for _, _, retain, _ in pub.sent))

    def test_signalk_transitions_and_subscribe_frame(self):
        h = Harness([[("idle",), ("close",)]])
        pub = h.run()
        seen = []
        for payload, _ in pub.on(SIGNALK):  # collapse repeats into transitions
            if not seen or seen[-1] != payload:
                seen.append(payload)
        self.assertEqual(seen, ["disconnected", "connected", "disconnected"])
        frame = json.loads(h.sources[0].sent[0])
        self.assertEqual(frame["context"], "vessels.self")
        self.assertEqual(frame["subscribe"][0]["path"], SOG)
        self.assertTrue(h.sources[0].closed)

    def test_delta_publishes_converted_value(self):
        hello = json.dumps({"name": "signalk-server", "self": "vessels.urn:mrn:test:1"})
        h = Harness([[("msg", hello), ("msg", delta(10 * KN, "urn:mrn:test:1")), ("idle",)]])
        pub = h.run()
        ok = [json.loads(p) for p, _ in pub.on(SOG_TOPIC) if json.loads(p)["st"] == "ok"]
        self.assertEqual(len(ok), 1)
        self.assertEqual((ok[0]["v"], ok[0]["u"]), (10.0, "kn"))
        self.assertEqual(h.bridge.self_urn, "vessels.urn:mrn:test:1")

    def test_foreign_vessel_and_garbage_are_ignored(self):
        other = delta(5, "urn:mrn:other")
        h = Harness([[("msg", "not json"), ("msg", "[]"), ("msg", other), ("idle",)]])
        h.bridge.self_urn = "urn:mrn:mine"
        pub = h.run()
        self.assertNotIn("ok", pub.states(SOG_TOPIC))

    def test_heartbeat_republishes_unchanged_value(self):
        script = [("msg", delta(10 * KN))] + [("idle",)] * 14
        pub = Harness([script]).run()
        oks = [p for p, _ in pub.on(SOG_TOPIC) if json.loads(p)["st"] == "ok"]
        self.assertGreaterEqual(len(oks), 2)

    def test_disconnect_makes_instrument_stale_after_threshold(self):
        # Value arrives, SignalK drops, reconnects keep failing: the clock-driven
        # tick must still age the value out after stale_after_s (10 s).
        h = Harness([[("msg", delta(10 * KN)), ("close",)]] + [ConnectionError("down")] * 30)
        pub = h.run()
        self.assertIn("stale", pub.states(SOG_TOPIC))
        stale = [json.loads(p) for p, _ in pub.on(SOG_TOPIC) if json.loads(p)["st"] == "stale"][0]
        self.assertIsNone(stale["v"])
        self.assertGreaterEqual(stale["age"], 10)
        self.assertLess(stale["age"], 10 + 2)  # within one tick of the threshold
        payloads = [p for p, _ in pub.on(SIGNALK)]
        self.assertEqual(payloads[payloads.index("connected") + 1], "disconnected")

    def test_backoff_doubles_with_cap(self):
        h = Harness([ConnectionError("down")] * 8)
        h.run()
        # rng=0.5 gives a jitter factor of exactly 1.0.
        gaps = [round(b - a) for a, b in zip(h.attempt_times, h.attempt_times[1:])]
        self.assertEqual(gaps[:7], [1, 2, 4, 8, 16, 30, 30])
        self.assertLessEqual(max(h.waits), 1.0)  # ticks keep running while backing off

    def test_backoff_resets_after_stable_connection(self):
        long_session = [("idle",)] * 12 + [("close",)]
        h = Harness([ConnectionError("a"), ConnectionError("b"), long_session,
                     ConnectionError("c")])
        h.run()
        gaps = [round(b - a) for a, b in zip(h.attempt_times, h.attempt_times[1:])]
        self.assertEqual((gaps[0], gaps[1]), (1, 2))
        self.assertEqual(gaps[3], 1)  # after the stable session the delay restarts at 1 s

    def test_mqtt_reconnect_republishes_online_and_signalk_state(self):
        h = Harness([])
        h.bridge.publisher.publish(STATUS, "x", True)
        h.bridge.signalk_state = "connected"
        h.bridge.on_mqtt_connected()
        self.assertEqual(h.publisher.sent[-2][:3], (STATUS, "online", True))
        self.assertEqual(h.publisher.sent[-1][:3], (SIGNALK, "connected", True))

    def test_sigterm_path_publishes_offline_and_stops_publisher(self):
        h = Harness([[("idle",), ("call", lambda: h.stop.set()), ("idle",)]])
        pub = h.run()
        self.assertEqual(pub.on(STATUS)[-1], ("offline", True))
        self.assertEqual(pub.on(SIGNALK)[-1], ("disconnected", True))
        self.assertTrue(pub.stopped)
        self.assertTrue(h.sources[0].closed)

    def test_stop_during_backoff_is_prompt(self):
        h = Harness([ConnectionError("down")])
        h.stop.set()
        pub = h.run()
        self.assertEqual(pub.on(STATUS)[-1], ("offline", True))
        self.assertTrue(pub.stopped)


class SettingsAndConfigTests(unittest.TestCase):
    ENV = {"DISPLAYS_CONFIG": "/x/displays.json", "SIGNALK_URL": "http://127.0.0.1:3000",
           "DISPLAYS_BRIDGE_MQTT_USERNAME": "vesselstack-bridge",
           "DISPLAYS_BRIDGE_MQTT_PASSWORD": "pw-secret-value"}

    def test_defaults_and_validation(self):
        s = bridge.load_settings(self.ENV)
        self.assertEqual((s.mqtt_host, s.mqtt_port, s.signalk_token), ("127.0.0.1", 1883, ""))
        for key in ("DISPLAYS_CONFIG", "SIGNALK_URL", "DISPLAYS_BRIDGE_MQTT_PASSWORD"):
            with self.assertRaises(bridge.SettingsError):
                bridge.load_settings({k: v for k, v in self.ENV.items() if k != key})
        with self.assertRaises(bridge.SettingsError):
            bridge.load_settings({**self.ENV, "MQTT_PORT": "99999"})
        with self.assertRaises(bridge.SettingsError):
            bridge.load_settings({**self.ENV, "SIGNALK_URL": "http://user" + ":pw@host:3000"})

    def test_token_header_only_when_configured(self):
        self.assertEqual(bridge.signalk_headers(""), {})
        self.assertEqual(bridge.signalk_headers("tok-123"), {"Authorization": "Bearer tok-123"})
        s = bridge.load_settings({**self.ENV, "DISPLAYS_SIGNALK_TOKEN": "  tok-123 "})
        self.assertEqual(s.signalk_token, "tok-123")

    def test_load_config_accepts_example_and_rejects_bad_files(self):
        config = bridge.load_config(str(ROOT / "displays" / "displays.example.json"))
        self.assertIn("sog", config["instruments"])
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "displays.json"
            bad.write_text('{"schema": 1}')
            with self.assertRaisesRegex(ValueError, "invalid"):
                bridge.load_config(str(bad))
            bad.write_text("{not json")
            with self.assertRaisesRegex(ValueError, "cannot read"):
                bridge.load_config(str(bad))
            with self.assertRaisesRegex(ValueError, "cannot read"):
                bridge.load_config(str(Path(directory) / "missing.json"))
            seeded = json.loads((ROOT / "displays" / "displays.example.json").read_text())
            seeded["instruments"], seeded["displays"] = {}, {}
            bad.write_text(json.dumps(seeded))
            with self.assertRaisesRegex(ValueError, "no instruments"):
                bridge.load_config(str(bad))

    def test_seeded_instruments_only_config_is_valid(self):
        seeded = json.loads((ROOT / "displays" / "displays.example.json").read_text())
        seeded["displays"] = {}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "displays.json"
            path.write_text(json.dumps(seeded))
            self.assertEqual(len(bridge.load_config(str(path))["instruments"]), 4)

    def test_main_invalid_config_exits_nonzero_with_clear_log_and_no_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "displays.json"
            path.write_text('{"schema": 1}')
            env = {**self.ENV, "DISPLAYS_CONFIG": str(path),
                   "DISPLAYS_SIGNALK_TOKEN": "tok-very-secret"}
            with self.assertLogs("vesselstack.displays.bridge", level="INFO") as logs:
                code = bridge.main(env)
        self.assertEqual(code, bridge.EXIT_CONFIG)
        text = "\n".join(logs.output)
        self.assertIn("invalid displays configuration", text)
        for secret in ("pw-secret-value", "tok-very-secret"):
            self.assertNotIn(secret, text)

    def test_main_invalid_settings_exits_nonzero(self):
        with self.assertLogs("vesselstack.displays.bridge", level="ERROR") as logs:
            self.assertEqual(bridge.main({}), bridge.EXIT_CONFIG)
        self.assertIn("SIGNALK_URL is required", "\n".join(logs.output))

    def test_script_exits_2_on_invalid_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "displays.json"
            path.write_text("{}")
            env = {**self.ENV, "DISPLAYS_CONFIG": str(path), "PATH": os.environ.get("PATH", "")}
            result = subprocess.run([sys.executable, "-I", str(ROOT / "displays" / "bridge.py")],
                                    env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid displays configuration", result.stderr)
        self.assertNotIn("pw-secret-value", result.stderr + result.stdout)

    def test_redacting_filter_masks_secrets(self):
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(bridge.RedactingFilter(["tok-abc", ""]))
        log = logging.getLogger("vesselstack.test.redact")
        log.addHandler(handler)
        log.propagate = False
        log.warning("failed with %s in header", "Bearer tok-abc")
        log.removeHandler(handler)
        self.assertNotIn("tok-abc", stream.getvalue())
        self.assertIn("Bearer ***", stream.getvalue())

    def test_loop_logs_never_contain_secrets(self):
        secret = "tok-in-error"
        h = Harness([ConnectionError(f"rejected Bearer {secret}")])
        handler_stream = StringIO()
        handler = logging.StreamHandler(handler_stream)
        handler.addFilter(bridge.RedactingFilter([secret]))
        bridge.LOG.addHandler(handler)
        bridge.LOG.setLevel(logging.INFO)
        try:
            h.run()
        finally:
            bridge.LOG.removeHandler(handler)
        self.assertIn("SignalK connection failed", handler_stream.getvalue())
        self.assertNotIn(secret, handler_stream.getvalue())


class BridgeEnvTests(unittest.TestCase):
    def test_env_line_escaping(self):
        self.assertEqual(displayctl.systemd_env_line("K", 'a"b\\c$d%e'), 'K="a\\"b\\\\c$d%e"\n')
        with self.assertRaises(ValueError):
            displayctl.systemd_env_line("K", "a\nb")

    def run_cmd(self, root, output, env):
        args = displayctl.build_parser().parse_args(
            ["bridge-env", "--root", str(root), "--output", str(output)])
        out, err = StringIO(), StringIO()
        with mock.patch.dict(os.environ, env, clear=False), \
                redirect_stdout(out), redirect_stderr(err):
            code = args.func(args)
        return code, out.getvalue(), err.getvalue()

    def test_bridge_env_file_content_and_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "displays.secrets.json").write_text(json.dumps(
                {"schema": 1, "bridge": {"mqtt_password": 'pa"ss'}, "displays": {}}))
            output = root / "config" / "displays-bridge.env"
            code, out, err = self.run_cmd(root, output, {
                "SIGNALK_URL": "http://127.0.0.1:3000", "MQTT_PORT": "1884",
                "DISPLAYS_BRIDGE_MQTT_USERNAME": "vesselstack-bridge",
                "DISPLAYS_SIGNALK_TOKEN": "tok"})
            self.assertEqual(code, 0, err)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            text = output.read_text()
            self.assertIn(f'DISPLAYS_CONFIG="{root}/config/displays.json"\n', text)
            self.assertIn('MQTT_HOST="127.0.0.1"\n', text)
            self.assertIn('MQTT_PORT="1884"\n', text)
            self.assertIn('DISPLAYS_BRIDGE_MQTT_PASSWORD="pa\\"ss"\n', text)
            self.assertIn('DISPLAYS_SIGNALK_TOKEN="tok"\n', text)
            self.assertNotIn("pa", out + err)
            # The file round-trips through the bridge's own settings loader.
            values = {}
            for line in text.splitlines():
                key, raw = line.split("=", 1)
                values[key] = json.loads(raw)
            self.assertEqual(bridge.load_settings(values).mqtt_password, 'pa"ss')

    def test_bridge_env_requires_a_generated_password(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            output = root / "config" / "displays-bridge.env"
            code, _, err = self.run_cmd(root, output, {})
            self.assertEqual(code, 1)
            self.assertIn("bridge-env failed", err)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
