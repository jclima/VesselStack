"""ESPHome project generator for VesselStack displays (stdlib only).

``render`` and ``export`` turn displays.json + the board registry + the stored
secrets into a self-contained ESPHome project for one display. Nothing here talks
to the network or runs ESPHome; compiling and flashing happen on a laptop (or, in
a later release, in a container on the Pi).

Safety notes:

* The device file contains no secrets (they go to ``secrets.yaml`` only).
* User strings (labels, units) are emitted as ``!literal`` double-quoted YAML
  scalars. ESPHome runs ``$var``, ``${jinja}`` and ``<% %>`` substitution (a full,
  unsandboxed Jinja environment) over every string, including values read from
  secrets.yaml, and 2026.10 has no ``$$`` escape; ``!literal`` is the only
  mechanism that suppresses it. They never appear inside a C++ lambda.
* The build tree is written under the root-owned ``$VESSELSTACK_ROOT/build``
  with directory-relative, no-follow operations (symlinks are refused).
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import gzip
import grp
import io
import os
import pwd
import re
import shutil
import stat
import struct
import sys
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lifecycle  # noqa: E402
import mqtt_auth  # noqa: E402
import schema  # noqa: E402
import topics  # noqa: E402

MODULE_DIR = Path(__file__).resolve().parent
ESPHOME_DIR = MODULE_DIR / "esphome"
FONTS_DIR = ESPHOME_DIR / "fonts"
BUILD_DIR_NAME = "build"
DIR_MODE = 0o700
FILE_MODE = 0o600
VALUE_GLYPHS = "0123456789.- "
BLANK_VALUE = "--"
PLACEHOLDER_WIFI = {"ssid": "placeholder-ssid", "password": "placeholder-password"}
PLACEHOLDER_NOTE = ("Wi-Fi credentials are not configured: wrote PLACEHOLDER values to "
                    "secrets.yaml. Do not flash this build.")
FONT_FILES = {"bold": "RobotoCondensed-Bold.ttf", "regular": "RobotoCondensed-Regular.ttf"}
FONT_LICENSE = "OFL.txt"
FONT_SIZES = {"grid": 56, "single": 96, "small": 20, "status_lg": 48, "status_sm": 30,
              "banner": 48, "banner_name": 28, "bar": 36}
STATUS_GAP = 6
STATUS_WORDS = {1: "OK", 2: "WARN", 3: "ALARM", 0: "--"}  # sev number -> fixed word
STATUS_FONTS = {"lg": "font_status_lg", "sm": "font_status_sm"}
TAP_TIME = "0ms"
BANNER_COLOR = 0xC62828
BANNER_HEIGHT = 96
BANNER_GLYPHS = "0123456789+ ALRMS"   # "<n> ALARM(S)" and "99+": the only text the banner composes
BANNER_COUNT_MAX = 99
BANNER_FLASH_INTERVAL = "500ms"
HEADER_CLOCK_X = -24      # clock label centre, relative to the screen's middle
HEADER_DOT_X = (13, 27, 41)   # SignalK, bridge, MQTT status dots (centres, same reference)
HEADER_DOT_SIZE = 10
HEADER_TITLE_ROW = 26     # title row offset below header_y (the narrower clock row is above it)
HEADER_TITLE_WIDTH = 200
PAGE_DOT_SIZE = 8
PAGE_DOT_GAP = 8
CLOCK_GLYPHS = "0123456789:-"
# Day and night palettes (RGB). Night keeps to the red/amber end of the spectrum so night vision
# survives; only an alarm tile's text is deliberately bright. The alarm banner is never themed.
DAY = {"tile": 0x101820, "name": 0x9AA5B1, "value": 0xFFFFFF, "clock": 0xFFFFFF, "title": 0x9AA5B1,
       "dot_ok": 0x1E7B34, "dot_bad": 0xC62828, "dot_unknown": 0x37474F, "page_hi": 0xFFFFFF,
       "page_lo": 0x37474F}
NIGHT = {"tile": 0x000000, "name": 0x6A1600, "value": 0xB02400, "clock": 0xA02000, "title": 0x6A1600,
         "dot_ok": 0x6B3000, "dot_bad": 0xB00000, "dot_unknown": 0x2A0A00, "page_hi": 0xA02000,
         "page_lo": 0x2A0A00}
NIGHT_SEV_COLORS = {0: (0x000000, 0x5A1200), 1: (0x2A0800, 0xC02800), 2: (0x7A3E00, 0xF0B060),
                    3: (0xB00000, 0xFFB090)}
# Bars and arcs (display dashboard slice S6). Indicator colours per state: 0 unknown/stale, 1 ok,
# 2 warn, 3 alarm, 4 fresh but the instrument has no thresholds (neither green nor a warning).
GAUGE_LAYOUTS = ("tank-bars", "gauge-arc")
GAUGE_DAY = {0: 0x37474F, 1: 0x1E7B34, 2: 0xF2A900, 3: 0xC62828, 4: 0x2E86C1}
GAUGE_NIGHT = {0: 0x2A0A00, 1: 0x6B3000, 2: 0xB06A00, 3: 0xC00000, 4: 0x8A4A00}
GAUGE_NEUTRAL = 4
TRACK_COLOR = {"day": 0x263238, "night": 0x1A0800}
BAR_WIDTH = 60            # at most 60 px, whatever the number of bars
BAR_HEIGHT = 210
BAR_RANGE = 100
BAR_PAD_ROW = 8
ARC_RANGE = 1000
ARC_WIDTH = 24
ARC_SWEEP = 270
ARC_MARGIN = 20           # gap between the arc and the screen edge
ZONE_WIDTH = 6
ZONE_GAP = 2              # between the arc track and the zone arcs, which sit just inside it
ARC_START_GAP_BOTTOM = 135   # LVGL: 0 deg = 3 o'clock, clockwise; 135 -> 45 leaves the gap at the bottom
ARC_START_GAP_TOP = 315      # with ui.header the 90 deg gap is at the top, where the header is
# Electrical page (display dashboard slice S7): one SoC ring, the battery voltage in the middle, and
# rows for the state of charge, the current (with a fixed CHG / DSG word), the shore power pill and solar.
ELECTRICAL = "electrical"
ELECTRICAL_ROLES = ("voltage", "soc", "current", "shore", "solar")
CURRENT_DEADBAND = 0.05      # amperes: inside +-0.05 A the word is empty
SHORE_ON_ABOVE = 0.5         # an instrument-backed shore pill reads ON above this value
CURRENT_WORDS = {1: "CHG", 2: "DSG", 0: ""}          # sign convention: SignalK current is positive while charging
SHORE_WORDS = {1: "ON", 2: "OFF", 0: BLANK_VALUE}    # instrument-backed pill: 0 is stale or unknown
SHORE_STATE_COLOR = {0: 0, 1: 1, 2: 4}               # pill state -> GAUGE palette entry (stale grey, on green, off blue)
ELEC_WIDTH = 300             # the text column, inside the 376 px free circle of the arc
ELEC_PAD_ROW = 2
ELEC_NAME_WIDTH = 88
ELEC_WORD_WIDTH = 48
ELEC_PILL_WIDTH = 92
ELEC_PILL_HEIGHT = 30
ELEC_VOLTAGE_ROW = 66        # line height of the 56 px bold digits
WARNING_ARCHIVE_SECRETS = ("WARNING: the archive contains the Wi-Fi password and the display's MQTT, "
                           "OTA and API secrets. Treat it like a password: move it over SSH only, "
                           "do not commit or e-mail it, delete it after flashing.")


class RenderError(Exception):
    """Operator-facing failure; ``code`` is the process exit status (1 or 2)."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def pinned_image() -> str:
    """The single source of truth for the pinned ESPHome image (displays/esphome/IMAGE)."""
    text = (ESPHOME_DIR / "IMAGE").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"ghcr\.io/esphome/esphome:[0-9.]+@sha256:[0-9a-f]{64}", text):
        raise RenderError(f"displays/esphome/IMAGE is malformed: {text!r}")
    return text


def esphome_version() -> str:
    return pinned_image().split(":", 1)[1].split("@", 1)[0]


# --- YAML text helpers ---------------------------------------------------------

def yq(text: str) -> str:
    """A YAML double-quoted scalar; everything outside printable ASCII becomes \\u escapes."""
    out = []
    for ch in text:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif 0x20 <= code <= 0x7E:
            out.append(ch)
        elif code <= 0xFFFF:
            out.append(f"\\u{code:04X}")
        else:
            out.append(f"\\U{code:08X}")
    return '"' + "".join(out) + '"'


def lit(text: str) -> str:
    """User text that ESPHome must not substitute (``$x``, ``${..}``, ``<% %>``)."""
    return "!literal " + yq(text)


# --- font coverage (TrueType cmap) -----------------------------------------------

_COVERAGE: dict = {}


def _cmap_codepoints(data: bytes) -> set:
    count = struct.unpack_from(">H", data, 4)[0]
    cmap = None
    for i in range(count):
        tag, _csum, offset, _length = struct.unpack_from(">4sIII", data, 12 + 16 * i)
        if tag == b"cmap":
            cmap = offset
    if cmap is None:
        raise RenderError("font has no cmap table")
    points: set = set()
    n_tables = struct.unpack_from(">H", data, cmap + 2)[0]
    for i in range(n_tables):
        platform, encoding, off = struct.unpack_from(">HHI", data, cmap + 4 + 8 * i)
        if (platform, encoding) not in ((3, 1), (3, 10), (0, 3), (0, 4)):
            continue
        base = cmap + off
        fmt = struct.unpack_from(">H", data, base)[0]
        if fmt == 4:
            segx2 = struct.unpack_from(">H", data, base + 6)[0]
            segs = segx2 // 2
            ends = struct.unpack_from(f">{segs}H", data, base + 14)
            starts = struct.unpack_from(f">{segs}H", data, base + 16 + segx2)
            deltas = struct.unpack_from(f">{segs}h", data, base + 16 + 2 * segx2)
            range_pos = base + 16 + 3 * segx2
            ranges = struct.unpack_from(f">{segs}H", data, range_pos)
            for s in range(segs):
                for code in range(starts[s], ends[s] + 1):
                    if code == 0xFFFF:
                        continue
                    if ranges[s] == 0:
                        gid = (code + deltas[s]) & 0xFFFF
                    else:
                        pos = range_pos + 2 * s + ranges[s] + 2 * (code - starts[s])
                        if pos + 2 > len(data):
                            continue
                        gid = struct.unpack_from(">H", data, pos)[0]
                        if gid:
                            gid = (gid + deltas[s]) & 0xFFFF
                    if gid:
                        points.add(code)
        elif fmt == 12:
            groups = struct.unpack_from(">I", data, base + 12)[0]
            for g in range(groups):
                start, end, gid = struct.unpack_from(">III", data, base + 16 + 12 * g)
                for code in range(start, end + 1):
                    if gid + (code - start):
                        points.add(code)
    return points


def font_coverage(name: str) -> set:
    if name not in _COVERAGE:
        _COVERAGE[name] = _cmap_codepoints((FONTS_DIR / FONT_FILES[name]).read_bytes())
    return _COVERAGE[name]


def unsupported_chars(text: str) -> list:
    covered = font_coverage("regular")
    return sorted({ch for ch in text if ord(ch) not in covered})


def _glyph_errors(where: str, text: str) -> list:
    bad = unsupported_chars(text)
    if not bad:
        return []
    shown = ", ".join(f"{ch!r} (U+{ord(ch):04X})" for ch in bad)
    return [f"{where}: the bundled font (Roboto Condensed) has no glyph for {shown}"]


def check_glyphs(config: dict) -> list:
    """Errors for labels and units the bundled display font cannot draw."""
    errors: list = []
    for iid, inst in config.get("instruments", {}).items():
        for key in ("label", "unit"):
            value = inst.get(key)
            if isinstance(value, str):
                errors.extend(_glyph_errors(f"$.instruments.{iid}.{key}", value))
    for nid, note in config.get("notifications", {}).items():
        value = note.get("label")
        if isinstance(value, str):
            errors.extend(_glyph_errors(f"$.notifications.{nid}.label", value))
    for did, display in config.get("displays", {}).items():
        if header_enabled(display):
            for pi, page in enumerate(display["pages"]):
                errors.extend(_glyph_errors(f"$.displays.{did}.pages[{pi}].title", page["title"]))
    return errors


# --- device file -----------------------------------------------------------------

def firmware_name(display_id: str) -> str:
    if display_id.endswith("-"):
        raise RenderError(f"display id {display_id!r} ends with a hyphen, which ESPHome rejects "
                          "in device names; rename the display", 2)
    return "vs-" + display_id


def used_instruments(display: dict) -> list:
    seen: list = []
    for page in display["pages"]:
        for iid in page["items"]:
            if iid not in seen:
                seen.append(iid)
    return seen


def stale_threshold_s(inst: dict, heartbeat_s: int) -> int:
    """Blank a tile after this many seconds without any message for the instrument."""
    return inst["stale_after_s"] + 2 * heartbeat_s


def is_notification(config: dict, iid: str) -> bool:
    return iid in config.get("notifications", {}) and iid not in config["instruments"]


def item_limit_ms(config: dict, iid: str, heartbeat_s: int) -> int:
    """How long without a message before an item's tile is blanked (milliseconds)."""
    if is_notification(config, iid):
        return 3 * heartbeat_s * 1000
    return stale_threshold_s(config["instruments"][iid], heartbeat_s) * 1000


def _ui(display: dict) -> dict:
    return display.get("ui", {})


def swipe_enabled(display: dict) -> bool:
    return _ui(display).get("swipe", False) is True


def header_enabled(display: dict) -> bool:
    return _ui(display).get("header", False) is True


def night_mode(display: dict) -> str:
    return _ui(display).get("night", "off")


def night_enabled(display: dict) -> bool:
    return night_mode(display) != "off"


def dim_after_s(display: dict) -> int:
    return _ui(display).get("dim_after_s", 0)


def dim_enabled(display: dict) -> bool:
    return dim_after_s(display) > 0


def power_enabled(display: dict) -> bool:
    """A backlight controller is generated only when a brightness, night or dim key asks for it."""
    return night_enabled(display) or dim_enabled(display) or "day_brightness" in _ui(display)


def day_brightness(display: dict) -> int:
    return _ui(display).get("day_brightness", schema.DEFAULT_DAY_BRIGHTNESS)


