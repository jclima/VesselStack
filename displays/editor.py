"""Page-editing backend of the Control Panel "Displays" tab (stdlib only).

Every function here is reached through a fixed ``vesselstackctl display ...`` command and returns a
plain dict that ``displayctl`` prints as JSON: ``catalog``, ``show``, ``set-pages``, ``history``,
``restore-config`` and ``serial-ports``. Nothing here prints a secret: ``show`` reports only whether a
password or Wi-Fi network is stored.

Writers follow the lifecycle rules: the per-display build lock is taken first and NON-BLOCKING (a
running build or flash makes the command refuse, exit status 4), then the configuration lock; the
revision (sha256 of the ``displays.json`` bytes) must match (exit status 3); the previous file is copied
to ``config/displays-backups/`` (directory 0700, files 0600, the newest 20 kept) before every change;
the new file is validated and written atomically by ``lifecycle.write_config``.
"""
from __future__ import annotations

import contextlib
import copy
import datetime
import hashlib
import ipaddress
import json
import os
import re
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog as catalog_mod  # noqa: E402
import firmware  # noqa: E402
import lifecycle  # noqa: E402
import mqtt_auth  # noqa: E402
import render  # noqa: E402
import schema  # noqa: E402
import topics  # noqa: E402

BACKUP_DIR = "displays-backups"
BACKUP_KEEP = 20
BACKUP_MODE = 0o600
BACKUP_DIR_MODE = 0o700
STAMP_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z(-[0-9]{1,3})?$")
BACKUP_FILE_RE = re.compile(r"^displays-([0-9]{8}T[0-9]{6}Z(?:-[0-9]{1,3})?)\.json$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STDIN_MAX_BYTES = 1024 * 1024
BRIDGE_UNIT = "vesselstack-displays-bridge.service"
SERIAL_BY_ID = Path(firmware.SERIAL_BY_ID_PREFIX)
LAST_FLASH_NAME = "last-flash.json"    # written by the flash command (a later release); absent is normal
LAST_FLASH_KEYS = ("at", "method", "ok", "firmware_built_at", "error")   # nothing else is ever passed on
SET_PAGES_KEYS = {"pages", "theme", "new_instruments", "new_notifications", "instrument_edits"}
EDITABLE_INSTRUMENT_KEYS = schema.INSTRUMENT_KEYS - {"path"}
MAX_ERRORS = 50

EXIT_OK, EXIT_FAILED, EXIT_INVALID, EXIT_REVISION, EXIT_BUSY = 0, 1, 2, 3, 4


class EditError(lifecycle.LifecycleError):
    """A failure with a JSON body: ``errors`` is the list the Control Panel shows."""

    def __init__(self, errors, code: int = EXIT_INVALID, **extra):
        errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__(errors[0] if errors else "failed", code)
        self.errors = errors[:MAX_ERRORS]
        self.extra = extra


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def revision_of(text: str) -> str:
    """The configuration revision: sha256 of the exact ``displays.json`` bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- reading --------------------------------------------------------------------------------

def _read_current(cfg_dir: lifecycle.ConfigDir, registry: dict) -> tuple:
    """``(text, config)`` of displays.json, which must exist and validate."""
    text = cfg_dir.read_text(lifecycle.DISPLAYS_NAME)
    if text is None:
        lifecycle.read_config(cfg_dir)   # raises the usual "does not exist" message
    config = lifecycle._parse_json(text, lifecycle.DISPLAYS_NAME)
    errors = schema.validate_config(config, registry)
    if errors:
        raise EditError([f"{lifecycle.DISPLAYS_NAME} is already invalid ({errors[0]}; {len(errors)} "
                         "error(s)); fix it or restore a backup. Nothing was changed"], EXIT_INVALID)
    return text, config


def _secrets_or_none(cfg_dir: lifecycle.ConfigDir):
    try:
        return lifecycle.read_secrets(cfg_dir)
    except PermissionError:
        return None


def alarm_sources(config: dict) -> set:
    """Ids of the items that count in the boat-wide alarm summary (see schema.counts_for_alarm_summary)."""
    out = set()
    for iid, inst in (config.get("instruments") or {}).items():
        if isinstance(inst, dict) and inst.get("alarm", "thresholds" in inst) is True:
            out.add(iid)
    for section in ("notifications", "ha"):
        for iid, item in (config.get(section) or {}).items():
            if isinstance(item, dict) and item.get("alarm", True) is True:
                out.add(iid)
    return out


def _used_on(config: dict) -> dict:
    used: dict = {}
    for did, display in config["displays"].items():
        for page in display.get("pages", []):
            for item in page.get("items", []):
                if did not in used.setdefault(item, []):
                    used[item].append(did)
    return used


# --- catalog ----------------------------------------------------------------------------------

def catalog(ctx: lifecycle.Context, live: bool = False) -> dict:
    """The merged data list for the Displays tab (``catalog [--live] --json``)."""
    registry = lifecycle.load_registry()
    try:
        data = catalog_mod.load_catalog()
    except (OSError, ValueError) as exc:
        raise EditError([f"cannot load the display data catalog: {exc}"], EXIT_FAILED) from exc
    with lifecycle.ConfigDir(ctx) as cfg_dir:
        _text, config = _read_current(cfg_dir, registry)
    units = catalog_mod.normalize_units(ctx.env.get("BOAT_UNITS"))
    result = None
    if live:
        result = catalog_mod.fetch_live(ctx.env.get("SIGNALK_URL", ""), ctx.env.get("DISPLAYS_SIGNALK_TOKEN", ""))
    rows = catalog_mod.merged_catalog(data, result, units, config, registry)
    groups = list(dict.fromkeys(row["group"] for row in rows))
    return {
        "schema": 1, "units": units, "heartbeat_s": catalog_mod._heartbeat(config),
        "live": {"requested": live, "ok": bool(result and result["ok"]) if live else None,
                 "error": result["error"] if result else None,
                 "truncated": bool(result and result.get("truncated")),
                 "leaf_count": len(result["leaves"]) if result else 0,
                 "notification_count": len(result["notifications"]) if result else 0},
        "groups": groups, "rows": rows,
    }


# --- show ---------------------------------------------------------------------------------------

def _is_loopback(address: str) -> bool:
    text = address.strip().strip("[]").lower()
    if text in ("", "localhost"):
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _run(ctx: lifecycle.Context, argv: list):
    try:
        return ctx.runner(argv)
    except (OSError, ValueError):
        return None


def _display_dir_state(ctx: lifecycle.Context, display_id: str) -> str:
    """``present``, ``absent`` or ``unreadable`` for build/displays/ID (a non-root user cannot look)."""
    try:
        os.stat(ctx.root / render.BUILD_DIR_NAME / "displays" / display_id)
    except FileNotFoundError:
        return "absent"
    except PermissionError:
        return "unreadable"
    except OSError:
        return "absent"
    return "present"


def _credentials_fingerprint(config: dict, display_id: str, secret_data) -> str | None:
    """sha256 of the secrets.yaml the stored credentials would render, or None when they are incomplete."""
    if not isinstance(secret_data, dict):
        return None
    wifi = secret_data.get("wifi")
    entry = (secret_data.get("displays") or {}).get(display_id)
    if not (isinstance(wifi, dict) and wifi.get("ssid") and isinstance(wifi.get("password"), str)
            and isinstance(entry, dict)):
        return None
    needed = ["mqtt_password", "ota_password"]
    if config["displays"][display_id].get("home_assistant_api", False):
        needed.append("api_encryption_key")
    if not all(isinstance(entry.get(key), str) and entry[key] for key in needed):
        return None
    return firmware.sha256_hex(render.secrets_yaml(config, display_id, wifi, entry).encode("utf-8"))


def _firmware_state(ctx: lifecycle.Context, display_id: str, config: dict, registry: dict, secret_data) -> tuple:
    """``(firmware, last_flash)`` blocks of ``show``. No credential, hash or path is reported."""
    state = _display_dir_state(ctx, display_id)
    base = {"status": "none", "built_at_utc": None, "esphome_version": None, "reasons": [],
            "credentials_checked": False, "has_previous": False}
    if state == "unreadable":
        return {**base, "status": "unknown", "reasons": ["the build folder is root-only: run as root"]}, None
    if state == "absent":
        return base, None
    manifest = None
    last_flash = None
    try:
        with firmware.DisplayDir(ctx.root, display_id, create=False) as dd:
            label = str(dd.path)
            try:
                manifest = json.loads(firmware._read_at(dd.display_fd, [firmware.FIRMWARE_DIR,
                                                                      firmware.MANIFEST_NAME], label))
            except (render.RenderError, ValueError):
                manifest = None
            base["has_previous"] = render._exists(dd.display_fd, firmware.PREVIOUS_DIR)
            try:
                last_flash = json.loads(firmware._read_at(dd.display_fd, [LAST_FLASH_NAME], label))
            except (render.RenderError, ValueError, OSError):
                last_flash = None
    except (render.RenderError, OSError):
        return base, None
    if isinstance(last_flash, dict):
        last_flash = {key: last_flash[key] for key in LAST_FLASH_KEYS
                      if isinstance(last_flash.get(key), (str, int, bool)) and len(str(last_flash[key])) <= 200}
    else:
        last_flash = None
    if not isinstance(manifest, dict):
        return base, last_flash
    base["built_at_utc"] = manifest.get("built_at_utc") if isinstance(manifest.get("built_at_utc"), str) else None
    base["esphome_version"] = manifest.get("esphome_version") if isinstance(
        manifest.get("esphome_version"), str) else None
    reasons = []
    try:
        current = firmware.source_hash(render.source_files(config, registry, display_id), display_id)
        if manifest.get("source_sha256") != current:
            reasons.append("the configuration changed since the firmware was built")
    except render.RenderError as exc:
        reasons.append(f"the configuration cannot be rendered ({exc})")
    fingerprint = _credentials_fingerprint(config, display_id, secret_data)
    if fingerprint is not None:
        base["credentials_checked"] = True
        if manifest.get("secrets_sha256") != fingerprint:
            reasons.append("the credentials changed since the firmware was built")
    base["status"] = "outdated" if reasons else "current"
    base["reasons"] = reasons
    return base, last_flash


def _readiness(ctx: lifecycle.Context, display_id: str, config: dict, registry: dict, secret_data) -> dict:
    entry = (secret_data or {}).get("displays", {}).get(display_id) if secret_data else None
    wifi = (secret_data or {}).get("wifi") if secret_data else None
    bind = (ctx.env.get("VESSELSTACK_DATA_BIND_ADDRESS") or "127.0.0.1")
    non_loopback = not _is_loopback(bind)
    docker = _run(ctx, ["docker", "info", "--format", "{{.ServerVersion}}"])
    docker_ok = bool(docker and docker.returncode == 0)
    image_present = cache_present = False
    if docker_ok:
        try:
            image_present = firmware._image_present(ctx, render.pinned_image())
            cache_present = firmware._volume_present(ctx)
        except (render.RenderError, OSError):
            pass
    try:
        free, _where = firmware.free_bytes(ctx)
    except (render.RenderError, OSError):
        try:
            info = os.statvfs(ctx.root)
            free = info.f_bavail * info.f_frsize
        except OSError:
            free = None
    fw, last_flash = _firmware_state(ctx, display_id, config, registry, secret_data)
    readiness = {
        "displays_enabled": ctx.env.get("DISPLAYS_ENABLE", "false") == "true",
        "secrets_readable": secret_data is not None,
        "mqtt_password_stored": (bool(isinstance(entry, dict) and entry.get("mqtt_password"))
                                 if secret_data is not None else None),
        "wifi_stored": (bool(isinstance(wifi, dict) and wifi.get("ssid")) if secret_data is not None else None),
        "data_bind_non_loopback": non_loopback,
        "data_bind_hint": None if non_loopback else (
            "MQTT only listens on this device (VESSELSTACK_DATA_BIND_ADDRESS is a loopback address), so a "
            "display on the network cannot connect. Use 'sudo vesselstackctl lan all --yes' to let it connect "
            "(it also opens InfluxDB and Prometheus; see the LAN section of the README)."),
        "docker_available": docker_ok,
        "esphome_image_present": image_present,
        "toolchain_cache_present": cache_present,
        "free_disk_bytes": free,
        "min_free_bytes_first_build": firmware.MIN_FREE_FIRST_BUILD,
        "min_free_bytes_warm_build": firmware.MIN_FREE_WARM_BUILD,
        "firmware": fw,
        "last_flash": last_flash,
    }
    return readiness


def _board_block(board_id: str, board: dict, registry: dict) -> tuple:
    specs = schema.layout_specs(registry)
    layouts = {}
    for name in board["layouts"]:
        spec = specs.get(name)
        if spec is None or name not in schema.SUPPORTED_LAYOUTS or board["shape"] not in spec["shapes"]:
            continue
        info = {"capacity": spec["capacity"], "min_items": spec["min_items"], "types": spec["types"],
                "needs_range": name in schema.RANGE_LAYOUTS}
        if "slots" in spec:
            info["slots"] = spec["slots"]
        layouts[name] = info
    block = {"id": board_id, "name": board["name"], "width": board["width"], "height": board["height"],
             "shape": board["shape"], "safe_inset": board["safe_inset"],
             "content_width": board["content_width"], "content_height": board["content_height"],
             "layouts": list(layouts)}
    if "ring" in board:
        block["ring"] = dict(board["ring"])
    return block, layouts


def list_displays(ctx: lifecycle.Context) -> dict:
    """The configured displays, for the Control Panel (``list --json``): ids, names and boards only."""
    registry = lifecycle.load_registry()
    with lifecycle.ConfigDir(ctx) as cfg_dir:
        if cfg_dir.read_text(lifecycle.DISPLAYS_NAME) is None:
            return {"schema": 1, "displays": []}
        _text, config = _read_current(cfg_dir, registry)
    return {"schema": 1, "displays": [
        {"id": display_id, "name": entry["name"], "board": entry["board"], "pages": len(entry["pages"])}
        for display_id, entry in config["displays"].items()]}


def show(ctx: lifecycle.Context, display_id: str) -> dict:
    """Everything the Displays tab needs to edit one display (``show ID --json``)."""
    registry = lifecycle.load_registry()
    with lifecycle.ConfigDir(ctx) as cfg_dir:
        text, config = _read_current(cfg_dir, registry)
        secret_data = _secrets_or_none(cfg_dir)
    display = lifecycle._display_or_error(config, display_id)
    board_id = display["board"]
    board = registry["boards"][board_id]
    board_block, layouts = _board_block(board_id, board, registry)
    ui = display.get("ui") if isinstance(display.get("ui"), dict) else {}
    header = schema.ui_header_enabled(ui)
    previews = [render.preview_geometry(board, page["layout"], len(page["items"]), header)
                for page in display["pages"]]
    used = _used_on(config)
    themes = schema.load_themes()["themes"]
    return {
        "schema": 1,
        "revision": revision_of(text),
        "display": {"id": display_id, "name": display["name"], "board": board_id,
                    "home_assistant_api": bool(display.get("home_assistant_api", False)),
                    "pages": copy.deepcopy(display["pages"]), "ui": copy.deepcopy(ui)},
        "theme": ui.get("theme", "classic"),
        "themes": [{"id": name, "description": themes[name]["description"]} for name in schema.theme_ids()],
        "board": board_block,
        "layouts": layouts,
        "preview": previews,
        "heartbeat_s": catalog_mod._heartbeat(config),
        "instruments": {iid: dict(inst, used_on=used.get(iid, [])) for iid, inst in config["instruments"].items()},
        "notifications": {nid: dict(note, used_on=used.get(nid, []))
                          for nid, note in (config.get("notifications") or {}).items()},
        "ha": {hid: {"label": item["label"], "kind": item["kind"], "used_on": used.get(hid, [])}
               for hid, item in (config.get("ha") or {}).items()},
        "alarm_sources": sorted(alarm_sources(config)),
        "readiness": _readiness(ctx, display_id, config, registry, secret_data),
    }


# --- backups ---------------------------------------------------------------------------------

def _backup_dir(cfg_dir: lifecycle.ConfigDir, create: bool):
    """No-follow fd of config/displays-backups (0700), or None when absent and not created."""
    try:
        info = os.lstat(BACKUP_DIR, dir_fd=cfg_dir.fd)
    except FileNotFoundError:
        if not create:
            return None
        os.mkdir(BACKUP_DIR, BACKUP_DIR_MODE, dir_fd=cfg_dir.fd)
    else:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise EditError([f"refusing to use {cfg_dir.ctx.config_dir / BACKUP_DIR}: not a plain directory"],
                            EXIT_FAILED)
    fd = os.open(BACKUP_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cfg_dir.fd)
    try:
        owner = mqtt_auth._owner(lifecycle.CONFIG_OWNER)
        if stat.S_IMODE(os.fstat(fd).st_mode) != BACKUP_DIR_MODE:
            os.fchmod(fd, BACKUP_DIR_MODE)
        if owner:
            os.fchown(fd, *owner)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _stamp_key(stamp: str) -> tuple:
    base, _, suffix = stamp.partition("-")
    return base, int(suffix) if suffix else 1


def _list_backups(fd: int) -> list:
    """Stamps of the backups in the directory, oldest first."""
    stamps = []
    for name in os.listdir(fd):
        match = BACKUP_FILE_RE.fullmatch(name)
        if match:
            stamps.append(match.group(1))
    return sorted(stamps, key=_stamp_key)


def _read_backup(fd: int, stamp: str) -> str | None:
    data = mqtt_auth._read_regular(f"displays-{stamp}.json", fd)
    return None if data is None else data.decode("utf-8")


def write_backup(cfg_dir: lifecycle.ConfigDir, text: str) -> str:
    """Copy ``text`` (the file about to be replaced) to a new backup and keep only the newest 20."""
    fd = _backup_dir(cfg_dir, create=True)
    try:
        base = _now().strftime("%Y%m%dT%H%M%SZ")
        # Two changes in one second get -2, -3...; always above every suffix that exists (or ever
        # existed before rotation), so a new backup is never older than the ones already kept.
        same = [_stamp_key(old)[1] for old in _list_backups(fd) if _stamp_key(old)[0] == base]
        stamp = f"{base}-{max(same) + 1}" if same else base
        mqtt_auth._write_new(f"displays-{stamp}.json", text.encode("utf-8"), fd, BACKUP_MODE,
                             mqtt_auth._owner(lifecycle.CONFIG_OWNER))
        for old in _list_backups(fd)[:-BACKUP_KEEP]:
            os.unlink(f"displays-{old}.json", dir_fd=fd)
        return stamp
    finally:
        os.close(fd)


def history(ctx: lifecycle.Context) -> dict:
    """The backups newest first (``history --json``)."""
    with lifecycle.ConfigDir(ctx) as cfg_dir:
        current_text = cfg_dir.read_text(lifecycle.DISPLAYS_NAME)
        try:
            fd = _backup_dir(cfg_dir, create=False)
        except PermissionError as exc:
            raise EditError(["the backups are root-only: run as root"], EXIT_FAILED) from exc
        entries = []
        if fd is not None:
            try:
                for stamp in reversed(_list_backups(fd)):
                    text = _read_backup(fd, stamp)
                    if text is not None:
                        entries.append({"stamp": stamp, "size": len(text.encode("utf-8")),
                                        "revision": revision_of(text)})
            finally:
                os.close(fd)
    return {"schema": 1, "keep": BACKUP_KEEP, "backups": entries,
            "current_revision": revision_of(current_text) if current_text is not None else None}


# --- writers -----------------------------------------------------------------------------------

@contextlib.contextmanager
def _build_locks(ctx: lifecycle.Context, display_ids):
    """Non-blocking per-display build locks (a running build or flash makes us refuse, exit 4)."""
    with contextlib.ExitStack() as stack:
        for display_id in sorted(set(display_ids)):
            try:
                stack.enter_context(render.build_lock(ctx, display_id, create=False, timeout=0))
            except render.BuildBusy as exc:
                raise EditError([f"a build/flash for {display_id} is running; try again when it has finished"],
                                EXIT_BUSY) from exc
            except render.RenderError as exc:
                raise EditError([str(exc)], exc.code) from exc
        yield


def _bridge_view(config: dict) -> dict:
    """What the displays bridge reads at start: everything but the displays, plus whether any display
    wants the clock (``topics.clock_wanted`` looks at ``ui.header`` and ``ui.night``). Verified: the
    bridge, ``topics`` and ``ha_client`` read no other part of ``displays``."""
    view = {key: value for key, value in config.items() if key != "displays"}
    view["_clock_wanted"] = topics.clock_wanted(config)
    return view


def acl_inputs(config) -> list:
    """Everything in displays.json that the broker ACL and password files derive from.

    ``mqtt_auth.sync`` reads the configuration only through ``load_display_ids`` (mqtt_auth.py:133-146),
    which returns the sorted display ids; roles, usernames and topics come from the environment and
    constants. ``None`` (an unreadable file) never equals a list, so it counts as a difference.
    """
    if not isinstance(config, dict) or not isinstance(config.get("displays"), dict):
        return None
    return sorted(config["displays"])


def _source_hashes(config: dict, registry: dict) -> dict:
    out = {}
    for did in config["displays"]:
        try:
            out[did] = firmware.source_hash(render.source_files(config, registry, did), did)
        except render.RenderError:
            out[did] = None
    return out


def _restart_bridge(ctx: lifecycle.Context, needed: bool) -> str:
    if not needed:
        return "not_needed"
    if ctx.env.get("DISPLAYS_ENABLE", "false") != "true":
        return "skipped_displays_disabled"
    result = _run(ctx, ["systemctl", "try-restart", BRIDGE_UNIT])
    if result is None:
        return "failed: systemctl is not available; restart the displays bridge by hand"
    if result.returncode != 0:
        return "failed: systemctl try-restart exited with status %d; run 'sudo systemctl restart %s'" % (
            result.returncode, BRIDGE_UNIT)
    return "restarted"


def _apply_additions(config: dict, payload: dict, display_id: str, errors: list) -> None:
    """Edit ``config`` in place from a set-pages payload; every problem goes to ``errors``."""
    for section, key in (("instruments", "new_instruments"), ("notifications", "new_notifications")):
        block = payload.get(key)
        if block is None:
            continue
        if not isinstance(block, dict):
            errors.append(f"{key}: must be an object of id -> definition")
            continue
        for item_id, definition in block.items():
            existing = config.get(section, {}).get(item_id)
            if existing is not None and existing != definition:
                errors.append(f"{key}.{item_id}: already exists with a different definition; "
                              "use instrument_edits to change it" if section == "instruments" else
                              f"{key}.{item_id}: already exists with a different definition")
            elif existing is None:
                config.setdefault(section, {})[item_id] = definition
    edits = payload.get("instrument_edits")
    if edits is not None:
        if not isinstance(edits, dict):
            errors.append("instrument_edits: must be an object of id -> {field: value or null}")
            edits = {}
        for item_id, changes in edits.items():
            inst = config["instruments"].get(item_id)
            if inst is None:
                errors.append(f"instrument_edits.{item_id}: unknown instrument")
            elif not isinstance(changes, dict):
                errors.append(f"instrument_edits.{item_id}: must be an object")
            else:
                for field_name, value in changes.items():
                    if field_name not in EDITABLE_INSTRUMENT_KEYS:
                        errors.append(f"instrument_edits.{item_id}.{field_name}: not an editable field "
                                      f"(one of {', '.join(sorted(EDITABLE_INSTRUMENT_KEYS))})")
                    elif value is None:
                        inst.pop(field_name, None)
                    else:
                        inst[field_name] = value
    display = config["displays"][display_id]
    if "theme" in payload:
        theme = payload["theme"]
        try:
            known = schema.theme_ids()
        except (OSError, ValueError) as exc:
            errors.append(f"theme: the themes file is unusable ({exc})")
            known = []
        if theme not in known:
            errors.append(f"theme: unknown theme {theme!r}; available: {', '.join(known)}")
        else:
            ui = display.get("ui") if isinstance(display.get("ui"), dict) else {}
            if theme == "classic":
                ui.pop("theme", None)
                if not ui:
                    display.pop("ui", None)
            else:
                display.setdefault("ui", {})["theme"] = theme
    display["pages"] = payload["pages"]


def parse_payload(text: str) -> dict:
    if len(text.encode("utf-8")) > STDIN_MAX_BYTES:
        raise EditError(["the request is larger than 1 MiB"], EXIT_INVALID)
    try:
        payload = json.loads(text, object_pairs_hook=schema._no_duplicate_keys)
    except ValueError as exc:
        raise EditError([f"the request is not valid JSON: {exc}"], EXIT_INVALID) from exc
    if not isinstance(payload, dict):
        raise EditError(["the request must be a JSON object"], EXIT_INVALID)
    problems = [f"{key}: unknown key" for key in payload if key not in SET_PAGES_KEYS]
    if "pages" not in payload:
        problems.append("pages: required")
    elif not isinstance(payload["pages"], list):
        problems.append("pages: must be a list of {title, layout, items}")
    if problems:
        raise EditError(problems, EXIT_INVALID)
    return payload


def _finish_write(ctx: lifecycle.Context, cfg_dir, registry: dict, text: str, config: dict, new: dict) -> dict:
    """Back up the current file, write ``new`` and describe the outcome (config lock held)."""
    before_sources = _source_hashes(config, registry)
    stamp = write_backup(cfg_dir, text)
    lifecycle.write_config(cfg_dir, new, registry)
    new_text = lifecycle.config_text(new)
    after_sources = _source_hashes(new, registry)
    return {
        "backup": stamp, "new_text": new_text,
        "rebuild_needed": [did for did in new["displays"] if after_sources[did] != before_sources.get(did)],
        "bridge_changed": _bridge_view(config) != _bridge_view(new),
        "new_alarm_sources": sorted(alarm_sources(new) - alarm_sources(config)),
    }


def set_pages(ctx: lifecycle.Context, display_id: str, if_revision: str, payload_text: str) -> dict:
    """Replace the pages of a display (and add or edit instruments) in one validated, backed-up write."""
    if not SHA256_RE.fullmatch(if_revision or ""):
        raise EditError(["--if-revision must be the 64-character sha256 revision from 'show'"], EXIT_INVALID)
    payload = parse_payload(payload_text)
    lifecycle._require_root(ctx, "set-pages")
    registry = lifecycle.load_registry()
    with _build_locks(ctx, [display_id]), lifecycle.ConfigDir(ctx) as cfg_dir:
        cfg_dir.lock()
        text, config = _read_current(cfg_dir, registry)
        current = revision_of(text)
        if current != if_revision:
            raise EditError(["the configuration changed since it was read; reload the display and try again"],
                            EXIT_REVISION, current_revision=current)
        lifecycle._display_or_error(config, display_id)
        new = copy.deepcopy(config)
        errors: list = []
        _apply_additions(new, payload, display_id, errors)
        errors += schema.validate_config(new, registry)
        if not errors:
            errors += render.check_glyphs(new)
        if errors:
            raise EditError(errors, EXIT_INVALID)
        if new == config:
            return {"ok": True, "changed": False, "revision": current, "previous_revision": current,
                    "backup": None, "bridge_restart": "not_needed", "rebuild_needed": [],
                    "new_alarm_sources": [], "added_instruments": [], "added_notifications": []}
        outcome = _finish_write(ctx, cfg_dir, registry, text, config, new)
    restart = _restart_bridge(ctx, outcome["bridge_changed"])
    return {"ok": True, "changed": True, "revision": revision_of(outcome["new_text"]),
            "previous_revision": current, "backup": outcome["backup"], "bridge_restart": restart,
            "rebuild_needed": outcome["rebuild_needed"], "new_alarm_sources": outcome["new_alarm_sources"],
            "added_instruments": sorted(set(new["instruments"]) - set(config["instruments"])),
            "added_notifications": sorted(set(new.get("notifications") or {}) - set(config.get("notifications") or {}))}


def restore_config(ctx: lifecycle.Context, stamp: str, if_revision: str | None = None) -> dict:
    """Put a backup back as displays.json (validated; the file it replaces is backed up first)."""
    if not isinstance(stamp, str) or not STAMP_RE.fullmatch(stamp):
        raise EditError(["STAMP must look like 20260101T120000Z (see 'history')"], EXIT_INVALID)
    if if_revision is not None and not SHA256_RE.fullmatch(if_revision):
        raise EditError(["--if-revision must be a 64-character sha256 revision"], EXIT_INVALID)
    lifecycle._require_root(ctx, "restore-config")
    registry = lifecycle.load_registry()
    with lifecycle.ConfigDir(ctx) as cfg_dir:   # unlocked peek: which displays to lock
        fd = _backup_dir(cfg_dir, create=False)
        if fd is None:
            raise EditError([f"no backup {stamp}"], EXIT_INVALID)
        try:
            backup_text = _read_backup(fd, stamp)
        finally:
            os.close(fd)
        if backup_text is None:
            raise EditError([f"no backup {stamp}"], EXIT_INVALID)
        names = set()
        for source in (backup_text, cfg_dir.read_text(lifecycle.DISPLAYS_NAME)):
            with contextlib.suppress(ValueError, lifecycle.LifecycleError, TypeError, AttributeError):
                names |= set(lifecycle._parse_json(source, "displays.json").get("displays", {}))
    names = {name for name in names if isinstance(name, str) and schema.DISPLAY_ID.fullmatch(name)}
    with _build_locks(ctx, names), lifecycle.ConfigDir(ctx) as cfg_dir:
        cfg_dir.lock()
        text = cfg_dir.read_text(lifecycle.DISPLAYS_NAME)
        if text is None:
            lifecycle.read_config(cfg_dir)
        current = revision_of(text)
        if if_revision is not None and current != if_revision:
            raise EditError(["the configuration changed since it was read; reload and try again"],
                            EXIT_REVISION, current_revision=current)
        try:
            restored = lifecycle._parse_json(backup_text, f"backup {stamp}")
        except lifecycle.LifecycleError as exc:
            raise EditError([str(exc)], EXIT_INVALID) from exc
        errors = schema.validate_config(restored, registry)
        if not errors:
            errors = render.check_glyphs(restored)
        if errors:
            raise EditError([f"backup {stamp} is not valid for this version: {e}" for e in errors], EXIT_INVALID)
        try:
            config = lifecycle._parse_json(text, lifecycle.DISPLAYS_NAME)
        except lifecycle.LifecycleError:
            config = None
        if text == backup_text:
            return {"ok": True, "changed": False, "revision": current, "previous_revision": current,
                    "backup": None, "bridge_restart": "not_needed", "rebuild_needed": [],
                    "new_alarm_sources": [], "mqtt_sync": "not_needed"}
        if isinstance(config, dict) and not schema.validate_config(config, registry):
            now_ids, then_ids = set(config["displays"]), set(restored["displays"])
            if now_ids != then_ids:
                parts = []
                if then_ids - now_ids:
                    parts.append("the backup adds display(s) " + ", ".join(sorted(then_ids - now_ids)))
                if now_ids - then_ids:
                    parts.append("the backup removes display(s) " + ", ".join(sorted(now_ids - then_ids)))
                raise EditError([f"backup {stamp} has a different set of displays ({'; '.join(parts)}). "
                                 "Restore never changes MQTT accounts or the ACL: use 'vesselstackctl display add' "
                                 "or 'display remove' to match, then restore again"], EXIT_INVALID)
        before_sources = _source_hashes(config, registry) if config and not schema.validate_config(config, registry) else {}
        stamp_made = write_backup(cfg_dir, text)
        cfg_dir.write_text(lifecycle.DISPLAYS_NAME, backup_text, lifecycle.DISPLAYS_MODE)
        after_sources = _source_hashes(restored, registry)
        bridge_changed = not isinstance(config, dict) or _bridge_view(config) != _bridge_view(restored)
        new_sources = sorted(alarm_sources(restored) - (alarm_sources(config) if isinstance(config, dict) else set()))
        rebuild = [did for did in restored["displays"] if after_sources[did] != before_sources.get(did)]
        mqtt_sync = "not_needed"
        if acl_inputs(config) != acl_inputs(restored):   # same display set is enforced above, so only a damaged file lands here
            try:
                lifecycle._check_sync_env(ctx)
                lifecycle._sync(ctx)
                mqtt_sync = "synced"
            except lifecycle.LifecycleError as exc:
                mqtt_sync = f"failed: {exc}"
    return {"ok": True, "changed": True, "revision": revision_of(backup_text), "previous_revision": current,
            "backup": stamp_made, "bridge_restart": _restart_bridge(ctx, bridge_changed),
            "rebuild_needed": rebuild, "new_alarm_sources": new_sources, "mqtt_sync": mqtt_sync}


# --- serial ports ----------------------------------------------------------------------------------

def serial_ports(ctx: lifecycle.Context, by_id: Path = SERIAL_BY_ID, lstat=os.lstat,
                 realpath=os.path.realpath) -> dict:
    """The USB serial devices under /dev/serial/by-id that ``flash --serial`` would accept."""
    try:
        names = sorted(os.listdir(by_id))
    except OSError:
        names = []
    ports = []
    for name in names:
        path = f"{firmware.SERIAL_BY_ID_PREFIX}{name}"
        try:
            device = firmware.validate_serial(path, lstat, realpath)
        except render.RenderError:
            continue
        ports.append({"path": path, "device": device})
    return {"schema": 1, "ports": ports}


# --- plain-text forms for people ---------------------------------------------------------------------

def format_text(kind: str, result: dict) -> str:
    lines = []
    if kind == "catalog":
        live = result["live"]
        lines.append(f"Units: {result['units']}; live data: "
                     + ("not requested" if not live["requested"] else
                        f"{live['leaf_count']} numeric value(s)" if live["ok"] else f"unavailable ({live['error']})"))
        group = None
        for row in result["rows"]:
            if row["group"] != group:
                group = row["group"]
                lines.append(f"\n{group}")
            where = f"  [on {', '.join(row['used_on'])} as {row['configured_as']}]" if row["configured_as"] else ""
            lines.append(f"  {row['proposed_id']:<20} {row['label']:<17} {row['path']}  ({row['live_state']}){where}")
    elif kind == "show":
        display = result["display"]
        lines.append(f"{display['id']}: {display['name']} on {result['board']['name']}, theme {result['theme']}")
        for index, page in enumerate(display["pages"]):
            lines.append(f"  page {index + 1} {page['title']!r}: {page['layout']} -> {', '.join(page['items'])}")
        ready = result["readiness"]
        lines.append(f"Firmware: {ready['firmware']['status']}; Wi-Fi stored: {ready['wifi_stored']}; "
                     f"MQTT password stored: {ready['mqtt_password_stored']}; docker: {ready['docker_available']}")
        lines.append(f"Revision: {result['revision']}")
    elif kind == "history":
        for entry in result["backups"]:
            lines.append(f"{entry['stamp']}  {entry['size']:>7} bytes  {entry['revision'][:12]}")
        if not result["backups"]:
            lines.append("No backups yet (one is made before every change).")
    elif kind == "serial-ports":
        for port in result["ports"]:
            lines.append(f"{port['path']} -> {port['device']}")
        if not result["ports"]:
            lines.append("No USB serial devices found under /dev/serial/by-id.")
    return "\n".join(lines)


__all__ = ["EditError", "catalog", "list_displays", "show", "set_pages", "history", "restore_config", "serial_ports",
           "parse_payload", "format_text", "revision_of", "write_backup"]
