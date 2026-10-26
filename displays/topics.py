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
BRIDGE_STATUS_TOPIC = f"{TOPIC_ROOT}/bridge/status"
BRIDGE_SIGNALK_TOPIC = f"{TOPIC_ROOT}/bridge/signalk"
DISPLAY_STATUS_TOPIC = f"{TOPIC_ROOT}/displays/{{username}}/status"


def instrument_topic(instrument_id: str) -> str:
    return INSTRUMENT_TOPIC_PREFIX + instrument_id


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


def subscribe_frame(config: dict) -> dict:
    periods: dict[str, int] = {}
    for inst in config["instruments"].values():
        interval = inst.get("min_interval_ms", DEFAULT_MIN_INTERVAL_MS)
        periods[inst["path"]] = min(interval, periods.get(inst["path"], interval))
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
