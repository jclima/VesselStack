"""MQTT topics, payloads and the freshness state machine. Pure logic: no I/O, clock injected."""
from __future__ import annotations

import json
import math
from urllib.parse import urlsplit, urlunsplit

import conversions
import severity
from schema import DEFAULT_HEARTBEAT_S, DEFAULT_MIN_INTERVAL_MS

TOPIC_ROOT = "vesselstack/v1"
INSTRUMENT_TOPIC_PREFIX = f"{TOPIC_ROOT}/instruments/"
NOTIFICATION_TOPIC_PREFIX = f"{TOPIC_ROOT}/notifications/"
NOTIFICATION_PERIOD_MS = 1000
# After (re)connecting, SignalK re-sends active notifications within about one period; until
# then a notification nobody has reported on yet is "unknown", never a possibly false "ok".
EVENT_SETTLE_S = 3.0
BRIDGE_STATUS_TOPIC = f"{TOPIC_ROOT}/bridge/status"
BRIDGE_SIGNALK_TOPIC = f"{TOPIC_ROOT}/bridge/signalk"
DISPLAY_STATUS_TOPIC = f"{TOPIC_ROOT}/displays/{{username}}/status"


def instrument_topic(instrument_id: str) -> str:
    return INSTRUMENT_TOPIC_PREFIX + instrument_id


def notification_topic(notification_id: str) -> str:
    return NOTIFICATION_TOPIC_PREFIX + notification_id


def display_status_topic(username: str) -> str:
    return DISPLAY_STATUS_TOPIC.format(username=username)


def build_payload(value, unit, state, ts, age, sev=None) -> str:
    """JSON payload; ``sev`` is added (after ``st``) only when not None."""
    body = {"v": value, "u": unit, "st": state}
    if sev is not None:
        body["sev"] = sev
    body.update({"ts": ts, "age": age})
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


