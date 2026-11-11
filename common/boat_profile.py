"""Validated, portable boat equipment profiles.

This module deliberately has no VesselStack runtime dependencies.  Consumers can
use :func:`load_profile` and the capability helpers without importing a service
or reading environment variables.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import shlex
import tempfile
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1
PROFILE_RELATIVE_PATH = Path("config/boat-profile.json")
MAX_PROFILE_BYTES = 64 * 1024
MAX_ENGINES = 8
MAX_BATTERY_BANKS = 16
MAX_FUEL_TANKS = 16
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,63}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9][A-Za-z0-9_-]*)*$")


class ProfileError(ValueError):
    """A profile is unreadable or violates the public schema."""


def legacy_default() -> dict[str, Any]:
    """Return the historical two-engine, one-house-bank profile."""
    return {
        "schema_version": 1,
        "engines": [
            {"id": "port", "label": "Port", "path": "propulsion.port", "kind": "combustion", "fuel_rate_supported": True},
            {"id": "starboard", "label": "Starboard", "path": "propulsion.starboard", "kind": "combustion", "fuel_rate_supported": True},
        ],
        "battery_banks": [{"id": "shunt", "label": "Shunt", "path": "electrical.batteries.shunt", "role": "house"}],
        "fuel_tanks": [
            {"id": "0", "label": "Fuel 0", "path": "tanks.fuel.0"},
            {"id": "1", "label": "Fuel 1", "path": "tanks.fuel.1"},
        ],
        "primary_battery_bank": "shunt",
    }


def _fail(message: str) -> None:
    raise ProfileError(message)


def _text(value: Any, field: str, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or not pattern.fullmatch(value):
        _fail(f"{field} must match {pattern.pattern}")
    return value


def _list(value: Any, field: str, limit: int) -> list[Any]:
    if type(value) is not list:
        _fail(f"{field} must be a list")
    if len(value) > limit:
        _fail(f"{field} may contain at most {limit} entries")
    return value


def _entry(value: Any, field: str, fields: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        _fail(f"{field} must contain exactly: {', '.join(sorted(fields))}")
    return value


def normalize_profile(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a canonical copy of a profile document."""
    if type(document) is not dict:
        _fail("profile root must be an object")
    required = {"schema_version", "engines", "battery_banks", "fuel_tanks", "primary_battery_bank"}
    if set(document) != required:
        _fail("profile must contain exactly: " + ", ".join(sorted(required)))
    if type(document["schema_version"]) is not int or document["schema_version"] != SCHEMA_VERSION:
        _fail("schema_version must be 1")
    engines = _list(document["engines"], "engines", MAX_ENGINES)
    banks = _list(document["battery_banks"], "battery_banks", MAX_BATTERY_BANKS)
    tanks = _list(document["fuel_tanks"], "fuel_tanks", MAX_FUEL_TANKS)
    result: dict[str, Any] = {"schema_version": 1, "engines": [], "battery_banks": [], "fuel_tanks": [], "primary_battery_bank": document["primary_battery_bank"]}
    seen_ids: set[str] = set(); seen_paths: set[str] = set()
    def add(items: list[Any], field: str, fields: set[str]) -> list[dict[str, Any]]:
        out = []
        for i, raw in enumerate(items):
            if field == "engines" and type(raw) is dict and set(raw) == fields - {"fuel_rate_supported"}:
                raw = dict(raw)
                raw["fuel_rate_supported"] = None
            row = _entry(raw, f"{field}[{i}]", fields)
            ident = _text(row["id"], f"{field}[{i}].id", _ID)
            label = _text(row["label"], f"{field}[{i}].label", _NAME)
            path = _text(row["path"], f"{field}[{i}].path", _PATH)
            expected_prefix = {"engines": "propulsion.", "battery_banks": "electrical.batteries.", "fuel_tanks": "tanks.fuel."}[field]
            if not path.startswith(expected_prefix):
                _fail(f"{field}[{i}].path must start with {expected_prefix}")
            if ident in seen_ids: _fail(f"duplicate id: {ident}")
            if path in seen_paths: _fail(f"duplicate path: {path}")
            seen_ids.add(ident); seen_paths.add(path)
            item = {"id": ident, "label": label, "path": path}
            if field == "engines":
                kind = row["kind"]
                if kind not in ("combustion", "electric"): _fail(f"{field}[{i}].kind is invalid")
                if row["fuel_rate_supported"] is None:
                    row["fuel_rate_supported"] = kind == "combustion"
                elif type(row["fuel_rate_supported"]) is not bool: _fail(f"{field}[{i}].fuel_rate_supported must be boolean")
                if kind == "electric" and row["fuel_rate_supported"]: _fail("electric engines cannot support fuel rate")
                item.update(kind=kind, fuel_rate_supported=row["fuel_rate_supported"])
            elif field == "battery_banks":
                if row["role"] not in ("house", "starter", "other"): _fail(f"{field}[{i}].role is invalid")
                item["role"] = row["role"]
            out.append(item)
        return out
    result["engines"] = add(engines, "engines", {"id", "label", "path", "kind", "fuel_rate_supported"})
    result["battery_banks"] = add(banks, "battery_banks", {"id", "label", "path", "role"})
    result["fuel_tanks"] = add(tanks, "fuel_tanks", {"id", "label", "path"})
    primary = document["primary_battery_bank"]
    if primary is not None and (type(primary) is not str or primary not in {x["id"] for x in result["battery_banks"]}):
        _fail("primary_battery_bank must be null or an existing battery bank id")
    if primary is not None and not any(x["id"] == primary and x["role"] == "house" for x in result["battery_banks"]):
        _fail("primary_battery_bank must reference a battery bank with role house")
    return result


