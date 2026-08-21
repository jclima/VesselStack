#!/usr/bin/env python3
"""Materialized telemetry summaries for Boat Chat.

This is a compact cache of derived facts, not a raw telemetry mirror. InfluxDB
and Home Assistant remain the sources of truth.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import memory_index


DEFAULT_POWER_DB_PATH = memory_index.ROOT / "data" / "power_tracking.sqlite"
LOCAL_TZ = ZoneInfo("America/Los_Angeles")
POWER_MAX_GAP_SECONDS = 15 * 60


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or memory_index.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma synchronous=normal")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists telemetry_summaries (
            category text not null,
            key text not null,
            payload text not null,
            source text not null,
            updated_at integer not null,
            primary key (category, key)
        );
        create index if not exists telemetry_summaries_category_updated
            on telemetry_summaries(category, updated_at);
        """
    )
    conn.commit()


def power_db_path() -> Path:
    return Path(os.environ.get("BOAT_CHAT_POWER_DB", str(DEFAULT_POWER_DB_PATH)))


def connect_power(path: Path | None = None) -> sqlite3.Connection:
    target = path or power_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma synchronous=normal")
    conn.executescript(
        """
        create table if not exists power_observations (
            observed_at integer primary key,
            shore_power_state text,
            underway_state text,
            distance_from_dock_ft real,
            dock_radius_ft real,
            port_rpm real,
            starboard_rpm real,
            battery_power_w real,
            battery_current_a real,
            battery_voltage_v real,
            battery_soc_pct real
        );
        create index if not exists power_observations_time
            on power_observations(observed_at);
        """
    )
    existing_columns = {
        str(row["name"])
        for row in conn.execute("pragma table_info(power_observations)").fetchall()
    }
    for column, declaration in {
        "underway_state": "text",
        "distance_from_dock_ft": "real",
        "dock_radius_ft": "real",
    }.items():
        if column not in existing_columns:
            conn.execute(f"alter table power_observations add column {column} {declaration}")
    conn.commit()
    return conn


