"""Presentation-only unit conversion for Boat Chat.

Telemetry and cache payloads deliberately remain in their existing canonical
forms.  This module is used at the boundary where values are shown to a
person or sent to a model.
"""
from __future__ import annotations

import copy
import os
from typing import Any, Callable

US_GALLONS_PER_LITER = 0.2641720524
PA_PER_PSI = 6894.757293168
PA_PER_INHG = 3386.389
M_PER_FT = 0.3048


def normalize_preference(value: str | None) -> str:
    return "metric" if str(value or "").strip().lower() in {"metric", "si", "metre", "meters"} else "us_customary"


def preference(setting_getter: Callable[[str, str], str] | None = None) -> str:
    raw = setting_getter("BOAT_UNITS", "") if setting_getter else os.environ.get("BOAT_UNITS", "")
    return normalize_preference(raw)


def unit_for_measurement(measurement: str, pref: str = "us_customary", source_unit: str | None = None) -> str:
    p = normalize_preference(pref)
    m = str(measurement).lower()
    # An explicit empty unit means that the source did not tell us its unit.
    # Do not infer or relabel that value.  This is important for HA sensors,
    # where an entity may be numeric but deliberately have no unit metadata.
    if source_unit == "":
        return ""
    if source_unit is not None and source_unit.lower() not in {"f", "°f", "c", "°c", "ft", "m", "psi", "kpa", "pa", "hpa", "inhg", "gal", "us gal", "gal/h", "gph", "l", "l/h"}:
        return source_unit
    if source_unit is not None and p == "us_customary":
        return source_unit
    if source_unit is not None and p == "metric":
        source_key = source_unit.lower().replace("°", "")
        if source_key in {"f", "c"}:
            return "°C"
        if source_key in {"psi", "kpa", "pa", "inhg", "hpa"}:
            return "hPa" if ("environment" in m or "barometer" in m) else "kPa"
        if source_key in {"ft", "m"}:
            return "m"
        if source_key in {"gal/h", "gph", "l/h"}:
            return "L/h"
        if source_key in {"gal", "us gal", "l"}:
            return "L"
    if "temperature" in m or m.endswith(".temp"):
        return "°C" if p == "metric" else "F"
    if "pressure" in m or "oilpressure" in m or "boostpressure" in m:
        if "environment" in m or "barometer" in m:
            return "hPa" if p == "metric" else "inHg"
        return "kPa" if p == "metric" else "psi"
    if "depth" in m or "distance" in m:
        return "m" if p == "metric" else "ft"
    if m.startswith("tanks.") or "currentlevel" in m or "stateofcharge" in m or "state_of_charge" in m:
        return "%"
    if m.endswith(".fuel.rate") or "fuel_rate" in m:
        return "L/h" if p == "metric" else "gal/h"
    if "nm_per_gallon" in m or "gallons_per_nm" in m:
        return "NM/L" if p == "metric" else "NM/US gal"
    if "liters_per_nm" in m:
        return "L/NM"
    if "gallon" in m or m.endswith("_fuel"):
        return "L" if p == "metric" else "US gal"
    if "speed" in m:
        return "kn"  # knots and nautical miles are intentionally retained.
    if "heading" in m or "courseoverground" in m or "attitude" in m:
        return "deg"
    if m == "navigation.rateofturn":
        return "deg/min"
    if m.endswith(".revolutions"):
        return "RPM"
    if m.endswith(".power"):
        return "W"
    if "voltage" in m:
        return "V"
    if "current" in m:
        return "A"
    if "humidity" in m or "stateofcharge" in m or "state_of_charge" in m:
        return "%"
    return source_unit if source_unit is not None else ""


