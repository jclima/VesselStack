#!/usr/bin/env python3
"""Command line management for the shared boat profile."""
from __future__ import annotations

import argparse
import json
import stat
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lifecycle_lock import LifecycleLock, LifecycleLockError

try:
    from .boat_profile import (MAX_PROFILE_BYTES, ProfileError, legacy_default,
                               load_profile, normalize_profile, profile_fingerprint,
                               write_profile)
except ImportError:  # direct execution from the repository root
    from boat_profile import (MAX_PROFILE_BYTES, ProfileError, legacy_default,
                              load_profile, normalize_profile, profile_fingerprint,
                              write_profile)


def _source(path: Path) -> dict:
    try:
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode): raise ProfileError("source must be a regular non-symlink file")
        if st.st_size > MAX_PROFILE_BYTES: raise ProfileError("source exceeds 64 KiB")
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ProfileError: raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileError(f"cannot read source: {exc}") from exc
    return normalize_profile(doc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="boat-profile")
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show"); show.add_argument("--root", default="/opt/vesselstack")
    val = sub.add_parser("validate"); val.add_argument("file")
    setp = sub.add_parser("set"); setp.add_argument("file"); setp.add_argument("--root", default="/opt/vesselstack")
    init = sub.add_parser("init"); init.add_argument("--root", default="/opt/vesselstack")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            profile = _source(Path(args.file)); print(json.dumps({"valid": True, "fingerprint": profile_fingerprint(profile)}, sort_keys=True)); return 0
        root = Path(args.root)
        if args.command == "show":
            profile, legacy = load_profile(root)
            print(json.dumps({"profile": profile, "fingerprint": profile_fingerprint(profile), "legacy_default": legacy, "diagnostic": "profile missing; using legacy default" if legacy else None}, sort_keys=True, indent=2)); return 0
        if args.command == "init":
            with LifecycleLock(root, os.environ.get("VESSELSTACK_SYSTEM_ROOT", "")):
                target = root / "config" / "boat-profile.json"
                if target.exists() or target.is_symlink():
                    existing, _ = load_profile(root)
                    if existing == normalize_profile(legacy_default()):
                        print(f"profile already initialized: {target}")
                        return 0
                    raise ProfileError("profile already exists; init never overwrites")
                written = write_profile(root, legacy_default(), backup=False)
            print(f"initialized {written} (next refresh uses the profile)"); return 0
        profile = _source(Path(args.file))
        with LifecycleLock(root, os.environ.get("VESSELSTACK_SYSTEM_ROOT", "")):
            written = write_profile(root, profile)
        print(f"installed {written}; next refresh uses the profile"); return 0
    except (ProfileError, LifecycleLockError) as exc:
        print(f"profile error: {exc}", file=__import__("sys").stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