def _optional_float(value: Any) -> float | None:
    try:
        if value in ("", None, "unknown", "unavailable"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def put_power_observation(
    *,
    shore_power_state: str | None,
    underway_state: str | None = None,
    distance_from_dock_ft: Any = None,
    dock_radius_ft: Any = None,
    port_rpm: Any,
    starboard_rpm: Any,
    battery_power_w: Any,
    battery_current_a: Any,
    battery_voltage_v: Any,
    battery_soc_pct: Any,
    observed_at: int | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    timestamp = int(observed_at or time.time())
    normalized_state = str(shore_power_state or "unknown").strip().lower()
    if normalized_state not in {"on", "off"}:
        normalized_state = "unknown"
    normalized_underway = str(underway_state or "unknown").strip().lower()
    if normalized_underway not in {"on", "off"}:
        normalized_underway = "unknown"
    observation = {
        "observed_at": timestamp,
        "shore_power_state": normalized_state,
        "underway_state": normalized_underway,
        "distance_from_dock_ft": _optional_float(distance_from_dock_ft),
        "dock_radius_ft": _optional_float(dock_radius_ft),
        "port_rpm": _optional_float(port_rpm),
        "starboard_rpm": _optional_float(starboard_rpm),
        "battery_power_w": _optional_float(battery_power_w),
        "battery_current_a": _optional_float(battery_current_a),
        "battery_voltage_v": _optional_float(battery_voltage_v),
        "battery_soc_pct": _optional_float(battery_soc_pct),
    }
    conn = connect_power(path)
    with conn:
        conn.execute(
            """
            insert into power_observations (
                observed_at, shore_power_state, underway_state, distance_from_dock_ft, dock_radius_ft,
                port_rpm, starboard_rpm,
                battery_power_w, battery_current_a, battery_voltage_v, battery_soc_pct
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(observed_at) do update set
                shore_power_state = excluded.shore_power_state,
                underway_state = excluded.underway_state,
                distance_from_dock_ft = excluded.distance_from_dock_ft,
                dock_radius_ft = excluded.dock_radius_ft,
                port_rpm = excluded.port_rpm,
                starboard_rpm = excluded.starboard_rpm,
                battery_power_w = excluded.battery_power_w,
                battery_current_a = excluded.battery_current_a,
                battery_voltage_v = excluded.battery_voltage_v,
                battery_soc_pct = excluded.battery_soc_pct
            """,
            tuple(observation[key] for key in observation),
        )
    conn.close()
    observation["observed_at_local"] = dt.datetime.fromtimestamp(timestamp, LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")
    observation["classification"] = classify_power_observation(observation)
    return observation


def classify_power_observation(observation: dict[str, Any], engine_rpm: float = 200.0) -> str:
    distance = _optional_float(observation.get("distance_from_dock_ft"))
    dock_radius = _optional_float(observation.get("dock_radius_ft"))
    beyond_dock_radius = (
        distance is not None
        and dock_radius is not None
        and dock_radius > 0
        and distance > dock_radius
    )
    underway = observation.get("underway_state") == "on"
    charge_proxy_off = observation.get("shore_power_state") == "off"
    if not (beyond_dock_radius or underway or charge_proxy_off):
        if observation.get("shore_power_state") == "on":
            return "charging_proxy_on_near_dock"
        return "shore_connection_unknown"
    port_rpm = _optional_float(observation.get("port_rpm"))
    starboard_rpm = _optional_float(observation.get("starboard_rpm"))
    if port_rpm is None or starboard_rpm is None:
        return "engine_state_unknown"
    if port_rpm >= engine_rpm or starboard_rpm >= engine_rpm:
        return "engine_running"
    if _optional_float(observation.get("battery_power_w")) is None:
        return "battery_power_unknown"
    return "off_shore_engines_off"


def prune_power_observations(
    *,
    keep_days: int = 400,
    now: int | None = None,
    path: Path | None = None,
) -> int:
    cutoff = int(now or time.time()) - max(1, int(keep_days)) * 86400
    conn = connect_power(path)
    with conn:
        cursor = conn.execute("delete from power_observations where observed_at < ?", (cutoff,))
    conn.close()
    return int(cursor.rowcount)


def power_tracking_summary(
    days: int = 30,
    *,
    now: int | None = None,
    path: Path | None = None,
    max_gap_seconds: int = POWER_MAX_GAP_SECONDS,
) -> dict[str, Any]:
    end = int(now or time.time())
    start = end - max(1, int(days)) * 86400
    conn = connect_power(path)
    rows = [
        dict(row)
        for row in conn.execute(
            """
            select observed_at, shore_power_state, underway_state, distance_from_dock_ft, dock_radius_ft,
                   port_rpm, starboard_rpm,
                   battery_power_w, battery_current_a, battery_voltage_v, battery_soc_pct
            from power_observations
            where observed_at >= ? and observed_at <= ?
            order by observed_at
            """,
            (start - max_gap_seconds, end),
        ).fetchall()
    ]
    total_count = int(conn.execute("select count(*) from power_observations").fetchone()[0])
    first_row = conn.execute("select min(observed_at) from power_observations").fetchone()
    conn.close()

    visible_rows = [row for row in rows if int(row["observed_at"]) >= start]
    qualifying = [row for row in visible_rows if classify_power_observation(row) == "off_shore_engines_off"]
    positive_wh = 0.0
    negative_wh = 0.0
    net_wh = 0.0
    covered_seconds = 0.0
    valid_intervals = 0
    skipped_gaps = 0
    for previous, current in zip(rows, rows[1:]):
        previous_time = int(previous["observed_at"])
        current_time = int(current["observed_at"])
        interval_start = max(previous_time, start)
        interval_end = min(current_time, end)
        seconds = interval_end - interval_start
        if seconds <= 0:
            continue
        if current_time - previous_time > max_gap_seconds:
            skipped_gaps += 1
            continue
        if (
            classify_power_observation(previous) != "off_shore_engines_off"
            or classify_power_observation(current) != "off_shore_engines_off"
        ):
            continue
        previous_power = float(previous["battery_power_w"])
        current_power = float(current["battery_power_w"])
        hours = seconds / 3600.0
        positive_wh += (max(previous_power, 0.0) + max(current_power, 0.0)) * 0.5 * hours
        negative_wh += (max(-previous_power, 0.0) + max(-current_power, 0.0)) * 0.5 * hours
        net_wh += (previous_power + current_power) * 0.5 * hours
        covered_seconds += seconds
        valid_intervals += 1

    first_timestamp = int(first_row[0]) if first_row and first_row[0] is not None else None
    latest = visible_rows[-1] if visible_rows else (rows[-1] if rows else None)
    if latest:
        latest = dict(latest)
        latest["classification"] = classify_power_observation(latest)
        latest["observed_at_local"] = dt.datetime.fromtimestamp(int(latest["observed_at"]), LOCAL_TZ).strftime(
            "%Y-%m-%d %H:%M %Z"
        )
    covered_hours = covered_seconds / 3600.0
    confidence = "none" if valid_intervals == 0 else "low" if covered_hours < 6 else "medium"
    return {
        "label": f"last {max(1, int(days))} days",
        "lookback_days": max(1, int(days)),
        "tracking_started_local": (
            dt.datetime.fromtimestamp(first_timestamp, LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")
            if first_timestamp is not None
            else None
        ),
        "observation_count": len(visible_rows),
        "total_observation_count": total_count,
        "qualifying_sample_count": len(qualifying),
        "valid_interval_count": valid_intervals,
        "skipped_gap_count": skipped_gaps,
        "qualifying_coverage_hours": round(covered_hours, 3),
        "inferred_net_charge_wh": round(positive_wh, 3),
        "observed_net_discharge_wh": round(negative_wh, 3),
        "net_battery_energy_wh": round(net_wh, 3),
        "peak_inferred_charge_w": (
            round(max(float(row["battery_power_w"]) for row in qualifying), 3) if qualifying else None
        ),
        "peak_observed_discharge_w": (
            round(max(max(-float(row["battery_power_w"]), 0.0) for row in qualifying), 3) if qualifying else None
        ),
        "latest_observation": latest,
        "confidence": confidence,
        "attribution": "inferred net solar or another uninstrumented charging source",
        "limitations": [
            "No dedicated solar-controller signal is present.",
            "The Home Assistant shore-power entity is a battery-charging proxy, not a physical AC-input sensor.",
            "Positive SmartShunt power is counted only when underway, beyond the configured dock radius, or the charging proxy is off, with both engines below 200 RPM.",
            "This is net battery charging after onboard loads, not gross panel production.",
            "Intervals longer than 15 minutes are excluded instead of interpolated.",
        ],
    }


def power_tracking_status(path: Path | None = None) -> dict[str, Any]:
    target = path or power_db_path()
    if not target.exists():
        return {"path": str(target), "exists": False, "observations": 0}
    conn = connect_power(target)
    row = conn.execute(
        "select count(*) as count, min(observed_at) as first_at, max(observed_at) as last_at from power_observations"
    ).fetchone()
    conn.close()
    return {
        "path": str(target),
        "exists": True,
        "observations": int(row["count"] or 0),
        "first_observation_local": (
            dt.datetime.fromtimestamp(int(row["first_at"]), LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")
            if row["first_at"] is not None
            else None
        ),
        "last_observation_local": (
            dt.datetime.fromtimestamp(int(row["last_at"]), LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")
            if row["last_at"] is not None
            else None
        ),
    }


def put_summary(
    category: str,
    key: str,
    payload: dict[str, Any],
    *,
    source: str = "boat-chat",
    updated_at: int | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    now = int(updated_at or time.time())
    stored = dict(payload)
    stored.setdefault("cache", {})
    stored["cache"].update({"category": category, "key": key, "updated_at": now, "source": source})
    conn = connect(path)
    with conn:
        conn.execute(
            """
            insert into telemetry_summaries (category, key, payload, source, updated_at)
            values (?, ?, ?, ?, ?)
            on conflict(category, key) do update set
                payload = excluded.payload,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (category, key, json.dumps(stored, sort_keys=True), source, now),
        )
    conn.close()
    return stored


def get_summary(
    category: str,
    key: str,
    *,
    max_age_seconds: int | None = None,
    path: Path | None = None,
) -> dict[str, Any] | None:
    target = path or memory_index.db_path()
    if not target.exists():
        return None
    conn = connect(target)
    row = conn.execute(
        """
        select payload, updated_at
        from telemetry_summaries
        where category = ? and key = ?
        """,
        (category, key),
    ).fetchone()
    conn.close()
    if not row:
        return None
    updated_at = int(row["updated_at"])
    if max_age_seconds is not None and int(time.time()) - updated_at > max_age_seconds:
        return None
    try:
        payload = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    payload.setdefault("cache", {})
    payload["cache"].update(
        {
            "hit": True,
            "category": category,
            "key": key,
            "updated_at": updated_at,
            "age_seconds": max(0, int(time.time()) - updated_at),
        }
    )
    return payload


def list_summaries(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or memory_index.db_path()
    if not target.exists():
        return []
    conn = connect(target)
    rows = conn.execute(
        """
        select category, key, source, updated_at
        from telemetry_summaries
        order by category, key
        """
    ).fetchall()
    conn.close()
    now = int(time.time())
    return [
        {
            "category": row["category"],
            "key": row["key"],
            "source": row["source"],
            "updated_at": row["updated_at"],
            "age_seconds": max(0, now - int(row["updated_at"])),
        }
        for row in rows
    ]


def prune_category(category: str, keep_keys: set[str], path: Path | None = None) -> int:
    conn = connect(path)
    if not keep_keys:
        with conn:
            cursor = conn.execute("delete from telemetry_summaries where category = ?", (category,))
        conn.close()
        return int(cursor.rowcount)

    placeholders = ",".join("?" for _ in keep_keys)
    params = [category, *sorted(keep_keys)]
    with conn:
        cursor = conn.execute(
            f"delete from telemetry_summaries where category = ? and key not in ({placeholders})",
            params,
        )
    conn.close()
    return int(cursor.rowcount)


def status(path: Path | None = None) -> dict[str, Any]:
    target = path or memory_index.db_path()
    summaries = list_summaries(target)
    return {
        "path": str(target),
        "exists": target.exists(),
        "summaries": len(summaries),
        "categories": sorted({item["category"] for item in summaries}),
        "items": summaries,
        "power_tracking": power_tracking_status(),
    }
