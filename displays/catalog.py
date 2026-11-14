"""Display data catalog: what a page can show, merged with what the Signal K server really publishes.

Stdlib only. ``catalog.json`` lists common Signal K paths with a label, icon, unit by unit system and
suggested limits. ``fetch_live`` asks the configured Signal K server once for its numeric leaves (and
the names of active notifications) so the Control Panel can mark what really has data;
``merged_catalog`` combines both with ``displays.json``; ``materialise`` turns a chosen row into the
instrument (or notification) to add to the configuration.

Privacy: ``fetch_live`` keeps only ``{path, si_units, age_s, value}`` of finite numeric leaves whose
path is not a position, destination, MMSI, name, UUID or call sign. The raw tree is never written
anywhere and nothing is cached on disk. Suggested thresholds are never applied unless the caller opts in
(an instrument with thresholds counts in the boat-wide alarm summary).
"""
from __future__ import annotations

import contextlib
import datetime
import http.client
import json
import math
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import conversions  # noqa: E402
import render  # noqa: E402
import schema  # noqa: E402

CATALOG_FILE = Path(__file__).resolve().parent / "catalog.json"
CATALOG_SCHEMA = 1
UNIT_SYSTEMS = ("metric", "us_customary")
# BOAT_UNITS aliases read as metric, like boat-chat/display_units.py; "nautical" is the legacy value
# of older setup wizards (knots and nautical miles are kept in both systems). Anything else is
# us_customary, the installer default.
METRIC_ALIASES = frozenset({"metric", "si", "metre", "meters", "nautical"})

LIVE_TIMEOUT_S = 5
LIVE_TOTAL_S = 10          # whole-request budget, so a slow-drip server cannot hold the command
LIVE_MAX_BYTES = 4 * 1024 * 1024
LIVE_MAX_LEAVES = 5000
LIVE_MAX_NODES = 200000
LIVE_MAX_DEPTH = 12
LIVE_PATH = "/signalk/v1/api/vessels/self"
# Keys of a Signal K leaf that are metadata, not child nodes.
LEAF_METADATA = frozenset({"meta", "$source", "source", "timestamp", "pgn", "sentence", "values"})
# A path with any of these segments is never returned, nor is one with a segment starting with
# "position" (navigation.position, navigation.anchor.position...) or "destination". The identity names
# are leaves with string values and would not pass the numeric filter anyway: this is a second wall.
# Engine and tank instances called "port" and "starboard" are ordinary data and stay.
DENY_SEGMENTS = frozenset({"latitude", "longitude", "mmsi", "uuid", "name", "callsign"})
DENY_PREFIXES = ("position", "destination")

# quantity -> unit system -> (conversion, display unit). Every conversion exists in conversions.py.
_BOTH = lambda pair: {"metric": pair, "us_customary": pair}   # noqa: E731
QUANTITIES = {
    "depth": {"metric": ("none", "m"), "us_customary": ("m->ft", "ft")},
    "temperature": {"metric": ("K->C", "°C"), "us_customary": ("K->F", "°F")},
    "pressure": {"metric": ("Pa->bar", "bar"), "us_customary": ("Pa->psi", "psi")},
    "barometric": _BOTH(("Pa->hPa", "hPa")),
    "volume": {"metric": ("m3->L", "L"), "us_customary": ("m3->gal", "gal")},
    "flow": {"metric": ("m3/s->L/h", "L/h"), "us_customary": ("m3/s->gal/h", "gal/h")},
    "speed": _BOTH(("m/s->kn", "kn")),
    "heading": _BOTH(("rad->deg_360", "°")),
    "angle": _BOTH(("rad->deg_signed", "°")),
    "ratio": _BOTH(("ratio->%", "%")),
    "rpm": _BOTH(("Hz->rpm", "rpm")),
    "charge": _BOTH(("C->Ah", "Ah")),
    "voltage": _BOTH(("none", "V")),
    "current": _BOTH(("none", "A")),
    "power": _BOTH(("none", "W")),
    "distance": _BOTH(("m->nmi", "nmi")),
    "duration": _BOTH(("s->h", "h")),
}
GROUP_ORDER = ("Navigation", "Wind", "Depth & water", "Electrical", "Tanks", "Engine", "Environment",
               "Notifications")
