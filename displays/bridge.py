#!/usr/bin/env python3
"""VesselStack displays bridge: SignalK (vessels.self) -> Mosquitto, v1 topic schema.

Runtime adapter only. All decisions (conversion, rounding, freshness,
heartbeat, rate limiting) live in topics.Engine; this module wires an Engine to
a SignalK websocket and an MQTT client.

Threading: the Engine is only ever touched by the main thread (one loop that
alternates a bounded websocket read with Engine.tick). paho's network thread
only sends packets and runs on_connect, which publishes retained status
messages and never touches the Engine, so no lock is needed.

Third-party imports (paho-mqtt, websockets) are deferred to the real adapters
so the loop is importable and unit-testable with the standard library alone.
"""
from __future__ import annotations

import json
import logging
import os
import random
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import schema  # noqa: E402
import topics  # noqa: E402

LOG = logging.getLogger("vesselstack.displays.bridge")

CLIENT_ID = "vesselstack-displays-bridge"
EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_DEPENDENCY = 3
TICK_INTERVAL_S = 1.0
BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 30.0
STABLE_CONNECTION_S = 10.0
MESSAGE_ERROR_LOG_INTERVAL_S = 60.0
MQTT_MAX_QUEUED_MESSAGES = 1000
STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"
SIGNALK_CONNECTED = "connected"
SIGNALK_DISCONNECTED = "disconnected"


class SettingsError(Exception):
    pass


class SourceClosed(Exception):
    """The SignalK connection ended (cleanly or not)."""


@dataclass(frozen=True)
class Settings:
    config_path: str
    signalk_url: str
    signalk_token: str
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str
    mqtt_password: str

    def secrets(self) -> list:
        return [value for value in (self.signalk_token, self.mqtt_password) if value]


def load_settings(env) -> Settings:
    def required(key):
        value = (env.get(key) or "").strip()
        if not value:
            raise SettingsError(f"{key} is required")
        return value

    signalk_url = required("SIGNALK_URL")
    try:
        topics.signalk_stream_url(signalk_url)
    except ValueError as exc:
        raise SettingsError(f"SIGNALK_URL is invalid: {exc}") from None
    if "@" in urlsplit(signalk_url).netloc:
        raise SettingsError("SIGNALK_URL must not embed credentials; use DISPLAYS_SIGNALK_TOKEN")
    port_text = (env.get("MQTT_PORT") or "1883").strip()
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise SettingsError("MQTT_PORT must be a TCP port number")
    return Settings(
        config_path=required("DISPLAYS_CONFIG"),
        signalk_url=signalk_url,
        signalk_token=(env.get("DISPLAYS_SIGNALK_TOKEN") or "").strip(),
        mqtt_host=(env.get("MQTT_HOST") or "127.0.0.1").strip(),
        mqtt_port=int(port_text),
        mqtt_username=required("DISPLAYS_BRIDGE_MQTT_USERNAME"),
        mqtt_password=required("DISPLAYS_BRIDGE_MQTT_PASSWORD"),
    )


def load_config(path) -> dict:
    """Load and validate displays.json; raises ValueError listing every problem."""
    try:
        registry = schema.load_registry()
        problems = schema.validate_registry(registry)
        if problems:
            raise ValueError("board registry invalid: " + "; ".join(problems))
        config = schema.load_json(path)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from None
    errors = schema.validate_config(config, registry)
    if errors:
        raise ValueError(f"{path} is invalid: " + "; ".join(errors))
    if not config["instruments"] and not config.get("notifications"):
        raise ValueError(f"{path} defines no instruments; add instruments to publish")
    return config


def signalk_headers(token: str) -> dict:
    """Extra websocket headers: a bearer token only when one is configured."""
    return {"Authorization": f"Bearer {token}"} if token else {}


class RedactingFilter(logging.Filter):
    """Defence in depth: never let a configured secret reach a log line."""

    def __init__(self, secrets):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record):
        message = record.getMessage()
        for secret in self._secrets:
            message = message.replace(secret, "***")
        record.msg, record.args = message, None
        return True


