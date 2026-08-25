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
import subprocess
import tempfile
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


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


FIELDS = [
    field("BOAT_NAME", "Boat name", "Vessel", description="Display name used across VesselStack."),
    field("BOAT_TYPE", "Boat type", "Vessel"),
    field("BOAT_MMSI", "MMSI", "Vessel", kind="secret", description="Optional private vessel identity; never commit it."),
    field("BOAT_CALLSIGN", "Call sign", "Vessel", kind="secret", description="Optional private vessel identity; never commit it."),
    field("BOAT_LOA_M", "Length overall (m)", "Vessel", kind="number"),
    field("BOAT_BEAM_M", "Beam (m)", "Vessel", kind="number"),
    field("BOAT_TIMEZONE", "Timezone", "Vessel", description="IANA timezone such as America/Los_Angeles."),
    field("BOAT_UNITS", "Display units", "Vessel", kind="select", choices=["us_customary", "metric"]),
    field("VESSELSTACK_USER", "Service account", "Installation", read_only=True, description="Change from the recovery shell with a reviewed migration."),
    field("VESSELSTACK_ROOT", "Application path", "Installation", kind="path", read_only=True, description="Change from the recovery shell with a reviewed migration."),
    field("VESSELSTACK_DATA", "Data path", "Installation", kind="path", read_only=True, description="Relocate data only with a stopped stack and verified backup."),
    field("VESSELSTACK_BACKUP", "Backup path", "Installation", kind="path"),
    field("SIGNALK_MODE", "SignalK mode", "SignalK", kind="select", choices=["existing", "docker", "native"]),
    field("SIGNALK_VERSION", "SignalK version", "SignalK"),
    field("SIGNALK_URL", "SignalK URL", "SignalK", kind="url"),
    field("SOCKETCAN_ENABLE", "Enable SocketCAN", "SignalK", kind="boolean"),
    field("SOCKETCAN_INTERFACE", "CAN interface", "SignalK"),
    field("SOCKETCAN_BITRATE", "CAN bitrate", "SignalK", kind="number"),
    field("AIS_ENABLE", "Enable AIS-catcher", "AIS", kind="boolean"),
    field("AIS_IMAGE", "AIS image", "AIS"),
    field("AIS_DEVICE", "AIS device", "AIS", kind="path"),
    field("AIS_CATCHER_ARGS", "AIS-catcher arguments", "AIS"),
    field("AIS_WEB_PORT", "AIS web port", "AIS", kind="port"),
    field("AIS_TCP_PORT", "AIS TCP output port", "AIS", kind="port"),
    field("HOME_ASSISTANT_URL", "Home Assistant URL", "Home Assistant", kind="url"),
    field("HOME_ASSISTANT_TOKEN", "Home Assistant token", "Home Assistant", kind="secret"),
    field("INFLUXDB_URL", "InfluxDB URL", "InfluxDB", kind="url"),
    field("INFLUXDB_PORT", "Published port", "InfluxDB", kind="port"),
    field("INFLUXDB_CONTAINER_NAME", "Container name", "InfluxDB"),
    field("INFLUXDB_ORG", "Organization", "InfluxDB"),
    field("INFLUXDB_USERNAME", "Admin username", "InfluxDB"),
    field("INFLUXDB_RAW_BUCKET", "Raw bucket", "InfluxDB"),
    field("INFLUXDB_HISTORY_BUCKET", "History bucket", "InfluxDB"),
    field("INFLUXDB_HOME_ASSISTANT_BUCKET", "Home Assistant bucket", "InfluxDB"),
    field("INFLUXDB_AIS_BUCKET", "AIS bucket", "InfluxDB"),
    field("INFLUXDB_PASSWORD", "Admin password", "InfluxDB", kind="secret"),
    field("INFLUXDB_TOKEN", "Admin token", "InfluxDB", kind="secret"),
    field("GRAFANA_ADMIN_PASSWORD", "Grafana admin password", "Grafana", kind="secret"),
    field("GRAFANA_PORT", "Published port", "Grafana", kind="port"),
    field("HEIMDALL_PORT", "HTTP port", "Heimdall", kind="port"),
    field("HEIMDALL_HTTPS_PORT", "HTTPS port", "Heimdall", kind="port"),
    field("PROMETHEUS_PORT", "Published port", "Prometheus", kind="port"),
    field("MQTT_USERNAME", "MQTT username", "MQTT"),
    field("MQTT_PASSWORD", "MQTT password", "MQTT", kind="secret"),
    field("MQTT_PORT", "Published port", "MQTT", kind="port"),
    field("VESSELSTACK_FIREWALL_ENABLE", "Enable interface firewall", "Network", kind="boolean"),
    field("VESSELSTACK_UNTRUSTED_INTERFACE", "Untrusted interface", "Network"),
    field("CONTROL_PANEL_HOST", "Control panel host", "Control Panel", description="Keep 127.0.0.1 unless access is protected by a trusted VPN or proxy."),
    field("CONTROL_PANEL_PORT", "Control panel port", "Control Panel", kind="port"),
    field("BOAT_CHAT_PROVIDER", "Primary provider", "Boat Chat", source="chat", kind="select", choices=["local", "codex_cli", "claude_cli", "openai", "vercel", "bedrock", "google", "ollama", "openai_compatible"]),
    field("BOAT_CHAT_MODEL", "Primary model", "Boat Chat", source="chat"),
    field("BOAT_CHAT_FALLBACK_PROVIDER", "Fallback provider", "Boat Chat", source="chat", kind="select", choices=["", "local", "codex_cli", "claude_cli", "openai", "vercel", "bedrock", "google", "ollama", "openai_compatible"]),
    field("BOAT_CHAT_FALLBACK_MODEL", "Fallback model", "Boat Chat", source="chat"),
    field("BOAT_CHAT_HOST", "Boat Chat host", "Boat Chat", source="chat"),
    field("BOAT_CHAT_PORT", "Boat Chat port", "Boat Chat", source="chat", kind="port"),
    field("TELEMETRY_INDEXER_ENABLE", "Refresh telemetry memory", "Boat Chat", kind="boolean"),
    field("OLLAMA_HOST", "Ollama URL", "Boat Chat", source="chat", kind="url"),
    field("BOAT_CHAT_BASE_URL", "Compatible API base URL", "Boat Chat", source="chat", kind="url"),
    field("OPENAI_API_KEY", "OpenAI API key", "Boat Chat", source="chat", kind="secret"),
    field("AI_GATEWAY_API_KEY", "Vercel AI Gateway key", "Boat Chat", source="chat", kind="secret"),
    field("BOAT_CHAT_API_KEY", "Compatible API key", "Boat Chat", source="chat", kind="secret"),
    field("AWS_REGION", "AWS region", "Boat Chat", source="chat"),
    field("AWS_ACCESS_KEY_ID", "AWS access key", "Boat Chat", source="chat", kind="secret"),
    field("AWS_SECRET_ACCESS_KEY", "AWS secret key", "Boat Chat", source="chat", kind="secret"),
    field("GOOGLE_API_KEY", "Google API key", "Boat Chat", source="chat", kind="secret"),
    field("GOOGLE_CLOUD_PROJECT", "Google Cloud project", "Boat Chat", source="chat"),
    field("TELEGRAM_BOT_TOKEN", "Telegram bot token", "Telegram", source="chat", kind="secret"),
    field("TELEGRAM_ALLOWED_CHAT_IDS", "Allowed Telegram chat IDs", "Telegram", source="chat", kind="secret"),
    field("TELEGRAM_ENABLE", "Run Telegram worker", "Telegram", kind="boolean"),
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
}
SYSTEMD_COMPONENTS = {
    "boat-chat": "vesselstack-chat.service",
    "telegram": "vesselstack-chat-telegram.service",
    "telemetry-indexer": "vesselstack-telemetry-indexer.timer",
    "signalk-native": "vesselstack-signalk.service",
    "socketcan": "vesselstack-socketcan.service",
    "firewall": "vesselstack-firewall.service",
    "control-panel": "vesselstack-control-panel.service",
}


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values
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
    if kind in {"number", "port"} and text:
        try:
            float(text)
        except ValueError as exc:
            raise ValueError(f"{spec['label']} must be numeric") from exc
    if kind == "port" and text:
        if not text.isdigit() or not 1 <= int(text) <= 65535:
            raise ValueError(f"{spec['label']} must be an integer from 1 to 65535")
    if spec["key"] in {"CONTROL_PANEL_HOST", "BOAT_CHAT_HOST"} and text:
        try:
            address = ipaddress.ip_address(text)
        except ValueError as exc:
            raise ValueError(f"{spec['label']} must be an IPv4 address") from exc
        if address.version != 4:
            raise ValueError(f"{spec['label']} must be an IPv4 address")
    if kind == "path" and text and not text.startswith("/"):
        raise ValueError(f"{spec['label']} must be an absolute path")
    if kind == "url" and text:
        parsed = urllib.parse.urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"{spec['label']} must be an HTTP(S) URL")
    return text


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
    return {"fields": FIELDS, "values": result}