UNCATALOGUED = "Uncatalogued"
ENTRY_KEYS = {"key", "path", "group", "label", "icon", "quantity", "precision", "stale_after_s",
              "min_interval_ms", "range", "suggested_thresholds", "roles", "default_instance", "note",
              "kind", "absent"}
ENTRY_KINDS = ("instrument", "notification")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
LABEL_MAX = 16
ELECTRICAL_ROLES = ("voltage", "soc", "current", "shore", "solar")
GENERIC_LABEL = "Value"


# --- units ---------------------------------------------------------------------------

def normalize_units(value) -> str:
    """BOAT_UNITS to ``metric`` or ``us_customary`` (legacy ``nautical`` is metric; unset is us_customary)."""
    return "metric" if str(value or "").strip().lower() in METRIC_ALIASES else "us_customary"


# --- loading and validating the catalog ----------------------------------------------------

def _wildcards(path: str) -> int:
    return path.split(".").count("*")


def _path_ok(path) -> bool:
    if not isinstance(path, str):
        return False
    segments = path.split(".")
    return (len(segments) >= 2 and re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", segments[0]) is not None
            and all(seg == "*" or SEGMENT_RE.fullmatch(seg) for seg in segments[1:]))


def validate_catalog(data) -> list[str]:
    """Problems in a parsed catalog.json (empty when valid)."""
    if not isinstance(data, dict):
        return ["$: catalog must be an object"]
    errors: list = []
    if data.get("schema") != CATALOG_SCHEMA:
        errors.append(f"$.schema: must be {CATALOG_SCHEMA}")
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        return errors + ["$.entries: must be a non-empty list"]
    seen: set = set()
    icons = schema.icon_map()
    for index, entry in enumerate(entries):
        where = f"$.entries[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be an object")
            continue
        for key in entry:
            if key not in ENTRY_KEYS:
                errors.append(f"{where}.{key}: unknown key")
        key = entry.get("key")
        if not (isinstance(key, str) and KEY_RE.fullmatch(key)):
            errors.append(f"{where}.key: must match {KEY_RE.pattern}")
        elif key in seen:
            errors.append(f"{where}.key: duplicate {key!r}")
        else:
            seen.add(key)
        kind = entry.get("kind", "instrument")
        if kind not in ENTRY_KINDS:
            errors.append(f"{where}.kind: must be one of {', '.join(ENTRY_KINDS)}")
        path = entry.get("path")
        if not _path_ok(path):
            errors.append(f"{where}.path: must be a dotted Signal K path ('*' stands for one segment)")
        elif kind == "notification" and (not path.startswith(schema.NOTIFICATION_PREFIX) or "*" in path):
            errors.append(f"{where}.path: a notification path starts with {schema.NOTIFICATION_PREFIX!r} "
                          "and has no wildcard")
        if not (isinstance(entry.get("group"), str) and entry["group"]):
            errors.append(f"{where}.group: required")
        label = entry.get("label")
        if not (isinstance(label, str) and label):
            errors.append(f"{where}.label: required")
        elif "{" in label.replace("{0}", ""):
            errors.append(f"{where}.label: only the {{0}} placeholder is allowed")
        elif "{0}" in label and _wildcards(path if isinstance(path, str) else "") < 1:
            errors.append(f"{where}.label: {{0}} needs a wildcard in the path")
        elif not label.replace("{0}", "").isprintable():
            errors.append(f"{where}.label: must be printable")
        if "icon" in entry and entry["icon"] not in icons:
            errors.append(f"{where}.icon: unknown icon {entry['icon']!r} (see esphome/fonts/icons.json)")
        if "default_instance" in entry and not (
                isinstance(entry["default_instance"], str) and SEGMENT_RE.fullmatch(entry["default_instance"])):
            errors.append(f"{where}.default_instance: must be a path segment")
        if _wildcards(path if isinstance(path, str) else "") and "default_instance" not in entry:
            errors.append(f"{where}.default_instance: required for a wildcard path")
        if _wildcards(path if isinstance(path, str) else "") > 1:
            errors.append(f"{where}.path: at most one wildcard")
        if kind == "notification":
            for banned in ("quantity", "range", "suggested_thresholds", "precision", "stale_after_s"):
                if banned in entry:
                    errors.append(f"{where}.{banned}: not used by a notification")
            if "absent" in entry and entry["absent"] not in schema.ABSENT_MODES:
                errors.append(f"{where}.absent: must be one of {', '.join(schema.ABSENT_MODES)}")
            continue
        if entry.get("quantity") not in QUANTITIES:
            errors.append(f"{where}.quantity: must be one of {', '.join(QUANTITIES)}")
        if not (schema.is_int(entry.get("precision")) and 0 <= entry["precision"] <= 3):
            errors.append(f"{where}.precision: must be an integer 0..3")
        if not (schema.is_int(entry.get("stale_after_s")) and 1 <= entry["stale_after_s"] <= 3600):
            errors.append(f"{where}.stale_after_s: must be an integer 1..3600")
        if "min_interval_ms" in entry and not (
                schema.is_int(entry["min_interval_ms"]) and 100 <= entry["min_interval_ms"] <= 60000):
            errors.append(f"{where}.min_interval_ms: must be an integer 100..60000")
        for role in entry.get("roles", []):
            if role not in ELECTRICAL_ROLES:
                errors.append(f"{where}.roles: unknown electrical role {role!r}")
        for field_name in ("range", "suggested_thresholds"):
            if field_name not in entry:
                continue
            block = entry[field_name]
            if not (isinstance(block, dict) and set(block) == set(UNIT_SYSTEMS)):
                errors.append(f"{where}.{field_name}: must have exactly {' and '.join(UNIT_SYSTEMS)}")
                continue
            for system in UNIT_SYSTEMS:
                if field_name == "range":
                    pair = block[system]
                    if not (isinstance(pair, list) and len(pair) == 2 and all(schema.finite_number(v) for v in pair)
                            and pair[0] < pair[1]):
                        errors.append(f"{where}.range.{system}: must be [min, max] with min < max")
                else:
                    schema.validate_thresholds(block[system], f"{where}.suggested_thresholds.{system}", errors)
    return errors


