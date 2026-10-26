"""Threshold severity with hysteresis. Pure logic: no I/O."""
from __future__ import annotations

import math
import re

LEVELS = ("ok", "warn", "alarm")


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def numeric_sev(value, thresholds: dict, prev=None) -> str:
    """Return "ok", "warn", "alarm" (or "unknown" for a missing/non-finite value).

    Bounds are strict: a value equal to a bound is still on the safer side. ``alarm_*`` is
    checked first, then ``warn_*``. ``prev`` is the previous result for this instrument;
    moving to a less severe level additionally requires the value to clear the bound by
    ``hysteresis`` (so alarm_below=10, hysteresis=0.5 leaves alarm at 10.5 and above).
    Going to a more severe level is always immediate. Pass ``prev=None`` (or "unknown") for no
    hysteresis, e.g. after the value was stale.
    """
    v = _num(value)
    if v is None:
        return "unknown"
    h = _num(thresholds.get("hysteresis", 0)) or 0.0

    def below(name, margin):
        bound = _num(thresholds.get(name))
        return bound is not None and v < round(bound + margin, 9)

    def above(name, margin):
        bound = _num(thresholds.get(name))
        return bound is not None and v > round(bound - margin, 9)

    if below("alarm_below", 0) or above("alarm_above", 0):
        return "alarm"
    if prev == "alarm" and (below("alarm_below", h) or above("alarm_above", h)):
        return "alarm"
    if below("warn_below", 0) or above("warn_above", 0):
        return "warn"
    if prev in ("warn", "alarm") and (below("warn_below", h) or above("warn_above", h)):
        return "warn"
    return "ok"


NOTIFICATION_STATES = {"normal": "ok", "nominal": "ok", "alert": "warn", "warn": "warn",
                       "alarm": "alarm", "emergency": "alarm"}


def notification_sev(value, absent: str = "ok") -> str:
    """Map a SignalK notification value to "ok", "warn", "alarm" or "unknown".

    ``value`` is the delta value of a ``notifications.*`` path: an object with a ``state``.
    ``None`` (a cleared notification, or one never raised) follows ``absent``: "ok" reads as
    healthy, "unknown" as not known. An unrecognised state, or anything that is not such an
    object, is "unknown" (never green). The ``message`` text is never read.
    """
    if value is None:
        return "ok" if absent == "ok" else "unknown"
    if not isinstance(value, dict):
        return "unknown"
    state = value.get("state")
    return NOTIFICATION_STATES.get(state, "unknown") if isinstance(state, str) else "unknown"


# Home Assistant ----------------------------------------------------------------------
# States that mean "HA has no value": shown as "--", severity unknown, never matched against
# the configured states (so a ``match`` of one of these words would be dead configuration).
HA_NO_VALUE = frozenset({"", "unknown", "unavailable", "none"})
_HA_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def ha_normalise(text) -> str:
    """Trimmed, case-folded state text, the form every comparison uses."""
    return text.strip().casefold()


def ha_state(state_str, kind: str, cfg: dict, prev=None) -> tuple:
    """Map a Home Assistant state string to ``(st, sev, k, v)``.

    ``st`` is "ok" for a usable value and "missing" otherwise; ``sev`` is "ok", "warn", "alarm" or
    "unknown"; ``k`` is the index of the matching ``states`` entry (-1: the default, None for a
    numeric item or a missing value); ``v`` is the rounded number of a numeric item. An
    "unknown" / "unavailable" / "none" / empty state, a missing state, or a numeric item whose
    state is not a finite number is "missing" with severity "unknown". Matching is case-insensitive
    after trimming and the first matching entry wins; a state no entry matches takes the
    configured ``default`` (for a binary item without one: warn). It never maps to "ok" by accident.
    ``prev`` is the previous severity of a numeric item, for the hysteresis.
    """
    if not isinstance(state_str, str) or ha_normalise(state_str) in HA_NO_VALUE:
        return ("missing", "unknown", None, None)
    if kind == "numeric":
        text = state_str.strip()
        if not _HA_NUMBER.fullmatch(text):
            return ("missing", "unknown", None, None)
        try:
            value = float(text)
        except (ValueError, OverflowError):
            return ("missing", "unknown", None, None)
        if not math.isfinite(value):
            return ("missing", "unknown", None, None)
        value = round(value, cfg.get("precision", 0))
        thresholds = cfg.get("thresholds")
        sev = numeric_sev(value, thresholds, prev) if thresholds else "ok"
        return ("ok", sev, None, value)
    state = ha_normalise(state_str)
    for index, entry in enumerate(cfg.get("states", ())):
        if "match" in entry:
            hit = state == ha_normalise(entry["match"])
        else:
            hit = state.startswith(ha_normalise(entry["prefix"]))
        if hit:
            return ("ok", entry["sev"], index, None)
    default = cfg.get("default") or {"sev": "warn"}
    return ("ok", default["sev"], -1, None)
