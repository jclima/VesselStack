#!/usr/bin/env python3
"""Command-line helper behind ``vesselstackctl passphrase`` and ``vesselstackctl devices``.

Run as root (the store is root-only). The passphrase is read from standard
input or a hidden prompt, never from argv.
"""

from __future__ import annotations

import getpass
import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Any, TextIO

SCOPES = ("panel", "chat")


def load_auth() -> Any:
    spec = importlib.util.spec_from_file_location("vesselstack_auth", Path(__file__).with_name("vesselstack_auth.py"))
    if not spec or not spec.loader:
        raise SystemExit("Missing vesselstack_auth.py next to this helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stores_for(auth: Any, root: str | None = None, data: str | None = None) -> dict[str, Any]:
    """Both stores. The chat file lives in the service user's data directory; root-created files take that directory's owner."""
    base = Path(root or os.environ.get("VESSELSTACK_ROOT", "/opt/vesselstack"))
    data_dir = Path(data or os.environ.get("VESSELSTACK_DATA") or base / "data")
    panel_path = base / "config" / auth.STORE_NAME
    chat_path = data_dir / "boat-chat" / auth.CHAT_STORE_NAME

    def other_hash(path: Path):
        def read() -> str | None:
            try:
                return auth.AuthStore(path).snapshot()["passphrase_hash"]
            except (OSError, auth.StoreError):
                return None
        return read
    return {
        "panel": auth.AuthStore(panel_path, differs_from=other_hash(chat_path),
                                differs_message="Choose a passphrase different from the Boat Chat one."),
        "chat": auth.AuthStore(chat_path, min_passphrase=auth.CHAT_MIN_PASSPHRASE_LENGTH, expose_role=True, inherit_owner=True,
                               differs_from=other_hash(panel_path),
                               differs_message="Choose a passphrase different from the Control Panel one."),
    }


def usage() -> str:
    return (
        "Usage:\n"
        "  vesselstackctl passphrase set panel|chat [--stdin]\n"
        "  vesselstackctl passphrase clear panel|chat\n"
        "  vesselstackctl passphrase status [panel|chat]\n"
        "  vesselstackctl devices list [panel|chat]\n"
        "  vesselstackctl devices revoke ID [panel|chat]\n"
        "  vesselstackctl devices revoke-all [panel|chat]\n"
    )


def stamp(value: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value)) if value else "-"


def main(argv: list[str], stdin: TextIO | None = None, out: TextIO | None = None, err: TextIO | None = None) -> int:
    stdin, out, err = stdin or sys.stdin, out or sys.stdout, err or sys.stderr
    auth = load_auth()
    args = list(argv)
    if len(args) < 2:
        err.write(usage())
        return 2
    group, verb, rest = args[0], args[1], args[2:]
    try:
        stores = stores_for(auth)
        if group == "passphrase":
            return passphrase_command(auth, stores, verb, rest, stdin, out, err)
        if group == "devices":
            return devices_command(auth, stores, verb, rest, out, err)
    except auth.AuthError as exc:
        err.write(f"{exc}\n")
        return 1
    except (auth.StoreError, OSError) as exc:
        err.write(f"Sign-in store problem: {exc}\n")
        return 1
    err.write(usage())
    return 2


def scope_ok(rest: list[str], err: TextIO, *, required: bool) -> bool:
    if rest and rest[0] in SCOPES:
        return True
    if not rest and not required:
        return True
    err.write(usage())
    return False


NAMES = {"panel": "Control Panel", "chat": "Boat Chat"}


def passphrase_command(auth: Any, stores: dict[str, Any], verb: str, rest: list[str], stdin: TextIO, out: TextIO, err: TextIO) -> int:
    if verb == "status":
        if not scope_ok(rest[:1], err, required=False) or len(rest) > 1:
            return 2
        for scope in (rest or list(SCOPES)):
            out.write(f"{scope} passphrase: {'set' if stores[scope].passphrase_set() else 'not set'}\n")
            out.write(f"{scope} signed-in devices: {len(stores[scope].list_devices())}\n")
        return 0
    if verb == "clear":
        if len(rest) != 1 or rest[0] not in SCOPES:
            err.write(usage())
            return 2
        revoked = stores[rest[0]].clear_passphrase()
        extra = " The long token still works." if rest[0] == "panel" else " The Access code still works."
        out.write(f"{NAMES[rest[0]]} passphrase sign-in is off; {revoked} signed-in device(s) revoked.{extra}\n")
        return 0
    if verb == "set":
        flags = [item for item in rest if item.startswith("--")]
        positional = [item for item in rest if not item.startswith("--")]
        if len(positional) != 1 or positional[0] not in SCOPES or any(flag != "--stdin" for flag in flags):
            err.write(usage())
            return 2
        scope = positional[0]
        if flags:
            first = stdin.readline().rstrip("\r\n")
        else:
            if not stdin.isatty():
                err.write("Refusing to read a passphrase from a pipe without --stdin.\n")
                return 2
            first = getpass.getpass(f"New {NAMES[scope]} passphrase: ")
            if getpass.getpass("Repeat it: ") != first:
                err.write("The two passphrases differ; nothing was changed.\n")
                return 1
        revoked = stores[scope].set_passphrase(first)
        out.write(f"{NAMES[scope]} passphrase set; {revoked} previously signed-in device(s) revoked.\n")
        return 0
    err.write(usage())
    return 2


def devices_command(auth: Any, stores: dict[str, Any], verb: str, rest: list[str], out: TextIO, err: TextIO) -> int:
    def pick(arguments: list[str]) -> Any:
        return stores[arguments[0] if arguments else "panel"]
    if verb == "list":
        if not scope_ok(rest, err, required=False) or len(rest) > 1:
            return 2
        store = pick(rest)
        devices = store.list_devices()
        if not devices:
            out.write("No signed-in devices.\n")
        for device in devices:
            role = f"  {device['role']}" if "role" in device else ""
            out.write(f"{device['id']}  {device['label']}{role}  created {stamp(device['created'])}  last seen {stamp(device['last_seen'])}  expires {stamp(device['expires'])}\n")
        return 0
    if verb == "revoke":
        if not rest or len(rest) > 2 or not scope_ok(rest[1:], err, required=False):
            err.write(usage())
            return 2
        if pick(rest[1:]).revoke(rest[0]):
            out.write(f"Revoked {rest[0]}.\n")
            return 0
        err.write(f"No signed-in device with id {rest[0]}.\n")
        return 1
    if verb == "revoke-all":
        if not scope_ok(rest, err, required=False) or len(rest) > 1:
            return 2
        out.write(f"Revoked {pick(rest).revoke_all()} device(s).\n")
        return 0
    err.write(usage())
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
