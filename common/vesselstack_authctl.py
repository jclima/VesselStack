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

SCOPES = ("panel",)


def load_auth() -> Any:
    spec = importlib.util.spec_from_file_location("vesselstack_auth", Path(__file__).with_name("vesselstack_auth.py"))
    if not spec or not spec.loader:
        raise SystemExit("Missing vesselstack_auth.py next to this helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def store_for(auth: Any, root: str | None = None) -> Any:
    base = Path(root or os.environ.get("VESSELSTACK_ROOT", "/opt/vesselstack"))
    return auth.AuthStore(base / "config" / auth.STORE_NAME)


def usage() -> str:
    return (
        "Usage:\n"
        "  vesselstackctl passphrase set panel [--stdin]\n"
        "  vesselstackctl passphrase clear panel\n"
        "  vesselstackctl passphrase status [panel]\n"
        "  vesselstackctl devices list [panel]\n"
        "  vesselstackctl devices revoke ID [panel]\n"
        "  vesselstackctl devices revoke-all [panel]\n"
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
        store = store_for(auth)
        if group == "passphrase":
            return passphrase_command(auth, store, verb, rest, stdin, out, err)
        if group == "devices":
            return devices_command(auth, store, verb, rest, out, err)
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


def passphrase_command(auth: Any, store: Any, verb: str, rest: list[str], stdin: TextIO, out: TextIO, err: TextIO) -> int:
    if verb == "status":
        if not scope_ok(rest[:1], err, required=False) or len(rest) > 1:
            return 2
        out.write(f"panel passphrase: {'set' if store.passphrase_set() else 'not set'}\n")
        out.write(f"panel signed-in devices: {len(store.list_devices())}\n")
        return 0
    if verb == "clear":
        if rest != ["panel"]:
            err.write(usage())
            return 2
        revoked = store.clear_passphrase()
        out.write(f"Panel passphrase sign-in is off; {revoked} signed-in device(s) revoked. The long token still works.\n")
        return 0
    if verb == "set":
        flags = [item for item in rest if item.startswith("--")]
        positional = [item for item in rest if not item.startswith("--")]
        if positional != ["panel"] or any(flag != "--stdin" for flag in flags):
            err.write(usage())
            return 2
        if flags:
            first = stdin.readline().rstrip("\r\n")
        else:
            if not stdin.isatty():
                err.write("Refusing to read a passphrase from a pipe without --stdin.\n")
                return 2
            first = getpass.getpass("New Control Panel passphrase: ")
            if getpass.getpass("Repeat it: ") != first:
                err.write("The two passphrases differ; nothing was changed.\n")
                return 1
        revoked = store.set_passphrase(first)
        out.write(f"Panel passphrase set; {revoked} previously signed-in device(s) revoked.\n")
        return 0
    err.write(usage())
    return 2


def devices_command(auth: Any, store: Any, verb: str, rest: list[str], out: TextIO, err: TextIO) -> int:
    if verb == "list":
        if not scope_ok(rest, err, required=False) or len(rest) > 1:
            return 2
        devices = store.list_devices()
        if not devices:
            out.write("No signed-in devices.\n")
        for device in devices:
            out.write(f"{device['id']}  {device['label']}  created {stamp(device['created'])}  last seen {stamp(device['last_seen'])}  expires {stamp(device['expires'])}\n")
        return 0
    if verb == "revoke":
        if not rest or len(rest) > 2 or not scope_ok(rest[1:], err, required=False):
            err.write(usage())
            return 2
        if store.revoke(rest[0]):
            out.write(f"Revoked {rest[0]}.\n")
            return 0
        err.write(f"No signed-in device with id {rest[0]}.\n")
        return 1
    if verb == "revoke-all":
        if not scope_ok(rest, err, required=False) or len(rest) > 1:
            return 2
        out.write(f"Revoked {store.revoke_all()} device(s).\n")
        return 0
    err.write(usage())
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
