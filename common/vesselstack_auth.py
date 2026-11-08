"""Shared sign-in primitives for VesselStack services (standard library only).

Installed root-owned (0644) at ``$VESSELSTACK_ROOT/common/`` and loaded by file
path, so it must never import anything from the repository.

Provides:
  * scrypt passphrase hashing and constant-time verification,
  * ``AuthStore``: an atomically written JSON store of the passphrase hash and
    signed-in devices (only the SHA-256 of each device token is stored),
  * ``LoginThrottle``: per-address exponential backoff plus a global lockout.

The store is keyed by scope-neutral records so later slices (Boat Chat, pairing
links) can reuse it: ``pairings`` is reserved and preserved untouched.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

SCHEMA = 1
ROLES = ("access", "admin")
MIN_PASSPHRASE_LENGTH = 12
MAX_PASSPHRASE_LENGTH = 256
MAX_DEVICES = 20
SESSION_SECONDS = 12 * 3600
REMEMBER_SECONDS = 30 * 24 * 3600
LAST_SEEN_INTERVAL = 3600
MAX_LABEL_LENGTH = 60
STORE_NAME = "control-panel-auth.json"
CHAT_STORE_NAME = "auth.json"
CHAT_MIN_PASSPHRASE_LENGTH = 8

SCRYPT_LOG2_N = 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
SALT_BYTES = 16
HASH_BYTES = 32

# One scrypt verification at a time: each costs ~32 MiB and ~100 ms of CPU.
_VERIFY_LOCK = threading.Lock()


class AuthError(Exception):
    """A user-correctable problem; the message is safe to show."""


class DeviceLimitError(AuthError):
    pass


class PassphraseChanged(AuthError):
    """The passphrase changed between the caller's check and the write."""


