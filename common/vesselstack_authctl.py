#!/usr/bin/env python3
"""Command-line helper behind ``vesselstackctl passphrase`` and ``vesselstackctl devices``.

Run as root (the store is root-only). The passphrase is read from standard
input or a hidden prompt, never from argv.
"""

from __future__ import annotations

import getpass
import json
import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Any, TextIO

SCOPES = ("panel", "chat")
EXIT_ALREADY_SET = 3  # set-hash --if-unset found a passphrase already stored


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
        "  vesselstackctl passphrase set-hash panel|chat [--if-unset]   (scrypt hash line on stdin; used by the installer;\n"
        "                                       --if-unset stores it only when no passphrase exists, else exits 3)\n"
        "  vesselstackctl passphrase status [panel|chat]\n"
        "  vesselstackctl devices list [panel|chat]\n"
        "  vesselstackctl devices revoke ID [panel|chat]\n"
        "  vesselstackctl devices revoke-all [panel|chat]\n"
        "  vesselstackctl panel-link [--for panel|chat] [--admin] [--label TEXT]\n"
    )


def stamp(value: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value)) if value else "-"


def main(argv: list[str], stdin: TextIO | None = None, out: TextIO | None = None, err: TextIO | None = None) -> int:
    stdin, out, err = stdin or sys.stdin, out or sys.stdout, err or sys.stderr
    auth = load_auth()
    args = list(argv)
    if "--json" in args:
        return json_command(auth, stores_for(auth), [a for a in args if a != "--json"], stdin, out)
    if args and args[0] == "panel-link":
        try:
            return panel_link_command(auth, stores_for(auth), args[1:], out, err)
        except auth.AuthError as exc:
            err.write(f"{exc}\n")
            return 1
        except (auth.StoreError, OSError) as exc:
            err.write(f"Sign-in store problem: {exc}\n")
            return 1
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


def listener(scope: str) -> tuple[str, int]:
    """(bind host, port) of the service, as passed in by vesselstackctl (the env files are the source of truth)."""
    prefix = "VS_PANEL" if scope == "panel" else "VS_CHAT"
    host = os.environ.get(prefix + "_HOST") or ("127.0.0.1" if scope == "panel" else "0.0.0.0")
    try:
        port = int(os.environ.get(prefix + "_PORT") or (8780 if scope == "panel" else 8765))
    except ValueError:
        port = 8780 if scope == "panel" else 8765
    return host, port


def chat_settings_token(auth: Any) -> str:
    """The admin code Boat Chat actually enforces: BOAT_CHAT_SETTINGS_TOKEN from the Settings-page override file when it
    names the key, else from the root-managed boat-chat.env (same precedence as the service); "" when none."""
    base = Path(os.environ.get("VESSELSTACK_ROOT", "/opt/vesselstack"))
    explicit = os.environ.get("BOAT_CHAT_ENV")
    data = os.environ.get("VESSELSTACK_DATA") or str(base / "data")
    override = Path(explicit) if explicit else Path(data) / "boat-chat" / "boat-chat.env"
    return auth.effective_chat_setting("BOAT_CHAT_SETTINGS_TOKEN", override, base / "config" / "boat-chat.env")


def panel_link_command(auth: Any, stores: dict[str, Any], rest: list[str], out: TextIO, err: TextIO) -> int:
    scope, admin, label, index = "panel", False, "", 0
    while index < len(rest):
        item = rest[index]
        if item == "--for" and index + 1 < len(rest):
            scope = rest[index + 1]
            index += 2
        elif item == "--admin":
            admin = True
            index += 1
        elif item == "--label" and index + 1 < len(rest):
            label = rest[index + 1]
            index += 2
        else:
            err.write(usage())
            return 2
    if scope not in SCOPES:
        err.write(usage())
        return 2
    if admin and scope != "chat":
        err.write("--admin only applies to --for chat.\n")
        return 2
    binding = stores["chat"].admin_binding(chat_settings_token(auth), create=True) if admin else ""
    if admin and binding == "none":
        raise auth.AuthError("Admin pairing needs an admin code: set BOAT_CHAT_SETTINGS_TOKEN first.")
    code, expires = stores[scope].create_pairing("admin" if admin else "access", label, binding)
    host, port = listener(scope)
    links = auth.pairing_urls(code, host, port)
    role = "admin" if admin else "access"
    out.write(f"{NAMES[scope]} pairing link ({role}). Valid once, until {stamp(expires)} (10 minutes).\n\n")
    for url in links["urls"]:
        out.write(f"  {url}\n")
    if links["tunnel"]:
        out.write(f"\n{NAMES[scope]} listens on this computer only. From the phone or laptop first open a tunnel:\n"
                  f"  ssh -L {port}:127.0.0.1:{port} USER@BOAT-HOSTNAME\n"
                  + ("or let the boat network reach it with: sudo vesselstackctl panel-access lan\n" if scope == "panel" else
                     "or set BOAT_CHAT_HOST to the boat network address.\n"))
    out.write(f"\nOr type this code on the sign-in screen: {auth.format_pairing_code(code)}\n\n")
    out.write(auth.qr_terminal(links["urls"][0], color=bool(getattr(out, "isatty", lambda: False)())) + "\n\n")
    out.write("Anyone who scans or opens this before it is used or expires gets a signed-in device. Keep it private.\n")
    return 0


