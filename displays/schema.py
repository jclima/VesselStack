"""Display configuration and board-registry validation (stdlib only, no I/O except load helpers)."""
from __future__ import annotations

import ipaddress
import json
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

TOP_KEYS = {"schema", "mqtt_host", "heartbeat_s", "instruments", "displays"}
INSTRUMENT_KEYS = {"path", "label", "convert", "unit", "precision", "stale_after_s", "min_interval_ms"}
INSTRUMENT_REQUIRED = {"path", "label", "convert", "unit", "precision", "stale_after_s"}
DISPLAY_KEYS = {"name", "board", "home_assistant_api", "pages"}
DISPLAY_REQUIRED = {"name", "board", "pages"}
PAGE_KEYS = {"title", "layout", "items"}
BOARD_KEYS = {"name", "width", "height", "shape", "safe_inset", "content_width", "content_height", "layouts",
              "esphome_package"}

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


def validate_registry(reg) -> list[str]:
    errors: list[str] = []
    if not isinstance(reg, dict):
        return ["$: registry must be an object"]
    _unknown(reg, {"schema", "layouts", "boards"}, "$", errors)
    _missing(reg, {"schema", "layouts", "boards"}, "$", errors)
    if "schema" in reg and (not is_int(reg["schema"]) or reg["schema"] != 1):
        errors.append("$.schema: must be 1")
    layouts = reg.get("layouts")
    known: set = set()
    if "layouts" in reg:
        if not isinstance(layouts, dict) or not layouts:
            errors.append("$.layouts: must be a non-empty object")
        else:
            for name, cap in layouts.items():
                if not LAYOUT_ID.fullmatch(str(name)):
                    errors.append(f"$.layouts.{name}: invalid layout id")
                if not is_int(cap) or not 1 <= cap <= 64:
                    errors.append(f"$.layouts.{name}: capacity must be an integer 1..64")
                known.add(name)
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
                _missing(board, BOARD_KEYS, where, errors)
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
                bl = board.get("layouts")
                if "layouts" in board:
                    if not isinstance(bl, list) or not bl:
                        errors.append(f"{where}.layouts: must be a non-empty list")
                    else:
                        for i, name in enumerate(bl):
                            if name not in known:
                                errors.append(f"{where}.layouts[{i}]: unknown layout {name!r}")
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

    displays = cfg.get("displays")
    if "displays" in cfg:
        if not isinstance(displays, dict):
            errors.append("$.displays: must be an object")
        else:
            boards = registry.get("boards", {})
            caps = registry.get("layouts", {})
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
                    capacity = None
                    if "layout" in page:
                        if not isinstance(layout, str) or layout not in caps:
                            errors.append(f"{pw}.layout: unknown layout {layout!r}")
                        elif board is not None and layout not in board.get("layouts", []):
                            errors.append(f"{pw}.layout: {layout!r} is not supported by board {disp['board']!r}")
                        else:
                            capacity = caps[layout]
                    items = page.get("items")
                    if "items" in page:
                        if not isinstance(items, list) or not items:
                            errors.append(f"{pw}.items: must be a non-empty list")
                            continue
                        if capacity is not None and len(items) > capacity:
                            errors.append(f"{pw}.items: {len(items)} items exceed layout capacity {capacity}")
                        seen: set = set()
                        for ii, item in enumerate(items):
                            if not isinstance(item, str) or item not in valid_ids:
                                errors.append(f"{pw}.items[{ii}]: unknown instrument {item!r}")
                            elif item in seen:
                                errors.append(f"{pw}.items[{ii}]: duplicate instrument {item!r}")
                            else:
                                seen.add(item)
    return errors


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
