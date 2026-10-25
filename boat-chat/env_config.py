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
from pathlib import Path

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
            settings[key] = parsed[0] if parsed else ""
        except ValueError:
            settings[key] = value.strip().strip("'\"")
    return settings