class StoreError(Exception):
    """The store could not be read or written safely."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def check_new_passphrase(passphrase: Any, minimum: int = MIN_PASSPHRASE_LENGTH) -> str:
    if not isinstance(passphrase, str):
        raise AuthError("A passphrase is required.")
    if len(passphrase) < minimum:
        raise AuthError(f"Use at least {minimum} characters; a few random words work well.")
    if len(passphrase) > MAX_PASSPHRASE_LENGTH:
        raise AuthError(f"Use at most {MAX_PASSPHRASE_LENGTH} characters.")
    return passphrase


_ANY = object()
_HASH_PATTERN = re.compile(r"^scrypt:(1[0-9]|20):([1-9]|1[0-6]):[1-4]:[A-Za-z0-9+/]{22}==:[A-Za-z0-9+/]{43}=$")


def valid_hash(value: Any) -> bool:
    """Strict shape check for a stored/imported hash (what ``hash_passphrase`` produces, within verify's bounds)."""
    return isinstance(value, str) and len(value) <= 200 and bool(_HASH_PATTERN.match(value))


def hash_passphrase(passphrase: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    with _VERIFY_LOCK:
        digest = hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=2 ** SCRYPT_LOG2_N, r=SCRYPT_R,
                                p=SCRYPT_P, maxmem=SCRYPT_MAXMEM, dklen=HASH_BYTES)
    return f"scrypt:{SCRYPT_LOG2_N}:{SCRYPT_R}:{SCRYPT_P}:{_b64(salt)}:{_b64(digest)}"


def verify_passphrase(passphrase: Any, stored: Any) -> bool:
    """Constant-time check; any malformed input is simply False."""
    if not isinstance(passphrase, str) or not isinstance(stored, str) or len(passphrase) > 1024:
        return False
    try:
        scheme, log2n, r, p, salt_text, hash_text = stored.split(":")
        if scheme != "scrypt":
            return False
        log2n, r, p = int(log2n), int(r), int(p)
        if not (10 <= log2n <= 20 and 1 <= r <= 16 and 1 <= p <= 4):
            return False
        salt, expected = _unb64(salt_text), _unb64(hash_text)
        with _VERIFY_LOCK:
            actual = hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=2 ** log2n, r=r, p=p,
                                    maxmem=SCRYPT_MAXMEM, dklen=len(expected))
    except (ValueError, TypeError, UnicodeError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


BINDING_PREFIX = "h1:"
MAX_BINDING_LENGTH = 80
_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def admin_binding(key: Any, token: Any) -> str:
    """Generation marker for admin devices: HMAC-SHA256 of the admin code (BOAT_CHAT_SETTINGS_TOKEN) under the
    store's random ``binding_key``, or "none" when no admin code is configured.

    Bindings from before this format (plain SHA-256, no prefix) never equal a computed one, so such devices read as
    non-admin (fail closed)."""
    text = token.strip() if isinstance(token, str) else ""
    if not text:
        return "none"
    if not isinstance(key, str) or not _KEY_PATTERN.match(key):
        raise StoreError("the sign-in store has no usable binding key")
    return BINDING_PREFIX + hmac.new(key.encode("ascii"), text.encode("utf-8"), hashlib.sha256).hexdigest()


def read_env_value(path: str | os.PathLike[str], key: str) -> str | None:
    """Value of ``key`` in a KEY=VALUE env file (last assignment wins, shell-style quoting like Boat Chat's own parser),
    or None when the file is unreadable (not a regular file, a symlink, missing) or the key is absent. Read-only."""
    import shlex
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            lines = handle.read(1024 * 1024).decode("utf-8", "ignore").splitlines()
    except OSError:
        return None
    finally:
        os.close(fd)
    found: str | None = None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != key:
            continue
        try:
            parsed = shlex.split(value, comments=False, posix=True)
            found = " ".join(parsed) if parsed else ""
        except ValueError:
            found = value.strip().strip("'\"")
    return found


def effective_chat_setting(key: str, override_path: str | os.PathLike[str] | None, base_path: str | os.PathLike[str] | None) -> str:
    """The value Boat Chat itself resolves for ``key``: the Settings-page override file wins when it names the key
    (even with an empty value), otherwise the root-managed ``boat-chat.env``. Stripped; "" when unset."""
    for path in (override_path, base_path):
        if path:
            value = read_env_value(path, key)
            if value is not None:
                return value.strip()
    return ""


def clean_label(label: Any) -> str:
    text = "".join(ch for ch in str(label or "") if ch.isprintable()).strip()
    return text[:MAX_LABEL_LENGTH] or "Unnamed device"


PAIRING_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U
PAIRING_CODE_LENGTH = 10
PAIRING_TTL_SECONDS = 600
MAX_PAIRINGS = 3


def new_pairing_code() -> str:
    return "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))


def normalize_pairing_code(text: Any) -> str | None:
    """Canonical code from what a person typed (case, dashes, spaces ignored; I/L -> 1, O -> 0), else None."""
    if not isinstance(text, str) or len(text) > 64:
        return None
    cleaned = "".join(ch for ch in text.upper() if ch not in "- \t\r\n").translate(str.maketrans("ILO", "110"))
    if len(cleaned) != PAIRING_CODE_LENGTH or any(ch not in PAIRING_ALPHABET for ch in cleaned):
        return None
    return cleaned


def format_pairing_code(code: str) -> str:
    return f"{code[:5]}-{code[5:]}"


def _empty() -> dict[str, Any]:
    return {"schema": SCHEMA, "passphrase_hash": None, "devices": [], "pairings": [], "binding_key": None}


def _whole(item: dict[str, Any], key: str) -> int:
    """A stored timestamp: absent/null is 0; anything but a number is corruption (TypeError, handled by the reader)."""
    value = item.get(key)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{key} is not a number")
    return int(value)


def _binding(item: dict[str, Any]) -> str:
    value = item.get("binding")
    return value if isinstance(value, str) and len(value) <= MAX_BINDING_LENGTH else ""


def _normalize(data: Any) -> dict[str, Any]:
    """Validated copy of a store. Wrong types raise (StoreError, or TypeError/OverflowError that ``_read_at`` maps to
    StoreError): a store that cannot be understood is never repaired or overwritten."""
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise StoreError("unsupported or malformed sign-in store")
    result = dict(data)
    stored = result.get("passphrase_hash")
    result["passphrase_hash"] = stored if isinstance(stored, str) and stored else None
    key = result.get("binding_key")
    result["binding_key"] = key if isinstance(key, str) and _KEY_PATTERN.match(key) else None
    for name in ("devices", "pairings"):
        if result.get(name) is None:
            result[name] = []
        if not isinstance(result[name], list):
            raise StoreError("malformed sign-in store")
    devices = []
    for item in result["devices"]:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("token_sha256"), str):
            devices.append({
                "id": item["id"], "token_sha256": item["token_sha256"], "label": clean_label(item.get("label")),
                "created": _whole(item, "created"), "last_seen": _whole(item, "last_seen"), "expires": _whole(item, "expires"),
                "role": item.get("role") if item.get("role") in ROLES else "access", "binding": _binding(item),
            })
    result["devices"] = devices
    pairings = []
    for item in result["pairings"]:
        if isinstance(item, dict) and isinstance(item.get("code_sha256"), str) and isinstance(item.get("id"), str):
            pairings.append({"id": item["id"], "code_sha256": item["code_sha256"], "label": clean_label(item.get("label")) if item.get("label") else "",
                             "created": _whole(item, "created"), "expires": _whole(item, "expires"),
                             "role": item.get("role") if item.get("role") in ROLES else "access", "binding": _binding(item)})
    result["pairings"] = pairings
    return result


