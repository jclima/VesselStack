#!/usr/bin/env python3
"""Authenticated local administration service for VesselStack."""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import urllib.parse
import zoneinfo
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from browser_updates import installed_version, release_catalog, read_operation, public_operation, release_snapshot, running_identity, acquire_slot, release_slot, start_detached
except ImportError:
    import importlib.util
    _spec = importlib.util.spec_from_file_location("browser_updates", Path(__file__).with_name("browser_updates.py"))
    assert _spec and _spec.loader
    _updates = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_updates)
    installed_version = _updates.installed_version; release_catalog = _updates.release_catalog
    read_operation = _updates.read_operation; public_operation = _updates.public_operation
    running_identity = _updates.running_identity
    acquire_slot = _updates.acquire_slot; release_slot = _updates.release_slot
    start_detached = _updates.start_detached
    release_snapshot = _updates.release_snapshot


ROOT = Path(os.environ.get("VESSELSTACK_ROOT", "/opt/vesselstack"))
STATIC_ROOT = Path(__file__).resolve().parent / "static"
VESSEL_ENV = Path(os.environ.get("VESSELSTACK_ENV", str(ROOT / "config/vesselstack.env")))
CHAT_ENV = ROOT / "config/boat-chat.env"
PANEL_ENV = ROOT / "config/control-panel.env"
INSTALLER = ROOT / "installer/install.sh"
CTL = Path(os.environ.get("VESSELSTACK_CTL", "/usr/local/sbin/vesselstackctl"))
REVIEWED_RELEASE_ROOT = Path(
    os.environ.get("VESSELSTACK_REVIEWED_RELEASES", "/var/lib/vesselstack/releases")
)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8780
RUNNING_IDENTITY = running_identity(ROOT)
CONFIG_BACKUP_ROOT = Path(
    os.environ.get("CONTROL_PANEL_CONFIG_BACKUPS", "/opt/vesselstack-data/control-panel/config-backups")
)


def field(
    key: str,
    label: str,
    section: str,
    source: str = "vessel",
    kind: str = "text",
    description: str = "",
    choices: list[str] | None = None,
    read_only: bool = False,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "section": section,
        "source": source,
        "kind": kind,
        "description": description,
        "choices": choices or [],
        "read_only": read_only,
    }


INIT_ONLY_NOTE = (
    "Read-only: {service} reads this only when its data volume is first created, so "
    "changing it here would desynchronise the config from the running service. "
    "Rotate it from the recovery shell (change it inside {service} first, then update "
    "config/vesselstack.env and re-apply the configuration); see the README, Rotating secrets."
)

SEC_VESSEL = "Vessel"
SEC_NETWORK = "Network & access"
SEC_CHAT = "Boat Chat"
SEC_SIGNALK = "Signal K & NMEA 2000"
SEC_DISPLAYS = "Displays"
SEC_STORAGE = "Storage & history"
SEC_SERVICES = "Services & ports"
SEC_AIS = "AIS receiver"
SEC_HA = "Home Assistant"
SEC_INSTALL = "Installation"

# Settings sections in display order. Advanced sections start collapsed in the UI.
SECTIONS = [
    {"name": SEC_VESSEL, "lead": "Who the boat is and how numbers are shown.", "advanced": False},
    {"name": SEC_NETWORK, "lead": "Who can reach the boat computer and its web pages.", "advanced": False},
    {"name": SEC_CHAT, "lead": "The assistant you can ask about your boat, and Telegram alerts.", "advanced": False},
    {"name": SEC_SIGNALK, "lead": "How boat data gets in.", "advanced": False},
    {"name": SEC_DISPLAYS, "lead": "Instrument displays that show boat data.", "advanced": False},
    {"name": SEC_STORAGE, "lead": "How long data is kept, where backups go, and the database settings.", "advanced": True},
    {"name": SEC_SERVICES, "lead": "Ports and accounts for the individual services.", "advanced": True},
    {"name": SEC_AIS, "lead": "The AIS-catcher receiver that shows nearby ships.", "advanced": True},
    {"name": SEC_HA, "lead": "How VesselStack reaches Home Assistant.", "advanced": True},
    {"name": SEC_INSTALL, "lead": "Read-only here. Change these from a recovery shell with a reviewed migration.", "advanced": True},
]