def load_catalog(path=None) -> dict:
    """Load and validate catalog.json; raise ValueError listing every problem."""
    data = schema.load_json(path or CATALOG_FILE)
    problems = validate_catalog(data)
    if problems:
        raise ValueError("invalid catalog file:\n  " + "\n  ".join(problems))
    return data


# --- paths, labels and ids ---------------------------------------------------------------

def match_path(pattern: str, path: str) -> tuple | None:
    """The segments a path has where the pattern has ``*`` (empty tuple for a plain match), else None."""
    want, have = pattern.split("."), path.split(".")
    if len(want) != len(have):
        return None
    found = []
    for expected, actual in zip(want, have):
        if expected == "*":
            found.append(actual)
        elif expected != actual:
            return None
    return tuple(found)


def _fill(pattern: str, instances) -> str:
    parts = iter(instances)
    return ".".join(next(parts) if seg == "*" else seg for seg in pattern.split("."))


def clean_label(text: str, fallback: str = GENERIC_LABEL) -> str:
    """A label the display font can draw: unsupported glyphs removed, 16 characters at most."""
    bad = set(render.unsupported_chars(text))
    cleaned = "".join(ch for ch in text if ch not in bad and ch.isprintable()).strip()
    return (cleaned[:LABEL_MAX].strip()) or fallback


def entry_label(template: str, instance: str | None, single: bool) -> str:
    """The label of an entry; ``{0}`` is the matched instance. A lone numeric instance (tank 0) is left out."""
    if "{0}" not in template:
        return clean_label(template)
    if instance is None:
        return clean_label(template.replace("{0}", "").replace("  ", " "))
    if single and instance.isdigit():
        return clean_label(re.sub(r"\s*\{0\}", "", template))
    shown = instance[:1].upper() + instance[1:]
    return clean_label(template.replace("{0}", shown))


def slug(text: str) -> str:
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text).lower()
    snake = re.sub(r"[^a-z0-9_]+", "_", snake)
    return re.sub(r"_+", "_", snake).strip("_")


def id_stem(text: str) -> str:
    stem = slug(text)[:32].strip("_") or "item"
    return stem if stem[0].isalpha() else ("i_" + stem)[:32]