class AuthStore:
    """JSON store: flock-serialised read-modify-write, atomic replace, mtime-aware reads."""

    def __init__(self, path: str | os.PathLike[str], *, clock: Callable[[], float] = time.time,
                 min_passphrase: int = MIN_PASSPHRASE_LENGTH, expose_role: bool = False,
                 inherit_owner: bool = False, differs_from: Callable[[], Any] | None = None,
                 differs_message: str = "Choose a passphrase different from the other VesselStack passphrase.",
                 runner: Callable[..., Any] | None = None, helper_path: str | os.PathLike[str] | None = None) -> None:
        """``inherit_owner``: the store lives in a service user's directory; when root uses it, mutations run in the
        helper process (vesselstack_authctl.py) started with that directory owner's identity (root never opens or
        chowns files there). ``runner`` replaces ``subprocess.run`` (tests).
        ``differs_from``: returns a stored hash (or None) the new passphrase must not equal."""
        self.path = Path(path)
        self.clock = clock
        self.min_passphrase = min_passphrase
        self.expose_role = expose_role
        self.inherit_owner = inherit_owner
        self.differs_from = differs_from
        self.differs_message = differs_message
        self.runner = runner
        self.helper_path = Path(helper_path) if helper_path else Path(__file__).with_name("vesselstack_authctl.py")
        self._mutex = threading.RLock()
        self._cache: dict[str, Any] | None = None
        self._signature: tuple[int, int, int] | None = None

    # -- file access -------------------------------------------------------
    # Every file operation is relative to a directory descriptor opened with O_NOFOLLOW, and every file
    # opened is checked (regular, single link) before use. Nothing here ever chowns: when root must touch a
    # directory owned by a service user, mutations run in a helper process that has dropped privileges.
    _CLOEXEC = getattr(os, "O_CLOEXEC", 0)

    def _euid(self) -> int:
        return os.geteuid()

    def _parent_owner(self) -> tuple[int, int]:
        info = os.stat(self.path.parent)
        return info.st_uid, info.st_gid

    def _open_dir(self) -> int:
        try:
            return os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | self._CLOEXEC)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise StoreError(f"cannot open the sign-in store directory: {exc.strerror}") from exc

    @staticmethod
    def _require_plain_file(fd: int, what: str) -> os.stat_result:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise StoreError(f"the sign-in {what} is not a regular file")
        if info.st_nlink != 1:
            raise StoreError(f"the sign-in {what} has more than one hard link")
        return info

    def _read_disk(self) -> tuple[dict[str, Any], tuple[int, int, int] | None]:
        try:
            dir_fd = self._open_dir()
        except FileNotFoundError:
            return _empty(), None
        try:
            return self._read_at(dir_fd)
        finally:
            os.close(dir_fd)

    def _read_at(self, dir_fd: int) -> tuple[dict[str, Any], tuple[int, int, int] | None]:
        try:
            fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | self._CLOEXEC, dir_fd=dir_fd)
        except FileNotFoundError:
            return _empty(), None
        except OSError as exc:
            raise StoreError(f"cannot open the sign-in store: {exc.strerror}") from exc
        try:
            info = self._require_plain_file(fd, "store")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(1024 * 1024)
        finally:
            os.close(fd)
        try:
            data = _normalize(json.loads(raw.decode("utf-8")))
        except (ValueError, UnicodeError, TypeError, AttributeError, KeyError, OverflowError, RecursionError) as exc:
            # Wrong types anywhere in the file are corruption too: fail closed (no passphrase, no devices) and never
            # let a mutation overwrite what could not be understood.
            raise StoreError("the sign-in store is not valid JSON") from exc
        return data, (info.st_mtime_ns, info.st_ino, info.st_size)

    def _stat_signature(self) -> tuple[int, int, int] | None:
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return None
        return (info.st_mtime_ns, info.st_ino, info.st_size)

    def snapshot(self) -> dict[str, Any]:
        """Current state; re-read only when the file changed. A broken store reads as empty (fail closed)."""
        with self._mutex:
            signature = self._stat_signature()
            if self._cache is None or signature != self._signature:
                try:
                    self._cache, self._signature = self._read_disk()
                except StoreError:
                    self._cache, self._signature = _empty(), signature
            return self._cache

    def _write_at(self, dir_fd: int, data: dict[str, Any]) -> None:
        name = self.path.name
        mode = 0o600
        try:
            existing = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | self._CLOEXEC, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise StoreError(f"cannot open the sign-in store: {exc.strerror}") from exc
        else:
            try:
                mode = stat.S_IMODE(self._require_plain_file(existing, "store").st_mode)
            finally:
                os.close(existing)
        temp = f".{name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | self._CLOEXEC, 0o600, dir_fd=dir_fd)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(json.dumps(data, indent=2, sort_keys=True).encode("utf-8") + b"\n")
                handle.flush()
                os.fchmod(handle.fileno(), mode)
                os.fsync(handle.fileno())
            os.rename(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(temp, dir_fd=dir_fd)
            except OSError:
                pass
            raise
        os.fsync(dir_fd)

    def _transaction_local(self, mutate: Callable[[dict[str, Any]], Any]) -> Any:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        dir_fd = self._open_dir()
        try:
            lock_fd = os.open(self.path.name + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | self._CLOEXEC,
                              0o600, dir_fd=dir_fd)
            try:
                self._require_plain_file(lock_fd, "lock file")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                data, _ = self._read_at(dir_fd)
                result = mutate(data)
                self._write_at(dir_fd, data)
                return result
            finally:
                os.close(lock_fd)
        finally:
            os.close(dir_fd)

    def _foreign_owner(self) -> tuple[int, int] | None:
        """(uid, gid) of the directory owner when root would otherwise be working in another user's directory."""
        if not self.inherit_owner or self._euid() != 0:
            return None
        try:
            uid, gid = self._parent_owner()
        except FileNotFoundError:
            raise StoreError("the service's sign-in directory does not exist yet; root does not create it") from None
        return (uid, gid) if uid != 0 else None

    def _helper_call(self, owner: tuple[int, int], args: list[str], payload: dict[str, Any]) -> dict[str, Any]:
        """Run the sign-in helper as a fresh process with the directory owner's identity (never a fork of this process).

        Root cannot hand a closure to another process, so the few mutations root needs (set/clear passphrase, pairing
        links, revoke) are helper subcommands with JSON on standard input and output."""
        uid, gid = owner
        command = [sys.executable, "-I", str(self.helper_path), *args, "--json"]
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "VESSELSTACK_DATA": str(self.path.parent.parent),
               "VESSELSTACK_ROOT": str(Path(self.helper_path).parent.parent)}
        runner = self.runner or subprocess.run
        try:
            done = runner(command, user=uid, group=gid, extra_groups=[], input=json.dumps(payload).encode("utf-8"),
                          capture_output=True, timeout=30, env=env, cwd="/")
        except (OSError, subprocess.SubprocessError) as exc:
            raise StoreError("the sign-in store helper could not run") from exc
        try:
            reply = json.loads((done.stdout or b"").decode("utf-8"))
            if not isinstance(reply, dict):
                raise ValueError
        except (ValueError, UnicodeError) as exc:
            raise StoreError("the sign-in store helper failed") from exc
        if not reply.get("ok"):
            kind = {"limit": DeviceLimitError, "auth": AuthError, "changed": PassphraseChanged}.get(reply.get("kind"), StoreError)
            raise kind(str(reply.get("message") or "sign-in store problem"))
        return reply

    def transaction(self, mutate: Callable[[dict[str, Any]], Any]) -> Any:
        """Run ``mutate(data)`` under an exclusive lock on fresh data, then persist.

        Root never mutates a store in a directory owned by another user in this process."""
        with self._mutex:
            try:
                if self._foreign_owner() is not None:
                    raise StoreError("root must change this sign-in store through the helper process, not in-process")
                return self._transaction_local(mutate)
            finally:
                self._cache, self._signature = None, None

    def _mutating(self, args: list[str], payload: dict[str, Any], key: str, local: Callable[[], Any]) -> Any:
        """Run ``local()`` in-process, or the helper subcommand ``args`` when root faces a foreign-owned directory."""
        with self._mutex:
            owner = self._foreign_owner()
            if owner is None:
                return local()
            try:
                return self._helper_call(owner, args, payload)[key]
            finally:
                self._cache, self._signature = None, None

    # -- passphrase --------------------------------------------------------
    def passphrase_set(self) -> bool:
        return self.snapshot()["passphrase_hash"] is not None

    def check_passphrase(self, passphrase: Any) -> bool:
        stored = self.snapshot()["passphrase_hash"]
        return stored is not None and verify_passphrase(passphrase, stored)

    def current_hash(self) -> str | None:
        """The stored passphrase hash, read fresh (use as ``expect_hash`` for a compare-and-set)."""
        return self.snapshot()["passphrase_hash"]

    def set_passphrase(self, passphrase: str, *, keep_device: str | None = None, expect_hash: Any = _ANY) -> int:
        """Set/replace the passphrase and revoke every device except ``keep_device``. Returns revoked count.

        ``expect_hash``: compare-and-set. The write happens only if the stored hash (None = unset) still equals it
        when read under the store lock; otherwise ``PassphraseChanged``."""
        checked = check_new_passphrase(passphrase, self.min_passphrase)
        other = self.differs_from() if self.differs_from else None
        if other and verify_passphrase(checked, other):
            raise AuthError(self.differs_message)
        return self.set_passphrase_hash(hash_passphrase(checked), keep_device=keep_device, expect_hash=expect_hash)

    def set_passphrase_hash(self, encoded: str, *, keep_device: str | None = None, expect_hash: Any = _ANY) -> int:
        """Store an already computed hash (strictly validated); see ``set_passphrase``."""
        if not valid_hash(encoded):
            raise AuthError("That is not a valid passphrase hash.")

        def mutate(data: dict[str, Any]) -> int:
            if expect_hash is not _ANY and data["passphrase_hash"] != expect_hash:
                raise PassphraseChanged("The passphrase changed while you were doing this. Try again.")
            before = len(data["devices"])
            data["passphrase_hash"] = encoded
            data["devices"] = [d for d in data["devices"] if keep_device and d["id"] == keep_device]
            data["pairings"] = []
            return before - len(data["devices"])
        payload: dict[str, Any] = {"hash": encoded, "keep_device": keep_device}
        if expect_hash is not _ANY:
            payload["expect_hash"] = expect_hash
        return self._mutating(["passphrase", "set-hash", "chat"], payload, "revoked", lambda: self.transaction(mutate))

    def clear_passphrase(self) -> int:
        def mutate(data: dict[str, Any]) -> int:
            count = len(data["devices"])
            data["passphrase_hash"] = None
            data["devices"] = []
            data["pairings"] = []
            return count
        return self._mutating(["passphrase", "clear", "chat"], {}, "revoked", lambda: self.transaction(mutate))

    # -- devices -----------------------------------------------------------
    def _add_device(self, data: dict[str, Any], digest: str, label: Any, remember: bool, role: str, now: int, binding: str = "") -> dict[str, Any]:
        data["devices"] = [d for d in data["devices"] if d["expires"] > now]
        if len(data["devices"]) >= MAX_DEVICES:
            raise DeviceLimitError(f"This boat already has {MAX_DEVICES} signed-in devices. Revoke one in Settings, or on the boat computer with: sudo vesselstackctl devices list")
        device = {"id": secrets.token_hex(6), "token_sha256": digest, "label": clean_label(label),
                  "created": now, "last_seen": now, "expires": now + (REMEMBER_SECONDS if remember else SESSION_SECONDS),
                  "role": role if role in ROLES else "access", "binding": binding}
        data["devices"].append(device)
        return device

    def create_device(self, label: Any, remember: bool, role: str = "access") -> tuple[str, dict[str, Any]]:
        token = secrets.token_urlsafe(32)
        digest = token_digest(token)
        now = int(self.clock())
        device = self.transaction(lambda data: self._add_device(data, digest, label, remember, role, now))
        return token, self.public_device(device)

    # -- pairing links -----------------------------------------------------
    def create_pairing(self, role: str = "access", label: Any = "", binding: str = "") -> tuple[str, int]:
        """Record a single-use pairing code (only its SHA-256 is stored). Returns (code, expires_epoch).

        An admin pairing needs ``binding`` (see ``admin_binding``): the device stops being admin when the admin code changes."""
        if role not in ROLES or (role == "admin" and not self.expose_role):
            raise AuthError("Admin pairing is only available for Boat Chat.")
        if role == "admin" and (not isinstance(binding, str) or not binding.startswith(BINDING_PREFIX) or len(binding) > MAX_BINDING_LENGTH):
            raise AuthError("Admin pairing needs an admin code: set BOAT_CHAT_SETTINGS_TOKEN first.")
        if role != "admin":
            binding = ""
        code = new_pairing_code()
        digest = token_digest(code)
        now = int(self.clock())
        expires = now + PAIRING_TTL_SECONDS
        text = clean_label(label) if label else ""

        def mutate(data: dict[str, Any]) -> int:
            data["pairings"] = [p for p in data["pairings"] if p["expires"] > now]
            if len(data["pairings"]) >= MAX_PAIRINGS:
                raise AuthError(f"{MAX_PAIRINGS} pairing links are already waiting to be used. They expire after 10 minutes.")
            data["pairings"].append({"id": secrets.token_hex(6), "code_sha256": digest, "label": text,
                                     "created": now, "expires": expires, "role": role, "binding": binding})
            return expires
        if self._foreign_owner() is not None:
            reply = self._helper_call(self._foreign_owner(), ["pairing", "create", "chat"], {"role": role, "label": text, "binding": binding})  # type: ignore[arg-type]
            return str(reply["code"]), int(reply["expires"])
        return code, self.transaction(mutate)

    def redeem_pairing(self, code: str, label: Any) -> tuple[str, dict[str, Any]] | None:
        """Exchange a valid code for a remembered device (single use). None when the code is unknown or expired."""
        digest = token_digest(code)
        token = secrets.token_urlsafe(32)
        token_hash = token_digest(token)
        now = int(self.clock())

        def mutate(data: dict[str, Any]) -> dict[str, Any] | None:
            data["pairings"] = [p for p in data["pairings"] if p["expires"] > now]
            found = None
            for record in data["pairings"]:  # compare every record so timing does not reveal a match
                if hmac.compare_digest(record["code_sha256"], digest):
                    found = record
            if found is None:
                return None
            device = self._add_device(data, token_hash, label or found.get("label"), True, found["role"], now, found.get("binding", ""))
            data["pairings"] = [p for p in data["pairings"] if p["id"] != found["id"]]
            return device
        device = self.transaction(mutate)
        return None if device is None else (token, self.public_device(device, role=True))

    def pending_pairings(self) -> int:
        now = int(self.clock())
        return sum(1 for p in self.snapshot()["pairings"] if p["expires"] > now)

    def verify_device_token(self, token: Any) -> dict[str, Any] | None:
        if not isinstance(token, str) or not (20 <= len(token) <= 128):
            return None
        digest = token_digest(token)
        now = int(self.clock())
        found = None
        for device in self.snapshot()["devices"]:
            # Compare every record so timing does not reveal which one matched.
            if hmac.compare_digest(device["token_sha256"], digest) and device["expires"] > now:
                found = device
        if found is None:
            return None
        if now - found["last_seen"] >= LAST_SEEN_INTERVAL:
            self._touch(found["id"], now)
        return {**self.public_device(found, role=True), "binding": found.get("binding", "")}

    def binding_key(self) -> str | None:
        return self.snapshot().get("binding_key")

    def ensure_binding_key(self) -> str:
        """The random key behind admin bindings, created on first need (in the helper process when root faces a
        service-owned directory)."""
        existing = self.binding_key()
        if existing:
            return existing

        def mutate(data: dict[str, Any]) -> str:
            if not data.get("binding_key"):
                data["binding_key"] = secrets.token_hex(32)
            return data["binding_key"]
        return self._mutating(["binding-key", "ensure", "chat"], {}, "key", lambda: self.transaction(mutate))

    def admin_binding(self, token: Any, *, create: bool = False) -> str | None:
        """Binding for the admin code ``token`` ("none" when empty). None when no key exists yet and ``create`` is false
        (nothing can have been bound), so a check never writes."""
        text = token.strip() if isinstance(token, str) else ""
        if not text:
            return "none"
        key = self.ensure_binding_key() if create else self.binding_key()
        return admin_binding(key, text) if key else None

    def prune_admin_devices(self, current_binding: str) -> int:
        """Drop admin devices (and waiting admin links) paired under a different admin code. Runs as the store's owner."""
        def mutate(data: dict[str, Any]) -> int:
            before = len(data["devices"])
            data["devices"] = [d for d in data["devices"] if d["role"] != "admin" or d.get("binding") == current_binding]
            data["pairings"] = [p for p in data["pairings"] if p["role"] != "admin" or p.get("binding") == current_binding]
            return before - len(data["devices"])
        return self.transaction(mutate)

    def _touch(self, device_id: str, now: int) -> None:
        def mutate(data: dict[str, Any]) -> None:
            for device in data["devices"]:
                if device["id"] == device_id:
                    device["last_seen"] = now
        try:
            self.transaction(mutate)
        except (OSError, StoreError):
            pass  # a read-only disk must not sign people out

    def list_devices(self, current_id: str | None = None) -> list[dict[str, Any]]:
        now = int(self.clock())
        return [{**self.public_device(d, role=self.expose_role), "current": d["id"] == current_id}
                for d in self.snapshot()["devices"] if d["expires"] > now]

    def revoke(self, device_id: str) -> bool:
        def mutate(data: dict[str, Any]) -> bool:
            before = len(data["devices"])
            data["devices"] = [d for d in data["devices"] if d["id"] != device_id]
            return len(data["devices"]) != before
        return self._mutating(["devices", "revoke", "chat"], {"id": device_id}, "found", lambda: self.transaction(mutate))

    def revoke_all(self, *, keep: str | None = None) -> int:
        def mutate(data: dict[str, Any]) -> int:
            before = len(data["devices"])
            data["devices"] = [d for d in data["devices"] if keep and d["id"] == keep]
            data["pairings"] = []
            return before - len(data["devices"])
        return self._mutating(["devices", "revoke-all", "chat"], {"keep": keep}, "revoked", lambda: self.transaction(mutate))

    def public_device(self, device: dict[str, Any], role: bool | None = None) -> dict[str, Any]:
        keys = ["id", "label", "created", "last_seen", "expires"]
        if self.expose_role if role is None else role:
            keys.append("role")
        return {key: device[key] for key in keys}