FIELDS = [
    # Vessel
    field("BOAT_NAME", "Boat name", SEC_VESSEL, description="Shown in headers, alerts and Boat Chat."),
    field("BOAT_TYPE", "Boat type", SEC_VESSEL, description="For example Motor vessel, Sailing yacht or Catamaran. Shown to people and Boat Chat; it does not change how anything is installed."),
    field("BOAT_UNITS", "Display units", SEC_VESSEL, kind="select", choices=["us_customary", "metric"], description="US customary shows feet, degrees F, psi and US gallons; metric shows metres, degrees C, kPa and litres. Speeds stay in knots and distances in nautical miles either way."),
    field("BOAT_TIMEZONE", "Timezone", SEC_VESSEL, description="A region and city name, such as Europe/Lisbon or America/Los_Angeles. Used for timestamps and daily summaries."),
    field("BOAT_LOA_M", "Length overall (m)", SEC_VESSEL, kind="number", description="Shown to Boat Chat as part of your boat's details. A number above 0, up to 500."),
    field("BOAT_BEAM_M", "Beam (m)", SEC_VESSEL, kind="number", description="Width at the widest point of the hull. A number above 0, up to 100."),
    field("BOAT_MMSI", "MMSI", SEC_VESSEL, kind="secret", description="Optional. Your vessel's radio identity number. Kept private on this boat computer; never commit it."),
    field("BOAT_CALLSIGN", "Call sign", SEC_VESSEL, kind="secret", description="Optional. Kept private on this boat computer; never commit it."),
    # Network & access
    field("VESSELSTACK_WEB_BIND_ADDRESS", "Web pages visible to", SEC_NETWORK, description="Grafana and Heimdall listen on this address. 127.0.0.1 means this device only; 0.0.0.0 means every device on the boat network."),
    field("VESSELSTACK_DATA_BIND_ADDRESS", "Data services visible to", SEC_NETWORK, description="InfluxDB, Prometheus and MQTT listen on this address. Keep 127.0.0.1 unless another device must connect over a network you trust."),
    field("HOMEASSISTANT_BIND_ADDRESS", "Home Assistant visible to", SEC_NETWORK, description="127.0.0.1 means this device only; 0.0.0.0 means every device on the boat network. Applied through a managed http block in Home Assistant's configuration.yaml when you apply your settings or run sudo vesselstackctl lan. Signal K has no such setting."),
    field("CONTROL_PANEL_HOST", "Control Panel address", SEC_NETWORK, description="Keep 127.0.0.1 to reach the panel through an SSH tunnel only. To open it to a trusted network, run sudo vesselstackctl panel-access lan on the boat computer; that exposes unencrypted HTTP on every interface and a strict firewall may still block it. Undo it with sudo vesselstackctl panel-access local."),
    field("CONTROL_PANEL_PORT", "Control Panel port", SEC_NETWORK, kind="port", description="The port the panel listens on. Restart the Control Panel after changing it."),
    field("VESSELSTACK_FIREWALL_ENABLE", "Protect the boat network with a firewall", SEC_NETWORK, kind="boolean", description="Blocks incoming connections on the interface named below. Strict mode allows only the ports you list, so keep another way in."),
    field("VESSELSTACK_UNTRUSTED_INTERFACE", "Untrusted network interface", SEC_NETWORK, description="The interface facing the marina or shore Wi-Fi, for example wlan0."),
    field("VESSELSTACK_FIREWALL_STRICT", "Strict firewall (allow-list only)", SEC_NETWORK, kind="boolean", description="Drops everything on the untrusted interface except the allowed ports below; keep another way to reach the device."),
    field("VESSELSTACK_FIREWALL_ALLOW_TCP", "Allowed TCP ports (strict mode)", SEC_NETWORK, description="Comma-separated ports or LOW:HIGH ranges, for example 22,8000:8100. Default 22."),
    field("VESSELSTACK_FIREWALL_ALLOW_UDP", "Allowed UDP ports (strict mode)", SEC_NETWORK, description="Comma-separated ports or LOW:HIGH ranges. Default none."),
    # Boat Chat
    field("BOAT_CHAT_PROVIDER", "Primary provider", SEC_CHAT, source="chat", kind="select", choices=["local", "codex_cli", "claude_cli", "openai", "vercel", "bedrock", "google", "ollama", "openai_compatible"], description="Where answers come from. Local keeps everything on the boat."),
    field("BOAT_CHAT_MODEL", "Primary model", SEC_CHAT, source="chat", description="Model name for the provider you picked."),
    field("BOAT_CHAT_FALLBACK_PROVIDER", "Fallback provider", SEC_CHAT, source="chat", kind="select", choices=["", "local", "codex_cli", "claude_cli", "openai", "vercel", "bedrock", "google", "ollama", "openai_compatible"], description="Used when the primary provider cannot be reached, for example offshore. Optional."),
    field("BOAT_CHAT_FALLBACK_MODEL", "Fallback model", SEC_CHAT, source="chat", description="Model name for the fallback provider."),
    field("BOAT_CHAT_HOST", "Boat Chat address", SEC_CHAT, source="chat", description="The address Boat Chat listens on. 127.0.0.1 keeps it on this device."),
    field("BOAT_CHAT_PORT", "Boat Chat port", SEC_CHAT, source="chat", kind="port", description="The port Boat Chat listens on."),
    field("BOAT_CHAT_ACCESS_TOKEN", "Boat Chat access token", SEC_CHAT, source="chat", kind="secret", description="Required by chat, status and data pages and the Telegram bridge."),
    field("TELEMETRY_INDEXER_ENABLE", "Refresh telemetry memory", SEC_CHAT, kind="boolean", description="Keeps Boat Chat's memory of your boat data up to date so it can answer questions about past readings."),
    field("OLLAMA_HOST", "Ollama address", SEC_CHAT, source="chat", kind="url", description="Where your Ollama server is. Used only when Ollama is a provider."),
    field("BOAT_CHAT_BASE_URL", "Compatible API base address", SEC_CHAT, source="chat", kind="url", description="Address of an OpenAI-compatible service. Used only with that provider."),
    field("OPENAI_API_KEY", "OpenAI API key", SEC_CHAT, source="chat", kind="secret", description="Write-only. Used only when OpenAI is a provider."),
    field("AI_GATEWAY_API_KEY", "Vercel AI Gateway key", SEC_CHAT, source="chat", kind="secret", description="Write-only. Used only when Vercel AI Gateway is a provider."),
    field("BOAT_CHAT_API_KEY", "Compatible API key", SEC_CHAT, source="chat", kind="secret", description="Write-only. Used only with an OpenAI-compatible service."),
    field("AWS_REGION", "AWS region", SEC_CHAT, source="chat", description="For example eu-west-1. Used only with Amazon Bedrock."),
    field("AWS_ACCESS_KEY_ID", "AWS access key", SEC_CHAT, source="chat", kind="secret", description="Write-only. Used only with Amazon Bedrock."),
    field("AWS_SECRET_ACCESS_KEY", "AWS secret key", SEC_CHAT, source="chat", kind="secret", description="Write-only. Used only with Amazon Bedrock."),
    field("GOOGLE_API_KEY", "Google API key", SEC_CHAT, source="chat", kind="secret", description="Write-only. Used only when Google is a provider."),
    field("GOOGLE_CLOUD_PROJECT", "Google Cloud project", SEC_CHAT, source="chat", description="The project name, if your Google setup needs one."),
    field("TELEGRAM_ENABLE", "Send alerts to Telegram", SEC_CHAT, kind="boolean", description="Runs the Telegram alerts component. It also needs the bot token below."),
    field("TELEGRAM_BOT_TOKEN", "Telegram bot token", SEC_CHAT, source="chat", kind="secret", description="From BotFather. Needed only for Telegram alerts."),
    field("TELEGRAM_ALLOWED_CHAT_IDS", "Allowed Telegram chats", SEC_CHAT, source="chat", kind="secret", description="The Telegram chat IDs that may use the bot. Kept private."),
    # Signal K & NMEA 2000
    field("SIGNALK_MODE", "Signal K mode", SEC_SIGNALK, kind="select", choices=["existing", "docker", "native"], description="Docker: VesselStack runs Signal K for you in a container (recommended). Existing: you already run a Signal K server and VesselStack only connects to it. Native: Signal K is installed directly on this Pi. Changing it takes effect when you apply your settings."),
    field("SIGNALK_VERSION", "Signal K version", SEC_SIGNALK, description="Release tag of the Signal K container image, for example v2.27.0."),
    field("SIGNALK_URL", "Signal K address", SEC_SIGNALK, kind="url", description="Where VesselStack reaches Signal K. Needed when the mode is Existing."),
    field("SIGNALK_VESSELTWIN_PRELOAD", "Preload the VesselTwin plugin", SEC_SIGNALK, kind="boolean", description="Installs the pinned signalk-vesseltwin plugin before Signal K starts (Docker and Native modes). Existing mode is never modified; turning this off skips installation and never uninstalls."),
    field("SOCKETCAN_ENABLE", "Read the NMEA 2000 network (SocketCAN)", SEC_SIGNALK, kind="boolean", description="Turn on only after the CAN adapter is installed and tested."),
    field("SOCKETCAN_INTERFACE", "CAN interface", SEC_SIGNALK, description="Name of the CAN interface on this Pi, usually can0."),
    field("SOCKETCAN_BITRATE", "CAN speed (bits per second)", SEC_SIGNALK, kind="number", description="NMEA 2000 always uses 250000. Change it only for a non-marine CAN network. Whole number from 10000 to 1000000."),
    # Displays
    field("DISPLAYS_ENABLE", "Enable instrument displays", SEC_DISPLAYS, kind="boolean", description="Lets the bridge publish boat data to each display, read-only. Adds the bridge and per-display accounts to the Mosquitto access list."),
    field("DISPLAYS_BRIDGE_MQTT_USERNAME", "Displays bridge MQTT username", SEC_DISPLAYS, description="Letters, numbers, dot, underscore and hyphen. Must differ from the main MQTT username."),
    field("DISPLAYS_SIGNALK_TOKEN", "Displays bridge Signal K token", SEC_DISPLAYS, kind="secret", description="Optional. Only needed if Signal K requires sign-in; leave blank otherwise."),
    field("DISPLAYS_HA_TOKEN", "Displays bridge Home Assistant token", SEC_DISPLAYS, kind="secret", description="Optional, only for Home Assistant items (ha) in displays.json; leave blank otherwise. A leaked token can operate every device in your Home Assistant; a non-admin user only prevents configuration changes. Keep it on the Pi (root-only), never reuse it, and revoke it in Home Assistant (Profile, Security) if it leaks."),
    # Advanced: Storage & history
    field("INFLUXDB_RAW_RETENTION", "Keep raw data for", SEC_STORAGE, description="A duration such as 720h, 30d or 8w, or 0 to keep forever. Empty means 30d on new installs and no change on existing ones. A value is applied to the existing raw bucket on the next apply or start and permanently deletes raw data older than the window."),
    field("PROMETHEUS_RETENTION", "Keep health numbers for", SEC_STORAGE, description="How long Prometheus keeps Pi and service health history: a duration such as 720h, 30d or 8w. Empty means 30d. Older metrics are deleted."),
    field("VESSELSTACK_BACKUP", "Backup folder", SEC_STORAGE, kind="path", description="Where backups are saved. Use a mounted external drive under /media or /mnt so a failed SD card does not take the backups with it."),
    field("INFLUXDB_URL", "InfluxDB address", SEC_STORAGE, kind="url", description="Where VesselStack reaches InfluxDB."),
    field("INFLUXDB_ORG", "InfluxDB organization", SEC_STORAGE, description="The InfluxDB organization name."),
    field("INFLUXDB_USERNAME", "InfluxDB admin username", SEC_STORAGE, description="The InfluxDB administrator account name."),
    field("INFLUXDB_RAW_BUCKET", "Raw data bucket", SEC_STORAGE, description="The InfluxDB bucket that holds incoming readings."),
    field("INFLUXDB_HISTORY_BUCKET", "History bucket", SEC_STORAGE, description="The InfluxDB bucket that holds the one-minute history averages."),
    field("INFLUXDB_HOME_ASSISTANT_BUCKET", "Home Assistant bucket", SEC_STORAGE, description="The InfluxDB bucket that holds data sent from Home Assistant."),
    field("INFLUXDB_AIS_BUCKET", "AIS bucket", SEC_STORAGE, description="The InfluxDB bucket that holds AIS targets."),
    field("INFLUXDB_PASSWORD", "InfluxDB admin password", SEC_STORAGE, kind="secret", read_only=True, description=INIT_ONLY_NOTE.format(service="InfluxDB")),
    field("INFLUXDB_TOKEN", "InfluxDB admin token", SEC_STORAGE, kind="secret", read_only=True, description=INIT_ONLY_NOTE.format(service="InfluxDB") + " Boat Chat and Grafana also use this token."),
    field("GRAFANA_ADMIN_PASSWORD", "Grafana admin password", SEC_STORAGE, kind="secret", read_only=True, description=INIT_ONLY_NOTE.format(service="Grafana")),
    # Advanced: Services & ports
    field("INFLUXDB_PORT", "InfluxDB port", SEC_SERVICES, kind="port", description="The port InfluxDB is published on."),
    field("INFLUXDB_CONTAINER_NAME", "InfluxDB container name", SEC_SERVICES, description="The Docker container name. Change only if it clashes with another container."),
    field("GRAFANA_PORT", "Grafana port", SEC_SERVICES, kind="port", description="The port Grafana is published on."),
    field("HEIMDALL_PORT", "Heimdall HTTP port", SEC_SERVICES, kind="port", description="The port Heimdall's start page is published on."),
    field("HEIMDALL_HTTPS_PORT", "Heimdall HTTPS port", SEC_SERVICES, kind="port", description="The secure port Heimdall is published on."),
    field("PROMETHEUS_PORT", "Prometheus port", SEC_SERVICES, kind="port", description="The port Prometheus is published on."),
    field("MQTT_USERNAME", "MQTT username", SEC_SERVICES, description="The account name used for the Mosquitto message broker."),
    field("MQTT_PASSWORD", "MQTT password", SEC_SERVICES, kind="secret", description="Applied to the Mosquitto password file on the next apply or start; update Home Assistant's MQTT integration to match."),
    field("MQTT_PORT", "MQTT port", SEC_SERVICES, kind="port", description="The port Mosquitto is published on."),
    field("NODE_EXPORTER_ENABLE", "Report Pi health numbers (Node Exporter)", SEC_SERVICES, kind="boolean", description="Runs the optional hardened host metrics exporter so Prometheus can record Pi temperature and disk use."),
    # Advanced: AIS receiver
    field("AIS_ENABLE", "Enable AIS-catcher", SEC_AIS, kind="boolean", description="Shows nearby ships from a USB AIS receiver. Turn on only when a receiver is plugged in."),
    field("AIS_IMAGE", "AIS-catcher image", SEC_AIS, description="The Docker image to run. Change only if you were told to."),
    field("AIS_DEVICE", "AIS receiver device", SEC_AIS, kind="path", description="Where the USB AIS receiver appears on this Pi. The default, /dev/bus/usb, works for most single receivers."),
    field("AIS_CATCHER_ARGS", "AIS-catcher options", SEC_AIS, description="Extra command-line options for AIS-catcher. Leave empty unless you know you need them."),
    field("AIS_WEB_PORT", "AIS web page port", SEC_AIS, kind="port", description="The port of the AIS-catcher web page."),
    field("AIS_TCP_PORT", "AIS data output port", SEC_AIS, kind="port", description="The TCP port AIS-catcher sends decoded messages on."),
    # Advanced: Home Assistant
    field("HOME_ASSISTANT_URL", "Home Assistant address", SEC_HA, kind="url", description="Where VesselStack reaches Home Assistant."),
    field("HOME_ASSISTANT_TOKEN", "Home Assistant token", SEC_HA, kind="secret", description="Write-only. Use a token from a non-admin user and revoke it in Home Assistant if it leaks."),
    # Advanced: Installation (read-only)
    field("VESSELSTACK_USER", "Service account", SEC_INSTALL, read_only=True, description="The account VesselStack services run as. Change it from the recovery shell with a reviewed migration."),
    field("VESSELSTACK_ROOT", "Application folder", SEC_INSTALL, kind="path", read_only=True, description="Where VesselStack is installed. Change it from the recovery shell with a reviewed migration."),
    field("VESSELSTACK_DATA", "Data folder", SEC_INSTALL, kind="path", read_only=True, description="Where your data lives. Move it only with services stopped and a verified backup."),
    field("VESSELSTACK_DISABLED_COMPONENTS", "Disabled components", SEC_INSTALL, read_only=True, description="A read-only list. Use Disable and Enable on the Home tab to change which core services run."),
]
FIELD_BY_KEY = {item["key"]: item for item in FIELDS}

