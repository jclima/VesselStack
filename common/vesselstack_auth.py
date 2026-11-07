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
import socket
import stat
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
                "role": item.get("role") if item.get("role") in ROLES else "access",
            })
    result["devices"] = devices
    return result


class AuthStore:
    """JSON store: flock-serialised read-modify-write, atomic replace, mtime-aware reads."""

    def __init__(self, path: str | os.PathLike[str], *, clock: Callable[[], float] = time.time,
                 min_passphrase: int = MIN_PASSPHRASE_LENGTH, expose_role: bool = False,
                 inherit_owner: bool = False, differs_from: Callable[[], Any] | None = None,
                 differs_message: str = "Choose a passphrase different from the other VesselStack passphrase.") -> None:
        """``inherit_owner``: the store lives in a service user's directory; when root uses it, transactions run in a
        child that has dropped to that directory's owner (root never opens or chowns files there).
        ``differs_from``: returns a stored hash (or None) the new passphrase must not equal."""
        self.path = Path(path)
        self.clock = clock
        self.min_passphrase = min_passphrase
        self.expose_role = expose_role
        self.inherit_owner = inherit_owner
        self.differs_from = differs_from
        self.differs_message = differs_message
        self._mutex = threading.RLock()
        self._cache: dict[str, Any] | None = None
        self._signature: tuple[int, int, int] | None = None

    # -- file access -------------------------------------------------------
    # Every file operation is relative to a directory descriptor opened with O_NOFOLLOW, and every file
    # opened is checked (regular, single link) before use. Nothing here ever chowns: when root must touch a
    # directory owned by a service user, the whole transaction runs in a child that has dropped privileges.
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

    def _drop_privileges(self, uid: int, gid: int) -> None:
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
        if os.getuid() != uid or os.geteuid() != uid or os.getgid() != gid:
            raise StoreError("could not drop privileges")

    def _foreign_owner(self) -> tuple[int, int] | None:
        """(uid, gid) of the directory owner when root would otherwise be working in another user's directory."""
        if not self.inherit_owner or self._euid() != 0:
            return None
        try:
            uid, gid = self._parent_owner()
        except FileNotFoundError:
            return None
        return (uid, gid) if uid != 0 else None

    def _transaction_as(self, owner: tuple[int, int], mutate: Callable[[dict[str, Any]], Any]) -> Any:
        """Run the transaction in a forked child that has become the directory owner; root never opens a file there."""
        reader, writer = os.pipe()
        pid = os.fork()
        if pid == 0:  # child: never returns into the caller's stack
            code = 1
            try:
                os.close(reader)
                try:
                    self._drop_privileges(*owner)
                    reply: dict[str, Any] = {"ok": True, "result": self._transaction_local(mutate)}
                except DeviceLimitError as exc:
                    reply = {"ok": False, "kind": "limit", "message": str(exc)}
                except AuthError as exc:
                    reply = {"ok": False, "kind": "auth", "message": str(exc)}
                except StoreError as exc:
                    reply = {"ok": False, "kind": "store", "message": str(exc)}
                except OSError as exc:
                    reply = {"ok": False, "kind": "store", "message": exc.strerror or "I/O error"}
                except BaseException:  # noqa: BLE001 - report generically, never leak details
                    reply = {"ok": False, "kind": "store", "message": "sign-in store transaction failed"}
                payload = json.dumps(reply).encode("utf-8")
                view = memoryview(payload)
                while view:
                    view = view[os.write(writer, view):]
                code = 0
            finally:
                os._exit(code)
        os.close(writer)
        try:
            with os.fdopen(reader, "rb") as handle:
                raw = handle.read(1024 * 1024)
        finally:
            _, status = os.waitpid(pid, 0)
        try:
            reply = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise StoreError("the sign-in store helper failed") from exc
        if not reply.get("ok"):
            kind = {"limit": DeviceLimitError, "auth": AuthError}.get(reply.get("kind"), StoreError)
            raise kind(str(reply.get("message") or "sign-in store problem"))
        return reply.get("result")

    def transaction(self, mutate: Callable[[dict[str, Any]], Any]) -> Any:
        """Run ``mutate(data)`` under an exclusive lock on fresh data, then persist.

        ``mutate`` must return JSON-serialisable data (it may run in a privilege-dropped child process)."""
        with self._mutex:
            try:
                owner = self._foreign_owner()
                if owner is not None:
                    return self._transaction_as(owner, mutate)
                return self._transaction_local(mutate)
            finally:
                self._cache, self._signature = None, None

    # -- passphrase --------------------------------------------------------
    def passphrase_set(self) -> bool:
        return self.snapshot()["passphrase_hash"] is not None

    def check_passphrase(self, passphrase: Any) -> bool:
        stored = self.snapshot()["passphrase_hash"]
        return stored is not None and verify_passphrase(passphrase, stored)

    def set_passphrase(self, passphrase: str, *, keep_device: str | None = None) -> int:
        """Set/replace the passphrase and revoke every device except ``keep_device``. Returns revoked count."""
        checked = check_new_passphrase(passphrase, self.min_passphrase)
        other = self.differs_from() if self.differs_from else None
        if other and verify_passphrase(checked, other):
            raise AuthError(self.differs_message)
        encoded = hash_passphrase(checked)

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
    def create_device(self, label: Any, remember: bool, role: str = "access") -> tuple[str, dict[str, Any]]:
        token = secrets.token_urlsafe(32)
        digest = token_digest(token)
        now = int(self.clock())
        lifetime = REMEMBER_SECONDS if remember else SESSION_SECONDS

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            data["devices"] = [d for d in data["devices"] if d["expires"] > now]
            if len(data["devices"]) >= MAX_DEVICES:
                raise DeviceLimitError(f"This boat already has {MAX_DEVICES} signed-in devices. Revoke one in Settings, or on the boat computer with: sudo vesselstackctl devices list")
            device = {"id": secrets.token_hex(6), "token_sha256": digest, "label": clean_label(label),
                      "created": now, "last_seen": now, "expires": now + lifetime,
                      "role": role if role in ROLES else "access"}
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
        return self.public_device(found, role=True)

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
        return self.transaction(mutate)

    def revoke_all(self, *, keep: str | None = None) -> int:
        def mutate(data: dict[str, Any]) -> int:
            before = len(data["devices"])
            data["devices"] = [d for d in data["devices"] if keep and d["id"] == keep]
            return before - len(data["devices"])
        return self.transaction(mutate)

    def public_device(self, device: dict[str, Any], role: bool | None = None) -> dict[str, Any]:
        keys = ["id", "label", "created", "last_seen", "expires"]
        if self.expose_role if role is None else role:
            keys.append("role")
        return {key: device[key] for key in keys}