def numeric(value):
    """Return float for finite non-bool numbers, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


class _State:
    def __init__(self):
        self.value = None          # latest converted+rounded value
        self.ts = None             # SignalK timestamp of latest value
        self.received = None       # bridge receive time of latest value
        self.state = "missing"     # current freshness
        self.pub_value = None
        self.pub_state = None
        self.sev = None            # severity of the latest ok value (hysteresis memory); None unless ok
        self.pub_sev = None
        self.pub_time = None
        self.dirty = False


class Engine:
    """Tracks instruments and decides what to publish. Times are seconds (monotonic or epoch)."""

    def __init__(self, config: dict):
        self.heartbeat_s = config.get("heartbeat_s", DEFAULT_HEARTBEAT_S)
        self.instruments = config["instruments"]
        self._state = {iid: _State() for iid in self.instruments}
        self._by_path: dict[str, list[str]] = {}
        for iid, inst in self.instruments.items():
            self._by_path.setdefault(inst["path"], []).append(iid)

    def paths(self):
        return list(self._by_path)

    def _min_interval(self, iid) -> float:
        return self.instruments[iid].get("min_interval_ms", DEFAULT_MIN_INTERVAL_MS) / 1000.0

    def _emit(self, iid, now):
        inst, st = self.instruments[iid], self._state[iid]
        ok = st.state == "ok"
        age = None if st.received is None else round(max(0.0, now - st.received), 1)
        st.pub_value, st.pub_state, st.pub_time, st.dirty = st.value if ok else None, st.state, now, False
        sev = None
        if "thresholds" in inst:
            sev = st.sev if ok and st.sev is not None else "unknown"
            st.pub_sev = sev
        payload = build_payload(st.pub_value, inst["unit"], st.state, st.ts, age, sev)
        return (instrument_topic(iid), payload, True)

    def update(self, path: str, value, ts, now: float) -> list:
        """Handle a SignalK value; returns [(topic, payload, retain)]."""
        raw = numeric(value)
        if raw is None:
            return []
        out = []
        for iid in self._by_path.get(path, ()):
            inst, st = self.instruments[iid], self._state[iid]
            rounded = conversions.convert_and_round(inst["convert"], raw, inst["precision"])
            if rounded is None:
                continue
            st.value = rounded
            if "thresholds" in inst:  # on the rounded value, so the colour matches the digits
                st.sev = severity.numeric_sev(rounded, inst["thresholds"], st.sev if st.state == "ok" else None)
            st.ts = ts if isinstance(ts, str) else None
            st.received = now
            st.state = "ok"
            if st.pub_state != "ok":
                out.append(self._emit(iid, now))
            elif st.value != st.pub_value or st.sev != st.pub_sev:
                if now - st.pub_time >= self._min_interval(iid):
                    out.append(self._emit(iid, now))
                else:
                    st.dirty = True
        return out

    def tick(self, now: float) -> list:
        out = []
        for iid, inst in self.instruments.items():
            st = self._state[iid]
            if st.state == "ok" and now - st.received > inst["stale_after_s"]:
                st.state = "stale"
                st.sev = None
                out.append(self._emit(iid, now))
            elif st.pub_time is None or st.pub_state != st.state:
                out.append(self._emit(iid, now))
            elif st.dirty and now - st.pub_time >= self._min_interval(iid):
                out.append(self._emit(iid, now))
            elif now - st.pub_time >= self.heartbeat_s:
                out.append(self._emit(iid, now))
        return out


class _EventState:
    def __init__(self):
        self.sev = None        # severity from the latest message (None: nothing heard since connecting)
        self.ts = None
        self.received = None
        self.pub = None        # (state, sev) last published
        self.pub_time = None


class EventEngine:
    """Event-driven items (SignalK notifications): no numeric value, a severity per item.

    Freshness follows the SignalK connection rather than a per-item timeout, because an
    alarm that is not changing is silent: while SignalK is connected an item is "ok" (never
    reported means "not raised", shown per its ``absent`` setting), when SignalK is
    disconnected it is "stale" with severity "unknown" and everything heard is forgotten
    (a notification cleared during the outage is never re-announced; SignalK re-sends the
    active ones on reconnect). Before the first connection it is "missing". Every item is
    republished each ``heartbeat_s`` so a display can tell a live bridge from a dead one.
    Times are seconds from an injected clock.
    """

    def __init__(self, config: dict):
        self.heartbeat_s = config.get("heartbeat_s", DEFAULT_HEARTBEAT_S)
        self.notifications = config.get("notifications", {})
        self._state = {nid: _EventState() for nid in self.notifications}
        self._by_path: dict[str, list[str]] = {}
        for nid, note in self.notifications.items():
            self._by_path.setdefault(note["path"], []).append(nid)
        self.connected = False
        self._ever_connected = False
        self._connected_at = None

    def paths(self):
        return list(self._by_path)

    def _freshness(self) -> str:
        if self.connected:
            return "ok"
        return "stale" if self._ever_connected else "missing"

    def _sev(self, nid, now) -> str:
        if not self.connected:
            return "unknown"
        st = self._state[nid]
        if st.sev is not None:
            return st.sev
        if now - self._connected_at < EVENT_SETTLE_S:
            return "unknown"
        return severity.notification_sev(None, self.notifications[nid].get("absent", "ok"))

    def _emit(self, nid, now):
        st = self._state[nid]
        state, sev = self._freshness(), self._sev(nid, now)
        age = None if st.received is None else round(max(0.0, now - st.received), 1)
        st.pub, st.pub_time = (state, sev), now
        return (notification_topic(nid), build_payload(None, "", state, st.ts, age, sev), True)

    def source_state(self, source: str, connected: bool, now: float) -> list:
        """The SignalK connection went up or down; returns [(topic, payload, retain)]."""
        if source != "signalk" or connected == self.connected:
            return []
        self.connected = connected
        if connected:
            self._ever_connected, self._connected_at = True, now
        else:
            self._connected_at = None
            for st in self._state.values():
                st.sev = st.ts = st.received = None
        return [self._emit(nid, now) for nid in self.notifications]

    def set(self, nid: str, raw, ts, now: float) -> list:
        """A notification value (an object with a ``state``, or None when cleared)."""
        note = self.notifications.get(nid)
        if note is None:
            return []
        st = self._state[nid]
        st.sev = severity.notification_sev(raw, note.get("absent", "ok"))
        st.ts = ts if isinstance(ts, str) else None
        st.received = now
        if st.pub == (self._freshness(), self._sev(nid, now)):
            return []
        return [self._emit(nid, now)]

    def update(self, path: str, value, ts, now: float) -> list:
        out = []
        for nid in self._by_path.get(path, ()):
            out.extend(self.set(nid, value, ts, now))
        return out

    def tick(self, now: float) -> list:
        out = []
        for nid in self.notifications:
            st = self._state[nid]
            if st.pub_time is None or st.pub != (self._freshness(), self._sev(nid, now)) \
                    or now - st.pub_time >= self.heartbeat_s:
                out.append(self._emit(nid, now))
        return out


def subscribe_frame(config: dict) -> dict:
    periods: dict[str, int] = {}
    for inst in config["instruments"].values():
        interval = inst.get("min_interval_ms", DEFAULT_MIN_INTERVAL_MS)
        periods[inst["path"]] = min(interval, periods.get(inst["path"], interval))
    for note in config.get("notifications", {}).values():
        periods.setdefault(note["path"], NOTIFICATION_PERIOD_MS)
    return {"context": "vessels.self",
            "subscribe": [{"path": p, "period": ms, "minPeriod": ms} for p, ms in periods.items()]}


def signalk_stream_url(base: str) -> str:
    parts = urlsplit(base.strip())
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(parts.scheme)
    if scheme is None or not parts.netloc:
        raise ValueError(f"unsupported SignalK URL: {base!r}")
    path = parts.path.rstrip("/") + "/signalk/v1/stream"
    return urlunsplit((scheme, parts.netloc, path, "subscribe=none", ""))


def parse_delta(message, self_urn: str | None = None) -> list:
    """Extract [(path, value, timestamp)] from a SignalK delta for the own vessel."""
    if isinstance(message, (str, bytes)):
        try:
            message = json.loads(message)
        except ValueError:
            return []
    if not isinstance(message, dict):
        return []
    context = message.get("context")
    own = {None, "vessels.self"}
    if self_urn:
        own.update({self_urn, f"vessels.{self_urn}"})
    if context not in own:
        return []
    results = []
    updates = message.get("updates")
    for update in updates if isinstance(updates, list) else []:
        if not isinstance(update, dict):
            continue
        ts = update.get("timestamp")
        values = update.get("values")
        for item in values if isinstance(values, list) else []:
            if isinstance(item, dict) and isinstance(item.get("path"), str) and "value" in item:
                results.append((item["path"], item["value"], ts))
    return results
