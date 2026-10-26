"""Mosquitto accounts and ACL rendering for VesselStack (stdlib only).

The ACL is always on. With displays disabled only the administrator account
exists. With displays enabled a scoped bridge account and one read-only account
per display are added. Passwords are hashed inside the pinned Mosquitto image
(``mosquitto_passwd -U``) so no secret ever appears on a command line.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import schema  # noqa: E402

BROKER_UID = 1883
BROKER_GID = 1883
# The auth DIRECTORY is root-owned and only group-readable by the broker, so the
# broker can traverse it but can never create, remove or rename entries (no
# planted symlinks for root-run code). The auth FILES are owned by the broker
# user with mode 0600 because Mosquitto warns about, and a future version will
# refuse, files it does not own. The files themselves cannot be swapped or
# redirected because the directory is not writable by the broker.
DIR_OWNER = (0, BROKER_GID)
DIR_MODE = 0o750
FILE_OWNER = (BROKER_UID, BROKER_GID)
# The temporary plaintext password list is never broker-readable: root:root 0600
# until the hashing container (running as root) hands over only the hashed file.
PLAINTEXT_OWNER = (0, 0)
FILE_MODE = 0o600
AUTH_DIR_NAME = "mosquitto-auth"
CONTAINER = "vesselstack-mosquitto"
DEFAULT_BRIDGE_USERNAME = "vesselstack-bridge"
DISPLAY_USER_PREFIX = "vsdisp-"
USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")  # always used with fullmatch
IMAGE_RE = re.compile(r"^\s*image:\s*(eclipse-mosquitto:\S+)\s*$", re.M)
SECRETS_NAME = "displays.secrets.json"
ROOT_TOPIC = "vesselstack/v1"
LOCK_NAME = ".displays.lock"
LOCK_TIMEOUT_S = 10.0
CONFIG_OWNER = (0, 0)
# mosquitto_passwd -U works on a copy: "<file>.backup.XXXXXX" (plaintext!) and
# "<file>.new". Anything that starts with this prefix is a possible plaintext
# leftover and is removed on every outcome.
PLAINTEXT_PREFIX = "passwords.new."

# umask 077 keeps every file mosquitto_passwd creates private. chmod runs before
# chown so only CHOWN is needed (a root process may chmod its own file). Leftover
# plaintext is removed by the trap on failure and by the host side afterwards.
HASH_SCRIPT = (
    "set -e; umask 077; "
    "trap 'rm -f /auth/passwords.new /auth/passwords.new.*' EXIT; "
    "mosquitto_passwd -U /auth/passwords.new; "
    "chmod 0600 /auth/passwords.new; "
    "chown 1883:1883 /auth/passwords.new; "
    "mv -f /auth/passwords.new /auth/passwords"
)
# The hashing container needs no network and only the CHOWN capability.
HASH_DOCKER_FLAGS = ["--network", "none", "--cap-drop", "ALL", "--cap-add", "CHOWN",
                     "--security-opt", "no-new-privileges"]


class MqttAuthError(Exception):
    """Raised for invalid input or a failed write; the message is operator-facing."""


def display_username(display_id: str) -> str:
    return DISPLAY_USER_PREFIX + display_id


def default_runner(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=False, capture_output=True, text=True)


def find_image(root: Path, compose: Path | None = None) -> str:
    """The Mosquitto image is pinned once, in compose.yml; read it from there.

    ``compose`` names the definition about to be installed (the installer passes
    the release's template, because the installed compose.yml is still the old
    one while the sync runs); otherwise the installed copy is used.
    """
    if compose is not None:
        candidates = [Path(compose)]
    else:
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
    if not isinstance(value, str) or not USERNAME_RE.fullmatch(value):
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
    """Write ``path`` atomically; a symlinked temp file or final path is refused."""
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise MqttAuthError(f"refusing to write {path}: it is a symbolic link")
    except FileNotFoundError:
        pass
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
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
                      f"topic write {ROOT_TOPIC}/notifications/#",
                      f"topic write {ROOT_TOPIC}/ha/#",
                      f"topic write {ROOT_TOPIC}/alarms/#",
                      f"topic write {ROOT_TOPIC}/bridge/#",
                      f"topic read {ROOT_TOPIC}/displays/+/status"]
        else:
            lines += [f"topic read {ROOT_TOPIC}/instruments/#",
                      f"topic read {ROOT_TOPIC}/notifications/#",
                      f"topic read {ROOT_TOPIC}/ha/#",
                      f"topic read {ROOT_TOPIC}/alarms/#",
                      f"topic read {ROOT_TOPIC}/bridge/#",
                      f"topic write {ROOT_TOPIC}/displays/{user}/status"]
        lines.append("")
    return "\n".join(lines)


def render_passwords(accounts: list[dict]) -> str:
    return "".join(f"{a['user']}:{a['password']}\n" for a in accounts)


# --- writing --------------------------------------------------------------
# Every operation below is relative to a directory file descriptor opened with
# O_NOFOLLOW, and every file open uses O_NOFOLLOW, so a symlink can never be
# followed even if something managed to plant one.

def _owner(owner):
    """Chown only when running as root; tests inject owner=None."""
    if owner is None or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None
    return owner


def _lstat(name: str, dir_fd: int):
    try:
        return os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return None


def _check_dir_component(path: Path, label: str, create: bool, mode: int):
    """lstat a directory; refuse symlinks and non-directories."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if not create:
            raise MqttAuthError(f"{label} {path} does not exist") from None
        os.mkdir(path, mode)
        info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise MqttAuthError(f"refusing to use {label} {path}: it is a symbolic link")
    if not stat.S_ISDIR(info.st_mode):
        raise MqttAuthError(f"refusing to use {label} {path}: it is not a directory")
    return info