def night_brightness(display: dict) -> int:
    return _ui(display).get("night_brightness", schema.DEFAULT_NIGHT_BRIGHTNESS)


def dim_brightness(display: dict) -> int:
    """Idle dimming goes to night_brightness when the display sets it, else to 10 %."""
    return _ui(display).get("night_brightness", schema.DIM_BRIGHTNESS)


def clock_enabled(display: dict) -> bool:
    return header_enabled(display) or night_mode(display) == "schedule"


def touch_managed(display: dict) -> bool:
    """Swipe, night long-press and idle dimming need the gesture-aware tap layer."""
    return swipe_enabled(display) or night_enabled(display) or dim_enabled(display)


def touch_layer_enabled(display: dict) -> bool:
    return len(display["pages"]) > 1 or night_enabled(display)


def _small_glyphs(config: dict, display: dict) -> str:
    chars = {" "}
    if header_enabled(display):
        chars.update(CLOCK_GLYPHS)
        for page in display["pages"]:
            chars.update(page["title"])
    if any(page["layout"] == ELECTRICAL for page in display["pages"]):   # values and fixed words in font_small
        chars.update(VALUE_GLYPHS)
        for words in (CURRENT_WORDS, SHORE_WORDS, STATUS_WORDS):
            chars.update("".join(words.values()))
    for iid in used_instruments(display):
        if is_notification(config, iid):
            chars.update(config["notifications"][iid]["label"])
            continue
        inst = config["instruments"][iid]
        chars.update(inst["label"])
        chars.update(inst["unit"])
    return "".join(sorted(chars))


def _label_id(pi: int, iid: str, part: str, themed: bool) -> str:
    """``id: pN_ID_name, `` for the static labels, which only the night palette needs to reach."""
    return f"id: p{pi}_{iid}_{part}, " if themed else ""


def _tile_widgets(ind: str, pi: int, iid: str, inst: dict, value_font: str, themed: bool = False) -> list:
    return [
        f"{ind}- label: {{{_label_id(pi, iid, 'name', themed)}text: {lit(inst['label'])}, "
        "text_font: font_small, text_color: 0x9AA5B1}",
        f"{ind}- label: {{id: p{pi}_{iid}_value, text: {yq(BLANK_VALUE)}, text_font: {value_font}, "
        "text_color: 0xFFFFFF}",
        f"{ind}- label: {{{_label_id(pi, iid, 'unit', themed)}text: {lit(inst['unit'])}, "
        "text_font: font_small, text_color: 0x9AA5B1}",
    ]


# Severity palette: sev number -> (tile background, value text colour). 0 is unknown/stale.
SEV_COLORS = {0: (0x37474F, 0x9AA5B1), 1: (0x1E7B34, 0xFFFFFF), 2: (0xF2A900, 0x000000),
              3: (0xC62828, 0xFFFFFF)}


def has_thresholds(inst: dict) -> bool:
    return "thresholds" in inst


def tile_id(pi: int, iid: str, layout: str) -> str:
    return f"p{pi}_content" if layout == "single" else f"p{pi}_{iid}_tile"


FLEX_COLUMN = "layout: {type: FLEX, flex_flow: COLUMN, flex_align_main: CENTER, flex_align_cross: CENTER}"


def status_grid_shape(count: int) -> tuple:
    """(columns, rows) of the status-grid for this many items."""
    if count <= 4:
        return 2, 2
    return (3, 2) if count <= 6 else (3, 3)