def validate_profile(document: Mapping[str, Any]) -> None:
    """Raise :class:`ProfileError` unless *document* is valid."""
    normalize_profile(document)


def canonical_json(document: Mapping[str, Any]) -> str:
    return json.dumps(normalize_profile(document), sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"


def profile_fingerprint(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def _read_regular(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        _fail(f"cannot read profile: {exc}")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_PROFILE_BYTES:
            _fail("profile must be a regular non-symlink file under 64 KiB")
        data = os.read(fd, MAX_PROFILE_BYTES + 1)
        if len(data) > MAX_PROFILE_BYTES:
            _fail("profile exceeds 64 KiB")
        return data
    except OSError as exc:
        _fail(f"cannot read profile: {exc}")
    finally:
        os.close(fd)


def load_profile(root: os.PathLike[str] | str = "/opt/vesselstack") -> tuple[dict[str, Any], bool]:
    """Load ``config/boat-profile.json``; return ``(profile, used_legacy)``."""
    path = Path(root) / PROFILE_RELATIVE_PATH
    try:
        path.lstat()
    except FileNotFoundError:
        return legacy_default(), True
    except OSError as exc:
        _fail(f"cannot inspect profile: {exc}")
    data = _read_regular(path)
    try: document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: _fail(f"malformed profile JSON: {exc}")
    return normalize_profile(document), False


def engine_capabilities(profile: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["id"]: {"path": e["path"], "kind": e["kind"], "fuel_rate_supported": e["fuel_rate_supported"]} for e in normalize_profile(profile)["engines"]}


def battery_capabilities(profile: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {b["id"]: {"path": b["path"], "role": b["role"]} for b in normalize_profile(profile)["battery_banks"]}


def fuel_tank_capabilities(profile: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["id"]: {"path": t["path"]} for t in normalize_profile(profile)["fuel_tanks"]}


def write_profile(root: os.PathLike[str] | str, document: Mapping[str, Any], *, backup: bool = True) -> Path:
    """Atomically install a validated profile, retaining the prior file."""
    normalized = normalize_profile(document)
    root_path = Path(root); config = root_path / "config"; target = config / "boat-profile.json"
    for directory in (root_path, config):
        try:
            if stat.S_ISLNK(directory.lstat().st_mode):
                _fail("profile directories must not be symlinks")
        except FileNotFoundError:
            pass
    config.mkdir(mode=0o750, parents=True, exist_ok=True)
    if target.exists() and backup:
        backups = config / "boat-profile-backups"; backups.mkdir(mode=0o750, exist_ok=True)
        if stat.S_ISLNK(backups.lstat().st_mode):
            _fail("profile backup directory must not be a symlink")
        stamp = hashlib.sha256(_read_regular(target)).hexdigest()[:16]
        backup_target = backups / f"boat-profile-{stamp}.json"
        if backup_target.exists() or backup_target.is_symlink():
            if backup_target.is_symlink(): _fail("profile backup target must not be a symlink")
        else:
            backup_fd, backup_tmp_name = tempfile.mkstemp(prefix=".boat-profile-backup.", dir=str(backups))
            try:
                os.fchmod(backup_fd, 0o640)
                if os.geteuid() == 0:
                    gid = _configured_gid(root_path)
                    if gid is not None: os.fchown(backup_fd, 0, gid)
                with os.fdopen(backup_fd, "wb") as backup_handle:
                    backup_handle.write(_read_regular(target)); backup_handle.flush(); os.fsync(backup_handle.fileno())
                os.replace(backup_tmp_name, backup_target)
            finally:
                if os.path.exists(backup_tmp_name): os.unlink(backup_tmp_name)
    fd, tmp_name = tempfile.mkstemp(prefix=".boat-profile.", dir=str(config), text=True)
    try:
        os.fchmod(fd, 0o640)
        if os.geteuid() == 0:
            gid = _configured_gid(root_path)
            if gid is not None:
                os.fchown(fd, 0, gid)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(normalized)); handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp_name, target)
        os.chmod(target, 0o640)
    finally:
        if os.path.exists(tmp_name): os.unlink(tmp_name)
    return target


def _configured_gid(root: Path) -> int | None:
    env_path = root / "config" / "vesselstack.env"
    if not env_path.exists():
        return None
    for line in _read_regular(env_path).decode("utf-8", "strict").splitlines():
        if line.startswith("VESSELSTACK_GID="):
            try:
                tokens = shlex.split(line.split("=", 1)[1].strip(), posix=True)
            except ValueError as exc:
                _fail(f"invalid VESSELSTACK_GID quoting: {exc}")
            if len(tokens) != 1:
                _fail("VESSELSTACK_GID must be numeric")
            value = tokens[0]
            if not re.fullmatch(r"[0-9]+", value):
                _fail("VESSELSTACK_GID must be numeric")
            gid = int(value)
            if gid < 0 or gid > 65535:
                _fail("VESSELSTACK_GID is out of range")
            return gid
    return None