def open_auth_dir(root: Path, dir_owner) -> int:
    """Create/verify $ROOT/config/mosquitto-auth and return a no-follow dir fd.

    When running as root the directory must already be root-owned and not
    group/world-writable; otherwise something other than root could have
    modified it and it is refused rather than repaired.
    """
    config = root / "config"
    _check_dir_component(config, "configuration directory", True, 0o755)
    auth = config / AUTH_DIR_NAME
    info = _check_dir_component(auth, "MQTT auth directory", True, DIR_MODE)
    if dir_owner:
        if info.st_uid != dir_owner[0]:
            raise MqttAuthError(
                f"refusing to use {auth}: it is owned by uid {info.st_uid}, expected {dir_owner[0]}")
        if info.st_mode & 0o022:
            raise MqttAuthError(
                f"refusing to use {auth}: it is group or world writable "
                f"(mode {info.st_mode & 0o777:o})")
    fd = os.open(auth, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        now = os.fstat(fd)
        if (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino):
            raise MqttAuthError(f"{auth} changed while it was being opened")
        os.fchmod(fd, DIR_MODE)
        if dir_owner:
            os.fchown(fd, *dir_owner)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _require_regular_or_absent(name: str, dir_fd: int) -> None:
    info = _lstat(name, dir_fd)
    if info is not None and not stat.S_ISREG(info.st_mode):
        raise MqttAuthError(f"refusing to use {name} in the MQTT auth directory: "
                            "it is not a regular file")


def _remove_stale(name: str, dir_fd: int) -> None:
    """Remove a leftover temp file, but only if it is a regular file."""
    _require_regular_or_absent(name, dir_fd)
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


def _write_new(name: str, data: bytes, dir_fd: int, mode: int, owner) -> None:
    _remove_stale(name, dir_fd)
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                 dir_fd=dir_fd)
    try:
        _write_all(fd, data)
        os.fchmod(fd, mode)
        if owner:
            os.fchown(fd, *owner)
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _read_regular(name: str, dir_fd: int) -> bytes | None:
    info = _lstat(name, dir_fd)
    if info is None:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise MqttAuthError(f"refusing to read {name}: it is not a regular file")
    # O_NONBLOCK: a FIFO swapped in after the lstat must not hang a root command in open().
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise MqttAuthError(f"refusing to read {name}: it is not a regular file")
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _keep_previous(name: str, dir_fd: int, owner) -> None:
    data = _read_regular(name, dir_fd)
    if data is None:
        return
    prev = name + ".prev"
    _require_regular_or_absent(prev, dir_fd)
    tmp = prev + ".tmp"
    _write_new(tmp, data, dir_fd, FILE_MODE, owner)
    os.replace(tmp, prev, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def _secure_existing(name: str, dir_fd: int, owner) -> None:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise MqttAuthError(f"refusing to use {name}: it is not a regular file")
        os.fchmod(fd, FILE_MODE)
        if owner:
            os.fchown(fd, *owner)
    finally:
        os.close(fd)


def _cleanup(dir_fd: int, *names: str) -> None:
    for name in names:
        try:
            info = os.lstat(name, dir_fd=dir_fd)
            if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                os.unlink(name, dir_fd=dir_fd)
        except OSError:
            pass


def _cleanup_plaintext(dir_fd: int) -> None:
    """Remove passwords.new and every leftover mosquitto_passwd made from it."""
    try:
        names = os.listdir(dir_fd)
    except OSError:
        return
    _cleanup(dir_fd, "passwords.new",
             *(name for name in names if name.startswith(PLAINTEXT_PREFIX)))


def write_broker_files(root: Path, accounts: list[dict], image: str,
                       runner=default_runner, dir_owner=DIR_OWNER,
                       file_owner=FILE_OWNER, plaintext_owner=PLAINTEXT_OWNER) -> None:
    """Install acl and hashed passwords in $ROOT/config/mosquitto-auth.

    The directory is root:1883 0750 and mounted read-only into the broker; the
    final files are 1883:1883 0600. The temporary plaintext list passwords.new
    (and any ``passwords.new.*`` file mosquitto_passwd derives from it, such as
    its ``.backup.XXXXXX`` copy) is root:root 0600 and is removed on every
    outcome. Nothing under the
    broker-writable data directory is touched.
    """
    owner = _owner(file_owner)
    plain_owner = _owner(plaintext_owner)
    auth = root / "config" / AUTH_DIR_NAME
    dir_fd = open_auth_dir(root, _owner(dir_owner))
    try:
        for name in ("acl", "acl.new", "acl.prev", "acl.prev.tmp",
                     "passwords", "passwords.new", "passwords.prev", "passwords.prev.tmp"):
            _require_regular_or_absent(name, dir_fd)
        try:
            _cleanup_plaintext(dir_fd)  # leftovers of an earlier interrupted run
            _write_new("acl.new", render_acl(accounts).encode("utf-8"), dir_fd, FILE_MODE, owner)
            # Plaintext is root:root 0600 (never the broker's uid) until the root
            # hashing container hashes it and hands over only the hashed file.
            _write_new("passwords.new", render_passwords(accounts).encode("utf-8"),
                       dir_fd, 0o600, plain_owner)
            _keep_previous("acl", dir_fd, owner)
            _keep_previous("passwords", dir_fd, owner)
            result = runner(["docker", "run", "--rm", *HASH_DOCKER_FLAGS,
                             "--entrypoint", "/bin/sh",
                             "-v", f"{auth}:/auth", image, "-c", HASH_SCRIPT])
            if result.returncode != 0:
                detail = (result.stderr or "").strip().splitlines()
                raise MqttAuthError("hashing MQTT passwords in the Mosquitto image failed"
                                    + (f": {detail[-1]}" if detail else ""))
            if _lstat("passwords", dir_fd) is None or _lstat("passwords.new", dir_fd) is not None:
                raise MqttAuthError("MQTT password file was not installed")
            _secure_existing("passwords", dir_fd, owner)
            os.replace("acl.new", "acl", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            _secure_existing("acl", dir_fd, owner)
        except BaseException:
            _cleanup(dir_fd, "acl.new", "acl.prev.tmp", "passwords.prev.tmp")
            _cleanup_plaintext(dir_fd)
            raise
        _cleanup_plaintext(dir_fd)
    finally:
        os.close(dir_fd)


def reload_broker(runner=default_runner, container: str = CONTAINER) -> bool:
    """SIGHUP the broker if it is running so it re-reads passwords and ACL."""
    state = runner(["docker", "inspect", "-f", "{{.State.Running}}", container])
    if state.returncode != 0 or (state.stdout or "").strip() != "true":
        return False
    result = runner(["docker", "kill", "-s", "HUP", container])
    if result.returncode != 0:
        raise MqttAuthError(f"could not signal {container} to reload its ACL")
    return True


def ensure_image(image: str, runner=default_runner) -> None:
    """Make sure the hashing image is local; fail clearly (before any write) if not."""
    if runner(["docker", "image", "inspect", image]).returncode == 0:
        return
    result = runner(["docker", "pull", image])
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise MqttAuthError(
            f"the Mosquitto image {image} is not available locally and could not be pulled"
            + (f" ({detail[-1]})" if detail else "")
            + "; connect to the internet once or load the image, then run the command again."
            " No MQTT access files were changed")


def sync(root: Path, data_dir: Path | None, env: dict, displays_config: Path | None = None,
         image: str | None = None, runner=default_runner,
         dir_owner=DIR_OWNER, file_owner=FILE_OWNER, container: str = CONTAINER,
         compose: Path | None = None) -> dict:
    """Render and install ACL + passwords from env values; returns a summary.

    ``data_dir`` is accepted for compatibility and deliberately unused: the
    broker-writable data directory is never touched by root-run code.
    """
    _check_dir_component(root / "config", "configuration directory", True, 0o755)
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
    image = image or find_image(root, compose)
    ensure_image(image, runner)
    secret_path = root / "config" / SECRETS_NAME
    secret_data = ensure_secrets(secret_path, display_ids, enabled) if enabled else \
        load_secrets(secret_path)
    accounts = build_accounts(admin_user, env["MQTT_PASSWORD"], enabled, bridge_user,
                              display_ids, secret_data)
    write_broker_files(root, accounts, image, runner, dir_owner, file_owner)
    reloaded = reload_broker(runner, container)
    return {"enabled": enabled, "accounts": len(accounts), "displays": len(display_ids),
            "reloaded": reloaded}


# --- configuration lock ------------------------------------------------------
# One exclusive flock on config/.displays.lock serialises every writer of
# displays.json, displays.secrets.json and the MQTT access files: the lifecycle
# commands, sync-mqtt (installer, Control Panel Apply) and bridge-env.

def acquire_lock(config_fd: int, timeout: float = LOCK_TIMEOUT_S) -> int:
    """Take the exclusive lock relative to a no-follow config directory fd.

    Returns the lock fd; closing it releases the lock. Refuses a lock path that
    is not a regular file and never follows a symlink.
    """
    try:
        info = os.lstat(LOCK_NAME, dir_fd=config_fd)
        if not stat.S_ISREG(info.st_mode):
            raise MqttAuthError(f"refusing to use {LOCK_NAME}: it is not a regular file")
    except FileNotFoundError:
        pass
    try:
        fd = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=config_fd)
    except OSError as exc:
        raise MqttAuthError(f"cannot open lock file {LOCK_NAME}: {exc.strerror}") from exc
    owner = _owner(CONFIG_OWNER)
    try:
        if owner:
            os.fchown(fd, *owner)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise MqttAuthError(
                        "another displayctl command is changing the display configuration "
                        f"(waited {timeout:g}s); try again shortly") from None
                time.sleep(0.1)
    except BaseException:
        os.close(fd)
        raise


@contextlib.contextmanager
def config_lock(root: Path, timeout: float = LOCK_TIMEOUT_S):
    """Hold the configuration lock for ``$ROOT/config`` (created if missing)."""
    config = root / "config"
    _check_dir_component(config, "configuration directory", True, 0o755)
    config_fd = os.open(config, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock_fd = -1
    try:
        lock_fd = acquire_lock(config_fd, timeout)
        yield
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(config_fd)


# --- preflight ---------------------------------------------------------------

def preflight(root: Path, env: dict, compose: Path | None = None,
              displays_config: Path | None = None, image: str | None = None,
              image_mode: str = "none", runner=default_runner) -> list[str]:
    """Read-only validation of everything sync would reject; returns warnings.

    Raises MqttAuthError for the first problem. Nothing is written, locked or
    started. ``image_mode``: ``none`` skips the image; ``check`` requires it to
    be local, or at least pullable (``docker manifest inspect``); ``pull`` pulls
    a missing image now (before the installer changes anything).
    """
    warnings: list[str] = []
    enabled_text = env.get("DISPLAYS_ENABLE", "false")
    if enabled_text not in ("true", "false"):
        raise MqttAuthError("DISPLAYS_ENABLE must be true or false")
    enabled = enabled_text == "true"
    admin_user = _check_username("MQTT_USERNAME", env.get("MQTT_USERNAME", ""))
    password = env.get("MQTT_PASSWORD", "")
    if password and password != "GENERATE":  # an empty/GENERATE value is replaced by a safe one
        _check_password("MQTT_PASSWORD", password)
    bridge_user = env.get("DISPLAYS_BRIDGE_MQTT_USERNAME") or DEFAULT_BRIDGE_USERNAME
    if displays_config is None:
        displays_config = root / "config" / "displays.json"
    display_ids = load_display_ids(displays_config, enabled)
    placeholder = {"bridge": {"mqtt_password": "x"},
                   "displays": {i: {"mqtt_password": "x"} for i in display_ids}}
    build_accounts(admin_user, "x", enabled, bridge_user, display_ids, placeholder)
    if image_mode != "none":
        image = image or find_image(root, compose)
        if image_mode == "pull":
            ensure_image(image, runner)
        elif runner(["docker", "image", "inspect", image]).returncode != 0:
            if runner(["docker", "manifest", "inspect", image]).returncode == 0:
                warnings.append(f"the Mosquitto image {image} is not local yet and will be "
                                "pulled when the installer runs")
            else:
                raise MqttAuthError(
                    f"the Mosquitto image {image} is not available locally and the registry "
                    f"cannot be reached; run 'docker pull {image}' while online (or load it "
                    "with 'docker load'), then run the installer again")
    return warnings