def convert(value: float, from_unit: str, to_unit: str) -> float:
    f, t = from_unit.lower().replace("°", ""), to_unit.lower().replace("°", "")
    if f == t or not f or not t:
        return value
    # Temperature is affine; deltas must use only the scale factor (handled by
    # convert_delta below).
    if f in {"f", "°f"} and t in {"c", "°c"}:
        return (value - 32.0) * 5.0 / 9.0
    if f in {"c", "°c"} and t in {"f", "°f"}:
        return value * 9.0 / 5.0 + 32.0
    factors = {
        ("pa", "kpa"): 1 / 1000,
        ("psi", "kpa"): PA_PER_PSI / 1000,
        ("pa", "psi"): 1 / PA_PER_PSI,
        ("psi", "pa"): PA_PER_PSI,
        ("kpa", "psi"): 1000 / PA_PER_PSI,
        ("psi", "hpa"): PA_PER_PSI / 100,
        ("hpa", "psi"): 100 / PA_PER_PSI,
        ("pa", "hpa"): 1 / 100,
        ("hpa", "kpa"): 0.1,
        ("kpa", "hpa"): 10,
        ("inhg", "hpa"): PA_PER_INHG / 100,
        ("hpa", "inhg"): 100 / PA_PER_INHG,
        ("inhg", "kpa"): PA_PER_INHG / 1000,
        ("kpa", "inhg"): 1000 / PA_PER_INHG,
        ("kpa", "pa"): 1000,
        ("hpa", "pa"): 100,
        ("ft", "m"): M_PER_FT,
        ("m", "ft"): 1 / M_PER_FT,
        ("gal/h", "l/h"): 1 / US_GALLONS_PER_LITER,
        ("l/h", "gal/h"): US_GALLONS_PER_LITER,
        ("us gal", "l"): 1 / US_GALLONS_PER_LITER,
        ("gal", "l"): 1 / US_GALLONS_PER_LITER,
        ("l", "us gal"): US_GALLONS_PER_LITER,
        ("l", "gal"): US_GALLONS_PER_LITER,
    }
    return value * factors.get((f, t), 1.0)


def convert_delta(value: float, from_unit: str, to_unit: str) -> float:
    f, t = from_unit.lower().replace("°", ""), to_unit.lower().replace("°", "")
    if {f, t} == {"f", "c"}:
        return value * (5.0 / 9.0 if f == "f" else 9.0 / 5.0)
    return convert(value, from_unit, to_unit)


def display_value(measurement: str, value: float, pref: str = "us_customary", source_unit: str | None = None) -> float:
    """Convert a value using its explicit source unit, or legacy SignalK units."""
    target = unit_for_measurement(measurement, pref, source_unit)
    lower = str(measurement).lower()
    if source_unit == "":
        return value
    if source_unit is not None:
        recognized = {"f", "°f", "c", "°c", "ft", "m", "psi", "kpa", "pa", "hpa", "inhg", "gal", "us gal", "gal/h", "gph", "l", "l/h"}
        if source_unit.lower() not in recognized:
            return value
        source = {"gph": "gal/h"}.get(source_unit.lower(), source_unit)
        return convert(value, source, target)
    # Legacy fallback applies only to known SignalK canonical paths.
    known = lower.startswith(("propulsion.", "environment.", "navigation.", "electrical.", "tanks."))
    if not known:
        return value
    if "temperature" in lower or lower.endswith(".temp"):
        return convert(value, "F", target)
    if "depth" in lower or "distance" in lower:
        return convert(value, "ft", target)
    if "oilpressure" in lower or "boostpressure" in lower:
        return convert(value, "psi", target)
    if "pressure" in lower or "barometer" in lower:
        return convert(value, "inHg", target)
    if lower.endswith(".fuel.rate") or "fuel_rate" in lower:
        return convert(value, "gal/h", target)
    return value


def _rename_metric_key(key: str) -> str:
    replacements = {"_gallons": "_liters", "_gph": "_lph", "_gallon": "_liter", "nm_per_gallon": "nm_per_liter", "gallons_per_nm": "liters_per_nm", "distance_from_dock_ft": "distance_from_dock_m", "dock_radius_ft": "dock_radius_m"}
    for old, new in replacements.items():
        if key.endswith(old) or old in key:
            return key.replace(old, new)
    return key