def allocate_id(stem: str, used: set) -> str:
    """``stem`` or ``stem_2``, ``stem_3``... that is free in ``used``; always a valid instrument id."""
    stem = id_stem(stem)
    candidate, n = stem, 2
    while candidate in used:
        suffix = f"_{n}"
        candidate = stem[:32 - len(suffix)].rstrip("_") + suffix
        n += 1
    return candidate


def _used_ids(config: dict) -> set:
    used: set = set()
    for section in ("instruments", "notifications", "ha"):
        block = config.get(section)
        if isinstance(block, dict):
            used.update(block)
    return used


def denied_path(path: str) -> bool:
    for segment in path.lower().split("."):
        if segment in DENY_SEGMENTS or segment.startswith(DENY_PREFIXES):
            return True
    return False


# --- live data from the Signal K server --------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect (the bridge refuses them too): a token must never follow one."""

    def redirect_request(self, *_args, **_kwargs):
        return None


def _rest_url(signalk_url: str) -> str:
    text = (signalk_url or "").strip()
    parts = urlsplit(text)
    scheme = {"http": "http", "https": "https", "ws": "http", "wss": "https"}.get(parts.scheme)
    if scheme is None or not parts.netloc:
        raise ValueError("SIGNALK_URL is missing or not an http(s) address")
    if "@" in parts.netloc:
        raise ValueError("SIGNALK_URL must not embed credentials; use DISPLAYS_SIGNALK_TOKEN")
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + LIVE_PATH, "", ""))


def _age_seconds(stamp, now: datetime.datetime) -> float | None:
    if not isinstance(stamp, str):
        return None
    try:
        when = datetime.datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return round(max(0.0, (now - when).total_seconds()), 1)


def _meta_units(node: dict) -> str | None:
    meta = node.get("meta")
    units = meta.get("units") if isinstance(meta, dict) else None
    return units if isinstance(units, str) and 0 < len(units) <= 16 and units.isprintable() else None


