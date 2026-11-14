#!/usr/bin/env python3
"""VesselStack displays control tool (preview: validation, MQTT account sync, device lifecycle, firmware config)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import editor  # noqa: E402
import firmware  # noqa: E402
import lifecycle  # noqa: E402
import mqtt_auth  # noqa: E402
import render  # noqa: E402
import schema  # noqa: E402

DEFAULT_CONFIG = schema.MODULE_DIR / "displays.example.json"
DEFAULT_HA_URL = "http://127.0.0.1:8123"


def cmd_validate(args) -> int:
    try:
        registry = schema.load_registry()
        registry_errors = schema.validate_registry(registry)
        if registry_errors:
            for err in registry_errors:
                print(f"registry: {err}", file=sys.stderr)
            return 1
        config = schema.load_json(args.file)
    except (OSError, ValueError) as exc:  # includes json.JSONDecodeError
        print(f"error: {exc}", file=sys.stderr)
        return 2
    errors = schema.validate_config(config, registry)
    if not errors:
        errors = render.check_glyphs(config)
    if errors:
        for err in errors:
            print(f"{args.file}: {err}", file=sys.stderr)
        print(f"{len(errors)} error(s); nothing written", file=sys.stderr)
        return 1
    for warning in schema.config_warnings(config):
        print(f"{args.file}: warning: {warning}", file=sys.stderr)
    notes = config.get("notifications")
    extra = f"{len(notes)} notification(s), " if notes else ""
    print(f"OK: {args.file}: {len(config['instruments'])} instrument(s), {extra}"
          f"{len(config['displays'])} display(s)")
    return 0


def cmd_sync_mqtt(args) -> int:
    """Render the Mosquitto ACL and hashed passwords from the environment."""
    root_text = args.root or os.environ.get("VESSELSTACK_ROOT", "")
    data_text = args.data or os.environ.get("VESSELSTACK_DATA", "")
    if not root_text or not data_text:
        print("error: VESSELSTACK_ROOT and VESSELSTACK_DATA (or --root/--data) are required",
              file=sys.stderr)
        return 2
    env = {key: os.environ[key] for key in
           ("MQTT_USERNAME", "MQTT_PASSWORD", "DISPLAYS_ENABLE",
            "DISPLAYS_BRIDGE_MQTT_USERNAME", "NODERED_ENABLE") if key in os.environ}
    try:
        # The same lock as the lifecycle commands: a Control Panel Apply cannot
        # interleave with a concurrent "displayctl add".
        with mqtt_auth.config_lock(Path(root_text)):
            summary = mqtt_auth.sync(Path(root_text), Path(data_text), env,
                                     displays_config=Path(args.config) if args.config else None,
                                     image=args.image, container=args.container,
                                     compose=Path(args.compose) if args.compose else None)
    except (mqtt_auth.MqttAuthError, OSError) as exc:
        print(f"error: sync-mqtt failed: {exc}", file=sys.stderr)
        return 1
    print(f"MQTT access control synced: {summary['accounts']} account(s), "
          f"{summary['displays']} display(s), displays "
          f"{'enabled' if summary['enabled'] else 'disabled'}, "
          f"broker {'reloaded' if summary['reloaded'] else 'not running'}")
    return 0


def cmd_preflight(args) -> int:
    """Read-only checks the installer runs before it changes anything."""
    root_text = args.root or os.environ.get("VESSELSTACK_ROOT", "")
    if not root_text:
        print("error: VESSELSTACK_ROOT (or --root) is required", file=sys.stderr)
        return 2
    env = {key: os.environ[key] for key in
           ("MQTT_USERNAME", "MQTT_PASSWORD", "DISPLAYS_ENABLE", "DISPLAYS_BRIDGE_MQTT_USERNAME",
            "DISPLAYS_HA_TOKEN", "HOME_ASSISTANT_URL", "NODERED_ENABLE") if key in os.environ}
    mode = "pull" if args.pull_image else "check" if args.check_image else "none"
    try:
        warnings = mqtt_auth.preflight(
            Path(root_text), env, compose=Path(args.compose) if args.compose else None,
            displays_config=Path(args.config) if args.config else None,
            image=args.image, image_mode=mode)
    except (mqtt_auth.MqttAuthError, OSError) as exc:
        print(f"error: MQTT access preflight failed: {exc}", file=sys.stderr)
        return 1
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print("MQTT access preflight passed")
    return 0


def systemd_env_line(key: str, value: str) -> str:
    """One systemd EnvironmentFile line: KEY="value" with backslash and quote escaped.

    systemd does not expand $ or % in EnvironmentFile values, so only the
    backslash and double quote need escaping; control characters are refused.
    """
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError(f"{key} must not contain control characters or line breaks")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"\n'


def render_bridge_env(root: Path, env: dict, bridge_password: str) -> str:
    username = env.get("DISPLAYS_BRIDGE_MQTT_USERNAME") or mqtt_auth.DEFAULT_BRIDGE_USERNAME
    values = [
        ("DISPLAYS_CONFIG", str(root / "config" / "displays.json")),
        ("SIGNALK_URL", env.get("SIGNALK_URL", "")),
        ("MQTT_HOST", "127.0.0.1"),
        ("MQTT_PORT", env.get("MQTT_PORT") or "1883"),
        ("DISPLAYS_BRIDGE_MQTT_USERNAME", username),
        ("DISPLAYS_BRIDGE_MQTT_PASSWORD", bridge_password),
        ("DISPLAYS_SIGNALK_TOKEN", env.get("DISPLAYS_SIGNALK_TOKEN", "")),
    ]
    if env.get("DISPLAYS_HA_TOKEN"):   # Home Assistant items are an opt-in extra: nothing is written without a token
        values.append(("HOME_ASSISTANT_URL", env.get("HOME_ASSISTANT_URL") or DEFAULT_HA_URL))
        values.append(("DISPLAYS_HA_TOKEN", env["DISPLAYS_HA_TOKEN"]))
    if env.get("BOAT_TIMEZONE"):  # the vessel's IANA zone: the bridge clock and night schedule follow it
        values.append(("BOAT_TIMEZONE", env["BOAT_TIMEZONE"]))
    return "".join(systemd_env_line(key, value) for key, value in values)


def cmd_bridge_env(args) -> int:
    """Write the bridge's systemd EnvironmentFile (root:root 0600) without secrets on argv.

    systemd (PID 1, root) reads EnvironmentFile= before it drops to the service
    user, so the service user never needs read access to this secret-bearing file.
    """
    root_text = args.root or os.environ.get("VESSELSTACK_ROOT", "")
    if not root_text:
        print("error: VESSELSTACK_ROOT (or --root) is required", file=sys.stderr)
        return 2
    root = Path(root_text)
    env = {key: os.environ[key] for key in
           ("SIGNALK_URL", "MQTT_PORT", "DISPLAYS_BRIDGE_MQTT_USERNAME", "DISPLAYS_SIGNALK_TOKEN",
            "DISPLAYS_HA_TOKEN", "HOME_ASSISTANT_URL", "BOAT_TIMEZONE") if key in os.environ}
    try:
        output = Path(args.output)
        with mqtt_auth.config_lock(root):
            secrets_data = mqtt_auth.load_secrets(root / "config" / mqtt_auth.SECRETS_NAME)
            password = secrets_data["bridge"].get("mqtt_password")
            if not isinstance(password, str) or not password:
                raise ValueError(
                    "no bridge MQTT password yet; run sync-mqtt with DISPLAYS_ENABLE=true first")
            text = render_bridge_env(root, env, password)
            mqtt_auth.write_atomic(output, text, 0o600)
            owner = mqtt_auth._owner(mqtt_auth.CONFIG_OWNER)
            if owner:
                os.chown(output, *owner)
    except (mqtt_auth.MqttAuthError, OSError, ValueError, KeyError) as exc:
        print(f"error: bridge-env failed: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {args.output} (mode 0600)")
    return 0


# SIGNALK_URL, DISPLAYS_SIGNALK_TOKEN and BOAT_UNITS feed 'catalog'; the token is only ever sent to
# SIGNALK_URL and never printed. VESSELSTACK_DATA_BIND_ADDRESS feeds the 'show' readiness block.
LIFECYCLE_ENV_KEYS = ("MQTT_USERNAME", "MQTT_PASSWORD", "DISPLAYS_ENABLE",
                      "DISPLAYS_BRIDGE_MQTT_USERNAME", "SIGNALK_URL", "DISPLAYS_SIGNALK_TOKEN", "BOAT_UNITS",
                      "VESSELSTACK_DATA_BIND_ADDRESS", "NODERED_ENABLE")


def run_lifecycle(args, action) -> int:
    """Run a lifecycle action with a Context built from the environment."""
    root_text = args.root or os.environ.get("VESSELSTACK_ROOT", "")
    if not root_text:
        print("error: VESSELSTACK_ROOT (or --root) is required", file=sys.stderr)
        return 2
    ctx = lifecycle.Context(
        root=Path(root_text),
        env={key: os.environ[key] for key in LIFECYCLE_ENV_KEYS if key in os.environ},
        require_root=not getattr(args, "allow_non_root", False),
        container=getattr(args, "container", None) or mqtt_auth.CONTAINER,
        image=getattr(args, "image", None))
    try:
        result = action(ctx)
    except lifecycle.LifecycleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except (mqtt_auth.MqttAuthError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return result if isinstance(result, int) else 0


def run_json(args, action, text_kind=None) -> int:
    """Run an editor action that returns a dict; print it as JSON (or text without --json).

    A failure prints ``{"ok": false, "code": N, "errors": [...]}`` on stdout and the first error on
    stderr. Exit codes: 0 done, 1 failed (I/O, lock, environment), 2 invalid input or configuration,
    3 the revision no longer matches, 4 a build or flash for the display is running.
    """
    root_text = args.root or os.environ.get("VESSELSTACK_ROOT", "")
    if not root_text:
        print("error: VESSELSTACK_ROOT (or --root) is required", file=sys.stderr)
        return 2
    ctx = lifecycle.Context(
        root=Path(root_text),
        env={key: os.environ[key] for key in LIFECYCLE_ENV_KEYS if key in os.environ},
        require_root=not getattr(args, "allow_non_root", False),
        container=getattr(args, "container", None) or mqtt_auth.CONTAINER,
        image=getattr(args, "image", None), out=lambda *_a, **_k: None)
    try:
        result = action(ctx)
    except editor.EditError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "errors": exc.errors, **exc.extra}))
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except lifecycle.LifecycleError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "errors": [str(exc)]}))
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except (mqtt_auth.MqttAuthError, OSError) as exc:
        message = getattr(exc, "strerror", None) or str(exc)
        print(json.dumps({"ok": False, "code": 1, "errors": [message]}))
        print(f"error: {message}", file=sys.stderr)
        return 1
    if text_kind and not getattr(args, "json", True):
        print(editor.format_text(text_kind, result))
    else:
        print(json.dumps(result))
    return 0


def read_stdin_limited() -> str:
    data = sys.stdin.buffer.read(editor.STDIN_MAX_BYTES + 1)
    if len(data) > editor.STDIN_MAX_BYTES:
        raise editor.EditError(["the request is larger than 1 MiB"], 2)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise editor.EditError(["the request is not valid UTF-8"], 2) from exc


def cmd_catalog(args) -> int:
    return run_json(args, lambda ctx: editor.catalog(ctx, live=args.live), "catalog")


def cmd_show(args) -> int:
    return run_json(args, lambda ctx: editor.show(ctx, args.id), "show")


def cmd_history(args) -> int:
    return run_json(args, editor.history, "history")


def cmd_serial_ports(args) -> int:
    return run_json(args, editor.serial_ports, "serial-ports")


def cmd_set_pages(args) -> int:
    return run_json(args, lambda ctx: editor.set_pages(ctx, args.id, args.if_revision, read_stdin_limited()))


def cmd_restore_config(args) -> int:
    return run_json(args, lambda ctx: editor.restore_config(ctx, args.stamp, args.if_revision))


def cmd_list(args) -> int:
    if getattr(args, "json", False):
        return run_json(args, editor.list_displays)
    return run_lifecycle(args, lifecycle.list_displays)


def cmd_add(args) -> int:
    return run_lifecycle(args, lambda ctx: lifecycle.add(
        ctx, args.id, args.board, args.name, args.home_assistant_api))


def cmd_remove(args) -> int:
    return run_lifecycle(args, lambda ctx: lifecycle.remove(ctx, args.id, args.yes))


def cmd_rotate(args) -> int:
    return run_lifecycle(args, lambda ctx: lifecycle.rotate(ctx, args.id))


def cmd_wifi(args) -> int:
    return run_lifecycle(args, lambda ctx: lifecycle.wifi(ctx, args.clear, args.open))


def cmd_set_theme(args) -> int:
    return run_lifecycle(args, lambda ctx: lifecycle.set_theme(ctx, args.id, args.theme))


def cmd_themes(args) -> int:
    """List the built-in display themes (reads the installed themes.json; needs no root)."""
    try:
        data = schema.load_themes()
        ids = schema.theme_ids()
    except (OSError, ValueError) as exc:
        print(f"error: cannot load the display themes: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"schema": data["schema"], "themes": {name: data["themes"][name] for name in ids}},
                         indent=2))
        return 0
    width = max(len(name) for name in ids)
    for name in ids:
        print(f"{name.ljust(width)}  {data['themes'][name]['description']}")
    print("Choose one with 'vesselstackctl display set-theme ID THEME', then rebuild and reflash the display.")
    return 0


def cmd_status(args) -> int:
    return run_lifecycle(args, lifecycle.status)


def _warn(text: str) -> None:
    print(text, file=sys.stderr)


def run_render(args, action) -> int:
    root_text = args.root or os.environ.get("VESSELSTACK_ROOT", "")
    if not root_text:
        print("error: VESSELSTACK_ROOT (or --root) is required", file=sys.stderr)
        return 2
    # Informational output goes to stderr so 'export --stdout' keeps stdout clean.
    ctx = lifecycle.Context(root=Path(root_text), env={}, require_root=not args.allow_non_root,
                            out=print if action in STDOUT_ACTIONS else _warn)
    try:
        action(ctx, args)
    except (render.RenderError, lifecycle.LifecycleError) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        hints = getattr(exc, "hints", None)
        if hints:
            print("To fix: " + " ".join(hints), flush=True)
        return exc.code
    except (mqtt_auth.MqttAuthError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_render_action(ctx, args) -> None:
    render.render(ctx, args.id, args.allow_missing_wifi, warn=_warn)


def cmd_export_action(ctx, args) -> None:
    render.export(ctx, args.id, args.output, args.stdout, warn=_warn,
                  allow_missing_wifi=args.allow_missing_wifi)


def cmd_build_action(ctx, args) -> None:
    firmware.build(ctx, args.id, warn=_warn)


def cmd_flash_action(ctx, args) -> None:
    firmware.flash(ctx, args.id, serial=args.serial, ota=args.ota, previous=args.previous,
                   force=args.force, notify=_warn, progress=args.progress)


def cmd_deploy_action(ctx, args) -> None:
    firmware.deploy(ctx, args.id, serial=args.serial, ota=args.ota, progress=args.progress, notify=_warn)


STDOUT_ACTIONS = (cmd_render_action, cmd_build_action, cmd_flash_action, cmd_deploy_action)


def cmd_build(args) -> int:
    return run_render(args, cmd_build_action)


def cmd_flash(args) -> int:
    return run_render(args, cmd_flash_action)


def cmd_deploy(args) -> int:
    return run_render(args, cmd_deploy_action)


def cmd_render(args) -> int:
    return run_render(args, cmd_render_action)


def cmd_export(args) -> int:
    return run_render(args, cmd_export_action)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="displayctl", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="validate a displays config file")
    validate.add_argument("file", nargs="?", default=str(DEFAULT_CONFIG))
    validate.set_defaults(func=cmd_validate)
    sync = sub.add_parser("sync-mqtt", help="render Mosquitto ACL and passwords")
    sync.add_argument("--root", help="VESSELSTACK_ROOT (default: environment)")
    sync.add_argument("--data", help="VESSELSTACK_DATA (default: environment)")
    sync.add_argument("--config", help="displays.json (default: $ROOT/config/displays.json)")
    sync.add_argument("--image", help="override the pinned Mosquitto image")
    sync.add_argument("--compose", help="compose.yml that pins the image (default: the installed one)")
    sync.add_argument("--container", default=mqtt_auth.CONTAINER,
                      help=argparse.SUPPRESS)  # integration tests use a unique broker name
    sync.set_defaults(func=cmd_sync_mqtt)
    preflight = sub.add_parser("preflight", help="read-only checks before an install or update")
    preflight.add_argument("--root", help="VESSELSTACK_ROOT (default: environment)")
    preflight.add_argument("--config", help="displays.json (default: $ROOT/config/displays.json)")
    preflight.add_argument("--compose", help="compose.yml that pins the image")
    preflight.add_argument("--image", help="override the pinned Mosquitto image")
    group = preflight.add_mutually_exclusive_group()
    group.add_argument("--check-image", action="store_true",
                       help="require the Mosquitto image to be local or pullable; never pulls")
    group.add_argument("--pull-image", action="store_true",
                       help="pull the Mosquitto image now when it is missing")
    preflight.set_defaults(func=cmd_preflight)
    bridge_env = sub.add_parser("bridge-env", help="write the displays bridge environment file")
    bridge_env.add_argument("--root", help="VESSELSTACK_ROOT (default: environment)")
    bridge_env.add_argument("--output", required=True, help="file to write (mode 0600)")
    bridge_env.set_defaults(func=cmd_bridge_env)
    def lifecycle_parser(name, help_text, func, with_id=False):
        parser_ = sub.add_parser(name, help=help_text)
        if with_id:
            parser_.add_argument("id", metavar="ID", help="display id")
        parser_.add_argument("--root", help="VESSELSTACK_ROOT (default: environment)")
        parser_.add_argument("--container", default=mqtt_auth.CONTAINER, help=argparse.SUPPRESS)
        parser_.add_argument("--image", help=argparse.SUPPRESS)
        # Integration tests on non-root hosts; file permissions still apply.
        parser_.add_argument("--allow-non-root", action="store_true", help=argparse.SUPPRESS)
        parser_.set_defaults(func=func)
        return parser_

    list_parser = lifecycle_parser("list", "list configured displays (never prints secrets)", cmd_list)
    list_parser.add_argument("--json", action="store_true",
                             help="print only id, name, board and page count as JSON (for the Control Panel)")
    add = lifecycle_parser("add", "add a display with a default page (root)", cmd_add, True)
    add.add_argument("--board", required=True, help="board id from the registry")
    add.add_argument("--name", help="display name (default: derived from the id)")
    add.add_argument("--home-assistant-api", action="store_true",
                     help="enable the Home Assistant API on the display")
    remove = lifecycle_parser("remove", "remove a display and its credentials (root)",
                              cmd_remove, True)
    remove.add_argument("--yes", action="store_true", help="confirm the removal")
    lifecycle_parser("rotate", "issue new generated credentials for a display (root)",
                     cmd_rotate, True)
    wifi = lifecycle_parser("wifi", "store the shared Wi-Fi credentials, read from the "
                            "terminal (root)", cmd_wifi)
    wifi.add_argument("--clear", action="store_true", help="remove the stored credentials")
    wifi.add_argument("--open", action="store_true",
                      help="allow an empty password (open network)")
    def render_parser(name, help_text, func):
        parser_ = sub.add_parser(name, help=help_text)
        parser_.add_argument("id", metavar="ID", help="display id")
        parser_.add_argument("--root", help="VESSELSTACK_ROOT (default: environment)")
        parser_.add_argument("--allow-missing-wifi", action="store_true",
                             help="write placeholder Wi-Fi values (CI and golden tests only)")
        parser_.add_argument("--allow-non-root", action="store_true", help=argparse.SUPPRESS)
        parser_.set_defaults(func=func)
        return parser_

    render_parser("render", "generate the ESPHome project in $VESSELSTACK_ROOT/build/displays/ID "
                  "(root)", cmd_render)
    export = render_parser("export", "write the ESPHome project as a tar.gz with secrets (root)",
                           cmd_export)
    target = export.add_mutually_exclusive_group(required=True)
    target.add_argument("--output", metavar="FILE", help="create FILE (mode 0600); refuses to overwrite")
    target.add_argument("--stdout", action="store_true", help="stream the archive to stdout")
    def firmware_parser(name, help_text, func):
        parser_ = sub.add_parser(name, help=help_text)
        parser_.add_argument("id", metavar="ID", help="display id")
        parser_.add_argument("--root", help="VESSELSTACK_ROOT (default: environment)")
        parser_.add_argument("--allow-non-root", action="store_true", help=argparse.SUPPRESS)
        parser_.set_defaults(func=func)
        return parser_

    firmware_parser("build", "compile the firmware on this machine with the pinned ESPHome "
                    "image and store it with a manifest (root, optional)", cmd_build)
    flash = firmware_parser("flash", "upload the stored firmware over USB serial or Wi-Fi "
                            "(root, optional; never compiles)", cmd_flash)
    flash_target = flash.add_mutually_exclusive_group(required=True)
    flash_target.add_argument("--serial", metavar="DEVICE",
                              help="USB serial device, /dev/ttyACM0 or /dev/serial/by-id/...")
    flash_target.add_argument("--ota", metavar="HOST",
                              help="IP address or host name of the display on the LAN")
    flash.add_argument("--previous", action="store_true",
                       help="flash the previous build (firmware.prev) to roll back")
    flash.add_argument("--force", action="store_true",
                       help="flash even if the credentials changed since the firmware was built")
    flash.add_argument("--progress", action="store_true",
                       help="print 'STAGE: upload|done' and hide secrets in the tool output (used by the Control Panel)")
    deploy = firmware_parser("deploy", "build if needed and upload in one step, holding the build lock "
                             "throughout (root, optional)", cmd_deploy)
    deploy_target = deploy.add_mutually_exclusive_group(required=True)
    deploy_target.add_argument("--serial", metavar="DEVICE",
                               help="USB serial device, /dev/ttyACM0 or /dev/serial/by-id/...")
    deploy_target.add_argument("--ota", metavar="HOST",
                               help="IP address or host name of the display on the LAN")
    deploy.add_argument("--progress", action="store_true",
                        help="print 'STAGE: render|compile|upload|done' lines (used by the Control Panel)")
    set_theme = lifecycle_parser("set-theme", "choose the colour theme of a display (root); rebuild and "
                                 "reflash afterwards", cmd_set_theme, True)
    set_theme.add_argument("theme", metavar="THEME", help="theme id (see 'themes'); classic removes the key")
    themes = sub.add_parser("themes", help="list the available display themes")
    themes.add_argument("--json", action="store_true", help="print the full palettes as JSON")
    themes.set_defaults(func=cmd_themes)
    catalog = lifecycle_parser("catalog", "list the data a page can show (JSON for the Control Panel)", cmd_catalog)
    catalog.add_argument("--live", action="store_true",
                         help="also ask the configured Signal K server which values have data")
    catalog.add_argument("--json", action="store_true", help="print JSON")
    show = lifecycle_parser("show", "everything needed to edit one display (JSON for the Control Panel)",
                            cmd_show, True)
    show.add_argument("--json", action="store_true", help="print JSON")
    set_pages = lifecycle_parser("set-pages", "replace a display's pages from JSON on stdin (root); "
                                 "prints JSON", cmd_set_pages, True)
    set_pages.add_argument("--if-revision", required=True, metavar="SHA256",
                           help="the revision from 'show'; refuse (exit 3) if displays.json changed since")
    set_pages.add_argument("--stdin", action="store_true", required=True,
                           help="read the request {pages, theme?, new_instruments?, new_notifications?, "
                                "instrument_edits?} from standard input")
    history = lifecycle_parser("history", "list the displays.json backups (root)", cmd_history)
    history.add_argument("--json", action="store_true", help="print JSON")
    restore = lifecycle_parser("restore-config", "restore a displays.json backup by STAMP (root); prints JSON",
                               cmd_restore_config)
    restore.add_argument("stamp", metavar="STAMP", help="a stamp from 'history', such as 20260101T120000Z")
    restore.add_argument("--if-revision", metavar="SHA256", help="refuse (exit 3) if displays.json changed")
    ports = lifecycle_parser("serial-ports", "list USB serial devices that flash --serial accepts",
                             cmd_serial_ports)
    ports.add_argument("--json", action="store_true", help="print JSON")
    lifecycle_parser("status", "show bridge and display online state from retained MQTT topics",
                     cmd_status)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
