"""Shared Boat Chat settings-file location and parsing.

Configuration model (highest precedence first):

1. Settings-page overrides: the file named by ``BOAT_CHAT_ENV`` (default
   ``$BOAT_CHAT_STATE_DIR/boat-chat.env``). It is the only settings file the
   service user can write, and only keys the Settings page knows are read.
2. Base configuration: process environment, populated by systemd from the
   root-managed ``<VESSELSTACK_ROOT>/config/boat-chat.env`` (written by the
   installer and the control panel).

Both Boat Chat and the Telegram bridge use this module so they resolve the same
file and the same precedence.
"""

from __future__ import annotations

import os
import shlex
import tempfile
import fcntl
from pathlib import Path
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parent


def state_dir() -> Path:
    return Path(os.environ.get("BOAT_CHAT_STATE_DIR") or ROOT)


def env_path() -> Path:
    return Path(os.environ.get("BOAT_CHAT_ENV") or state_dir() / "boat-chat.env")


def parse_env_file(path: Path, keys) -> dict[str, str]:
    """Parse KEY=VALUE lines for the allowed keys; unreadable files yield {}."""
    settings: dict[str, str] = {}
    try:
        raw_lines = Path(path).read_text(errors="ignore").splitlines()
    except OSError:
        return settings
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in keys:
            continue
        try:
            parsed = shlex.split(value, comments=False, posix=True)
            settings[key] = " ".join(parsed) if parsed else ""
        except ValueError:
            settings[key] = value.strip().strip("'\"")
    return settings


@contextmanager
def locked(path: Path):
    """Serialize read/modify/write settings transactions across processes."""
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    """Write a complete settings file without exposing a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