def _numeric(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def extract_leaves(tree, now: datetime.datetime | None = None) -> dict:
    """Numeric leaves and active notification paths of a ``vessels/self`` document (pure; nothing is kept).

    Returns ``{"leaves": [{path, si_units, age_s, value}], "notifications": [path], "truncated": bool}``.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    leaves: list = []
    notes: list = []
    truncated = False
    nodes = 0
    stack = [(tree, ())]
    while stack:
        node, segments = stack.pop()
        nodes += 1
        if nodes > LIVE_MAX_NODES or len(segments) > LIVE_MAX_DEPTH:
            truncated = True
            continue
        if not isinstance(node, dict):
            continue
        if segments and "value" in node:
            path = ".".join(segments)
            if not schema.SK_PATH.fullmatch(path) or denied_path(path):
                continue
            value = node["value"]
            if path.startswith(schema.NOTIFICATION_PREFIX):
                if isinstance(value, dict) and "state" in value:
                    notes.append(path)   # the name only: the message text is never read
            elif _numeric(value):
                if len(leaves) >= LIVE_MAX_LEAVES:
                    truncated = True
                    continue
                leaves.append({"path": path, "si_units": _meta_units(node),
                               "age_s": _age_seconds(node.get("timestamp"), now), "value": value})
            continue
        for key, child in node.items():
            if isinstance(key, str) and key not in LEAF_METADATA and isinstance(child, dict):
                stack.append((child, segments + (key,)))
    leaves.sort(key=lambda leaf: leaf["path"])
    return {"leaves": leaves, "notifications": sorted(set(notes)), "truncated": truncated}


def live_error(message: str) -> dict:
    return {"ok": False, "error": message, "leaves": [], "notifications": [], "truncated": False}


def _socket_of(response):
    """The socket under an ``http.client.HTTPResponse`` (``fp`` is a BufferedReader over a SocketIO), or None."""
    return getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)


def _download(request, opener, deadline: float):
    """GET ``request`` and return ``(body, error)``; every wait is bounded by ``deadline`` (a monotonic time).

    Connect uses ``min(LIVE_TIMEOUT_S, remaining)``; before each body read the socket timeout is set to the
    remaining budget and ``read1`` (at most one recv) is used, so a server that drips bytes cannot stretch the
    total. A header-phase drip is bounded by the caller's thread join.
    """
    try:
        with opener.open(request, timeout=max(0.05, min(LIVE_TIMEOUT_S, deadline - time.monotonic()))) as response:
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > LIVE_MAX_BYTES:
                return None, "the Signal K response is larger than 4 MiB; refusing it"
            sock = _socket_of(response)
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, "the Signal K server answered too slowly"
                if sock is not None:
                    with contextlib.suppress(OSError):   # already closed after the last byte: read1 then returns b""
                        sock.settimeout(remaining)
                chunk = response.read1(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > LIVE_MAX_BYTES:
                    return None, "the Signal K response is larger than 4 MiB; refusing it"
                chunks.append(chunk)
    except urllib.error.HTTPError as exc:
        exc.close()
        if 300 <= exc.code < 400:
            return None, "the Signal K server redirected the request; redirects are refused"
        if exc.code in (401, 403):
            return None, f"the Signal K server refused access (HTTP {exc.code}); check DISPLAYS_SIGNALK_TOKEN"
        return None, f"the Signal K server answered HTTP {exc.code}"
    except TimeoutError:
        return None, "the Signal K server answered too slowly"
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            return None, "the Signal K server answered too slowly"
        return None, f"cannot reach the Signal K server ({type(exc).__name__})"
    return b"".join(chunks), None


def fetch_live(signalk_url: str, token: str = "", now: datetime.datetime | None = None) -> dict:
    """GET the configured server's ``vessels/self`` once and keep only the safe numeric leaves.

    Never raises for an unreachable server: the result is ``{"ok": False, "error": ...}``. Redirects are
    refused, the bearer token is sent only when set, the body is capped at 4 MiB, nothing is stored and the
    whole request (connect, headers, body) is bounded by ``LIVE_TOTAL_S``.
    """
    try:
        url = _rest_url(signalk_url)
    except ValueError as exc:
        return live_error(str(exc))
    headers = {"Accept": "application/json", "User-Agent": "vesselstack-displays"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    request = urllib.request.Request(url, headers=headers, method="GET")
    deadline = time.monotonic() + LIVE_TOTAL_S
    box: list = []
    worker = threading.Thread(target=lambda: box.append(_download(request, opener, deadline)), daemon=True)
    worker.start()
    worker.join(LIVE_TOTAL_S)
    if not box:   # still connecting or reading headers: abandon the daemon thread (its socket times out on its own)
        return live_error("the Signal K server answered too slowly")
    body, error = box[0]
    if error:
        return live_error(error)
    try:
        tree = json.loads(body)
    except ValueError:
        return live_error("the Signal K server did not return JSON")
    if not isinstance(tree, dict):
        return live_error("the Signal K server returned an unexpected document")
    found = extract_leaves(tree, now)
    return {"ok": True, "error": None, **found}


# --- resolving entries ---------------------------------------------------------------------

def resolve_quantity(quantity: str, units: str) -> tuple:
    return QUANTITIES[quantity][normalize_units(units)]


def _heartbeat(config: dict) -> int:
    value = config.get("heartbeat_s", schema.DEFAULT_HEARTBEAT_S)
    return value if schema.is_int(value) and 1 <= value <= 60 else schema.DEFAULT_HEARTBEAT_S


def stale_for(base: int, heartbeat: int) -> int:
    return max(1, min(3600, max(base, 2 * heartbeat)))


def _precision_for(convert: str, unit: str) -> int:
    if unit in ("°", "%", "rpm") or convert.startswith("rad->"):
        return 0
    return 2 if unit == "V" else 1


def uncatalogued_defaults(path: str, si_units: str | None, heartbeat: int) -> dict:
    """Instrument fields for a live numeric path the catalog does not know (from its ``meta.units``)."""
    last = path.rsplit(".", 1)[-1]
    lowered = path.lower()
    quantity = None
    unit_text = ""
    if si_units == "m/s":
        quantity = "speed"
    elif si_units == "rad":
        quantity = "heading" if any(word in lowered for word in ("heading", "course", "direction", "bearing")) \
            else "angle"
    elif si_units == "K":
        quantity = "temperature"
    elif si_units == "Pa":
        quantity = "pressure"
    elif si_units == "m":
        quantity = "depth"
    elif si_units == "ratio":
        quantity = "ratio"
    elif si_units == "m3":
        quantity = "volume"
    elif si_units == "m3/s":
        quantity = "flow"
    elif si_units == "C":
        quantity = "charge"
    elif si_units == "Hz" and ("revolution" in lowered or "rpm" in lowered):
        quantity = "rpm"
    elif si_units in ("V", "A", "W"):
        unit_text = si_units
    return {"quantity": quantity, "unit_text": unit_text, "label": clean_label(last),
            "stale_after_s": stale_for(10, heartbeat), "id_stem": id_stem("_".join(path.split(".")[-2:]))}


def layouts_for(kind: str, has_range: bool, roles: list, registry: dict | None) -> list:
    """Layouts of the registry that can show this kind of row (a bar or arc also needs min and max)."""
    if registry is None:
        return []
    out = []
    for name, spec in schema.layout_specs(registry).items():
        if name not in schema.SUPPORTED_LAYOUTS or kind not in spec["types"]:
            continue
        if kind == "instrument" and name in schema.RANGE_LAYOUTS and not has_range:
            continue
        if name == "electrical" and kind == "instrument" and not roles:
            continue
        out.append(name)
    return out


# --- the merged catalog ---------------------------------------------------------------------

def _used_on(config: dict, item_id: str) -> list:
    displays = config.get("displays")
    if not isinstance(displays, dict):
        return []
    return [did for did, display in displays.items()
            if isinstance(display, dict) and any(isinstance(page, dict) and item_id in page.get("items", [])
                                                 for page in display.get("pages", []))]


def _configured(config: dict, row: dict) -> str | None:
    """Id of the configured item with this row's path (and conversion, for an instrument)."""
    if row["kind"] == "notification":
        block = config.get("notifications")
        return next((i for i, n in (block or {}).items()
                     if isinstance(n, dict) and n.get("path") == row["path"]), None)
    block = config.get("instruments")
    return next((i for i, inst in (block or {}).items()
                 if isinstance(inst, dict) and inst.get("path") == row["path"]
                 and inst.get("convert") == row["convert"]), None)


def _instrument_row(entry: dict, units: str, heartbeat: int, path: str, instance, label: str, live_leaf,
                    template: bool, registry) -> dict:
    convert, unit = resolve_quantity(entry["quantity"], units)
    row = {
        "row_id": entry["key"] + (f":{instance}" if instance is not None else ""),
        "kind": "instrument", "catalog_key": entry["key"], "path": path, "group": entry["group"],
        "label": label, "quantity": entry["quantity"], "convert": convert, "unit": unit,
        "precision": entry["precision"], "stale_after_s": stale_for(entry["stale_after_s"], heartbeat),
    }
    if "*" in entry["path"]:
        row.update(path_template=entry["path"], instance=instance, template=template,
                   label_template=entry["label"])
    else:
        row.update(path_template=None, instance=None, template=False)
    for key in ("icon", "min_interval_ms", "note"):
        if key in entry:
            row[key] = entry[key]
    if "range" in entry:
        row["min"], row["max"] = entry["range"][units]
    if "suggested_thresholds" in entry:
        row["suggested_thresholds"] = dict(entry["suggested_thresholds"][units])
    row["roles"] = list(entry.get("roles", []))
    row["id_stem"] = entry["key"] if instance is None else f"{entry['key']}_{slug(instance)}"
    if live_leaf is not None:
        row["age_s"] = live_leaf["age_s"]
        shown = conversions.convert_and_round(convert, live_leaf["value"], entry["precision"])
        if shown is not None:
            row["value"] = shown
    row["layouts"] = layouts_for("instrument", "min" in row, row["roles"], registry)
    return row


def _notification_row(path: str, key, group: str, label: str, icon, absent, registry, note=None) -> dict:
    row = {"row_id": key or "note:" + path, "kind": "notification", "catalog_key": key, "path": path,
           "group": group, "label": label, "path_template": None, "instance": None, "template": False,
           "roles": [], "id_stem": id_stem(key or path.rsplit(".", 1)[-1])}
    if icon:
        row["icon"] = icon
    if absent:
        row["absent"] = absent
    if note:
        row["note"] = note
    row["layouts"] = layouts_for("notification", False, [], registry)
    return row


def merged_catalog(catalog: dict, live: dict | None, units: str, config: dict, registry: dict | None = None) -> list:
    """Every row a page can offer, in display order.

    ``live`` is a ``fetch_live`` result (None: not fetched). Each row carries ``live_state`` (``live``,
    ``no_live_data`` or ``unknown`` when the server was not asked), the ``definition`` that selecting it
    would add (absent when ``configured_as`` is set), a ``proposed_id`` and, when the row is already in
    ``displays.json``, ``configured_as`` and ``used_on``. Thresholds are listed as
    ``suggested_thresholds`` but are never part of ``definition``.
    """
    units = normalize_units(units)
    heartbeat = _heartbeat(config)
    asked = bool(live and live.get("ok"))
    leaves = {leaf["path"]: leaf for leaf in (live or {}).get("leaves", [])} if asked else {}
    active_notes = set((live or {}).get("notifications", [])) if asked else set()
    rows: list = []
    matched: set = set()
    matched_notes: set = set()
    for entry in catalog["entries"]:
        if entry.get("kind", "instrument") == "notification":
            row = _notification_row(entry["path"], entry["key"], entry["group"], clean_label(entry["label"]),
                                    entry.get("icon"), entry.get("absent"), registry, entry.get("note"))
            row["live_state"] = ("live" if entry["path"] in active_notes else "no_live_data") if asked else "unknown"
            if entry["path"] in active_notes:
                matched_notes.add(entry["path"])
            rows.append(row)
            continue
        pattern = entry["path"]
        if "*" not in pattern:
            leaf = leaves.get(pattern)
            row = _instrument_row(entry, units, heartbeat, pattern, None, clean_label(entry["label"]), leaf,
                                  False, registry)
            row["live_state"] = ("live" if leaf else "no_live_data") if asked else "unknown"
            if leaf:
                matched.add(pattern)
            rows.append(row)
            continue
        found = sorted((m[0], path) for path, leaf in leaves.items() if (m := match_path(pattern, path)))
        if found:
            for instance, path in found:
                matched.add(path)
                row = _instrument_row(entry, units, heartbeat, path, instance,
                                      entry_label(entry["label"], instance, len(found) == 1), leaves[path],
                                      False, registry)
                row["live_state"] = "live"
                rows.append(row)
        else:
            instance = entry["default_instance"]
            row = _instrument_row(entry, units, heartbeat, _fill(pattern, [instance]), instance,
                                  entry_label(entry["label"], instance, True), None, True, registry)
            row["live_state"] = "no_live_data" if asked else "unknown"
            rows.append(row)
    if asked:
        for path, leaf in leaves.items():
            if path in matched:
                continue
            info = uncatalogued_defaults(path, leaf["si_units"], heartbeat)
            if info["quantity"]:
                convert, unit = resolve_quantity(info["quantity"], units)
            else:
                convert, unit = "none", info["unit_text"]
            if unit and render.unsupported_chars(unit):
                unit = ""
            precision = _precision_for(convert, unit)
            shown = conversions.convert_and_round(convert, leaf["value"], precision)
            row = {"row_id": "unc:" + path, "kind": "instrument", "catalog_key": None, "path": path,
                   "group": UNCATALOGUED, "label": info["label"], "quantity": info["quantity"],
                   "convert": convert, "unit": unit, "precision": precision,
                   "stale_after_s": info["stale_after_s"], "path_template": None, "instance": None,
                   "template": False, "roles": [], "id_stem": info["id_stem"], "live_state": "live",
                   "age_s": leaf["age_s"], "si_units": leaf["si_units"]}
            if shown is not None:
                row["value"] = shown
            row["layouts"] = layouts_for("instrument", False, [], registry)
            rows.append(row)
        for path in (live or {}).get("notifications", []):
            if path in matched_notes:
                continue
            label = clean_label(path.rsplit(".", 1)[-1])
            row = _notification_row(path, None, "Notifications", label, None, "ok", registry)
            row["live_state"] = "live"
            rows.append(row)
    order = {name: i for i, name in enumerate(GROUP_ORDER)}
    rank = lambda group: len(order) + 1 if group == UNCATALOGUED else order.get(group, len(order))   # noqa: E731
    rows = [row for _, row in sorted(enumerate(rows), key=lambda pair: (rank(pair[1]["group"]), pair[0]))]
    used = _used_ids(config)
    represented = set()
    for row in rows:
        existing = _configured(config, row)
        if existing:
            represented.add(existing)
            row["configured_as"] = existing
            row["used_on"] = _used_on(config, existing)
            row["proposed_id"] = existing
            if row["kind"] == "instrument":
                definition = config["instruments"][existing]
                for key in ("min", "max"):
                    row.pop(key, None)
                    if key in definition:
                        row[key] = definition[key]
                row["layouts"] = layouts_for("instrument", "min" in row and "max" in row, row["roles"], registry)
            continue
        row["configured_as"] = None
        row["used_on"] = []
        item_id, definition = materialise(row, config, units, reserved=used)
        used.add(item_id)
        row["proposed_id"] = item_id
        row["definition"] = definition

    # A source can stop reporting without ceasing to be a configured value.
    for section, kind in (("instruments", "instrument"), ("notifications", "notification")):
        for item_id, definition in (config.get(section) or {}).items():
            if item_id in represented or denied_path(definition["path"]):
                continue
            path = definition["path"]
            has_range = "min" in definition and "max" in definition
            is_live = path in (leaves if kind == "instrument" else active_notes)
            row = {
                **{key: definition[key] for key in ("label", "unit", "min", "max", "convert") if key in definition},
                "row_id": f"configured:{item_id}", "group": "Configured values", "kind": kind,
                "path": path, "template": False, "roles": [],
                "live_state": ("live" if is_live else "no_live_data") if asked else "unknown",
                "configured_as": item_id, "proposed_id": item_id, "used_on": _used_on(config, item_id),
                "layouts": layouts_for(kind, has_range, [], registry),
            }
            if kind == "instrument" and is_live:
                value = conversions.convert_and_round(definition["convert"], leaves[path]["value"], definition["precision"])
                if value is not None:
                    row["value"] = value
            rows.append(row)
    return rows


# --- turning a row into configuration ------------------------------------------------------------

def materialise(row: dict, config: dict, units: str, apply_thresholds: bool = False,
                reserved=None) -> tuple:
    """``(id, definition)`` to put in ``instruments`` (or ``notifications`` for a notification row).

    An existing item with the same path (and conversion) is reused unchanged: nothing is ever modified
    or deleted implicitly. Otherwise the id is a slug of the row (``^[a-z][a-z0-9_]{0,31}$``) made unique
    across instruments, notifications and Home Assistant items (``_2``, ``_3``...). Suggested thresholds
    are added only with ``apply_thresholds=True`` (an instrument with thresholds, or a notification with
    no ``alarm`` key, counts in the boat-wide alarm summary); a new notification is written with
    ``"alarm": false`` unless ``apply_thresholds`` is set.
    """
    units = normalize_units(units)
    row = dict(row)
    if row.get("kind", "instrument") == "instrument" and row.get("quantity") and "convert" not in row:
        row["convert"], row["unit"] = resolve_quantity(row["quantity"], units)
    existing = _configured(config, row)
    if existing:
        block = config["notifications" if row["kind"] == "notification" else "instruments"]
        return existing, json.loads(json.dumps(block[existing]))
    used = _used_ids(config) | set(reserved or ())
    item_id = allocate_id(row.get("id_stem") or row["path"].rsplit(".", 1)[-1], used)
    label = clean_label(row.get("label") or row["path"].rsplit(".", 1)[-1])
    if row["kind"] == "notification":
        note = {"path": row["path"], "label": label}
        if row.get("absent"):
            note["absent"] = row["absent"]
        if not apply_thresholds:
            note["alarm"] = False
        if row.get("icon"):
            note["icon"] = row["icon"]
        return item_id, note
    inst = {"path": row["path"], "label": label, "convert": row["convert"], "unit": row["unit"],
            "precision": row["precision"], "stale_after_s": row["stale_after_s"]}
    if row.get("min_interval_ms"):
        inst["min_interval_ms"] = row["min_interval_ms"]
    if "min" in row and "max" in row:
        inst["min"], inst["max"] = row["min"], row["max"]
    if apply_thresholds and row.get("suggested_thresholds"):
        inst["thresholds"] = dict(row["suggested_thresholds"])
    if row.get("icon"):
        inst["icon"] = row["icon"]
    return item_id, inst
