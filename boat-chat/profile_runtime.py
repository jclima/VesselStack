"""Runtime adapter for the shared boat equipment profile.

The profile is deliberately loaded at request/indexer boundaries.  Callers may
keep the returned immutable-ish mapping for the duration of a calculation, but
must not cache it process-wide: an owner edit takes effect on the next request.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
from pathlib import Path
from typing import Any, Iterator, Mapping

import sys
COMMON = Path(__file__).resolve().parents[1] / "common"
if str(COMMON) not in sys.path:
    sys.path.insert(0, str(COMMON))
from boat_profile import ProfileError, legacy_default, load_profile, profile_fingerprint  # noqa: E402

_CURRENT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("boat_profile", default=None)


def install_root() -> Path:
    override = os.environ.get("VESSELSTACK_ROOT") or os.environ.get("BOAT_CHAT_ROOT")
    return Path(override).expanduser() if override else Path(__file__).resolve().parents[1]


def read(root: os.PathLike[str] | str | None = None) -> tuple[dict[str, Any], bool, str | None]:
    """Return ``(profile, legacy, diagnostic)``; malformed profiles never raise."""
    try:
        profile, legacy = load_profile(root or install_root())
        return profile, legacy, None
    except (ProfileError, OSError, ValueError) as exc:
        # An invalid explicit file is not the same as a missing file.  Keep the
        # runtime available for chat/settings, but expose no equipment so
        # analytics cannot silently query the historical topology.
        return {"schema_version": 1, "engines": [], "battery_banks": [], "fuel_tanks": [], "primary_battery_bank": None}, False, f"Boat profile unavailable: {exc}"


def snapshot(root: os.PathLike[str] | str | None = None) -> dict[str, Any]:
    profile, legacy, diagnostic = read(root)
    return {"profile": profile, "legacy": legacy, "fingerprint": profile_fingerprint(profile), "diagnostic": diagnostic}


def current(root: os.PathLike[str] | str | None = None) -> dict[str, Any]:
    value = _CURRENT.get()
    return value if value is not None else snapshot(root)


@contextlib.contextmanager
def scope(root: os.PathLike[str] | str | None = None, runtime: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
    """Install one immutable request snapshot for all nested runtime reads."""
    active = _CURRENT.get()
    token = _CURRENT.set(runtime if runtime is not None else (active if active is not None and root is None else snapshot(root)))
    try:
        yield _CURRENT.get()  # type: ignore[misc]
    finally:
        _CURRENT.reset(token)


def engines(profile: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    return list((profile or current()["profile"]).get("engines", []))


def banks(profile: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    return list((profile or current()["profile"]).get("battery_banks", []))


def tanks(profile: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    return list((profile or current()["profile"]).get("fuel_tanks", []))


def paths(profile: Mapping[str, Any] | None = None) -> list[str]:
    p = profile or current()["profile"]
    return [x["path"] for x in engines(p) + banks(p) + tanks(p)]


def unsupported(profile: Mapping[str, Any], operation: str) -> dict[str, Any]:
    return {"available": False, "unsupported": True, "operation": operation,
            "diagnostic": f"{operation} is unsupported for this equipment profile; no legacy paired assumption was applied."}


def legacy_pair(profile: Mapping[str, Any] | None = None) -> bool:
    candidate = dict(profile or current()["profile"])
    # The paired comparison is a compatibility operation, not a generic
    # two-engine feature. Require the complete historical topology and
    # capabilities so aliases or partial profiles cannot enter legacy queries;
    # display labels and declaration order are presentation-only.
    expected = legacy_default()
    engine_keys = lambda item: (item.get("id"), item.get("path"), item.get("kind"), item.get("fuel_rate_supported"))
    bank_keys = lambda item: (item.get("id"), item.get("path"), item.get("role"))
    tank_keys = lambda item: (item.get("id"), item.get("path"))
    return (
        {engine_keys(item) for item in candidate.get("engines", [])} == {engine_keys(item) for item in expected["engines"]}
        and {bank_keys(item) for item in candidate.get("battery_banks", [])} == {bank_keys(item) for item in expected["battery_banks"]}
        and {tank_keys(item) for item in candidate.get("fuel_tanks", [])} == {tank_keys(item) for item in expected["fuel_tanks"]}
        and candidate.get("primary_battery_bank") == expected.get("primary_battery_bank")
    )