def update_configuration(updates: dict[str, Any]) -> str:
    vessel = parse_env(VESSEL_ENV)
    chat = parse_env(CHAT_ENV)
    panel = parse_env(PANEL_ENV)
    for key, raw in updates.items():
        spec = FIELD_BY_KEY.get(key)
        if not spec:
            raise ValueError(f"Unsupported setting: {key}")
        if spec["read_only"]:
            raise ValueError(f"{spec['label']} is read-only in the control panel")
        if spec["kind"] == "secret" and str(raw or "") == "":
            if raw is not None:
                continue
            value = ""
        else:
            value = validate_value(spec, raw)
        target = chat if spec["source"] == "chat" else vessel
        target[key] = value
        if key in {"CONTROL_PANEL_HOST", "CONTROL_PANEL_PORT"}:
            panel[key] = value
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
    return str(backup)


def compose_base() -> list[str]:
    return [
        "docker", "compose", "--profile", "signalk", "--profile", "ais",
        "--env-file", str(VESSEL_ENV), "-f", str(ROOT / "compose.yml"),
    ]


def component_command(component: str, verb: str) -> list[str]:
    if verb not in {"start", "stop", "restart"}:
        raise ValueError("Unsupported component action")
    if component in COMPOSE_COMPONENTS:
        settings = parse_env(VESSEL_ENV)
        if component == "signalk" and settings.get("SIGNALK_MODE", "existing") != "docker":
            raise ValueError("Docker SignalK is disabled by configuration")
        if component == "ais-catcher" and settings.get("AIS_ENABLE", "false") != "true":
            raise ValueError("AIS-catcher is disabled by configuration")
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
            "signalk-native": settings.get("SIGNALK_MODE") == "native",
            "socketcan": settings.get("SOCKETCAN_ENABLE", "false") == "true",
            "firewall": settings.get("VESSELSTACK_FIREWALL_ENABLE", "false") == "true",
        }
        if component in enabled and not enabled[component]:
            raise ValueError(f"{component} is disabled by configuration")
        return ["systemctl", verb, SYSTEMD_COMPONENTS[component]]
    raise ValueError("Unknown component")


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
        candidates = [reviewed_root, resolved, resolved / "install.sh", resolved / "VERSION"]
        if source.is_symlink() or not candidates[-2].is_file() or not candidates[-1].is_file():
            raise ValueError("Reviewed release must contain regular install.sh and VERSION files")
        for candidate in candidates:
            stat = candidate.stat()
            if stat.st_uid != os.geteuid() or stat.st_mode & 0o022:
                raise ValueError("Reviewed release paths must be panel-owner controlled and not group/world writable")
        return [str(CTL), "update", str(resolved)]
    raise ValueError("Unsupported action")


