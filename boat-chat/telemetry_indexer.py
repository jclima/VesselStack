#!/usr/bin/env python3
"""Refresh Boat Chat materialized telemetry summaries."""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

import app
import telemetry_cache


HEALTH_ENTITY_IDS = [
    "sensor.boat_health_summary",
    "binary_sensor.boat_ok",
    "sensor.boat_watch_summary",
    "binary_sensor.shore_power_connected",
    "input_boolean.underway_mode",
    "sensor.distance_from_dock",
    "input_number.dock_radius",
    "sensor.signalk_api_up",
    "sensor.engine_alarm_status",
    "sensor.weather_risk_level",
    "sensor.audit_health_summary",
]


def refresh_item(category: str, key: str, func, results: list[dict[str, Any]]) -> None:
    started = time.monotonic()
    try:
        payload = func()
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
            }
        )


def summarize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in [
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
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "pruned": pruned,
        "items": results,
    }
    telemetry_cache.put_summary("indexer", "last_run", output, source="telemetry_indexer")
    return output


def refresh_latest_context() -> dict[str, Any]:
    current_telemetry = app.current_telemetry_snapshot(
        "solar battery power current voltage state of charge both engine RPM alternator position latitude longitude"
    )
    ha_states = app.get_ha_states(HEALTH_ENTITY_IDS)
    solar_inference = refresh_solar_inference(current_telemetry, ha_states)
    payload = {
        "boat_facts": app.load_boat_facts(),
        "current_telemetry": current_telemetry,
        "ha_states": ha_states,
        "solar_inference": solar_inference,
        "ais": app.current_ais_context(limit=12),
    }
    telemetry_cache.put_summary("latest_context", "boat_status", payload, source="telemetry_indexer")
    return payload


def refresh_solar_inference(
    current_telemetry: dict[str, Any],
    ha_states: dict[str, Any],
) -> dict[str, Any]:
    values = current_telemetry.get("values", {}) if isinstance(current_telemetry, dict) else {}
    shore = ha_states.get("binary_sensor.shore_power_connected", {})
    underway = ha_states.get("input_boolean.underway_mode", {})
    distance_from_dock = ha_states.get("sensor.distance_from_dock", {})
    dock_radius = ha_states.get("input_number.dock_radius", {})
    observation = telemetry_cache.put_power_observation(
        shore_power_state=shore.get("state") if isinstance(shore, dict) else None,
        underway_state=underway.get("state") if isinstance(underway, dict) else None,
        distance_from_dock_ft=distance_from_dock.get("state") if isinstance(distance_from_dock, dict) else None,
        dock_radius_ft=dock_radius.get("state") if isinstance(dock_radius, dict) else None,
        port_rpm=values.get("propulsion.port.revolutions"),
        starboard_rpm=values.get("propulsion.starboard.revolutions"),
        battery_power_w=values.get("electrical.batteries.shunt.power"),
        battery_current_a=values.get("electrical.batteries.shunt.current"),
        battery_voltage_v=values.get("electrical.batteries.shunt.voltage"),
        battery_soc_pct=values.get("electrical.batteries.shunt.capacity.stateOfCharge"),
    )
    telemetry_cache.prune_power_observations(keep_days=400)
    summaries: dict[str, Any] = {}
    keep_keys: set[str] = set()
    for days in [1, 7, 30]:
        key = f"{days}d"
        keep_keys.add(key)
        summary = telemetry_cache.power_tracking_summary(days)
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
    parser.add_argument("command", nargs="?", default="run", choices=["run", "status"])
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(telemetry_cache.status(), indent=2, sort_keys=True))
        return
    print(json.dumps(refresh_all(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
