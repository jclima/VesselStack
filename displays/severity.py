"""Threshold severity with hysteresis. Pure logic: no I/O."""
from __future__ import annotations

import math

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
