"""Display lifecycle: list/add/remove/rotate/wifi/status (stdlib only; render/export live in render.py).

All edits of ``config/displays.json`` and ``config/displays.secrets.json`` are
made under an exclusive ``flock`` on ``config/.displays.lock`` and go through
directory-relative, no-follow file operations (symlinks are refused). Files are
replaced atomically (temp file created ``O_EXCL|O_NOFOLLOW``, fsync, rename).
Secret values are never printed and never placed on a command line.
"""
from __future__ import annotations

import base64
import getpass
import json
import os
import secrets as secrets_module
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mqtt_auth  # noqa: E402
import schema  # noqa: E402
import topics  # noqa: E402

DISPLAYS_NAME = "displays.json"
LOCK_NAME = mqtt_auth.LOCK_NAME
LOCK_TIMEOUT_S = mqtt_auth.LOCK_TIMEOUT_S
CONFIG_OWNER = mqtt_auth.CONFIG_OWNER
DISPLAYS_MODE = 0o644
SECRETS_MODE = 0o600
# Per-display generated secrets. rotate regenerates every key listed here, so a
# future generated secret only needs to be added to this table.
GENERATED_SECRETS = {
    "mqtt_password": lambda: secrets_module.token_urlsafe(32),
    "ota_password": lambda: secrets_module.token_urlsafe(32),
    # ESPHome Home Assistant API encryption key: 32 random bytes, base64. Always
    # generated so enabling home_assistant_api later needs no new key.
    "api_encryption_key": lambda: base64.b64encode(secrets_module.token_bytes(32)).decode("ascii"),
}
STATUS_WAIT_S = 3
SANITIZE_MAX = 40


