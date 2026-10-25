#!/usr/bin/env python3
"""VesselStack displays control tool (preview: validation and MQTT account sync)."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

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
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