COMPOSE_COMPONENTS = {
    "homeassistant": "homeassistant",
    "influxdb": "influxdb",
    "grafana": "grafana",
    "prometheus": "prometheus",
    "mosquitto": "mosquitto",
    "heimdall": "heimdall",
    "signalk": "signalk",
    "ais-catcher": "ais-catcher",
    "node-exporter": "node-exporter",
}
CORE_COMPONENTS = ("homeassistant", "influxdb", "grafana", "prometheus", "mosquitto", "heimdall")
# Display name, plain description and the Settings section that configures it.
COMPONENT_INFO = {
    "homeassistant": ("Home Assistant", "Dashboards and automations for the boat.", SEC_HA),
    "influxdb": ("InfluxDB", "Stores the boat's data history.", SEC_STORAGE),
    "grafana": ("Grafana", "Charts of your history: trips, batteries, temperatures.", SEC_SERVICES),
    "prometheus": ("Prometheus", "Collects Pi and service health numbers.", SEC_STORAGE),
    "mosquitto": ("Mosquitto", "Passes messages between devices (MQTT).", SEC_SERVICES),
    "heimdall": ("Heimdall", "A start page with links to every boat web page.", SEC_SERVICES),
    "signalk": ("Signal K", "Reads boat data and shares it with everything else.", SEC_SIGNALK),
    "ais-catcher": ("AIS-catcher", "Shows nearby ships from your AIS receiver.", SEC_AIS),
    "node-exporter": ("Node Exporter", "Reports Pi health (temperature, disk) for Prometheus.", SEC_SERVICES),
    "boat-chat": ("Boat Chat", "Ask questions about your boat in plain language.", SEC_CHAT),
    "telegram": ("Telegram alerts", "Sends boat alerts to your phone through Telegram.", SEC_CHAT),
    "telemetry-indexer": ("Telemetry memory", "Keeps Boat Chat's memory of your boat data up to date.", SEC_CHAT),
    "telemetry-sample": ("Telemetry sampler", "Records regular samples of your boat data for Boat Chat.", SEC_CHAT),
    "displays-bridge": ("Displays bridge", "Feeds data to the boat's instrument displays.", SEC_DISPLAYS),
    "signalk-native": ("Signal K (on this Pi)", "Signal K installed directly on this Pi instead of in a container.", SEC_SIGNALK),
    "socketcan": ("SocketCAN", "Connects to the NMEA 2000 network through a CAN adapter.", SEC_SIGNALK),
    "firewall": ("Network firewall", "Blocks unwanted connections on the marina-facing network.", SEC_NETWORK),
    "control-panel": ("Control Panel", "This page. Restart it from the Maintenance tab.", ""),
}
RUNNING_STATES = {"running", "active", "activating", "reloading", "restarting"}
LIFECYCLE_LOCK = threading.Lock()


