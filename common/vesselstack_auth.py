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
import secrets
import stat
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

SCHEMA = 1
MIN_PASSPHRASE_LENGTH = 12
MAX_PASSPHRASE_LENGTH = 256
MAX_DEVICES = 20
SESSION_SECONDS = 12 * 3600
REMEMBER_SECONDS = 30 * 24 * 3600
LAST_SEEN_INTERVAL = 3600
MAX_LABEL_LENGTH = 60
STORE_NAME = "control-panel-auth.json"

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


class StoreError(Exception):
    """The store could not be read or written safely."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def check_new_passphrase(passphrase: Any) -> str:
    if not isinstance(passphrase, str):
        raise AuthError("A passphrase is required.")
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise AuthError(f"Use at least {MIN_PASSPHRASE_LENGTH} characters; a few random words work well.")
    if len(passphrase) > MAX_PASSPHRASE_LENGTH:
        raise AuthError(f"Use at most {MAX_PASSPHRASE_LENGTH} characters.")
    return passphrase


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


def clean_label(label: Any) -> str:
    text = "".join(ch for ch in str(label or "") if ch.isprintable()).strip()
    return text[:MAX_LABEL_LENGTH] or "Unnamed device"


def _empty() -> dict[str, Any]:
    return {"schema": SCHEMA, "passphrase_hash": None, "devices": []}


def _normalize(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise StoreError("unsupported or malformed sign-in store")
    result = dict(data)
    stored = result.get("passphrase_hash")
    result["passphrase_hash"] = stored if isinstance(stored, str) and stored else None
    devices = []
    for item in result.get("devices") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("token_sha256"), str):
            devices.append({
                "id": item["id"], "token_sha256": item["token_sha256"], "label": clean_label(item.get("label")),
                "created": int(item.get("created") or 0), "last_seen": int(item.get("last_seen") or 0),
                "expires": int(item.get("expires") or 0),
            })
    result["devices"] = devices
    return result


class AuthStore:
    """JSON store: flock-serialised read-modify-write, atomic replace, mtime-aware reads."""

    def __init__(self, path: str | os.PathLike[str], *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self._mutex = threading.RLock()
        self._cache: dict[str, Any] | None = None
        self._signature: tuple[int, int, int] | None = None

    # -- file access -------------------------------------------------------
    def _read_disk(self) -> tuple[dict[str, Any], tuple[int, int, int] | None]:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        except FileNotFoundError:
            return _empty(), None
        except OSError as exc:
            raise StoreError(f"cannot open the sign-in store: {exc.strerror}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise StoreError("the sign-in store is not a regular file")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(1024 * 1024)
        finally:
            os.close(fd)
        try:
            data = _normalize(json.loads(raw.decode("utf-8")))
        except (ValueError, UnicodeError) as exc:
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

    def _write(self, data: dict[str, Any]) -> None:
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        owner = mode = None
        try:
            existing = os.lstat(self.path)
            if not stat.S_ISREG(existing.st_mode):
                raise StoreError("the sign-in store is not a regular file")
            owner, mode = (existing.st_uid, existing.st_gid), stat.S_IMODE(existing.st_mode)
        except FileNotFoundError:
            pass
        fd, temp = tempfile.mkstemp(prefix=".control-panel-auth.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(json.dumps(data, indent=2, sort_keys=True).encode("utf-8") + b"\n")
                handle.flush()
                os.fchmod(handle.fileno(), mode if mode is not None else 0o600)
                if owner is not None and hasattr(os, "fchown"):
                    try:
                        os.fchown(handle.fileno(), *owner)
                    except PermissionError:
                        pass
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def transaction(self, mutate: Callable[[dict[str, Any]], Any]) -> Any:
        """Run ``mutate(data)`` under an exclusive lock on fresh data, then persist."""
        with self._mutex:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_name(self.path.name + ".lock")
            lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                data, _ = self._read_disk()
                result = mutate(data)
                self._write(data)
                self._cache, self._signature = None, None
                return result
            finally:
                os.close(lock_fd)

    # -- passphrase --------------------------------------------------------
    def passphrase_set(self) -> bool:
        return self.snapshot()["passphrase_hash"] is not None

    def check_passphrase(self, passphrase: Any) -> bool:
        stored = self.snapshot()["passphrase_hash"]
        return stored is not None and verify_passphrase(passphrase, stored)

    def set_passphrase(self, passphrase: str, *, keep_device: str | None = None) -> int:
        """Set/replace the passphrase and revoke every device except ``keep_device``. Returns revoked count."""
        encoded = hash_passphrase(check_new_passphrase(passphrase))

        def mutate(data: dict[str, Any]) -> int:
            before = len(data["devices"])
            data["passphrase_hash"] = encoded
            data["devices"] = [d for d in data["devices"] if keep_device and d["id"] == keep_device]
            return before - len(data["devices"])
        return self.transaction(mutate)

    def clear_passphrase(self) -> int:
        def mutate(data: dict[str, Any]) -> int:
            count = len(data["devices"])
            data["passphrase_hash"] = None
            data["devices"] = []
            return count
        return self.transaction(mutate)

    # -- devices -----------------------------------------------------------
    def create_device(self, label: Any, remember: bool) -> tuple[str, dict[str, Any]]:
        token = secrets.token_urlsafe(32)
        digest = token_digest(token)
        now = int(self.clock())
        lifetime = REMEMBER_SECONDS if remember else SESSION_SECONDS

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            data["devices"] = [d for d in data["devices"] if d["expires"] > now]
            if len(data["devices"]) >= MAX_DEVICES:
                raise DeviceLimitError(f"This boat already has {MAX_DEVICES} signed-in devices. Revoke one in Settings, or on the boat computer with: sudo vesselstackctl devices list")
            device = {"id": secrets.token_hex(6), "token_sha256": digest, "label": clean_label(label),
                      "created": now, "last_seen": now, "expires": now + lifetime}
            data["devices"].append(device)
            return device
        device = self.transaction(mutate)
        return token, self.public_device(device)

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
        return self.public_device(found)

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
        return [{**self.public_device(d), "current": d["id"] == current_id}
                for d in self.snapshot()["devices"] if d["expires"] > now]

    def revoke(self, device_id: str) -> bool:
        def mutate(data: dict[str, Any]) -> bool:
            before = len(data["devices"])
            data["devices"] = [d for d in data["devices"] if d["id"] != device_id]
            return len(data["devices"]) != before
        return self.transaction(mutate)

    def revoke_all(self, *, keep: str | None = None) -> int:
        def mutate(data: dict[str, Any]) -> int:
            before = len(data["devices"])
            data["devices"] = [d for d in data["devices"] if keep and d["id"] == keep]
            return before - len(data["devices"])
        return self.transaction(mutate)

    @staticmethod
    def public_device(device: dict[str, Any]) -> dict[str, Any]:
        return {key: device[key] for key in ("id", "label", "created", "last_seen", "expires")}


class LoginThrottle:
    """Per-address exponential backoff, plus a global lockout for shared (tunnel) addresses."""

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
        self._global: deque[float] = deque()
        self._global_until = 0.0

    def retry_after(self, ip: str) -> int:
        """Seconds until a passphrase attempt is allowed again (0 means allowed now)."""
        now = self.clock()
        with self._lock:
            wait = max(self._global_until - now, 0.0)
            entry = self._ips.get(ip)
            if entry:
                wait = max(wait, entry["blocked_until"] - now)
        return int(wait) + 1 if wait > 0 else 0

    def failure(self, ip: str) -> None:
        now = self.clock()
        with self._lock:
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
            self._global.append(now)
            while self._global and now - self._global[0] > self.GLOBAL_WINDOW:
                self._global.popleft()
            if len(self._global) > self.GLOBAL_LIMIT:
                self._global_until = now + self.GLOBAL_LOCKOUT
                self._global.clear()

    def success(self, ip: str) -> None:
        with self._lock:
            self._ips.pop(ip, None)
