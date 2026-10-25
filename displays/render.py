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
FONT_SIZES = {"grid": 56, "single": 96, "small": 20}
TAP_TIME = "0ms"
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


def _small_glyphs(config: dict, display: dict) -> str:
    chars = {" "}
    for iid in used_instruments(display):
        inst = config["instruments"][iid]
        chars.update(inst["label"])
        chars.update(inst["unit"])
    return "".join(sorted(chars))


def _tile_widgets(ind: str, pi: int, iid: str, inst: dict, value_font: str) -> list:
    return [
        f"{ind}- label: {{text: {lit(inst['label'])}, text_font: font_small, text_color: 0x9AA5B1}}",
        f"{ind}- label: {{id: p{pi}_{iid}_value, text: {yq(BLANK_VALUE)}, text_font: {value_font}, "
        "text_color: 0xFFFFFF}",
        f"{ind}- label: {{text: {lit(inst['unit'])}, text_font: font_small, text_color: 0x9AA5B1}}",
    ]


FLEX_COLUMN = "layout: {type: FLEX, flex_flow: COLUMN, flex_align_main: CENTER, flex_align_cross: CENTER}"


def _page_lines(config: dict, registry_board: dict, display: dict, pi: int, page: dict) -> list:
    n_pages = len(display["pages"])
    cw, ch = registry_board["content_width"], registry_board["content_height"]
    layout = page["layout"]
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
        lines += _tile_widgets("              ", pi, iid, config["instruments"][iid], "font_value_single")
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
            lines += [
                "              - obj:",
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
                                   "font_value_grid")
    else:
        raise RenderError(f"layout {layout!r} is not supported by the firmware generator yet", 2)
    if n_pages > 1:
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
    if "grid-2x2" in layouts:
        fonts.append(("font_value_grid", "bold", FONT_SIZES["grid"], VALUE_GLYPHS))
    if "single" in layouts:
        fonts.append(("font_value_single", "bold", FONT_SIZES["single"], VALUE_GLYPHS))
    fonts.append(("font_small", "regular", FONT_SIZES["small"], _small_glyphs(config, display)))
    lines = ["font:"]
    for fid, weight, size, glyphs in fonts:
        lines += [f"  - file: fonts/{FONT_FILES[weight]}", f"    id: {fid}", f"    size: {size}",
                  "    bpp: 4", f"    glyphs: {lit(glyphs)}"]
    return lines


def _json_handler(iid: str, decimals: int) -> list:
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
        "            }",
        "        - script.execute: render_tiles",
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
                  f'  - {{id: ins_{iid}_seen, type: uint32_t, initial_value: "0"}}',
                  f"  - {{id: ins_{iid}_val, type: std::string, initial_value: '\"{BLANK_VALUE}\"'}}"]
    lines += ["", *_fonts_lines(display, config), "", "mqtt:", "  on_message:",
              f"    - topic: {topics.BRIDGE_STATUS_TOPIC}", "      then:",
              '        - lambda: \'id(bridge_online) = (x == "online");\'',
              "        - script.execute: render_tiles", "  on_json_message:"]
    for iid in used:
        lines += _json_handler(iid, config["instruments"][iid]["precision"])
    lines += ["  on_disconnect:", "    - lambda: 'id(bridge_online) = false;'",
              "    - script.execute: render_tiles", "", "script:", "  - id: render_tiles",
              "    mode: restart", "    then:"]
    for pi, page in enumerate(display["pages"]):
        for iid in page["items"]:
            limit_ms = stale_threshold_s(config["instruments"][iid], heartbeat) * 1000
            lines += [
                "      - lvgl.label.update:",
                f"          id: p{pi}_{iid}_value",
                "          text: !lambda |-",
                "            bool fresh = id(bridge_online) && mqtt::global_mqtt_client->is_connected()"
                f" && id(ins_{iid}_ok) && (millis() - id(ins_{iid}_seen)) < {limit_ms}UL;",
                f"            return fresh ? id(ins_{iid}_val) : std::string(\"{BLANK_VALUE}\");",
            ]
    lines += ["", "interval:", "  - interval: 1s", "    then:", "      - script.execute: render_tiles",
              "", "lvgl:", "  displays: [board_display]", "  touchscreens: [board_touch]",
              "  buffer_size: 100%", "  style_definitions:", "    - id: tile_style",
              "      bg_color: 0x101820", "      bg_opa: COVER", "      border_width: 0",
              "      radius: 14", "      pad_all: 4", "  pages:"]
    for pi, page in enumerate(display["pages"]):
        lines += _page_lines(config, board, display, pi, page)
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
