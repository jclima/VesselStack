#!/usr/bin/env python3
"""Deterministic, I/O-free interpretation of boat questions."""
from __future__ import annotations
import calendar
import datetime as dt
import os
import re
from typing import Any
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo(os.environ.get("BOAT_TIMEZONE", "UTC"))
SIGNAL_TERMS = {
    "ais": ("ais", "nearby vessel", "nearest vessel", "closest vessel", "marine traffic", "other boat", "other vessel"),
    "battery": ("battery", "batteries", "smartshunt", "shunt", "state of charge", "soc", "house bank"),
    "bilge": ("bilge", "water aboard", "water in the boat"), "depth": ("depth", "deepest", "shallow", "shallowest"),
    "engine": ("engine", "engines", "motor", "motors", "propulsion"),
    "fuel_level": ("fuel left", "fuel level", "fuel remains", "fuel remaining", "fuel tank", "fuel tanks", "range"),
    "fuel_rate": ("fuel rate", "fuel burn", "burn rate", "consumption", "gph", "gallons per hour"),
    "generator": ("generator", "genset"), "humidity": ("humidity", "humid"),
    "position": ("position", "location", "where is the boat", "where are we", "latitude", "longitude", "gps"),
    "rpm": ("rpm", "revolutions"), "shore_power": ("shore power", "charger", "charging"),
    "solar": ("solar", "photovoltaic", "pv panel", "pv panels"), "speed": ("speed", "sog", "speed over ground"),
    "tank": ("fresh tank", "water tank", "waste tank", "holding tank", "tank level"),
    "temperature": ("temperature", "temperatures", "temp", "coolant", "how hot", "overheat", "overheated"),
    "weather": ("weather", "forecast", "wind", "storm"),
}

def _contains(text: str, term: str) -> bool:
    return bool(re.search(r"\b" + re.escape(term) + r"\b", text))

def detect_signals(message: str) -> set[str]:
    lower = " ".join(message.lower().split())
    return {signal for signal, terms in SIGNAL_TERMS.items() if any(_contains(lower, term) for term in terms)}

def resolve_time_window(message: str, now: dt.datetime | None = None) -> dict[str, Any] | None:
    current = now or dt.datetime.now(LOCAL_TZ)
    current = current.replace(tzinfo=LOCAL_TZ) if current.tzinfo is None else current.astimezone(LOCAL_TZ)
    lower = " ".join(message.lower().split()); start = None; stop = current; label = ""
    match = re.search(r"\b(?:last|past)\s+(\d+)\s*(hours?|hrs?|days?|weeks?|months?|years?)\b", lower)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        days = amount * (7 if unit.startswith("week") else 30 if unit.startswith("month") else 365 if unit.startswith("year") else 1)
        start = current - (dt.timedelta(hours=amount) if unit.startswith(("hour", "hr")) else dt.timedelta(days=days)); label = match.group(0)
    elif "last night" in lower or "overnight" in lower:
        start = dt.datetime.combine(current.date()-dt.timedelta(days=1), dt.time(18), tzinfo=LOCAL_TZ); stop = dt.datetime.combine(current.date(), dt.time(6), tzinfo=LOCAL_TZ); label = "last night"
    elif "this morning" in lower: start = dt.datetime.combine(current.date(), dt.time.min, tzinfo=LOCAL_TZ); label = "this morning"
    elif "yesterday" in lower:
        start = dt.datetime.combine(current.date()-dt.timedelta(days=1), dt.time.min, tzinfo=LOCAL_TZ); stop = start+dt.timedelta(days=1); label = "yesterday"
    elif "today" in lower: start = dt.datetime.combine(current.date(), dt.time.min, tzinfo=LOCAL_TZ); label = "today"
    elif "this week" in lower: start = dt.datetime.combine(current.date()-dt.timedelta(days=current.weekday()), dt.time.min, tzinfo=LOCAL_TZ); label = "this week"
    elif "last week" in lower:
        week=current.date()-dt.timedelta(days=current.weekday()); start=dt.datetime.combine(week-dt.timedelta(days=7),dt.time.min,tzinfo=LOCAL_TZ); stop=dt.datetime.combine(week,dt.time.min,tzinfo=LOCAL_TZ); label="last week"
    elif "this month" in lower: start=dt.datetime(current.year,current.month,1,tzinfo=LOCAL_TZ); label="this month"
    elif "last month" in lower:
        stop=dt.datetime(current.year,current.month,1,tzinfo=LOCAL_TZ); year,month=((stop.year-1,12) if stop.month==1 else (stop.year,stop.month-1)); start=dt.datetime(year,month,1,tzinfo=LOCAL_TZ); label="last month"
    elif any(x in lower for x in ("this year","year to date","ytd")): start=dt.datetime(current.year,1,1,tzinfo=LOCAL_TZ); label="this year"
    elif "last year" in lower: start=dt.datetime(current.year-1,1,1,tzinfo=LOCAL_TZ); stop=dt.datetime(current.year,1,1,tzinfo=LOCAL_TZ); label="last year"
    elif match := re.search(r"\bsince\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", lower):
        weekday=list(calendar.day_name).index(match.group(1).title()); start=dt.datetime.combine(current.date()-dt.timedelta(days=(current.weekday()-weekday)%7),dt.time.min,tzinfo=LOCAL_TZ); label=match.group(0)
    if start is None: return None
    return {"label":label,"start":start,"stop":stop,"start_utc":start.astimezone(dt.timezone.utc).isoformat().replace("+00:00","Z"),"stop_utc":stop.astimezone(dt.timezone.utc).isoformat().replace("+00:00","Z"),"start_local":start.strftime("%Y-%m-%d %H:%M %Z"),"stop_local":stop.strftime("%Y-%m-%d %H:%M %Z")}

def build_query_plan(message: str, now: dt.datetime | None = None) -> dict[str, Any]:
    lower=" ".join(message.lower().split()); signals=detect_signals(lower); window=resolve_time_window(lower,now); operation="current"
    if any(x in lower for x in ("maximum","highest","hottest","how hot","deepest")): operation="maximum"
    elif any(x in lower for x in ("minimum","lowest","coldest","shallowest")): operation="minimum"
    elif any(x in lower for x in ("average","mean")): operation="average"
    elif any(x in lower for x in ("how long","runtime","hours did","hours has")): operation="duration"
    elif any(x in lower for x in ("how many times","count","did the bilge")): operation="event_count"
    elif any(x in lower for x in ("when did","when was","when were","last time","first time")): operation="transition"
    comparison = "port_starboard" if any(x in lower for x in ("compare","versus"," vs ","difference","what about starboard")) else None
    historical=bool(window) or operation!="current" or any(x in lower for x in ("history","trend","dip","drop","during","while underway")); complex_query=bool(comparison) or any(x in lower for x in ("why","cause","caused","at that time","before","after","while")); filters={}
    rpm=re.search(r"\b(?:at|between)?\s*(\d{3,4})(?:\s*(?:-|to|and)\s*(\d{3,4}))?\s*rpm\b",lower)
    if rpm: filters={"rpm_min":int(rpm.group(1)),"rpm_max":int(rpm.group(2) or rpm.group(1))}; historical=True
    if "while underway" in lower: filters["underway"]=True
    return {"signals":sorted(signals),"operation":operation,"historical":historical,"complex":complex_query,"comparison":comparison,"engine_sides":[s for s in ("port","starboard") if _contains(lower,s)],"filters":filters,"window":window,"confidence":"high" if signals else "low"}
