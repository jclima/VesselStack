"""One definition of how old a reading may be before it stops being current.

Pure and standard-library only. The caller passes ``now`` in; nothing here
reads the clock, so every verdict is reproducible.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Iterable, NamedTuple

FRESH_SECONDS = 600
FUTURE_TOLERANCE_SECONDS = 60
EARLIEST_PLAUSIBLE = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
CLOCK_FLOOR = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
GPS_MISMATCH_SECONDS = 300
LOCAL_FORMAT = "%a %Y-%m-%d %H:%M %Z"


class Age(NamedTuple):
    state: str  # fresh | stale | future | missing | invalid
    age_seconds: float | None
    observed_utc: str | None
    observed_local: str | None


def parse_timestamp(stamp) -> dt.datetime | None:
    """Return an aware UTC datetime, or None when missing, naive or unparseable."""
    if stamp is None or isinstance(stamp, bool):
        return None
    text = str(stamp).strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        parsed = parsed.astimezone(dt.timezone.utc)
        parsed.timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return parsed


def classify(stamp, now: dt.datetime, tz: dt.tzinfo | None = None) -> Age:
    if stamp is None or (isinstance(stamp, str) and not stamp.strip()):
        return Age("missing", None, None, None)
    parsed = parse_timestamp(stamp)
    if parsed is None or parsed < EARLIEST_PLAUSIBLE:
        return Age("invalid", None, None, None)
    age = (now - parsed).total_seconds()
    if not math.isfinite(age):
        return Age("invalid", None, None, None)
    if age > FRESH_SECONDS:
        state = "stale"
    elif age < -FUTURE_TOLERANCE_SECONDS:
        state = "future"
    else:
        state = "fresh"
    local = parsed.astimezone(tz or dt.timezone.utc).strftime(LOCAL_FORMAT)
    return Age(state, age, parsed.isoformat().replace("+00:00", "Z"), local)


def describe_age(seconds: float) -> str:
    """Short age text: 'under 1 min', '4 min', '2 h 10 min', '3 h', '1 day', '2 days'."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return "under 1 min"
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    if seconds < 86400:
        hours, minutes = int(seconds // 3600), int(seconds % 3600 // 60)
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days = int(seconds // 86400)
    return f"{days} day" if days == 1 else f"{days} days"


def clock_status(now: dt.datetime, timestamps: Iterable[str], gps_iso: str | None = None) -> str:
    """'ok' only when the boat clock agrees with GPS; 'suspect' when it is clearly wrong."""
    gps = parse_timestamp(gps_iso)
    if now < CLOCK_FLOOR:
        return "suspect"
    if gps is not None and abs((now - gps).total_seconds()) > GPS_MISMATCH_SECONDS:
        return "suspect"
    parseable = [s for s in timestamps if parse_timestamp(s) is not None]
    if len(parseable) >= 3:
        future = sum(classify(s, now).state == "future" for s in parseable)
        if future * 2 > len(parseable):
            return "suspect"
    return "ok" if gps is not None else "unverified"