class Attempt:
    """Handle for one attempt admitted by ``LoginThrottle.begin()``."""

    __slots__ = ("ip", "stamp_id", "set_block", "prev_block")

    def __init__(self, ip: str, stamp_id: int, set_block: float | None = None, prev_block: float = 0.0) -> None:
        self.ip = ip
        self.stamp_id = stamp_id
        self.set_block = set_block  # the backoff deadline this very attempt put in place, if any
        self.prev_block = prev_block


class LoginThrottle:
    """Per-address exponential backoff, plus a global lockout for shared (tunnel) addresses.

    ``begin()`` is the atomic entry point: under one lock it checks both limits and, when the attempt
    is allowed, records it as a failure up front ("pending") and returns an ``Attempt`` handle.
    Concurrent requests therefore cannot all pass the check before any failure is counted.
    ``success(handle)`` withdraws only that one attempt: its slot in the global window, its own count against the
    address, and the backoff deadline that very attempt set. Every other recorded failure (the caller's or anyone
    else's) stays counted and the address's backoff is NOT cleared by a success; it decays with time (an hour
    of quiet forgives the count). Tradeoff: bad attempts from many addresses can still trip the global lockout
    (more than GLOBAL_LIMIT in GLOBAL_WINDOW), which blocks passphrase and pairing sign-in for everyone until it
    expires or the service restarts; access codes and tokens are not throttled.
    ``failure(handle)`` just confirms the attempt (no double count).
    """

    IP_FREE_FAILURES = 5
    IP_BASE_DELAY = 30
    MAX_DELAY = 15 * 60
    GLOBAL_LIMIT = 20
    GLOBAL_WINDOW = 15 * 60
    GLOBAL_LOCKOUT = 15 * 60
    MAX_TRACKED = 1024

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._ips: dict[str, dict[str, float]] = {}
        self._global: deque[tuple[int, float]] = deque()
        self._global_until = 0.0
        self._pending: set[int] = set()
        self._next_id = 0

    def _wait_locked(self, ip: str, now: float) -> float:
        wait = max(self._global_until - now, 0.0)
        entry = self._ips.get(ip)
        if entry:
            wait = max(wait, entry["blocked_until"] - now)
        return wait

    def retry_after(self, ip: str) -> int:
        """Seconds until a passphrase attempt is allowed again (0 means allowed now). Read-only."""
        now = self.clock()
        with self._lock:
            wait = self._wait_locked(ip, now)
        return int(wait) + 1 if wait > 0 else 0

    def begin(self, ip: str) -> tuple[bool, int, Attempt | None]:
        """Atomically check the limits and count this attempt. Returns (allowed, retry_after_seconds, handle)."""
        now = self.clock()
        with self._lock:
            wait = self._wait_locked(ip, now)
            if wait > 0:
                return False, int(wait) + 1, None
            before = self._ips.get(ip, {}).get("blocked_until", 0.0)
            stamp_id = self._record_locked(ip, now)
            after = self._ips.get(ip, {}).get("blocked_until", 0.0)
            self._pending.add(stamp_id)
            if len(self._pending) > 4 * self.MAX_TRACKED:
                live = {entry[0] for entry in self._global}
                self._pending &= live
        return True, 0, Attempt(ip, stamp_id, after if after != before else None, before)

    def _record_locked(self, ip: str, now: float) -> int:
        entry = self._ips.setdefault(ip, {"count": 0, "blocked_until": 0.0, "last": now})
        if now - entry["last"] > 3600 and entry["blocked_until"] <= now:
            entry["count"] = 0  # an hour of quiet forgives old failures
        entry["count"] += 1
        entry["last"] = now
        over = entry["count"] - self.IP_FREE_FAILURES
        if over >= 0:
            entry["blocked_until"] = now + min(self.IP_BASE_DELAY * (2 ** over), self.MAX_DELAY)
        if len(self._ips) > self.MAX_TRACKED:
            for stale in sorted(self._ips, key=lambda key: self._ips[key]["last"])[: len(self._ips) - self.MAX_TRACKED]:
                del self._ips[stale]
        self._next_id += 1
        stamp_id = self._next_id
        self._global.append((stamp_id, now))
        while self._global and now - self._global[0][1] > self.GLOBAL_WINDOW:
            self._global.popleft()
        if len(self._global) > self.GLOBAL_LIMIT:
            self._global_until = now + self.GLOBAL_LOCKOUT
            self._global.clear()
        return stamp_id

    def failure(self, attempt: "Attempt | str") -> None:
        """Confirm a failed attempt. A handle from ``begin()`` is already counted; a bare address is counted now."""
        with self._lock:
            if isinstance(attempt, Attempt):
                self._pending.discard(attempt.stamp_id)
                return
            self._record_locked(str(attempt), self.clock())

    def success(self, attempt: Attempt) -> None:
        """A correct credential: withdraw only this attempt (see the class docstring); other backoff stays."""
        if not isinstance(attempt, Attempt):
            raise TypeError("success() needs the handle returned by begin()")
        with self._lock:
            entry = self._ips.get(attempt.ip)
            if entry is not None and attempt.stamp_id in self._pending:
                entry["count"] = max(entry["count"] - 1, 0)
                if attempt.set_block is not None and entry["blocked_until"] == attempt.set_block:
                    entry["blocked_until"] = attempt.prev_block
            self._pending.discard(attempt.stamp_id)
            for index, (stamp_id, _) in enumerate(self._global):
                if stamp_id == attempt.stamp_id:
                    del self._global[index]
                    break


