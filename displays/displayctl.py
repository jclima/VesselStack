#!/usr/bin/env python3
"""VesselStack displays control tool (preview: validation, MQTT account sync, device lifecycle)."""
from __future__ import annotations

import argparse
import os
import pwd
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lifecycle  # noqa: E402
import mqtt_auth  # noqa: E402
import schema  # noqa: E402

DEFAULT_CONFIG = schema.MODULE_DIR / "displays.example.json"


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
    if errors:
        for err in errors:
            print(f"{args.file}: {err}", file=sys.stderr)
        print(f"{len(errors)} error(s); nothing written", file=sys.stderr)
        return 1
    print(f"OK: {args.file}: {len(config['instruments'])} instrument(s), "
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
            "DISPLAYS_BRIDGE_MQTT_USERNAME") if key in os.environ}
    try:
        summary = mqtt_auth.sync(Path(root_text), Path(data_text), env,
                                 displays_config=Path(args.config) if args.config else None,
                                 image=args.image, container=args.container)
    except (mqtt_auth.MqttAuthError, OSError) as exc:
        print(f"error: sync-mqtt failed: {exc}", file=sys.stderr)
        return 1
    print(f"MQTT access control synced: {summary['accounts']} account(s), "
          f"{summary['displays']} display(s), displays "
          f"{'enabled' if summary['enabled'] else 'disabled'}, "
          f"broker {'reloaded' if summary['reloaded'] else 'not running'}")
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
    return "".join(systemd_env_line(key, value) for key, value in values)


def cmd_bridge_env(args) -> int:
    """Write the bridge's systemd EnvironmentFile (0600) without secrets on argv."""
    root_text = args.root or os.environ.get("VESSELSTACK_ROOT", "")
    if not root_text:
        print("error: VESSELSTACK_ROOT (or --root) is required", file=sys.stderr)
        return 2
    root = Path(root_text)
    env = {key: os.environ[key] for key in
           ("SIGNALK_URL", "MQTT_PORT", "DISPLAYS_BRIDGE_MQTT_USERNAME",
            "DISPLAYS_SIGNALK_TOKEN") if key in os.environ}
    try:
        secrets_data = mqtt_auth.load_secrets(root / "config" / mqtt_auth.SECRETS_NAME)
        password = secrets_data["bridge"].get("mqtt_password")
        if not isinstance(password, str) or not password:
            raise ValueError("no bridge MQTT password yet; run sync-mqtt with DISPLAYS_ENABLE=true first")
        text = render_bridge_env(root, env, password)
        owner = pwd.getpwnam(args.owner) if args.owner else None
        output = Path(args.output)
        mqtt_auth.write_atomic(output, text, 0o600)
        if owner is not None:
            os.chown(output, owner.pw_uid, owner.pw_gid)
    except (mqtt_auth.MqttAuthError, OSError, ValueError, KeyError) as exc:
        print(f"error: bridge-env failed: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {args.output} (mode 0600)")
    return 0


LIFECYCLE_ENV_KEYS = ("MQTT_USERNAME", "MQTT_PASSWORD", "DISPLAYS_ENABLE",
                      "DISPLAYS_BRIDGE_MQTT_USERNAME")


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


def cmd_list(args) -> int:
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


def cmd_status(args) -> int:
    return run_lifecycle(args, lifecycle.status)


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
    sync.add_argument("--container", default=mqtt_auth.CONTAINER,
                      help=argparse.SUPPRESS)  # integration tests use a unique broker name
    sync.set_defaults(func=cmd_sync_mqtt)
    bridge_env = sub.add_parser("bridge-env", help="write the displays bridge environment file")
    bridge_env.add_argument("--root", help="VESSELSTACK_ROOT (default: environment)")
    bridge_env.add_argument("--output", required=True, help="file to write (mode 0600)")
    bridge_env.add_argument("--owner", help="user that will own the file")
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

    lifecycle_parser("list", "list configured displays (never prints secrets)", cmd_list)
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
    lifecycle_parser("status", "show bridge and display online state from retained MQTT topics",
                     cmd_status)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