def disabled_components(settings: dict[str, str] | None = None) -> set[str]:
    raw = (settings or parse_env(VESSEL_ENV)).get("VESSELSTACK_DISABLED_COMPONENTS", "")
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if len(values) != len(set(values)) or any(item not in CORE_COMPONENTS for item in values):
        raise ValueError("Invalid disabled component configuration")
    return set(values)
SYSTEMD_COMPONENTS = {
    "boat-chat": "vesselstack-chat.service",
    "telegram": "vesselstack-chat-telegram.service",
    "telemetry-indexer": "vesselstack-telemetry-indexer.timer",
    "telemetry-sample": "vesselstack-telemetry-sample.timer",
    "displays-bridge": "vesselstack-displays-bridge.service",
    "signalk-native": "vesselstack-signalk.service",
    "socketcan": "vesselstack-socketcan.service",
    "firewall": "vesselstack-firewall.service",
    "control-panel": "vesselstack-control-panel.service",
}


def parse_env(path: Path) -> dict[str, str]:
    try:
        text = path.read_text()
    except OSError:
        return {}
    return parse_env_text(text)


def parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    lines = text.splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            continue
        try:
            parsed = shlex.split(raw, posix=True)
            values[key] = parsed[0] if parsed else ""
        except ValueError:
            values[key] = raw.strip().strip("'\"")
    return values


def write_env(path: Path, values: dict[str, str], preferred_order: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = [key for key in preferred_order if key in values]
    ordered.extend(sorted(key for key in values if key not in ordered))
    content = "".join(f"{key}={shlex.quote(str(values[key]))}\n" for key in ordered)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


# Allowed range for each plain-number setting: (minimum, maximum, whole number only).
# The minimum is exclusive for sizes (zero metres is not a boat) and inclusive otherwise.
NUMBER_RULES: dict[str, tuple[float, float, bool, bool]] = {
    "BOAT_LOA_M": (0, 500, False, True),
    "BOAT_BEAM_M": (0, 100, False, True),
    "SOCKETCAN_BITRATE": (10000, 1000000, True, False),
}
NUMBER_TEXT = re.compile(r"[0-9]+(\.[0-9]+)?")


def validate_number(spec: dict[str, Any], text: str) -> None:
    label = spec["label"]
    if not NUMBER_TEXT.fullmatch(text):
        raise ValueError(f"{label} must be a plain number such as 12.5 (no units, signs or exponents)")
    minimum, maximum, whole, exclusive = NUMBER_RULES.get(spec["key"], (0, 1e12, False, True))
    if whole and "." in text:
        raise ValueError(f"{label} must be a whole number")
    value = float(text)
    low_ok = value > minimum if exclusive else value >= minimum
    if not low_ok or value > maximum or value != value:
        shown_min = f"more than {minimum:g}" if exclusive else f"at least {minimum:g}"
        raise ValueError(f"{label} must be {shown_min} and at most {maximum:g}")


def timezone_exists(name: str) -> bool:
    try:
        known = zoneinfo.available_timezones()
    except Exception:
        return True
    # Without a timezone database nothing can be checked; do not block saving.
    return not known or name in known or name == "UTC"


class FieldErrors(ValueError):
    """Validation failed for one or more settings; ``fields`` maps key to message."""

    def __init__(self, fields: dict[str, str]) -> None:
        self.fields = dict(fields)
        first = next(iter(self.fields.values()))
        super().__init__(first if len(self.fields) == 1 else f"{len(self.fields)} settings need fixing. {first}")


MQTT_ACCOUNT_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
DEFAULT_BRIDGE_MQTT_USERNAME = "vesselstack-bridge"


def validate_value(spec: dict[str, Any], value: Any) -> str:
    text = str(value).strip()
    kind = spec["kind"]
    if "\x00" in text or "\n" in text or "\r" in text:
        raise ValueError(f"{spec['label']} contains unsupported characters")
    if kind == "boolean":
        if text.lower() not in {"true", "false"}:
            raise ValueError(f"{spec['label']} must be true or false")
        return text.lower()
    if kind == "select" and text not in spec["choices"]:
        raise ValueError(f"Invalid value for {spec['label']}")
    if spec["key"] == "VESSELSTACK_DISABLED_COMPONENTS":
        values = [item.strip() for item in text.split(",") if item.strip()]
        if len(values) != len(set(values)) or any(item not in CORE_COMPONENTS for item in values):
            raise ValueError("Disabled components must be unique allowed core component IDs")
        return ",".join(values)
    if kind == "port" and text:
        if not re.fullmatch(r"[0-9]+", text):
            raise ValueError(f"{spec['label']} must be numeric")
        if not 1 <= int(text) <= 65535:
            raise ValueError(f"{spec['label']} must be an integer from 1 to 65535")
    if kind == "number" and text:
        validate_number(spec, text)
    if spec["key"] == "BOAT_TIMEZONE" and text and not timezone_exists(text):
        raise ValueError("Timezone not recognised. Use a name like Europe/Lisbon")
    if spec["key"] in {"CONTROL_PANEL_HOST", "BOAT_CHAT_HOST", "VESSELSTACK_DATA_BIND_ADDRESS", "VESSELSTACK_WEB_BIND_ADDRESS", "HOMEASSISTANT_BIND_ADDRESS"} and text:
        try:
            address = ipaddress.ip_address(text)
        except ValueError as exc:
            raise ValueError(f"{spec['label']} must be an IPv4 address") from exc
        if address.version != 4:
            raise ValueError(f"{spec['label']} must be an IPv4 address")
    # Mirrors the installer: account names go into the Mosquitto password and ACL
    # files, and the SignalK and Home Assistant tokens into a systemd EnvironmentFile line.
    if spec["key"] in {"MQTT_USERNAME", "DISPLAYS_BRIDGE_MQTT_USERNAME"} and text:
        if not MQTT_ACCOUNT_NAME.fullmatch(text):
            raise ValueError(f"{spec['label']} may contain 1-64 letters, numbers, dot, underscore, and hyphen")
    if spec["key"] in {"DISPLAYS_SIGNALK_TOKEN", "DISPLAYS_HA_TOKEN"} and any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise ValueError(f"{spec['label']} must not contain control characters")
    if spec["key"] in {"VESSELSTACK_FIREWALL_ALLOW_TCP", "VESSELSTACK_FIREWALL_ALLOW_UDP"} and text:
        if not re.fullmatch(r"[0-9]{1,5}(:[0-9]{1,5})?(,[0-9]{1,5}(:[0-9]{1,5})?)*", text):
            raise ValueError(f"{spec['label']} must be comma-separated ports or LOW:HIGH ranges")
    if spec["key"] == "PROMETHEUS_RETENTION" and text and not re.fullmatch(r"[1-9][0-9]{0,4}[hdw]", text):
        raise ValueError("Use a duration such as 720h, 30d or 8w.")
    if spec["key"] == "INFLUXDB_RAW_RETENTION" and text and not re.fullmatch(r"0|[1-9][0-9]{0,4}[hdw]", text):
        raise ValueError(f"{spec['label']} must be 0, or a duration such as 720h, 30d or 8w")
    if kind == "path" and text and not text.startswith("/"):
        raise ValueError(f"{spec['label']} must be an absolute path")
    if kind == "url" and text:
        parsed = urllib.parse.urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"{spec['label']} must be an HTTP(S) URL")
    return text