class Attempt:
    """Handle for one attempt admitted by ``LoginThrottle.begin()``."""

    __slots__ = ("ip", "stamp_id")

    def __init__(self, ip: str, stamp_id: int) -> None:
        self.ip = ip
        self.stamp_id = stamp_id


class LoginThrottle:
    """Per-address exponential backoff, plus a global lockout for shared (tunnel) addresses.

    ``begin()`` is the atomic entry point: under one lock it checks both limits and, when the attempt
    is allowed, records it as a failure up front ("pending") and returns an ``Attempt`` handle.
    Concurrent requests therefore cannot all pass the check before any failure is counted.
    ``success(handle)`` withdraws only that one attempt from the global window and clears the address's
    backoff; every other recorded failure (the caller's or anyone else's) stays counted.
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
            stamp_id = self._record_locked(ip, now)
            self._pending.add(stamp_id)
            if len(self._pending) > 4 * self.MAX_TRACKED:
                live = {entry[0] for entry in self._global}
                self._pending &= live
        return True, 0, Attempt(ip, stamp_id)

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
        """A correct credential: withdraw only this attempt from the global window and clear the address backoff."""
        if not isinstance(attempt, Attempt):
            raise TypeError("success() needs the handle returned by begin()")
        with self._lock:
            self._ips.pop(attempt.ip, None)
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