def json_command(auth: Any, stores: dict[str, Any], args: list[str], stdin: TextIO, out: TextIO) -> int:
    """Machine interface used when root must change a store owned by a service user: JSON on stdin and stdout.

    Run by root as a separate process that has dropped to the directory owner's uid/gid (see AuthStore._helper_call)."""
    def reply(body: dict[str, Any], code: int = 0) -> int:
        out.write(json.dumps(body) + "\n")
        return code
    try:
        raw = stdin.read(65536).strip()
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        group, verb, scope = (args + ["", "", ""])[:3]
        if scope not in SCOPES:
            return reply({"ok": False, "kind": "store", "message": "unknown store"}, 2)
        store = stores[scope]
        if (group, verb) == ("pairing", "create"):
            code, expires = store.create_pairing(str(payload.get("role") or "access"), payload.get("label") or "",
                                                 str(payload.get("binding") or ""))
            return reply({"ok": True, "code": code, "expires": expires})
        if (group, verb) == ("binding-key", "ensure"):
            return reply({"ok": True, "key": store.ensure_binding_key()})
        if (group, verb) == ("passphrase", "set-hash"):
            extra = {"expect_hash": payload["expect_hash"]} if "expect_hash" in payload else {}
            keep = payload.get("keep_device")
            revoked = store.set_passphrase_hash(payload.get("hash"), keep_device=keep if isinstance(keep, str) else None, **extra)
            return reply({"ok": True, "revoked": revoked})
        if (group, verb) == ("passphrase", "clear"):
            return reply({"ok": True, "revoked": store.clear_passphrase()})
        if (group, verb) == ("devices", "revoke") and isinstance(payload.get("id"), str):
            return reply({"ok": True, "found": store.revoke(payload["id"])})
        if (group, verb) == ("devices", "revoke-all"):
            keep = payload.get("keep")
            return reply({"ok": True, "revoked": store.revoke_all(keep=keep if isinstance(keep, str) else None)})
        return reply({"ok": False, "kind": "store", "message": "unknown helper command"}, 2)
    except auth.PassphraseChanged as exc:
        return reply({"ok": False, "kind": "changed", "message": str(exc)}, 1)
    except auth.DeviceLimitError as exc:
        return reply({"ok": False, "kind": "limit", "message": str(exc)}, 1)
    except auth.AuthError as exc:
        return reply({"ok": False, "kind": "auth", "message": str(exc)}, 1)
    except (auth.StoreError, OSError) as exc:
        return reply({"ok": False, "kind": "store", "message": getattr(exc, "strerror", None) or str(exc)}, 1)
    except ValueError as exc:
        return reply({"ok": False, "kind": "store", "message": str(exc)}, 2)


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
    if verb == "set-hash":
        flags = [item for item in rest if item.startswith("--")]
        positional = [item for item in rest if not item.startswith("--")]
        if len(positional) != 1 or positional[0] not in SCOPES or any(flag != "--if-unset" for flag in flags):
            err.write(usage())
            return 2
        scope, hash_line = positional[0], stdin.readline().strip()
        if flags:
            try:
                revoked = stores[scope].set_passphrase_hash(hash_line, expect_hash=None)
            except auth.PassphraseChanged:
                err.write(f"A {NAMES[scope]} passphrase is already set; left unchanged.\n")
                return EXIT_ALREADY_SET
        else:
            revoked = stores[scope].set_passphrase_hash(hash_line)
        out.write(f"{NAMES[rest[0]]} passphrase set from a prepared hash; {revoked} previously signed-in device(s) revoked.\n")
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
