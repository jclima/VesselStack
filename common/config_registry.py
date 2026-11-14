#!/usr/bin/env python3
"""The shared, data-only configuration contract.

The wizard and Control Panel deliberately keep their human-facing labels and
help text local.  This module owns the values which must agree between those
surfaces (defaults, types, ranges, choices, ownership and components).
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import urllib.parse
import shlex
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Setting:
    default: str = ""
    kind: str = "text"
    owner: str = "vessel"
    required: bool = False
    enum: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    component: str | None = None


def _s(default: str = "", **kwargs: Any) -> Setting:
    return Setting(default=str(default), **kwargs)


# The wizard defaults are canonical.  Legacy installer defaults are explicit:
# these are intentionally different for an existing Signal K and for the raw
# Influx bucket, and must not be silently normalized to wizard defaults.
SETTINGS: dict[str, Setting] = {
    "HOMEPAGE_THEME": _s("dark", enum=("dark", "light"), component="homepage"),
    "HOMEPAGE_COLUMNS": _s("standard", enum=("compact", "standard", "wide"), component="homepage"),
    "HOMEPAGE_HEADING": _s("boat", enum=("boat", "brand"), component="homepage"),
    "HOMEPAGE_CLOCK": _s("24h", enum=("24h", "12h", "hidden"), component="homepage"),
    "SIGNALK_VESSELTWIN_PRELOAD": _s("true", kind="boolean", component="signalk"),
    "SIGNALK_DATAPATH_AUTOCONFIG": _s("true", kind="boolean", component="signalk"),
    "SIGNALK_MARINE_BUNDLE": _s("false", kind="boolean", component="signalk"),
    "BOAT_NAME": _s("My Boat", required=True), "BOAT_TYPE": _s("Motor vessel"),
    "BOAT_MMSI": _s(), "BOAT_CALLSIGN": _s(), "BOAT_LOA_M": _s(kind="number", minimum=0.01, maximum=500), "BOAT_BEAM_M": _s(kind="number", minimum=0.01, maximum=100),
    "BOAT_TIMEZONE": _s("UTC", required=True), "BOAT_UNITS": _s("us_customary", enum=("us_customary", "metric")),
    "VESSELSTACK_USER": _s("boat", required=True), "VESSELSTACK_UID": _s("1000", kind="number", minimum=1, maximum=65534, required=True), "VESSELSTACK_GID": _s("1000", kind="number", minimum=1, maximum=65534, required=True),
    "VESSELSTACK_ROOT": _s("/opt/vesselstack", required=True), "VESSELSTACK_DATA": _s("/opt/vesselstack-data", required=True), "VESSELSTACK_BACKUP": _s("/media/vesselstack-backup/backups", required=True),
    "SIGNALK_MODE": _s("docker", enum=("existing", "docker", "native"), component="signalk"), "SIGNALK_VERSION": _s("v2.27.0", required=True), "SIGNALK_URL": _s("http://127.0.0.1:3000", kind="url", required=True),
    "SOCKETCAN_ENABLE": _s("false", kind="boolean", component="socketcan"), "VESSELSTACK_CAN_CREATE_VCAN": _s("false", kind="boolean", component="socketcan"), "SOCKETCAN_INTERFACE": _s("can0", required=True, component="socketcan"), "SOCKETCAN_BITRATE": _s("250000", kind="number", minimum=10000, maximum=1000000, required=True, component="socketcan"),
    "AIS_ENABLE": _s("false", kind="boolean", component="ais-catcher"), "AIS_IMAGE": _s("ghcr.io/jvde-github/ais-catcher:v0.70", required=True, component="ais-catcher"), "AIS_DEVICE": _s("/dev/bus/usb", required=True, component="ais-catcher"), "AIS_CATCHER_ARGS": _s("-q -N 8100 -S 5011", required=True, component="ais-catcher"), "AIS_WEB_PORT": _s("8100", kind="port", minimum=1, maximum=65535, required=True, component="ais-catcher"), "AIS_TCP_PORT": _s("5011", kind="port", minimum=1, maximum=65535, required=True, component="ais-catcher"),
    "HOME_ASSISTANT_URL": _s("http://127.0.0.1:8123", kind="url"), "HOME_ASSISTANT_TOKEN": _s("", owner="vessel"), "INFLUXDB_URL": _s("http://127.0.0.1:8086", kind="url", required=True), "INFLUXDB_PORT": _s("8086", kind="port", minimum=1, maximum=65535, required=True, component="influxdb"), "INFLUXDB_CONTAINER_NAME": _s("vesselstack-influxdb", required=True, component="influxdb"), "INFLUXDB_ORG": _s("vesselstack", required=True, component="influxdb"), "INFLUXDB_USERNAME": _s("boatadmin", required=True, component="influxdb"),
    "INFLUXDB_RAW_BUCKET": _s("signalk", required=True, component="influxdb"), "INFLUXDB_HISTORY_BUCKET": _s("signalk_1m", required=True, component="influxdb"), "INFLUXDB_HOME_ASSISTANT_BUCKET": _s("homeassistant", required=True, component="influxdb"), "INFLUXDB_AIS_BUCKET": _s("ais", required=True, component="influxdb"), "INFLUXDB_RAW_RETENTION": _s(), "INFLUXDB_PASSWORD": _s("GENERATE", owner="vessel"), "INFLUXDB_TOKEN": _s("GENERATE", owner="vessel"), "GRAFANA_ADMIN_PASSWORD": _s("GENERATE", owner="vessel"),
    "MQTT_USERNAME": _s("homeassistant", required=True, component="mosquitto"), "MQTT_PASSWORD": _s("GENERATE", component="mosquitto"), "MQTT_PORT": _s("1883", kind="port", minimum=1, maximum=65535, required=True, component="mosquitto"), "BOAT_CHAT_PROVIDER": _s("local", owner="chat", enum=("local", "codex_cli", "claude_cli", "openai", "vercel", "bedrock", "google", "ollama", "openai_compatible"), component="boat-chat"), "BOAT_CHAT_HOST": _s("0.0.0.0", owner="chat", required=True, component="boat-chat"), "BOAT_CHAT_PORT": _s("8765", kind="port", owner="chat", minimum=1, maximum=65535, required=True, component="boat-chat"), "BOAT_CHAT_SETTINGS_TOKEN": _s("GENERATE", owner="chat", component="boat-chat"), "BOAT_CHAT_ACCESS_TOKEN": _s("GENERATE", owner="chat", component="boat-chat"),
    "VESSELSTACK_DATA_BIND_ADDRESS": _s("127.0.0.1"), "VESSELSTACK_WEB_BIND_ADDRESS": _s("0.0.0.0"), "HOMEASSISTANT_BIND_ADDRESS": _s("0.0.0.0"), "TELEGRAM_ENABLE": _s("false", kind="boolean", owner="vessel", component="telegram"), "TELEMETRY_INDEXER_ENABLE": _s("true", kind="boolean", owner="vessel", component="telemetry-indexer"), "NODE_EXPORTER_ENABLE": _s("false", kind="boolean", component="node-exporter"),
    # Optional Node-RED (README "Node-RED (optional)"). Local-only (127.0.0.1) by default; it no longer follows VESSELSTACK_WEB_BIND_ADDRESS.
    "NODERED_ENABLE": _s("false", kind="boolean", component="nodered"), "NODERED_PORT": _s("1880", kind="port", minimum=1, maximum=65535, required=True, component="nodered"), "NODERED_BIND_ADDRESS": _s("127.0.0.1", component="nodered"),
    "NODERED_ADMIN_PASSWORD": _s("GENERATE", owner="installer", component="nodered"), "NODERED_ADMIN_PASSWORD_HASH": _s("GENERATE", owner="installer", component="nodered"), "NODERED_DASHBOARD_PASSWORD": _s("GENERATE", owner="installer", component="nodered"), "NODERED_DASHBOARD_PASSWORD_HASH": _s("GENERATE", owner="installer", component="nodered"), "NODERED_CREDENTIAL_SECRET": _s("GENERATE", owner="installer", component="nodered"),
    "GRAFANA_PORT": _s("43000", kind="port", minimum=1, maximum=65535, required=True, component="grafana"), "HOMEPAGE_PORT": _s("80", kind="port", minimum=1, maximum=65535, required=True, component="homepage"), "PROMETHEUS_PORT": _s("9090", kind="port", minimum=1, maximum=65535, required=True, component="prometheus"), "PROMETHEUS_RETENTION": _s("30d", required=True), "VESSELSTACK_UNTRUSTED_INTERFACE": _s("wlan0"), "VESSELSTACK_FIREWALL_ENABLE": _s("false", kind="boolean", component="firewall"), "VESSELSTACK_FIREWALL_STRICT": _s("false", kind="boolean", component="firewall"), "VESSELSTACK_FIREWALL_ALLOW_TCP": _s("22", component="firewall"), "VESSELSTACK_FIREWALL_ALLOW_UDP": _s("", component="firewall"), "CONTROL_PANEL_HOST": _s("127.0.0.1", required=True, component="control-panel"), "CONTROL_PANEL_PORT": _s("8780", kind="port", minimum=1, maximum=65535, required=True, component="control-panel"),
    "DISPLAYS_ENABLE": _s("false", kind="boolean", component="displays-bridge"), "DISPLAYS_BRIDGE_MQTT_USERNAME": _s("vesselstack-bridge", required=True, component="displays-bridge"), "DISPLAYS_SIGNALK_TOKEN": _s(), "DISPLAYS_HA_TOKEN": _s(), "VESSELSTACK_DISABLED_COMPONENTS": _s(), "HOMEPAGE_ALLOWED_HOSTS": _s(), "HOMEPAGE_ALLOWED_HOSTS_EXTRA": _s(), "CONTROL_PANEL_PASSPHRASE_HASH": _s(owner="panel"), "BOAT_CHAT_PASSPHRASE_HASH": _s(owner="chat"),
    "VESSELSTACK_WATCHDOG": _s("auto", enum=("auto", "true", "false")), "VESSELSTACK_JOURNAL_MAX_USE": _s("200M", required=True),
    "VESSELSTACK_BACKUP_SCHEDULE": _s("off", enum=("off", "daily", "weekly")), "VESSELSTACK_BACKUP_KEEP": _s("8", kind="number", minimum=1, maximum=365, required=True), "VESSELSTACK_BACKUP_SCHEDULE_SYSTEM_DISK": _s("false", kind="boolean"),
    # Opt-in Telegram alerts (README "Alerts"). Blank thresholds and entities leave that alert off.
    "ALERTS_ENABLE": _s("false", kind="boolean", component="alerts"), "ALERT_TELEGRAM_CHAT_IDS": _s(component="alerts"), "ALERT_BATTERY_LOW_V": _s(component="alerts"), "ALERT_BILGE_ENTITY": _s(component="alerts"), "ALERT_SHORE_POWER_ENTITY": _s(component="alerts"),
    # Optional Tailscale remote access (README "Remote access with Tailscale"). Off by default; enabled only by `vesselstackctl remote enable`.
    "REMOTE_ACCESS": _s("off", enum=("off", "tailscale"), component="remote"), "REMOTE_PANEL_SERVE": _s("true", kind="boolean", component="remote"),
    "ALERT_HEARTBEAT_URL": _s(component="alerts"), "ALERT_REPEAT_MINUTES": _s("60", kind="number", minimum=5, maximum=10080, required=True, component="alerts"), "ALERT_STALE_DATA_MINUTES": _s("15", kind="number", minimum=2, maximum=10080, required=True, component="alerts"),
}

DEFAULTS = {key: spec.default for key, spec in SETTINGS.items()}
LEGACY_DEFAULTS = {"SIGNALK_MODE": "existing", "INFLUXDB_RAW_BUCKET": "vesselstack_raw", "INFLUXDB_HISTORY_BUCKET": "vesselstack_1m", "VESSELSTACK_BACKUP": "/var/backups/vesselstack"}
CORE_COMPONENTS = ("homeassistant", "influxdb", "grafana", "prometheus", "mosquitto", "homepage")
COMPOSE_COMPONENTS = {name: name for name in (*CORE_COMPONENTS, "signalk", "ais-catcher", "node-exporter", "nodered")}
SYSTEMD_COMPONENTS = {"boat-chat": "vesselstack-chat.service", "telegram": "vesselstack-chat-telegram.service", "telemetry-indexer": "vesselstack-telemetry-indexer.timer", "telemetry-sample": "vesselstack-telemetry-sample.timer", "displays-bridge": "vesselstack-displays-bridge.service", "signalk-native": "vesselstack-signalk.service", "socketcan": "vesselstack-socketcan.service", "firewall": "vesselstack-firewall.service", "control-panel": "vesselstack-control-panel.service", "alerts": "vesselstack-alerts.timer"}

def validate_value(key: str, value: Any, *, allow_blank: bool = True) -> str:
    if key not in SETTINGS:
        raise ValueError(f"Unknown setting: {key}")
    spec = SETTINGS[key]; text = str(value).strip()
    if "\x00" in text or "\n" in text or "\r" in text:
        raise ValueError(f"Invalid value for {key}")
    if not text:
        if spec.required: raise ValueError(f"{key} is required")
        if allow_blank: return text
    if spec.enum and text not in spec.enum: raise ValueError(f"Invalid value for {key}")
    if spec.kind == "boolean" and text not in {"true", "false"}: raise ValueError(f"Invalid boolean for {key}")
    if spec.kind in {"number", "port"} and text:
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text): raise ValueError(f"Invalid number for {key}")
        if spec.kind == "port" and "." in text: raise ValueError(f"Invalid integer for {key}")
        if spec.kind == "number" and "." in text and spec.minimum is not None and float(spec.minimum).is_integer() and spec.maximum is not None and float(spec.maximum).is_integer(): raise ValueError(f"Invalid integer for {key}")
        number = float(text)
        if spec.minimum is not None and number < spec.minimum or spec.maximum is not None and number > spec.maximum: raise ValueError(f"Invalid range for {key}")
    if spec.kind == "url" and text:
        parsed = urllib.parse.urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc: raise ValueError(f"Invalid URL for {key}")
    if key in {"CONTROL_PANEL_HOST", "BOAT_CHAT_HOST", "VESSELSTACK_DATA_BIND_ADDRESS", "VESSELSTACK_WEB_BIND_ADDRESS", "HOMEASSISTANT_BIND_ADDRESS", "NODERED_BIND_ADDRESS"} and text:
        try:
            if ipaddress.ip_address(text).version != 4: raise ValueError
        except ValueError: raise ValueError(f"Invalid IPv4 address for {key}") from None
    if key in {"MQTT_USERNAME", "DISPLAYS_BRIDGE_MQTT_USERNAME"} and text and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", text): raise ValueError(f"Invalid MQTT name for {key}")
    if key == "VESSELSTACK_JOURNAL_MAX_USE" and text and text != "off" and not re.fullmatch(r"[1-9][0-9]*[KMG]", text): raise ValueError("Invalid VESSELSTACK_JOURNAL_MAX_USE: use a size such as 200M or 1G, or off")
    if key == "VESSELSTACK_BACKUP_KEEP" and text and not (re.fullmatch(r"[1-9][0-9]*", text) and int(text) <= 365): raise ValueError("Invalid VESSELSTACK_BACKUP_KEEP: use a whole number from 1 to 365")
    if key == "ALERT_BATTERY_LOW_V" and text and not (re.fullmatch(r"[0-9]{1,2}(?:\.[0-9]{1,2})?", text) and 0 < float(text) <= 60): raise ValueError("Invalid ALERT_BATTERY_LOW_V: use a voltage such as 12.0 (12 V bank) or 24.0 (24 V bank), or leave it blank to turn the alert off")
    if key in {"ALERT_BILGE_ENTITY", "ALERT_SHORE_POWER_ENTITY"} and text and not re.fullmatch(r"[a-z_]+\.[A-Za-z0-9_]+", text): raise ValueError(f"Invalid {key}: use a Home Assistant entity id such as binary_sensor.bilge_pump")
    if key == "ALERT_TELEGRAM_CHAT_IDS" and text and not re.fullmatch(r"-?[0-9]{1,20}(?:\s*,\s*-?[0-9]{1,20})*", text): raise ValueError("Invalid ALERT_TELEGRAM_CHAT_IDS: use comma-separated numeric Telegram chat ids")
    if key == "ALERT_HEARTBEAT_URL" and text and not (re.fullmatch(r"https://[^\s'\"$`\\]+", text) and urllib.parse.urlparse(text).netloc): raise ValueError("Invalid ALERT_HEARTBEAT_URL: use an https:// address")
    if key == "BOAT_NAME" and text and not re.fullmatch(r"[A-Za-z0-9_. -]{1,60}", text): raise ValueError("Invalid BOAT_NAME")
    if key in {"VESSELSTACK_FIREWALL_ALLOW_TCP", "VESSELSTACK_FIREWALL_ALLOW_UDP"} and text:
        if not re.fullmatch(r"[0-9]{1,5}(?::[0-9]{1,5})?(?:,[0-9]{1,5}(?::[0-9]{1,5})?)*", text): raise ValueError(f"Invalid port list for {key}")
        for item in text.split(","):
            ends = [int(x) for x in item.split(":")]
            if any(x < 1 or x > 65535 for x in ends) or len(ends) == 2 and ends[0] > ends[1]: raise ValueError(f"Invalid port list for {key}")
    return text

def validate(values: dict[str, Any], *, surface: str = "wizard", allow_blank: bool = True) -> dict[str, str]:
    unknown = set(values) - set(SETTINGS)
    if unknown: raise ValueError("Unknown settings: " + ", ".join(sorted(unknown)))
    result = DEFAULTS.copy()
    for key, value in values.items(): result[key] = validate_value(key, value, allow_blank=allow_blank)
    return result

def effective_chat_port(env: dict[str, Any] | None = None, override: dict[str, Any] | None = None, default: int = 8765) -> int:
    if override is not None and "BOAT_CHAT_PORT" in override:
        raw = str(override.get("BOAT_CHAT_PORT") or "").strip()
        return default if not raw else int(validate_value("BOAT_CHAT_PORT", raw, allow_blank=False))
    if env is not None and "BOAT_CHAT_PORT" in env:
        raw = str(env.get("BOAT_CHAT_PORT") or "").strip()
        return default if not raw else int(validate_value("BOAT_CHAT_PORT", raw, allow_blank=False))
    return default

def _read_env_file(path: Any) -> dict[str, str]:
    allowed = {"VESSELSTACK_DATA", "BOAT_CHAT_HOST", "BOAT_CHAT_PORT", "BOAT_CHAT_ENV"}
    values: dict[str, str] = {}
    try: lines = open(path, encoding="utf-8")
    except OSError: return values
    with lines:
        for raw in lines:
            match = re.fullmatch(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)", raw.rstrip("\n"))
            if not match: continue
            key = match.group(1)
            if key not in allowed:
                continue
            try: tokens = shlex.split(match.group(2), comments=False, posix=True)
            except ValueError as exc: raise ValueError(f"invalid env quoting in {path}: {exc}") from None
            values[key] = " ".join(tokens) if tokens else ""
    return values

def load_effective_chat_endpoint(root: Any, *, data: Any | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    base = _read_env_file(str(root) + "/config/vesselstack.env")
    base.update(_read_env_file(str(root) + "/config/boat-chat.env"))
    data_root = data or base.get("VESSELSTACK_DATA")
    explicit = (env or {}).get("BOAT_CHAT_ENV")
    if explicit:
        if os.path.isfile(explicit): base.update(_read_env_file(explicit))
    elif data_root:
        base.update(_read_env_file(str(data_root) + "/boat-chat/boat-chat.env"))
    host = base.get("BOAT_CHAT_HOST") or "0.0.0.0"
    host = validate_value("BOAT_CHAT_HOST", host, allow_blank=False)
    port = effective_chat_port(base)
    return {"host": host, "port": port, "url": f"http://{host}:{port}"}

def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("key", nargs="?"); parser.add_argument("--effective-chat-port", nargs=2, metavar=("BASE", "OVERRIDE")); parser.add_argument("--endpoint-root"); parser.add_argument("--core-components", action="store_true"); args = parser.parse_args()
    if args.endpoint_root:
        print(json.dumps(load_effective_chat_endpoint(args.endpoint_root, env=os.environ), sort_keys=True)); return 0
    if args.core_components:
        print("\n".join(CORE_COMPONENTS)); return 0
    if args.effective_chat_port:
        base, override = args.effective_chat_port
        print(effective_chat_port({"BOAT_CHAT_PORT": base}, {"BOAT_CHAT_PORT": override})); return 0
    payload = asdict(SETTINGS[args.key]) if args.key else {"defaults": DEFAULTS, "legacy_defaults": LEGACY_DEFAULTS, "core_components": CORE_COMPONENTS, "compose_components": COMPOSE_COMPONENTS, "systemd_components": SYSTEMD_COMPONENTS, "settings": {k: asdict(v) for k, v in SETTINGS.items()}}
    print(json.dumps(payload, indent=2, sort_keys=True)); return 0
if __name__ == "__main__": raise SystemExit(_main())