def _is_wildcard(host: str) -> bool:
    return host in {"", "0.0.0.0", "::", "[::]", "*"}


def _is_loopback(host: str) -> bool:
    return host == "localhost" or host.startswith("127.") or host in {"::1", "[::1]"}


class HostAllowlist:
    """Accepts only Host headers naming this machine (defence against DNS rebinding).

    Always allowed: 127.0.0.1, localhost and [::1]. When the service listens beyond loopback (LAN mode)
    the machine's hostname, ``hostname.local`` and its current interface addresses are added; the set is
    recomputed at most every ``REFRESH_SECONDS`` so a DHCP change is picked up. Operators can add names
    with ``extra`` (host or host:port entries). Every entry needs the configured port; a Host without a
    port is accepted only when the port is 80.
    """

    REFRESH_SECONDS = 60

    def __init__(self, bind_host: str, port: int, *, extra: Callable[[], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 discover: Callable[[], set[str]] | None = None) -> None:
        self.bind_host = (bind_host or "").strip().lower()
        self.port = int(port)
        self._extra = extra
        self._clock = clock
        self._discover = discover or discover_local_names
        self._lock = threading.Lock()
        self._names: set[str] = set()
        self._expires = float("-inf")

    @property
    def lan(self) -> bool:
        return not _is_loopback(self.bind_host)

    def _compute(self) -> set[str]:
        names = {"127.0.0.1", "localhost", "[::1]"}
        if self.bind_host and not _is_wildcard(self.bind_host):
            names.add(self.bind_host if not (":" in self.bind_host and not self.bind_host.startswith("[")) else f"[{self.bind_host}]")
        if self.lan:
            names |= {name.lower() for name in self._discover()}
        raw = self._extra() if self._extra else ""
        if isinstance(raw, str):
            raw = [part for part in raw.split(",")]
        for item in raw or ():
            host, port = self._split(str(item))
            if host and port in (None, self.port):
                names.add(host)
        return names

    @staticmethod
    def _split(value: str) -> tuple[str, int | None] | tuple[None, None]:
        """(host, port) from ``host``, ``host:port``, ``[v6]`` or ``[v6]:port``; (None, None) when malformed."""
        value = value.strip().lower()
        if not value or any(ch in value for ch in " /\\@?#\t\r\n"):
            return None, None
        if value.startswith("["):
            end = value.find("]")
            if end < 0:
                return None, None
            host, rest = value[: end + 1], value[end + 1:]
        elif value.count(":") > 1:
            return None, None  # unbracketed IPv6 is not a valid Host
        else:
            host, _, port_text = value.partition(":")
            rest = ":" + port_text if ":" in value else ""
        if not host or host in {"[]"}:
            return None, None
        if not rest:
            return host, None
        if not rest.startswith(":") or not rest[1:].isdigit() or not (0 < int(rest[1:]) < 65536):
            return None, None
        return host, int(rest[1:])

    def allowed(self, host_header: Any) -> bool:
        if not isinstance(host_header, str):
            return False
        host, port = self._split(host_header)
        if host is None:
            return False
        if (port if port is not None else 80) != self.port:
            # extra entries may carry their own port
            return self._extra_with_port(host, port)
        now = self._clock()
        with self._lock:
            if now >= self._expires:
                try:
                    self._names = self._compute()
                except Exception:
                    self._names = {"127.0.0.1", "localhost", "[::1]"}
                self._expires = now + self.REFRESH_SECONDS
            names = self._names
        if host in names:
            return True
        return self.lan and _is_local_address(host)

    def _extra_with_port(self, host: str, port: int | None) -> bool:
        if port is None:
            return False
        raw = self._extra() if self._extra else ""
        items = raw.split(",") if isinstance(raw, str) else list(raw or ())
        return any(self._split(str(item)) == (host, port) for item in items if str(item).strip())


def _is_local_address(host: str) -> bool:
    """True when ``host`` is an IP literal currently assigned to this machine (bind probe; no DNS)."""
    literal = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, literal)
        except (OSError, ValueError):
            continue
        probe = socket.socket(family, socket.SOCK_STREAM)
        try:
            probe.bind((literal, 0))
            return True
        except OSError:
            return False
        finally:
            probe.close()
    return False


