"""Display configuration and board-registry validation (stdlib only, no I/O except load helpers)."""
from __future__ import annotations

import ipaddress
import json
import math
import re
from pathlib import Path

import conversions

MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_REGISTRY = MODULE_DIR / "boards" / "registry.json"

INSTRUMENT_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
DISPLAY_ID = re.compile(r"^[a-z]([a-z0-9-]{0,22}[a-z0-9])?$")
ESPHOME_PACKAGE = re.compile(r"^boards/[a-z0-9][a-z0-9.-]{0,63}\.yaml$")
BOARD_ID = re.compile(r"^[a-z][a-z0-9.-]{0,63}$")
LAYOUT_ID = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
SK_PATH = re.compile(r"^[A-Za-z][A-Za-z0-9]*(\.[A-Za-z0-9_-]+)+$")
HOST_LABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

TOP_KEYS = {"schema", "mqtt_host", "heartbeat_s", "instruments", "notifications", "ha", "displays",
            "night_schedule"}
NIGHT_SCHEDULE_KEYS = {"start", "end"}
HHMM = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
NOTIFICATION_KEYS = {"path", "label", "absent", "alarm"}
NOTIFICATION_REQUIRED = {"path", "label"}
NOTIFICATION_PREFIX = "notifications."
ABSENT_MODES = ("ok", "unknown")
ITEM_TYPES = ("instrument", "notification", "ha")
# Home Assistant items: a numeric sensor, or a binary / multi-valued state mapped to a severity and a fixed text.
HA_ENTITY_ID = re.compile(r"^[a-z_]+\.[a-z0-9_]{1,64}$")
HA_KEYS = {"entity_id", "label", "kind", "unit", "precision", "thresholds", "min", "max", "states", "default",
           "alarm"}
HA_REQUIRED = {"entity_id", "label", "kind"}
HA_KINDS = ("binary", "state", "numeric")
HA_NUMERIC_ONLY = {"unit", "precision", "thresholds", "min", "max"}
HA_STATE_KINDS = ("binary", "state")
HA_SEVERITIES = ("ok", "warn", "alarm")
HA_STATE_KEYS = {"match", "prefix", "sev", "text"}
HA_DEFAULT_KEYS = {"sev", "text"}
HA_MAX_STATES = 8
HA_TEXT_MAX = 10
HA_PATTERN_MAX = 64
# A binary item without an explicit ``default`` reads "?" as a warning: never green by accident.
# Home Assistant states that mean "no value" (kept in step with severity.HA_NO_VALUE).
HA_NO_VALUE = frozenset({"", "unknown", "unavailable", "none"})
HA_BINARY_DEFAULT = {"sev": "warn", "text": "?"}
SHAPES = ("round", "rect")
LAYOUT_KEYS = {"capacity", "min_items", "shapes", "types", "slots"}
# Positional slots (layout "electrical"): item N of a page fills slot N of the layout.
SLOT_KEYS = {"role", "types", "needs", "optional"}
SLOT_NEEDS = ("numeric", "min_max")
SLOT_ROLE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
# Layouts that scale a reading into a bar or an arc: every instrument on them needs a min and a max.
RANGE_LAYOUTS = ("tank-bars", "gauge-arc")
LAYOUT_HINTS = {"gauge-arc": "gauge-arc needs a round board; use single",
                "electrical": "electrical needs a round board; use grid-2x2"}
INSTRUMENT_KEYS = {"path", "label", "convert", "unit", "precision", "stale_after_s", "min_interval_ms",
                   "thresholds", "min", "max", "alarm"}
# Bounds in severity order; "low" bounds are checked with <, "high" bounds with >.
THRESHOLD_BOUNDS = (("alarm_below", "low"), ("warn_below", "low"), ("warn_above", "high"), ("alarm_above", "high"))
THRESHOLD_KEYS = {name for name, _ in THRESHOLD_BOUNDS} | {"hysteresis"}
INSTRUMENT_REQUIRED = {"path", "label", "convert", "unit", "precision", "stale_after_s"}
DISPLAY_KEYS = {"name", "board", "home_assistant_api", "pages", "ui"}
UI_KEYS = {"alarm_takeover", "alarm_page", "swipe", "header", "night", "day_brightness",
           "night_brightness", "dim_after_s"}
UI_BOOLEANS = ("alarm_takeover", "swipe", "header")
NIGHT_MODES = ("off", "manual", "schedule")
DEFAULT_DAY_BRIGHTNESS = 100
DEFAULT_NIGHT_BRIGHTNESS = 20
DIM_BRIGHTNESS = 10          # used by dim_after_s when night_brightness is not set
DISPLAY_REQUIRED = {"name", "board", "pages"}
PAGE_KEYS = {"title", "layout", "items"}
BOARD_REQUIRED = {"name", "width", "height", "shape", "safe_inset", "content_width", "content_height", "layouts",
                  "esphome_package"}
BOARD_KEYS = BOARD_REQUIRED | {"ring"}
RING_KEYS = {"header_y", "header_h", "footer_y"}
RING_MIN_HEADER_H = 50