class Bridge:
    """Drives an Engine from a message source and publishes through a publisher.

    publisher: start(on_connect), publish(topic, payload, retain, qos=0), stop()
    connect:   () -> source with send(text), recv(timeout) -> str|bytes|None, close();
               recv raises SourceClosed when the connection ends
    clock:     monotonic seconds, also drives the Engine
    wait:      (seconds, stop) -> True when stop was requested (real: Event.wait)
    """

    def __init__(self, config, publisher, connect, clock=time.monotonic,
                 wait=None, rng=random.random):
        self.engine = topics.Engine(config)
        self.events = topics.EventEngine(config)
        self.frame = json.dumps(topics.subscribe_frame(config), separators=(",", ":"))
        self.publisher = publisher
        self._connect = connect
        self.clock = clock
        self._wait = wait or (lambda seconds, stop: stop.wait(seconds))
        self._rng = rng
        self.self_urn = None
        self.signalk_state = SIGNALK_DISCONNECTED
        self._next_tick = 0.0
        self._error_logged_at = None
        self._errors_suppressed = 0

    # -- publishing -------------------------------------------------------
    def _send(self, messages):
        for topic, payload, retain in messages:
            self.publisher.publish(topic, payload, retain)

    def on_mqtt_connected(self):
        """Called on every (re)connection to the broker, possibly from its thread."""
        self.publisher.publish(topics.BRIDGE_STATUS_TOPIC, STATUS_ONLINE, True, qos=1)
        self.publisher.publish(topics.BRIDGE_SIGNALK_TOPIC, self.signalk_state, True, qos=1)

    def _set_signalk(self, state):
        if state != self.signalk_state:
            self.signalk_state = state
            LOG.info("SignalK %s", state)
            self._send(self.events.source_state("signalk", state == SIGNALK_CONNECTED, self.clock()))
        self.publisher.publish(topics.BRIDGE_SIGNALK_TOPIC, state, True, qos=1)

    def _tick(self):
        now = self.clock()
        if now >= self._next_tick:
            self._next_tick = now + TICK_INTERVAL_S
            self._send(self.engine.tick(now))
            self._send(self.events.tick(now))

    # -- loop -------------------------------------------------------------
    def run(self, stop) -> None:
        self.publisher.start(self.on_mqtt_connected)
        try:
            self._set_signalk(SIGNALK_DISCONNECTED)
            self._loop(stop)
        finally:
            self.shutdown()

    def _loop(self, stop):
        delay = BACKOFF_START_S
        while not stop.is_set():
            source = None
            try:
                source = self._connect()
                source.send(self.frame)
            except Exception as exc:  # any connect failure is retried
                LOG.warning("SignalK connection failed: %s: %s", type(exc).__name__, exc)
                if source is not None:
                    self._close(source)
                if self._idle(self._jittered(delay), stop):
                    return
                delay = min(delay * 2, BACKOFF_MAX_S)
                continue
            self._set_signalk(SIGNALK_CONNECTED)
            started = self.clock()
            try:
                self._pump(source, stop)
            except SourceClosed as exc:
                LOG.warning("SignalK connection closed: %s", exc)
            except Exception as exc:
                LOG.warning("SignalK stream error: %s: %s", type(exc).__name__, exc)
            finally:
                self._close(source)
                self._set_signalk(SIGNALK_DISCONNECTED)
            if stop.is_set():
                return
            delay = BACKOFF_START_S if self.clock() - started >= STABLE_CONNECTION_S \
                else min(delay * 2, BACKOFF_MAX_S)
            if self._idle(self._jittered(delay), stop):
                return

    @staticmethod
    def _close(source):
        try:
            source.close()
        except Exception:  # closing a dead socket is best effort
            pass

    def _jittered(self, delay):
        return min(BACKOFF_MAX_S, delay * (0.8 + 0.4 * self._rng()))

    def _idle(self, seconds, stop) -> bool:
        """Wait while still ticking the Engine; True if stop was requested."""
        deadline = self.clock() + seconds
        while True:
            self._tick()
            remaining = deadline - self.clock()
            if remaining <= 0:
                return stop.is_set()
            if self._wait(min(TICK_INTERVAL_S, remaining), stop):
                return True

    def _pump(self, source, stop):
        while not stop.is_set():
            self._tick()
            timeout = max(0.05, min(TICK_INTERVAL_S, self._next_tick - self.clock()))
            message = source.recv(timeout)
            if message is not None:
                self._handle_safely(message)

    def _handle_safely(self, message):
        """One bad message must not cost the SignalK connection: log it, drop it.

        Logging is rate limited (once per MESSAGE_ERROR_LOG_INTERVAL_S, with the
        number of errors suppressed in between) so a stream of bad messages
        cannot flood the journal.
        """
        try:
            self.handle_message(message)
        except Exception as exc:
            now = self.clock()
            if self._error_logged_at is None or now - self._error_logged_at >= MESSAGE_ERROR_LOG_INTERVAL_S:
                suppressed, self._errors_suppressed = self._errors_suppressed, 0
                self._error_logged_at = now
                LOG.warning("dropped an unprocessable SignalK message: %s: %s%s",
                            type(exc).__name__, exc,
                            f" ({suppressed} similar error(s) suppressed)" if suppressed else "")
            else:
                self._errors_suppressed += 1

    def handle_message(self, message):
        try:
            data = json.loads(message)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        if "updates" not in data and isinstance(data.get("self"), str):  # hello
            self.self_urn = data["self"]
            return
        for path, value, ts in topics.parse_delta(data, self.self_urn):
            now = self.clock()
            self._send(self.engine.update(path, value, ts, now))
            self._send(self.events.update(path, value, ts, now))

    def shutdown(self):
        if self.signalk_state != SIGNALK_DISCONNECTED:
            self._set_signalk(SIGNALK_DISCONNECTED)
        self.publisher.publish(topics.BRIDGE_STATUS_TOPIC, STATUS_OFFLINE, True, qos=1)
        self.publisher.stop()


