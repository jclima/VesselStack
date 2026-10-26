#!/usr/bin/env python3
"""Bridge loop tests with fakes: no network, no paho-mqtt, no websockets."""

from __future__ import annotations

import json
import logging
import os
import queue
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
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

    def __init__(self, attempts, config=CONFIG, ha=None):
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
                                    wait=wait, rng=lambda: 0.5, ha=ha)

    def run(self):
        self.bridge.run(self.stop)
        return self.publisher


NOTE_PATH = "notifications.navigation.anchor"
NOTE_TOPIC = "vesselstack/v1/notifications/anchor"
NOTE_CONFIG = dict(CONFIG, notifications={"anchor": {"path": NOTE_PATH, "label": "Anchor"}})


def note_delta(value, ts="2026-01-01T00:00:00.000Z"):
    return json.dumps({"updates": [{"timestamp": ts, "values": [{"path": NOTE_PATH, "value": value}]}]})


class NotificationBridgeTests(unittest.TestCase):
    def sevs(self, pub):
        return [(json.loads(p)["st"], json.loads(p)["sev"]) for p, _ in pub.on(NOTE_TOPIC)]

    def test_subscribe_frame_and_missing_before_signalk(self):
        h = Harness([[("idle",), ("close",)]], NOTE_CONFIG)
        pub = h.run()
        frame = json.loads(h.sources[0].sent[0])
        paths = {e["path"]: e["period"] for e in frame["subscribe"]}
        self.assertEqual(paths, {SOG: 500, NOTE_PATH: 1000})
        self.assertEqual(self.sevs(Harness([], NOTE_CONFIG).run())[0], ("missing", "unknown"))

    def test_raise_then_clear_then_disconnect(self):
        script = [("idle",)] * 4 + [("msg", note_delta({"state": "alarm", "message": "Anchor drag"})),
                                    ("idle",), ("msg", note_delta(None)), ("idle",), ("close",)]
        pub = Harness([script], NOTE_CONFIG).run()
        seen = []
        for item in self.sevs(pub):
            if not seen or seen[-1] != item:
                seen.append(item)
        self.assertEqual(seen, [("ok", "unknown"), ("ok", "ok"), ("ok", "alarm"),
                                ("ok", "ok"), ("stale", "unknown")])
        self.assertNotIn("Anchor drag", json.dumps(pub.sent))

    def test_instrument_engine_ignores_notification_objects_and_vice_versa(self):
        script = [("msg", note_delta({"state": "warn"})), ("msg", delta(10 * KN)), ("idle",)]
        pub = Harness([script], NOTE_CONFIG).run()
        self.assertIn(("ok", "warn"), self.sevs(pub))
        self.assertEqual(len([p for p, _ in pub.on(SOG_TOPIC) if json.loads(p)["st"] == "ok"]), 1)

    def test_notifications_only_config_is_loadable(self):
        cfg = json.loads((ROOT / "displays" / "displays.example.json").read_text())
        cfg["instruments"], cfg["displays"] = {}, {}
        cfg["notifications"] = {"anchor": {"path": NOTE_PATH, "label": "Anchor"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "displays.json"
            path.write_text(json.dumps(cfg))
            self.assertIn("anchor", bridge.load_config(str(path))["notifications"])


SUMMARY = topics.ALARM_SUMMARY_TOPIC


class AlarmSummaryBridgeTests(unittest.TestCase):
    def summaries(self, pub):
        return [(json.loads(p), r, q) for t, p, r, q in pub.sent if t == SUMMARY]

    def test_alarm_raise_clear_and_disconnect(self):
        script = [("idle",)] * 4 + [("msg", note_delta({"state": "alarm", "message": "secret text"})),
                                    ("idle",), ("msg", note_delta(None)), ("idle",), ("close",)]
        pub = Harness([script], NOTE_CONFIG).run()
        seen = []
        for body, retain, _ in self.summaries(pub):
            self.assertTrue(retain)
            key = (body["max"], body["na"], body["nu"], tuple(body["ids"]))
            if not seen or seen[-1] != key:
                seen.append(key)
        self.assertEqual(seen, [("unknown", 0, 1, ()), ("ok", 0, 0, ()), ("alarm", 1, 0, ("anchor",)),
                                ("ok", 0, 0, ()), ("unknown", 0, 1, ())])
        self.assertNotIn("secret text", json.dumps(pub.sent))

    def test_instrument_without_thresholds_is_not_counted_and_nothing_published(self):
        pub = Harness([[("msg", delta(10 * KN)), ("idle",), ("close",)]]).run()
        self.assertEqual(self.summaries(pub), [])

    def test_republished_each_heartbeat_but_not_on_every_tick(self):
        pub = Harness([[("idle",)] * 14 + [("close",)]], NOTE_CONFIG).run()
        times = [body["ts"] for body, _, _ in self.summaries(pub)]
        self.assertGreaterEqual(len(times), 3)
        self.assertLess(len(times), 8)

    def test_clean_shutdown_publishes_unknown_last(self):
        script = [("idle",)] * 4 + [("msg", note_delta({"state": "alarm"})), ("idle",)]
        h = Harness([script], NOTE_CONFIG)
        pub = h.run()
        self.assertTrue(any(b["max"] == "alarm" for b, _, _ in self.summaries(pub)))
        last, retain, qos = self.summaries(pub)[-1]
        self.assertEqual((last["max"], last["na"], last["nu"], last["ids"]), ("unknown", 0, 1, []))
        self.assertTrue(retain)
        self.assertEqual(qos, 1)
        self.assertEqual(pub.sent[-1][0], STATUS)
        self.assertEqual(pub.sent[-1][1], "offline")

    def test_timestamp_is_utc_iso(self):
        pub = Harness([[("idle",), ("close",)]], NOTE_CONFIG).run()
        self.assertRegex(self.summaries(pub)[0][0]["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


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

    def test_one_bad_message_does_not_drop_the_connection(self):
        h = Harness([[("msg", delta(5 * KN)), ("msg", delta(6 * KN)), ("msg", delta(7 * KN)),
                      ("idle",)]])
        calls = []
        real = h.bridge.handle_message

        def flaky(message):
            calls.append(message)
            if len(calls) == 2:
                raise RuntimeError("boom")
            return real(message)
        h.bridge.handle_message = flaky
        with self.assertLogs(bridge.LOG, "WARNING") as logs:
            pub = h.run()
        self.assertEqual(len(h.sources), 1)  # no reconnect
        self.assertEqual(len(calls), 3)  # the message after the failing one was still processed
        self.assertEqual(sum("dropped an unprocessable" in line for line in logs.output), 1)
        self.assertNotIn("stream error", " ".join(logs.output))
        oks = [json.loads(p)["v"] for p, _ in pub.on(SOG_TOPIC) if json.loads(p)["st"] == "ok"]
        self.assertIn(7.0, oks)

    def test_bad_message_logging_is_rate_limited_with_a_suppressed_count(self):
        h = Harness([])
        h.bridge.handle_message = mock.Mock(side_effect=ValueError("bad"))
        with self.assertLogs(bridge.LOG, "WARNING") as logs:
            for _ in range(5):
                h.bridge._handle_safely("x")
            self.assertEqual(len(logs.output), 1)
            h.clock.now += bridge.MESSAGE_ERROR_LOG_INTERVAL_S
            h.bridge._handle_safely("x")
        self.assertEqual(len(logs.output), 2)
        self.assertIn("4 similar error(s) suppressed", logs.output[1])

    def test_mqtt_client_queue_is_bounded(self):
        created = []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                created.append(self)
                self.calls = []

            def __getattr__(self, name):
                def record(*args, **kwargs):
                    self.calls.append((name, args, kwargs))
                return record

        fake_mqtt = mock.Mock()
        fake_mqtt.Client = FakeClient
        fake_pkg = mock.Mock(client=fake_mqtt)
        settings = bridge.load_settings({
            "SIGNALK_URL": "http://127.0.0.1:3000", "DISPLAYS_CONFIG": "/x",
            "DISPLAYS_BRIDGE_MQTT_USERNAME": "u", "DISPLAYS_BRIDGE_MQTT_PASSWORD": "p"})
        with mock.patch.dict(sys.modules, {"paho": mock.Mock(mqtt=fake_pkg),
                                           "paho.mqtt": fake_pkg,
                                           "paho.mqtt.client": fake_mqtt}):
            bridge.MqttPublisher(settings).start(lambda: None)
        names = [name for name, _, _ in created[0].calls]
        self.assertIn("max_queued_messages_set", names)
        call = next(c for c in created[0].calls if c[0] == "max_queued_messages_set")
        self.assertEqual(call[1], (bridge.MQTT_MAX_QUEUED_MESSAGES,))
        self.assertEqual(bridge.MQTT_MAX_QUEUED_MESSAGES, 1000)

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

    @staticmethod
    def systemd_unquote(line):
        """systemd.exec(5) double-quoted EnvironmentFile value: backslash escapes the
        next character (\\ and \" are the only ones the renderer emits); no $ or %
        expansion; a bare quote ends the value."""
        key, raw = line.split("=", 1)
        assert raw.startswith('"') and raw.endswith('"') and len(raw) >= 2, raw
        body, out, i = raw[1:-1], [], 0
        while i < len(body):
            ch = body[i]
            if ch == "\\":
                out.append(body[i + 1])
                i += 2
                continue
            assert ch != '"', f"unescaped quote in {raw!r}"
            out.append(ch)
            i += 1
        return key, "".join(out)

    def test_env_line_survives_a_systemd_style_parse(self):
        tokens = ['a$b`c"d\\e\'f#g;h%i j  k', " lead and trail ", "$HOME ${PATH} %h %n \\n \\x41 \\$",
                  "\\", '"', "'", "#start", 'x\\"y', "\\\\", "trail\\"]
        for token in tokens:
            key, value = self.systemd_unquote(displayctl.systemd_env_line("T", token).rstrip("\n"))
            self.assertEqual((key, value), ("T", token))
        for bad in ("a\tb", "a\x7fb", "a\0b"):
            with self.assertRaises(ValueError):
                displayctl.systemd_env_line("T", bad)

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
            self.assertNotIn("pa\"ss", out + err)
            # The file round-trips through the bridge's own settings loader.
            values = {}
            for line in text.splitlines():
                key, raw = line.split("=", 1)
                values[key] = json.loads(raw)
            self.assertEqual(bridge.load_settings(values).mqtt_password, 'pa"ss')

    def test_bridge_env_carries_boat_timezone_only_when_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "displays.secrets.json").write_text(json.dumps(
                {"schema": 1, "bridge": {"mqtt_password": "pw"}, "displays": {}}))
            output = root / "config" / "displays-bridge.env"
            env = {"SIGNALK_URL": "http://127.0.0.1:3000", "DISPLAYS_BRIDGE_MQTT_USERNAME": "vesselstack-bridge"}
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("BOAT_TIMEZONE", None)
                code, _, err = self.run_cmd(root, output, env)
                self.assertEqual(code, 0, err)
                self.assertNotIn("BOAT_TIMEZONE", output.read_text())
                code, _, err = self.run_cmd(root, output, {**env, "BOAT_TIMEZONE": "Europe/Oslo"})
            self.assertEqual(code, 0, err)
            self.assertIn('BOAT_TIMEZONE="Europe/Oslo"\n', output.read_text())

    def test_bridge_env_requires_a_generated_password(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            output = root / "config" / "displays-bridge.env"
            code, _, err = self.run_cmd(root, output, {})
            self.assertEqual(code, 1)
            self.assertIn("bridge-env failed", err)
            self.assertFalse(output.exists())


CLOCK_TOPIC = topics.BRIDGE_CLOCK_TOPIC
HEADER_CONFIG = dict(CONFIG, displays={"helm": {"ui": {"header": True}}})
SCHEDULE_CONFIG = dict(CONFIG, displays={"helm": {"ui": {"night": "schedule"}}},
                       night_schedule={"start": "21:30", "end": "06:15"})


class FakeLocal:
    """A local wall clock the test can move; naive datetimes are fine for strftime."""

    def __init__(self, hour, minute):
        self.value = datetime(2026, 10, 25, hour, minute, 30)

    def __call__(self):
        return self.value


class ClockBridgeTests(unittest.TestCase):
    def run_bridge(self, config, local, script):
        h = Harness([script], config)
        h.bridge._local = local
        h.bridge._wall = lambda: datetime(2026, 10, 25, 21, 5, 30, tzinfo=timezone.utc)
        return h.run()

    def clocks(self, pub):
        return [(json.loads(p), r) for t, p, r, _ in pub.sent if t == CLOCK_TOPIC]

    def test_nothing_is_published_when_no_display_needs_it(self):
        pub = self.run_bridge(CONFIG, FakeLocal(14, 5), [("idle",)] * 8 + [("close",)])
        self.assertEqual(self.clocks(pub), [])
        pub = self.run_bridge(dict(CONFIG, displays={"helm": {"ui": {"night": "manual", "swipe": True}}}),
                              FakeLocal(14, 5), [("idle",)] * 3 + [("close",)])
        self.assertEqual(self.clocks(pub), [])

    def test_payload_shape_retained_and_exact_keys(self):
        pub = self.run_bridge(HEADER_CONFIG, FakeLocal(14, 5), [("idle",)] * 2 + [("close",)])
        body, retain = self.clocks(pub)[0]
        self.assertTrue(retain)
        self.assertEqual(list(body), ["hm", "night", "ts"])
        self.assertEqual(body, {"hm": "14:05", "night": False, "ts": "2026-10-25T21:05:30Z"})
        qos = [q for t, _, _, q in pub.sent if t == CLOCK_TOPIC]
        self.assertTrue(all(q == 1 for q in qos))

    def test_published_on_connect_and_republished_each_heartbeat_not_each_tick(self):
        pub = self.run_bridge(HEADER_CONFIG, FakeLocal(14, 5), [("idle",)] * 14 + [("close",)])
        count = len(self.clocks(pub))
        self.assertGreaterEqual(count, 3)   # connect, then about every 5 s
        self.assertLess(count, 8)

    def test_minute_change_publishes_at_once(self):
        local = FakeLocal(14, 5)
        steps = [("idle",), ("call", lambda: setattr(local, "value", datetime(2026, 10, 25, 14, 6, 0))),
                 ("idle",), ("close",)]
        pub = self.run_bridge(HEADER_CONFIG, local, steps)
        self.assertEqual([b["hm"] for b, _ in self.clocks(pub)][0], "14:05")
        self.assertIn("14:06", [b["hm"] for b, _ in self.clocks(pub)])
        self.assertLess(len(self.clocks(pub)), 6)

    def test_night_flag_follows_the_schedule_across_midnight(self):
        for hour, minute, expected in ((22, 0, True), (3, 0, True), (6, 14, True), (6, 15, False),
                                       (12, 0, False), (21, 29, False), (21, 30, True)):
            pub = self.run_bridge(SCHEDULE_CONFIG, FakeLocal(hour, minute), [("idle",), ("close",)])
            self.assertEqual(self.clocks(pub)[0][0]["night"], expected, (hour, minute))

    def test_night_is_false_without_a_schedule(self):
        pub = self.run_bridge(HEADER_CONFIG, FakeLocal(2, 0), [("idle",), ("close",)])
        self.assertFalse(self.clocks(pub)[0][0]["night"])


class LocalTimeSourceTests(unittest.TestCase):
    def test_boat_timezone_is_used_when_known(self):
        try:
            import zoneinfo
            zoneinfo.ZoneInfo("Europe/Oslo")
        except Exception:
            self.skipTest("no tz database on this machine")
        now = bridge.local_time_source("Europe/Oslo")()
        self.assertEqual(now.tzinfo.key, "Europe/Oslo")

    def test_unknown_zone_falls_back_to_the_system_zone_with_a_warning(self):
        with self.assertLogs("vesselstack.displays.bridge", level="WARNING") as logs:
            now = bridge.local_time_source("Mars/Olympus")()
        self.assertIsNotNone(now.tzinfo)
        self.assertIn("unusable", "\n".join(logs.output))

    def test_system_zone_when_unset(self):
        self.assertIsNotNone(bridge.local_time_source("")().tzinfo)

    def test_settings_carry_boat_timezone(self):
        env = SettingsAndConfigTests.ENV
        self.assertEqual(bridge.load_settings(env).timezone, "")
        self.assertEqual(bridge.load_settings({**env, "BOAT_TIMEZONE": " Europe/Oslo "}).timezone, "Europe/Oslo")


HA_TOPIC = "vesselstack/v1/ha/bilge"
HA_STATUS = topics.BRIDGE_HA_TOPIC
HA_CONFIG = dict(CONFIG, ha={
    "bilge": {"entity_id": "binary_sensor.bilge", "label": "Bilge", "kind": "binary",
              "states": [{"match": "on", "sev": "warn", "text": "RUNNING"},
                         {"match": "off", "sev": "ok", "text": "OFF"}]},
    "high": {"entity_id": "binary_sensor.high_water", "label": "High water", "kind": "binary",
             "states": [{"match": "high", "sev": "alarm", "text": "HIGH"}, {"match": "dry", "sev": "ok", "text": "DRY"}]},
})


class FakeHa:
    """Stands in for ha_client.HaThread: a queue the main loop drains, start/stop recorded."""

    def __init__(self):
        self.queue = queue.Queue()
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def put(self, *item):
        return ("call", lambda: self.queue.put(item))


class HomeAssistantBridgeTests(unittest.TestCase):
    def body(self, pub, topic=HA_TOPIC):
        return [json.loads(p) for p, _ in pub.on(topic)]

    def test_without_a_client_items_are_missing_status_is_disabled_and_signalk_keeps_working(self):
        pub = Harness([[("msg", delta(10 * KN)), ("idle",), ("idle",), ("close",)]], HA_CONFIG).run()
        self.assertEqual({p for p, _ in pub.on(HA_STATUS)}, {"disabled"})
        self.assertTrue(all(r for _, r in pub.on(HA_STATUS)))
        self.assertEqual({(b["st"], b["sev"]) for b in self.body(pub)}, {("missing", "unknown")})
        self.assertIn("ok", pub.states(SOG_TOPIC))
        qos = {q for t, _, _, q in pub.sent if t == HA_STATUS}
        self.assertEqual(qos, {1})

    def test_no_homeassistant_output_without_ha_items(self):
        pub = Harness([[("idle",), ("close",)]]).run()
        self.assertEqual([t for t, *_ in pub.sent if "/ha/" in t or t == HA_STATUS], [])

    def test_connected_updates_publish_index_severity_and_feed_the_summary(self):
        ha = FakeHa()
        script = [ha.put("connected"), ("idle",), ha.put("update", "binary_sensor.bilge", "on"),
                  ha.put("update", "binary_sensor.high_water", "dry"), ("idle",), ("idle",),
                  ha.put("update", "binary_sensor.high_water", "HIGH"), ("idle",), ("idle",), ("close",)]
        h = Harness([script], HA_CONFIG, ha=ha)
        pub = h.run()
        self.assertTrue(ha.started and ha.stopped)
        self.assertEqual([p for p, _ in pub.on(HA_STATUS)][:2], ["disconnected", "connected"])
        bilge = [(b["st"], b["sev"], b["k"], b["v"]) for b in self.body(pub)]
        self.assertIn(("ok", "warn", 0, None), bilge)
        high = [(b["st"], b["sev"], b["k"]) for b in self.body(pub, "vesselstack/v1/ha/high")]
        self.assertIn(("ok", "ok", 1), high)
        self.assertIn(("ok", "alarm", 0), high)
        summaries = [json.loads(p) for p, _ in pub.on(SUMMARY)]
        self.assertTrue(any(x["max"] == "alarm" and x["ids"] == ["high", "bilge"] for x in summaries), summaries)
        self.assertNotIn("RUNNING", json.dumps(pub.sent))
        self.assertNotIn("binary_sensor", json.dumps([m for m in pub.sent if m[0].startswith("vesselstack/v1/ha/")]))

    def test_ha_disconnect_goes_stale_while_signalk_items_stay_ok(self):
        ha = FakeHa()
        script = [ha.put("connected"), ha.put("update", "binary_sensor.bilge", "off"), ("msg", delta(10 * KN)),
                  ("idle",), ("idle",), ha.put("disconnected"), ("msg", delta(10 * KN)), ("idle",), ("idle",),
                  ("msg", delta(10 * KN)), ("idle",), ("close",)]
        pub = Harness([script], HA_CONFIG, ha=ha).run()
        seq = []
        for b in self.body(pub):
            if not seq or seq[-1] != (b["st"], b["sev"]):
                seq.append((b["st"], b["sev"]))
        self.assertEqual(seq[:3], [("missing", "unknown"), ("ok", "ok"), ("stale", "unknown")])
        self.assertEqual(pub.states(SOG_TOPIC)[-1], "ok")
        sk = [json.loads(p) for p, _ in pub.on(SUMMARY)]
        self.assertTrue(any(x["max"] == "unknown" for x in sk))

    def test_disabled_event_publishes_disabled_and_blanks_items(self):
        ha = FakeHa()
        script = [ha.put("connected"), ha.put("update", "binary_sensor.bilge", "on"), ("idle",),
                  ha.put("disabled"), ("idle",), ("idle",), ("close",)]
        pub = Harness([script], HA_CONFIG, ha=ha).run()
        self.assertEqual([p for p, _ in pub.on(HA_STATUS)], ["disconnected", "connected", "disabled"])
        self.assertEqual(self.body(pub)[-1]["sev"], "unknown")

    def test_updates_are_applied_while_signalk_is_down(self):
        ha = FakeHa()
        h = Harness([ConnectionError("down"), [("close",)]], HA_CONFIG, ha=ha)
        ha.queue.put(("connected",))
        ha.queue.put(("update", "binary_sensor.bilge", "on"))
        pub = h.run()
        self.assertIn(("ok", "warn"), {(b["st"], b["sev"]) for b in self.body(pub)})

    def test_unprocessable_queue_items_are_dropped_and_logged_without_content(self):
        ha = FakeHa()
        script = [ha.put("connected"), ha.put("update", "binary_sensor.bilge", object()), ha.put("bogus"), ha.put(),
                  ha.put("update", "binary_sensor.bilge", "on"), ("idle",), ("idle",), ("close",)]
        with self.assertLogs("vesselstack.displays.bridge", level="WARNING") as logs:
            pub = Harness([script], HA_CONFIG, ha=ha).run()
        self.assertIn(("ok", "warn"), {(b["st"], b["sev"]) for b in self.body(pub)})
        self.assertNotIn("binary_sensor", "\n".join(logs.output))

    def test_a_failing_stop_still_publishes_offline(self):
        ha = FakeHa()
        ha.stop = lambda: (_ for _ in ()).throw(RuntimeError("stuck"))
        with self.assertLogs("vesselstack.displays.bridge", level="WARNING"):
            pub = Harness([[("close",)]], HA_CONFIG, ha=ha).run()
        self.assertEqual(pub.on(STATUS)[-1], ("offline", True))

    def test_clean_stop_publishes_every_counted_ha_item_as_unknown(self):
        ha = FakeHa()
        script = [ha.put("connected"), ha.put("update", "binary_sensor.high_water", "high"), ("idle",), ("idle",),
                  ("close",)]
        pub = Harness([script], HA_CONFIG, ha=ha).run()
        last = json.loads(pub.on(SUMMARY)[-1][0])
        self.assertEqual((last["max"], last["na"], last["ids"]), ("unknown", 0, []))

    def test_ha_only_config_is_loadable(self):
        cfg = json.loads((ROOT / "displays" / "displays.example.json").read_text())
        cfg["instruments"], cfg["displays"] = {}, {}
        cfg["ha"] = HA_CONFIG["ha"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "displays.json"
            path.write_text(json.dumps(cfg))
            self.assertIn("bilge", bridge.load_config(str(path))["ha"])


class HomeAssistantSettingsTests(unittest.TestCase):
    ENV = SettingsAndConfigTests.ENV

    def test_settings_read_url_and_token_and_redact_the_token(self):
        s = bridge.load_settings({**self.ENV, "HOME_ASSISTANT_URL": " http://ha:8123 ", "DISPLAYS_HA_TOKEN": " ha-tok "})
        self.assertEqual((s.ha_url, s.ha_token), ("http://ha:8123", "ha-tok"))
        self.assertIn("ha-tok", s.secrets())
        self.assertEqual(bridge.load_settings(self.ENV).ha_token, "")

    def test_make_ha_client_variants(self):
        settings = lambda **kw: bridge.load_settings({**self.ENV, **kw})  # noqa: E731
        with self.assertLogs("vesselstack.displays.bridge", level="WARNING") as logs:
            self.assertIsNone(bridge.make_ha_client(settings(), HA_CONFIG))
        self.assertIn("DISPLAYS_HA_TOKEN is empty", "\n".join(logs.output))
        self.assertIsNone(bridge.make_ha_client(settings(DISPLAYS_HA_TOKEN="t"), CONFIG))     # no ha items: silent
        with self.assertLogs("vesselstack.displays.bridge", level="WARNING") as logs:
            self.assertIsNone(bridge.make_ha_client(settings(DISPLAYS_HA_TOKEN="secret-ha-tok",
                                                             HOME_ASSISTANT_URL="ftp://nope"), HA_CONFIG))
        self.assertNotIn("secret-ha-tok", "\n".join(logs.output))
        with self.assertLogs("vesselstack.displays.bridge", level="WARNING") as logs:
            self.assertIsNone(bridge.make_ha_client(settings(DISPLAYS_HA_TOKEN="t",
                                                             HOME_ASSISTANT_URL="http://u" + ":pw-secret@h:8123"), HA_CONFIG))
        self.assertNotIn("pw-secret", "\n".join(logs.output))
        with mock.patch.object(bridge, "ha_connector", return_value=lambda: None) as connector:
            client = bridge.make_ha_client(settings(DISPLAYS_HA_TOKEN="t"), HA_CONFIG)
        connector.assert_called_once_with("http://127.0.0.1:8123")
        self.assertEqual(sorted(client.runner._entities), ["binary_sensor.bilge", "binary_sensor.high_water"])

    def test_bridge_env_passthrough_is_not_in_this_module(self):
        self.assertNotIn("DISPLAYS_HA_TOKEN", bridge.Settings.__dataclass_fields__)   # the field is ha_token


if __name__ == "__main__":
    unittest.main()