def command_output(command: list[str], timeout: int = 8) -> tuple[int, str]:
    try:
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False)
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def component_status() -> list[dict[str, Any]]:
    vessel = parse_env(VESSEL_ENV)
    compose_state: dict[str, str] = {}
    code, output = command_output(compose_base() + ["ps", "--format", "json"])
    if code == 0 and output:
        try:
            decoded = json.loads(output)
            rows = decoded if isinstance(decoded, list) else [decoded]
        except json.JSONDecodeError:
            rows = []
            for line in output.splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        for row in rows:
            service = str(row.get("Service") or row.get("service") or "")
            compose_state[service] = str(row.get("State") or row.get("state") or "unknown")
    components = []
    for name, service in COMPOSE_COMPONENTS.items():
        enabled = True
        if name == "signalk":
            enabled = vessel.get("SIGNALK_MODE", "existing") == "docker"
        elif name == "ais-catcher":
            enabled = vessel.get("AIS_ENABLE", "false") == "true"
        components.append({"id": name, "label": name.replace("-", " ").title(), "kind": "container", "enabled": enabled, "state": compose_state.get(service, "stopped")})
    for name, unit in SYSTEMD_COMPONENTS.items():
        code, output = command_output(["systemctl", "is-active", unit], timeout=3)
        enabled = True
        if name == "signalk-native":
            enabled = vessel.get("SIGNALK_MODE") == "native"
        elif name == "telegram":
            enabled = vessel.get("TELEGRAM_ENABLE", "false") == "true"
        elif name == "telemetry-indexer":
            enabled = vessel.get("TELEMETRY_INDEXER_ENABLE", "true") == "true"
        elif name == "socketcan":
            enabled = vessel.get("SOCKETCAN_ENABLE", "false") == "true"
        elif name == "firewall":
            enabled = vessel.get("VESSELSTACK_FIREWALL_ENABLE", "false") == "true"
        components.append({"id": name, "label": name.replace("-", " ").title(), "kind": "systemd", "enabled": enabled, "state": output or ("active" if code == 0 else "inactive")})
    return components


