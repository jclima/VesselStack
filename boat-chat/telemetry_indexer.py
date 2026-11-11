#!/usr/bin/env python3
"""Refresh Boat Chat materialized telemetry summaries."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import re
import time
import datetime as dt
from typing import Any

import app
import memory_index
import telemetry_cache
import profile_runtime


HEALTH_ENTITY_IDS = [
    "sensor.boat_health_summary",
    "binary_sensor.boat_ok",
    "sensor.boat_watch_summary",
    "binary_sensor.shore_power_connected",
    "input_boolean.underway_mode",
    "input_select.boat_watch_mode",
    "sensor.distance_from_dock",
    "input_number.dock_radius",
    "sensor.signalk_api_up",
    "sensor.engine_alarm_status",
    "sensor.weather_risk_level",
    "sensor.audit_health_summary",
]

LOCK_DIR = telemetry_cache.memory_index.db_path().parent


def _lock_path(mode: str):
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    return LOCK_DIR / f"telemetry-{mode}.lock"


class ModeLock:
    def __init__(self, mode: str):
        self.path = _lock_path(mode)
        self.handle = None

    def __enter__(self):
        self.handle = self.path.open("a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            raise RuntimeError("already running")
        return self

    def __exit__(self, *_args):
        if self.handle:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _error_payload(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("error", "errors", "signalk_error"):
            if payload.get(key):
                return key
        current = payload.get("current_telemetry")
        if isinstance(current, dict) and current.get("signalk_error"):
            return "signalk_error"
        availability = payload.get("source_availability")
        if isinstance(availability, dict) and availability.get("home_assistant") == "error":
            return "home_assistant_error"
    return None


def _safe_result(ok: bool, count: int, failed: int, error_class: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": ok, "count": count, "failed": failed}
    if error_class:
        result["error_class"] = error_class
    return result


def refresh_item(category: str, key: str, func, results: list[dict[str, Any]]) -> None:
    started = time.monotonic()
    try:
        payload = func()
        error_key = _error_payload(payload)
        if error_key:
            raise ValueError(error_key)
        results.append(
            {
                "category": category,
                "key": key,
                "ok": True,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "summary": summarize_payload(payload),
            }
        )
    except Exception as exc:
        results.append(
            {
                "category": category,
                "key": key,
                "ok": False,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "error": str(exc),
                "error_class": type(exc).__name__,
            }
        )


def summarize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in [
        "action",
        "chunks",
        "direction",
        "label",
        "current_state",
        "running_minutes",
        "source_bucket",
        "start_local",
        "stop_local",
        "first_running_sample_local",
        "last_running_sample_local",
        "first_sample_local",
        "last_sample_local",
        "samples",
    ]:
        if key in payload:
            summary[key] = payload[key]
    if isinstance(payload.get("selected"), dict):
        summary["selected_time_local"] = payload["selected"].get("time_local")
    if isinstance(payload.get("totals"), dict):
        summary["total_gallons"] = payload["totals"].get("total_gallons")
        summary["fuel_gallons"] = payload["totals"].get("fuel_gallons")
        summary["distance_nm"] = payload["totals"].get("distance_nm")
    if isinstance(payload.get("economy"), dict):
        summary["nm_per_gallon"] = payload["economy"].get("nm_per_gallon")
    if isinstance(payload.get("voltage"), dict):
        summary["voltage_min"] = payload["voltage"].get("min")
        summary["voltage_avg"] = payload["voltage"].get("avg")
        summary["voltage_max"] = payload["voltage"].get("max")
        summary["voltage_latest"] = payload["voltage"].get("latest")
    if isinstance(payload.get("smartshunt_soc"), dict):
        summary["smartshunt_soc_latest"] = payload["smartshunt_soc"].get("latest")
    if isinstance(payload.get("solar_inference"), dict):
        solar = payload["solar_inference"]
        summary["solar_observations"] = solar.get("observation_count")
        summary["solar_qualifying_hours"] = solar.get("qualifying_coverage_hours")
        summary["inferred_net_charge_wh"] = solar.get("inferred_net_charge_wh")
    return summary


def fuel_window_messages() -> list[str]:
    return [
        "How much fuel did I use today?",
        "How much fuel did I use yesterday?",
        "How much fuel did I use this weekend?",
        "How much fuel did I use in the last 24 hours?",
        "How much fuel did I use in the last 7 days?",
        "How much fuel did I use this week?",
    ]


def refresh_all() -> dict[str, Any]:
    telemetry_cache.connect().close()
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    engine_keys: set[str] = set()

    # The memory index backs Boat Chat's readiness. Cheap hash comparison; rebuilds
    # only when it is missing, empty or the bundled knowledge files changed. A failure
    # is recorded here and never stops the telemetry refresh below.
    refresh_item("memory_index", "documents", memory_index.ensure, results)

    for direction in ["first", "last"]:
        key = app.engine_run_cache_key(direction, 365, app.ENGINE_RUNNING_RPM)
        engine_keys.add(key)
        refresh_item(
            "engine_run_history",
            key,
            lambda direction=direction: app.engine_run_history_summary(direction=direction, use_cache=False),
            results,
        )

    seen_windows: set[str] = set()
    for message in fuel_window_messages():
        window = app.resolve_fuel_usage_window(message)
        key = app.fuel_usage_cache_key(window, app.ENGINE_RUNNING_RPM)
        if key in seen_windows:
            continue
        seen_windows.add(key)
        refresh_item(
            "fuel_usage",
            key,
            lambda window=window: app.fuel_usage_summary(window, use_cache=False),
            results,
        )

    economy_keys: set[str] = set()
    for days in [365, 90, 30]:
        key = app.fuel_economy_cache_key(days, app.ENGINE_RUNNING_RPM)
        economy_keys.add(key)
        refresh_item(
            "fuel_economy",
            key,
            lambda days=days: app.fuel_economy_summary(days=days, use_cache=False),
            results,
        )
    for message in ["What is my average fuel economy this year?"]:
        window = app.resolve_fuel_usage_window(message)
        key = app.fuel_usage_cache_key(window, app.ENGINE_RUNNING_RPM)
        economy_keys.add(key)
        refresh_item(
            "fuel_economy",
            key,
            lambda window=window: app.fuel_economy_summary(window=window, use_cache=False),
            results,
        )

    shore_keys: set[str] = {"off:30d", "on:30d", "any:30d"}
    for message in ["When did shore power turn off?", "When did shore power turn on?", "What is shore power status?"]:
        started_item = time.monotonic()
        try:
            summary = app.shore_power_history_summary(message, use_cache=False)
            key = app.shore_power_cache_key(summary.get("target_state"), int(summary.get("lookback_days", 30)))
            results.append(
                {
                    "category": "shore_power_history",
                    "key": key,
                    "ok": True,
                    "elapsed_ms": int((time.monotonic() - started_item) * 1000),
                    "summary": summarize_payload(summary),
                }
            )
        except Exception as exc:
            results.append(
                {
                    "category": "shore_power_history",
                    "key": message,
                    "ok": False,
                    "elapsed_ms": int((time.monotonic() - started_item) * 1000),
                    "error": str(exc),
                }
            )

    battery_keys: set[str] = set()
    for days in [14, 7, 30]:
        key = app.battery_voltage_cache_key(days)
        battery_keys.add(key)
        refresh_item(
            "battery_voltage",
            key,
            lambda days=days: app.battery_voltage_summary(days=days, use_cache=False),
            results,
        )

    pruned = {
        "engine_run_history": telemetry_cache.prune_category("engine_run_history", engine_keys),
        "fuel_usage": telemetry_cache.prune_category("fuel_usage", seen_windows),
        "fuel_economy": telemetry_cache.prune_category("fuel_economy", economy_keys),
        "shore_power_history": telemetry_cache.prune_category("shore_power_history", shore_keys),
        "battery_voltage": telemetry_cache.prune_category("battery_voltage", battery_keys),
    }
    refresh_item(
        "latest_context",
        "boat_status",
        refresh_latest_context,
        results,
    )

    ok = all(item.get("ok") for item in results)
    output = {
        "ok": ok,
        "profile_fingerprint": profile_runtime.current()["fingerprint"],
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "pruned": pruned,
        "items": results,
    }
    telemetry_cache.put_summary("indexer", "last_run", output, source="telemetry_indexer")
    return output


def refresh_latest_context() -> dict[str, Any]:
    runtime = profile_runtime.current()
    with profile_runtime.scope(runtime=runtime):
        current_telemetry = app.current_telemetry_snapshot(
            "solar battery power current voltage state of charge engine RPM alternator position latitude longitude"
        )
        ha_states = app.get_ha_states(HEALTH_ENTITY_IDS)
        solar_inference = refresh_solar_inference(current_telemetry, ha_states, runtime=runtime)
    ha_errors = [item.get("error", "") for item in ha_states.values() if isinstance(item, dict) and item.get("error")]
    ha_configured = bool(app.load_auth_headers("ha").get("Authorization"))
    non_404_ha_error = any(not re.search(r"(?:HTTP Error )?404\b", str(error), re.IGNORECASE) for error in ha_errors)
    payload = {
        "boat_facts": app.load_boat_facts(),
        "current_telemetry": current_telemetry,
        "ha_states": ha_states,
        "solar_inference": solar_inference,
        "profile_fingerprint": runtime["fingerprint"],
        "profile_diagnostic": runtime.get("diagnostic"),
        "ais": app.current_ais_context(limit=12),
        "source_availability": {
            "signalk": "unknown" if current_telemetry.get("signalk_error") else "available",
            "home_assistant": "error" if ha_configured and non_404_ha_error else "unknown" if ha_errors else "available",
            "home_assistant_configured": ha_configured,
        },
    }
    telemetry_cache.put_summary("latest_context", "boat_status", payload, source="telemetry_indexer")
    return payload


def _unit_factor_to_ft(unit: Any) -> float | None:
    normalized = str(unit or "").strip().lower()
    return {
        "ft": 1.0, "feet": 1.0, "foot": 1.0,
        "m": 3.280839895, "meter": 3.280839895, "meters": 3.280839895,
        "km": 3280.839895, "kilometer": 3280.839895, "kilometers": 3280.839895,
    }.get(normalized)


def distance_ft_from_ha(state: Any) -> float | None:
    if not isinstance(state, dict):
        return None
    stamp_text = state.get("last_reported") or state.get("last_updated")
    if stamp_text:
        try:
            stamp = dt.datetime.fromisoformat(str(stamp_text).replace("Z", "+00:00")).timestamp()
            age = time.time() - stamp
            if age > 600 or age < -60:
                return None
        except (TypeError, ValueError, OverflowError):
            return None
    try:
        number = float(state.get("state"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    attrs = state.get("attributes")
    unit = attrs.get("unit_of_measurement") if isinstance(attrs, dict) else None
    factor = _unit_factor_to_ft(unit)
    return number * factor if factor is not None else None


def ha_state_is_fresh(state: Any, now: float | None = None) -> bool:
    if not isinstance(state, dict):
        return False
    stamp_text = state.get("last_reported") or state.get("last_updated")
    if not stamp_text:
        return False
    try:
        parsed = dt.datetime.fromisoformat(str(stamp_text).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return False
        age = (now or time.time()) - parsed.timestamp()
        return -60 <= age <= 600
    except (TypeError, ValueError, OverflowError):
        return False


def refresh_solar_inference(
    current_telemetry: dict[str, Any],
    ha_states: dict[str, Any],
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    values = current_telemetry.get("values", {}) if isinstance(current_telemetry, dict) else {}
    shore = ha_states.get("binary_sensor.shore_power_connected", {})
    underway = ha_states.get("input_boolean.underway_mode", {})
    distance_from_dock = ha_states.get("sensor.distance_from_dock", {})
    dock_radius = ha_states.get("input_number.dock_radius", {})
    now = time.time()
    runtime_obj = runtime or profile_runtime.current()
    equipment = runtime_obj["profile"]
    engines = equipment.get("engines", [])
    banks = equipment.get("battery_banks", [])
    primary = next((b for b in banks if b.get("id") == equipment.get("primary_battery_bank") and b.get("role") == "house"), None)
    if primary is None:
        # Persist an explicit profile barrier.  Without this marker, a later
        # valid profile could integrate across an unavailable/invalid profile.
        telemetry_cache.put_power_observation(
            shore_power_state="unknown",
            port_rpm=None,
            starboard_rpm=None,
            battery_power_w=None,
            battery_current_a=None,
            battery_voltage_v=None,
            battery_soc_pct=None,
            engine_rpms={e["id"]: None for e in engines},
            profile_fingerprint=runtime_obj["fingerprint"],
            source_timestamp_quality="profile_unavailable",
        )
        return {"available": False, "diagnostic": "Solar inference unavailable: no valid primary house battery bank."}
    source_paths = [f"{e['path']}.revolutions" for e in engines]
    source_paths.append(f"{primary['path']}.power")
    timestamps = current_telemetry.get("timestamps", {}) if isinstance(current_telemetry, dict) else {}
    source_times = []
    for path in source_paths:
        try:
            parsed = dt.datetime.fromisoformat(str(timestamps[path]).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("naive source timestamp")
            stamp = parsed.timestamp()
            if math.isfinite(stamp) and stamp <= now + 60 and now - stamp <= 600:
                source_times.append(stamp)
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
    source_quality = "verified" if len(source_times) == len(source_paths) else "legacy_unverified"
    source_timestamp = None
    try:
        parsed = dt.datetime.fromisoformat(str(timestamps[f"{primary['path']}.power"]).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("naive source timestamp")
        source_timestamp = int(parsed.timestamp())
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    if isinstance(distance_from_dock, dict):
        # HA helper state is valid until replaced; use its report/update time
        # for provenance, never last_changed (which is intentionally stable).
        distance_from_dock = dict(distance_from_dock)
        distance_from_dock["source_timestamp"] = distance_from_dock.get("last_reported") or distance_from_dock.get("last_updated")
    if not ha_state_is_fresh(shore, now):
        shore = {"state": "unknown"}
    if distance_from_dock and not ha_state_is_fresh(distance_from_dock, now):
        distance_from_dock = {"state": "unknown"}
    observation = telemetry_cache.put_power_observation(
        shore_power_state=shore.get("state") if isinstance(shore, dict) else None,
        underway_state=underway.get("state") if isinstance(underway, dict) else None,
        distance_from_dock_ft=distance_ft_from_ha(distance_from_dock),
        dock_radius_ft=distance_ft_from_ha(dock_radius),
        port_rpm=values.get("propulsion.port.revolutions"),
        starboard_rpm=values.get("propulsion.starboard.revolutions"),
        battery_power_w=values.get(f"{primary['path']}.power"),
        battery_current_a=values.get(f"{primary['path']}.current"),
        battery_voltage_v=values.get(f"{primary['path']}.voltage"),
        battery_soc_pct=values.get(f"{primary['path']}.capacity.stateOfCharge"),
        engine_rpms={e["id"]: values.get(f"{e['path']}.revolutions") for e in engines},
        profile_fingerprint=runtime_obj["fingerprint"],
        source_observed_at=source_timestamp,
        source_timestamp_quality=source_quality,
    )
    telemetry_cache.prune_power_observations(keep_days=400)
    summaries: dict[str, Any] = {}
    keep_keys: set[str] = set()
    for days in [1, 7, 30]:
        key = f"{days}d:profile-{runtime_obj['fingerprint'][:16]}"
        keep_keys.add(key)
        summary = telemetry_cache.power_tracking_summary(days, profile_fingerprint=runtime_obj["fingerprint"])
        summary["profile_fingerprint"] = runtime_obj["fingerprint"]
        telemetry_cache.put_summary("solar_inference", key, summary, source="telemetry_indexer")
        summaries[key] = summary
    telemetry_cache.prune_category("solar_inference", keep_keys)
    return {
        "latest_observation": observation,
        "rolling": summaries,
        "measurement_type": "inferred net solar-to-battery",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="history", choices=["run", "history", "sample", "status"])
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(telemetry_cache.status(), indent=2, sort_keys=True))
        return
    mode = "sample" if args.command == "sample" else "history"
    try:
        with ModeLock(mode):
            with profile_runtime.scope():
                payload = refresh_latest_context() if mode == "sample" else refresh_all()
            if mode == "sample":
                error = _error_payload(payload)
                result = _safe_result(error is None, 1, int(error is not None), error)
            else:
                failed = sum(1 for item in payload.get("items", []) if not item.get("ok"))
                result = _safe_result(failed == 0, len(payload.get("items", [])), failed, "refresh_failed" if failed else None)
            print(json.dumps(result if args.quiet else payload, separators=(",", ":") if args.quiet else None,
                             indent=None if args.quiet else 2, sort_keys=not args.quiet))
    except Exception as exc:
        print(json.dumps(_safe_result(False, 0, 1, type(exc).__name__), separators=(",", ":") if args.quiet else None,
                         indent=None if args.quiet else 2, sort_keys=not args.quiet))
        raise SystemExit(1)
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