def normalize_context(context: dict[str, Any], pref: str = "us_customary") -> dict[str, Any]:
    """Deep-copy context and make unit-bearing model fields self-consistent."""
    p = normalize_preference(pref)
    result = copy.deepcopy(context)
    fallback_units: dict[str, str] = {}
    ha = result.get("ha_telemetry")
    if isinstance(ha, dict):
        for item in ha.get("matched", []):
            if isinstance(item, dict) and item.get("entity_id"):
                fallback_units[str(item["entity_id"])] = str(item.get("unit") or "")
    if p == "us_customary":
        result.setdefault("display_units", "us_customary")
        return result

    def walk(value: Any, parent: str = "", inherited_units: dict[str, str] | None = None) -> Any:
        if isinstance(value, dict):
            source_units = dict(inherited_units or fallback_units)
            if isinstance(value.get("units"), dict):
                source_units = {str(key): str(unit or "") for key, unit in value["units"].items()}
            def source_for(measurement: str) -> str | None:
                if measurement in source_units:
                    return source_units[measurement]
                return next((unit for key, unit in source_units.items() if measurement.endswith("." + key)), None)
            if p == "metric" and isinstance(value.get("port_measurement"), str):
                measurement = str(value["port_measurement"])
                for comparison_key in ("all_aligned_samples", "rpm_matched_within_50"):
                    payload = value.get(comparison_key)
                    if isinstance(payload, dict):
                        for stat_key in ("port_avg", "starboard_avg", "starboard_minus_port"):
                            if isinstance(payload.get(stat_key), (int, float)):
                                payload[stat_key] = convert_delta(float(payload[stat_key]), "F", "C") if "temp" in measurement.lower() and stat_key == "starboard_minus_port" else display_value(measurement, float(payload[stat_key]), p)
            if p == "metric" and parent and "." in parent and isinstance(value, dict):
                # Numeric history stats are nested beneath canonical measurement
                # paths. Convert only those known telemetry paths, never facts
                # or arbitrary user dictionaries containing a temperature key.
                known = parent in source_units or parent.startswith(("propulsion.", "environment.", "navigation.", "electrical.", "tanks."))
                if known:
                    value_keys = {"min", "max", "avg", "average", "last", "latest", "first", "delta", "difference", "p50", "median", "stddev"}
                    for stat_key, stat_value in list(value.items()):
                        if stat_key in value_keys and isinstance(stat_value, (int, float)):
                            source = source_for(parent)
                            source_key = (source if source is not None else "F").lower().replace("°", "")
                            canonical_temp = "temperature" in parent.lower() or parent.lower().endswith(".temp")
                            explicit_temp = source is not None and source != "" and source_key in {"f", "c"}
                            value[stat_key] = convert_delta(float(stat_value), source or "F", "C") if stat_key in {"delta", "difference", "stddev"} and (canonical_temp or explicit_temp) and (source is None or source_key in {"f", "c"}) else display_value(parent, float(stat_value), p, source)
            out: dict[str, Any] = {}
            for raw_key, item in value.items():
                key = _rename_metric_key(str(raw_key))
                if p == "metric" and parent == "units" and isinstance(item, str):
                    item = unit_for_measurement(str(raw_key), p, item) or item
                if key != raw_key and isinstance(item, (int, float)):
                    # Known summary keys are US customary in the canonical
                    # payloads.  Ratios are handled by their key semantics.
                    if "nm_per_liter" in key:
                        item = float(item) * US_GALLONS_PER_LITER
                    elif "liters_per_nm" in key:
                        item = float(item) / US_GALLONS_PER_LITER
                    elif key.endswith("_m") and str(raw_key).endswith("_ft"):
                        item = float(item) * M_PER_FT
                    elif "liters" in key or "lph" in key:
                        item = convert(float(item), "us gal" if "liter" in key else "gal/h", "L" if "liter" in key else "L/h")
                if p == "metric" and isinstance(item, (int, float)) and (str(raw_key) in source_units or str(raw_key).startswith(("propulsion.", "environment.", "navigation.", "electrical.", "tanks."))):
                    item = display_value(str(raw_key), float(item), p, source_for(str(raw_key)))
                out[key] = walk(item, key, source_units)
            if p == "metric" and "state" in out and ("unit" in out or "unit_of_measurement" in out):
                source = str(out.get("unit") or out.get("unit_of_measurement") or "")
                targets = {"F": "°C", "°F": "°C", "psi": "kPa", "Pa": "kPa", "inHg": "hPa", "ft": "m", "gal": "L", "gal/h": "L/h"}
                target = targets.get(source)
                try:
                    if target:
                        out["state"] = str(round(convert(float(out["state"]), source, target), 4))
                        if "unit" in out:
                            out["unit"] = target
                        if "unit_of_measurement" in out:
                            out["unit_of_measurement"] = target
                except (TypeError, ValueError):
                    pass
            return out
        if isinstance(value, list):
            items = [walk(item, parent, inherited_units) for item in value]
            if p == "metric" and parent == "notes":
                items = [
                    item.replace("Fuel rate is integrated by minute and reported in US gallons (gal, gal/h).", "Fuel rate is integrated by minute and reported in liters (L, L/h).")
                    .replace("Fuel rate is converted from SignalK m3/s to gal/h.", "Fuel rate is converted from SignalK m3/s to L/h.")
                    if isinstance(item, str) else item for item in items
                ]
            return items
        return value

    result = walk(result)
    result["display_units"] = "metric"
    result["display_unit_rules"] = {"fuel": "L and L/h", "temperature": "°C", "engine_pressure": "kPa", "barometer": "hPa", "depth_distance": "m", "speed_distance": "knots and NM"}
    return result