def status_cell_size(box_width: int, box_height: int, count: int) -> tuple:
    """(width, height) in pixels of one status-grid cell (gaps of STATUS_GAP between cells)."""
    cols, rows = status_grid_shape(count)
    return ((box_width - STATUS_GAP * (cols - 1)) // cols, (box_height - STATUS_GAP * (rows - 1)) // rows)


def status_font(count: int) -> str:
    """Large words in the 2-column grid, smaller ones where three cells share the width."""
    return STATUS_FONTS["lg" if status_grid_shape(count)[0] == 2 else "sm"]


def _status_glyphs(config: dict, display: dict) -> dict:
    """font id -> sorted glyph string, for the status-grid fonts the display actually needs."""
    glyphs: dict = {}
    for page in display["pages"]:
        if page["layout"] != "status-grid":
            continue
        chars = glyphs.setdefault(status_font(len(page["items"])), set())
        for iid in page["items"]:
            chars.update("".join(STATUS_WORDS.values()) if is_notification(config, iid) else VALUE_GLYPHS)
    return {fid: "".join(sorted(chars)) for fid, chars in glyphs.items()}


def _status_cell(config: dict, ind: str, pi: int, iid: str, font: str, index: int, cols: int,
                 themed: bool = False) -> list:
    """One status-grid cell: static label, then the state word (notification) or value + unit."""
    notification = is_notification(config, iid)
    lines = [f"{ind}- obj:"]
    if notification or has_thresholds(config["instruments"][iid]) or themed:
        lines.append(f"{ind}    id: {tile_id(pi, iid, 'status-grid')}")
    lines += [
        f"{ind}    styles: tile_style",
        f"{ind}    grid_cell_row_pos: {index // cols}",
        f"{ind}    grid_cell_column_pos: {index % cols}",
        f"{ind}    grid_cell_x_align: STRETCH",
        f"{ind}    grid_cell_y_align: STRETCH",
        f"{ind}    scrollable: false",
        f"{ind}    {FLEX_COLUMN}",
        f"{ind}    widgets:",
    ]
    inner = ind + "      "
    fit = "width: 100%, text_align: CENTER"
    label = config["notifications"][iid]["label"] if notification else config["instruments"][iid]["label"]
    lines.append(f"{inner}- label: {{{_label_id(pi, iid, 'name', themed)}text: {lit(label)}, "
                 f"text_font: font_small, text_color: 0x9AA5B1, {fit}, long_mode: DOT}}")
    lines.append(f"{inner}- label: {{id: p{pi}_{iid}_value, text: {yq(BLANK_VALUE)}, text_font: {font}, "
                 f"text_color: 0xFFFFFF, {fit}, long_mode: CLIP}}")
    if not notification:
        lines.append(f"{inner}- label: {{{_label_id(pi, iid, 'unit', themed)}"
                     f"text: {lit(config['instruments'][iid]['unit'])}, "
                     f"text_font: font_small, text_color: 0x9AA5B1, {fit}, long_mode: DOT}}")
    return lines


# --- bars and arcs (tank-bars, gauge-arc) ------------------------------------------

def electrical_slots(page: dict) -> dict:
    """role -> item id for an electrical page (items fill the slots in order)."""
    return dict(zip(ELECTRICAL_ROLES, page["items"]))


def electrical_roles(display: dict, iid: str) -> list:
    """(page index, role) for every electrical page that shows this item."""
    return [(pi, role) for pi, page in enumerate(display["pages"]) if page["layout"] == ELECTRICAL
            for role, item in electrical_slots(page).items() if item == iid]


def gauge_pages(display: dict, iid: str) -> list:
    """(page index, page) of the bar and arc pages that show this item (the SoC on an electrical page)."""
    return [(pi, page) for pi, page in enumerate(display["pages"])
            if (page["layout"] in GAUGE_LAYOUTS and iid in page["items"])
            or (page["layout"] == ELECTRICAL and electrical_slots(page).get("soc") == iid)]


def gauge_kind(page: dict) -> str:
    """Which scaling a page needs: an electrical page scales its SoC like a gauge-arc."""
    return "gauge-arc" if page["layout"] == ELECTRICAL else page["layout"]


def shore_instrument(config: dict, display: dict, iid: str) -> bool:
    """True when an instrument (not a notification) fills the shore slot of an electrical page."""
    return not is_notification(config, iid) and any(role == "shore" for _, role in electrical_roles(display, iid))


def keeps_number(config: dict, display: dict, iid: str) -> bool:
    """True when the firmware needs the raw number of this instrument, not only its text."""
    return on_gauge_page(display, iid) or any(
        role == "current" or (role == "shore" and not is_notification(config, iid))
        for _, role in electrical_roles(display, iid))


def on_gauge_page(display: dict, iid: str) -> bool:
    return bool(gauge_pages(display, iid))


def on_tile_page(display: dict, iid: str) -> bool:
    return any(page["layout"] not in GAUGE_LAYOUTS and page["layout"] != ELECTRICAL and iid in page["items"]
               for page in display["pages"])


def severity_tile(config: dict, display: dict, iid: str) -> bool:
    """True when a tile (not a bar or an arc) is coloured by the item's severity."""
    if is_notification(config, iid):
        return True
    return has_thresholds(config["instruments"][iid]) and on_tile_page(display, iid)


def cfloat(value) -> str:
    """A C++ float literal for a finite number from the (validated) configuration."""
    return repr(float(value)) + "f"


def _clamp(value: float, lo: float, hi: float) -> float:
    return lo if not value > lo else (hi if value > hi else value)   # NaN becomes lo


def bar_percent(value: float, lo: float, hi: float) -> int:
    """0..100 for a reading between lo and hi; the firmware does the same with lroundf."""
    scaled = _clamp(BAR_RANGE * (value - lo) / (hi - lo), 0.0, float(BAR_RANGE))
    return int(scaled + 0.5)


def arc_permille(value: float, lo: float, hi: float) -> int:
    """0..1000 for a reading between lo and hi."""
    scaled = _clamp(ARC_RANGE * (value - lo) / (hi - lo), 0.0, float(ARC_RANGE))
    return int(scaled + 0.5)


def bar_column_width(box_width: int, count: int) -> int:
    return box_width // count


def gauge_diameter(board: dict) -> int:
    """The arc fills the round screen, ARC_MARGIN px from the edge."""
    return min(board["width"], board["height"]) - 2 * ARC_MARGIN


def arc_start_angle(display: dict) -> int:
    """LVGL angles: 0 = 3 o'clock, clockwise. With the header the 90 deg gap sits at the top."""
    return ARC_START_GAP_TOP if header_enabled(display) else ARC_START_GAP_BOTTOM


def arc_end_angle(start: int) -> int:
    return (start + ARC_SWEEP) % 360


def arc_angle(start: int, value: float, lo: float, hi: float) -> int:
    """Angle (whole degrees) of a reading on the arc that starts at ``start``."""
    fraction = _clamp((value - lo) / (hi - lo), 0.0, 1.0)
    return (start + int(ARC_SWEEP * fraction + 0.5)) % 360


def zone_arcs(inst: dict, start: int) -> list:
    """Static warn and alarm zones as (severity number, start angle, end angle), clockwise.

    Built from the thresholds of the instrument and its min/max; zones outside the range or
    shorter than a degree are dropped.
    """
    th, lo, hi = inst.get("thresholds", {}), inst["min"], inst["max"]
    spans = []
    if "alarm_below" in th:
        spans.append((3, lo, th["alarm_below"]))
    if "warn_below" in th:
        spans.append((2, th.get("alarm_below", lo), th["warn_below"]))
    if "warn_above" in th:
        spans.append((2, th["warn_above"], th.get("alarm_above", hi)))
    if "alarm_above" in th:
        spans.append((3, th["alarm_above"], hi))
    zones = []
    for severity, a, b in spans:
        a, b = _clamp(a, lo, hi), _clamp(b, lo, hi)
        first, last = arc_angle(start, a, lo, hi), arc_angle(start, b, lo, hi)
        if b > a and (last - first) % 360 > 0:
            zones.append((severity, first, last))
    return zones


def arc_zone_diameter(diameter: int) -> int:
    return diameter - 2 * (ARC_WIDTH + ZONE_GAP)


def arc_text_box(diameter: int) -> tuple:
    """(width, height) of the label, value and unit column in the middle of the arc."""
    inner = arc_zone_diameter(diameter) // 2 - ZONE_WIDTH
    side = int(inner * 1.4142) // 2 * 2
    return side, side * 4 // 5


BAR_COLUMN = (f"layout: {{type: FLEX, flex_flow: COLUMN, flex_align_main: CENTER, "
              f"flex_align_cross: CENTER, pad_row: {BAR_PAD_ROW}}}")
BOX_RESET = ["bg_opa: TRANSP", "border_width: 0", "pad_all: 0", "scrollable: false"]


def _bar_column(ind: str, pi: int, iid: str, inst: dict, width: int, themed: bool) -> list:
    fit = "width: 100%, text_align: CENTER"
    inner = ind + "      "
    lines = [f"{ind}- obj:", f"{ind}    width: {width}", f"{ind}    height: 100%"]
    lines += [f"{ind}    {line}" for line in BOX_RESET]
    lines += [f"{ind}    {BAR_COLUMN}", f"{ind}    widgets:"]
    lines.append(f"{inner}- label: {{{_label_id(pi, iid, 'name', themed)}text: {lit(inst['label'])}, "
                 f"text_font: font_small, text_color: 0x{DAY['name']:06X}, {fit}, long_mode: DOT}}")
    lines += [
        f"{inner}- bar:", f"{inner}    id: p{pi}_{iid}_bar", f"{inner}    width: {BAR_WIDTH}",
        f"{inner}    height: {BAR_HEIGHT}", f"{inner}    min_value: 0", f"{inner}    max_value: {BAR_RANGE}",
        f"{inner}    value: 0", f"{inner}    mode: NORMAL", f"{inner}    bg_color: 0x{TRACK_COLOR['day']:06X}",
        f"{inner}    bg_opa: COVER", f"{inner}    border_width: 0", f"{inner}    radius: 8",
        f"{inner}    indicator:", f"{inner}      bg_color: 0x{GAUGE_DAY[0]:06X}", f"{inner}      bg_opa: COVER",
        f"{inner}      radius: 8",
        f"{inner}- obj:", f"{inner}    width: 100%", f"{inner}    height: SIZE_CONTENT",
    ]
    lines += [f"{inner}    {line}" for line in BOX_RESET]
    lines += [
        f"{inner}    layout: {{type: FLEX, flex_flow: ROW, flex_align_main: CENTER, "
        "flex_align_cross: END, pad_column: 2}",
        f"{inner}    widgets:",
        f"{inner}      - label: {{id: p{pi}_{iid}_value, text: {yq(BLANK_VALUE)}, text_font: font_value_bar, "
        f"text_color: 0x{DAY['value']:06X}, long_mode: CLIP}}",
        f"{inner}      - label: {{{_label_id(pi, iid, 'unit', themed)}text: {lit(inst['unit'])}, "
        f"text_font: font_small, text_color: 0x{DAY['name']:06X}, long_mode: CLIP}}",
    ]
    return lines


def _tank_bars_page(config: dict, board: dict, pi: int, page: dict, themed: bool) -> list:
    cw, ch = board["content_width"], board["content_height"]
    width = bar_column_width(cw, len(page["items"]))
    lines = [
        "        - obj:", f"            id: p{pi}_content", "            align: CENTER",
        f"            width: {cw}", f"            height: {ch}",
        *[f"            {line}" for line in BOX_RESET],
        "            layout: {type: FLEX, flex_flow: ROW, flex_align_main: SPACE_EVENLY, "
        "flex_align_cross: CENTER}",
        "            widgets:",
    ]
    for iid in page["items"]:
        lines += _bar_column("              ", pi, iid, config["instruments"][iid], width, themed)
    return lines


def _arc_style(width: int, color: int) -> list:
    return [f"arc_width: {width}", f"arc_color: 0x{color:06X}", "arc_rounded: false"]


def _arc_widgets(board: dict, display: dict, pi: int, inst: dict) -> list:
    """The track arc and the static warn and alarm zone arcs of a page (gauge-arc and electrical)."""
    diameter = gauge_diameter(board)
    start = arc_start_angle(display)
    track = f"0x{TRACK_COLOR['day']:06X}"
    common = ["align: CENTER", "clickable: false", "scrollable: false", "min_value: 0",
              f"max_value: {ARC_RANGE}", "value: 0", f"start_angle: {start}", f"end_angle: {arc_end_angle(start)}",
              "rotation: 0", "adjustable: false", "mode: NORMAL", "bg_opa: TRANSP", "border_width: 0",
              "pad_all: 0"]
    lines = ["        - arc:", f"            id: p{pi}_arc", f"            width: {diameter}",
             f"            height: {diameter}"]
    lines += [f"            {line}" for line in common]
    lines += [f"            arc_width: {ARC_WIDTH}", f"            arc_color: {track}", "            arc_rounded: false",
              "            indicator:"]
    lines += [f"              {line}" for line in _arc_style(ARC_WIDTH, GAUGE_DAY[0])]
    lines += ["            knob:", "              bg_opa: TRANSP", "              pad_all: 0"]
    zone_d = arc_zone_diameter(diameter)
    for k, (severity, first, last) in enumerate(zone_arcs(inst, start)):
        lines += ["        - arc:", f"            id: p{pi}_zone{k}", f"            width: {zone_d}",
                  f"            height: {zone_d}"]
        lines += [f"            {line}" for line in common if not line.startswith(("start_angle", "end_angle"))]
        lines += [f"            start_angle: {first}", f"            end_angle: {last}",
                  *[f"            {line}" for line in _arc_style(ZONE_WIDTH, GAUGE_DAY[severity])],
                  "            indicator:", "              arc_opa: TRANSP", "            knob:",
                  "              bg_opa: TRANSP", "              pad_all: 0"]
    return lines


def _gauge_arc_page(config: dict, board: dict, display: dict, pi: int, page: dict, themed: bool) -> list:
    iid = page["items"][0]
    inst = config["instruments"][iid]
    lines = _arc_widgets(board, display, pi, inst)
    box_w, box_h = arc_text_box(gauge_diameter(board))
    fit = "width: 100%, text_align: CENTER"
    lines += [
        "        - obj:", f"            id: p{pi}_content", "            align: CENTER", f"            width: {box_w}",
        f"            height: {box_h}", *[f"            {line}" for line in BOX_RESET], f"            {FLEX_COLUMN}",
        "            widgets:",
        f"              - label: {{{_label_id(pi, iid, 'name', themed)}text: {lit(inst['label'])}, "
        f"text_font: font_small, text_color: 0x{DAY['name']:06X}, {fit}, long_mode: DOT}}",
        f"              - label: {{id: p{pi}_{iid}_value, text: {yq(BLANK_VALUE)}, text_font: font_value_single, "
        f"text_color: 0x{DAY['value']:06X}, {fit}, long_mode: CLIP}}",
        f"              - label: {{{_label_id(pi, iid, 'unit', themed)}text: {lit(inst['unit'])}, "
        f"text_font: font_small, text_color: 0x{DAY['name']:06X}, {fit}, long_mode: DOT}}",
    ]
    return lines


def _gauge_lines(config: dict, display: dict, iid: str, heartbeat: int, night: bool) -> list:
    """Bar level, arc angle and indicator colour for one instrument; colours only on change.

    Not fresh means level 0 and the grey indicator; the text is "--" through render_tiles.
    """
    inst = config["instruments"][iid]
    pages = gauge_pages(display, iid)
    lo, hi = cfloat(inst["min"]), cfloat(inst["max"])
    limit_ms = item_limit_ms(config, iid, heartbeat)
    kinds = {gauge_kind(page) for _, page in pages}
    lines = [
        "      - lambda: |-",
        "          bool fresh = id(bridge_online) && mqtt::global_mqtt_client->is_connected()"
        f" && id(ins_{iid}_ok) && (millis() - id(ins_{iid}_seen)) < {limit_ms}UL;",
        f"          float n = id(ins_{iid}_num);",
    ]
    for layout, scale, var in (("tank-bars", BAR_RANGE, "pb"), ("gauge-arc", ARC_RANGE, "pa")):
        if layout not in kinds:
            continue
        lines += [f"          float {var} = fresh ? {scale}.0f * (n - {lo}) / ({hi} - {lo}) : 0.0f;",
                  f"          if (!({var} > 0.0f)) {var} = 0.0f;",
                  f"          if ({var} > {scale}.0f) {var} = {scale}.0f;"]
        for pi, page in pages:
            if gauge_kind(page) == layout:
                setter = (f"lv_bar_set_value(id(p{pi}_{iid}_bar), lroundf({var}), LV_ANIM_OFF);"
                          if layout == "tank-bars" else f"lv_arc_set_value(id(p{pi}_arc), lroundf({var}));")
                lines.append(f"          {setter}")
    state = f"id(ins_{iid}_sev)" if has_thresholds(inst) else str(GAUGE_NEUTRAL)
    lines += [
        f"          uint8_t g = fresh ? {state} : 0;",
        f"          if (g != id(ins_{iid}_gdrawn)) {{",
        f"            id(ins_{iid}_gdrawn) = g;",
        f"            uint32_t c = 0x{GAUGE_DAY[0]:06X};",
    ]
    for number in (1, 2, 3, GAUGE_NEUTRAL):
        lines.append(f"            if (g == {number}) c = 0x{GAUGE_DAY[number]:06X};")
    if night:
        lines.append("            if (id(night_active)) {")
        lines.append(f"              c = 0x{GAUGE_NIGHT[0]:06X};")
        for number in (1, 2, 3, GAUGE_NEUTRAL):
            lines.append(f"              if (g == {number}) c = 0x{GAUGE_NIGHT[number]:06X};")
        lines.append("            }")
    for pi, page in pages:
        if page["layout"] == "tank-bars":
            lines.append(f"            lv_obj_set_style_bg_color(id(p{pi}_{iid}_bar), lv_color_hex(c), "
                         "LV_PART_INDICATOR);")
        else:
            lines.append(f"            lv_obj_set_style_arc_color(id(p{pi}_arc), lv_color_hex(c), "
                         "LV_PART_INDICATOR);")
    lines.append("          }")
    return lines


def _gauge_night_lines(config: dict, display: dict, pi: int, page: dict) -> list:
    """Night palette for the text, the tracks and the zone arcs of one bar or arc page."""
    lines = []
    for iid in page["items"]:
        for part, key in (("name", "name"), ("value", "value"), ("unit", "name")):
            lines.append(f"            lv_obj_set_style_text_color(id(p{pi}_{iid}_{part}), lv_color_hex("
                         f"n ? 0x{NIGHT[key]:06X} : 0x{DAY[key]:06X}), LV_PART_MAIN);")
        if page["layout"] == "tank-bars":
            track = f"n ? 0x{TRACK_COLOR['night']:06X} : 0x{TRACK_COLOR['day']:06X}"
            lines.append(f"            lv_obj_set_style_bg_color(id(p{pi}_{iid}_bar), lv_color_hex({track}), "
                         "LV_PART_MAIN);")
            continue
        lines += _arc_night_lines(config, display, pi, iid)
    return lines


def _arc_night_lines(config: dict, display: dict, pi: int, iid: str) -> list:
    """Night palette for the track and the zone arcs of the arc that shows this instrument."""
    track = f"n ? 0x{TRACK_COLOR['night']:06X} : 0x{TRACK_COLOR['day']:06X}"
    lines = [f"            lv_obj_set_style_arc_color(id(p{pi}_arc), lv_color_hex({track}), LV_PART_MAIN);"]
    start = arc_start_angle(display)
    for k, (severity, _, _) in enumerate(zone_arcs(config["instruments"][iid], start)):
        lines.append(f"            lv_obj_set_style_arc_color(id(p{pi}_zone{k}), lv_color_hex("
                     f"n ? 0x{GAUGE_NIGHT[severity]:06X} : 0x{GAUGE_DAY[severity]:06X}), LV_PART_MAIN);")
    return lines


def _text_night(pi: int, iid: str, part: str, key: str) -> str:
    return (f"            lv_obj_set_style_text_color(id(p{pi}_{iid}_{part}), lv_color_hex("
            f"n ? 0x{NIGHT[key]:06X} : 0x{DAY[key]:06X}), LV_PART_MAIN);")


def _electrical_night_lines(config: dict, display: dict, pi: int, page: dict) -> list:
    """Night palette for the text, the SoC ring and its zones of one electrical page.

    The shore pill background and a notification pill's text are repainted by the guarded
    severity and pill colour code, which the night script resets to run again.
    """
    lines = []
    for role, iid in electrical_slots(page).items():
        lines.append(_text_night(pi, iid, "name", "name"))
        if is_notification(config, iid):
            continue
        lines.append(_text_night(pi, iid, "value", "value"))
        if role != "shore":
            lines.append(_text_night(pi, iid, "unit", "name"))
        if role == "current":
            lines.append(_text_night(pi, iid, "word", "value"))
    lines += _arc_night_lines(config, display, pi, electrical_slots(page)["soc"])
    return lines


def electrical_stack_height(page: dict) -> int:
    """Height in pixels of the electrical text column: name, voltage, SoC, current, then optional rows."""
    small = FONT_SIZES["small"] + 3          # one 20 px text line
    rows = [small, ELEC_VOLTAGE_ROW, small, small]
    if "shore" in electrical_slots(page):
        rows.append(ELEC_PILL_HEIGHT)
    if "solar" in electrical_slots(page):
        rows.append(small)
    return sum(rows) + ELEC_PAD_ROW * (len(rows) - 1)


def _elec_row(ind: str, pi: int, iid: str, inst: dict, themed: bool, word: bool = False) -> list:
    """name, value, unit (and a fixed-width state word) on one line."""
    lines = [f"{ind}- obj:", f"{ind}    width: 100%", f"{ind}    height: SIZE_CONTENT"]
    lines += [f"{ind}    {line}" for line in BOX_RESET]
    lines += [f"{ind}    layout: {{type: FLEX, flex_flow: ROW, flex_align_main: CENTER, flex_align_cross: CENTER, "
              "pad_column: 6}", f"{ind}    widgets:"]
    inner = ind + "      "
    lines += [
        f"{inner}- label: {{{_label_id(pi, iid, 'name', themed)}text: {lit(inst['label'])}, text_font: font_small, "
        f"text_color: 0x{DAY['name']:06X}, width: {ELEC_NAME_WIDTH}, text_align: RIGHT, long_mode: DOT}}",
        f"{inner}- label: {{id: p{pi}_{iid}_value, text: {yq(BLANK_VALUE)}, text_font: font_small, "
        f"text_color: 0x{DAY['value']:06X}, long_mode: CLIP}}",
        f"{inner}- label: {{{_label_id(pi, iid, 'unit', themed)}text: {lit(inst['unit'])}, text_font: font_small, "
        f"text_color: 0x{DAY['name']:06X}, long_mode: CLIP}}",
    ]
    if word:
        lines.append(f"{inner}- label: {{id: p{pi}_{iid}_word, text: {lit('')}, text_font: font_small, "
                     f"text_color: 0x{DAY['value']:06X}, width: {ELEC_WORD_WIDTH}, text_align: LEFT, "
                     "long_mode: CLIP}")
    return lines


def _elec_shore_row(ind: str, config: dict, pi: int, iid: str, themed: bool) -> list:
    """The shore power pill: the item's label, then a rounded box holding a fixed word."""
    notification = is_notification(config, iid)
    label = config["notifications"][iid]["label"] if notification else config["instruments"][iid]["label"]
    grey, grey_text = SEV_COLORS[0] if notification else (GAUGE_DAY[0], DAY["value"])
    lines = [f"{ind}- obj:", f"{ind}    width: 100%", f"{ind}    height: SIZE_CONTENT"]
    lines += [f"{ind}    {line}" for line in BOX_RESET]
    lines += [f"{ind}    layout: {{type: FLEX, flex_flow: ROW, flex_align_main: CENTER, flex_align_cross: CENTER, "
              "pad_column: 6}", f"{ind}    widgets:"]
    inner = ind + "      "
    lines += [
        f"{inner}- label: {{{_label_id(pi, iid, 'name', themed)}text: {lit(label)}, text_font: font_small, "
        f"text_color: 0x{DAY['name']:06X}, width: {ELEC_NAME_WIDTH}, text_align: RIGHT, long_mode: DOT}}",
        f"{inner}- obj:", f"{inner}    id: {tile_id(pi, iid, ELECTRICAL)}", f"{inner}    width: {ELEC_PILL_WIDTH}",
        f"{inner}    height: {ELEC_PILL_HEIGHT}", f"{inner}    radius: {ELEC_PILL_HEIGHT // 2}",
        f"{inner}    bg_color: 0x{grey:06X}", f"{inner}    bg_opa: COVER", f"{inner}    border_width: 0",
        f"{inner}    pad_all: 0", f"{inner}    scrollable: false", f"{inner}    {FLEX_COLUMN}",
        f"{inner}    widgets:",
        f"{inner}      - label: {{id: p{pi}_{iid}_value, text: {yq(BLANK_VALUE)}, text_font: font_small, "
        f"text_color: 0x{grey_text:06X}, long_mode: CLIP}}",
    ]
    return lines


def _electrical_page(config: dict, board: dict, display: dict, pi: int, page: dict, themed: bool) -> list:
    """SoC ring (the gauge-arc ring), voltage in the middle, SoC and current rows, shore pill, solar row."""
    slots = electrical_slots(page)
    inst = config["instruments"]
    volt = inst[slots["voltage"]]
    lines = _arc_widgets(board, display, pi, inst[slots["soc"]])
    fit = "width: 100%, text_align: CENTER"
    lines += [
        "        - obj:", f"            id: p{pi}_content", "            align: CENTER", f"            width: {ELEC_WIDTH}",
        "            height: SIZE_CONTENT", *[f"            {line}" for line in BOX_RESET],
        f"            layout: {{type: FLEX, flex_flow: COLUMN, flex_align_main: CENTER, flex_align_cross: CENTER, "
        f"pad_row: {ELEC_PAD_ROW}}}",
        "            widgets:",
        f"              - label: {{{_label_id(pi, slots['voltage'], 'name', themed)}text: {lit(volt['label'])}, "
        f"text_font: font_small, text_color: 0x{DAY['name']:06X}, {fit}, long_mode: DOT}}",
        "              - obj:", "                  width: 100%", "                  height: SIZE_CONTENT",
        *[f"                  {line}" for line in BOX_RESET],
        "                  layout: {type: FLEX, flex_flow: ROW, flex_align_main: CENTER, flex_align_cross: END, "
        "pad_column: 4}",
        "                  widgets:",
        f"                    - label: {{id: p{pi}_{slots['voltage']}_value, text: {yq(BLANK_VALUE)}, "
        f"text_font: font_value_grid, text_color: 0x{DAY['value']:06X}, long_mode: CLIP}}",
        f"                    - label: {{{_label_id(pi, slots['voltage'], 'unit', themed)}text: {lit(volt['unit'])}, "
        f"text_font: font_small, text_color: 0x{DAY['name']:06X}, long_mode: CLIP}}",
    ]
    lines += _elec_row("              ", pi, slots["soc"], inst[slots["soc"]], themed)
    lines += _elec_row("              ", pi, slots["current"], inst[slots["current"]], themed, word=True)
    if "shore" in slots:
        lines += _elec_shore_row("              ", config, pi, slots["shore"], themed)
    if "solar" in slots:
        lines += _elec_row("              ", pi, slots["solar"], inst[slots["solar"]], themed)
    return lines


def _fresh_expr(config: dict, iid: str, heartbeat: int) -> str:
    return (f"id(bridge_online) && mqtt::global_mqtt_client->is_connected() && id(ins_{iid}_ok)"
            f" && (millis() - id(ins_{iid}_seen)) < {item_limit_ms(config, iid, heartbeat)}UL")


def _state_branches(pages: list, iid: str, part: str, state: str, words: dict) -> list:
    """One if/then per state: a fixed word (no runtime strings) on every page, from one gated state."""
    lines = []
    for pi in pages:
        for number, word in words.items():
            lines += [
                "      - if:", "          condition:", f"            lambda: 'return ({state}) == {number};'",
                "          then:", "            - lvgl.label.update:", f"                id: p{pi}_{iid}_{part}",
                f"                text: {lit(word)}",
            ]
    return lines


def _electrical_lines(config: dict, display: dict, iid: str, heartbeat: int, night: bool) -> list:
    """CHG / DSG for the current and the ON / OFF pill for an instrument-backed shore slot.

    Both read a state gated on freshness, so a stale or unknown reading gives an empty word and
    the grey "--" pill, never the last direction or a green ON.
    """
    roles = electrical_roles(display, iid)
    fresh = _fresh_expr(config, iid, heartbeat)
    lines = []
    current = [pi for pi, role in roles if role == "current"]
    if current:
        n = f"id(ins_{iid}_num)"
        state = f"({fresh}) ? ({n} > {cfloat(CURRENT_DEADBAND)} ? 1 : {n} < -{cfloat(CURRENT_DEADBAND)} ? 2 : 0) : 0"
        lines += _state_branches(current, iid, "word", state, CURRENT_WORDS)
    shore = [pi for pi, role in roles if role == "shore"]
    if shore and not is_notification(config, iid):
        state = f"({fresh}) ? (id(ins_{iid}_num) > {cfloat(SHORE_ON_ABOVE)} ? 1 : 2) : 0"
        lines += _state_branches(shore, iid, "value", state, SHORE_WORDS)
        lines += [
            "      - lambda: |-",
            f"          bool fresh = {fresh};",
            f"          uint8_t s = fresh ? (id(ins_{iid}_num) > {cfloat(SHORE_ON_ABOVE)} ? 1 : 2) : 0;",
            f"          if (s != id(ins_{iid}_sdrawn)) {{",
            f"            id(ins_{iid}_sdrawn) = s;",
            f"            uint32_t c = 0x{GAUGE_DAY[SHORE_STATE_COLOR[0]]:06X};",
        ]
        for number in (1, 2):
            lines.append(f"            if (s == {number}) c = 0x{GAUGE_DAY[SHORE_STATE_COLOR[number]]:06X};")
        if night:
            lines += ["            if (id(night_active)) {", f"              c = 0x{GAUGE_NIGHT[SHORE_STATE_COLOR[0]]:06X};"]
            for number in (1, 2):
                lines.append(f"              if (s == {number}) c = 0x{GAUGE_NIGHT[SHORE_STATE_COLOR[number]]:06X};")
            lines.append("            }")
        for pi in shore:
            lines.append(f"            lv_obj_set_style_bg_color(id({tile_id(pi, iid, ELECTRICAL)}), "
                         "lv_color_hex(c), LV_PART_MAIN);")
        lines.append("          }")
    return lines


def _show_page(ind: str, target: int) -> list:
    return [f"{ind}- lvgl.page.show:", f"{ind}    id: page_{target}", f"{ind}    animation: NONE",
            f"{ind}    time: {TAP_TIME}"]


def _touch_layer(display: dict, pi: int) -> list:
    """The full-screen touch object of a display with swipe, night or dimming.

    Paging uses ``on_short_click`` (a long press must not also turn the page) and every action
    first checks ``touch_ignore``: set by a press that only woke a dimmed screen, and by a swipe
    or long press that already acted, so neither can be followed by a stray page change.
    """
    n_pages = len(display["pages"])
    nxt, prev = (pi + 1) % n_pages, (pi - 1) % n_pages
    gate = ["              - if:", "                  condition:",
            "                    lambda: 'return !id(touch_ignore);'", "                  then:"]
    done = "              - lambda: 'id(touch_ignore) = true;'"
    wake = "id(dim_idle)" if dim_enabled(display) else "false"
    lines = [
        "        - obj:", f"            id: p{pi}_tap", "            x: 0", "            y: 0",
        "            width: 100%", "            height: 100%", "            bg_opa: TRANSP",
        "            border_width: 0", "            scrollable: false",
        "            on_press:", f"              - lambda: 'id(touch_ignore) = {wake};'",
    ]
    if n_pages > 1:
        lines += ["            on_short_click:", *gate, *_show_page("                    ", nxt)]
        if swipe_enabled(display):
            lines += ["            on_swipe_left:", *gate, *_show_page("                    ", nxt), done,
                      "            on_swipe_right:", *gate, *_show_page("                    ", prev), done]
    if night_enabled(display):
        lines += ["            on_long_press:", *gate,
                  "                    - lambda: 'id(night_override) = id(night_active) ? 0 : 1;'",
                  "                    - script.execute: apply_night",
                  "                    - script.execute: apply_power", done]
    return lines


def _header_lines(registry_board: dict, display: dict, pi: int, page: dict) -> list:
    """Clock and status dots in the narrow top row, the page title below, page dots at the bottom.

    Everything is static except colours and the clock digits, which render_header sets.
    """
    ring = registry_board["ring"]
    y, n_pages = ring["header_y"], len(display["pages"])
    lines = [
        f"        - label: {{id: p{pi}_clock, text: {yq('--:--')}, text_font: font_small, "
        f"text_color: 0x{DAY['clock']:06X}, align: TOP_MID, x: {HEADER_CLOCK_X}, y: {y}}}",
    ]
    for name, x in zip(("sk", "bridge", "mqtt"), HEADER_DOT_X):
        lines += [
            "        - obj:", f"            id: p{pi}_dot_{name}", "            align: TOP_MID",
            f"            x: {x}", f"            y: {y + 6}", f"            width: {HEADER_DOT_SIZE}",
            f"            height: {HEADER_DOT_SIZE}", f"            radius: {HEADER_DOT_SIZE // 2}",
            f"            bg_color: 0x{DAY['dot_unknown']:06X}", "            bg_opa: COVER",
            "            border_width: 0", "            pad_all: 0", "            scrollable: false",
        ]
    lines.append(
        f"        - label: {{id: p{pi}_title, text: {lit(page['title'])}, text_font: font_small, "
        f"text_color: 0x{DAY['title']:06X}, align: TOP_MID, y: {y + HEADER_TITLE_ROW}, "
        f"width: {HEADER_TITLE_WIDTH}, text_align: CENTER, long_mode: DOT}}")
    if n_pages > 1:
        lines += [
            "        - obj:", f"            id: p{pi}_pages", "            align: TOP_MID",
            f"            y: {ring['footer_y']}",
            f"            width: {n_pages * (PAGE_DOT_SIZE + PAGE_DOT_GAP)}",
            f"            height: {PAGE_DOT_SIZE + 2}", "            bg_opa: TRANSP", "            border_width: 0",
            "            pad_all: 0", "            scrollable: false",
            f"            layout: {{type: FLEX, flex_flow: ROW, flex_align_main: CENTER, "
            f"flex_align_cross: CENTER, pad_column: {PAGE_DOT_GAP}}}",
            "            widgets:",
        ]
        for j in range(n_pages):
            color = DAY["page_hi"] if j == pi else DAY["page_lo"]
            lines.append(f"              - obj: {{width: {PAGE_DOT_SIZE}, height: {PAGE_DOT_SIZE}, "
                         f"radius: {PAGE_DOT_SIZE // 2}, bg_color: 0x{color:06X}, bg_opa: COVER, "
                         "border_width: 0, pad_all: 0}")
    return lines


def _page_lines(config: dict, registry_board: dict, display: dict, pi: int, page: dict) -> list:
    n_pages = len(display["pages"])
    cw, ch = registry_board["content_width"], registry_board["content_height"]
    layout = page["layout"]
    themed = night_enabled(display)
    lines = [f"    - id: page_{pi}", "      bg_color: 0x000000", "      widgets:"]
    if layout == "single":
        iid = page["items"][0]
        lines += [
            "        - obj:",
            f"            id: p{pi}_content",
            "            align: CENTER",
            f"            width: {cw}",
            f"            height: {ch}",
            "            styles: tile_style",
            "            scrollable: false",
            f"            {FLEX_COLUMN}",
            "            widgets:",
        ]
        lines += _tile_widgets("              ", pi, iid, config["instruments"][iid], "font_value_single",
                               themed)
    elif layout == "grid-2x2":
        lines += [
            "        - obj:",
            f"            id: p{pi}_content",
            "            align: CENTER",
            f"            width: {cw}",
            f"            height: {ch}",
            "            bg_opa: TRANSP",
            "            border_width: 0",
            "            pad_all: 0",
            "            scrollable: false",
            "            layout:",
            "              type: GRID",
            "              pad_row: 6",
            "              pad_column: 6",
            "              grid_rows: [FR(1), FR(1)]",
            "              grid_columns: [FR(1), FR(1)]",
            "            widgets:",
        ]
        for index, iid in enumerate(page["items"]):
            lines += ["              - obj:"]
            if has_thresholds(config["instruments"][iid]) or themed:
                lines += [f"                  id: {tile_id(pi, iid, 'grid-2x2')}"]
            lines += [
                "                  styles: tile_style",
                f"                  grid_cell_row_pos: {index // 2}",
                f"                  grid_cell_column_pos: {index % 2}",
                "                  grid_cell_x_align: STRETCH",
                "                  grid_cell_y_align: STRETCH",
                "                  scrollable: false",
                f"                  {FLEX_COLUMN}",
                "                  widgets:",
            ]
            lines += _tile_widgets("                    ", pi, iid, config["instruments"][iid],
                                   "font_value_grid", themed)
    elif layout == "status-grid":
        cols, rows = status_grid_shape(len(page["items"]))
        lines += [
            "        - obj:",
            f"            id: p{pi}_content",
            "            align: CENTER",
            f"            width: {cw}",
            f"            height: {ch}",
            "            bg_opa: TRANSP",
            "            border_width: 0",
            "            pad_all: 0",
            "            scrollable: false",
            "            layout:",
            "              type: GRID",
            f"              pad_row: {STATUS_GAP}",
            f"              pad_column: {STATUS_GAP}",
            f"              grid_rows: [{', '.join(['FR(1)'] * rows)}]",
            f"              grid_columns: [{', '.join(['FR(1)'] * cols)}]",
            "            widgets:",
        ]
        font = status_font(len(page["items"]))
        for index, iid in enumerate(page["items"]):
            lines += _status_cell(config, "              ", pi, iid, font, index, cols, themed)
    elif layout == "tank-bars":
        lines += _tank_bars_page(config, registry_board, pi, page, themed)
    elif layout == "gauge-arc":
        lines += _gauge_arc_page(config, registry_board, display, pi, page, themed)
    elif layout == ELECTRICAL:
        lines += _electrical_page(config, registry_board, display, pi, page, themed)
    else:
        raise RenderError(f"layout {layout!r} is not supported by the firmware generator yet", 2)
    if header_enabled(display):
        lines += _header_lines(registry_board, display, pi, page)
    if touch_managed(display):
        if touch_layer_enabled(display):
            lines += _touch_layer(display, pi)
    elif n_pages > 1:
        # A transparent full-screen object on top: a tap anywhere shows the next page.
        lines += [
            "        - obj:",
            f"            id: p{pi}_tap",
            "            x: 0",
            "            y: 0",
            "            width: 100%",
            "            height: 100%",
            "            bg_opa: TRANSP",
            "            border_width: 0",
            "            scrollable: false",
            "            on_click:",
            "              - lvgl.page.show:",
            f"                  id: page_{(pi + 1) % n_pages}",
            "                  animation: NONE",
            f"                  time: {TAP_TIME}",
        ]
    return lines


def _fonts_lines(display: dict, config: dict) -> list:
    layouts = {page["layout"] for page in display["pages"]}
    fonts = []
    if "grid-2x2" in layouts or ELECTRICAL in layouts:
        fonts.append(("font_value_grid", "bold", FONT_SIZES["grid"], VALUE_GLYPHS))
    if "single" in layouts or "gauge-arc" in layouts:
        fonts.append(("font_value_single", "bold", FONT_SIZES["single"], VALUE_GLYPHS))
    if "tank-bars" in layouts:
        fonts.append(("font_value_bar", "bold", FONT_SIZES["bar"], VALUE_GLYPHS))
    status = _status_glyphs(config, display)
    for key in ("lg", "sm"):
        if STATUS_FONTS[key] in status:
            fonts.append((STATUS_FONTS[key], "bold", FONT_SIZES[f"status_{key}"], status[STATUS_FONTS[key]]))
    fonts.append(("font_small", "regular", FONT_SIZES["small"], _small_glyphs(config, display)))
    if alarm_takeover_enabled(display):
        fonts.append(("font_banner", "bold", FONT_SIZES["banner"], BANNER_GLYPHS))
        fonts.append(("font_banner_name", "bold", FONT_SIZES["banner_name"], _small_glyphs(config, display)))
    lines = ["font:"]
    for fid, weight, size, glyphs in fonts:
        lines += [f"  - file: fonts/{FONT_FILES[weight]}", f"    id: {fid}", f"    size: {size}",
                  "    bpp: 4", f"    glyphs: {lit(glyphs)}"]
    return lines


def _json_handler(iid: str, decimals: int, with_sev: bool = False, numeric: bool = False) -> list:
    sev_lines = []
    if with_sev:
        sev_lines = [
            '            const char *sv = x["sev"] | "unknown";',
            f'            id(ins_{iid}_sev) = strcmp(sv, "ok") == 0 ? 1 : strcmp(sv, "warn") == 0 ? 2 : '
            'strcmp(sv, "alarm") == 0 ? 3 : 0;',
        ]
    return [
        f"    - topic: {topics.instrument_topic(iid)}",
        "      then:",
        "        - lambda: |-",
        '            const char *st = x["st"] | "missing";',
        f"            id(ins_{iid}_seen) = millis();",
        '            bool ok = strcmp(st, "ok") == 0 && x["v"].is<float>();',
        f"            id(ins_{iid}_ok) = ok;",
        "            if (ok) {",
        "              char buf[VALUE_ACCURACY_MAX_LEN];",
        f'              value_accuracy_to_buf(buf, x["v"].as<float>(), {decimals});',
        f"              id(ins_{iid}_val) = buf;",
        *([f'              id(ins_{iid}_num) = x["v"].as<float>();'] if numeric else []),
        "            }",
        *sev_lines,
        "        - script.execute: render_tiles",
    ]


def _notification_handler(iid: str) -> list:
    return [
        f"    - topic: {topics.notification_topic(iid)}",
        "      then:",
        "        - lambda: |-",
        '            const char *st = x["st"] | "missing";',
        f"            id(ins_{iid}_seen) = millis();",
        f'            id(ins_{iid}_ok) = strcmp(st, "ok") == 0;',
        '            const char *sv = x["sev"] | "unknown";',
        f'            id(ins_{iid}_sev) = strcmp(sv, "ok") == 0 ? 1 : strcmp(sv, "warn") == 0 ? 2 : '
        'strcmp(sv, "alarm") == 0 ? 3 : 0;',
        "        - script.execute: render_tiles",
    ]


def _word_lines(config: dict, display: dict, iid: str, heartbeat: int) -> list:
    """Fixed state words for a notification, one if/then per severity (no runtime strings).

    The condition re-derives the gated severity (0 when the item is not fresh), so a stale
    or unknown notification always reads "--", never OK.
    """
    limit_ms = item_limit_ms(config, iid, heartbeat)
    fresh = (f"id(bridge_online) && mqtt::global_mqtt_client->is_connected() && id(ins_{iid}_ok)"
             f" && (millis() - id(ins_{iid}_seen)) < {limit_ms}UL")
    lines = []
    for pi, page in enumerate(display["pages"]):
        if iid not in page["items"]:
            continue
        for number in (3, 2, 1, 0):
            lines += [
                "      - if:",
                "          condition:",
                f"            lambda: 'return (({fresh}) ? id(ins_{iid}_sev) : 0) == {number};'",
                "          then:",
                "            - lvgl.label.update:",
                f"                id: p{pi}_{iid}_value",
                f"                text: {lit(STATUS_WORDS[number])}",
            ]
    return lines


def _sev_lines(config: dict, display: dict, iid: str, heartbeat: int, night: bool = False) -> list:
    """Guarded colour update for one thresholded instrument, on every page that shows it."""
    limit_ms = item_limit_ms(config, iid, heartbeat)
    sets = []
    for pi, page in enumerate(display["pages"]):
        if iid in page["items"] and page["layout"] not in GAUGE_LAYOUTS and (
                page["layout"] != ELECTRICAL or is_notification(config, iid)):
            tile, value = tile_id(pi, iid, page["layout"]), f"p{pi}_{iid}_value"
            sets.append((tile, value))
    lines = [
        "      - lambda: |-",
        "          bool fresh = id(bridge_online) && mqtt::global_mqtt_client->is_connected()"
        f" && id(ins_{iid}_ok) && (millis() - id(ins_{iid}_seen)) < {limit_ms}UL;",
        f"          uint8_t sev = fresh ? id(ins_{iid}_sev) : 0;",
        f"          if (sev != id(ins_{iid}_drawn_sev)) {{",
        f"            id(ins_{iid}_drawn_sev) = sev;",
        "            uint32_t bg = 0x%06X, fg = 0x%06X;" % (SEV_COLORS[0][0], SEV_COLORS[0][1]),
    ]
    for number in (1, 2, 3):
        bg, fg = SEV_COLORS[number]
        lines.append(f"            if (sev == {number}) {{ bg = 0x{bg:06X}; fg = 0x{fg:06X}; }}")
    if night:
        lines.append("            if (id(night_active)) {")
        lines.append("              bg = 0x%06X; fg = 0x%06X;" % NIGHT_SEV_COLORS[0])
        for number in (1, 2, 3):
            bg, fg = NIGHT_SEV_COLORS[number]
            lines.append(f"              if (sev == {number}) {{ bg = 0x{bg:06X}; fg = 0x{fg:06X}; }}")
        lines.append("            }")
    for tile, value in sets:
        lines += [f"            lv_obj_set_style_bg_color(id({tile}), lv_color_hex(bg), LV_PART_MAIN);",
                  f"            lv_obj_set_style_text_color(id({value}), lv_color_hex(fg), LV_PART_MAIN);"]
    lines.append("          }")
    return lines


def alarm_takeover_enabled(display: dict) -> bool:
    return display.get("ui", {}).get("alarm_takeover", False) is True


def alarm_page_index(display: dict) -> int:
    return display.get("ui", {}).get("alarm_page", 0)


def summary_stale_ms(heartbeat: int) -> int:
    """The alarm summary is republished every heartbeat; three missed ones make it stale."""
    return 3 * heartbeat * 1000


ALARM_GLOBALS = [
    '  - {id: summary_max, type: uint8_t, initial_value: "0"}',      # 0 unknown, 1 ok, 2 warn, 3 alarm
    '  - {id: summary_na, type: int, initial_value: "0"}',
    '  - {id: summary_seen, type: uint32_t, initial_value: "0"}',
    '  - {id: summary_valid, type: bool, initial_value: "false"}',
    '  - {id: summary_first, type: int, initial_value: "-1"}',       # index of ids[0] among this display\'s items
    '  - {id: alarm_rising, type: bool, initial_value: "false"}',
    '  - {id: alarm_dismissed, type: bool, initial_value: "false"}',
    '  - {id: alarm_idx, type: int, initial_value: "-1"}',           # item whose name the banner should show
    '  - {id: alarm_drawn_idx, type: int, initial_value: "-2"}',     # item whose name the banner shows
    '  - {id: alarm_flash, type: bool, initial_value: "false"}',
]


def _alarm_handler(used: list, display: dict) -> list:
    """The alarm summary message: store it, find the first alarm's item, detect a rising edge."""
    lines = [
        f"    - topic: {topics.ALARM_SUMMARY_TOPIC}",
        "      then:",
        "        - lambda: |-",
        '            const char *mx = x["max"] | "unknown";',
        '            uint8_t m = strcmp(mx, "ok") == 0 ? 1 : strcmp(mx, "warn") == 0 ? 2 : '
        'strcmp(mx, "alarm") == 0 ? 3 : 0;',
        '            int na = x["na"] | 0;',
        '            const char *id0 = x["ids"][0] | "";',
        "            int first = -1;",
    ]
    for index, iid in enumerate(used):
        lines.append(f'            {"" if index == 0 else "else "}if (strcmp(id0, "{iid}") == 0) first = {index};')
    lines += [
        "            bool rising = m == 3 && (id(summary_max) != 3 || na > id(summary_na));",
        "            id(summary_max) = m;",
        "            id(summary_na) = na;",
        "            id(summary_first) = first;",
        "            id(summary_seen) = millis();",
        "            id(summary_valid) = true;",
        "            if (rising) {",
        "              id(alarm_rising) = true;",
        "              id(alarm_dismissed) = false;",
        "            }",
        "        - if:",
        "            condition:",
        "              lambda: 'return id(alarm_rising);'",
        "            then:",
        "              - lambda: 'id(alarm_rising) = false;'",
        "              - lvgl.page.show:",
        f"                  id: page_{alarm_page_index(display)}",
        "                  animation: NONE",
        f"                  time: {TAP_TIME}",
        "              - light.turn_on:",
        "                  id: board_backlight",
        "                  brightness: 100%",
    ]
    if power_enabled(display):  # the brightness controller must know the backlight is at 100 %
        lines.append("              - lambda: 'id(bl_drawn) = 100;'")
    lines.append("        - script.execute: render_alarm")
    return lines


def _item_label(config: dict, iid: str) -> str:
    return (config["notifications"] if is_notification(config, iid) else config["instruments"])[iid]["label"]


def _alarm_script(config: dict, used: list, heartbeat: int) -> list:
    """Show or hide the banner; the first alarm's name comes from fixed !literal branches."""
    lines = [
        "  - id: render_alarm",
        "    mode: restart",
        "    then:",
        "      - lambda: |-",
        "          bool seen = id(summary_valid) && (millis() - id(summary_seen)) < "
        f"{summary_stale_ms(heartbeat)}UL;",
        "          if (!seen) {",
        "            id(summary_max) = 0;",
        "            id(summary_na) = 0;",
        "          }",
        "          bool show = seen && id(bridge_online) && mqtt::global_mqtt_client->is_connected()"
        " && id(summary_max) == 3 && id(summary_na) > 0 && !id(alarm_dismissed);",
        "          id(alarm_idx) = show ? id(summary_first) : -1;",
        "          if (show) {",
        "            char buf[16];",
        f"            snprintf(buf, sizeof(buf), \"%d%s %s\", id(summary_na) > {BANNER_COUNT_MAX} ? "
        f"{BANNER_COUNT_MAX} : id(summary_na), id(summary_na) > {BANNER_COUNT_MAX} ? \"+\" : \"\", "
        "id(summary_na) == 1 ? \"ALARM\" : \"ALARMS\");",
        "            if (strcmp(lv_label_get_text(id(alarm_banner_count)), buf) != 0)"
        " lv_label_set_text(id(alarm_banner_count), buf);",
        "            lv_obj_clear_flag(id(alarm_banner), LV_OBJ_FLAG_HIDDEN);",
        "          } else {",
        "            lv_obj_add_flag(id(alarm_banner), LV_OBJ_FLAG_HIDDEN);",
        "            lv_obj_set_style_bg_opa(id(alarm_banner), LV_OPA_COVER, LV_PART_MAIN);",
        "            id(alarm_flash) = false;",
        "          }",
    ]
    for index in range(-1, len(used)):
        text = yq("") if index < 0 else lit(_item_label(config, used[index]))
        lines += [
            "      - if:",
            "          condition:",
            f"            lambda: 'return id(alarm_idx) == {index} && id(alarm_drawn_idx) != {index};'",
            "          then:",
            "            - lvgl.label.update:",
            "                id: alarm_banner_name",
            f"                text: {text}",
            f"            - lambda: 'id(alarm_drawn_idx) = {index};'",
        ]
    return lines


def _alarm_banner(board: dict) -> list:
    """Banner on LVGL's top layer: above every page, one object, never covered by the tap layer."""
    fit = "width: 100%, text_align: CENTER, long_mode: DOT"
    return [
        "  top_layer:",
        "    widgets:",
        "      - obj:",
        "          id: alarm_banner",
        "          align: CENTER",
        f"          width: {board['content_width']}",
        f"          height: {BANNER_HEIGHT}",
        f"          bg_color: 0x{BANNER_COLOR:06X}",
        "          bg_opa: COVER",
        "          border_width: 0",
        "          radius: 14",
        "          pad_all: 4",
        "          hidden: true",
        "          scrollable: false",
        f"          {FLEX_COLUMN}",
        "          on_click:",
        "            - lambda: 'id(alarm_dismissed) = true;'",
        "            - script.execute: render_alarm",
        "          widgets:",
        f"            - label: {{id: alarm_banner_count, text: {yq('')}, text_font: font_banner, "
        f"text_color: 0xFFFFFF, {fit}}}",
        f"            - label: {{id: alarm_banner_name, text: {yq('')}, text_font: font_banner_name, "
        f"text_color: 0xFFFFFF, {fit}}}",
    ]


def _ui_globals(display: dict) -> list:
    lines = []
    if touch_managed(display) and touch_layer_enabled(display):
        lines.append('  - {id: touch_ignore, type: bool, initial_value: "false"}')
    if dim_enabled(display):
        lines.append('  - {id: dim_idle, type: bool, initial_value: "false"}')
    if power_enabled(display):
        lines.append('  - {id: bl_drawn, type: int, initial_value: "-1"}')
    if night_enabled(display):
        lines += ['  - {id: night_override, type: int, initial_value: "-1"}',   # -1 none, 0 day, 1 night
                  '  - {id: night_active, type: bool, initial_value: "false"}',
                  '  - {id: night_drawn, type: int, initial_value: "-1"}',
                  '  - {id: night_dirty, type: bool, initial_value: "false"}']
    if night_mode(display) == "schedule":
        lines.append('  - {id: night_sched, type: bool, initial_value: "false"}')
    if clock_enabled(display):
        lines += ['  - {id: clock_h, type: int, initial_value: "-1"}',
                  '  - {id: clock_m, type: int, initial_value: "-1"}',
                  '  - {id: clock_seen, type: uint32_t, initial_value: "0"}']
    if header_enabled(display):
        lines += ['  - {id: sk_state, type: uint8_t, initial_value: "0"}',   # 0 unknown, 1 connected, 2 down
                  '  - {id: clock_drawn, type: int, initial_value: "-2"}',
                  '  - {id: hdr_drawn, type: int, initial_value: "-1"}']
    return lines


def _after_ui_change(display: dict, indent: str = "        ") -> list:
    """The actions that follow a change of clock, night state or connectivity."""
    lines = []
    if night_enabled(display):
        lines.append(f"{indent}- script.execute: apply_night")
    if header_enabled(display):
        lines.append(f"{indent}- script.execute: render_header")
    if power_enabled(display):
        lines.append(f"{indent}- script.execute: apply_power")
    return lines


def _signalk_message(display: dict) -> list:
    return [
        f"    - topic: {topics.BRIDGE_SIGNALK_TOPIC}", "      then:",
        '        - lambda: \'id(sk_state) = (x == "connected") ? 1 : (x == "disconnected") ? 2 : 0;\'',
        "        - script.execute: render_header",
    ]


def _clock_handler(display: dict) -> list:
    """The bridge clock: two integers parsed from ``hm`` (the text itself is never shown) and the flag."""
    lines = [
        f"    - topic: {topics.BRIDGE_CLOCK_TOPIC}", "      then:", "        - lambda: |-",
        '            const char *hm = x["hm"] | "";',
        "            int h = -1, m = -1;",
        "            if (strlen(hm) == 5 && hm[2] == ':' && isdigit((unsigned char) hm[0])"
        " && isdigit((unsigned char) hm[1]) && isdigit((unsigned char) hm[3])"
        " && isdigit((unsigned char) hm[4])) {",
        "              h = (hm[0] - '0') * 10 + (hm[1] - '0');",
        "              m = (hm[3] - '0') * 10 + (hm[4] - '0');",
        "              if (h > 23 || m > 59) { h = -1; m = -1; }",
        "            }",
        "            id(clock_h) = h;",
        "            id(clock_m) = m;",
        "            id(clock_seen) = millis();",
    ]
    if night_mode(display) == "schedule":
        lines += [
            '            if (h >= 0 && x["night"].is<bool>()) {',
            '              bool n = x["night"].as<bool>();',
            "              if (n != id(night_sched)) { id(night_sched) = n; id(night_override) = -1; }",
            "            }",
        ]
    return lines + _after_ui_change(display)


def _color_expr(name: str, night: bool) -> str:
    return f"(nt ? 0x{NIGHT[name]:06X} : 0x{DAY[name]:06X})" if night else f"0x{DAY[name]:06X}"


def _header_script(board: dict, display: dict, heartbeat: int) -> list:
    """Clock digits (generated with snprintf from two integers) and status colours, only on change."""
    night = night_enabled(display)
    pages = range(len(display["pages"]))
    limit = summary_stale_ms(heartbeat)
    lines = [
        "  - id: render_header", "    mode: restart", "    then:", "      - lambda: |-",
        "          bool mq = mqtt::global_mqtt_client->is_connected();",
        "          bool online = id(bridge_online) && mq;",
        f"          bool fresh = online && id(clock_h) >= 0 && (millis() - id(clock_seen)) < {limit}UL;",
        "          int hm = fresh ? id(clock_h) * 60 + id(clock_m) : -1;",
        "          if (hm != id(clock_drawn)) {",
        "            id(clock_drawn) = hm;",
        "            char buf[8];",
        '            if (hm < 0) snprintf(buf, sizeof(buf), "--:--");',
        '            else snprintf(buf, sizeof(buf), "%02d:%02d", hm / 60, hm % 60);',
    ]
    lines += [f"            lv_label_set_text(id(p{pi}_clock), buf);" for pi in pages]
    lines += [
        "          }",
        "          // 0 unknown (grey), 1 good, 2 bad; the SignalK flag is retained, so it only counts"
        " while the bridge is online",
        "          int dsk = !online ? 0 : (id(sk_state) == 1 ? 1 : (id(sk_state) == 2 ? 2 : 0));",
        "          int dbr = online ? 1 : 2;",
        "          int dmq = mq ? 1 : 2;",
        f"          bool nt = {'id(night_active)' if night else 'false'};",
        "          int state = dsk | (dbr << 2) | (dmq << 4) | (nt ? 64 : 0);",
        "          if (state != id(hdr_drawn)) {",
        "            id(hdr_drawn) = state;",
        f"            uint32_t good = {_color_expr('dot_ok', night)}, bad = {_color_expr('dot_bad', night)}, "
        f"unk = {_color_expr('dot_unknown', night)};",
        f"            uint32_t hi = {_color_expr('page_hi', night)}, lo = {_color_expr('page_lo', night)};",
        "            uint32_t sk = dsk == 1 ? good : (dsk == 2 ? bad : unk);",
        "            uint32_t br = dbr == 1 ? good : bad;",
        "            uint32_t mt = dmq == 1 ? good : bad;",
    ]
    for pi in pages:
        lines += [
            f"            lv_obj_set_style_bg_color(id(p{pi}_dot_sk), lv_color_hex(sk), LV_PART_MAIN);",
            f"            lv_obj_set_style_bg_color(id(p{pi}_dot_bridge), lv_color_hex(br), LV_PART_MAIN);",
            f"            lv_obj_set_style_bg_color(id(p{pi}_dot_mqtt), lv_color_hex(mt), LV_PART_MAIN);",
            f"            lv_obj_set_style_text_color(id(p{pi}_clock), lv_color_hex({_color_expr('clock', night)}),"
            " LV_PART_MAIN);",
            f"            lv_obj_set_style_text_color(id(p{pi}_title), lv_color_hex({_color_expr('title', night)}),"
            " LV_PART_MAIN);",
        ]
        if len(display["pages"]) > 1:
            lines += [
                f"            for (int j = 0; j < {len(display['pages'])}; j++)"
                f" lv_obj_set_style_bg_color(lv_obj_get_child(id(p{pi}_pages), j),"
                f" lv_color_hex(j == {pi} ? hi : lo), LV_PART_MAIN);",
            ]
    lines.append("          }")
    return lines


def _night_script(config: dict, display: dict, used: list) -> list:
    """Resolve night (schedule flag or manual, with the override) and repaint the palette on change."""
    schedule = night_mode(display) == "schedule"
    lines = [
        "  - id: apply_night", "    mode: restart", "    then:", "      - lambda: |-",
        f"          bool base = {'id(night_sched)' if schedule else 'false'};",
        "          bool n = id(night_override) >= 0 ? id(night_override) == 1 : base;",
        "          id(night_active) = n;",
        "          if (n != (id(night_drawn) == 1)) {",
        "            id(night_drawn) = n ? 1 : 0;",
        "            id(night_dirty) = true;",
    ]
    for pi, page in enumerate(display["pages"]):
        if page["layout"] in GAUGE_LAYOUTS:
            lines += _gauge_night_lines(config, display, pi, page)
            continue
        if page["layout"] == ELECTRICAL:
            lines += _electrical_night_lines(config, display, pi, page)
            continue
        for iid in page["items"]:
            notification = is_notification(config, iid)
            tile = tile_id(pi, iid, page["layout"])
            lines += [
                f"            lv_obj_set_style_bg_color(id({tile}), lv_color_hex("
                f"n ? 0x{NIGHT['tile']:06X} : 0x{DAY['tile']:06X}), LV_PART_MAIN);",
                f"            lv_obj_set_style_text_color(id(p{pi}_{iid}_name), lv_color_hex("
                f"n ? 0x{NIGHT['name']:06X} : 0x{DAY['name']:06X}), LV_PART_MAIN);",
                f"            lv_obj_set_style_text_color(id(p{pi}_{iid}_value), lv_color_hex("
                f"n ? 0x{NIGHT['value']:06X} : 0x{DAY['value']:06X}), LV_PART_MAIN);",
            ]
            if not notification:
                lines.append(f"            lv_obj_set_style_text_color(id(p{pi}_{iid}_unit), lv_color_hex("
                             f"n ? 0x{NIGHT['name']:06X} : 0x{DAY['name']:06X}), LV_PART_MAIN);")
    for iid in used:   # the guarded severity colours must repaint with the new palette
        if severity_tile(config, display, iid):
            lines.append(f"            id(ins_{iid}_drawn_sev) = 255;")
        if not is_notification(config, iid) and on_gauge_page(display, iid):
            lines.append(f"            id(ins_{iid}_gdrawn) = 255;")
        if shore_instrument(config, display, iid):
            lines.append(f"            id(ins_{iid}_sdrawn) = 255;")
    lines += [
        "          }",
        "      - if:", "          condition:", "            lambda: 'return id(night_dirty);'", "          then:",
        "            - lambda: 'id(night_dirty) = false;'", "            - script.execute: render_tiles",
    ]
    if header_enabled(display):
        lines.append("            - script.execute: render_header")
    if power_enabled(display):
        lines.append("            - script.execute: apply_power")
    return lines


def _power_script(display: dict) -> list:
    """One backlight controller: banner 100 %, else idle dim, else night, else day; only on change."""
    lines = [
        "  - id: apply_power", "    mode: restart", "    then:", "      - lambda: |-",
        f"          int target = {day_brightness(display)};",
    ]
    if night_enabled(display):
        lines.append(f"          if (id(night_active)) target = {night_brightness(display)};")
    if dim_enabled(display):
        lines.append(f"          if (id(dim_idle) && target > {dim_brightness(display)}) "
                     f"target = {dim_brightness(display)};")
    if alarm_takeover_enabled(display):
        lines.append("          if (!lv_obj_has_flag(id(alarm_banner), LV_OBJ_FLAG_HIDDEN)) target = 100;")
    lines += [
        "          if (target != id(bl_drawn)) {",
        "            id(bl_drawn) = target;",
        "            auto call = id(board_backlight).turn_on();",
        "            call.set_brightness(target / 100.0f);",
        "            call.perform();",
        "          }",
    ]
    return lines


def _dim_interval(display: dict) -> list:
    """Four times a second: has the touchscreen been idle for dim_after_s? Then re-apply the backlight."""
    return [
        "  - interval: 250ms", "    then:", "      - if:", "          condition:", "            lvgl.is_idle:",
        f"              timeout: {dim_after_s(display)}s", "          then:",
        "            - lambda: 'id(dim_idle) = true;'", "          else:",
        "            - lambda: 'id(dim_idle) = false;'", "      - script.execute: apply_power",
    ]


def render_device_yaml(config: dict, registry: dict, display_id: str, *, board_package: str) -> str:
    display = config["displays"][display_id]
    board = registry["boards"][display["board"]]
    heartbeat = config.get("heartbeat_s", schema.DEFAULT_HEARTBEAT_S)
    name = firmware_name(display_id)
    username = mqtt_auth.display_username(display_id)
    used = used_instruments(display)
    for iid in used:
        assert schema.INSTRUMENT_ID.fullmatch(iid), iid  # C++ identifier safety
    lines = [
        "# Generated by VesselStack (vesselstackctl display render). Do not edit; regenerate",
        "# instead. This file holds no secrets: they are in secrets.yaml next to it.",
        "substitutions:",
        f"  device_name: {name}",
        f"  mqtt_username: {username}",
        "",
        "esphome:",
        "  name: ${device_name}",
        f"  min_version: {esphome_version()}",
        "",
        "packages:",
        f"  board: !include {board_package}",
    ]
    lines.append("  app: !include app/vesselstack.yaml")
    if display.get("home_assistant_api", False):
        lines.append("  api: !include app/api.yaml")
    lines += ["", "# ---- generated user interface ----", "globals:",
              '  - {id: bridge_online, type: bool, initial_value: "false"}']
    for iid in used:
        lines += [f'  - {{id: ins_{iid}_ok, type: bool, initial_value: "false"}}',
                  f'  - {{id: ins_{iid}_seen, type: uint32_t, initial_value: "0"}}']
        if is_notification(config, iid):
            lines += [f'  - {{id: ins_{iid}_sev, type: uint8_t, initial_value: "0"}}',
                      f'  - {{id: ins_{iid}_drawn_sev, type: uint8_t, initial_value: "255"}}']
            continue
        lines.append(f"  - {{id: ins_{iid}_val, type: std::string, initial_value: '\"{BLANK_VALUE}\"'}}")
        if has_thresholds(config["instruments"][iid]):
            lines.append(f'  - {{id: ins_{iid}_sev, type: uint8_t, initial_value: "0"}}')
        if severity_tile(config, display, iid):
            lines.append(f'  - {{id: ins_{iid}_drawn_sev, type: uint8_t, initial_value: "255"}}')
        if keeps_number(config, display, iid):
            lines.append(f'  - {{id: ins_{iid}_num, type: float, initial_value: "0"}}')
        if on_gauge_page(display, iid):
            lines.append(f'  - {{id: ins_{iid}_gdrawn, type: uint8_t, initial_value: "255"}}')
        if shore_instrument(config, display, iid):
            lines.append(f'  - {{id: ins_{iid}_sdrawn, type: uint8_t, initial_value: "255"}}')
    takeover = alarm_takeover_enabled(display)
    if takeover:
        lines += ALARM_GLOBALS
    lines += _ui_globals(display)
    lines += ["", *_fonts_lines(display, config), "", "mqtt:", "  on_message:",
              f"    - topic: {topics.BRIDGE_STATUS_TOPIC}", "      then:",
              '        - lambda: \'id(bridge_online) = (x == "online");\'',
              "        - script.execute: render_tiles"]
    if header_enabled(display):
        lines += ["        - script.execute: render_header", *_signalk_message(display)]
    lines.append("  on_json_message:")
    for iid in used:
        if is_notification(config, iid):
            lines += _notification_handler(iid)
        else:
            lines += _json_handler(iid, config["instruments"][iid]["precision"],
                                   has_thresholds(config["instruments"][iid]), keeps_number(config, display, iid))
    if takeover:
        lines += _alarm_handler(used, display)
    if clock_enabled(display):
        lines += _clock_handler(display)
    lines += ["  on_disconnect:", "    - lambda: 'id(bridge_online) = false;'",
              "    - script.execute: render_tiles", "", "script:", "  - id: render_tiles",
              "    mode: restart", "    then:"]
    for pi, page in enumerate(display["pages"]):
        for iid in page["items"]:
            if is_notification(config, iid):
                continue  # drawn by the fixed-word branches below
            if page["layout"] == ELECTRICAL and electrical_slots(page).get("shore") == iid:
                continue  # the pill shows ON / OFF / -- from the branches below, not the number
            limit_ms = stale_threshold_s(config["instruments"][iid], heartbeat) * 1000
            lines += [
                "      - lvgl.label.update:",
                f"          id: p{pi}_{iid}_value",
                "          text: !lambda |-",
                "            bool fresh = id(bridge_online) && mqtt::global_mqtt_client->is_connected()"
                f" && id(ins_{iid}_ok) && (millis() - id(ins_{iid}_seen)) < {limit_ms}UL;",
                f"            return fresh ? id(ins_{iid}_val) : std::string(\"{BLANK_VALUE}\");",
            ]
    for iid in used:
        if is_notification(config, iid):
            lines += _word_lines(config, display, iid, heartbeat)
    for iid in used:
        if severity_tile(config, display, iid):
            lines += _sev_lines(config, display, iid, heartbeat, night_enabled(display))
        if not is_notification(config, iid) and on_gauge_page(display, iid):
            lines += _gauge_lines(config, display, iid, heartbeat, night_enabled(display))
        if not is_notification(config, iid):
            lines += _electrical_lines(config, display, iid, heartbeat, night_enabled(display))
    if takeover:
        lines += _alarm_script(config, used, heartbeat)
    if header_enabled(display):
        lines += _header_script(board, display, heartbeat)
    if night_enabled(display):
        lines += _night_script(config, display, used)
    if power_enabled(display):
        lines += _power_script(display)
    lines += ["", "interval:", "  - interval: 1s", "    then:", "      - script.execute: render_tiles"]
    if takeover:
        lines.append("      - script.execute: render_alarm")
    lines += _after_ui_change(display, "      ")
    if takeover:
        lines += [f"  - interval: {BANNER_FLASH_INTERVAL}", "    then:", "      - lambda: |-",
                  "          if (lv_obj_has_flag(id(alarm_banner), LV_OBJ_FLAG_HIDDEN)) return;",
                  "          id(alarm_flash) = !id(alarm_flash);",
                  "          lv_obj_set_style_bg_opa(id(alarm_banner), id(alarm_flash) ? LV_OPA_40 : LV_OPA_COVER,"
                  " LV_PART_MAIN);"]
    if dim_enabled(display):
        lines += _dim_interval(display)
    lines += ["", "lvgl:", "  displays: [board_display]", "  touchscreens: [board_touch]",
              "  buffer_size: 100%", "  style_definitions:", "    - id: tile_style",
              "      bg_color: 0x101820", "      bg_opa: COVER", "      border_width: 0",
              "      radius: 14", "      pad_all: 4", "  pages:"]
    for pi, page in enumerate(display["pages"]):
        lines += _page_lines(config, board, display, pi, page)
    if takeover:
        lines += _alarm_banner(board)
    return "\n".join(lines) + "\n"


def render_secrets_yaml(values: dict) -> str:
    lines = ["# Generated by VesselStack. Contains secrets: keep this file private (mode 0600).",
             "# Values are !literal so ESPHome does not treat $ or <% in a password as syntax."]
    for key, value in values.items():
        lines.append(f"{key}: {lit(value)}")
    return "\n".join(lines) + "\n"


# --- secrets and inputs -------------------------------------------------------------

@dataclass
class Rendered:
    display_id: str
    files: dict = field(default_factory=dict)  # relative path -> bytes, sorted on use
    warnings: list = field(default_factory=list)


def ensure_display_secrets(cfg_dir, secret_data: dict, display_id: str) -> dict:
    """Lazily generate the OTA password and API key; mqtt_password must already exist."""
    entry = secret_data["displays"].get(display_id)
    entry = dict(entry) if isinstance(entry, dict) else {}
    if not entry.get("mqtt_password"):
        raise RenderError(
            f"display {display_id!r} has no MQTT password yet; run "
            "'vesselstackctl display sync-mqtt' (with DISPLAYS_ENABLE=true) first", 2)
    changed = False
    for key, generate in lifecycle.GENERATED_SECRETS.items():
        if not entry.get(key):
            entry[key] = generate()
            changed = True
    if changed:
        secret_data["displays"][display_id] = entry
        lifecycle.write_secrets(cfg_dir, secret_data)
    return entry


def _read_package(relative: str) -> bytes:
    path = (ESPHOME_DIR / relative).resolve()
    if ESPHOME_DIR.resolve() not in path.parents or not path.is_file():
        raise RenderError(f"missing firmware package file displays/esphome/{relative}")
    return path.read_bytes()


def collect(ctx, display_id: str, allow_missing_wifi: bool = False) -> Rendered:
    """Validate everything and build the in-memory project (generates missing secrets)."""
    registry = lifecycle.load_registry()
    with lifecycle.ConfigDir(ctx) as cfg_dir:
        cfg_dir.lock()
        config = lifecycle._loaded_config(cfg_dir, registry)
        lifecycle._display_or_error(config, display_id)
        secret_data = lifecycle.read_secrets(cfg_dir)
        glyph_errors = check_glyphs(config)
        if glyph_errors:
            raise RenderError("; ".join(glyph_errors), 2)
        result = Rendered(display_id)
        wifi = secret_data.get("wifi")
        if not (isinstance(wifi, dict) and wifi.get("ssid") and isinstance(wifi.get("password"), str)):
            if not allow_missing_wifi:
                raise RenderError("no Wi-Fi credentials are stored; run "
                                  "'vesselstackctl display wifi' first (or pass "
                                  "--allow-missing-wifi for a test build with placeholders)", 2)
            wifi = PLACEHOLDER_WIFI
            result.warnings.append(PLACEHOLDER_NOTE)
        entry = ensure_display_secrets(cfg_dir, secret_data, display_id)
    display = config["displays"][display_id]
    board_id = display["board"]
    board = registry["boards"][board_id]
    package = board["esphome_package"]
    host = config["mqtt_host"]
    if host.lower().endswith(".local"):
        result.warnings.append(
            f"mqtt_host {host!r} is an mDNS name. ESP-IDF builds resolve .local names through "
            "lwIP's mDNS query hook, but that is not verified on the display hardware and fails on "
            "networks that block multicast; prefer a fixed IP address or a DNS name")
    files = result.files
    prefix = display_id
    files[f"{prefix}/{package}"] = _read_package(package)
    files[f"{prefix}/app/vesselstack.yaml"] = _read_package("app/vesselstack.yaml")
    if display.get("home_assistant_api", False):
        files[f"{prefix}/app/api.yaml"] = _read_package("app/api.yaml")
    for font in sorted(FONT_FILES.values()) + [FONT_LICENSE]:
        files[f"{prefix}/fonts/{font}"] = _read_package(f"fonts/{font}")
    device = render_device_yaml(config, registry, display_id, board_package=package)
    files[f"{prefix}/{display_id}.yaml"] = device.encode("utf-8")
    secret_values = {"wifi_ssid": wifi["ssid"], "wifi_password": wifi["password"],
                     "mqtt_host": host, "mqtt_password": entry["mqtt_password"],
                     "ota_password": entry["ota_password"]}
    if display.get("home_assistant_api", False):
        secret_values["api_encryption_key"] = entry["api_encryption_key"]
    files[f"{prefix}/secrets.yaml"] = render_secrets_yaml(secret_values).encode("utf-8")
    files[f"{prefix}/README.txt"] = readme_text(display_id, display, bool(result.warnings)).encode("utf-8")
    return result


def readme_text(display_id: str, display: dict, placeholders: bool) -> str:
    image = pinned_image()
    version = esphome_version()
    device = f"{display_id}.yaml"
    note = ("\nNOTE: this build uses PLACEHOLDER Wi-Fi values; do not flash it.\n"
            if placeholders else "")
    return f"""VesselStack display firmware project: {display_id} ({display['name']})
{note}
This directory holds secrets (secrets.yaml: Wi-Fi password, MQTT password, OTA
password{', Home Assistant API key' if display.get('home_assistant_api') else ''}). Keep it private and delete it after flashing.

Compile (Docker; first run downloads about 2 GB of toolchain into the cache volume):

  docker run --rm -v "$PWD":/config -v vesselstack-esphome-cache:/cache \\
    {image} compile /config/{device}

First flash over USB. Docker Desktop on macOS and Windows cannot pass USB serial
devices to containers, so use a native ESPHome there:

  pipx install esphome=={version}      # or: pip install esphome=={version}
  esphome run {device}

On Linux the container can flash directly:

  docker run --rm -v "$PWD":/config -v vesselstack-esphome-cache:/cache \\
    --device=/dev/ttyACM0 {image} run /config/{device} --device /dev/ttyACM0

Alternatively upload the firmware.factory.bin that "compile" creates with
https://web.esphome.io (Chrome or Edge).

Later updates over Wi-Fi (OTA):

  esphome upload {device} --device <display-ip>

Run `vesselstackctl display rotate {display_id}` only together with a USB reflash:
the display keeps its old OTA, MQTT and API secrets until reflashed.
"""


# --- build directory (root-owned, no-follow) ---------------------------------------

def _enforce_owner() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _open_dir(parent_fd: int, name: str, label: str) -> int:
    """Open (creating, mode 0700) a subdirectory without following symlinks."""
    try:
        os.mkdir(name, DIR_MODE, dir_fd=parent_fd)
    except FileExistsError:
        pass
    try:
        info = os.lstat(name, dir_fd=parent_fd)
    except OSError as exc:
        raise RenderError(f"cannot use {label}: {exc.strerror}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise RenderError(f"refusing to use {label}: it is a symbolic link")
    if not stat.S_ISDIR(info.st_mode):
        raise RenderError(f"refusing to use {label}: it is not a directory")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise RenderError(f"refusing to use {label}: {exc.strerror}") from exc
    try:
        now = os.fstat(fd)
        if (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino):
            raise RenderError(f"{label} changed while it was being opened")
        if _enforce_owner() and now.st_uid != 0:
            raise RenderError(f"refusing to use {label}: not owned by root (uid {now.st_uid})")
        os.fchmod(fd, DIR_MODE)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_existing_dir(parent_fd: int, name: str, label: str):
    """Open an existing subdirectory without following symlinks; None when it is absent."""
    try:
        info = os.lstat(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RenderError(f"refusing to use {label}: it is not a plain directory")
    return _open_dir(parent_fd, name, label)


@contextlib.contextmanager
def build_lock(ctx, display_id: str, create: bool = True, notify=None, timeout=None):
    """Exclusive per-display lock ``build/displays/.<id>.lock`` (root, 0600, no-follow flock).

    Every command that creates, replaces, reads for flashing or removes this display's
    build directory holds it for its whole run: render and export (around write_project),
    build (render + compile + artifact copy + rotation), flash and remove. It is separate
    from the 10 s configuration lock; always take it FIRST and the configuration lock
    (inside ``collect``) second. flock belongs to the open file description, so never
    take it twice in one process: only the commands above lock, write_project does not.

    Builds are long, so a waiter says so, waits up to ``ctx.build_lock_timeout`` seconds
    and then fails with a clear message instead of blocking silently. With
    ``create=False`` nothing is created: yields False when ``build/displays`` is absent.
    ``timeout`` overrides ``ctx.build_lock_timeout``.
    """
    if not schema.DISPLAY_ID.fullmatch(display_id):
        raise RenderError("display id must match ^[a-z]([a-z0-9-]{0,22}[a-z0-9])?$", 2)
    notify = notify or (lambda text: print(text, file=sys.stderr))
    try:
        root_fd = os.open(ctx.root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise RenderError(f"cannot open {ctx.root}: {exc.strerror}") from exc
    fds = [root_fd]
    lock_fd = -1
    try:
        label = str(ctx.root / BUILD_DIR_NAME)
        if create:
            build_fd = _open_dir(root_fd, BUILD_DIR_NAME, label)
        else:
            build_fd = _open_existing_dir(root_fd, BUILD_DIR_NAME, label)
        if build_fd is None:
            yield False
            return
        fds.append(build_fd)
        if create:
            parent_fd = _open_dir(build_fd, "displays", label + "/displays")
        else:
            parent_fd = _open_existing_dir(build_fd, "displays", label + "/displays")
        if parent_fd is None:
            yield False
            return
        fds.append(parent_fd)
        name = f".{display_id}.lock"
        try:
            info = os.lstat(name, dir_fd=parent_fd)
            if not stat.S_ISREG(info.st_mode):
                raise RenderError(f"refusing to use {label}/displays/{name}: not a regular file")
        except FileNotFoundError:
            pass
        try:
            lock_fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, FILE_MODE,
                              dir_fd=parent_fd)
        except OSError as exc:
            raise RenderError(f"cannot open the build lock {name}: {exc.strerror}") from exc
        owner = mqtt_auth._owner((0, 0))
        if owner:
            os.fchown(lock_fd, *owner)
        timeout = ctx.build_lock_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        told = False
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
            if time.monotonic() >= deadline:
                raise RenderError(f"another build/flash for {display_id} is running (waited "
                                  f"{timeout:g} s); try again when it has finished")
            if not told:
                notify(f"another build/flash for {display_id} is running; waiting up to "
                       f"{timeout:g} s for it to finish")
                told = True
            time.sleep(0.1)
        yield True
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)  # closing releases the flock
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


PROJECT_DIR = "project"
PROJECT_NEW = "project.new"
PROJECT_PREV = "project.prev"
# Everything the per-display directory legitimately holds in the current layout.
# firmware/, firmware.prev/ and firmware.new/ belong to firmware.py and are never touched here.
DISPLAY_DIR_ENTRIES = (PROJECT_DIR, PROJECT_NEW, PROJECT_PREV, "firmware", "firmware.prev",
                       "firmware.new")


def write_project(ctx, rendered: Rendered) -> Path:
    """Write the ESPHome project into ``$ROOT/build/displays/<id>/project/`` and return that path.

    The caller must hold the build lock (``build_lock``). The tree is first written next
    to its final place as ``project.new`` and then swapped in; the previous project stays
    as ``project.prev``. ``firmware/`` and ``firmware.prev/`` live beside ``project/`` and
    are never touched, so rendering again cannot discard a finished build.
    """
    display_id = rendered.display_id
    root = ctx.root
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise RenderError(f"cannot open {root}: {exc.strerror}") from exc
    fds = [root_fd]
    try:
        build_fd = _open_dir(root_fd, BUILD_DIR_NAME, str(root / BUILD_DIR_NAME))
        fds.append(build_fd)
        parent_fd = _open_dir(build_fd, "displays", str(root / BUILD_DIR_NAME / "displays"))
        fds.append(parent_fd)
        container = root / BUILD_DIR_NAME / "displays" / display_id
        display_fd = _open_dir(parent_fd, display_id, str(container))
        fds.append(display_fd)
        migrate_legacy_layout(parent_fd, display_fd, display_id)
        _remove_tree(display_fd, PROJECT_NEW)
        owner = mqtt_auth._owner((0, 0))
        top_fd = _open_dir(display_fd, PROJECT_NEW, str(container / PROJECT_NEW))
        fds.append(top_fd)
        try:
            _write_files(top_fd, rendered.files, display_id, owner)
        except BaseException:
            _remove_tree(display_fd, PROJECT_NEW)
            raise
        os.close(fds.pop())
        _remove_tree(display_fd, PROJECT_PREV)
        if _exists(display_fd, PROJECT_DIR):
            os.rename(PROJECT_DIR, PROJECT_PREV, src_dir_fd=display_fd, dst_dir_fd=display_fd)
        os.rename(PROJECT_NEW, PROJECT_DIR, src_dir_fd=display_fd, dst_dir_fd=display_fd)
        return container / PROJECT_DIR
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


def _displays_dir(ctx):
    """``build/displays`` as an fd, or None when it does not exist (nothing is created)."""
    try:
        root_fd = os.open(ctx.root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise RenderError(f"cannot open {ctx.root}: {exc.strerror}") from exc
    try:
        label = str(ctx.root / BUILD_DIR_NAME)
        build_fd = _open_existing_dir(root_fd, BUILD_DIR_NAME, label)
        if build_fd is None:
            return None
        try:
            return _open_existing_dir(build_fd, "displays", label + "/displays")
        finally:
            os.close(build_fd)
    finally:
        os.close(root_fd)


def remove_build_tree(ctx, display_id: str) -> bool:
    """Delete everything under build/displays for one display; True if anything was there.

    Exactly ``<id>`` (project, project.prev, stored firmware, scratch) and the old-layout
    siblings ``<id>.new`` and ``<id>.prev``; ids cannot contain a dot, so these names can
    never belong to another display. No symlink is followed. The caller holds the build lock.
    """
    parent_fd = _displays_dir(ctx)
    if parent_fd is None:
        return False
    try:
        found = False
        for name in (display_id, f"{display_id}.new", f"{display_id}.prev"):
            if _exists(parent_fd, name):
                _remove_tree(parent_fd, name)
                found = True
        return found
    finally:
        os.close(parent_fd)


LEGACY_SIBLING_SUFFIXES = (".new", ".prev")


def _scrub_ids(parent_fd: int) -> list:
    """Display ids that own at least one entry in ``build/displays``.

    ``<id>``, and the old-layout siblings ``<id>.new`` and ``<id>.prev`` (ids cannot
    contain a dot, so these names never belong to another display). Lock files and
    everything else are ignored.
    """
    ids = set()
    for name in os.listdir(parent_fd):
        base = name
        for suffix in LEGACY_SIBLING_SUFFIXES:
            if name.endswith(suffix):
                base = name[:-len(suffix)]
        if schema.DISPLAY_ID.fullmatch(base):
            ids.add(base)
    return sorted(ids)


def scrub_project_secrets(ctx, notify=None, timeout: float = 2.0) -> tuple[list, list]:
    """Delete ``secrets.yaml`` from every rendered project tree (not the stored firmware).

    Covers, for every valid display id, ``<id>/project``, ``project.new`` and
    ``project.prev``, the old layout's ``<id>/secrets.yaml`` and its ``<id>.new`` and
    ``<id>.prev`` siblings (also the ``project*`` directories inside them). Each display
    is handled under its own build lock and no symlink is followed. Used by
    ``wifi --clear``. Returns (cleaned display ids, ids skipped because a build or
    flash held their lock for longer than ``timeout`` seconds).
    """
    parent_fd = _displays_dir(ctx)
    if parent_fd is None:
        return [], []
    try:
        names = _scrub_ids(parent_fd)
    finally:
        os.close(parent_fd)
    cleaned, skipped = [], []
    for display_id in names:
        try:
            with build_lock(ctx, display_id, create=False, notify=notify, timeout=timeout) as held:
                if held and _scrub_one(ctx, display_id):
                    cleaned.append(display_id)
        except RenderError:
            skipped.append(display_id)
    return cleaned, skipped


def _open_plain_dir(parent_fd: int, name: str, label: str):
    """Open an existing real directory; None when absent, a symlink or not a directory."""
    try:
        info = os.lstat(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None  # never followed, never touched
    return _open_dir(parent_fd, name, label)


def _unlink_secrets(dir_fd: int) -> bool:
    try:
        os.unlink("secrets.yaml", dir_fd=dir_fd)
        return True
    except FileNotFoundError:
        return False


def _scrub_tree(top_fd: int, label: str) -> bool:
    """secrets.yaml directly in ``top_fd`` and in its project/project.new/project.prev."""
    removed = _unlink_secrets(top_fd)
    for sub in (PROJECT_DIR, PROJECT_NEW, PROJECT_PREV):
        sub_fd = _open_plain_dir(top_fd, sub, f"{label}/{sub}")
        if sub_fd is not None:
            try:
                removed = _unlink_secrets(sub_fd) or removed
            finally:
                os.close(sub_fd)
    return removed


def _scrub_one(ctx, display_id: str) -> bool:
    parent_fd = _displays_dir(ctx)
    if parent_fd is None:
        return False
    removed = False
    try:
        for name in (display_id, *(display_id + suffix for suffix in LEGACY_SIBLING_SUFFIXES)):
            top_fd = _open_plain_dir(parent_fd, name, name)
            if top_fd is None:
                continue
            try:
                removed = _scrub_tree(top_fd, name) or removed
            finally:
                os.close(top_fd)
    finally:
        os.close(parent_fd)
    return removed


def migrate_legacy_layout(parent_fd: int, display_fd: int, display_id: str) -> bool:
    """Move an old-layout build directory to the current one; return True if it migrated.

    Before, the project files sat directly in ``build/displays/<id>/`` next to
    ``firmware/`` and ``firmware.prev/``, with ``<id>.new`` and ``<id>.prev`` as siblings.
    ``firmware/`` and ``firmware.prev/`` already are where they belong, so they stay put
    (nothing is copied or renamed, so a crash cannot lose them). Everything else in the
    old directory, and the old siblings, is the superseded project with its plaintext
    secrets: it is deleted and the caller renders the project again. ``<id>.yaml`` goes
    last, so an interrupted migration is recognised and finished by the next run.
    The caller must hold the build lock.
    """
    migrated = False
    for stale in (f"{display_id}.new", f"{display_id}.prev"):
        if _exists(parent_fd, stale):
            _remove_tree(parent_fd, stale)
            migrated = True
    marker = f"{display_id}.yaml"
    if _exists(display_fd, marker):
        for name in sorted(os.listdir(display_fd)):
            if name not in DISPLAY_DIR_ENTRIES and name != marker:
                _remove_tree(display_fd, name)
        _remove_tree(display_fd, marker)
        migrated = True
    return migrated


def _exists(parent_fd: int, name: str) -> bool:
    try:
        os.lstat(name, dir_fd=parent_fd)
        return True
    except FileNotFoundError:
        return False


def _remove_tree(parent_fd: int, name: str) -> None:
    """Remove a leftover directory (or file/symlink) by name without following links."""
    try:
        info = os.lstat(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        if shutil.rmtree.avoids_symlink_attacks:
            shutil.rmtree(name, dir_fd=parent_fd)
        else:  # pragma: no cover - platforms without fd-based rmtree
            raise RenderError("this platform cannot remove directories safely")
    else:
        os.unlink(name, dir_fd=parent_fd)


def _write_files(top_fd: int, files: dict, display_id: str, owner) -> None:
    open_dirs = {"": top_fd}
    extra = []
    try:
        for path in sorted(files):
            relative = path[len(display_id) + 1:]
            parts = relative.split("/")
            current = ""
            for part in parts[:-1]:
                nxt = f"{current}/{part}" if current else part
                if nxt not in open_dirs:
                    fd = _open_dir(open_dirs[current], part, nxt)
                    open_dirs[nxt] = fd
                    extra.append(fd)
                current = nxt
            mqtt_auth._write_new(parts[-1], files[path], open_dirs[current], FILE_MODE, owner)
    finally:
        for fd in extra:
            os.close(fd)


# --- archive -----------------------------------------------------------------------

def make_archive(rendered: Rendered) -> bytes:
    """Deterministic tar.gz of the project: sorted, fixed mtime, uid/gid 0, modes 0700/0600."""
    display_id = rendered.display_id
    directories = {display_id}
    for path in rendered.files:
        parts = path.split("/")
        for i in range(1, len(parts)):
            directories.add("/".join(parts[:i]))
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        entries = [(d, None) for d in directories] + sorted(rendered.files.items())
        for name, data in sorted(entries, key=lambda item: item[0] + ("/" if item[1] is None else "")):
            info = tarfile.TarInfo(name)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if data is None:
                info.type = tarfile.DIRTYPE
                info.mode = DIR_MODE
                tar.addfile(info)
            else:
                info.size = len(data)
                info.mode = FILE_MODE
                tar.addfile(info, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", filename="", mtime=0, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


# --- commands ----------------------------------------------------------------------

def render(ctx, display_id: str, allow_missing_wifi: bool = False, warn=None) -> Path:
    lifecycle._require_root(ctx, "render")
    with build_lock(ctx, display_id):
        rendered = collect(ctx, display_id, allow_missing_wifi)
        path = write_project(ctx, rendered)
    for message in rendered.warnings:
        (warn or ctx.out)(f"warning: {message}")
    ctx.out(str(path))
    return path


def _may_chown() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def sudo_owner(environ) -> tuple[int, int] | None:
    """(uid, gid) of the user who ran ``sudo``, when SUDO_UID/SUDO_GID name a real account."""
    uid_text, gid_text = environ.get("SUDO_UID", ""), environ.get("SUDO_GID", "")
    for text in (uid_text, gid_text):
        if not (text.isascii() and text.isdigit() and len(text) <= 10):
            return None
    uid, gid = int(uid_text), int(gid_text)
    if uid == 0:
        return None  # plain root login: nothing to hand over
    try:
        pwd.getpwuid(uid)
        grp.getgrgid(gid)
    except (KeyError, OverflowError):
        return None
    return uid, gid


def export(ctx, display_id: str, output: str | None, to_stdout: bool, warn=None, stdout=None,
           allow_missing_wifi: bool = False, environ=None) -> None:
    lifecycle._require_root(ctx, "export")
    stream = stdout if stdout is not None else sys.stdout
    if to_stdout:
        if output:
            raise RenderError("use either --output or --stdout, not both", 2)
        if stream.isatty():
            raise RenderError("refusing to write the archive to a terminal; redirect stdout to a "
                              "file or use --output FILE", 2)
    elif not output:
        raise RenderError("choose --output FILE or --stdout", 2)
    with build_lock(ctx, display_id):
        rendered = collect(ctx, display_id, allow_missing_wifi)
        write_project(ctx, rendered)
    archive = make_archive(rendered)
    emit = warn or (lambda text: print(text, file=sys.stderr))
    for message in rendered.warnings:
        emit(f"warning: {message}")
    emit(WARNING_ARCHIVE_SECRETS)
    if to_stdout:
        buffer = getattr(stream, "buffer", stream)
        buffer.write(archive)
        buffer.flush()
        return
    directory, leaf = os.path.split(output)
    if leaf in ("", ".", ".."):
        raise RenderError(f"--output {output!r} must name a file, not a directory", 2)
    # The checked directory is the one the file is created in: open it without following a
    # symlink as its last component, and create the file relative to that descriptor.
    try:
        dir_fd = os.open(directory or ".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise RenderError(f"cannot use the directory of {output}: {exc.strerror} (a symbolic "
                          "link is refused)", 2) from exc
    try:
        _export_to_dir(dir_fd, leaf, output, archive, emit, environ)
    finally:
        os.close(dir_fd)


def _handover_allowed(info, owner) -> str | None:
    """None when the file may be given to ``owner``; otherwise why it must stay root-owned.

    Root creates the file, so handing it over is only safe in a directory that already
    belongs to that user and that nobody else can write to: otherwise a user allowed to run
    only vesselstackctl could pick any directory (say /etc/cron.d) and end up owning a file
    there, or others could replace the file afterwards.
    """
    if info.st_uid != owner[0]:
        return f"its directory is owned by uid {info.st_uid}, not uid {owner[0]}"
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return "its directory is writable by group or others"
    return None


def _fstat_dir(fd: int):
    return os.fstat(fd)


def _export_to_dir(dir_fd: int, leaf: str, output: str, archive: bytes, emit, environ) -> None:
    try:
        fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE,
                     dir_fd=dir_fd)
    except FileExistsError:
        raise RenderError(f"{output} already exists; refusing to overwrite it", 2) from None
    except OSError as exc:
        raise RenderError(f"cannot create {output}: {exc.strerror}") from exc
    try:
        os.fchmod(fd, FILE_MODE)
        # Under sudo the file would belong to root in the user's directory: hand it to the
        # invoking user (still mode 0600) only when that directory is theirs alone.
        owner = sudo_owner(os.environ if environ is None else environ)
        owned = ""
        if owner and _may_chown():
            refusal = _handover_allowed(_fstat_dir(dir_fd), owner)
            if refusal:
                emit(f"note: {output} stays owned by root (mode 0600) because {refusal}. Take it "
                     f'with: sudo chown "$USER" {output}   (or write with --stdout inside '
                     "(umask 077; ...) so your shell creates the file)")
            else:
                try:
                    os.fchown(fd, *owner)
                    owned = f", owned by uid {owner[0]}"
                except OSError as exc:
                    emit(f"warning: could not give {output} to uid {owner[0]}: {exc.strerror}")
        view = memoryview(archive)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(leaf, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    os.close(fd)
    emit(f"Wrote {output} (mode 0600{owned}, {len(archive)} bytes)")
