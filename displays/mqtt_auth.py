"""Mosquitto accounts and ACL rendering for VesselStack (stdlib only).

The ACL is always on. With displays disabled only the administrator account
exists. With displays enabled a scoped bridge account and one read-only account
per display are added. Passwords are hashed inside the pinned Mosquitto image
(``mosquitto_passwd -U``) so no secret ever appears on a command line.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import schema  # noqa: E402

BROKER_UID = 1883
BROKER_GID = 1883
CONTAINER = "vesselstack-mosquitto"
DEFAULT_BRIDGE_USERNAME = "vesselstack-bridge"
DISPLAY_USER_PREFIX = "vsdisp-"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
IMAGE_RE = re.compile(r"^\s*image:\s*(eclipse-mosquitto:\S+)\s*$", re.M)
SECRETS_NAME = "displays.secrets.json"
ROOT_TOPIC = "vesselstack/v1"

HASH_SCRIPT = (
    "set -e; "
    "mosquitto_passwd -U /mosquitto/data/passwords.new; "
    "chown 1883:1883 /mosquitto/data/passwords.new; "
    "chmod 0600 /mosquitto/data/passwords.new; "
    "mv -f /mosquitto/data/passwords.new /mosquitto/data/passwords"
)


class MqttAuthError(Exception):
    """Raised for invalid input or a failed write; the message is operator-facing."""


def display_username(display_id: str) -> str:
    return DISPLAY_USER_PREFIX + display_id


def default_runner(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=False, capture_output=True, text=True)


def find_image(root: Path) -> str:
    """The Mosquitto image is pinned once, in compose.yml; read it from there."""
    candidates = [
        root / "compose.yml",
        root / "installer" / "templates" / "compose.yml",
        Path(__file__).resolve().parent.parent / "templates" / "compose.yml",
    ]
    for path in candidates:
        try:
            match = IMAGE_RE.search(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if match:
            return match.group(1)
    raise MqttAuthError("cannot find the pinned eclipse-mosquitto image in compose.yml")


# --- inputs ---------------------------------------------------------------

def _check_password(label: str, value) -> str:
    if not isinstance(value, str) or not value:
        raise MqttAuthError(f"{label} must not be empty")
    if "\n" in value or "\r" in value or "\0" in value:
        raise MqttAuthError(f"{label} must not contain line breaks")
    # mosquitto_passwd -U splits the plaintext file at ':' and trims whitespace,
    # which would silently store a different password than the one configured.
    if ":" in value or value != value.strip():
        raise MqttAuthError(
            f"{label} must not contain ':' or leading/trailing whitespace; choose another value")
    return value


def _check_username(label: str, value) -> str:
    if not isinstance(value, str) or not USERNAME_RE.match(value):
        raise MqttAuthError(
            f"{label} must match ^[A-Za-z0-9_.-]{{1,64}}$ (letters, numbers, dot, underscore, hyphen)")
    return value


def load_display_ids(config_path: Path | None, enabled: bool) -> list[str]:
    if not enabled:
        return []
    if config_path is None or not config_path.exists():
        return []
    try:
        registry = schema.load_registry()
        config = schema.load_json(config_path)
    except (OSError, ValueError) as exc:
        raise MqttAuthError(f"cannot read {config_path}: {exc}") from exc
    errors = schema.validate_config(config, registry)
    if errors:
        raise MqttAuthError(f"{config_path} is invalid: {errors[0]} ({len(errors)} error(s))")
    return sorted(config["displays"])


def load_secrets(path: Path) -> dict:
    empty = {"schema": 1, "bridge": {}, "displays": {}}
    if not path.exists():
        return empty
    try:
        data = schema.load_json(path)
    except (OSError, ValueError) as exc:
        raise MqttAuthError(f"cannot read {path}: {exc}") from exc
    if (not isinstance(data, dict) or data.get("schema") != 1
            or not isinstance(data.get("bridge"), dict)
            or not isinstance(data.get("displays"), dict)):
        raise MqttAuthError(f"{path} is not a schema 1 secrets file; refusing to overwrite it")
    return data


def write_atomic(path: Path, text: str, mode: int = 0o600) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def ensure_secrets(path: Path, display_ids: list[str], enabled: bool) -> dict:
    """Create missing passwords once; never regenerate or drop existing ones."""
    data = load_secrets(path)
    changed = not path.exists()
    if enabled:
        if not data["bridge"].get("mqtt_password"):
            data["bridge"] = {"mqtt_password": secrets.token_urlsafe(32)}
            changed = True
        for display_id in display_ids:
            entry = data["displays"].get(display_id)
            if not isinstance(entry, dict) or not entry.get("mqtt_password"):
                data["displays"][display_id] = {"mqtt_password": secrets.token_urlsafe(32)}
                changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, json.dumps(data, indent=2, sort_keys=True) + "\n")
    return data


# --- rendering ------------------------------------------------------------

def build_accounts(admin_user, admin_password, enabled, bridge_user, display_ids,
                   secret_data) -> list[dict]:
    """Ordered account list: admin, bridge, then displays sorted by id."""
    admin_user = _check_username("MQTT_USERNAME", admin_user)
    admin_password = _check_password("MQTT_PASSWORD", admin_password)
    accounts = [{"role": "admin", "user": admin_user, "password": admin_password}]
    if enabled:
        bridge_user = _check_username("DISPLAYS_BRIDGE_MQTT_USERNAME", bridge_user)
        accounts.append({"role": "bridge", "user": bridge_user,
                         "password": _check_password("bridge password",
                                                     secret_data["bridge"].get("mqtt_password"))})
        for display_id in sorted(display_ids):
            accounts.append({
                "role": "display", "user": display_username(display_id),
                "password": _check_password(f"display {display_id} password",
                                            secret_data["displays"][display_id].get("mqtt_password")),
            })
    names = [account["user"] for account in accounts]
    for name in names:
        if names.count(name) > 1:
            raise MqttAuthError(f"MQTT username {name!r} is used by more than one account")
    return accounts


def render_acl(accounts: list[dict]) -> str:
    lines: list[str] = []
    for account in accounts:
        user = account["user"]
        lines.append(f"user {user}")
        if account["role"] == "admin":
            lines += ["topic readwrite #", "topic read $SYS/#"]
        elif account["role"] == "bridge":
            lines += [f"topic write {ROOT_TOPIC}/instruments/#",
                      f"topic write {ROOT_TOPIC}/bridge/#",
                      f"topic read {ROOT_TOPIC}/displays/+/status"]
        else:
            lines += [f"topic read {ROOT_TOPIC}/instruments/#",
                      f"topic read {ROOT_TOPIC}/bridge/#",
                      f"topic write {ROOT_TOPIC}/displays/{user}/status"]
        lines.append("")
    return "\n".join(lines)


def render_passwords(accounts: list[dict]) -> str:
    return "".join(f"{a['user']}:{a['password']}\n" for a in accounts)


# --- writing --------------------------------------------------------------

def _owner(uid, gid):
    """Chown only when running as root; tests inject uid=None."""
    if uid is None or gid is None or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None
    return uid, gid


def _secure(path: Path, owner) -> None:
    os.chmod(path, 0o600)
    if owner:
        os.chown(path, *owner)


def _new_file(path: Path, text: str, owner) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    _secure(path, owner)


def _keep_previous(live: Path, owner) -> None:
    if not live.exists():
        return
    prev = live.with_name(live.name + ".prev")
    tmp = live.with_name(live.name + ".prev.tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(live.read_bytes())
    _secure(tmp, owner)
    os.replace(tmp, prev)


def _cleanup(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def write_broker_files(data_dir: Path, accounts: list[dict], image: str,
                       runner=default_runner, uid=BROKER_UID, gid=BROKER_GID) -> None:
    """Install acl and hashed passwords, both owned by the broker with mode 0600."""
    owner = _owner(uid, gid)
    mdir = data_dir / "mosquitto"
    mdir.mkdir(mode=0o750, parents=True, exist_ok=True)
    acl, acl_new = mdir / "acl", mdir / "acl.new"
    pw, pw_new = mdir / "passwords", mdir / "passwords.new"
    try:
        _new_file(acl_new, render_acl(accounts), owner)
        _new_file(pw_new, render_passwords(accounts), owner)
        _keep_previous(acl, owner)
        _keep_previous(pw, owner)
        result = runner(["docker", "run", "--rm", "--entrypoint", "/bin/sh",
                         "-v", f"{mdir}:/mosquitto/data", image, "-c", HASH_SCRIPT])
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            raise MqttAuthError("hashing MQTT passwords in the Mosquitto image failed"
                                + (f": {detail[-1]}" if detail else ""))
        if not pw.exists() or pw_new.exists():
            raise MqttAuthError("MQTT password file was not installed")
        _secure(pw, owner)
        os.replace(acl_new, acl)
        _secure(acl, owner)
    except BaseException:
        _cleanup(acl_new, pw_new)
        raise


def reload_broker(runner=default_runner, container: str = CONTAINER) -> bool:
    """SIGHUP the broker if it is running so it re-reads passwords and ACL."""
    state = runner(["docker", "inspect", "-f", "{{.State.Running}}", container])
    if state.returncode != 0 or (state.stdout or "").strip() != "true":
        return False
    result = runner(["docker", "kill", "-s", "HUP", container])
    if result.returncode != 0:
        raise MqttAuthError(f"could not signal {container} to reload its ACL")
    return True


def sync(root: Path, data_dir: Path, env: dict, displays_config: Path | None = None,
         image: str | None = None, runner=default_runner,
         uid=BROKER_UID, gid=BROKER_GID, container: str = CONTAINER) -> dict:
    """Render and install ACL + passwords from env values; returns a summary."""
    enabled_text = env.get("DISPLAYS_ENABLE", "false")
    if enabled_text not in ("true", "false"):
        raise MqttAuthError("DISPLAYS_ENABLE must be true or false")
    enabled = enabled_text == "true"
    if displays_config is None:
        displays_config = root / "config" / "displays.json"
    bridge_user = env.get("DISPLAYS_BRIDGE_MQTT_USERNAME") or DEFAULT_BRIDGE_USERNAME
    admin_user = _check_username("MQTT_USERNAME", env.get("MQTT_USERNAME", ""))
    _check_password("MQTT_PASSWORD", env.get("MQTT_PASSWORD", ""))
    display_ids = load_display_ids(displays_config, enabled)
    secret_path = root / "config" / SECRETS_NAME
    secret_data = ensure_secrets(secret_path, display_ids, enabled) if enabled else \
        load_secrets(secret_path)
    accounts = build_accounts(admin_user, env["MQTT_PASSWORD"], enabled, bridge_user,
                              display_ids, secret_data)
    write_broker_files(data_dir, accounts, image or find_image(root), runner, uid, gid)
    reloaded = reload_broker(runner, container)
    return {"enabled": enabled, "accounts": len(accounts), "displays": len(display_ids),
            "reloaded": reloaded}