def chat_override_path(vessel: dict[str, str] | None = None) -> Path | None:
    """Boat Chat Settings-page override file (BOAT_CHAT_ENV), if locatable."""
    explicit = os.environ.get("BOAT_CHAT_ENV")
    if explicit:
        return Path(explicit)
    data = (vessel if vessel is not None else parse_env(VESSEL_ENV)).get("VESSELSTACK_DATA", "")
    return Path(data) / "boat-chat" / "boat-chat.env" if data else None


def chat_overrides(vessel: dict[str, str] | None = None) -> set[str]:
    path = chat_override_path(vessel)
    if not path:
        return set()
    try:
        return set(parse_env_text(_read_nofollow(path)[0]))
    except OSError:
        return set()


def _read_nofollow(path: Path) -> tuple[str, os.stat_result]:
    """Read a regular file without following a final symlink; return text and fstat."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"{path} is not a regular file")
        with os.fdopen(fd, "r", closefd=False) as handle:
            return handle.read(), info
    finally:
        os.close(fd)


def replace_in_service_dir(path: Path, content: str) -> None:
    """Atomically replace a file in a directory the service user can write.

    The panel may run as root while the directory belongs to the service user, so
    nothing here may follow a path the service user could have swapped for a
    symlink: refuse symlinks, use an O_EXCL random temporary name, and apply
    mode/ownership through the open descriptor rather than by path.
    """
    if stat.S_ISLNK(os.lstat(path).st_mode):
        raise OSError(f"Refusing to rewrite symlink {path}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"{path} is not a regular file")
    temp_fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        try:
            with os.fdopen(temp_fd, "w", closefd=False) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(temp_fd)
            os.fchmod(temp_fd, stat.S_IMODE(info.st_mode))
            try:
                os.fchown(temp_fd, info.st_uid, info.st_gid)
            except OSError:
                pass  # not running as root; ownership already matches the caller
        finally:
            os.close(temp_fd)
        if stat.S_ISLNK(os.lstat(temporary).st_mode):
            raise OSError(f"Refusing symlinked temporary file {temporary}")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def clear_chat_overrides(keys: set[str], vessel: dict[str, str]) -> None:
    """Drop keys saved here from the Settings-page override file.

    The override file wins over config/boat-chat.env, so without this a value
    saved in the control panel would be silently shadowed by a stale UI value.
    The file lives in the service-user-writable state directory, so it is never
    read or written through a symlink.
    """
    path = chat_override_path(vessel)
    if not path:
        return
    try:
        text, _ = _read_nofollow(path)
    except OSError:
        return  # absent, a symlink, or not a regular file: leave it alone
    stale = {key for key in keys if key in parse_env_text(text)}
    if not stale:
        return
    kept = []
    for line in text.splitlines():
        name = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and name in stale:
            continue
        kept.append(line)
    replace_in_service_dir(path, "\n".join(kept) + "\n")


def public_configuration() -> dict[str, Any]:
    vessel = parse_env(VESSEL_ENV)
    chat = parse_env(CHAT_ENV)
    panel = parse_env(PANEL_ENV)
    result: dict[str, Any] = {}
    for spec in FIELDS:
        source = chat if spec["source"] == "chat" else vessel
        value = source.get(spec["key"], "")
        if spec["key"] in {"CONTROL_PANEL_HOST", "CONTROL_PANEL_PORT"}:
            value = panel.get(spec["key"], value)
        result[spec["key"]] = {"configured": bool(value)} if spec["kind"] == "secret" else value
    overridden = chat_overrides(vessel)
    fields = []
    for spec in FIELDS:
        spec = dict(spec)
        if spec["source"] == "chat" and spec["key"] in overridden:
            spec["description"] = (spec["description"] + " " if spec["description"] else "") + (
                "Overridden by the Boat Chat Settings page; the value shown here is not in effect. "
                "Saving this field replaces the override."
            )
        fields.append(spec)
    return {"sections": SECTIONS, "fields": fields, "values": result, "overridden": sorted(overridden & {s["key"] for s in FIELDS if s["source"] == "chat"})}


def take_lifecycle_slot():
    if not LIFECYCLE_LOCK.acquire(blocking=False):
        raise ValueError("Another operation is already running")
    try:
        return acquire_slot(ROOT)
    except Exception:
        LIFECYCLE_LOCK.release()
        raise


def release_lifecycle_slot(slot_fd) -> None:
    try:
        release_slot(slot_fd)
    finally:
        LIFECYCLE_LOCK.release()


def update_configuration(updates: dict[str, Any]) -> str:
    slot_fd = take_lifecycle_slot()
    try:
        return _update_configuration(updates)
    finally:
        release_lifecycle_slot(slot_fd)


def _update_configuration(updates: dict[str, Any]) -> str:
    saved_chat_keys: set[str] = set()
    vessel = parse_env(VESSEL_ENV)
    chat = parse_env(CHAT_ENV)
    panel = parse_env(PANEL_ENV)
    problems: dict[str, str] = {}
    for key, raw in updates.items():
        spec = FIELD_BY_KEY.get(key)
        if not spec:
            raise ValueError(f"Unsupported setting: {key}")
        if spec["read_only"] and spec["kind"] == "secret" and raw == "":
            continue  # a blank secret means "keep the stored value"
        if spec["read_only"]:
            raise ValueError(f"{spec['label']} is read-only in the control panel")
        if spec["kind"] == "secret" and str(raw or "") == "":
            if raw is not None:
                continue
            value = ""
        else:
            try:
                value = validate_value(spec, raw)
            except ValueError as exc:
                problems[key] = str(exc)
                continue
        target = chat if spec["source"] == "chat" else vessel
        target[key] = value
        if spec["source"] == "chat":
            saved_chat_keys.add(key)
        if key in {"CONTROL_PANEL_HOST", "CONTROL_PANEL_PORT"}:
            panel[key] = value
    if problems:
        raise FieldErrors(problems)
    if vessel.get("DISPLAYS_ENABLE", "false") == "true":
        bridge_user = vessel.get("DISPLAYS_BRIDGE_MQTT_USERNAME") or DEFAULT_BRIDGE_MQTT_USERNAME
        if bridge_user == vessel.get("MQTT_USERNAME", ""):
            raise FieldErrors({"DISPLAYS_BRIDGE_MQTT_USERNAME": "Displays bridge MQTT username must differ from the MQTT username"})
    paths = [VESSEL_ENV, CHAT_ENV, PANEL_ENV]
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{time.time_ns() % 1_000_000_000:09d}"
    backup = CONFIG_BACKUP_ROOT / stamp
    backup.mkdir(parents=True, mode=0o700)
    originals: dict[Path, Path | None] = {}
    for path in paths:
        snapshot = backup / path.name
        if path.is_file():
            shutil.copy2(path, snapshot)
            os.chmod(snapshot, 0o600)
            originals[path] = snapshot
        else:
            originals[path] = None
    try:
        write_env(VESSEL_ENV, vessel, [item["key"] for item in FIELDS if item["source"] == "vessel"])
        write_env(CHAT_ENV, chat, [item["key"] for item in FIELDS if item["source"] == "chat"])
        uid = vessel.get("VESSELSTACK_UID", "")
        gid = vessel.get("VESSELSTACK_GID", "")
        if uid.isdigit() and gid.isdigit():
            os.chown(CHAT_ENV, int(uid), int(gid))
        if panel:
            write_env(PANEL_ENV, panel, ["CONTROL_PANEL_HOST", "CONTROL_PANEL_PORT", "CONTROL_PANEL_TOKEN"])
    except Exception:
        for path, snapshot in originals.items():
            if snapshot is None:
                path.unlink(missing_ok=True)
            else:
                shutil.copy2(snapshot, path)
        raise
    clear_chat_overrides(saved_chat_keys, vessel)
    return str(backup)


def compose_base() -> list[str]:
    return [
        "docker", "compose", "--profile", "signalk", "--profile", "ais", "--profile", "metrics",
        "--env-file", str(VESSEL_ENV), "-f", str(ROOT / "compose.yml"),
    ]


def component_command(component: str, verb: str) -> list[str]:
    if verb in {"add", "remove"}:
        if component not in CORE_COMPONENTS:
            raise ValueError("Unknown core component")
        return [str(CTL), "component", verb, component]
    if verb not in {"start", "stop", "restart"}:
        raise ValueError("Unsupported component action")
    if component in COMPOSE_COMPONENTS:
        settings = parse_env(VESSEL_ENV)
        if component in CORE_COMPONENTS and component in disabled_components(settings) and verb in {"start", "restart"}:
            raise ValueError("Component is disabled by configuration")
        if component == "signalk" and settings.get("SIGNALK_MODE", "existing") != "docker":
            raise ValueError("Docker SignalK is disabled by configuration")
        if component == "ais-catcher" and settings.get("AIS_ENABLE", "false") != "true":
            raise ValueError("AIS-catcher is disabled by configuration")
        if component == "node-exporter" and settings.get("NODE_EXPORTER_ENABLE", "false") != "true" and verb in {"start", "restart"}:
            raise ValueError("node-exporter is disabled by configuration")
        service = COMPOSE_COMPONENTS[component]
        if verb == "start":
            return compose_base() + ["up", "-d", service]
        return compose_base() + [verb, service]
    if component in SYSTEMD_COMPONENTS:
        if component == "control-panel":
            raise ValueError("Use systemctl from the recovery shell to restart the control panel")
        settings = parse_env(VESSEL_ENV)
        enabled = {
            "telegram": settings.get("TELEGRAM_ENABLE", "false") == "true",
            "telemetry-indexer": settings.get("TELEMETRY_INDEXER_ENABLE", "true") == "true",
            "telemetry-sample": settings.get("TELEMETRY_INDEXER_ENABLE", "true") == "true",
            "signalk-native": settings.get("SIGNALK_MODE") == "native",
            "socketcan": settings.get("SOCKETCAN_ENABLE", "false") == "true",
            "firewall": settings.get("VESSELSTACK_FIREWALL_ENABLE", "false") == "true",
            "displays-bridge": settings.get("DISPLAYS_ENABLE", "false") == "true",
        }
        if component in enabled and not enabled[component]:
            raise ValueError(f"{component} is disabled by configuration")
        return ["systemctl", verb, SYSTEMD_COMPONENTS[component]]
    raise ValueError("Unknown component")


def verify_release_path(path: Path) -> None:
    stat = path.lstat()
    if stat.st_uid not in {0, os.geteuid()} or stat.st_mode & 0o022:
        raise ValueError("Reviewed release paths must be root/panel-owner controlled and not group/world writable")


def verify_release_tree(release: Path) -> None:
    """Reject any symlink, foreign owner or group/world-writable entry in the release.

    install.sh runs as root and executes/copies scripts/, boat-chat/, templates/
    and systemd/, so the whole tree must be tamper-resistant, not just its top.
    """
    for directory, subdirectories, filenames in os.walk(release, followlinks=False):
        for name in [*subdirectories, *filenames]:
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError("Reviewed release must not contain symbolic links")
            verify_release_path(path)


def action_command(action: str, parameters: dict[str, Any]) -> list[str]:
    if action in {"preflight", "apply", "install"}:
        if not INSTALLER.is_file():
            raise ValueError(f"Bundled installer not found at {INSTALLER}")
        command = [str(INSTALLER), "--config", str(VESSEL_ENV)]
        if action == "preflight":
            command.append("--dry-run")
        elif action == "install":
            command.append("--start")
        return command
    if action in {"start", "stop", "restart", "backup"}:
        return [str(CTL), action]
    if action == "bootstrap-history":
        return [str(ROOT / "installer/scripts/bootstrap-influx.sh"), str(VESSEL_ENV)]
    if action == "update":
        source = Path(str(parameters.get("source_directory", ""))).expanduser()
        if not source.is_absolute():
            raise ValueError("Update source must be an absolute reviewed VesselStack release directory")
        try:
            reviewed_root = REVIEWED_RELEASE_ROOT.resolve(strict=True)
            resolved = source.resolve(strict=True)
            resolved.relative_to(reviewed_root)
        except (FileNotFoundError, ValueError) as exc:
            raise ValueError(f"Update source must be inside {REVIEWED_RELEASE_ROOT}") from exc
        if source.is_symlink() or not (resolved / "install.sh").is_file() or not (resolved / "VERSION").is_file():
            raise ValueError("Reviewed release must contain regular install.sh and VERSION files")
        for candidate in (reviewed_root, resolved):
            verify_release_path(candidate)
        verify_release_tree(resolved)
        return [str(CTL), "update", str(resolved)]
    raise ValueError("Unsupported action")


def command_output(command: list[str], timeout: int = 8) -> tuple[int, str]:
    try:
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False)
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def describe_component(name: str, kind: str, enabled: bool, state: str) -> dict[str, Any]:
    """One component as the UI shows it: real name, plain status, no contradictions."""
    label, description, section = COMPONENT_INFO.get(name, (name.replace("-", " ").title(), "", ""))
    raw = str(state or "unknown").strip().lower()
    running = raw.split(" ")[0] in RUNNING_STATES
    if not enabled:
        status = "disabled"
    elif running:
        status = "running"
    elif raw == "unknown":
        status = "unknown"
    else:
        status = "stopped"
    item: dict[str, Any] = {
        "id": name, "label": label, "description": description, "section": section,
        "kind": kind, "enabled": enabled, "state": state, "status": status,
        "still_running": status == "disabled" and running,
        "manageable": name in CORE_COMPONENTS,
    }
    if status == "stopped" and raw not in {"stopped", "inactive", "dead", "exited"}:
        item["detail"] = raw
    return item


def component_status() -> list[dict[str, Any]]:
    vessel = parse_env(VESSEL_ENV)
    disabled = disabled_components(vessel)
    compose_state: dict[str, str] = {}
    code, output = command_output(compose_base() + ["ps", "--all", "--format", "json"])
    compose_parse_failed = code != 0
    rows: list[Any] = []
    if code == 0:
        if not output:
            compose_parse_failed = False
        else:
            try:
                decoded = json.loads(output)
                rows = decoded if isinstance(decoded, list) else [decoded]
                compose_parse_failed = False
            except json.JSONDecodeError:
                compose_parse_failed = False
                for line in output.splitlines():
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        compose_parse_failed = True
        for row in rows:
            if not isinstance(row, dict):
                compose_parse_failed = True
                continue
            service = str(row.get("Service") or row.get("service") or "")
            if not service:
                compose_parse_failed = True
                continue
            compose_state[service] = str(row.get("State") or row.get("state") or "unknown")
    components = []
    for name, service in COMPOSE_COMPONENTS.items():
        enabled = name not in disabled
        if name == "signalk":
            enabled = vessel.get("SIGNALK_MODE", "existing") == "docker"
        elif name == "ais-catcher":
            enabled = vessel.get("AIS_ENABLE", "false") == "true"
        elif name == "node-exporter":
            enabled = vessel.get("NODE_EXPORTER_ENABLE", "false") == "true"
        state = compose_state.get(service, "unknown" if compose_parse_failed else "stopped")
        components.append(describe_component(name, "container", enabled, state))
    for name, unit in SYSTEMD_COMPONENTS.items():
        code, output = command_output(["systemctl", "is-active", unit], timeout=3)
        enabled = True
        if name == "signalk-native":
            enabled = vessel.get("SIGNALK_MODE") == "native"
        elif name == "telegram":
            enabled = vessel.get("TELEGRAM_ENABLE", "false") == "true"
        elif name == "telemetry-indexer":
            enabled = vessel.get("TELEMETRY_INDEXER_ENABLE", "true") == "true"
        elif name == "telemetry-sample":
            enabled = vessel.get("TELEMETRY_INDEXER_ENABLE", "true") == "true"
        elif name == "socketcan":
            enabled = vessel.get("SOCKETCAN_ENABLE", "false") == "true"
        elif name == "firewall":
            enabled = vessel.get("VESSELSTACK_FIREWALL_ENABLE", "false") == "true"
        elif name == "displays-bridge":
            enabled = vessel.get("DISPLAYS_ENABLE", "false") == "true"
        components.append(describe_component(name, "systemd", enabled, output or ("active" if code == 0 else "inactive")))
    return components


DEFAULT_BACKUP_DIR = "/var/backups/vesselstack"
BACKUP_ARCHIVE = re.compile(r"vesselstack-[0-9A-Za-z._-]+\.tar\.gz")


def backup_status(vessel: dict[str, str] | None = None) -> dict[str, Any]:
    """Read-only view of the backup destination, derived the way vesselstackctl backup does.

    The destination is VESSELSTACK_BACKUP (or the default). A /media or /mnt destination must
    sit on a mounted filesystem, otherwise vesselstackctl refuses to back up. The newest archive
    that has its .sha256 manifest next to it is reported by modification time; nothing is hashed
    or written. Any problem reading the folder yields "unknown" and never an error.
    """
    result: dict[str, Any] = {"path": "", "mount": "unknown", "writable": None, "last_backup_at": None, "state": "unknown", "message": ""}
    try:
        settings = vessel if vessel is not None else parse_env(VESSEL_ENV)
        destination = os.path.realpath(settings.get("VESSELSTACK_BACKUP") or os.environ.get("VESSELSTACK_BACKUP") or DEFAULT_BACKUP_DIR)
        result["path"] = destination
        parts = Path(destination).parts
        removable = len(parts) >= 3 and parts[1] in {"media", "mnt"}
        if removable:
            mount_dir = os.path.join("/", parts[1], parts[2])
            result["mount"] = "mounted" if os.path.ismount(mount_dir) else "not_mounted"
        else:
            result["mount"] = "system_disk"
        if result["mount"] == "not_mounted":
            result["message"] = "The backup drive is not mounted, so backups cannot be saved or checked."
            return result
        if not os.path.isdir(destination):
            result["state"] = "none"
            result["message"] = "No backup has been made yet."
            return result
        result["writable"] = os.access(destination, os.W_OK | os.X_OK)
        newest = 0
        with os.scandir(destination) as entries:
            for entry in entries:
                if not BACKUP_ARCHIVE.fullmatch(entry.name) or not entry.is_file(follow_symlinks=False):
                    continue
                if not os.path.isfile(os.path.join(destination, entry.name + ".sha256")):
                    continue
                newest = max(newest, int(entry.stat(follow_symlinks=False).st_mtime))
        if newest:
            result["last_backup_at"] = newest
            result["state"] = "ok" if result["writable"] else "read_only"
        else:
            result["state"] = "none"
            result["message"] = "No backup has been made yet."
    except OSError:
        result.update({"state": "unknown", "last_backup_at": None, "message": "The backup folder could not be read."})
    except Exception:
        result.update({"state": "unknown", "last_backup_at": None, "message": "Backup status is not available."})
    return result


ERROR_LINE = re.compile(r"^(?:\[[^\]]*\]\s*)?(?:ERROR|Error|error|FATAL|Refusing[^:]*|Backup[^:]*failed)[:\s]+(.*)$")


def summarize_operation(operation: dict[str, Any]) -> dict[str, Any]:
    """Plain facts about a finished operation, read from its output only."""
    lines = [str(line).strip() for line in operation.get("output") or [] if str(line).strip()]
    summary: dict[str, Any] = {}
    if operation.get("state") == "succeeded" and operation.get("label") == "backup":
        for line in reversed(lines):
            if line.startswith("/") and line.endswith(".tar.gz"):
                summary["saved_path"] = line
                break
    if operation.get("state") == "failed" and lines:
        reason = lines[-1]
        for line in reversed(lines):
            found = ERROR_LINE.match(line)
            if found:
                reason = found.group(1).strip() or line
                break
        summary["reason"] = reason[:400]
    return summary


def with_summary(operation: dict[str, Any]) -> dict[str, Any]:
    if operation.get("state") in {"succeeded", "failed"}:
        return {**operation, "summary": summarize_operation(operation)}
    return operation


def restart_needed() -> bool:
    """True when a newer Control Panel is installed than the one now running."""
    try:
        installed = installed_version(ROOT)
        running = str(RUNNING_IDENTITY.get("running_version") or "")
        return bool(running) and running != "unknown" and installed != running
    except Exception:
        return False


class OperationStore:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current: dict[str, Any] | None = None
        self.counter = 0

    def start(self, label: str, command_or_factory: list[str] | Any) -> dict[str, Any]:
        slot_fd = take_lifecycle_slot()
        operation = None
        try:
            command = command_or_factory() if callable(command_or_factory) else command_or_factory
            with self.lock:
                if self.current and self.current["state"] == "running":
                    raise ValueError("Another operation is already running")
                self.counter += 1
                operation = {
                    "id": self.counter, "label": label,
                    "command": command[0:1] + ["[arguments hidden]"],
                    "state": "running", "started_at": int(time.time()),
                    "finished_at": None, "exit_code": None, "output": deque(maxlen=400),
                }
                self.current = operation
            threading.Thread(target=self._run, args=(operation, command, slot_fd), daemon=True).start()
            return self.public()
        except Exception:
            if operation is not None:
                with self.lock:
                    operation["state"] = "failed"
                    operation["exit_code"] = 1
                    operation["finished_at"] = int(time.time())
                    operation["output"].append("Operation failed")
            release_lifecycle_slot(slot_fd)
            raise

    def _run(self, operation: dict[str, Any], command: list[str], slot_fd) -> None:
        environment = os.environ.copy()
        environment["VESSELSTACK_SKIP_PANEL_RESTART"] = "true"
        exit_code = 1
        succeeded = False
        try:
            process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment)
            assert process.stdout is not None
            for line in process.stdout:
                with self.lock:
                    operation["output"].append(line.rstrip())
            exit_code = process.wait()
            succeeded = exit_code == 0
            with self.lock:
                if succeeded and operation["label"] in {"apply", "install", "update"} and restart_needed():
                    operation["output"].append("A newer Control Panel is installed than the one running. Use Restart Control Panel on the Maintenance tab.")
        except Exception as exc:
            with self.lock:
                operation["output"].append("Operation failed")
        finally:
            release_lifecycle_slot(slot_fd)
            with self.lock:
                operation["exit_code"] = exit_code
                operation["state"] = "succeeded" if succeeded else "failed"
                operation["finished_at"] = int(time.time())

    def public(self) -> dict[str, Any]:
        with self.lock:
            if not self.current:
                return {"state": "idle", "output": []}
            return {**self.current, "output": list(self.current["output"])}


OPERATIONS = OperationStore()


def panel_settings() -> dict[str, str]:
    settings = parse_env(PANEL_ENV)
    settings.update({key: value for key, value in os.environ.items() if key.startswith("CONTROL_PANEL_")})
    return settings


def normalize_token(text: str) -> str:
    """Accept a pasted ``CONTROL_PANEL_TOKEN=...`` line as well as the bare token."""
    value = text.strip()
    value = re.sub(r"^(export\s+)?CONTROL_PANEL_TOKEN\s*=\s*", "", value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1].strip()
    return value


class ControlPanelHandler(BaseHTTPRequestHandler):
    server_version = f"VesselStackControlPanel/{os.environ.get('VESSELSTACK_VERSION', 'development')}"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args), flush=True)

    def send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authenticated(self) -> bool:
        expected = panel_settings().get("CONTROL_PANEL_TOKEN", "").strip()
        supplied = normalize_token(self.headers.get("X-VesselStack-Token") or "")
        return bool(expected) and hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))

    def require_auth(self) -> bool:
        if self.authenticated():
            return True
        self.send_json(401, {"error": "A valid X-VesselStack-Token header is required"})
        return False

    def read_payload(self) -> dict[str, Any]:
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > 1024 * 1024:
            raise ValueError("Request is too large")
        try:
            raw = self.rfile.read(length).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("request body must be valid UTF-8") from exc
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        return payload

    def static_target(self, path: str) -> Path | None:
        if path == "/":
            return STATIC_ROOT / "index.html"
        candidate = (STATIC_ROOT / urllib.parse.unquote(path.lstrip("/"))).resolve()
        try:
            candidate.relative_to(STATIC_ROOT.resolve())
        except ValueError:
            return None
        return candidate

    def send_static(self, target: Path | None) -> None:
        if target is None or not target.is_file():
            self.send_error(404)
            return
        content_type = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml"}.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            self.send_json(200, {"ok": True, "version": RUNNING_IDENTITY["running_version"], "boot_id": RUNNING_IDENTITY["boot_id"]})
            return
        if path.startswith("/api/"):
            if not self.require_auth():
                return
            if path == "/api/config":
                self.send_json(200, public_configuration())
            elif path == "/api/status":
                persisted = public_operation(ROOT)
                memory = OPERATIONS.public()
                operation = memory if memory.get("state") == "running" else max((persisted, memory), key=lambda item: int(item.get("finished_at") or item.get("started_at") or 0))
                vessel = parse_env(VESSEL_ENV)
                self.send_json(200, {"components": component_status(), "operation": with_summary(operation), "installed_version": installed_version(ROOT), "boat_name": vessel.get("BOAT_NAME", ""), "backup": backup_status(vessel), "restart_needed": restart_needed(), **RUNNING_IDENTITY})
            elif path == "/api/operation":
                persisted = public_operation(ROOT)
                memory = OPERATIONS.public()
                self.send_json(200, with_summary(memory if memory.get("state") == "running" else max((persisted, memory), key=lambda item: int(item.get("finished_at") or item.get("started_at") or 0))))
            elif path == "/api/releases":
                channel = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("channel", ["stable"])[0]
                if channel not in {"stable", "lab"}:
                    self.send_json(400, {"error": "channel must be stable or lab"})
                    return
                try:
                    catalog = release_catalog(channel, ROOT, running_version=RUNNING_IDENTITY["running_version"])
                except Exception:
                    self.send_json(502, {"error": "Release catalog unavailable; please retry."})
                    return
                self.send_json(200, catalog)
            else:
                self.send_json(404, {"error": "Not found"})
            return
        self.send_static(self.static_target(path))

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        origin = self.headers.get("Origin")
        if origin:
            try:
                parsed = urllib.parse.urlsplit(origin)
                expected = urllib.parse.urlsplit(f"//{self.headers.get('Host', '')}")
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.hostname.lower() != (expected.hostname or "").lower() or parsed.port != expected.port:
                    raise ValueError
            except (ValueError, UnicodeError):
                self.send_json(403, {"error": "request origin is not allowed"})
                return
        if not self.require_auth():
            return
        try:
            payload = self.read_payload()
            if path == "/api/config":
                updates = payload.get("settings", payload)
                if not isinstance(updates, dict):
                    raise ValueError("settings object required")
                backup = update_configuration(updates)
                self.send_json(200, {"ok": True, "backup": backup, "configuration": public_configuration(), "restart_note": "Apply configuration to render service changes. Restart the panel from the recovery shell after changing its listener."})
                return
            if path == "/api/action":
                action = str(payload.get("action", ""))
                if action == "restart-panel":
                    slot = take_lifecycle_slot()
                    try:
                        result = start_detached("restart-panel", ROOT, RUNNING_IDENTITY)
                    finally:
                        release_lifecycle_slot(slot)
                    self.send_json(202, result); return
                if action == "update-release":
                    channel = str(payload.get("channel", "")); release_id = payload.get("release_id")
                    if channel not in {"stable", "lab"} or not isinstance(release_id, int) or isinstance(release_id, bool):
                        raise ValueError("release_id and channel are required")
                    if OPERATIONS.public().get("state") == "running":
                        raise ValueError("Another operation is already running")
                    slot = take_lifecycle_slot()
                    try:
                        snapshot = release_snapshot(release_id, channel, ROOT)
                        result = start_detached("update-release", ROOT, RUNNING_IDENTITY, release_id, channel, target_version=snapshot["version"], snapshot=snapshot)
                    finally:
                        release_lifecycle_slot(slot)
                    self.send_json(202, result); return
                self.send_json(202, OPERATIONS.start(action, lambda: action_command(action, payload)))
                return
            if path == "/api/restart-panel":
                slot = take_lifecycle_slot()
                try:
                    result = start_detached("restart-panel", ROOT, RUNNING_IDENTITY)
                finally:
                    release_lifecycle_slot(slot)
                self.send_json(202, result)
                return
            if path == "/api/component":
                component = str(payload.get("component", ""))
                verb = str(payload.get("action", ""))
                self.send_json(202, OPERATIONS.start(f"{verb} {component}", lambda: component_command(component, verb)))
                return
            self.send_json(404, {"error": "Not found"})
        except FieldErrors as exc:
            self.send_json(400, {"error": str(exc), "fields": exc.fields})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})


def main() -> None:
    settings = panel_settings()
    host = settings.get("CONTROL_PANEL_HOST", DEFAULT_HOST)
    port = int(settings.get("CONTROL_PANEL_PORT", str(DEFAULT_PORT)))
    if not settings.get("CONTROL_PANEL_TOKEN"):
        raise SystemExit("CONTROL_PANEL_TOKEN is required")
    server = ThreadingHTTPServer((host, port), ControlPanelHandler)
    print(f"VesselStack Control Panel listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