DEFAULT_HEARTBEAT_S = 5
DEFAULT_MIN_INTERVAL_MS = 1000


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _text(value, lo: int, hi: int) -> bool:
    return isinstance(value, str) and lo <= len(value) <= hi and value.isprintable()


def valid_host(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    if len(value) > 253 or value.endswith("."):
        return False
    labels = value.split(".")
    if not all(HOST_LABEL.fullmatch(label) for label in labels):
        return False
    return not labels[-1].isdigit()  # numeric last label means a malformed IPv4 literal


def finite_number(value) -> bool:
    """True for int/float (not bool) that is finite."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_thresholds(th, where: str, errors: list) -> None:
    if not isinstance(th, dict):
        errors.append(f"{where}: must be an object")
        return
    _unknown(th, THRESHOLD_KEYS, where, errors)
    bounds = []
    for name, side in THRESHOLD_BOUNDS:
        if name not in th:
            continue
        if not finite_number(th[name]):
            errors.append(f"{where}.{name}: must be a finite number")
        else:
            bounds.append((name, side, th[name]))
    if not any(name in th for name, _ in THRESHOLD_BOUNDS):
        errors.append(f"{where}: needs at least one of alarm_below, warn_below, warn_above, alarm_above")
    if len(bounds) != sum(1 for name, _ in THRESHOLD_BOUNDS if name in th):
        return  # a bound is not a number: ordering cannot be checked
    gaps = []
    for (n1, s1, v1), (n2, s2, v2) in zip(bounds, bounds[1:]):
        if s1 == s2:
            if v1 > v2:
                errors.append(f"{where}: {n1} must not be greater than {n2}")
        elif v1 >= v2:
            errors.append(f"{where}: {n1} must be less than {n2}")
        gaps.append(v2 - v1)
    if "hysteresis" in th:
        h = th["hysteresis"]
        if not finite_number(h) or h < 0:
            errors.append(f"{where}.hysteresis: must be a finite number >= 0")
        elif gaps and h >= min(gaps):
            errors.append(f"{where}.hysteresis: must be less than the smallest gap between bounds ({min(gaps)})")


def ha_default(item: dict) -> dict:
    """The severity and text of a state no ``states`` entry matched (binary items have a built-in default)."""
    return item.get("default") or HA_BINARY_DEFAULT


def _validate_ha_states(item: dict, where: str, errors: list) -> None:
    kind = item.get("kind")
    states = item.get("states")
    if "states" not in item:
        errors.append(f"{where}.states: required for kind {kind!r}")
    elif not isinstance(states, list) or not 1 <= len(states) <= HA_MAX_STATES:
        errors.append(f"{where}.states: must be a list of 1..{HA_MAX_STATES} entries")
    else:
        for i, entry in enumerate(states):
            w = f"{where}.states[{i}]"
            if not isinstance(entry, dict):
                errors.append(f"{w}: must be an object")
                continue
            _unknown(entry, HA_STATE_KEYS, w, errors)
            keys = [k for k in ("match", "prefix") if k in entry]
            if len(keys) != 1:
                errors.append(f"{w}: needs exactly one of match or prefix")
            for key in keys:
                value = entry[key]
                if not (isinstance(value, str) and value.strip() and len(value) <= HA_PATTERN_MAX
                        and value.isprintable()):
                    errors.append(f"{w}.{key}: must be 1..{HA_PATTERN_MAX} printable characters "
                                  "(not only spaces)")
                elif value.strip().casefold() in HA_NO_VALUE:
                    errors.append(f"{w}.{key}: {value!r} is never matched: unknown, unavailable, none and "
                                  "empty states always read as no value")
            _validate_ha_outcome(entry, w, errors)
    default = item.get("default")
    if "default" not in item:
        if kind == "state":
            errors.append(f"{where}.default: required for kind 'state' (what an unmatched state shows)")
    elif not isinstance(default, dict):
        errors.append(f"{where}.default: must be an object with sev and text")
    else:
        _unknown(default, HA_DEFAULT_KEYS, f"{where}.default", errors)
        _validate_ha_outcome(default, f"{where}.default", errors)


def _validate_ha_outcome(entry: dict, where: str, errors: list) -> None:
    if "sev" not in entry:
        errors.append(f"{where}.sev: required")
    elif not (isinstance(entry["sev"], str) and entry["sev"] in HA_SEVERITIES):
        errors.append(f"{where}.sev: must be one of {', '.join(HA_SEVERITIES)}")
    if "text" not in entry:
        errors.append(f"{where}.text: required")
    elif not _text(entry["text"], 1, HA_TEXT_MAX):
        errors.append(f"{where}.text: must be 1..{HA_TEXT_MAX} printable characters")


def validate_ha_item(item, where: str, heartbeat: int, errors: list) -> None:
    """One ``ha`` entry: a numeric sensor (like an instrument) or a binary / state mapping."""
    if not isinstance(item, dict):
        errors.append(f"{where}: must be an object")
        return
    _unknown(item, HA_KEYS, where, errors)
    _missing(item, HA_REQUIRED, where, errors)
    if "entity_id" in item and not (isinstance(item["entity_id"], str) and HA_ENTITY_ID.fullmatch(item["entity_id"])):
        errors.append(f"{where}.entity_id: must look like domain.object_id (lower case letters, digits, underscores)")
    if "label" in item and not _text(item["label"], 1, 16):
        errors.append(f"{where}.label: must be 1..16 printable characters")
    if "alarm" in item and not isinstance(item["alarm"], bool):
        errors.append(f"{where}.alarm: must be true or false")
    kind = item.get("kind")
    if "kind" in item and not (isinstance(kind, str) and kind in HA_KINDS):
        errors.append(f"{where}.kind: must be one of {', '.join(HA_KINDS)}")
        return
    if kind == "numeric":
        for key in ("states", "default"):
            if key in item:
                errors.append(f"{where}.{key}: not allowed for kind 'numeric'")
        for key in ("unit", "precision"):
            if key not in item:
                errors.append(f"{where}.{key}: required for kind 'numeric'")
        if "unit" in item and not (isinstance(item["unit"], str) and len(item["unit"]) <= 8
                                   and item["unit"].isprintable()):
            errors.append(f"{where}.unit: must be a printable string of at most 8 characters")
        if "precision" in item:
            _int_range(item, "precision", 0, 3, where, errors)
        if "thresholds" in item:
            validate_thresholds(item["thresholds"], f"{where}.thresholds", errors)
        for key in ("min", "max"):
            if key in item and not finite_number(item[key]):
                errors.append(f"{where}.{key}: must be a finite number")
        if (finite_number(item.get("min")) and finite_number(item.get("max"))
                and item["min"] >= item["max"]):
            errors.append(f"{where}.min: must be less than max")
    elif kind in HA_STATE_KINDS:
        for key in sorted(HA_NUMERIC_ONLY & set(item)):
            errors.append(f"{where}.{key}: only allowed for kind 'numeric'")
        _validate_ha_states(item, where, errors)


def ha_numeric(item) -> bool:
    return isinstance(item, dict) and item.get("kind") == "numeric"


def counts_for_alarm_summary(cfg: dict) -> bool:
    """True when at least one item would be counted in the bridge's alarm summary."""
    instruments = cfg.get("instruments")
    notifications = cfg.get("notifications")
    ha = cfg.get("ha")
    return any(isinstance(i, dict) and i.get("alarm", "thresholds" in i) is True
               for i in (instruments.values() if isinstance(instruments, dict) else ())) \
        or any(isinstance(n, dict) and n.get("alarm", True) is True
               for n in (notifications.values() if isinstance(notifications, dict) else ())) \
        or any(isinstance(h, dict) and h.get("alarm", True) is True
               for h in (ha.values() if isinstance(ha, dict) else ()))


def validate_night_schedule(value, where: str, errors: list) -> bool:
    """``{"start": "HH:MM", "end": "HH:MM"}`` in 24 h local time; the window may cross midnight."""
    if not isinstance(value, dict):
        errors.append(f"{where}: must be an object with start and end (HH:MM)")
        return False
    start = len(errors)
    _unknown(value, NIGHT_SCHEDULE_KEYS, where, errors)
    _missing(value, NIGHT_SCHEDULE_KEYS, where, errors)
    for key in sorted(NIGHT_SCHEDULE_KEYS & set(value)):
        if not (isinstance(value[key], str) and HHMM.fullmatch(value[key])):
            errors.append(f"{where}.{key}: must be a 24-hour time such as 21:30")
    if len(errors) == start and value["start"] == value["end"]:
        errors.append(f"{where}: start and end must differ")
    return len(errors) == start


def ui_header_enabled(ui) -> bool:
    return isinstance(ui, dict) and ui.get("header") is True


def validate_ui(ui, where: str, pages, instruments, notifications, errors: list, board=None,
                night_schedule=None, ha=None) -> None:
    """Per-display ``ui`` options (see UI_KEYS); absent keys keep the firmware exactly as it was."""
    if not isinstance(ui, dict):
        errors.append(f"{where}: must be an object")
        return
    _unknown(ui, UI_KEYS, where, errors)
    for key in UI_BOOLEANS:
        if key in ui and not isinstance(ui[key], bool):
            errors.append(f"{where}.{key}: must be true or false")
    if "alarm_page" in ui:
        count = len(pages) if isinstance(pages, list) else None
        if not is_int(ui["alarm_page"]) or ui["alarm_page"] < 0:
            errors.append(f"{where}.alarm_page: must be a page index (an integer >= 0)")
        elif count is not None and ui["alarm_page"] >= count:
            errors.append(f"{where}.alarm_page: page index {ui['alarm_page']} is out of range "
                          f"(the display has {count} page(s))")
    if ui.get("alarm_takeover") is True and not counts_for_alarm_summary(
            {"instruments": instruments, "notifications": notifications, "ha": ha}):
        errors.append(f"{where}.alarm_takeover: nothing counts towards the alarm summary; add an "
                      "instrument with thresholds (or alarm: true), a notification or a Home Assistant item")
    if ui.get("header") is True and isinstance(board, dict) and "ring" not in board:
        errors.append(f"{where}.header: the board has no header band (its registry entry has no "
                      "'ring' geometry)")
    night = ui.get("night", "off")
    if "night" in ui and not (isinstance(night, str) and night in NIGHT_MODES):
        errors.append(f"{where}.night: must be one of {', '.join(NIGHT_MODES)}")
    elif night == "schedule" and night_schedule is None:
        errors.append(f"{where}.night: 'schedule' needs a top-level night_schedule "
                      '({"start": "HH:MM", "end": "HH:MM"})')
    for key in ("day_brightness", "night_brightness"):
        if key in ui and not (is_int(ui[key]) and 5 <= ui[key] <= 100):
            errors.append(f"{where}.{key}: must be an integer 5..100 (percent)")
    if "dim_after_s" in ui and not (is_int(ui["dim_after_s"]) and 0 <= ui["dim_after_s"] <= 3600):
        errors.append(f"{where}.dim_after_s: must be an integer 0..3600 (0 = never dim)")
    day = ui.get("day_brightness", DEFAULT_DAY_BRIGHTNESS)
    dark = ui.get("night_brightness", DEFAULT_NIGHT_BRIGHTNESS)
    if (is_int(day) and is_int(dark) and 5 <= day <= 100 and 5 <= dark <= 100
            and "night_brightness" in ui and dark > day):
        errors.append(f"{where}.night_brightness: must not exceed day_brightness ({day})")


def _layout_hint(layout, board: dict, specs: dict) -> str:
    """The fix for a layout the board cannot show, when the layout knows one (rect board, gauge-arc)."""
    hint = LAYOUT_HINTS.get(layout)
    if hint and board.get("shape") not in specs[layout]["shapes"]:
        return f"; {hint}"
    return ""


def _unknown(obj: dict, allowed: set, where: str, errors: list) -> None:
    for key in sorted(obj, key=str):
        if key not in allowed:
            errors.append(f"{where}.{key}: unknown key")


def _missing(obj: dict, required: set, where: str, errors: list) -> None:
    for key in sorted(required - set(obj)):
        errors.append(f"{where}.{key}: required")


def _int_range(obj: dict, key: str, lo: int, hi: int, where: str, errors: list) -> bool:
    value = obj.get(key)
    if not is_int(value) or not lo <= value <= hi:
        errors.append(f"{where}.{key}: must be an integer {lo}..{hi}")
        return False
    return True


def _validate_layout(name, spec, version, errors: list):
    """Validate one registry layout entry; return it normalised, or None when invalid."""
    where = f"$.layouts.{name}"
    if is_int(spec) and version != 2:  # schema 1: a bare capacity
        if not 1 <= spec <= 64:
            errors.append(f"{where}: capacity must be an integer 1..64")
            return None
        return normalise_layout(spec)
    if not isinstance(spec, dict) or version == 1:
        errors.append(f"{where}: " + ("must be an integer capacity in schema 1"
                                      if version == 1 else "must be an object"))
        return None
    start = len(errors)
    _unknown(spec, LAYOUT_KEYS, where, errors)
    _missing(spec, {"capacity", "min_items", "shapes", "types"}, where, errors)
    if "capacity" in spec and not (is_int(spec["capacity"]) and 1 <= spec["capacity"] <= 64):
        errors.append(f"{where}: capacity must be an integer 1..64")
    if "min_items" in spec and not (is_int(spec["min_items"]) and 1 <= spec["min_items"] <= 64):
        errors.append(f"{where}.min_items: must be an integer 1..64")
    if (is_int(spec.get("capacity")) and is_int(spec.get("min_items"))
            and spec["min_items"] > spec["capacity"]):
        errors.append(f"{where}.min_items: must not exceed capacity")
    for key, allowed in (("shapes", SHAPES), ("types", ITEM_TYPES)):
        if key in spec:
            value = spec[key]
            if not (isinstance(value, list) and value and len(set(map(str, value))) == len(value)
                    and all(isinstance(v, str) and v in allowed for v in value)):
                errors.append(f"{where}.{key}: must be a non-empty list of unique values from "
                              f"{', '.join(allowed)}")
    if "slots" in spec:
        _validate_slots(spec, where, errors)
    return normalise_layout(spec) if len(errors) == start else None


def _validate_slots(spec: dict, where: str, errors: list) -> None:
    """``slots``: one entry per position of a page's items, with role, types, needs and optional."""
    slots, sw = spec["slots"], f"{where}.slots"
    if not isinstance(slots, list) or not slots:
        errors.append(f"{sw}: must be a non-empty list")
        return
    roles: set = set()
    seen_optional = False
    required = 0
    layout_types = spec.get("types") if isinstance(spec.get("types"), list) else []
    for i, slot in enumerate(slots):
        w = f"{sw}[{i}]"
        if not isinstance(slot, dict):
            errors.append(f"{w}: must be an object")
            continue
        _unknown(slot, SLOT_KEYS, w, errors)
        _missing(slot, {"role", "types"}, w, errors)
        role = slot.get("role")
        if "role" in slot:
            if not (isinstance(role, str) and SLOT_ROLE.fullmatch(role)):
                errors.append(f"{w}.role: must match ^[a-z][a-z0-9_]{{0,15}}$")
            elif role in roles:
                errors.append(f"{w}.role: duplicate role {role!r}")
            else:
                roles.add(role)
        if "types" in slot:
            types = slot["types"]
            if not (isinstance(types, list) and types and len(set(map(str, types))) == len(types)
                    and all(isinstance(t, str) and t in ITEM_TYPES for t in types)):
                errors.append(f"{w}.types: must be a non-empty list of unique values from "
                              f"{', '.join(ITEM_TYPES)}")
            elif not set(types) <= set(layout_types):
                errors.append(f"{w}.types: must be a subset of the layout types ({', '.join(layout_types)})")
        if "needs" in slot:
            needs = slot["needs"]
            if not (isinstance(needs, list) and len(set(map(str, needs))) == len(needs)
                    and all(isinstance(n, str) and n in SLOT_NEEDS for n in needs)):
                errors.append(f"{w}.needs: must be a list of unique values from {', '.join(SLOT_NEEDS)}")
        if "optional" in slot and not isinstance(slot["optional"], bool):
            errors.append(f"{w}.optional: must be true or false")
        if slot.get("optional") is True:
            seen_optional = True
        else:
            required += 1
            if seen_optional:
                errors.append(f"{w}: a required slot cannot follow an optional one (optional slots are trailing)")
    if is_int(spec.get("capacity")) and len(slots) != spec["capacity"]:
        errors.append(f"{sw}: must have one slot per item (capacity {spec['capacity']})")
    if is_int(spec.get("min_items")) and required != spec["min_items"]:
        errors.append(f"{where}.min_items: must equal the number of required slots ({required})")


def normalise_layout(spec) -> dict:
    """The v2 form of a layout entry; a schema 1 integer is the capacity of an instrument layout."""
    if isinstance(spec, dict):
        out = {"capacity": spec["capacity"], "min_items": spec["min_items"],
               "shapes": list(spec["shapes"]), "types": list(spec["types"])}
        if "slots" in spec:
            out["slots"] = [{**slot, **{k: list(slot[k]) for k in ("types", "needs") if k in slot}}
                            for slot in spec["slots"]]
        return out
    return {"capacity": spec, "min_items": 1, "shapes": list(SHAPES), "types": ["instrument"]}


def layout_specs(registry: dict) -> dict:
    """Every layout of a (validated) registry in the normalised v2 form, whatever its schema."""
    return {name: normalise_layout(spec) for name, spec in registry.get("layouts", {}).items()}


def _validate_ring(board: dict, where: str, dims_ok: bool, errors: list) -> None:
    """Optional header/footer bands, in pixels: they must lie outside the content box."""
    ring = board["ring"]
    rw = f"{where}.ring"
    if not isinstance(ring, dict):
        errors.append(f"{rw}: must be an object")
        return
    _unknown(ring, RING_KEYS, rw, errors)
    _missing(ring, RING_KEYS, rw, errors)
    good = all(_int_range(ring, key, 0, 4096, rw, errors) for key in sorted(RING_KEYS & set(ring)))
    if not good or set(ring) != RING_KEYS:
        return
    if ring["header_h"] < RING_MIN_HEADER_H:
        errors.append(f"{rw}.header_h: must be at least {RING_MIN_HEADER_H} pixels (two text rows)")
    if not (dims_ok and is_int(board.get("content_height")) and is_int(board.get("height"))):
        return
    top = (board["height"] - board["content_height"]) // 2
    if ring["header_y"] + ring["header_h"] > top:
        errors.append(f"{rw}: the header band (y {ring['header_y']}..{ring['header_y'] + ring['header_h']}) "
                      f"overlaps the content box that starts at y {top}")
    if ring["footer_y"] < top + board["content_height"] or ring["footer_y"] + 10 > board["height"]:
        errors.append(f"{rw}.footer_y: the page indicator (10 px high) must lie between the content "
                      f"box (ends at y {top + board['content_height']}) and the bottom edge")


def validate_registry(reg) -> list[str]:
    errors: list[str] = []
    if not isinstance(reg, dict):
        return ["$: registry must be an object"]
    _unknown(reg, {"schema", "layouts", "boards"}, "$", errors)
    _missing(reg, {"schema", "layouts", "boards"}, "$", errors)
    version = reg.get("schema")
    if "schema" in reg and (not is_int(version) or version not in (1, 2)):
        errors.append("$.schema: must be 1 or 2")
        version = None
    layouts = reg.get("layouts")
    known: dict = {}
    if "layouts" in reg:
        if not isinstance(layouts, dict) or not layouts:
            errors.append("$.layouts: must be a non-empty object")
        else:
            for name, spec in layouts.items():
                if not LAYOUT_ID.fullmatch(str(name)):
                    errors.append(f"$.layouts.{name}: invalid layout id")
                normalised = _validate_layout(name, spec, version, errors)
                known[name] = normalised
    boards = reg.get("boards")
    if "boards" in reg:
        if not isinstance(boards, dict) or not boards:
            errors.append("$.boards: must be a non-empty object")
        else:
            for bid, board in boards.items():
                where = f"$.boards.{bid}"
                if not BOARD_ID.fullmatch(str(bid)):
                    errors.append(f"{where}: invalid board id")
                if not isinstance(board, dict):
                    errors.append(f"{where}: must be an object")
                    continue
                _unknown(board, BOARD_KEYS, where, errors)
                _missing(board, BOARD_REQUIRED, where, errors)
                if "name" in board and not _text(board["name"], 1, 64):
                    errors.append(f"{where}.name: must be 1..64 printable characters")
                dims_ok = True
                for key in ("width", "height"):
                    if key in board:
                        dims_ok &= _int_range(board, key, 1, 4096, where, errors)
                if "esphome_package" in board and not (
                        isinstance(board["esphome_package"], str)
                        and ESPHOME_PACKAGE.fullmatch(board["esphome_package"])
                        and ".." not in board["esphome_package"]):
                    errors.append(f"{where}.esphome_package: must look like boards/NAME.yaml")
                if "shape" in board and board["shape"] not in ("round", "rect"):
                    errors.append(f"{where}.shape: must be 'round' or 'rect'")
                if "safe_inset" in board:
                    _int_range(board, "safe_inset", 0, 2048, where, errors)
                for key, dim in (("content_width", "width"), ("content_height", "height")):
                    if key in board and _int_range(board, key, 1, 4096, where, errors):
                        if dims_ok and board[key] > board[dim]:
                            errors.append(f"{where}.{key}: must not exceed {dim}")
                if "ring" in board:
                    _validate_ring(board, where, dims_ok, errors)
                bl = board.get("layouts")
                if "layouts" in board:
                    if not isinstance(bl, list) or not bl:
                        errors.append(f"{where}.layouts: must be a non-empty list")
                    else:
                        for i, name in enumerate(bl):
                            if not isinstance(name, str) or name not in known:
                                errors.append(f"{where}.layouts[{i}]: unknown layout {name!r}")
                            elif (known[name] is not None and board.get("shape") in SHAPES
                                  and board["shape"] not in known[name]["shapes"]):
                                errors.append(f"{where}.layouts[{i}]: layout {name!r} does not support "
                                              f"the {board['shape']} shape of this board")
    return errors


def validate_config(cfg, registry) -> list[str]:
    errors: list[str] = []
    if not isinstance(cfg, dict):
        return ["$: config must be an object"]
    _unknown(cfg, TOP_KEYS, "$", errors)
    _missing(cfg, {"schema", "mqtt_host", "instruments", "displays"}, "$", errors)
    if "schema" in cfg and (not is_int(cfg["schema"]) or cfg["schema"] != 1):
        errors.append("$.schema: must be 1")
    if "mqtt_host" in cfg and not valid_host(cfg["mqtt_host"]):
        errors.append("$.mqtt_host: must be an RFC 1123 hostname or an IP address")
    heartbeat = DEFAULT_HEARTBEAT_S
    if "heartbeat_s" in cfg:
        if _int_range(cfg, "heartbeat_s", 1, 60, "$", errors):
            heartbeat = cfg["heartbeat_s"]

    instruments = cfg.get("instruments")
    valid_ids: set = set()
    if "instruments" in cfg:
        if not isinstance(instruments, dict):
            errors.append("$.instruments: must be an object")
        else:
            for iid, inst in instruments.items():
                where = f"$.instruments.{iid}"
                if not INSTRUMENT_ID.fullmatch(str(iid)):
                    errors.append(f"{where}: instrument id must match ^[a-z][a-z0-9_]{{0,31}}$")
                if not isinstance(inst, dict):
                    errors.append(f"{where}: must be an object")
                    continue
                valid_ids.add(iid)
                _unknown(inst, INSTRUMENT_KEYS, where, errors)
                _missing(inst, INSTRUMENT_REQUIRED, where, errors)
                if "path" in inst and not (isinstance(inst["path"], str) and SK_PATH.fullmatch(inst["path"])):
                    errors.append(f"{where}.path: must be a dotted SignalK path")
                if "label" in inst and not _text(inst["label"], 1, 16):
                    errors.append(f"{where}.label: must be 1..16 printable characters")
                if "convert" in inst and not (isinstance(inst["convert"], str) and inst["convert"] in conversions.CONVERSIONS):
                    errors.append(f"{where}.convert: unknown conversion (one of {', '.join(conversions.CONVERSIONS)})")
                if "unit" in inst and not (isinstance(inst["unit"], str) and len(inst["unit"]) <= 8 and inst["unit"].isprintable()):
                    errors.append(f"{where}.unit: must be a printable string of at most 8 characters")
                if "precision" in inst:
                    _int_range(inst, "precision", 0, 3, where, errors)
                if "stale_after_s" in inst and _int_range(inst, "stale_after_s", 1, 3600, where, errors):
                    if inst["stale_after_s"] < 2 * heartbeat:
                        errors.append(f"{where}.stale_after_s: must be at least 2 x heartbeat_s ({2 * heartbeat})")
                if "min_interval_ms" in inst:
                    _int_range(inst, "min_interval_ms", 100, 60000, where, errors)
                if "thresholds" in inst:
                    validate_thresholds(inst["thresholds"], f"{where}.thresholds", errors)
                for key in ("min", "max"):
                    if key in inst and not finite_number(inst[key]):
                        errors.append(f"{where}.{key}: must be a finite number")
                if (finite_number(inst.get("min")) and finite_number(inst.get("max"))
                        and inst["min"] >= inst["max"]):
                    errors.append(f"{where}.min: must be less than max")
                if "alarm" in inst and not isinstance(inst["alarm"], bool):
                    errors.append(f"{where}.alarm: must be true or false")

    notifications = cfg.get("notifications")
    notification_ids: set = set()
    if "notifications" in cfg:
        if not isinstance(notifications, dict):
            errors.append("$.notifications: must be an object")
        else:
            for nid, note in notifications.items():
                where = f"$.notifications.{nid}"
                if not INSTRUMENT_ID.fullmatch(str(nid)):
                    errors.append(f"{where}: notification id must match ^[a-z][a-z0-9_]{{0,31}}$")
                if nid in valid_ids:
                    errors.append(f"{where}: id is already used by an instrument; ids must be unique")
                if not isinstance(note, dict):
                    errors.append(f"{where}: must be an object")
                    continue
                notification_ids.add(nid)
                _unknown(note, NOTIFICATION_KEYS, where, errors)
                _missing(note, NOTIFICATION_REQUIRED, where, errors)
                if "path" in note and not (isinstance(note["path"], str) and SK_PATH.fullmatch(note["path"])
                                           and note["path"].startswith(NOTIFICATION_PREFIX)):
                    errors.append(f"{where}.path: must be a dotted SignalK path starting with "
                                  f"{NOTIFICATION_PREFIX!r}")
                if "label" in note and not _text(note["label"], 1, 16):
                    errors.append(f"{where}.label: must be 1..16 printable characters")
                if "absent" in note and not (isinstance(note["absent"], str) and note["absent"] in ABSENT_MODES):
                    errors.append(f"{where}.absent: must be one of {', '.join(ABSENT_MODES)}")
                if "alarm" in note and not isinstance(note["alarm"], bool):
                    errors.append(f"{where}.alarm: must be true or false")

    ha = cfg.get("ha")
    ha_ids: set = set()
    if "ha" in cfg:
        if not isinstance(ha, dict):
            errors.append("$.ha: must be an object")
        else:
            for hid, item in ha.items():
                where = f"$.ha.{hid}"
                if not INSTRUMENT_ID.fullmatch(str(hid)):
                    errors.append(f"{where}: Home Assistant item id must match ^[a-z][a-z0-9_]{{0,31}}$")
                if hid in valid_ids:
                    errors.append(f"{where}: id is already used by an instrument; ids must be unique")
                if hid in notification_ids:
                    errors.append(f"{where}: id is already used by a notification; ids must be unique")
                if isinstance(item, dict):
                    ha_ids.add(hid)
                validate_ha_item(item, where, heartbeat, errors)

    night_ok = None
    if "night_schedule" in cfg and validate_night_schedule(cfg["night_schedule"], "$.night_schedule", errors):
        night_ok = cfg["night_schedule"]

    displays = cfg.get("displays")
    if "displays" in cfg:
        if not isinstance(displays, dict):
            errors.append("$.displays: must be an object")
        else:
            boards = registry.get("boards", {})
            specs = layout_specs(registry)
            for did, disp in displays.items():
                where = f"$.displays.{did}"
                if not DISPLAY_ID.fullmatch(str(did)):
                    errors.append(f"{where}: display id must match ^[a-z]([a-z0-9-]{{0,22}}[a-z0-9])?$")
                if not isinstance(disp, dict):
                    errors.append(f"{where}: must be an object")
                    continue
                _unknown(disp, DISPLAY_KEYS, where, errors)
                _missing(disp, DISPLAY_REQUIRED, where, errors)
                if "name" in disp and not _text(disp["name"], 1, 32):
                    errors.append(f"{where}.name: must be 1..32 printable characters")
                if "home_assistant_api" in disp and not isinstance(disp["home_assistant_api"], bool):
                    errors.append(f"{where}.home_assistant_api: must be true or false")
                board = boards.get(disp.get("board")) if isinstance(disp.get("board"), str) else None
                if "board" in disp and board is None:
                    errors.append(f"{where}.board: unknown board {disp['board']!r}")
                pages = disp.get("pages")
                if "ui" in disp:
                    validate_ui(disp["ui"], f"{where}.ui", pages, instruments, notifications, errors,
                                board=board, night_schedule=night_ok, ha=ha)
                if "pages" not in disp:
                    continue
                if not isinstance(pages, list) or not 1 <= len(pages) <= 8:
                    errors.append(f"{where}.pages: must be a list of 1..8 pages")
                    continue
                for pi, page in enumerate(pages):
                    pw = f"{where}.pages[{pi}]"
                    if not isinstance(page, dict):
                        errors.append(f"{pw}: must be an object")
                        continue
                    _unknown(page, PAGE_KEYS, pw, errors)
                    _missing(page, PAGE_KEYS, pw, errors)
                    if "title" in page and not _text(page["title"], 1, 16):
                        errors.append(f"{pw}.title: must be 1..16 printable characters")
                    layout = page.get("layout")
                    spec = None
                    if "layout" in page:
                        if not isinstance(layout, str) or layout not in specs:
                            errors.append(f"{pw}.layout: unknown layout {layout!r}")
                        elif board is not None and layout not in board.get("layouts", []):
                            errors.append(f"{pw}.layout: {layout!r} is not supported by board {disp['board']!r}"
                                          + _layout_hint(layout, board, specs))
                        elif board is not None and board.get("shape") not in specs[layout]["shapes"]:
                            errors.append(f"{pw}.layout: {layout!r} does not support the "
                                          f"{board.get('shape')} shape of board {disp['board']!r}"
                                          + _layout_hint(layout, board, specs))
                        else:
                            spec = specs[layout]
                    items = page.get("items")
                    if "items" in page:
                        if not isinstance(items, list) or not items:
                            errors.append(f"{pw}.items: must be a non-empty list")
                            continue
                        if spec is not None and len(items) > spec["capacity"]:
                            errors.append(f"{pw}.items: {len(items)} items exceed layout capacity "
                                          f"{spec['capacity']}")
                        if spec is not None and len(items) < spec["min_items"]:
                            errors.append(f"{pw}.items: layout {layout!r} needs at least "
                                          f"{spec['min_items']} item(s)")
                        seen: set = set()
                        slots = spec.get("slots") if spec is not None else None
                        for ii, item in enumerate(items):
                            if not isinstance(item, str) or (item not in valid_ids and item not in notification_ids
                                                             and item not in ha_ids):
                                if ha_ids:
                                    what = "instrument, notification or Home Assistant item"
                                elif notification_ids:
                                    what = "instrument or notification"
                                else:
                                    what = "instrument"
                                errors.append(f"{pw}.items[{ii}]: unknown {what} {item!r}")
                            elif item in seen:
                                errors.append(f"{pw}.items[{ii}]: duplicate item {item!r}")
                            else:
                                seen.add(item)
                                kind = ("instrument" if item in valid_ids else
                                        "notification" if item in notification_ids else "ha")
                                slot = slots[ii] if slots and ii < len(slots) else None
                                if slot is not None:
                                    problem = _slot_problem(slot, kind, item, layout, instruments, ha)
                                    if problem:
                                        errors.append(f"{pw}.items[{ii}] ({slot['role']}): {problem}")
                                    continue
                                if spec is not None and kind not in spec["types"]:
                                    errors.append(f"{pw}.items[{ii}]: layout {layout!r} does not accept "
                                                  f"{'Home Assistant' if kind == 'ha' else kind} items ({item!r})")
                                elif kind == "ha" and spec is not None:
                                    problem = _ha_layout_problem(ha[item], item, layout)
                                    if problem:
                                        errors.append(f"{pw}.items[{ii}]: {problem}")
                                elif (layout in RANGE_LAYOUTS and kind == "instrument"
                                      and isinstance(instruments.get(item), dict)
                                      and not all(finite_number(instruments[item].get(k)) for k in ("min", "max"))):
                                    errors.append(f"{pw}.items[{ii}]: instrument {item!r} needs min and max "
                                                  f"on layout {layout!r}")
    return errors


STATUS_LAYOUT = "status-grid"


def _ha_layout_problem(entry, item: str, layout: str) -> str | None:
    """Why a Home Assistant item cannot be shown on a layout that accepts the type, or None.

    Numeric items behave like instruments (a bar or an arc also needs min and max); a binary or
    state item is a word, which only the status-grid can show.
    """
    if not isinstance(entry, dict):
        return None
    if entry.get("kind") in HA_STATE_KINDS:
        if layout != STATUS_LAYOUT:
            return (f"Home Assistant item {item!r} is a {entry['kind']} item; layout {layout!r} shows "
                    f"numbers, only {STATUS_LAYOUT} can show it")
        return None
    if layout in RANGE_LAYOUTS and not all(finite_number(entry.get(k)) for k in ("min", "max")):
        return f"Home Assistant item {item!r} needs min and max on layout {layout!r}"
    return None


def _slot_problem(slot: dict, kind: str, item: str, layout: str, instruments, ha=None) -> str | None:
    """Why ``item`` cannot fill ``slot`` of ``layout``, or None."""
    if kind not in slot["types"]:
        return (f"layout {layout!r} does not accept {kind} items here ({item!r}); "
                f"this slot takes {' or '.join(slot['types'])}")
    if kind == "ha":
        entry = ha.get(item) if isinstance(ha, dict) else None
        if isinstance(entry, dict) and entry.get("kind") != "binary":
            return f"Home Assistant item {item!r} must be a binary item to fill this slot"
        return None
    needs = slot.get("needs", [])
    if "numeric" in needs and kind != "instrument":
        return f"{kind} {item!r} is not numeric; this slot needs an instrument"
    inst = instruments.get(item) if kind == "instrument" and isinstance(instruments, dict) else None
    if ("min_max" in needs and isinstance(inst, dict)
            and not all(finite_number(inst.get(k)) for k in ("min", "max"))):
        return f"instrument {item!r} needs min and max on layout {layout!r}"
    return None


def _no_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=_no_duplicate_keys)


def load_registry(path=DEFAULT_REGISTRY):
    return load_json(path)