def discover_local_names() -> set[str]:
    """This machine's hostname, hostname.local and its current addresses (bracketed for IPv6)."""
    names: set[str] = set()
    try:
        name = socket.gethostname().strip().lower()
    except OSError:
        name = ""
    if name:
        short = name.split(".", 1)[0]
        names.update({name, short, f"{short}.local"})
        try:
            for item in socket.getaddrinfo(name, None):
                names.add(_literal(item[4][0]))
        except OSError:
            pass
    for family, target in ((socket.AF_INET, ("192.0.2.1", 9)), (socket.AF_INET6, ("2001:db8::1", 9))):
        try:
            probe = socket.socket(family, socket.SOCK_DGRAM)
            try:
                probe.connect(target)  # no packet is sent; the kernel just picks the outgoing address
                names.add(_literal(probe.getsockname()[0]))
            finally:
                probe.close()
        except OSError:
            pass
    return {n for n in names if n}


def _literal(address: str) -> str:
    address = address.split("%", 1)[0].lower()
    return f"[{address}]" if ":" in address else address


# -- pairing links and QR codes ------------------------------------------------

def local_ipv4_addresses() -> list[str]:
    """Non-loopback IPv4 addresses this machine currently has (best effort, no packets are sent)."""
    found: list[str] = []
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(item[4][0])
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))
            found.append(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    result: list[str] = []
    for address in found:
        if address and not address.startswith("127.") and not address.startswith("169.254.") and address not in result:
            result.append(address)
    return result


def pairing_urls(code: str, bind_host: str, port: int, *, hostname: str | None = None,
                 addresses: list[str] | None = None) -> dict[str, Any]:
    """URLs that carry ``code`` in the fragment (never sent to the server or logged).

    Loopback listener: only the SSH-tunnel form. Otherwise ``hostname.local`` first, then each IPv4 address
    (just the bound address when the service listens on one specific interface)."""
    bind = (bind_host or "").strip().lower()
    if _is_loopback(bind):
        return {"urls": [f"http://127.0.0.1:{port}/#pair={code}"], "tunnel": True}
    try:
        name = (hostname if hostname is not None else socket.gethostname()).split(".", 1)[0].strip()
    except OSError:
        name = ""
    hosts: list[str] = []
    if _is_wildcard(bind):
        if name:
            hosts.append(f"{name}.local")
        hosts += addresses if addresses is not None else local_ipv4_addresses()
    else:
        hosts.append(bind)
    if not hosts:
        hosts.append("127.0.0.1")
    return {"urls": [f"http://{host}:{port}/#pair={code}" for host in hosts], "tunnel": False}


def load_qrcodegen() -> Any:
    import importlib.util
    path = Path(__file__).with_name("qrcodegen.py")
    spec = importlib.util.spec_from_file_location("vesselstack_qrcodegen", path)
    if not spec or not spec.loader or not path.is_file():
        raise StoreError("the QR code module is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def qr_matrix(text: str) -> list[list[int]]:
    """Module matrix (1 = dark) for ``text``, error correction level L (short URLs stay small and easy to scan)."""
    qr = load_qrcodegen()
    code = qr.QrCode.encode_text(text, qr.QrCode.Ecc.LOW)
    size = code.get_size()
    return [[1 if code.get_module(x, y) else 0 for x in range(size)] for y in range(size)]


def qr_terminal(text: str, *, color: bool = True, border: int = 2) -> str:
    """Half-block rendering: two module rows per text line. ``color`` forces black-on-white so dark terminals scan."""
    matrix = qr_matrix(text)
    size = len(matrix)

    def dark(x: int, y: int) -> bool:
        return 0 <= x < size and 0 <= y < size and bool(matrix[y][x])
    glyphs = {(False, False): " ", (True, False): "\u2580", (False, True): "\u2584", (True, True): "\u2588"}
    lines = []
    for y in range(-border, size + border, 2):
        row = "".join(glyphs[(dark(x, y), dark(x, y + 1))] for x in range(-border, size + border))
        lines.append(f"\x1b[30;47m{row}\x1b[0m" if color else row)
    return "\n".join(lines)