class OperationStore:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current: dict[str, Any] | None = None
        self.counter = 0

    def start(self, label: str, command: list[str]) -> dict[str, Any]:
        with self.lock:
            if self.current and self.current["state"] == "running":
                raise ValueError("Another operation is already running")
            self.counter += 1
            operation = {
                "id": self.counter,
                "label": label,
                "command": command[0:1] + ["[arguments hidden]"],
                "state": "running",
                "started_at": int(time.time()),
                "finished_at": None,
                "exit_code": None,
                "output": deque(maxlen=400),
            }
            self.current = operation
        threading.Thread(target=self._run, args=(operation, command), daemon=True).start()
        return self.public()

    def _run(self, operation: dict[str, Any], command: list[str]) -> None:
        environment = os.environ.copy()
        environment["VESSELSTACK_SKIP_PANEL_RESTART"] = "true"
        try:
            process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment)
            assert process.stdout is not None
            for line in process.stdout:
                operation["output"].append(line.rstrip())
            operation["exit_code"] = process.wait()
            operation["state"] = "succeeded" if operation["exit_code"] == 0 else "failed"
            if operation["state"] == "succeeded" and operation["label"] in {"apply", "install", "update"}:
                operation["output"].append(
                    "If this operation changed Control Panel code or its listener, restart "
                    "vesselstack-control-panel.service from the recovery shell."
                )
        except Exception as exc:
            operation["output"].append(str(exc))
            operation["exit_code"] = 1
            operation["state"] = "failed"
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
        supplied = (self.headers.get("X-VesselStack-Token") or "").strip()
        return bool(expected) and hmac.compare_digest(expected, supplied)

    def require_auth(self) -> bool:
        if self.authenticated():
            return True
        self.send_json(401, {"error": "A valid X-VesselStack-Token header is required"})
        return False

    def read_payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1024 * 1024:
            raise ValueError("Request is too large")
        payload = json.loads(self.rfile.read(length).decode() or "{}")
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
            self.send_json(200, {"ok": True, "version": os.environ.get("VESSELSTACK_VERSION", "development")})
            return
        if path.startswith("/api/"):
            if not self.require_auth():
                return
            if path == "/api/config":
                self.send_json(200, public_configuration())
            elif path == "/api/status":
                self.send_json(200, {"components": component_status(), "operation": OPERATIONS.public()})
            elif path == "/api/operation":
                self.send_json(200, OPERATIONS.public())
            else:
                self.send_json(404, {"error": "Not found"})
            return
        self.send_static(self.static_target(path))

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
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
                command = action_command(action, payload)
                self.send_json(202, OPERATIONS.start(action, command))
                return
            if path == "/api/component":
                component = str(payload.get("component", ""))
                verb = str(payload.get("action", ""))
                command = component_command(component, verb)
                self.send_json(202, OPERATIONS.start(f"{verb} {component}", command))
                return
            self.send_json(404, {"error": "Not found"})
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
