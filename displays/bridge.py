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
import queue
import random
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ha_client  # noqa: E402
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
HA_CONNECTED = "connected"
HA_DISCONNECTED = "disconnected"
HA_DISABLED = "disabled"
DEFAULT_HA_URL = "http://127.0.0.1:8123"
HA_TICK_INTERVAL_S = 0.25       # with Home Assistant items the loop looks at the queue four times a second
HA_DRAIN_LIMIT = 1000
HA_MAX_MESSAGE_BYTES = 32 * 1024 * 1024    # a get_states fallback returns every entity of the instance


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
    timezone: str = ""
    ha_url: str = ""
    ha_token: str = ""

    def secrets(self) -> list:
        return [value for value in (self.signalk_token, self.mqtt_password, self.ha_token) if value]


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
        timezone=(env.get("BOAT_TIMEZONE") or "").strip(),
        ha_url=(env.get("HOME_ASSISTANT_URL") or "").strip(),
        ha_token=(env.get("DISPLAYS_HA_TOKEN") or "").strip(),
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
    if not config["instruments"] and not config.get("notifications") and not config.get("ha"):
        raise ValueError(f"{path} defines no instruments; add instruments to publish")
    return config


def local_time_source(timezone_name: str = ""):
    """() -> aware local datetime: BOAT_TIMEZONE (IANA) when set and known, else the system zone.

    The system zone comes from the C library (TZ, then /etc/localtime), so it is whatever the
    Pi is set to; the bridge never sets or guesses a zone itself.
    """
    zone = None
    if timezone_name:
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(timezone_name)
        except Exception as exc:  # unknown zone, missing tzdata
            LOG.warning("BOAT_TIMEZONE %r is unusable (%s: %s); using the system time zone",
                        timezone_name, type(exc).__name__, exc)
    if zone is not None:
        return lambda: datetime.now(zone)
    return lambda: datetime.now().astimezone()


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
                 wait=None, rng=random.random, wall=None, local=None, ha=None):
        self.engine = topics.Engine(config)
        self.events = topics.EventEngine(config)
        self.has = topics.HaEngine(config)
        self._ha = ha                       # None: no Home Assistant client (no token or URL): items stay "--"
        self._ha_wanted = bool(config.get("ha"))
        self.ha_state = HA_DISCONNECTED if ha is not None else HA_DISABLED
        self.frame = json.dumps(topics.subscribe_frame(config), separators=(",", ":"))
        self.publisher = publisher
        self._connect = connect
        self.clock = clock
        self._wait = wait or (lambda seconds, stop: stop.wait(seconds))
        self._rng = rng
        self._wall = wall or (lambda: datetime.now(timezone.utc))
        self._local = local or local_time_source()
        self._night_schedule = config.get("night_schedule")
        self._clock_wanted = topics.clock_wanted(config)
        self._clock_sent = None     # (state, clock time) of the last bridge clock published
        self._summary_sent = None   # (content, clock time) of the last summary published
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
        if self._ha_wanted:
            self.publisher.publish(topics.BRIDGE_HA_TOPIC, self.ha_state, True, qos=1)
        if self._clock_wanted:
            self.publisher.publish(topics.BRIDGE_CLOCK_TOPIC, topics.clock_payload(
                topics.clock_state(self._local(), self._night_schedule), self._summary_ts()), True, qos=1)

    def _set_signalk(self, state):
        if state != self.signalk_state:
            self.signalk_state = state
            LOG.info("SignalK %s", state)
            self._send(self.events.source_state("signalk", state == SIGNALK_CONNECTED, self.clock()))
            self._publish_summary(self.clock())
        self.publisher.publish(topics.BRIDGE_SIGNALK_TOPIC, state, True, qos=1)

    def _set_ha(self, state):
        if state != self.ha_state:
            self.ha_state = state
            LOG.info("Home Assistant %s", state)
            self._send(self.has.source_state(state == HA_CONNECTED, self.clock()))
            self._publish_summary(self.clock())
            self.publisher.publish(topics.BRIDGE_HA_TOPIC, state, True, qos=1)

    def _drain_ha(self):
        """Apply what the Home Assistant thread queued; the main thread is the only one touching engines."""
        if self._ha is None:
            return
        for _ in range(HA_DRAIN_LIMIT):
            try:
                item = self._ha.queue.get_nowait()
            except queue.Empty:
                break
            try:
                if item[0] == "update":
                    self._send(self.has.set_state(item[1], item[2], self._summary_ts(), self.clock()))
                    self._publish_summary(self.clock())
                elif item[0] in (HA_CONNECTED, HA_DISCONNECTED, HA_DISABLED):
                    self._set_ha(item[0])
            except Exception as exc:  # one bad queue item must not cost the SignalK stream
                LOG.warning("dropped an unprocessable Home Assistant update: %s", type(exc).__name__)

    def _summary_ts(self):
        return self._wall().strftime("%Y-%m-%dT%H:%M:%SZ")

    def _publish_summary(self, now, items=None, force=False):
        """Publish the retained alarm summary on change and every heartbeat; none when nothing counts."""
        if items is None:
            items = self.engine.summary_items() + self.events.summary_items(now) + self.has.summary_items()
        payload = topics.alarm_summary(items, self._summary_ts())
        if payload is None:
            return
        content = json.loads(payload)
        content.pop("ts")
        sent = self._summary_sent
        if force or sent is None or sent[0] != content or now - sent[1] >= self.engine.heartbeat_s:
            self._summary_sent = (content, now)
            self.publisher.publish(topics.ALARM_SUMMARY_TOPIC, payload, True, qos=1)

    def _publish_clock(self, now):
        """Retained local wall clock and night flag: on a minute (or flag) change and every heartbeat."""
        if not self._clock_wanted:
            return
        state = topics.clock_state(self._local(), self._night_schedule)
        sent = self._clock_sent
        if sent is None or sent[0] != state or now - sent[1] >= self.engine.heartbeat_s:
            self._clock_sent = (state, now)
            self.publisher.publish(topics.BRIDGE_CLOCK_TOPIC, topics.clock_payload(state, self._summary_ts()),
                                   True, qos=1)

    def _tick(self):
        now = self.clock()
        if now >= self._next_tick:
            self._next_tick = now + TICK_INTERVAL_S
            self._publish_clock(now)
            self._send(self.engine.tick(now))
            self._send(self.events.tick(now))
            self._send(self.has.tick(now))
            self._publish_summary(now)

    # -- loop -------------------------------------------------------------
    def run(self, stop) -> None:
        self.publisher.start(self.on_mqtt_connected)
        if self._ha is not None:
            self._ha.start()
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
            self._drain_ha()
            self._tick()
            remaining = deadline - self.clock()
            if remaining <= 0:
                return stop.is_set()
            if self._wait(min(self._tick_interval(), remaining), stop):
                return True

    def _tick_interval(self):
        return HA_TICK_INTERVAL_S if self._ha is not None else TICK_INTERVAL_S

    def _pump(self, source, stop):
        while not stop.is_set():
            self._drain_ha()
            self._tick()
            timeout = max(0.05, min(self._tick_interval(), self._next_tick - self.clock()))
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
        self._publish_summary(self.clock())

    def shutdown(self):
        if self._ha is not None:
            try:
                self._ha.stop()
            except Exception as exc:  # stopping must always get as far as the offline message
                LOG.warning("stopping the Home Assistant client failed: %s", type(exc).__name__)
        if self.ha_state == HA_CONNECTED:
            self._set_ha(HA_DISCONNECTED)
        if self.signalk_state != SIGNALK_DISCONNECTED:
            self._set_signalk(SIGNALK_DISCONNECTED)
        # A clean stop must not leave a retained "ok" (or a stale "alarm"): publish every counted
        # item as unknown. A crash cannot do this; displays then rely on the summary going stale.
        now = self.clock()
        self._publish_summary(now, [(i, "unknown") for i, _ in
                                    self.engine.summary_items() + self.events.summary_items(now)
                                    + self.has.summary_items()], force=True)
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