class LifecycleError(Exception):
    """Operator-facing failure; ``code`` is the process exit status (1 or 2)."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def default_exec_runner(argv: list[str], input_text: str | None = None):
    return subprocess.run(argv, check=False, capture_output=True, text=True, input=input_text)


@dataclass
class Context:
    root: Path
    env: dict = field(default_factory=dict)
    runner: Callable = mqtt_auth.default_runner
    exec_runner: Callable = default_exec_runner
    dir_owner: tuple = mqtt_auth.DIR_OWNER
    file_owner: tuple = mqtt_auth.FILE_OWNER
    require_root: bool = True
    lock_timeout: float = LOCK_TIMEOUT_S
    image: str | None = None
    container: str = mqtt_auth.CONTAINER
    out: Callable = print

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def enabled(self) -> bool:
        text = self.env.get("DISPLAYS_ENABLE", "false")
        if text not in ("true", "false"):
            raise LifecycleError("DISPLAYS_ENABLE must be true or false", 2)
        return text == "true"


# --- guarded file access ---------------------------------------------------

class ConfigDir:
    """The config directory as a no-follow directory fd with locked, atomic edits."""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.fd = -1
        self.lock_fd = -1

    def __enter__(self):
        path = self.ctx.config_dir
        try:
            mqtt_auth._check_dir_component(path, "configuration directory", False, 0o755)
            self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except mqtt_auth.MqttAuthError as exc:
            raise LifecycleError(str(exc)) from exc
        except OSError as exc:
            raise LifecycleError(f"cannot open {path}: {exc.strerror}") from exc
        return self

    def __exit__(self, *_exc):
        self.unlock()
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def lock(self) -> None:
        """Exclusive lock; waits up to ctx.lock_timeout seconds, then fails."""
        try:
            self.lock_fd = mqtt_auth.acquire_lock(self.fd, self.ctx.lock_timeout)
        except mqtt_auth.MqttAuthError as exc:
            raise LifecycleError(str(exc)) from exc

    def unlock(self) -> None:
        if self.lock_fd >= 0:
            os.close(self.lock_fd)  # closing releases the flock
            self.lock_fd = -1

    def exists(self, name: str) -> bool:
        return self._regular_or_none(name) is not None

    def _regular_or_none(self, name: str):
        try:
            info = os.lstat(name, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            raise LifecycleError(f"refusing to use {self.ctx.config_dir / name}: it is a symbolic link")
        if not stat.S_ISREG(info.st_mode):
            raise LifecycleError(f"refusing to use {self.ctx.config_dir / name}: not a regular file")
        return info

    def read_text(self, name: str) -> str | None:
        if self._regular_or_none(name) is None:
            return None
        try:
            data = mqtt_auth._read_regular(name, self.fd)
        except mqtt_auth.MqttAuthError as exc:
            raise LifecycleError(str(exc)) from exc
        except PermissionError:
            raise
        except OSError as exc:
            raise LifecycleError(f"cannot read {self.ctx.config_dir / name}: {exc.strerror}") from exc
        return data.decode("utf-8")

    def write_text(self, name: str, text: str, mode: int) -> None:
        self._regular_or_none(name)  # refuse a symlink or special file target
        tmp = name + ".tmp"
        mqtt_auth._cleanup(self.fd, tmp)  # a stale temp (even a symlink) is unlinked, never followed
        mqtt_auth._write_new(tmp, text.encode("utf-8"), self.fd, mode,
                             mqtt_auth._owner(CONFIG_OWNER))
        try:
            os.replace(tmp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        except BaseException:
            mqtt_auth._cleanup(self.fd, tmp)
            raise


def _parse_json(text: str, label: str):
    try:
        return json.loads(text, object_pairs_hook=schema._no_duplicate_keys)
    except ValueError as exc:
        raise LifecycleError(f"cannot parse {label}: {exc}") from exc


def read_config(cfg_dir: ConfigDir) -> dict:
    text = cfg_dir.read_text(DISPLAYS_NAME)
    if text is None:
        raise LifecycleError(
            f"{cfg_dir.ctx.config_dir / DISPLAYS_NAME} does not exist; run the installer with "
            "DISPLAYS_ENABLE=true first (it seeds the file) or create it from "
            "displays/displays.example.json")
    return _parse_json(text, DISPLAYS_NAME)


def read_secrets(cfg_dir: ConfigDir) -> dict:
    text = cfg_dir.read_text(mqtt_auth.SECRETS_NAME)
    if text is None:
        return {"schema": 1, "bridge": {}, "displays": {}}
    data = _parse_json(text, mqtt_auth.SECRETS_NAME)
    if (not isinstance(data, dict) or data.get("schema") != 1
            or not isinstance(data.get("bridge"), dict)
            or not isinstance(data.get("displays"), dict)):
        raise LifecycleError(f"{mqtt_auth.SECRETS_NAME} is not a schema 1 secrets file; "
                             "refusing to overwrite it")
    return data


def write_config(cfg_dir: ConfigDir, config: dict, registry: dict) -> None:
    errors = schema.validate_config(config, registry)
    if errors:
        raise LifecycleError(
            f"the resulting {DISPLAYS_NAME} would be invalid ({errors[0]}); nothing written", 2)
    text = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    cfg_dir.write_text(DISPLAYS_NAME, text, DISPLAYS_MODE)


def write_secrets(cfg_dir: ConfigDir, data: dict) -> None:
    cfg_dir.write_text(mqtt_auth.SECRETS_NAME, json.dumps(data, indent=2, sort_keys=True) + "\n",
                       SECRETS_MODE)


def load_registry() -> dict:
    try:
        registry = schema.load_registry()
    except (OSError, ValueError) as exc:
        raise LifecycleError(f"cannot load the board registry: {exc}") from exc
    errors = schema.validate_registry(registry)
    if errors:
        raise LifecycleError(f"board registry is invalid: {errors[0]}")
    return registry


# --- shared steps ----------------------------------------------------------

def _require_root(ctx: Context, command: str) -> None:
    if ctx.require_root and (not hasattr(os, "geteuid") or os.geteuid() != 0):
        raise LifecycleError(f"'{command}' changes root-owned files; run it as root (sudo)")


def _check_sync_env(ctx: Context) -> None:
    if ctx.enabled:
        for key in ("MQTT_USERNAME", "MQTT_PASSWORD"):
            if not ctx.env.get(key):
                raise LifecycleError(
                    f"{key} is required to update MQTT access; run through "
                    "'vesselstackctl display ...' or export the vesselstack.env values", 2)


def _sync(ctx: Context) -> None:
    if not ctx.enabled:
        ctx.out("Displays are disabled (DISPLAYS_ENABLE=false): the files were updated, but "
                "MQTT access changes only take effect once displays are enabled and the "
                "installer or 'sync-mqtt' runs.")
        return
    try:
        summary = mqtt_auth.sync(ctx.root, None, ctx.env, image=ctx.image, runner=ctx.runner,
                                 dir_owner=ctx.dir_owner, file_owner=ctx.file_owner,
                                 container=ctx.container)
    except (mqtt_auth.MqttAuthError, OSError) as exc:
        raise LifecycleError(
            f"the files were updated but the MQTT sync failed: {exc}. Run "
            "'vesselstackctl display sync-mqtt' to retry") from exc
    ctx.out(f"MQTT access control synced: {summary['accounts']} account(s), "
            f"{summary['displays']} display(s), "
            f"broker {'reloaded' if summary['reloaded'] else 'not running'}")


def _display_or_error(config: dict, display_id: str) -> dict:
    if display_id not in config["displays"]:
        known = ", ".join(sorted(config["displays"])) or "none"
        raise LifecycleError(f"unknown display {display_id!r} (configured: {known})", 2)
    return config["displays"][display_id]


def _loaded_config(cfg_dir: ConfigDir, registry: dict) -> dict:
    config = read_config(cfg_dir)
    errors = schema.validate_config(config, registry)
    if errors:
        raise LifecycleError(
            f"{DISPLAYS_NAME} is already invalid ({errors[0]}; {len(errors)} error(s)); fix it "
            "or run 'validate' first. Nothing was changed", 2)
    return config


def _bridge_note(ctx: Context) -> None:
    if ctx.enabled:
        ctx.out("The displays bridge publishes all configured instruments and needs no restart.")


# --- commands --------------------------------------------------------------

def default_page(registry: dict, config: dict, board_id: str) -> dict:
    board = registry["boards"][board_id]
    caps = registry["layouts"]
    layout = max(board["layouts"], key=lambda name: caps[name])
    instruments = list(config["instruments"])
    return {"title": "Main", "layout": layout, "items": instruments[:caps[layout]]}


def add(ctx: Context, display_id: str, board: str, name: str | None = None,
        home_assistant_api: bool = False) -> None:
    if not schema.DISPLAY_ID.fullmatch(display_id):
        raise LifecycleError("display id must match ^[a-z]([a-z0-9-]{0,22}[a-z0-9])?$ (lowercase letters, "
                             "digits and hyphens, starting with a letter)", 2)
    registry = load_registry()
    if board not in registry["boards"]:
        raise LifecycleError(f"unknown board {board!r}; supported: "
                             f"{', '.join(sorted(registry['boards']))}", 2)
    _require_root(ctx, "add")
    _check_sync_env(ctx)
    with ConfigDir(ctx) as cfg_dir:
        cfg_dir.lock()
        config = _loaded_config(cfg_dir, registry)
        read_secrets(cfg_dir)  # refuse a symlinked or malformed secrets file before editing
        if display_id in config["displays"]:
            raise LifecycleError(f"display {display_id!r} already exists; use 'rotate' to issue "
                                 "new credentials or 'remove' first", 2)
        if not config["instruments"]:
            raise LifecycleError(
                f"{DISPLAYS_NAME} has no instruments to show; add at least one instrument "
                "(see displays/displays.example.json) and run add again", 2)
        page = default_page(registry, config, board)
        entry = {"name": name if name is not None else display_id.replace("-", " ").title(),
                 "board": board}
        if home_assistant_api:
            entry["home_assistant_api"] = True
        entry["pages"] = [page]
        config["displays"][display_id] = entry
        write_config(cfg_dir, config, registry)
        username = mqtt_auth.display_username(display_id)
        ctx.out(f"Added display {display_id!r} ({registry['boards'][board]['name']}): page "
                f"'Main', layout {page['layout']}, {len(page['items'])} item(s).")
        _sync(ctx)
    ctx.out(f"MQTT username: {username} (the password is generated and stored in "
            f"{mqtt_auth.SECRETS_NAME}; it is never printed)")
    ctx.out("Next steps:")
    ctx.out(f"  1. Edit the pages in {ctx.config_dir / DISPLAYS_NAME} and check with 'validate'.")
    ctx.out("  2. Store the shared Wi-Fi credentials with 'vesselstackctl display wifi'.")
    ctx.out(f"  3. Generate the firmware project: 'vesselstackctl display export {display_id} "
            "--stdout' (see the README for building and flashing).")
    _bridge_note(ctx)


def remove(ctx: Context, display_id: str, yes: bool) -> None:
    if not yes:
        raise LifecycleError(
            f"remove {display_id} deletes the display, its MQTT account and its stored "
            "credentials; run again with --yes to confirm", 2)
    _require_root(ctx, "remove")
    registry = load_registry()
    _check_sync_env(ctx)
    with ConfigDir(ctx) as cfg_dir:
        cfg_dir.lock()
        config = _loaded_config(cfg_dir, registry)
        _display_or_error(config, display_id)
        secret_data = read_secrets(cfg_dir)
        del config["displays"][display_id]
        write_config(cfg_dir, config, registry)
        if display_id in secret_data["displays"]:
            del secret_data["displays"][display_id]
            write_secrets(cfg_dir, secret_data)
        ctx.out(f"Removed display {display_id!r} and its stored credentials.")
        _sync(ctx)
    ctx.out("The device is disconnected by the broker reload; erase or reflash it before reuse. "
            "Adding the same id again creates new credentials.")
    _bridge_note(ctx)


def rotate(ctx: Context, display_id: str) -> None:
    _require_root(ctx, "rotate")
    registry = load_registry()
    _check_sync_env(ctx)
    with ConfigDir(ctx) as cfg_dir:
        cfg_dir.lock()
        config = _loaded_config(cfg_dir, registry)
        _display_or_error(config, display_id)
        secret_data = read_secrets(cfg_dir)
        entry = secret_data["displays"].get(display_id)
        entry = dict(entry) if isinstance(entry, dict) else {}
        for key, generate in GENERATED_SECRETS.items():
            entry[key] = generate()
        secret_data["displays"][display_id] = entry
        write_secrets(cfg_dir, secret_data)
        ctx.out(f"Rotated {', '.join(GENERATED_SECRETS)} for display {display_id!r}.")
        _sync(ctx)
    ctx.out("WARNING: the device still holds the old credentials and is refused by the broker; "
            "it must be reflashed with the new ones.")


def validate_wifi(ssid: str, password: str, allow_open: bool) -> None:
    if not ssid or len(ssid.encode("utf-8")) > 32 or not ssid.isprintable():
        raise LifecycleError("Wi-Fi SSID must be 1-32 bytes of printable UTF-8 text", 2)
    if password == "":
        if not allow_open:
            raise LifecycleError("an empty Wi-Fi password means an open network; pass --open to "
                                 "confirm that", 2)
        return
    if not 8 <= len(password) <= 63 or not all(0x20 <= ord(ch) <= 0x7E for ch in password):
        raise LifecycleError("Wi-Fi password must be 8-63 printable ASCII characters", 2)


def read_wifi_input(stdin=None) -> tuple[str, str]:
    """SSID then password from the terminal (password hidden), or two stdin lines."""
    stream = stdin if stdin is not None else sys.stdin
    if stream.isatty():
        ssid = input("Wi-Fi SSID: ")
        password = getpass.getpass("Wi-Fi password (empty only with --open): ")
        return ssid, password
    lines = stream.read().split("\n")
    ssid = lines[0].rstrip("\r")
    password = lines[1].rstrip("\r") if len(lines) > 1 else ""
    return ssid, password


def wifi(ctx: Context, clear: bool = False, allow_open: bool = False, stdin=None) -> None:
    _require_root(ctx, "wifi")
    credentials = None
    if not clear:
        ssid, password = read_wifi_input(stdin)
        validate_wifi(ssid, password, allow_open)
        credentials = {"ssid": ssid, "password": password}
    with ConfigDir(ctx) as cfg_dir:
        cfg_dir.lock()
        secret_data = read_secrets(cfg_dir)
        if clear:
            if "wifi" not in secret_data:
                ctx.out("No Wi-Fi credentials were stored.")
                return
            del secret_data["wifi"]
            write_secrets(cfg_dir, secret_data)
            ctx.out("Wi-Fi credentials removed.")
            return
        secret_data["wifi"] = credentials
        write_secrets(cfg_dir, secret_data)
    kind = "open network" if credentials["password"] == "" else "WPA passphrase"
    ctx.out(f"Wi-Fi credentials saved ({kind}) in {mqtt_auth.SECRETS_NAME}, shared by all "
            "displays. Displays must be reflashed to pick up changes.")


def list_displays(ctx: Context) -> list[list[str]]:
    registry = load_registry()
    with ConfigDir(ctx) as cfg_dir:
        config = read_config(cfg_dir)
        errors = schema.validate_config(config, registry)
        if errors:
            raise LifecycleError(f"{DISPLAYS_NAME} is invalid ({errors[0]}); run 'validate'", 2)
        try:
            secret_data = read_secrets(cfg_dir)
            secret_state = "yes"
        except PermissionError:
            secret_data, secret_state = None, "unknown"
    if secret_data is None:
        wifi_state = "unknown"
    else:
        value = secret_data.get("wifi")
        wifi_state = "yes" if isinstance(value, dict) and value.get("ssid") else "no"
    header = ["ID", "NAME", "BOARD", "PAGES", "MQTT USER", "SECRETS", "WIFI"]
    rows = [header]
    for display_id, entry in config["displays"].items():
        if secret_data is None:
            has_secret = "unknown"
        else:
            record = secret_data["displays"].get(display_id)
            has_secret = "yes" if isinstance(record, dict) and record.get("mqtt_password") \
                else "no"
        rows.append([display_id, entry["name"], entry["board"], str(len(entry["pages"])),
                     mqtt_auth.display_username(display_id), has_secret, wifi_state])
    if len(rows) == 1:
        ctx.out("No displays configured. Add one with 'displayctl add ID --board BOARD'.")
        return rows
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    for row in rows:
        ctx.out("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return rows


# --- status ------------------------------------------------------------------

STATUS_SCRIPT = (
    # The options file (broker credentials) arrives on stdin and lives only in a
    # 0600 file in the container for the life of this one command.
    'umask 077; o=$(mktemp) || exit 70; trap \'rm -f "$o"\' EXIT; cat > "$o"; '
    f'mosquitto_sub -o "$o" -h 127.0.0.1 -p 1883 --retained-only -W {STATUS_WAIT_S} '
    '-F "%t\\t%p" -t "$1" -t "$2" -t "$3"'
)


def _clean(text: str) -> str:
    cleaned = "".join(ch if ch.isprintable() else "?" for ch in text)
    return cleaned[:SANITIZE_MAX]


def _options_file(user: str, password: str) -> str:
    for label, value in (("MQTT_USERNAME", user), ("MQTT_PASSWORD", password)):
        if not value or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            raise LifecycleError(f"{label} is missing or contains control characters", 2)
    return f"-u {user}\n-P {password}\n"


def parse_retained(output: str) -> dict:
    messages = {}
    for line in output.splitlines():
        topic, sep, payload = line.partition("\t")
        if sep:
            messages[topic] = payload
    return messages


def status(ctx: Context) -> int:
    registry = load_registry()
    with ConfigDir(ctx) as cfg_dir:
        config = read_config(cfg_dir)
    errors = schema.validate_config(config, registry)
    if errors:
        raise LifecycleError(f"{DISPLAYS_NAME} is invalid ({errors[0]}); run 'validate'", 2)
    options = _options_file(ctx.env.get("MQTT_USERNAME", ""), ctx.env.get("MQTT_PASSWORD", ""))
    state = ctx.runner(["docker", "inspect", "-f", "{{.State.Running}}", ctx.container])
    if state.returncode != 0 or (state.stdout or "").strip() != "true":
        raise LifecycleError(f"the Mosquitto container {ctx.container} is not running; "
                             "start the stack with 'vesselstackctl start'")
    result = ctx.exec_runner(
        ["docker", "exec", "-i", ctx.container, "sh", "-c", STATUS_SCRIPT, "status",
         topics.BRIDGE_STATUS_TOPIC, topics.BRIDGE_SIGNALK_TOPIC,
         f"{topics.TOPIC_ROOT}/displays/+/status"],
        options)
    if result.returncode not in (0, 27):  # 27: mosquitto_sub -W timeout, the normal end
        detail = ((result.stderr or "").strip().splitlines() or ["no detail"])[-1]
        raise LifecycleError(f"could not read retained status from the broker: {_clean(detail)}")
    messages = parse_retained(result.stdout or "")

    def state_of(topic: str, missing: str) -> str:
        return _clean(messages[topic]) if topic in messages else missing

    ctx.out(f"{'Bridge':<20}{state_of(topics.BRIDGE_STATUS_TOPIC, 'unknown (no retained status)')}")
    ctx.out(f"{'SignalK link':<20}{state_of(topics.BRIDGE_SIGNALK_TOPIC, 'unknown (no retained status)')}")
    if not config["displays"]:
        ctx.out("No displays configured.")
        return 0
    ctx.out("")
    ctx.out(f"{'DISPLAY':<20}STATE")
    for display_id in config["displays"]:
        topic = topics.display_status_topic(mqtt_auth.display_username(display_id))
        ctx.out(f"{display_id:<20}{state_of(topic, 'never seen (no retained status)')}")
    return 0