# -- real adapters ----------------------------------------------------------
class WebsocketSource:
    def __init__(self, connection, closed_exc):
        self._ws = connection
        self._closed_exc = closed_exc

    def send(self, text):
        try:
            self._ws.send(text)
        except self._closed_exc as exc:
            raise SourceClosed(str(exc)) from None

    def recv(self, timeout):
        try:
            return self._ws.recv(timeout=timeout)
        except TimeoutError:
            return None
        except self._closed_exc as exc:
            raise SourceClosed(str(exc)) from None

    def close(self):
        self._ws.close()


def websocket_connector(base_url, token):
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    url = topics.signalk_stream_url(base_url)
    headers = signalk_headers(token)

    def open_connection():
        # legacy=True is websockets' documented "return the connection directly" mode
        # (the default today, but deprecated without the explicit flag).
        connection = connect(url, additional_headers=headers or None, proxy=None,
                             compression=None, open_timeout=10, close_timeout=3,
                             ping_interval=20, ping_timeout=20, legacy=True)
        return WebsocketSource(connection, ConnectionClosed)

    return open_connection


class MqttPublisher:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = None
        self._pending = []

    def start(self, on_connect):
        import paho.mqtt.client as mqtt

        s = self._settings
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID)
        client.username_pw_set(s.mqtt_username, s.mqtt_password)
        client.will_set(topics.BRIDGE_STATUS_TOPIC, STATUS_OFFLINE, qos=1, retain=True)
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        # Bound memory while the broker is down (paho's default queue is unbounded).
        client.max_queued_messages_set(MQTT_MAX_QUEUED_MESSAGES)

        def connected(_client, _userdata, _flags, reason, _properties):
            if reason.is_failure:
                LOG.error("MQTT connection refused: %s", reason)
                return
            LOG.info("MQTT connected to %s:%s", s.mqtt_host, s.mqtt_port)
            on_connect()

        def disconnected(_client, _userdata, _flags, reason, _properties):
            LOG.warning("MQTT disconnected: %s", reason)

        client.on_connect = connected
        client.on_disconnect = disconnected
        client.connect_async(s.mqtt_host, s.mqtt_port, keepalive=30)
        client.loop_start()
        self._client = client

    def publish(self, topic, payload, retain, qos=0):
        info = self._client.publish(topic, payload, qos=qos, retain=retain)
        if qos > 0:
            self._pending = (self._pending + [info])[-8:]

    def stop(self):
        client = self._client
        if client is None:
            return
        for info in self._pending:
            try:
                info.wait_for_publish(timeout=3)
            except (ValueError, RuntimeError):
                pass
        client.disconnect()
        client.loop_stop()


def main(env=None) -> int:
    env = os.environ if env is None else env
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    try:
        settings = load_settings(env)
    except SettingsError as exc:
        LOG.error("invalid bridge settings: %s", exc)
        return EXIT_CONFIG
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactingFilter(settings.secrets()))
    try:
        config = load_config(settings.config_path)
    except ValueError as exc:
        LOG.error("invalid displays configuration: %s", exc)
        return EXIT_CONFIG
    try:
        connect = websocket_connector(settings.signalk_url, settings.signalk_token)
        publisher = MqttPublisher(settings)
        import paho.mqtt.client  # noqa: F401  (fail early if missing)
    except ImportError as exc:
        LOG.error("missing Python dependency (%s); reinstall with displays/.venv/bin/pip "
                  "install -r displays/requirements.txt", exc)
        return EXIT_DEPENDENCY
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _signum, _frame: stop.set())
    LOG.info("starting: %d instrument(s), %d notification(s), SignalK %s, MQTT %s:%s as %s",
             len(config["instruments"]), len(config.get("notifications", {})), settings.signalk_url, settings.mqtt_host,
             settings.mqtt_port, settings.mqtt_username)
    Bridge(config, publisher, connect).run(stop)
    LOG.info("stopped")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