def ha_connector(base_url):
    """() -> a websocket source for Home Assistant's /api/websocket (the token is sent as a message, not here)."""
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    url = topics.ha_websocket_url(base_url)

    def open_connection():
        connection = connect(url, proxy=None, compression=None, open_timeout=10, close_timeout=3,
                             ping_interval=20, ping_timeout=20, max_size=HA_MAX_MESSAGE_BYTES, legacy=True)
        return WebsocketSource(connection, ConnectionClosed)

    return open_connection


def make_ha_client(settings: Settings, config: dict):
    """The Home Assistant thread, or None (with one warning) when it cannot run.

    Home Assistant is an extra: whatever is wrong here, the SignalK side keeps working and the
    Home Assistant items just show "--".
    """
    if not config.get("ha"):
        return None
    if not settings.ha_token:
        LOG.warning("Home Assistant items are configured but DISPLAYS_HA_TOKEN is empty: they are disabled and "
                    "show --; create a long-lived access token (see the README) and set DISPLAYS_HA_TOKEN")
        return None
    try:
        connect = ha_connector(settings.ha_url or DEFAULT_HA_URL)
    except ValueError as exc:
        LOG.warning("Home Assistant is disabled: HOME_ASSISTANT_URL is invalid: %s", exc)
        return None
    except ImportError as exc:
        LOG.warning("Home Assistant is disabled: missing Python dependency (%s)", exc)
        return None
    runner = ha_client.HaRunner(settings.ha_token, topics.HaEngine(config).entity_ids(), queue.Queue(), connect)
    return ha_client.HaThread(runner)


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
    if config.get("ha"):
        LOG.info("%d Home Assistant item(s)", len(config["ha"]))
    ha = make_ha_client(settings, config)
    Bridge(config, publisher, connect, local=local_time_source(settings.timezone), ha=ha).run(stop)
    LOG.info("stopped")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
