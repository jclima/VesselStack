#!/usr/bin/env python3
"""Local read-only boat diagnostic chat service."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import hmac
import io
import ipaddress
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import memory_index
import query_planner
import session_store
import telemetry_cache


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
STATIC_ROOT = ROOT / "static"
MCP_CONFIG = REPO_ROOT / ".mcp.json"
ENV_CONFIG = ROOT / "boat-chat.env"
BOAT_FACTS = ROOT / "boat_facts.json"
BOAT_CHAT_AGENT = ROOT / "BOAT_CHAT_AGENT.md"
KNOWLEDGE_ROOT = ROOT / "knowledge"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
MAX_REQUEST_BODY = 1024 * 1024
DEFAULT_OPENAI_MODEL = "gpt-5.5"
DEFAULT_GOOGLE_MODEL = "gemini-2.5-flash"
DEFAULT_VERCEL_MODEL = "alibaba/qwen3.5-flash"
DEFAULT_OLLAMA_MODEL = "llama3.2"
DEFAULT_OLLAMA_FALLBACK_MODEL = "qwen2.5:3b"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_CODEX_MODEL = "gpt-5.4-mini"
DEFAULT_CODEX_EFFORT = "low"
DEFAULT_CLAUDE_EFFORT = "low"
DEFAULT_CLI_TIMEOUT_SECONDS = 120
BOAT_NAME = os.environ.get("BOAT_NAME", "VesselStack")
DEFAULT_SYSTEM_PROMPT = (
    f"You are the Boat Diagnostic Agent for {BOAT_NAME}. Answer boat-specific questions by grounding every assessment "
    "in the provided local telemetry, Home Assistant state, Influx summaries, and local docs. Use public "
    "marine/mechanical knowledge only as general background. Optimize every answer for mobile chat. Default to "
    "1-3 short sentences or a few compact bullets, with the most useful answer first. Only use diagnostic sections "
    "when the user explicitly asks for diagnosis or troubleshooting. Never guess; if the question is ambiguous or "
    "needed boat data is missing, ask one short clarifying question. Do not claim mechanical certainty from "
    "telemetry alone. Use SI/unit conversions already provided in context. Do not mention internal context tiers, "
    "history flags, MCP, prompts, implementation files, or query access."
)

SETTING_KEYS = [
    "BOAT_CHAT_PROVIDER",
    "BOAT_CHAT_MODEL",
    "BOAT_CHAT_FALLBACK_PROVIDER",
    "BOAT_CHAT_FALLBACK_MODEL",
    "BOAT_CHAT_MAX_TOKENS",
    "BOAT_CHAT_CONTEXT_CHARS",
    "BOAT_CHAT_OLLAMA_NUM_CTX",
    "BOAT_CHAT_SETTINGS_TOKEN",
    "BOAT_CHAT_ACCESS_TOKEN",
    "BOAT_CHAT_WEB_SEARCH",
    "BOAT_CHAT_CLI_TIMEOUT",
    "BOAT_CHAT_CODEX_MODEL",
    "BOAT_CHAT_CODEX_EFFORT",
    "BOAT_CHAT_CODEX_BIN",
    "BOAT_CHAT_CLAUDE_MODEL",
    "BOAT_CHAT_CLAUDE_EFFORT",
    "BOAT_CHAT_CLAUDE_BIN",
    "BOAT_CHAT_CLAUDE_MAX_BUDGET_USD",
    "BOAT_CHAT_HOST",
    "BOAT_CHAT_PORT",
    "BOAT_CHAT_URL",
    "OLLAMA_HOST",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_CHAT_IDS",
    "TELEGRAM_POLL_TIMEOUT",
    "OPENAI_API_KEY",
    "AI_GATEWAY_API_KEY",
    "VERCEL_OIDC_TOKEN",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_CLOUD_ACCESS_TOKEN",
    "GOOGLE_OAUTH_ACCESS_TOKEN",
    "BOAT_CHAT_BASE_URL",
    "BOAT_CHAT_API_KEY",
]
SECRET_SETTING_KEYS = {
    "BOAT_CHAT_SETTINGS_TOKEN",
    "BOAT_CHAT_ACCESS_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "OPENAI_API_KEY",
    "AI_GATEWAY_API_KEY",
    "VERCEL_OIDC_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_CLOUD_ACCESS_TOKEN",
    "GOOGLE_OAUTH_ACCESS_TOKEN",
    "BOAT_CHAT_API_KEY",
}
PROVIDER_OPTIONS = ["local", "codex_cli", "claude_cli", "openai", "vercel", "bedrock", "google", "ollama", "openai_compatible"]
CHAT_SEMAPHORE = threading.BoundedSemaphore(2)
RATE_LIMIT_LOCK = threading.Lock()
RATE_LIMIT_REQUESTS: dict[str, list[float]] = {}
RATE_LIMIT_MAX_REQUESTS = 20
RATE_LIMIT_WINDOW_SECONDS = 60


class ModelCallError(RuntimeError):
    """Raised when a configured LLM provider cannot return an answer."""


def json_default(value: Any) -> str:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

def load_system_prompt() -> str:
    try:
        prompt = BOAT_CHAT_AGENT.read_text(errors="ignore").strip()
    except Exception:
        prompt = ""
    return prompt or DEFAULT_SYSTEM_PROMPT


_SYSTEM_PROMPT_CACHE: dict[str, Any] = {"mtime": None, "text": ""}


def system_prompt() -> str:
    """Return the system prompt, reloading BOAT_CHAT_AGENT.md when it changes on disk."""
    try:
        mtime: float | None = BOAT_CHAT_AGENT.stat().st_mtime
    except OSError:
        mtime = None
    if _SYSTEM_PROMPT_CACHE["mtime"] != mtime or not _SYSTEM_PROMPT_CACHE["text"]:
        _SYSTEM_PROMPT_CACHE["text"] = load_system_prompt()
        _SYSTEM_PROMPT_CACHE["mtime"] = mtime
    return str(_SYSTEM_PROMPT_CACHE["text"])


DOC_PATHS = [
    ROOT / "boat_facts.json",
    ROOT / "BOAT_CHAT_AGENT.md",
    ROOT / "README.md",
    KNOWLEDGE_ROOT / "telemetry.md",
    KNOWLEDGE_ROOT / "answering.md",
]

PROPULSION_PATHS = [
    "propulsion.port.fuel.rate",
    "propulsion.starboard.fuel.rate",
    "propulsion.port.revolutions",
    "propulsion.starboard.revolutions",
    "propulsion.port.engineLoad",
    "propulsion.starboard.engineLoad",
    "propulsion.port.engineTorque",
    "propulsion.starboard.engineTorque",
    "propulsion.port.boostPressure",
    "propulsion.starboard.boostPressure",
    "propulsion.port.drive.trimState",
    "propulsion.starboard.drive.trimState",
    "propulsion.port.temperature",
    "propulsion.starboard.temperature",
    "propulsion.port.oilPressure",
    "propulsion.starboard.oilPressure",
]
TRIP_CORE_PATHS = [
    "navigation.speedOverGround",
    "environment.depth.belowTransducer",
    "electrical.batteries.shunt.capacity.stateOfCharge",
    "electrical.batteries.shunt.voltage",
    "propulsion.port.revolutions",
    "propulsion.starboard.revolutions",
    "propulsion.port.fuel.rate",
    "propulsion.starboard.fuel.rate",
    "propulsion.port.temperature",
    "propulsion.starboard.temperature",
    "propulsion.port.oilPressure",
    "propulsion.starboard.oilPressure",
    "tanks.fuel.0.currentLevel",
    "tanks.fuel.1.currentLevel",
]
# Legacy defaults preserve existing installations; new installs inject the
# vessel-neutral names from vesselstack.env.
INFLUX_RAW_BUCKET = os.environ.get("INFLUXDB_RAW_BUCKET", "vesselstack_raw")
INFLUX_HISTORY_BUCKET = os.environ.get("INFLUXDB_HISTORY_BUCKET", "vesselstack_1m")
INFLUX_HOME_ASSISTANT_BUCKET = os.environ.get("INFLUXDB_HOME_ASSISTANT_BUCKET", "homeassistant")
INFLUX_AIS_BUCKET = os.environ.get("INFLUXDB_AIS_BUCKET", "ais")
INFLUX_HISTORY_BUCKETS = [INFLUX_HISTORY_BUCKET, INFLUX_HOME_ASSISTANT_BUCKET]
HA_TELEMETRY_DOMAINS = {"binary_sensor", "device_tracker", "input_boolean", "input_number", "input_select", "input_text", "sensor", "switch"}
HA_EVENT_DOMAINS = {"automation", "binary_sensor", "input_boolean", "switch"}
TOKEN_STOPWORDS = {
    "about",
    "after",
    "again",
    "against",
    "all",
    "and",
    "any",
    "are",
    "boat",
    "can",
    "could",
    "current",
    "data",
    "did",
    "does",
    "for",
    "from",
    "give",
    "has",
    "have",
    "history",
    "how",
    "hour",
    "hours",
    "info",
    "information",
    "last",
    "latest",
    "look",
    "many",
    "much",
    "now",
    "over",
    "past",
    "please",
    "right",
    "see",
    "show",
    "state",
    "status",
    "telemetry",
    "tell",
    "that",
    "the",
    "this",
    "trend",
    "trended",
    "trending",
    "was",
    "were",
    "what",
    "when",
    "which",
    "with",
    "you",
}
TELEMETRY_CONCEPT_TERMS = {
    "ais": ["ais", "nearby vessel", "nearby vessels", "marine traffic", "traffic", "other vessel", "other vessels"],
    "alarm": ["alarm", "alarms", "alert", "alerts", "fault", "faults", "warning", "warnings", "wrong", "abnormal"],
    "alternator": ["alternator"],
    "attitude": ["attitude", "pitch", "roll", "yaw"],
    "battery": ["battery", "batteries", "smartshunt", "shunt", "state of charge", "soc"],
    "bilge": ["bilge"],
    "boost": ["boost", "turbo"],
    "course": ["course", "cog"],
    "depth": ["depth", "deepest", "shallow", "shallowest", "water depth"],
    "engine": ["engine", "engines", "motor", "motors", "propulsion"],
    "freshness": [
        "stale",
        "stopped updating",
        "stop updating",
        "not updating",
        "stopped reporting",
        "stop reporting",
        "not reporting",
        "last update",
        "old timestamp",
        "old home assistant timestamp",
        "freshness",
        "offline sensor",
        "offline sensors",
        "unavailable sensor",
        "unavailable sensors",
    ],
    "fuel": ["fuel", "gas", "diesel"],
    "fuel_level": ["fuel level", "fuel remains", "fuel remaining", "fuel left", "tank level", "tank levels", "fuel tank", "fuel tanks", "range"],
    "fuel_rate": ["fuel rate", "fuel burn", "burn rate", "consumption", "gallons per hour", "gal/h", "gph"],
    "heading": ["heading"],
    "humidity": ["humidity", "humid"],
    "load": ["engine load", "load"],
    "oil_pressure": ["oil pressure"],
    "position": ["position", "gps", "latitude", "longitude", "location"],
    "pressure": ["barometer", "barometric", "air pressure", "pressure"],
    "rpm": ["rpm", "revolutions"],
    "service": ["service", "maintenance", "outage", "outages", "stack", "signalk", "docker"],
    "shore_power": ["shore power", "charger", "charging"],
    "solar": ["solar", "solar panel", "solar panels", "photovoltaic", "pv panel", "pv panels"],
    "speed": ["speed", "sog", "speed over ground", "speed through water"],
    "temperature": ["temperature", "temp", "coolant", "over temperature", "over-temperature"],
    "tide": ["tide", "tides"],
    "torque": ["torque"],
    "trip": ["trip", "underway", "voyage"],
    "weather": ["weather", "forecast", "wind", "storm"],
}
TELEMETRY_MEASUREMENT_TERMS = {
    "ais": ["ais", "nearby vessels", "marine traffic"],
    "alarm": ["alarm", "alert", "check engine", "low coolant", "low oil", "over temperature", "water in fuel", "warning", "boat health", "boat ok", "safety"],
    "alternator": ["alternator"],
    "attitude": ["attitude", "pitch", "roll", "yaw"],
    "battery": ["batteries shunt", "battery", "smartshunt", "state of charge", "voltage", "current", "power", "time remaining", "discharge since full"],
    "bilge": ["bilge", "h5100 5519"],
    "boost": ["boost pressure"],
    "course": ["course over ground", "course", "cog"],
    "depth": ["depth", "water depth"],
    "engine": ["propulsion", "engine port", "engine starboard", "engines running"],
    "freshness": ["gps age", "last updated", "last update", "backup age", "signalk api up", "signalk up"],
    "fuel": ["fuel"],
    "fuel_level": ["tanks fuel", "fuel tank", "fuel range", "fuel remaining"],
    "fuel_rate": ["fuel rate", "total fuel rate", "trip fuel used"],
    "heading": ["heading"],
    "humidity": ["humidity", "humid"],
    "load": ["engine load"],
    "oil_pressure": ["oil pressure"],
    "position": ["position", "latitude", "longitude", "distance from anchor", "distance from dock", "vessel"],
    "pressure": ["barometric pressure", "air pressure"],
    "rpm": ["revolutions", "engine port rpm", "engine starboard rpm", "engines running"],
    "service": ["hours until service", "maintenance", "service", "stack services down", "docker not running", "signalk down", "audit health"],
    "shore_power": ["shore power", "charger"],
    "solar": ["solar", "photovoltaic", "pv power", "pv voltage", "pv current", "solar yield"],
    "speed": ["speed over ground", "speed through water", "boat speed", "velocity made good"],
    "temperature": ["temperature", "coolant temp", "over temperature"],
    "tide": ["tide"],
    "torque": ["engine torque"],
    "trip": ["trip", "underway"],
    "weather": ["weather", "forecast", "wind", "storm"],
}
LOCAL_TZ = ZoneInfo(os.environ.get("BOAT_TIMEZONE", "UTC"))
ENGINE_RUNNING_RPM = 200.0
US_GALLONS_PER_LITER = 0.2641720524
TELEMETRY_CACHE_FRESH_SECONDS = 15 * 60
TELEMETRY_CACHE_STATIC_SECONDS = 24 * 60 * 60
AGM_VOLTAGE_SOC_POINTS = [
    (13.00, 100),
    (12.75, 90),
    (12.50, 80),
    (12.30, 70),
    (12.15, 60),
    (12.05, 50),
    (11.95, 40),
    (11.81, 30),
    (11.66, 20),
    (11.51, 10),
    (10.50, 0),
]


def load_mcp_config() -> dict[str, Any]:
    try:
        return json.loads(MCP_CONFIG.read_text())
    except Exception:
        return {"mcpServers": {}}


def load_boat_facts() -> dict[str, Any]:
    try:
        return json.loads(BOAT_FACTS.read_text())
    except Exception:
        return {
            "vessel": {
                "name": BOAT_NAME,
                "type": "Motor vessel",
                "mmsi": "000000000",
                "callsign": "UNSET",
            }
        }


def parse_env_config() -> dict[str, str]:
    settings: dict[str, str] = {}
    if not ENV_CONFIG.exists():
        return settings
    try:
        raw_lines = ENV_CONFIG.read_text(errors="ignore").splitlines()
    except OSError:
        # The production file is deliberately mode 600. Read-only tooling and
        # unit tests may run as another user and should fall back to process
        # environment/defaults instead of failing unrelated requests.
        return settings
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in SETTING_KEYS:
            continue
        try:
            parsed = shlex.split(value, comments=False, posix=True)
            settings[key] = parsed[0] if parsed else ""
        except ValueError:
            settings[key] = value.strip().strip("'\"")
    return settings


def setting_value(key: str, default: str | None = None) -> str | None:
    settings = parse_env_config()
    if key in settings:
        return settings[key]
    return os.environ.get(key, default)


def quote_env_value(value: str) -> str:
    if value == "":
        return '""'
    if re.search(r"\s|#|'|\"|\\\\", value):
        return shlex.quote(value)
    return value


def normalize_http_url(value: str, default_scheme: str = "http") -> str:
    text = value.strip()
    if not text:
        return text
    if "://" not in text:
        return f"{default_scheme}://{text}"
    return text


def write_env_config(settings: dict[str, str]) -> None:
    service_name = os.environ.get("BOAT_CHAT_SERVICE_NAME", "boat-chat.service")
    lines = [
        f"# Local runtime settings for {service_name}.",
        "# This file is ignored by Git; keep provider credentials here if needed.",
        "",
    ]
    for key in SETTING_KEYS:
        if key in settings and settings[key] != "":
            lines.append(f"{key}={quote_env_value(settings[key])}")
    lines.extend(
        [
            "",
            "# Provider values: local, codex_cli, claude_cli, openai, vercel, bedrock, google, ollama, openai_compatible",
            "# Optional fallback provider values use the same provider names.",
            f"# Host/port changes require: sudo systemctl restart {service_name}",
        ]
    )
    ENV_CONFIG.write_text("\n".join(lines) + "\n")
    try:
        ENV_CONFIG.chmod(0o600)
    except OSError:
        pass


def public_settings() -> dict[str, Any]:
    settings = parse_env_config()
    payload: dict[str, Any] = {
        "providers": PROVIDER_OPTIONS,
        "active_provider": configured_provider(),
        "active_fallback_provider": configured_fallback_provider(),
        "env_path": str(ENV_CONFIG),
        "settings": {},
        "secrets": {},
        "restart_required_for": ["BOAT_CHAT_HOST", "BOAT_CHAT_PORT"],
    }
    for key in SETTING_KEYS:
        value = settings.get(key, os.environ.get(key, ""))
        if key in SECRET_SETTING_KEYS:
            payload["secrets"][key] = bool(value)
        else:
            payload["settings"][key] = value
    return payload


def update_settings(updates: dict[str, Any]) -> dict[str, Any]:
    current = parse_env_config()
    for key, value in updates.items():
        if key not in SETTING_KEYS:
            continue
        if value is None:
            current.pop(key, None)
            continue
        text = str(value).strip()
        if key in SECRET_SETTING_KEYS and text == "":
            continue
        if text == "":
            current.pop(key, None)
        else:
            current[key] = text
    write_env_config(current)
    return public_settings()


def http_get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 8) -> Any:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None, timeout: int = 30) -> Any:
    data = json.dumps(payload).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def load_auth_headers(server_name: str) -> dict[str, str]:
    server = load_mcp_config().get("mcpServers", {}).get(server_name, {})
    headers = dict(server.get("headers", {}))
    if server_name == "ha" and "Authorization" not in headers:
        token = os.environ.get("HOME_ASSISTANT_TOKEN") or parse_env_config().get("HOME_ASSISTANT_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def load_influx_env() -> dict[str, str]:
    settings = parse_env_config()
    server = load_mcp_config().get("mcpServers", {}).get("influxdb", {})
    settings.update({str(key): str(value) for key, value in server.get("env", {}).items()})
    for key in ["INFLUXDB_URL", "INFLUXDB_ORG", "INFLUXDB_TOKEN"]:
        if os.environ.get(key):
            settings[key] = os.environ[key]
    return settings


def service_url(name: str, default: str) -> str:
    return (os.environ.get(name) or parse_env_config().get(name) or default).rstrip("/")


def query_influx(flux: str) -> list[dict[str, str]]:
    cfg = load_influx_env()
    url = f"{cfg.get('INFLUXDB_URL', 'http://127.0.0.1:8086')}/api/v2/query?org={urllib.parse.quote(cfg.get('INFLUXDB_ORG', 'vesselstack'))}"
    headers = {
        "Authorization": f"Token {cfg.get('INFLUXDB_TOKEN', '')}",
        "Accept": "text/csv",
        "Content-Type": "application/json",
    }
    payload = {"query": flux, "type": "flux"}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=25) as response:
        text = response.read().decode("utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def get_signalk_self() -> dict[str, Any]:
    try:
        return http_get_json(f"{service_url('SIGNALK_URL', 'http://127.0.0.1:3000')}/signalk/v1/api/vessels/self", timeout=6)
    except Exception as exc:
        return {"error": str(exc)}


def get_signalk_vessels() -> dict[str, Any]:
    try:
        data = http_get_json(f"{service_url('SIGNALK_URL', 'http://127.0.0.1:3000')}/signalk/v1/api/vessels", timeout=8)
        return data if isinstance(data, dict) else {"error": "SignalK vessels response was not an object"}
    except Exception as exc:
        return {"error": str(exc)}


def signalk_node(vessel: dict[str, Any], path: str) -> Any:
    node: Any = vessel
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def signalk_node_value(vessel: dict[str, Any], path: str) -> Any:
    node = signalk_node(vessel, path)
    if isinstance(node, dict) and "value" in node:
        return node.get("value")
    return node


def distance_nm(left: dict[str, Any], right: dict[str, Any]) -> float | None:
    try:
        lat1 = math.radians(float(left["latitude"]))
        lon1 = math.radians(float(left["longitude"]))
        lat2 = math.radians(float(right["latitude"]))
        lon2 = math.radians(float(right["longitude"]))
    except (KeyError, TypeError, ValueError):
        return None
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    haversine = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return 3440.065 * 2 * math.asin(min(1.0, math.sqrt(haversine)))


def bearing_deg(left: dict[str, Any], right: dict[str, Any]) -> float | None:
    try:
        lat1 = math.radians(float(left["latitude"])); lat2 = math.radians(float(right["latitude"]))
        delta_lon = math.radians(float(right["longitude"]) - float(left["longitude"]))
    except (KeyError, TypeError, ValueError):
        return None
    y = math.sin(delta_lon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def current_ais_context(limit: int = 20) -> dict[str, Any]:
    vessels = get_signalk_vessels()
    if "error" in vessels:
        return {"error": vessels["error"], "targets": []}
    facts = load_boat_facts()
    own_mmsi = str(facts.get("vessel", {}).get("mmsi", ""))
    own_vessel = next(
        (
            vessel
            for vessel in vessels.values()
            if isinstance(vessel, dict) and str(vessel.get("mmsi", "")) == own_mmsi
        ),
        {},
    )
    own_position = signalk_node_value(own_vessel, "navigation.position")
    targets: list[dict[str, Any]] = []
    for vessel_id, vessel in vessels.items():
        if not isinstance(vessel, dict) or str(vessel.get("mmsi", "")) == own_mmsi:
            continue
        position = signalk_node_value(vessel, "navigation.position")
        position_node = signalk_node(vessel, "navigation.position")
        position_timestamp = position_node.get("timestamp") if isinstance(position_node, dict) else None
        position_time = parse_influx_time(position_timestamp)
        age_minutes = (
            max(0.0, (dt.datetime.now(dt.timezone.utc) - position_time.astimezone(dt.timezone.utc)).total_seconds() / 60.0)
            if position_time
            else None
        )
        sog = safe_float(signalk_node_value(vessel, "navigation.speedOverGround"))
        cog = safe_float(signalk_node_value(vessel, "navigation.courseOverGroundTrue"))
        target = {
            "id": vessel_id,
            "mmsi": vessel.get("mmsi"),
            "name": vessel.get("name") or "unknown",
            "position": position,
            "distance_nm": round(distance_nm(own_position, position), 2)
            if isinstance(own_position, dict) and isinstance(position, dict) and distance_nm(own_position, position) is not None
            else None,
            "bearing_deg": round(bearing_deg(own_position, position), 1)
            if isinstance(own_position, dict) and isinstance(position, dict) and bearing_deg(own_position, position) is not None
            else None,
            "speed_kn": round(sog * 1.94384449, 2) if sog is not None else None,
            "course_deg": round(cog * 180.0 / math.pi, 1) if cog is not None else None,
            "navigation_state": signalk_node_value(vessel, "navigation.state"),
            "ship_type": signalk_node_value(vessel, "design.aisShipType"),
            "position_timestamp_utc": position_timestamp,
            "position_age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
            "position_stale": age_minutes > 15 if age_minutes is not None else None,
        }
        targets.append({key: value for key, value in target.items() if value not in (None, "")})
    targets.sort(key=lambda item: (item.get("distance_nm") is None, item.get("distance_nm", float("inf")), str(item.get("name", ""))))
    return {
        "source": "SignalK AIS vessels",
        "target_count": len(targets),
        "targets": targets[:limit],
        "truncated": len(targets) > limit,
    }


def get_ha_states(entity_ids: list[str]) -> dict[str, Any]:
    headers = load_auth_headers("ha")
    states: dict[str, Any] = {}
    for entity_id in entity_ids:
        try:
            states[entity_id] = http_get_json(f"{service_url('HOME_ASSISTANT_URL', 'http://127.0.0.1:8123')}/api/states/{entity_id}", headers=headers, timeout=5)
        except Exception as exc:
            states[entity_id] = {"error": str(exc)}
    return states


def get_ha_all_states() -> list[dict[str, Any]]:
    headers = load_auth_headers("ha")
    data = http_get_json(f"{service_url('HOME_ASSISTANT_URL', 'http://127.0.0.1:8123')}/api/states", headers=headers, timeout=10)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def ha_state_history(entity_id: str, days: int = 30) -> list[dict[str, Any]]:
    headers = load_auth_headers("ha")
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")
    query = urllib.parse.urlencode({"filter_entity_id": entity_id})
    url = f"{service_url('HOME_ASSISTANT_URL', 'http://127.0.0.1:8123')}/api/history/period/{urllib.parse.quote(start)}?{query}"
    data = http_get_json(url, headers=headers, timeout=10)
    if isinstance(data, list) and data and isinstance(data[0], list):
        return [item for item in data[0] if isinstance(item, dict)]
    return []


def shore_power_history_summary(message: str, days: int = 30, use_cache: bool = True) -> dict[str, Any]:
    entity_id = "binary_sensor.shore_power_connected"
    lower = message.lower()
    if any(term in lower for term in ["off", "lost", "disconnected", "turned off", "turn off"]):
        target_state = "off"
    elif any(term in lower for term in ["on", "restored", "connected", "turned on", "turn on"]):
        target_state = "on"
    else:
        target_state = ""
    cache_key = shore_power_cache_key(target_state or None, days)
    if use_cache:
        cached = telemetry_cache.get_summary("shore_power_history", cache_key, max_age_seconds=TELEMETRY_CACHE_FRESH_SECONDS)
        if cached:
            return cached
    current = get_ha_states([entity_id]).get(entity_id, {})
    history = ha_state_history(entity_id, days=days)
    matches = [item for item in history if not target_state or item.get("state") == target_state]
    latest = matches[-1] if matches else None
    summary = {
        "entity_id": entity_id,
        "lookback_days": days,
        "target_state": target_state or None,
        "current_state": current.get("state"),
        "current_last_changed_utc": current.get("last_changed"),
        "current_last_changed_local": format_local_time(current.get("last_changed")),
        "latest_matching_change": {
            "state": latest.get("state"),
            "last_changed_utc": latest.get("last_changed"),
            "last_changed_local": format_local_time(latest.get("last_changed")),
        }
        if latest
        else None,
        "history_events_returned": len(history),
        "notes": [
            "Shore Power Connected is a Home Assistant template binary sensor.",
            "It has a 2-minute on delay and 30-minute off delay to avoid float-mode false positives.",
        ],
    }
    telemetry_cache.put_summary("shore_power_history", cache_key, summary)
    return summary


def flatten_signalk(node: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(node, dict):
        if "value" in node:
            return {prefix: node.get("value")}
        out: dict[str, Any] = {}
        for key, value in node.items():
            child_prefix = f"{prefix}.{key}" if prefix else key
            out.update(flatten_signalk(value, child_prefix))
        return out
    return {}


def safe_float(value: Any) -> float | None:
    try:
        if value in ("", None, "unknown", "unavailable"):
            return None
        return float(value)
    except Exception:
        return None


def parse_influx_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_local_time(value: str | None) -> str:
    parsed = parse_influx_time(value)
    if not parsed:
        return value or "unknown"
    return parsed.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")


def normalize_match_text(value: str) -> str:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", value)
    return re.sub(r"[^a-z0-9]+", " ", spaced.lower()).strip()


def message_tokens(message: str) -> set[str]:
    tokens = set(re.findall(r"[a-zA-Z0-9]{3,}", normalize_match_text(message)))
    return {token for token in tokens if token not in TOKEN_STOPWORDS}


def telemetry_concepts(message: str) -> set[str]:
    normalized = f" {normalize_match_text(message)} "
    concepts: set[str] = set()
    for concept, terms in TELEMETRY_CONCEPT_TERMS.items():
        if any(f" {normalize_match_text(term)} " in normalized for term in terms):
            concepts.add(concept)
    if "fuel" in normalized:
        if any(term in normalized for term in [" tank ", " level ", " remain ", " remaining ", " left ", " range "]):
            concepts.add("fuel_level")
        if any(term in normalized for term in [" rate ", " burn ", " consumption ", " gph ", " gal h "]):
            concepts.add("fuel_rate")
    if "oil" in normalized and "pressure" in normalized:
        concepts.add("oil_pressure")
    if "coolant" in normalized:
        concepts.add("temperature")
    if concepts.intersection({"oil_pressure", "boost"}):
        concepts.discard("pressure")
    if concepts.intersection({"fuel_level", "fuel_rate"}):
        concepts.discard("fuel")
    concepts.update(query_planner.detect_signals(message))
    return concepts


def requested_engine_sides(message: str) -> set[str]:
    lower = message.lower()
    sides: set[str] = set()
    if re.search(r"\bport\b", lower):
        sides.add("port")
    if re.search(r"\bstarboard\b|\bstbd\b", lower):
        sides.add("starboard")
    if re.search(r"\bboth\b|\beach engine\b|\beach motor\b", lower):
        sides.update(["port", "starboard"])
    return sides


def measurement_side_allowed(name: str, sides: set[str]) -> bool:
    if not sides or sides == {"port", "starboard"}:
        return True
    normalized = normalize_match_text(name)
    if sides == {"port"} and "starboard" in normalized:
        return False
    if sides == {"starboard"} and re.search(r"\bport\b", normalized):
        return False
    return True


def semantic_match_score(name: str, message: str) -> int:
    sides = requested_engine_sides(message)
    if not measurement_side_allowed(name, sides):
        return 0
    normalized_name = normalize_match_text(name)
    concepts = telemetry_concepts(message)
    specific_engine_concepts = concepts.intersection(
        {"alternator", "boost", "fuel_rate", "load", "oil_pressure", "rpm", "temperature", "torque"}
    )
    engine_scoped = "engine" in concepts and bool(specific_engine_concepts)
    engine_measurement = any(
        term in normalized_name
        for term in [" propulsion ", "propulsion ", " engine port ", " engine starboard ", "engine port ", "engine starboard "]
    )
    if specific_engine_concepts:
        concepts.discard("engine")
    score = (
        0
        if (engine_scoped and not engine_measurement) or concepts == {"freshness"}
        else match_score(name, message_tokens(message))
    )
    for concept in concepts:
        if engine_scoped and concept in specific_engine_concepts and not engine_measurement:
            continue
        if any(normalize_match_text(term) in normalized_name for term in TELEMETRY_MEASUREMENT_TERMS.get(concept, [])):
            score += 8
    return score


def history_requested(message: str) -> bool:
    if query_planner.build_query_plan(message)["historical"]:
        return True
    lower = message.lower()
    return bool(
        re.search(r"\b(?:last|past)\s+\d+\s*(?:hours?|hrs?|days?|weeks?|months?|years?)\b", lower)
        or re.search(r"\b(?:today|yesterday|weekend|this week|last week|this month|last month|this year|last year|ytd|season)\b", lower)
        or any(
            term in lower
            for term in [
                "history",
                "trend",
                "over time",
                "recent",
                "recently",
                "last trip",
                "previous trip",
                "when did",
                "when was",
                "when were",
                "minimum",
                "maximum",
                "lowest",
                "highest",
                "deepest",
                "shallowest",
                "correlat",
                "increased",
                "decreased",
                "rise before",
                "fall before",
                "how long",
            ]
        )
    )


def complex_history_requested(message: str) -> bool:
    lower = message.lower()
    return any(
        term in lower
        for term in [
            " at that time",
            " abnormal",
            " before ",
            " after ",
            " below ",
            " above ",
            " between ",
            " comparable ",
            " compare ",
            " correlat",
            " deepest",
            " diagnose",
            " difference",
            " how long",
            " increased",
            " imply",
            " last trip",
            " lowest",
            " maximum",
            " minimum",
            " more than",
            " relative to",
            " shallowest",
            " threshold",
            " what happened",
            " while ",
            " why ",
        ]
    )


def sanitize_conversation_history(value: Any, limit: int = 8) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    history: list[dict[str, str]] = []
    for item in value[-limit:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip().lower()
        content = str(item.get("content", "")).strip()
        if role not in {"user", "assistant"} or not content:
            continue
        history.append({"role": role, "content": content[:4000]})
    return history


def effective_question(message: str, history: list[dict[str, str]] | None = None) -> str:
    history = history or []
    previous_user = next((item["content"] for item in reversed(history) if item.get("role") == "user"), "")
    if not previous_user:
        return message
    concepts = telemetry_concepts(message)
    followup_terms = ("and ", "what about ", "how about ", "same ", "for ", "why", "was that", "at that time", "while ", "compare it", "port", "starboard")
    followup = (
        len(message) <= 100
        and (
            (history_requested(message) and not concepts)
            or message.lower().startswith(followup_terms)
        )
    )
    if not followup:
        return message
    return f"{previous_user}\nFollow-up constraint: {message}"


def telemetry_overview_requested(message: str) -> bool:
    lower = message.lower()
    return any(
        term in lower
        for term in [
            "all telemetry",
            "available telemetry",
            "telemetry history",
            "what telemetry",
            "what data",
            "what information",
            "what can you see",
            "what do you have access",
        ]
    )


def history_bucket_for_hours(hours: int) -> str:
    # All history math runs on the 1-minute downsample bucket; the raw
    # The raw bucket adds no precision once samples are aggregated to 1m.
    return INFLUX_HISTORY_BUCKET


def rpm_cache_suffix(rpm_threshold: float) -> str:
    return f"{rpm_threshold:g}rpm"


def engine_run_cache_key(direction: str, days: int, rpm_threshold: float) -> str:
    return f"{direction}:{int(days)}d:{rpm_cache_suffix(rpm_threshold)}"


def fuel_usage_cache_key(window: dict[str, Any], rpm_threshold: float) -> str:
    if window.get("cache_key"):
        return f"{window['cache_key']}:{rpm_cache_suffix(rpm_threshold)}"
    return f"{window['start_utc']}:{window['stop_utc']}:{rpm_cache_suffix(rpm_threshold)}"


def shore_power_cache_key(target_state: str | None, days: int) -> str:
    return f"{target_state or 'any'}:{int(days)}d"


def fuel_economy_cache_key(days: int, rpm_threshold: float) -> str:
    return f"{int(days)}d:{rpm_cache_suffix(rpm_threshold)}"


def battery_voltage_cache_key(days: int) -> str:
    return f"{int(days)}d"


def agm_soc_from_voltage(voltage: float | None) -> float | None:
    if voltage is None:
        return None
    if voltage >= AGM_VOLTAGE_SOC_POINTS[0][0]:
        return 100.0
    for high, low in zip(AGM_VOLTAGE_SOC_POINTS, AGM_VOLTAGE_SOC_POINTS[1:]):
        high_voltage, high_soc = high
        low_voltage, low_soc = low
        if high_voltage >= voltage >= low_voltage:
            span = high_voltage - low_voltage
            if span <= 0:
                return float(low_soc)
            ratio = (voltage - low_voltage) / span
            return round(low_soc + ratio * (high_soc - low_soc), 1)
    return 0.0


def numeric_stats(values: list[float], digits: int = 3) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "avg": None, "max": None, "latest": None}
    ordered = sorted(values)
    return {
        "count": len(values),
        "min": round(ordered[0], digits),
        "avg": round(sum(values) / len(values), digits),
        "max": round(ordered[-1], digits),
        "latest": round(values[-1], digits),
    }


def compact_ha_state(state: dict[str, Any]) -> dict[str, Any]:
    attrs = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
    compact: dict[str, Any] = {
        "entity_id": state.get("entity_id"),
        "state": state.get("state"),
        "unit": attrs.get("unit_of_measurement"),
        "friendly_name": attrs.get("friendly_name"),
        "device_class": attrs.get("device_class"),
        "last_changed_local": format_local_time(state.get("last_changed")),
        "last_updated_local": format_local_time(state.get("last_updated")),
    }
    extra_attrs: dict[str, Any] = {}
    for key in ["latitude", "longitude", "gps_accuracy", "options", "last_triggered"]:
        if key in attrs:
            extra_attrs[key] = attrs[key]
    if extra_attrs:
        compact["attributes"] = extra_attrs
    return {key: value for key, value in compact.items() if value not in (None, "")}


def token_aliases(tokens: set[str]) -> set[str]:
    aliases = {
        "battery": ["batteries", "shunt", "smartshunt", "soc", "voltage", "current", "power"],
        "alternator": ["alternator", "alternatorVoltage"],
        "barometer": ["barometric", "pressure"],
        "barometric": ["barometric", "pressure"],
        "bilge": ["h5100_5519", "humid", "humidity"],
        "boost": ["boost", "boostPressure"],
        "cabin": ["h5100_1625"],
        "charge": ["stateOfCharge", "soc", "battery"],
        "bathroom": ["h5100_4a33"],
        "coolant": ["coolant"],
        "course": ["course", "courseOverGround"],
        "depth": ["depth"],
        "fuel": ["fuel", "tanks"],
        "gps": ["gps", "position", "latitude", "longitude"],
        "heading": ["heading", "headingMagnetic"],
        "level": ["level", "currentLevel"],
        "load": ["load", "engineLoad"],
        "motor": ["engine", "propulsion", "rpm", "revolutions"],
        "motors": ["engine", "propulsion", "rpm", "revolutions"],
        "oil": ["oil", "oilPressure"],
        "position": ["position", "latitude", "longitude"],
        "latitude": ["latitude", "position"],
        "longitude": ["longitude", "position"],
        "range": ["range"],
        "roll": ["roll", "attitude"],
        "rpm": ["rpm", "revolutions"],
        "shore": ["shore_power_connected", "shunt", "voltage", "current"],
        "soc": ["stateOfCharge", "battery", "smartshunt"],
        "tank": ["tank", "tanks", "currentLevel", "level"],
        "temperature": ["temperature", "temp"],
        "temp": ["temperature", "temp"],
        "torque": ["torque", "engineTorque"],
        "wind": ["wind"],
    }
    expanded = set(tokens)
    for token in list(tokens):
        expanded.update(aliases.get(token, []))
    return expanded


def required_tokens_for_query(tokens: set[str]) -> set[str]:
    required = set()
    for token in [
        "alternator",
        "battery",
        "bilge",
        "boost",
        "charge",
        "coolant",
        "course",
        "depth",
        "fuel",
        "gps",
        "heading",
        "humidity",
        "level",
        "load",
        "oil",
        "position",
        "range",
        "roll",
        "rpm",
        "soc",
        "speed",
        "tank",
        "temperature",
        "tide",
        "torque",
        "wind",
    ]:
        if token in tokens:
            required.add(token)
    return required


def match_score(name: str, tokens: set[str]) -> int:
    haystack = normalize_match_text(name)
    expanded = token_aliases(tokens)
    required = required_tokens_for_query(tokens)
    if required and not any(
        normalize_match_text(token) in haystack
        or any(normalize_match_text(alias) in haystack for alias in token_aliases({token}))
        for token in required
    ):
        return 0
    score = 0
    for token in expanded:
        if normalize_match_text(token) in haystack:
            score += 1
    for token in tokens:
        token_text = normalize_match_text(token)
        alias_texts = {normalize_match_text(alias) for alias in token_aliases({token})}
        if token_text in haystack or any(alias and alias in haystack for alias in alias_texts):
            score += 2
    return score


def ha_state_score(state: dict[str, Any], tokens: set[str]) -> int:
    entity_id = str(state.get("entity_id", ""))
    domain = entity_id.split(".", 1)[0]
    if domain not in HA_TELEMETRY_DOMAINS:
        return 0
    attrs = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
    return match_score(f"{entity_id} {attrs.get('friendly_name', '')} {attrs.get('device_class', '')}", tokens)


def ha_state_matches(state: dict[str, Any], tokens: set[str]) -> bool:
    return ha_state_score(state, tokens) > 0


def freshness_candidate(state: dict[str, Any]) -> bool:
    entity_id = str(state.get("entity_id", ""))
    if any(marker in entity_id for marker in ("iphone", "ipad", "phone_battery", "mobile_device")):
        return False
    if "automatic_backup" in entity_id:
        return False
    domain = entity_id.split(".", 1)[0]
    if domain != "sensor":
        return False
    attrs = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
    device_class = str(attrs.get("device_class", ""))
    state_class = str(attrs.get("state_class", ""))
    unit = attrs.get("unit_of_measurement")
    if any(term in entity_id for term in ["_events_", "_issue_events", "notification_count"]):
        return False
    return bool(
        unit
        or state_class in {"measurement", "total", "total_increasing"}
        or device_class
        in {
            "apparent_power",
            "battery",
            "current",
            "data_rate",
            "distance",
            "duration",
            "energy",
            "frequency",
            "gas",
            "humidity",
            "illuminance",
            "monetary",
            "power",
            "pressure",
            "signal_strength",
            "speed",
            "temperature",
            "timestamp",
            "voltage",
            "volume",
            "volume_flow_rate",
            "water",
            "weight",
            "wind_speed",
        }
    )


def ha_telemetry_context(message: str, limit: int = 24) -> dict[str, Any]:
    states = get_ha_all_states()
    telemetry_states = [
        state
        for state in states
        if str(state.get("entity_id", "")).split(".", 1)[0] in HA_TELEMETRY_DOMAINS
    ]
    tokens = message_tokens(message)
    overview = telemetry_overview_requested(message)
    scored = [
        (
            semantic_match_score(
                f"{state.get('entity_id', '')} {(state.get('attributes') or {}).get('friendly_name', '')}",
                message,
            ),
            state,
        )
        for state in telemetry_states
    ] if tokens or telemetry_concepts(message) else []
    matched = [state for score, state in sorted(scored, key=lambda item: (-item[0], str(item[1].get("entity_id", "")))) if score > 0]
    if "freshness" in telemetry_concepts(message):
        matched = [state for state in matched if freshness_candidate(state)]
    if overview and not matched:
        priority_terms = {
            "anchor",
            "audit",
            "battery",
            "bilge",
            "boat",
            "depth",
            "dock",
            "engine",
            "fuel",
            "gps",
            "health",
            "humidity",
            "power",
            "pressure",
            "shore",
            "signalk",
            "speed",
            "temperature",
            "tide",
            "voltage",
            "watch",
            "weather",
            "wind",
        }
        matched = [state for state in telemetry_states if ha_state_matches(state, priority_terms)]
    domains: dict[str, int] = {}
    for state in telemetry_states:
        domain = str(state.get("entity_id", "")).split(".", 1)[0]
        domains[domain] = domains.get(domain, 0) + 1
    now = dt.datetime.now(dt.timezone.utc)
    stale: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for state in telemetry_states:
        if not freshness_candidate(state):
            continue
        compact = compact_ha_state(state)
        if str(state.get("state", "")).lower() in {"unknown", "unavailable"}:
            unavailable.append(compact)
        updated = parse_influx_time(state.get("last_updated"))
        if updated and (now - updated.astimezone(dt.timezone.utc)).total_seconds() > 6 * 60 * 60:
            stale.append(compact)
    stale.sort(key=lambda item: str(item.get("last_updated_local", "")))
    payload = {
        "source": "Home Assistant /api/states",
        "total_states": len(states),
        "telemetry_state_count": len(telemetry_states),
        "domains": domains,
        "matched_count": len(matched),
        "matched": [compact_ha_state(state) for state in matched[:limit]],
        "truncated": len(matched) > limit,
    }
    if "freshness" in telemetry_concepts(message):
        payload["freshness_note"] = (
            "Home Assistant last_updated older than 6 hours is a review signal, not proof of failure; "
            "stable values may legitimately keep an old timestamp."
        )
        payload["unavailable"] = unavailable[:limit]
        payload["stale_over_6h"] = stale[:limit]
        payload["unavailable_count"] = len(unavailable)
        payload["stale_over_6h_count"] = len(stale)
    return payload


def ha_event_history_context(message: str, limit: int = 8) -> dict[str, Any]:
    window = query_planner.build_query_plan(message).get("window") or resolve_fuel_usage_window(message)
    lookback_days = max(1, math.ceil((dt.datetime.now(LOCAL_TZ) - window["start"]).total_seconds() / 86400))
    states = get_ha_all_states()
    candidates: list[tuple[int, dict[str, Any]]] = []
    for state in states:
        entity_id = str(state.get("entity_id", ""))
        domain = entity_id.split(".", 1)[0]
        if domain not in HA_EVENT_DOMAINS:
            continue
        attrs = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
        score = semantic_match_score(f"{entity_id} {attrs.get('friendly_name', '')}", message)
        if score > 0:
            candidates.append((score, state))
    candidates.sort(key=lambda item: (-item[0], str(item[1].get("entity_id", ""))))

    entities: dict[str, Any] = {}
    start_utc = window["start"].astimezone(dt.timezone.utc)
    stop_utc = window["stop"].astimezone(dt.timezone.utc)
    for _score, state in candidates[:limit]:
        entity_id = str(state.get("entity_id", ""))
        try:
            raw_history = ha_state_history(entity_id, days=lookback_days)
        except Exception as exc:
            entities[entity_id] = {"error": str(exc)}
            continue
        transitions: list[dict[str, Any]] = []
        previous_state: str | None = None
        for item in raw_history:
            changed = parse_influx_time(item.get("last_changed"))
            if not changed or not (start_utc <= changed.astimezone(dt.timezone.utc) <= stop_utc):
                continue
            item_state = str(item.get("state", "unknown"))
            if item_state == previous_state:
                continue
            previous_state = item_state
            transitions.append(
                {
                    "state": item_state,
                    "time_utc": item.get("last_changed"),
                    "time_local": format_local_time(item.get("last_changed")),
                }
            )
        entities[entity_id] = {
            "friendly_name": (state.get("attributes") or {}).get("friendly_name"),
            "current_state": state.get("state"),
            "transitions": transitions[-24:],
            "transition_count": len(transitions),
        }
    return {
        "source": "Home Assistant retained state transitions",
        "label": window["label"],
        "start_local": window["start_local"],
        "stop_local": window["stop_local"],
        "entities": entities,
    }


def influx_measurements(bucket: str) -> list[str]:
    cached = telemetry_cache.get_summary("influx_measurements", bucket, max_age_seconds=TELEMETRY_CACHE_STATIC_SECONDS)
    if cached and isinstance(cached.get("measurements"), list):
        return [str(item) for item in cached["measurements"]]
    flux = f'''
import "influxdata/influxdb/schema"
schema.measurements(bucket: "{bucket}")
'''
    rows = query_influx(flux)
    measurements = sorted({str(row.get("_value")) for row in rows if row.get("_value")})
    telemetry_cache.put_summary("influx_measurements", bucket, {"bucket": bucket, "measurements": measurements})
    return measurements


def influx_measurement_matches(measurement: str, message: str) -> bool:
    return semantic_match_score(measurement, message) > 0


def measurement_priority(measurement: str) -> tuple[int, str]:
    if measurement.startswith("sensor."):
        return (0, measurement)
    if measurement.startswith("binary_sensor."):
        return (1, measurement)
    if measurement.startswith("device_tracker."):
        return (2, measurement)
    if measurement.startswith("input_"):
        return (3, measurement)
    if measurement.startswith("propulsion.") or measurement.startswith("navigation.") or measurement.startswith("electrical."):
        return (0, measurement)
    if measurement.startswith("environment.") or measurement.startswith("tanks."):
        return (1, measurement)
    if measurement.startswith("automation.") or measurement.startswith("script."):
        return (8, measurement)
    return (5, measurement)


def sort_measurements_for_query(measurements: list[str], message: str) -> list[str]:
    return sorted(measurements, key=lambda item: (-semantic_match_score(item, message), measurement_priority(item), item))


def history_interval_for_window(window: dict[str, Any]) -> tuple[str, int]:
    start = window.get("start")
    stop = window.get("stop")
    seconds = (stop - start).total_seconds() if isinstance(start, dt.datetime) and isinstance(stop, dt.datetime) else 7 * 86400
    if seconds <= 2 * 86400:
        return ("1m", 1)
    if seconds <= 14 * 86400:
        return ("5m", 5)
    if seconds <= 31 * 86400:
        return ("15m", 15)
    if seconds <= 90 * 86400:
        return ("1h", 60)
    return ("6h", 360)


def pearson_correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 3:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    left_sum = sum((x - left_mean) ** 2 for x in left)
    right_sum = sum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_sum * right_sum)
    if denominator <= 0:
        return None
    return numerator / denominator


def analyze_history_rows(
    message: str,
    rows: list[dict[str, str]],
    selected: list[str],
    sample_minutes: int,
) -> dict[str, Any]:
    by_time: dict[str, dict[str, float]] = {}
    for row in rows:
        timestamp = str(row.get("_time") or "")
        measurement = str(row.get("_measurement") or "")
        value = safe_float(row.get("_value"))
        if not timestamp or not measurement or value is None:
            continue
        by_time.setdefault(timestamp, {})[measurement] = value

    correlations: list[dict[str, Any]] = []
    correlated_measurements = [item for item in selected if any(item in values for values in by_time.values())][:16]
    for index, left_name in enumerate(correlated_measurements):
        for right_name in correlated_measurements[index + 1 :]:
            pairs = [
                (values[left_name], values[right_name])
                for values in by_time.values()
                if left_name in values and right_name in values
            ]
            coefficient = pearson_correlation([item[0] for item in pairs], [item[1] for item in pairs])
            if coefficient is None:
                continue
            correlations.append(
                {
                    "left": left_name,
                    "right": right_name,
                    "coefficient": round(coefficient, 3),
                    "paired_samples": len(pairs),
                }
            )
    correlations.sort(key=lambda item: abs(float(item["coefficient"])), reverse=True)

    summary = summarize_numeric_rows(rows)
    snapshots: list[dict[str, Any]] = []
    seen_times: set[str] = set()
    for measurement in selected[:6]:
        stats = summary.get(measurement, {})
        for label, timestamp in [("minimum", stats.get("min_time_utc")), ("maximum", stats.get("max_time_utc"))]:
            if not timestamp or timestamp in seen_times or timestamp not in by_time:
                continue
            seen_times.add(str(timestamp))
            values = by_time[str(timestamp)]
            snapshots.append(
                {
                    "reason": f"{measurement} {label}",
                    "time_utc": timestamp,
                    "time_local": format_local_time(str(timestamp)),
                    "values": {name: round(values[name], 4) for name in selected[:16] if name in values},
                }
            )
            if len(snapshots) >= 10:
                break
        if len(snapshots) >= 10:
            break

    threshold_results: list[dict[str, Any]] = []
    threshold_match = re.search(
        r"\b(below|under|above|over)\s+(-?\d+(?:\.\d+)?)\s*(v|volts?|psi|knots?|kt|ft|feet|°?f|percent|%)?\b",
        message.lower(),
    )
    if threshold_match:
        direction = threshold_match.group(1)
        threshold = float(threshold_match.group(2))
        for measurement in selected[:8]:
            samples = [values[measurement] for values in by_time.values() if measurement in values]
            if not samples:
                continue
            matching = [value for value in samples if value < threshold] if direction in {"below", "under"} else [value for value in samples if value > threshold]
            threshold_results.append(
                {
                    "measurement": measurement,
                    "direction": "below" if direction in {"below", "under"} else "above",
                    "threshold": threshold,
                    "matching_samples": len(matching),
                    "total_samples": len(samples),
                    "approximate_matching_hours": round(len(matching) * sample_minutes / 60.0, 2),
                }
            )

    side_comparisons: list[dict[str, Any]] = []
    for port_name in [item for item in selected if ".port." in item or "engine_port_" in item]:
        starboard_name = port_name.replace(".port.", ".starboard.").replace("engine_port_", "engine_starboard_")
        if starboard_name not in selected:
            continue
        all_pairs: list[tuple[float, float]] = []
        rpm_matched_pairs: list[tuple[float, float]] = []
        for values in by_time.values():
            if port_name not in values or starboard_name not in values:
                continue
            pair = (values[port_name], values[starboard_name])
            all_pairs.append(pair)
            port_rpm = values.get("propulsion.port.revolutions")
            starboard_rpm = values.get("propulsion.starboard.revolutions")
            if port_rpm is not None and starboard_rpm is not None and abs(port_rpm - starboard_rpm) <= 50:
                rpm_matched_pairs.append(pair)

        def comparison_payload(pairs: list[tuple[float, float]]) -> dict[str, Any] | None:
            if not pairs:
                return None
            port_avg = sum(item[0] for item in pairs) / len(pairs)
            starboard_avg = sum(item[1] for item in pairs) / len(pairs)
            difference = starboard_avg - port_avg
            return {
                "samples": len(pairs),
                "port_avg": round(port_avg, 4),
                "starboard_avg": round(starboard_avg, 4),
                "starboard_minus_port": round(difference, 4),
                "starboard_percent_vs_port": round(difference / port_avg * 100.0, 2) if abs(port_avg) > 1e-9 else None,
            }

        side_comparisons.append(
            {
                "port_measurement": port_name,
                "starboard_measurement": starboard_name,
                "all_aligned_samples": comparison_payload(all_pairs),
                "rpm_matched_within_50": comparison_payload(rpm_matched_pairs),
            }
        )

    return {
        "correlations": correlations[:20],
        "notable_snapshots": snapshots,
        "threshold_analysis": threshold_results,
        "side_comparisons": side_comparisons,
    }


def engine_running_history_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    running_times = {
        str(row.get("_time") or "")
        for row in rows
        if str(row.get("_measurement", "")).endswith(".revolutions")
        and (safe_float(row.get("_value")) or 0.0) >= ENGINE_RUNNING_RPM
    }
    return [row for row in rows if str(row.get("_time") or "") in running_times]


def history_quality_flags(summary: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    for measurement, stats in summary.items():
        count = int(stats.get("count") or 0)
        minimum = safe_float(stats.get("min"))
        maximum = safe_float(stats.get("max"))
        if measurement.endswith((".engineLoad", ".engineTorque", ".trimState")) and minimum is not None and minimum < 0:
            flags.append(
                {
                    "measurement": measurement,
                    "flag": "out_of_range",
                    "detail": f"Observed minimum {minimum}%; expected ratio-derived values are normally 0-100%.",
                }
            )
        if count >= 5 and minimum is not None and maximum is not None and abs(maximum - minimum) < 1e-9:
            flags.append(
                {
                    "measurement": measurement,
                    "flag": "constant",
                    "detail": f"All {count} aggregated samples were {minimum}; treat this signal as potentially stale or unsupported.",
                }
            )
    return flags


def resolve_history_days(message: str, default_days: int = 7) -> int:
    lower = message.lower()
    if any(term in lower for term in ["last 365", "past 365", "year", "season", "annual", "ytd"]):
        return 365
    if any(term in lower for term in ["last 90", "past 90", "90d", "90 d"]):
        return 90
    if any(term in lower for term in ["last 30", "past 30", "30d", "30 d", "month"]):
        return 30
    if any(term in lower for term in ["last 14", "past 14", "14d", "14 d", "2 week", "two week", "fortnight"]):
        return 14
    if any(term in lower for term in ["last 24", "past 24", "24h", "24 h", "today"]):
        return 1
    if any(term in lower for term in ["last 7", "past 7", "7d", "7 d", "week"]):
        return 7
    return default_days


def generic_influx_history_summary(message: str, limit: int = 24, include_numeric: bool | None = None) -> dict[str, Any]:
    tokens = message_tokens(message)
    plan = query_planner.build_query_plan(message)
    concepts = telemetry_concepts(message) | set(plan["signals"])
    overview = telemetry_overview_requested(message)
    if include_numeric is None:
        include_numeric = not overview
    window = plan.get("window") or resolve_fuel_usage_window(message)
    interval, interval_minutes = history_interval_for_window(window)
    buckets: dict[str, Any] = {}
    for bucket in INFLUX_HISTORY_BUCKETS:
        try:
            measurements = influx_measurements(bucket)
        except Exception as exc:
            buckets[bucket] = {"error": str(exc)}
            continue
        matched = [item for item in measurements if influx_measurement_matches(item, message)] if tokens or telemetry_concepts(message) else []
        if overview and not matched:
            matched = measurements
        if bucket == INFLUX_HISTORY_BUCKET and "trip" in concepts:
            matched.extend(item for item in TRIP_CORE_PATHS if item in measurements and item not in matched)
        matched = sort_measurements_for_query(matched, message)
        if (
            bucket == INFLUX_HISTORY_BUCKET
            and complex_history_requested(f" {message.lower()} ")
            and any(item.startswith("propulsion.") for item in matched)
        ):
            support = [
                item
                for item in [
                    "propulsion.port.revolutions",
                    "propulsion.starboard.revolutions",
                ]
                if item in measurements and measurement_side_allowed(item, requested_engine_sides(message))
            ]
            matched.extend(item for item in support if item not in matched)
        bucket_limit = min(limit, 12) if bucket == INFLUX_HOME_ASSISTANT_BUCKET and INFLUX_HISTORY_BUCKET in buckets else limit
        selected = matched[:bucket_limit]
        bucket_payload: dict[str, Any] = {
            "measurement_count": len(measurements),
            "matched_count": len(matched),
            "matched_measurements": selected,
            "truncated": len(matched) > limit,
        }
        ha_numeric_needed = bool(
            concepts.intersection({"alarm", "bilge", "freshness", "fuel_level", "generator", "humidity", "position", "service", "shore_power", "tank", "tide", "trip", "weather"})
        )
        if selected and include_numeric and (bucket != INFLUX_HOME_ASSISTANT_BUCKET or ha_numeric_needed or not buckets.get(INFLUX_HISTORY_BUCKET, {}).get("matched_count")):
            measurement_filter = " or ".join([f'r._measurement == "{item}"' for item in selected])
            flux = f'''
import "types"
from(bucket: "{bucket}")
  |> range(start: {flux_time(window["start"])}, stop: {flux_time(window["stop"])})
  |> filter(fn: (r) => {measurement_filter})
  |> filter(fn: (r) => r._field == "value")
  |> filter(fn: (r) => types.isType(v: r._value, type: "float") or types.isType(v: r._value, type: "int") or types.isType(v: r._value, type: "uint"))
  |> aggregateWindow(every: {interval}, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_measurement", "_value"])
  |> sort(columns: ["_time"])
'''
            try:
                rows = convert_generic_rows(query_influx(flux))
                chart_series = []
                for measurement in selected[:3]:
                    points = [
                        {"time": row.get("_time"), "value": safe_float(row.get("_value"))}
                        for row in rows
                        if row.get("_measurement") == measurement and safe_float(row.get("_value")) is not None
                    ]
                    if points:
                        stride = max(1, math.ceil(len(points) / 80))
                        chart_series.append({"label": display_measurement_name(measurement), "unit": unit_for_measurement(measurement, {}), "points": points[::stride][-80:]})
                bucket_payload["chart_series"] = chart_series
                bucket_payload["sample_interval"] = f"{interval} mean"
                bucket_payload["numeric_summary"] = summarize_numeric_rows(rows)
                bucket_payload["units"] = {
                    measurement: unit_for_measurement(measurement, {})
                    for measurement in selected
                    if unit_for_measurement(measurement, {})
                }
                bucket_payload["quality_flags"] = history_quality_flags(bucket_payload["numeric_summary"])
                bucket_payload["analysis"] = analyze_history_rows(message, rows, selected, interval_minutes)
                running_rows = engine_running_history_rows(rows)
                if running_rows:
                    bucket_payload["engine_running_numeric_summary"] = summarize_numeric_rows(running_rows)
                    bucket_payload["engine_running_quality_flags"] = history_quality_flags(
                        bucket_payload["engine_running_numeric_summary"]
                    )
                    bucket_payload["engine_running_analysis"] = analyze_history_rows(
                        message,
                        running_rows,
                        selected,
                        interval_minutes,
                    )
                    bucket_payload["engine_running_sample_times"] = len(
                        {str(row.get("_time") or "") for row in running_rows}
                    )
            except Exception as exc:
                bucket_payload["summary_error"] = str(exc)
        buckets[bucket] = bucket_payload
    return {
        "source": "InfluxDB retained buckets",
        "label": window["label"],
        "start_local": window["start_local"],
        "stop_local": window["stop_local"],
        "lookback_days": max(1, math.ceil((window["stop"] - window["start"]).total_seconds() / 86400)),
        "buckets": buckets,
        "trip_summary": window.get("trip_summary"),
    }


def complex_answer_focus(message: str, history: dict[str, Any]) -> dict[str, Any] | None:
    lower = message.lower()
    directions: list[str] = []
    if any(term in lower for term in ["minimum", "lowest", "shallowest"]):
        directions.append("minimum")
    if any(term in lower for term in ["maximum", "highest", "deepest"]):
        directions.append("maximum")
    if not directions:
        return None

    target_groups = [
        (["oil pressure"], ["oilpressure", "oil pressure"]),
        (["boost pressure"], ["boostpressure", "boost pressure"]),
        (["depth", "deepest", "shallowest"], ["depth", "water depth"]),
        (["coolant", "temperature", "temp"], ["temperature", "coolant temp"]),
        (["battery voltage", "voltage"], ["voltage"]),
        (["rpm", "revolutions"], ["revolutions", " rpm"]),
        (["fuel rate", "fuel burn"], ["fuel rate"]),
        (["speed"], ["speed over ground", "speed through water", "boat speed"]),
        (["humidity"], ["humidity"]),
        (["barometric", "barometer"], ["barometric pressure"]),
    ]
    target_terms = next(
        (measurement_terms for query_terms, measurement_terms in target_groups if any(term in lower for term in query_terms)),
        [],
    )
    for bucket_name in [INFLUX_HISTORY_BUCKET, INFLUX_HOME_ASSISTANT_BUCKET]:
        bucket = (history.get("buckets") or {}).get(bucket_name, {})
        if not isinstance(bucket, dict):
            continue
        all_summary = bucket.get("numeric_summary") if isinstance(bucket.get("numeric_summary"), dict) else {}
        running_summary = (
            bucket.get("engine_running_numeric_summary")
            if isinstance(bucket.get("engine_running_numeric_summary"), dict)
            else {}
        )
        candidates = [
            measurement
            for measurement in all_summary
            if not target_terms
            or any(term in normalize_match_text(measurement) for term in target_terms)
        ]
        if not candidates:
            continue
        candidates.sort(key=lambda item: (-semantic_match_score(item, message), measurement_priority(item), item))
        measurement = candidates[0]
        use_running = measurement.startswith("propulsion.") and measurement in running_summary
        summary = running_summary if use_running else all_summary
        analysis_key = "engine_running_analysis" if use_running else "analysis"
        analysis = bucket.get(analysis_key) if isinstance(bucket.get(analysis_key), dict) else {}
        units = bucket.get("units") if isinstance(bucket.get("units"), dict) else {}
        extrema: list[dict[str, Any]] = []
        for direction in directions:
            stat_key = "min" if direction == "minimum" else "max"
            time_key = f"{stat_key}_time_local"
            snapshot_reason = f"{measurement} {direction}"
            snapshot = next(
                (
                    item
                    for item in analysis.get("notable_snapshots", [])
                    if isinstance(item, dict) and item.get("reason") == snapshot_reason
                ),
                None,
            )
            aligned_values = snapshot.get("values") if isinstance(snapshot, dict) else None
            aligned_display = (
                {
                    name: {
                        "value": format_stat_value(value),
                        "unit": unit_for_measurement(name, {}),
                    }
                    for name, value in aligned_values.items()
                }
                if isinstance(aligned_values, dict)
                else None
            )
            raw_value = summary.get(measurement, {}).get(stat_key)
            extrema.append(
                {
                    "direction": direction,
                    "value": format_stat_value(raw_value),
                    "unit": units.get(measurement) or unit_for_measurement(measurement, {}),
                    "time_local": summary.get(measurement, {}).get(time_key),
                    "engine_running_only": use_running,
                    "aligned_values": aligned_display,
                }
            )
        quality_key = "engine_running_quality_flags" if use_running else "quality_flags"
        return {
            "instruction": "Lead with every requested extremum value, unit, and time before aligned signals.",
            "measurement": measurement,
            "source_bucket": bucket_name,
            "window": history.get("label"),
            "extrema": extrema,
            "quality_flags": bucket.get(quality_key, []),
        }
    return None


def resolve_battery_history_days(message: str) -> int:
    lower = message.lower()
    if any(term in lower for term in ["last 90", "past 90", "90d", "90 d"]):
        return 90
    if any(term in lower for term in ["last 30", "past 30", "30d", "30 d", "month"]):
        return 30
    if any(term in lower for term in ["last 14", "past 14", "14d", "14 d", "2 week", "two week", "fortnight"]):
        return 14
    if any(term in lower for term in ["last 7", "past 7", "7d", "7 d", "last week", "past week"]):
        return 7
    if any(term in lower for term in ["last 24", "past 24", "24h", "24 h", "today"]):
        return 1
    return 14


def resolve_solar_history_days(message: str) -> int:
    lower = message.lower()
    numeric = re.search(r"\b(?:last|past)\s+(\d+)\s*(?:days?|d)\b", lower)
    if numeric:
        return max(1, min(int(numeric.group(1)), 365))
    if any(term in lower for term in ["today", "last 24", "past 24", "24h", "24 h"]):
        return 1
    if any(term in lower for term in ["last week", "past week", "last 7", "past 7", "7d", "7 d"]):
        return 7
    if any(term in lower for term in ["last month", "past month", "last 30", "past 30", "30d", "30 d"]):
        return 30
    return 30


def solar_inference_summary(message: str) -> dict[str, Any]:
    days = resolve_solar_history_days(message)
    key = f"{days}d"
    cached = telemetry_cache.get_summary("solar_inference", key, max_age_seconds=TELEMETRY_CACHE_FRESH_SECONDS)
    if cached:
        return cached
    return telemetry_cache.power_tracking_summary(days)


def battery_voltage_summary(days: int = 14, use_cache: bool = True) -> dict[str, Any]:
    days = max(1, min(int(days), 365))
    cache_key = battery_voltage_cache_key(days)
    if use_cache:
        cached = telemetry_cache.get_summary("battery_voltage", cache_key, max_age_seconds=TELEMETRY_CACHE_FRESH_SECONDS)
        if cached:
            return cached
    flux = f'''
from(bucket: "{INFLUX_HISTORY_BUCKET}")
  |> range(start: -{days}d)
  |> filter(fn: (r) => r._measurement == "electrical.batteries.shunt.voltage" or r._measurement == "electrical.batteries.shunt.capacity.stateOfCharge" or r._measurement == "electrical.batteries.shunt.current")
  |> filter(fn: (r) => r._field == "value")
  |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_measurement", "_value"])
  |> pivot(rowKey: ["_time"], columnKey: ["_measurement"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''
    rows = query_influx(flux)
    readings: list[dict[str, Any]] = []
    for row in rows:
        voltage = safe_float(row.get("electrical.batteries.shunt.voltage"))
        if voltage is None:
            continue
        soc_raw = safe_float(row.get("electrical.batteries.shunt.capacity.stateOfCharge"))
        soc = soc_raw * 100.0 if soc_raw is not None and soc_raw <= 1.2 else soc_raw
        current = safe_float(row.get("electrical.batteries.shunt.current"))
        readings.append(
            {
                "time_utc": row.get("_time"),
                "time_local": format_local_time(row.get("_time")),
                "voltage": voltage,
                "smartshunt_soc": soc,
                "current": current,
            }
        )

    voltages = [float(item["voltage"]) for item in readings]
    socs = [float(item["smartshunt_soc"]) for item in readings if item.get("smartshunt_soc") is not None]
    currents = [float(item["current"]) for item in readings if item.get("current") is not None]
    min_voltage_reading = min(readings, key=lambda item: item["voltage"]) if readings else None
    max_voltage_reading = max(readings, key=lambda item: item["voltage"]) if readings else None
    latest_reading = readings[-1] if readings else None
    charging_or_float_samples = sum(
        1
        for item in readings
        if float(item["voltage"]) >= 13.0 or (item.get("current") is not None and float(item["current"]) > 0.2)
    )
    voltage_stats = numeric_stats(voltages, digits=3)
    summary = {
        "label": f"last {days} days",
        "lookback_days": days,
        "source_bucket": INFLUX_HISTORY_BUCKET,
        "sample_interval": "1h mean",
        "samples": len(readings),
        "first_sample_local": readings[0]["time_local"] if readings else None,
        "last_sample_local": latest_reading.get("time_local") if latest_reading else None,
        "voltage": {
            **voltage_stats,
            "min_time_local": min_voltage_reading.get("time_local") if min_voltage_reading else None,
            "max_time_local": max_voltage_reading.get("time_local") if max_voltage_reading else None,
        },
        "smartshunt_soc": numeric_stats(socs, digits=1),
        "current": numeric_stats(currents, digits=3),
        "agm_voltage_soc_estimate": {
            "min_voltage_percent": agm_soc_from_voltage(voltage_stats.get("min")),
            "latest_voltage_percent": agm_soc_from_voltage(voltage_stats.get("latest")),
            "note": "AGM voltage-to-SOC is a resting-voltage reference; charging, float mode, and active loads can make it misleading.",
        },
        "thresholds": {
            "below_11_8_samples": sum(1 for voltage in voltages if voltage < 11.8),
            "below_12_05_samples": sum(1 for voltage in voltages if voltage < 12.05),
            "at_or_above_13_0_samples": sum(1 for voltage in voltages if voltage >= 13.0),
            "charging_or_float_samples": charging_or_float_samples,
        },
    }
    telemetry_cache.put_summary("battery_voltage", cache_key, summary)
    return summary


def engine_run_history_summary(
    direction: str = "first",
    days: int = 365,
    rpm_threshold: float = ENGINE_RUNNING_RPM,
    use_cache: bool = True,
) -> dict[str, Any]:
    cache_key = engine_run_cache_key(direction, days, rpm_threshold)
    if use_cache:
        max_age = TELEMETRY_CACHE_STATIC_SECONDS if direction == "first" else TELEMETRY_CACHE_FRESH_SECONDS
        cached = telemetry_cache.get_summary("engine_run_history", cache_key, max_age_seconds=max_age)
        if cached:
            return cached
    threshold_rev_s = rpm_threshold / 60.0
    desc = "true" if direction == "last" else "false"
    flux = f'''
from(bucket: "{INFLUX_HISTORY_BUCKET}")
  |> range(start: -{int(days)}d)
  |> filter(fn: (r) => r._measurement == "propulsion.port.revolutions" or r._measurement == "propulsion.starboard.revolutions")
  |> filter(fn: (r) => r._field == "value")
  |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
  |> filter(fn: (r) => r._value >= {threshold_rev_s})
  |> group()
  |> sort(columns: ["_time"], desc: {desc})
  |> limit(n: 20)
'''
    rows = query_influx(flux)
    samples: list[dict[str, Any]] = []
    for row in rows:
        rpm = safe_float(row.get("_value"))
        if rpm is None:
            continue
        measurement = row.get("_measurement", "")
        side = "port" if ".port." in measurement else "starboard" if ".starboard." in measurement else measurement
        samples.append(
            {
                "time_utc": row.get("_time"),
                "time_local": format_local_time(row.get("_time")),
                "side": side,
                "rpm": round(rpm * 60.0, 1),
            }
        )
    first = samples[0] if samples else None
    same_minute = [sample for sample in samples if first and sample["time_utc"] == first["time_utc"]]
    summary = {
        "direction": direction,
        "lookback_days": days,
        "source_bucket": INFLUX_HISTORY_BUCKET,
        "rpm_threshold": rpm_threshold,
        "selected": first,
        "same_minute_samples": same_minute,
        "sample_count_returned": len(samples),
        "interpretation_note": (
            "Retained 1-minute downsample where either engine RPM exceeded threshold; "
            "not necessarily vessel lifetime history."
        ),
    }
    telemetry_cache.put_summary("engine_run_history", cache_key, summary)
    return summary


def engine_first_run_summary(days: int = 365, rpm_threshold: float = ENGINE_RUNNING_RPM) -> dict[str, Any]:
    summary = engine_run_history_summary("first", days=days, rpm_threshold=rpm_threshold)
    summary["first"] = summary.get("selected")
    return summary


def flux_time(value: dt.datetime) -> str:
    utc = value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return f'time(v: "{utc}")'


def last_trip_window(now: dt.datetime | None = None) -> dict[str, Any] | None:
    now = now or dt.datetime.now(LOCAL_TZ)
    state = get_ha_states(["input_text.last_trip_summary"]).get("input_text.last_trip_summary", {})
    summary = str(state.get("state", ""))
    match = re.search(r"\b(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2}):\s*(\d+)\s*min\b", summary)
    if not match:
        return None
    month, day, hour, minute, duration_minutes = (int(value) for value in match.groups())
    stop = dt.datetime(now.year, month, day, hour, minute, tzinfo=LOCAL_TZ)
    if stop > now + dt.timedelta(days=1):
        stop = stop.replace(year=stop.year - 1)
    start = stop - dt.timedelta(minutes=duration_minutes)
    return {
        "label": "last recorded trip",
        "cache_key": f"last_trip:{start.isoformat()}:{stop.isoformat()}",
        "start": start,
        "stop": stop,
        "start_local": start.strftime("%Y-%m-%d %H:%M %Z"),
        "stop_local": stop.strftime("%Y-%m-%d %H:%M %Z"),
        "start_utc": start.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "stop_utc": stop.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "trip_summary": summary,
    }


def resolve_fuel_usage_window(message: str, now: dt.datetime | None = None) -> dict[str, Any]:
    lower = message.lower()
    now = now or dt.datetime.now(LOCAL_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=LOCAL_TZ)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if "last trip" in lower or "previous trip" in lower:
        trip = last_trip_window(now)
        if trip:
            return trip

    numeric_window = re.search(r"\b(?:last|past)\s+(\d+)\s*(hours?|hrs?|days?|weeks?|months?|years?)\b", lower)
    if numeric_window:
        count = max(1, int(numeric_window.group(1)))
        unit = numeric_window.group(2)
        if unit.startswith(("hour", "hr")):
            delta = dt.timedelta(hours=min(count, 24 * 365))
            unit_label = "hour" if count == 1 else "hours"
        elif unit.startswith("week"):
            delta = dt.timedelta(days=min(count * 7, 365))
            unit_label = "week" if count == 1 else "weeks"
        elif unit.startswith("month"):
            delta = dt.timedelta(days=min(count * 30, 365))
            unit_label = "month" if count == 1 else "months"
        elif unit.startswith("year"):
            delta = dt.timedelta(days=min(count * 365, 365))
            unit_label = "year" if count == 1 else "years"
        else:
            delta = dt.timedelta(days=min(count, 365))
            unit_label = "day" if count == 1 else "days"
        start = now - delta
        stop = now
        label = f"last {count} {unit_label}"
        cache_key = f"rolling:{int(delta.total_seconds())}s"
    elif "yesterday" in lower:
        start = today_start - dt.timedelta(days=1)
        stop = today_start
        label = "yesterday"
        cache_key = f"yesterday:{start.date().isoformat()}"
    elif "today" in lower:
        start = today_start
        stop = now
        label = "today"
        cache_key = f"today:{today_start.date().isoformat()}"
    elif "weekend" in lower:
        weekday = now.weekday()
        days_since_saturday = (weekday - 5) % 7
        if "last weekend" in lower and weekday >= 5:
            days_since_saturday += 7
        start = today_start - dt.timedelta(days=days_since_saturday)
        if weekday < 5 or "last weekend" in lower:
            stop = start + dt.timedelta(days=2)
            label = "last completed weekend" if "last" not in lower else "last weekend"
        else:
            stop = now
            label = "this weekend so far"
        stop_key = stop.date().isoformat() if stop.hour == 0 and stop.minute == 0 and stop.second == 0 else "so_far"
        cache_key = f"weekend:{start.date().isoformat()}:{stop_key}"
    elif any(term in lower for term in ["last 24", "past 24", "24h", "24 h"]):
        start = now - dt.timedelta(hours=24)
        stop = now
        label = "last 24 hours"
        cache_key = "rolling:last_24h"
    elif any(term in lower for term in ["last 7", "past 7", "7d", "7 d", "last week"]):
        start = now - dt.timedelta(days=7)
        stop = now
        label = "last 7 days"
        cache_key = "rolling:last_7d"
    elif any(term in lower for term in ["last 30", "past 30", "30d", "30 d"]):
        start = now - dt.timedelta(days=30)
        stop = now
        label = "last 30 days"
        cache_key = "rolling:last_30d"
    elif any(term in lower for term in ["last 90", "past 90", "90d", "90 d"]):
        start = now - dt.timedelta(days=90)
        stop = now
        label = "last 90 days"
        cache_key = "rolling:last_90d"
    elif "this week" in lower:
        start = today_start - dt.timedelta(days=now.weekday())
        stop = now
        label = "this week"
        cache_key = f"this_week:{start.date().isoformat()}"
    elif "last month" in lower:
        this_month = today_start.replace(day=1)
        stop = this_month
        start = (this_month - dt.timedelta(days=1)).replace(day=1)
        label = "last month"
        cache_key = f"last_month:{start.strftime('%Y-%m')}"
    elif "this month" in lower:
        start = today_start.replace(day=1)
        stop = now
        label = "this month"
        cache_key = f"this_month:{start.strftime('%Y-%m')}"
    elif "last year" in lower:
        start = today_start.replace(year=today_start.year - 1, month=1, day=1)
        stop = today_start.replace(month=1, day=1)
        label = "last year"
        cache_key = f"last_year:{start.year}"
    elif any(term in lower for term in ["this year", "year to date", "ytd", "season", "annual"]):
        start = today_start.replace(month=1, day=1)
        stop = now
        label = "this year"
        cache_key = f"this_year:{start.year}"
    else:
        start = now - dt.timedelta(days=7)
        stop = now
        label = "last 7 days"
        cache_key = "rolling:last_7d"

    return {
        "label": label,
        "cache_key": cache_key,
        "start": start,
        "stop": stop,
        "start_local": start.strftime("%Y-%m-%d %H:%M %Z"),
        "stop_local": stop.strftime("%Y-%m-%d %H:%M %Z"),
        "start_utc": start.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "stop_utc": stop.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def fuel_usage_summary(
    window: dict[str, Any],
    rpm_threshold: float = ENGINE_RUNNING_RPM,
    use_cache: bool = True,
) -> dict[str, Any]:
    cache_key = fuel_usage_cache_key(window, rpm_threshold)
    if use_cache:
        stop = window.get("stop")
        now = dt.datetime.now(LOCAL_TZ)
        window_closed = isinstance(stop, dt.datetime) and stop < now - dt.timedelta(minutes=5)
        max_age = TELEMETRY_CACHE_STATIC_SECONDS if window_closed else TELEMETRY_CACHE_FRESH_SECONDS
        cached = telemetry_cache.get_summary("fuel_usage", cache_key, max_age_seconds=max_age)
        if cached:
            return cached
    start = window["start"]
    stop = window["stop"]
    flux = f'''
from(bucket: "{INFLUX_HISTORY_BUCKET}")
  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})
  |> filter(fn: (r) => r._measurement == "propulsion.port.fuel.rate" or r._measurement == "propulsion.starboard.fuel.rate" or r._measurement == "propulsion.port.revolutions" or r._measurement == "propulsion.starboard.revolutions")
  |> filter(fn: (r) => r._field == "value")
  |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_measurement", "_value"])
  |> pivot(rowKey: ["_time"], columnKey: ["_measurement"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''
    rows = query_influx(flux)
    running: list[dict[str, Any]] = []
    port_liters = 0.0
    stbd_liters = 0.0
    port_fuel_samples = 0
    stbd_fuel_samples = 0
    for row in rows:
        port_rpm_raw = safe_float(row.get("propulsion.port.revolutions"))
        stbd_rpm_raw = safe_float(row.get("propulsion.starboard.revolutions"))
        port_fuel_raw = safe_float(row.get("propulsion.port.fuel.rate"))
        stbd_fuel_raw = safe_float(row.get("propulsion.starboard.fuel.rate"))
        port_rpm = port_rpm_raw * 60.0 if port_rpm_raw is not None else None
        stbd_rpm = stbd_rpm_raw * 60.0 if stbd_rpm_raw is not None else None
        if (port_rpm or 0.0) < rpm_threshold and (stbd_rpm or 0.0) < rpm_threshold:
            continue
        if port_fuel_raw is not None:
            port_liters += (port_fuel_raw * 3600000.0) / 60.0
            port_fuel_samples += 1
        if stbd_fuel_raw is not None:
            stbd_liters += (stbd_fuel_raw * 3600000.0) / 60.0
            stbd_fuel_samples += 1
        running.append(
            {
                "time": row.get("_time", ""),
                "port_rpm": round(port_rpm or 0.0, 1),
                "starboard_rpm": round(stbd_rpm or 0.0, 1),
            }
        )

    running_hours = len(running) / 60.0
    total_liters = port_liters + stbd_liters
    summary = {
        "label": window["label"],
        "source_bucket": INFLUX_HISTORY_BUCKET,
        "rpm_threshold": rpm_threshold,
        "start_local": window["start_local"],
        "stop_local": window["stop_local"],
        "start_utc": window["start_utc"],
        "stop_utc": window["stop_utc"],
        "running_minutes": len(running),
        "first_running_sample_utc": running[0]["time"] if running else None,
        "last_running_sample_utc": running[-1]["time"] if running else None,
        "first_running_sample_local": format_local_time(str(running[0]["time"])) if running else None,
        "last_running_sample_local": format_local_time(str(running[-1]["time"])) if running else None,
        "totals": {
            "port_liters": round(port_liters, 2),
            "starboard_liters": round(stbd_liters, 2),
            "total_liters": round(total_liters, 2),
            "port_gallons": round(port_liters * US_GALLONS_PER_LITER, 2),
            "starboard_gallons": round(stbd_liters * US_GALLONS_PER_LITER, 2),
            "total_gallons": round(total_liters * US_GALLONS_PER_LITER, 2),
        },
        "averages": {
            "port_gph_running": round(port_liters * US_GALLONS_PER_LITER / running_hours, 3) if running_hours else None,
            "starboard_gph_running": round(stbd_liters * US_GALLONS_PER_LITER / running_hours, 3) if running_hours else None,
            "total_gph_running": round(total_liters * US_GALLONS_PER_LITER / running_hours, 3) if running_hours else None,
        },
        "sample_counts": {
            "running_minutes": len(running),
            "port_fuel_samples": port_fuel_samples,
            "starboard_fuel_samples": stbd_fuel_samples,
        },
        "notes": [
            "Fuel totals integrate 1-minute average fuel-rate samples while either engine RPM exceeds threshold.",
            "Fuel rate is integrated by minute and reported in US gallons (gal, gal/h).",
        ],
    }
    telemetry_cache.put_summary("fuel_usage", cache_key, summary)
    return summary


def resolve_rpm_band(message: str) -> tuple[float, float] | None:
    match = re.search(
        r"\bbetween\s+(\d+(?:\.\d+)?)\s*(?:and|-|to)\s*(\d+(?:\.\d+)?)\s*rpm\b",
        message.lower(),
    )
    if not match:
        return None
    low, high = (float(value) for value in match.groups())
    return (min(low, high), max(low, high))


def fuel_economy_summary(
    days: int = 365,
    window: dict[str, Any] | None = None,
    rpm_threshold: float = ENGINE_RUNNING_RPM,
    rpm_band: tuple[float, float] | None = None,
    require_both_engines: bool = False,
    use_cache: bool = True,
) -> dict[str, Any]:
    cache_key = fuel_usage_cache_key(window, rpm_threshold) if window else fuel_economy_cache_key(days, rpm_threshold)
    if rpm_band:
        cache_key += f":{rpm_band[0]:g}-{rpm_band[1]:g}rpm:{'both' if require_both_engines else 'either'}"
    if use_cache:
        cached = telemetry_cache.get_summary("fuel_economy", cache_key, max_age_seconds=TELEMETRY_CACHE_FRESH_SECONDS)
        if cached:
            return cached
    bucket = INFLUX_HISTORY_BUCKET
    if window:
        range_clause = f"range(start: {flux_time(window['start'])}, stop: {flux_time(window['stop'])})"
        label = window["label"]
        lookback_days: int | None = None
    else:
        range_clause = f"range(start: -{int(days)}d)"
        label = f"last {days} days"
        lookback_days = days
    flux = f'''
from(bucket: "{bucket}")
  |> {range_clause}
  |> filter(fn: (r) => r._measurement == "propulsion.port.fuel.rate" or r._measurement == "propulsion.starboard.fuel.rate" or r._measurement == "propulsion.port.revolutions" or r._measurement == "propulsion.starboard.revolutions" or r._measurement == "navigation.speedOverGround")
  |> filter(fn: (r) => r._field == "value")
  |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_measurement", "_value"])
  |> pivot(rowKey: ["_time"], columnKey: ["_measurement"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''
    rows = query_influx(flux)
    running_minutes = 0
    fuel_liters = 0.0
    distance_nm = 0.0
    distance_samples = 0
    first_running: str | None = None
    last_running: str | None = None
    for row in rows:
        port_rpm_raw = safe_float(row.get("propulsion.port.revolutions"))
        stbd_rpm_raw = safe_float(row.get("propulsion.starboard.revolutions"))
        port_fuel_raw = safe_float(row.get("propulsion.port.fuel.rate"))
        stbd_fuel_raw = safe_float(row.get("propulsion.starboard.fuel.rate"))
        speed_mps = safe_float(row.get("navigation.speedOverGround"))
        port_rpm = port_rpm_raw * 60.0 if port_rpm_raw is not None else None
        stbd_rpm = stbd_rpm_raw * 60.0 if stbd_rpm_raw is not None else None
        if rpm_band:
            low, high = rpm_band
            port_in_band = port_rpm is not None and low <= port_rpm <= high
            stbd_in_band = stbd_rpm is not None and low <= stbd_rpm <= high
            if (require_both_engines and not (port_in_band and stbd_in_band)) or (
                not require_both_engines and not (port_in_band or stbd_in_band)
            ):
                continue
        if (port_rpm or 0.0) < rpm_threshold and (stbd_rpm or 0.0) < rpm_threshold:
            continue
        running_minutes += 1
        first_running = first_running or row.get("_time")
        last_running = row.get("_time")
        if port_fuel_raw is not None:
            fuel_liters += (port_fuel_raw * 3600000.0) / 60.0
        if stbd_fuel_raw is not None:
            fuel_liters += (stbd_fuel_raw * 3600000.0) / 60.0
        if speed_mps is not None and speed_mps >= 0:
            distance_nm += speed_mps * 60.0 / 1852.0
            distance_samples += 1

    fuel_gallons = fuel_liters * US_GALLONS_PER_LITER
    running_hours = running_minutes / 60.0
    summary = {
        "label": label,
        "source_bucket": bucket,
        "rpm_threshold": rpm_threshold,
        "rpm_filter": {
            "minimum_rpm": rpm_band[0],
            "maximum_rpm": rpm_band[1],
            "require_both_engines": require_both_engines,
        }
        if rpm_band
        else None,
        "lookback_days": lookback_days,
        "start_local": window.get("start_local") if window else None,
        "stop_local": window.get("stop_local") if window else None,
        "running_minutes": running_minutes,
        "first_running_sample_utc": first_running,
        "last_running_sample_utc": last_running,
        "first_running_sample_local": format_local_time(first_running),
        "last_running_sample_local": format_local_time(last_running),
        "totals": {
            "fuel_liters": round(fuel_liters, 2),
            "fuel_gallons": round(fuel_gallons, 2),
            "distance_nm": round(distance_nm, 2),
        },
        "economy": {
            "nm_per_gallon": round(distance_nm / fuel_gallons, 3) if fuel_gallons > 0 else None,
            "gallons_per_nm": round(fuel_gallons / distance_nm, 3) if distance_nm > 0 else None,
            "avg_speed_knots_running": round(distance_nm / running_hours, 2) if running_hours > 0 else None,
        },
        "sample_counts": {
            "running_minutes": running_minutes,
            "distance_samples": distance_samples,
        },
        "notes": [
            "Fuel economy integrates fuel-rate and speed-over-ground samples only while either engine RPM exceeds threshold.",
            "Distance is estimated from navigation.speedOverGround, so current/tide and GPS noise can affect the result.",
        ],
    }
    telemetry_cache.put_summary("fuel_economy", cache_key, summary)
    return summary


def fuel_balance_summary(hours: int = 168, rpm_threshold: float = ENGINE_RUNNING_RPM) -> dict[str, Any]:
    bucket = history_bucket_for_hours(hours)
    flux = f'''
from(bucket: "{bucket}")
  |> range(start: -{int(hours)}h)
  |> filter(fn: (r) => r._measurement == "propulsion.port.fuel.rate" or r._measurement == "propulsion.starboard.fuel.rate" or r._measurement == "propulsion.port.revolutions" or r._measurement == "propulsion.starboard.revolutions")
  |> filter(fn: (r) => r._field == "value")
  |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_measurement", "_value"])
  |> pivot(rowKey: ["_time"], columnKey: ["_measurement"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''
    rows = query_influx(flux)
    running: list[dict[str, float | str]] = []
    for row in rows:
        port_rpm_raw = safe_float(row.get("propulsion.port.revolutions"))
        stbd_rpm_raw = safe_float(row.get("propulsion.starboard.revolutions"))
        port_fuel_raw = safe_float(row.get("propulsion.port.fuel.rate"))
        stbd_fuel_raw = safe_float(row.get("propulsion.starboard.fuel.rate"))
        port_rpm = port_rpm_raw * 60.0 if port_rpm_raw is not None else None
        stbd_rpm = stbd_rpm_raw * 60.0 if stbd_rpm_raw is not None else None
        if (port_rpm or 0.0) < rpm_threshold and (stbd_rpm or 0.0) < rpm_threshold:
            continue
        if port_fuel_raw is None or stbd_fuel_raw is None:
            continue
        running.append(
            {
                "time": row.get("_time", ""),
                "port_rpm": round(port_rpm or 0.0, 4),
                "starboard_rpm": round(stbd_rpm or 0.0, 4),
                "port_fuel_gph": round(port_fuel_raw * 951019.4, 4),
                "starboard_fuel_gph": round(stbd_fuel_raw * 951019.4, 4),
            }
        )

    def avg(key: str) -> float | None:
        values = [float(row[key]) for row in running if row.get(key) is not None]
        return round(sum(values) / len(values), 3) if values else None

    port_fuel = avg("port_fuel_gph")
    stbd_fuel = avg("starboard_fuel_gph")
    port_rpm = avg("port_rpm")
    stbd_rpm = avg("starboard_rpm")
    derived: dict[str, Any] = {}
    if port_fuel is not None and stbd_fuel is not None:
        diff = stbd_fuel - port_fuel
        derived["starboard_minus_port_fuel_gph"] = round(diff, 3)
        derived["starboard_fuel_percent_vs_port"] = round((diff / port_fuel) * 100, 1) if abs(port_fuel) > 0.1 else None
        derived["starboard_minus_port_total_gallons"] = round(diff * (len(running) / 60.0), 2)
    if port_rpm is not None and stbd_rpm is not None:
        derived["starboard_minus_port_rpm"] = round(stbd_rpm - port_rpm, 1)

    confidence = "none"
    if len(running) >= 30:
        confidence = "high"
    elif len(running) >= 10:
        confidence = "medium"
    elif running:
        confidence = "low"

    return {
        "window_hours": hours,
        "source_bucket": bucket,
        "rpm_threshold": rpm_threshold,
        "running_minutes": len(running),
        "first_running_sample_utc": running[0]["time"] if running else None,
        "last_running_sample_utc": running[-1]["time"] if running else None,
        "first_running_sample_local": format_local_time(str(running[0]["time"])) if running else None,
        "last_running_sample_local": format_local_time(str(running[-1]["time"])) if running else None,
        "averages": {
            "port_fuel_gph": port_fuel,
            "starboard_fuel_gph": stbd_fuel,
            "port_rpm": port_rpm,
            "starboard_rpm": stbd_rpm,
        },
        "derived": derived,
        "confidence": confidence,
        "notes": [
            "Fuel comparison only includes 1-minute samples where either engine RPM exceeded threshold.",
            "Fuel rate is converted from SignalK m3/s to gal/h.",
        ],
    }


def summarize_numeric_rows(rows: list[dict[str, str]], unit_scale: dict[str, float] | None = None) -> dict[str, dict[str, Any]]:
    unit_scale = unit_scale or {}
    values: dict[str, list[tuple[str, float]]] = {}
    for row in rows:
        measurement = row.get("_measurement") or row.get("_value") or ""
        value = safe_float(row.get("_value"))
        if not measurement or value is None:
            continue
        values.setdefault(measurement, []).append((str(row.get("_time") or ""), value * unit_scale.get(measurement, 1.0)))
    summary: dict[str, dict[str, Any]] = {}
    for measurement, samples in values.items():
        if not samples:
            continue
        samples.sort(key=lambda item: item[0])
        nums = [value for _timestamp, value in samples]
        min_sample = min(samples, key=lambda item: item[1])
        max_sample = max(samples, key=lambda item: item[1])
        summary[measurement] = {
            "count": len(nums),
            "avg": round(sum(nums) / len(nums), 4),
            "min": round(min_sample[1], 4),
            "min_time_utc": min_sample[0] or None,
            "min_time_local": format_local_time(min_sample[0]) if min_sample[0] else None,
            "max": round(max_sample[1], 4),
            "max_time_utc": max_sample[0] or None,
            "max_time_local": format_local_time(max_sample[0]) if max_sample[0] else None,
            "first": round(samples[0][1], 4),
            "first_time_local": format_local_time(samples[0][0]) if samples[0][0] else None,
            "last": round(nums[-1], 4),
            "last_time_local": format_local_time(samples[-1][0]) if samples[-1][0] else None,
        }
    return summary


def convert_generic_value(measurement: str, value: float) -> float:
    if measurement.startswith("tanks.fuel.") and measurement.endswith(".currentLevel"):
        return value * 100.0
    if "stateOfCharge" in measurement or "state_of_charge" in measurement:
        return value * 100.0 if abs(value) <= 1.2 else value
    if measurement.endswith(".temperature"):
        return (value - 273.15) * 9.0 / 5.0 + 32.0
    if measurement.startswith("environment.depth."):
        return value * 3.280839895
    if "environment" in measurement and "pressure" in measurement.lower():
        return value / 3386.389 if value > 2000 else value
    if measurement.endswith(".oilPressure") or measurement.endswith(".boostPressure") or "pressure" in measurement.lower() and value > 2000:
        return value / 6894.757
    if measurement in {"navigation.speedOverGround", "navigation.speedThroughWater"}:
        return value * 1.94384449
    if any(term in measurement for term in ["headingMagnetic", "courseOverGroundTrue", "attitude.pitch", "attitude.roll", "attitude.yaw"]):
        return value * 180.0 / 3.141592653589793
    if measurement == "navigation.rateOfTurn":
        return value * 180.0 / 3.141592653589793 * 60.0
    if measurement.endswith(".revolutions"):
        return value * 60.0
    if measurement.endswith(".fuel.rate"):
        return value * 951019.4
    if measurement.endswith(".runTime") or measurement.endswith(".timeRemaining"):
        return value / 3600.0
    if measurement.endswith((".engineLoad", ".engineTorque", ".trimState")):
        return value * 100.0
    return value


def convert_generic_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    converted: list[dict[str, str]] = []
    for row in rows:
        measurement = row.get("_measurement") or ""
        value = safe_float(row.get("_value"))
        if not measurement or value is None:
            continue
        item = dict(row)
        item["_value"] = str(convert_generic_value(measurement, value))
        converted.append(item)
    return converted


def propulsion_summary(hours: int = 24) -> dict[str, Any]:
    path_filter = " or ".join([f'r._measurement == "{path}"' for path in PROPULSION_PATHS])
    flux = f'''
from(bucket: "{INFLUX_RAW_BUCKET}")
  |> range(start: -{int(hours)}h)
  |> filter(fn: (r) => {path_filter})
  |> filter(fn: (r) => r._field == "value")
  |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
'''
    rows = query_influx(flux)
    # US units: fuel gal/h, temperature °F, oil pressure psi. Temperature needs
    # add+scale, not multiply; handle all conversions manually here.
    values: dict[str, list[float]] = {}
    for row in rows:
        measurement = row.get("_measurement") or ""
        value = safe_float(row.get("_value"))
        if not measurement or value is None:
            continue
        if measurement.endswith(".fuel.rate"):
            value *= 951019.4
        elif measurement.endswith(".revolutions"):
            value *= 60.0
        elif measurement.endswith(".temperature"):
            value = (value - 273.15) * 9.0 / 5.0 + 32.0
        elif measurement.endswith(".oilPressure") or measurement.endswith(".boostPressure"):
            value /= 6894.757
        values.setdefault(measurement, []).append(value)
    summary = summarize_numeric_rows(
        [{"_measurement": key, "_value": str(value)} for key, nums in values.items() for value in nums]
    )
    port_fuel = summary.get("propulsion.port.fuel.rate", {}).get("avg")
    stbd_fuel = summary.get("propulsion.starboard.fuel.rate", {}).get("avg")
    port_rpm = summary.get("propulsion.port.revolutions", {}).get("avg")
    stbd_rpm = summary.get("propulsion.starboard.revolutions", {}).get("avg")
    derived: dict[str, Any] = {}
    if port_fuel is not None and stbd_fuel is not None:
        diff = stbd_fuel - port_fuel
        derived["starboard_minus_port_fuel_gph"] = round(diff, 2)
        derived["starboard_fuel_percent_vs_port"] = round((diff / port_fuel) * 100, 1) if abs(port_fuel) > 0.1 else None
    if port_rpm is not None and stbd_rpm is not None:
        derived["starboard_minus_port_rpm"] = round(stbd_rpm - port_rpm, 1)
    return {
        "window_hours": hours,
        "units": {
            "fuel.rate": "gal/h",
            "revolutions": "RPM",
            "temperature": "F",
            "oilPressure/boostPressure": "psi",
            "engineLoad/engineTorque/trimState": "source units",
        },
        "summary": summary,
        "derived": derived,
    }


def current_telemetry_snapshot(message: str = "") -> dict[str, Any]:
    signalk = get_signalk_self()
    flat = flatten_signalk(signalk) if "error" not in signalk else {}
    baseline = {
        "navigation.speedOverGround",
        "navigation.courseOverGroundTrue",
        "navigation.headingMagnetic",
        "navigation.position",
        "environment.depth.belowTransducer",
        "electrical.batteries.shunt.capacity.stateOfCharge",
        "electrical.batteries.shunt.voltage",
        "electrical.batteries.shunt.current",
        "electrical.batteries.shunt.power",
        "propulsion.port.revolutions",
        "propulsion.starboard.revolutions",
        "propulsion.port.fuel.rate",
        "propulsion.starboard.fuel.rate",
        "propulsion.port.engineLoad",
        "propulsion.starboard.engineLoad",
        "tanks.fuel.0.currentLevel",
        "tanks.fuel.1.currentLevel",
    }
    current_prefixes = ("electrical.", "environment.", "navigation.", "notifications.", "performance.", "propulsion.", "tanks.")
    concepts = telemetry_concepts(message)
    matched = {
        key
        for key in flat
        if message
        and key.startswith(current_prefixes)
        and semantic_match_score(key, message) > 0
    }
    selected_paths = matched if concepts == {"ais"} else baseline.union(matched)
    values: dict[str, Any] = {}
    units: dict[str, str] = {}
    for key in sorted(selected_paths):
        if key not in flat or key.startswith("notifications."):
            continue
        value = flat[key]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = round(convert_generic_value(key, float(value)), 4)
        values[key] = value
        unit = unit_for_measurement(key, {})
        if unit:
            units[key] = unit

    active_notifications: dict[str, Any] = {}
    normal_notification_count = 0
    for key, value in flat.items():
        if not key.startswith("notifications."):
            continue
        state = value.get("state") if isinstance(value, dict) else value
        normalized = str(state).strip().lower()
        if normalized in {"", "0", "false", "none", "normal", "nominal", "off", "ok"}:
            normal_notification_count += 1
        else:
            active_notifications[key] = value
    quality_flags = [
        {
            "measurement": key,
            "flag": "out_of_range",
            "detail": f"Current value {value}% is outside the expected 0-100% range; treat it as unsupported.",
        }
        for key, value in values.items()
        if key.endswith((".engineLoad", ".engineTorque", ".trimState"))
        and isinstance(value, (int, float))
        and value < 0
    ]

    result: dict[str, Any] = {}
    if "ais" in concepts:
        result["ais"] = current_ais_context()
    result.update({
        "source": "SignalK self vessel",
        "signalk_error": signalk.get("error") if isinstance(signalk, dict) else None,
        "available_path_count": len(flat),
        "matched_path_count": len(matched),
        "values": values,
        "units": units,
        "active_notifications": active_notifications,
        "alarm_summary": (
            f"{len(active_notifications)} active SignalK notification(s)."
            if active_notifications
            else f"No active SignalK notifications; {normal_notification_count} notification paths report normal."
        ),
        "normal_notification_count": normal_notification_count,
        "interpretation_notes": [
            "When RPM is below 200, zero oil pressure and zero fuel rate are expected engine-off values.",
            "Engine-off boost pressure near 14.7 psi is ambient absolute pressure, not active turbo boost.",
        ],
        "quality_flags": quality_flags,
    })
    return result


def search_local_docs(query: str, limit: int = 8) -> list[dict[str, str]]:
    indexed = memory_index.search(query, limit=limit)
    if indexed:
        return [
            {
                "path": str(item["path"]),
                "line": str(item["line"]),
                "title": str(item.get("title", "")),
                "excerpt": str(item["excerpt"]),
                "score": str(item["score"]),
                "retrieval": str(item.get("retrieval", "index")),
            }
            for item in indexed
        ]

    terms = [t.lower() for t in re.findall(r"[a-zA-Z0-9_./:-]{3,}", query)]
    results: list[dict[str, str]] = []
    for path in DOC_PATHS:
        if not path.exists():
            continue
        try:
            lines = path.read_text(errors="ignore").splitlines()
        except Exception:
            continue
        for idx, line in enumerate(lines, start=1):
            lower = line.lower()
            score = sum(1 for term in terms if term in lower)
            if score <= 0:
                continue
            start = max(1, idx - 2)
            end = min(len(lines), idx + 3)
            excerpt = "\n".join(lines[start - 1 : end])
            results.append({"path": str(path), "line": str(idx), "excerpt": excerpt, "score": str(score), "retrieval": "scan"})
    results.sort(key=lambda r: int(r["score"]), reverse=True)
    return results[:limit]


def classify_question(message: str) -> dict[str, Any]:
    lower = message.lower()
    query_plan = query_planner.build_query_plan(message)
    # Word-boundary regexes: plain substring checks made "loa" match "load"
    # and "beam" match "beaming", routing engine questions to the identity answer.
    identity_patterns = [
        r"boat name",
        r"vessel name",
        r"\bwhat boat(?:\s+is\s+this|\s+do\s+i\s+have|\?)",
        r"\bwhich boat(?:\s+is\s+this|\?)",
        r"boat is this",
        r"name of the boat",
        r"\bmmsi\b",
        r"\bcall ?sign\b",
        r"\bloa\b",
        r"\bbeam\b",
        r"\bcutwater\b",
    ]
    engine_terms = any(word in lower for word in ["motor", "motors", "engine", "engines", "rpm"])
    running_terms = any(word in lower for word in ["running", "run", "ran", "started", "start", "stopped", "stop"])
    first_engine_run = (
        any(term in lower for term in ["first time", "earliest", "first detected", "first run", "first running", "first started"])
        and engine_terms
    )
    last_engine_run = (
        any(
            term in lower
            for term in [
                "last time",
                "latest",
                "most recent",
                "recently",
                "last run",
                "last ran",
                "last running",
                "when were",
                "when was",
                "when did",
            ]
        )
        and engine_terms
        and running_terms
        and not first_engine_run
    )
    fuel_terms = any(word in lower for word in ["fuel", "gas", "diesel", "gallon", "gallons", "liter", "liters", "litre", "litres"])
    period_terms = [
        "weekend",
        "today",
        "yesterday",
        "last 24",
        "past 24",
        "24h",
        "24 h",
        "last 7",
        "past 7",
        "7d",
        "7 d",
        "this week",
        "last week",
        "this year",
        "year to date",
        "ytd",
        "annual",
        "season",
        "last 30",
        "past 30",
        "30d",
        "30 d",
        "last 90",
        "past 90",
        "90d",
        "90 d",
    ]
    has_period = any(term in lower for term in period_terms) or bool(
        re.search(r"\b(?:last|past)\s+\d+\s*(?:hours?|hrs?|days?|weeks?|months?|years?)\b", lower)
    ) or any(term in lower for term in ["last trip", "previous trip", "this month", "last month", "last year"])
    economy_terms = any(
        term in lower
        for term in [
            "fuel economy",
            "fuel mileage",
            "miles per gallon",
            "nautical miles per gallon",
            "nm/gal",
            "nmpg",
            "mpg",
            "gallons per mile",
            "gallons per nautical mile",
            "average economy",
            "average fuel",
        ]
    )
    # Fuel balance means comparing engines, not totaling usage. Requiring
    # comparison intent keeps "how much fuel did my engines use" on the
    # fuel_usage path, and word boundaries stop "one" matching "money"/"done"
    # and "port" matching "report".
    fuel_side_terms = bool(re.search(r"\bstarboard\b|\bport\b|\bstbd\b", lower))
    fuel_both_sides = bool(re.search(r"\bstarboard\b", lower)) and bool(re.search(r"\bport\b|\bstbd\b", lower))
    fuel_compare_terms = any(
        term in lower
        for term in [
            "more than",
            "less than",
            "more fuel",
            "less fuel",
            "vs",
            "versus",
            "compare",
            "comparison",
            "balance",
            "difference",
            "different",
            "imbalance",
            "uneven",
            "each engine",
            "per engine",
            "which engine",
            "one engine",
            "one motor",
            "one of the",
        ]
    )
    fuel_balance = (
        any(word in lower for word in ["fuel", "consume", "consuming", "burn", "l/h", "lph"])
        and (fuel_both_sides or ((engine_terms or fuel_side_terms) and fuel_compare_terms))
    )
    fuel_economy = fuel_terms and economy_terms
    fuel_economy_needs_window = fuel_economy and not has_period
    fuel_usage_intent = fuel_terms and any(
        re.search(pattern, lower)
        for pattern in [
            r"\bhow much\s+(?:fuel|gas|diesel)\s+did\b.{0,40}\b(?:use|burn|consume)\b",
            r"\bhow many\s+gallons\b.{0,40}\b(?:use|burn|consume)\b",
            r"\b(?:fuel|gas|diesel)\s+(?:usage|used|burned|burnt|consumed|consumption)\b",
            r"\btotal\s+(?:fuel|gas|diesel)\s+(?:used|burned|burnt|consumed)\b",
        ]
    )
    fuel_usage = fuel_usage_intent and not fuel_balance and not fuel_economy
    fuel_usage_needs_window = fuel_usage and not has_period
    shore_power_history = (
        "shore" in lower
        and "power" in lower
        and any(term in lower for term in ["when", "last", "latest", "turned", "turn", "lost", "off", "on", "connected", "disconnected", "restored"])
    )
    battery_terms = any(word in lower for word in ["battery", "batteries", "soc", "voltage", "volt", "volts"])
    battery_history_terms = history_requested(message) or any(
        term in lower for term in ["below", "under", "above", "low voltage", "voltage drop"]
    )
    battery_voltage_history = battery_terms and battery_history_terms
    concepts = telemetry_concepts(message) | set(query_plan["signals"])
    solar_hardware = "solar" in concepts and any(
        term in lower
        for term in [
            "what should i add",
            "what can i add",
            "what do i need",
            "hardware",
            "install",
            "buy",
            "upgrade",
            "instrument",
        ]
    )
    telemetry_terms = bool(concepts) or any(
        word in lower
        for word in [
            "anchor",
            "alternator",
            "barometric",
            "barometer",
            "bilge",
            "boost",
            "cabin",
            "charge",
            "coolant",
            "course",
            "current",
            "depth",
            "engine",
            "engines",
            "fuel",
            "gps",
            "heading",
            "humidity",
            "influx",
            "latitude",
            "level",
            "load",
            "longitude",
            "oil",
            "position",
            "pressure",
            "range",
            "roll",
            "rpm",
            "soc",
            "speed",
            "tank",
            "temperature",
            "telemetry",
            "sensor",
            "sensors",
            "service",
            "stale",
            "tide",
            "torque",
            "trend",
            "voltage",
            "weather",
            "wind",
        ]
    )
    generic_history = telemetry_terms and (history_requested(message) or bool(query_plan["historical"]))
    complex_history = generic_history and (complex_history_requested(f" {lower} ") or bool(query_plan["complex"]))
    fuel_balance_complex = fuel_balance and (
        complex_history
        or bool(concepts.intersection({"alarm", "boost", "load", "oil_pressure", "temperature", "torque", "trip"}))
    )
    complex_history = complex_history or fuel_balance_complex
    telemetry_overview = telemetry_overview_requested(message)
    current_request = any(
        term in lower
        for term in ["right now", "currently", "current ", "now?", "at the moment", "remains", "remaining", "fuel left"]
    )
    return {
        "identity": any(re.search(pattern, lower) for pattern in identity_patterns),
        "first_engine_run": first_engine_run,
        "last_engine_run": last_engine_run,
        "engine_run_history": first_engine_run or last_engine_run,
        "engine_run_direction": "first" if first_engine_run else "last" if last_engine_run else "",
        "fuel_balance": fuel_balance,
        "fuel_balance_complex": fuel_balance_complex,
        "fuel_economy": fuel_economy,
        "fuel_economy_needs_window": fuel_economy_needs_window,
        "fuel_usage": fuel_usage,
        "fuel_usage_needs_window": fuel_usage_needs_window,
        "shore_power_history": shore_power_history,
        "solar": "solar" in concepts,
        "solar_hardware": solar_hardware,
        "battery": battery_terms or "solar" in concepts or any(word in lower for word in ["shore", "charge"]),
        "battery_voltage_history": battery_voltage_history,
        "health": any(word in lower for word in ["health", "status", "wrong", "alert", "alarm", "issue", "ok", "okay"]),
        "telemetry_overview": telemetry_overview,
        "generic_history": generic_history,
        "complex_history": complex_history,
        "current_request": current_request,
        "generic_telemetry": telemetry_terms or telemetry_overview,
        "concepts": sorted(concepts),
        "query_plan": query_plan,
    }


def ha_entity_ids_for_question(kind: dict[str, Any]) -> list[str]:
    if kind.get("generic_history") and not kind.get("current_request"):
        return []
    if (
        kind.get("identity")
        or kind.get("engine_run_history")
        or (kind.get("fuel_balance") and not kind.get("complex_history"))
        or kind.get("fuel_economy")
        or kind.get("fuel_usage")
        or (kind.get("shore_power_history") and not kind.get("complex_history"))
        or (kind.get("battery_voltage_history") and not kind.get("complex_history"))
    ):
        return []
    ids = [
        "sensor.boat_health_summary",
        "binary_sensor.boat_ok",
        "sensor.boat_watch_summary",
    ]
    if kind.get("battery") or kind.get("solar") or kind.get("health"):
        ids.extend(
            [
                "binary_sensor.shore_power_connected",
                "sensor.signalk_api_up",
                "sensor.engine_alarm_status",
                "sensor.weather_risk_level",
                "sensor.audit_health_summary",
            ]
        )
    return ids


def should_read_live_telemetry(kind: dict[str, Any]) -> bool:
    if kind.get("generic_history") and not kind.get("current_request") and not kind.get("telemetry_overview"):
        return False
    if kind.get("complex_history"):
        return True
    return not (
        kind.get("identity")
        or kind.get("engine_run_history")
        or kind.get("fuel_balance")
        or kind.get("fuel_economy")
        or kind.get("fuel_usage")
        or kind.get("shore_power_history")
        or kind.get("battery_voltage_history")
    )


def should_search_docs(kind: dict[str, Any], message: str) -> bool:
    if kind.get("solar_hardware"):
        return True
    if (
        kind.get("identity")
        or kind.get("engine_run_history")
        or kind.get("fuel_economy")
        or kind.get("fuel_usage")
        or (kind.get("shore_power_history") and not kind.get("complex_history"))
        or (kind.get("battery_voltage_history") and not kind.get("complex_history"))
    ):
        return False
    if kind.get("fuel_balance"):
        lower = message.lower()
        return any(term in lower for term in ["why", "cause", "inspect", "what should", "recommend"])
    if kind.get("generic_telemetry") or kind.get("generic_history") or kind.get("telemetry_overview"):
        lower = message.lower()
        return any(term in lower for term in ["why", "cause", "diagnose", "troubleshoot", "inspect", "what should", "recommend"])
    return True


def should_collect_ha_telemetry(kind: dict[str, Any]) -> bool:
    if not kind.get("generic_telemetry") or kind.get("solar"):
        return False
    ha_history_concepts = {
        "alarm",
        "bilge",
        "freshness",
        "fuel_level",
        "humidity",
        "position",
        "service",
        "shore_power",
        "tide",
        "trip",
        "weather",
    }
    return bool(
        not kind.get("generic_history")
        or kind.get("telemetry_overview")
        or (kind.get("generic_history") and not kind.get("complex_history"))
        or kind.get("current_request")
        or set(kind.get("concepts", [])).intersection(ha_history_concepts)
    )


def answer_from_clarification(context: dict[str, Any]) -> str | None:
    kind = context.get("question_type", {})
    if kind.get("fuel_usage_needs_window"):
        return "What time window should I use: today, this weekend, last 7 days, or a specific trip?"
    if kind.get("fuel_economy_needs_window"):
        return "What time window should I use for fuel economy: this year, last 30 days, last 90 days, or a specific trip?"
    return None


def answer_from_ais_freshness(context: dict[str, Any]) -> str | None:
    kind = context.get("question_type", {})
    concepts = set(kind.get("concepts", []))
    if not {"ais", "freshness"}.issubset(concepts):
        return None
    current = context.get("current_telemetry")
    ais = current.get("ais") if isinstance(current, dict) else None
    if not isinstance(ais, dict):
        error = context.get("current_telemetry_error")
        return f"I could not read nearby AIS target freshness: {error}" if error else None

    targets = [target for target in ais.get("targets", []) if isinstance(target, dict)]
    stale = [target for target in targets if target.get("position_stale") is True]
    if not stale:
        return (
            f"No stale AIS positions are present among {len(targets)} nearby targets. "
            "AIS positions are treated as stale after 15 minutes."
        )

    lines = [
        f"{len(stale)} of {len(targets)} nearby AIS targets have positions older than 15 minutes:"
    ]
    for target in stale:
        name = target.get("name")
        mmsi = target.get("mmsi")
        label = str(name) if name and name != "unknown" else f"MMSI {mmsi or 'unknown'}"
        distance = target.get("distance_nm")
        age = target.get("position_age_minutes")
        distance_text = f"{float(distance):.2f} nm away" if isinstance(distance, (int, float)) else "distance unknown"
        age_text = f"{float(age):.1f} minutes old" if isinstance(age, (int, float)) else "age unknown"
        lines.append(f"- {label}: {distance_text}; position is {age_text}.")
    fresh_count = len([target for target in targets if target.get("position_stale") is False])
    if fresh_count:
        lines.append(f"The other {fresh_count} target positions are within the 15-minute freshness limit.")
    return "\n".join(lines)


def answer_from_freshness(context: dict[str, Any]) -> str | None:
    kind = context.get("question_type", {})
    concepts = set(kind.get("concepts", []))
    if "freshness" not in concepts or "ais" in concepts:
        return None
    ha = context.get("ha_telemetry")
    if not isinstance(ha, dict):
        error = context.get("ha_telemetry_error")
        return f"I could not read Home Assistant sensor freshness: {error}" if error else None

    unavailable = [
        item
        for item in ha.get("unavailable", [])
        if isinstance(item, dict) and str(item.get("state", "")).lower() == "unavailable"
    ]
    unknown = [
        item
        for item in ha.get("unavailable", [])
        if isinstance(item, dict) and str(item.get("state", "")).lower() == "unknown"
    ]
    lines: list[str] = []
    if unavailable:
        lines.append("Confirmed unavailable sensors:")
        for item in unavailable[:8]:
            name = item.get("friendly_name") or item.get("entity_id")
            lines.append(f"- {name}, since {item.get('last_updated_local', 'unknown')}")
    else:
        lines.append("No measurement sensor currently reports `unavailable`.")
    if unknown:
        names = ", ".join(str(item.get("friendly_name") or item.get("entity_id")) for item in unknown[:4])
        lines.append(f"Unknown computed values: {names}. An unknown time-remaining estimate can be normal at zero battery current.")

    stale_count = int(ha.get("stale_over_6h_count") or 0)
    if stale_count:
        lines.append(
            f"{stale_count} measurement entities have unchanged Home Assistant timestamps older than 6 hours. "
            "That is a review list, not proof they stopped updating; engine-off and stable values commonly keep old timestamps."
        )
    current = context.get("current_telemetry", {})
    if isinstance(current, dict) and not current.get("signalk_error"):
        lines.append(f"Live SignalK is reachable and currently exposes {current.get('available_path_count', 0)} self-vessel paths.")
    return "\n".join(lines)


def answer_from_solar_inference(context: dict[str, Any]) -> str | None:
    kind = context.get("question_type", {})
    if not kind.get("solar") or kind.get("solar_hardware"):
        return None
    summary = context.get("solar_inference")
    if not isinstance(summary, dict):
        error = context.get("solar_inference_error")
        return f"Solar inference tracking is unavailable: {error}" if error else None

    observations = int(summary.get("observation_count") or 0)
    intervals = int(summary.get("valid_interval_count") or 0)
    lines = [
        "Solar inference tracking is active.",
        (
            f"It has {observations} observations for {summary.get('label', 'the requested period')}, "
            f"starting {summary.get('tracking_started_local') or 'when the next indexer sample arrives'}."
        ),
    ]
    latest = summary.get("latest_observation")
    if isinstance(latest, dict):
        classification = {
            "off_shore_engines_off": "away from shore charging with engines off",
            "charging_proxy_on_near_dock": "charging detected near the dock; source unknown",
            "shore_connection_unknown": "shore connection unknown",
            "engine_state_unknown": "engine state unknown",
            "engine_running": "engine charging possible",
            "battery_power_unknown": "battery power unavailable",
        }.get(str(latest.get("classification")), str(latest.get("classification") or "unknown"))
        lines.append(
            "Latest sample: "
            f"{classification} at {latest.get('observed_at_local', 'unknown')}; "
            f"battery {format_stat_value(latest.get('battery_power_w'))} W, "
            f"{format_stat_value(latest.get('battery_current_a'))} A, "
            f"{format_stat_value(latest.get('battery_voltage_v'))} V."
        )
    if intervals <= 0:
        lines.append(
            "No complete qualifying interval has been measured yet. A qualifying interval requires evidence that the "
            "boat is underway, beyond the dock radius, or not charging; both engines below 200 RPM; battery power; "
            "and no sampling gap longer than 15 minutes."
        )
    else:
        lines.extend(
            [
                (
                    f"Qualifying coverage: {format_stat_value(summary.get('qualifying_coverage_hours'))} hours "
                    f"across {intervals} intervals; confidence {summary.get('confidence', 'unknown')}."
                ),
                (
                    f"Inferred net charge into the battery: {format_stat_value(summary.get('inferred_net_charge_wh'))} Wh. "
                    f"Observed net discharge during the same conditions: "
                    f"{format_stat_value(summary.get('observed_net_discharge_wh'))} Wh."
                ),
                (
                    f"Peak inferred charging was {format_stat_value(summary.get('peak_inferred_charge_w'))} W; "
                    f"peak observed discharge was {format_stat_value(summary.get('peak_observed_discharge_w'))} W."
                ),
            ]
        )
    lines.append(
        "The current shore-power entity is a charging proxy, not a physical AC-input sensor. Away from the dock, this "
        "attributes positive SmartShunt power to solar or another uninstrumented source after excluding engine charging. "
        "It measures net battery charging after onboard loads, not gross panel production."
    )
    return "\n".join(lines)


def answer_from_solar_hardware(context: dict[str, Any]) -> str | None:
    kind = context.get("question_type", {})
    if not kind.get("solar_hardware"):
        return None
    return "\n".join(
        [
            "Use this upgrade order:",
            "1. Identify the existing solar controller and panel labels. Record controller model, panel watts, Voc, Isc, and battery profile before buying anything.",
            "2. If it is Victron SmartSolar or compatible BlueSolar, connect it to the already installed SignalK Victron BLE plugin using its advertisement key, or use VE.Direct-to-USB. This can expose panel power and daily solar yield directly.",
            "3. If the controller has no usable data interface, replace it with a correctly sized Victron SmartSolar MPPT. Size from cold-weather array Voc and maximum current, not panel watts alone.",
            "4. For individual 12 V loads, use a Simarine PICO with SCQ25/SCQ25T four-channel 25 A shunts or SCQ50 four-channel 50 A shunts. Start with refrigerator, electronics/network, lighting/accessories, and pumps.",
            "5. Add a physical 120 V shore-input energy or CT meter in a dry protected location. The current shore-power entity only infers charging from the battery, so it cannot distinguish shore charging from solar. Have a qualified marine electrician select and install the AC equipment.",
            "Collect two to four representative weeks of gross solar yield, net battery charge, overnight baseline load, and per-circuit Wh before deciding whether the panel earns its space.",
        ]
    )


def answer_from_facts(message: str, facts: dict[str, Any]) -> str | None:
    kind = classify_question(message)
    if not kind.get("identity"):
        return None
    vessel = facts.get("vessel", {})
    host = facts.get("host", {})
    telemetry = facts.get("telemetry", {})
    engines = facts.get("engines", {})
    lines = [
        f"The boat is {vessel.get('name', BOAT_NAME)}, a {vessel.get('type', 'Motor vessel')}.",
        f"MMSI: {vessel.get('mmsi', '000000000')}; call sign: {vessel.get('callsign', 'UNSET')}.",
    ]
    if vessel.get("length_overall_m") or vessel.get("beam_m"):
        lines.append(f"LOA: {vessel.get('length_overall_m', 'unknown')} m; beam: {vessel.get('beam_m', 'unknown')} m.")
    if host or telemetry:
        lines.append(
            "Boat stack: "
            f"{host.get('platform', 'Linux')} hostname {host.get('hostname', 'vesselstack')}, "
            f"{telemetry.get('marine_hub', 'SignalK')} -> {telemetry.get('automation', 'Home Assistant')} -> {telemetry.get('history', 'InfluxDB')}."
        )
    if engines:
        lines.append(f"Engines: {engines.get('layout', 'Twin Volvo IPS')}.")
    return "\n".join(lines)


def answer_from_health_state(message: str, context: dict[str, Any]) -> str | None:
    kind = context.get("question_type", {})
    if not kind.get("health") or kind.get("fuel_balance"):
        return None
    lower = message.lower()
    if not any(term in lower for term in ["is the boat ok","is the boat okay","boat ok","boat okay","boat health","boat status","overall health","overall status","status of the boat"]):
        return None
    states=context.get("ha_states",{}); summary=states.get("sensor.boat_health_summary",{}); boat_ok=states.get("binary_sensor.boat_ok",{}); watch=states.get("sensor.boat_watch_summary",{})
    status=str(summary.get("state","unknown")); ok_state=str(boat_ok.get("state","unknown")); watch_state=str(watch.get("state","unknown"))
    if status == "OK" or ok_state == "on":
        return f"Yes. {context.get('boat_facts', {}).get('vessel', {}).get('name', BOAT_NAME)} is OK right now. Boat Health Summary is {status}; Boat OK is {ok_state}; watch summary is {watch_state}."
    return f"No clear OK state right now. Boat Health Summary is {status}; Boat OK is {ok_state}; watch summary is {watch_state}."


def answer_from_briefing(message: str, context: dict[str, Any]) -> str | None:
    if "brief" not in message.lower():
        return None
    values=(context.get("current_telemetry") or {}).get("values",{}); states=context.get("ha_states") or {}
    state=lambda entity,default="unknown":str((states.get(entity) or {}).get("state",default))
    soc=safe_float(values.get("electrical.batteries.shunt.capacity.stateOfCharge")); voltage=safe_float(values.get("electrical.batteries.shunt.voltage")); port=safe_float(values.get("propulsion.port.revolutions")) or 0; starboard=safe_float(values.get("propulsion.starboard.revolutions")) or 0
    lines=[f"Boat briefing: {state('sensor.boat_health_summary','health unknown')}; {state('sensor.boat_watch_summary','watch state unavailable')}."]
    if soc is not None or voltage is not None: lines.append(f"Battery: {f'{soc:g}%' if soc is not None else 'SOC unavailable'}, {f'{voltage:.2f} V' if voltage is not None else 'voltage unavailable'}.")
    lines.append(f"Engines: port {port:.0f} RPM, starboard {starboard:.0f} RPM. Shore charging proxy: {state('binary_sensor.shore_power_connected')}.")
    alarms=(context.get("current_telemetry") or {}).get("alarm_summary")
    if alarms: lines.append(str(alarms))
    return "\n".join(lines)


def answer_from_engine_history(context: dict[str, Any]) -> str | None:
    kind = context.get("question_type", {})
    if not kind.get("engine_run_history"):
        return None
    summary = context.get("engine_run_history", {}) or context.get("engine_first_run", {})
    selected = summary.get("selected") or summary.get("first")
    direction = summary.get("direction") or kind.get("engine_run_direction") or "first"
    label = "first retained" if direction == "first" else "last retained"
    if not selected:
        return (
            "I did not find an engine-running sample in retained history. "
            f"I checked {summary.get('lookback_days', 365)} days in {summary.get('source_bucket', INFLUX_HISTORY_BUCKET)} "
            f"using RPM >= {summary.get('rpm_threshold', ENGINE_RUNNING_RPM)}."
        )
    samples = summary.get("same_minute_samples") or [selected]
    rpm_text = ", ".join(f"{sample.get('side')}: {sample.get('rpm')} RPM" for sample in samples)
    return (
        f"The {label} engine-running sample is {selected.get('time_local')} "
        f"({selected.get('time_utc')}). Detected above {summary.get('rpm_threshold', ENGINE_RUNNING_RPM)} RPM: {rpm_text}. "
        f"Note: this is based on retained 1-minute downsample history in {summary.get('source_bucket', INFLUX_HISTORY_BUCKET)}."
    )


def answer_from_fuel_usage(context: dict[str, Any]) -> str | None:
    kind = context.get("question_type", {})
    if not kind.get("fuel_usage"):
        return None
    summary = context.get("fuel_usage", {})
    if not summary:
        error = context.get("fuel_usage_error")
        if error:
            return f"I could not compute fuel usage from InfluxDB: {error}"
        return None
    running_minutes = int(summary.get("running_minutes") or 0)
    if running_minutes <= 0:
        return (
            f"I found no engine-running fuel samples for {summary.get('label', 'the requested period')} "
            f"({summary.get('start_local')} to {summary.get('stop_local')}) using RPM >= "
            f"{summary.get('rpm_threshold', ENGINE_RUNNING_RPM)}."
        )
    totals = summary.get("totals", {})
    averages = summary.get("averages", {})
    return (
        f"Fuel used for {summary.get('label', 'the requested period')} "
        f"({summary.get('start_local')} to {summary.get('stop_local')}): "
        f"{totals.get('total_gallons')} gal total. "
        f"Port: {totals.get('port_gallons')} gal; "
        f"starboard: {totals.get('starboard_gallons')} gal. "
        f"Running time: {running_minutes} minutes, from {summary.get('first_running_sample_local')} "
        f"to {summary.get('last_running_sample_local')}. "
        f"Average burn while running: {averages.get('total_gph_running')} gal/h total. "
        "This is a computed telemetry answer from retained 1-minute InfluxDB history."
    )


def answer_from_fuel_economy(context: dict[str, Any]) -> str | None:
    kind = context.get("question_type", {})
    if not kind.get("fuel_economy"):
        return None
    summary = context.get("fuel_economy", {})
    if not summary:
        error = context.get("fuel_economy_error")
        if error:
            return f"I could not compute fuel economy from InfluxDB: {error}"
        return None
    running_minutes = int(summary.get("running_minutes") or 0)
    totals = summary.get("totals", {})
    economy = summary.get("economy", {})
    if running_minutes <= 0 or not economy.get("nm_per_gallon"):
        return (
            f"I do not have enough retained running fuel/speed samples to compute fuel economy for "
            f"{summary.get('label', 'the requested period')}."
        )
    rpm_filter = summary.get("rpm_filter")
    filter_text = ""
    if isinstance(rpm_filter, dict):
        engine_scope = "both engines" if rpm_filter.get("require_both_engines") else "either engine"
        filter_text = (
            f" with {engine_scope} between {rpm_filter.get('minimum_rpm'):g} and "
            f"{rpm_filter.get('maximum_rpm'):g} RPM"
        )
    return (
        f"Average fuel economy over {summary.get('label', 'the retained period')}: "
        f"{economy.get('nm_per_gallon')} NM/gal "
        f"({economy.get('gallons_per_nm')} gal/NM){filter_text}. "
        f"That is based on {round(running_minutes / 60.0, 1)} engine-running hours, "
        f"{totals.get('distance_nm')} NM, and {totals.get('fuel_gallons')} gal from "
        f"{summary.get('first_running_sample_local')} to {summary.get('last_running_sample_local')}."
    )


def answer_from_shore_power_history(context: dict[str, Any]) -> str | None:
    kind = context.get("question_type", {})
    if not kind.get("shore_power_history") or kind.get("complex_history"):
        return None
    summary = context.get("shore_power_history", {})
    if not summary:
        error = context.get("shore_power_history_error")
        if error:
            return f"I could not read Home Assistant shore-power history: {error}"
        return None

    target = summary.get("target_state")
    latest = summary.get("latest_matching_change")
    current = summary.get("current_state", "unknown")
    current_changed = summary.get("current_last_changed_local", "unknown")
    if latest:
        state_text = "turned off" if latest.get("state") == "off" else "turned on"
        return (
            f"Shore power last {state_text} at {latest.get('last_changed_local')} "
            f"({latest.get('last_changed_utc')}). Current state is {current}, last changed at {current_changed}. "
            f"Home Assistant returned {summary.get('history_events_returned', 0)} shore-power history events in the last "
            f"{summary.get('lookback_days', 30)} days. Note: the shore-power sensor has a 30-minute off delay."
        )
    if target:
        desired = "off" if target == "off" else "on"
        return (
            f"I did not find a recorded shore-power {desired} transition in the last "
            f"{summary.get('lookback_days', 30)} days. Current state is {current}, last changed at {current_changed}. "
            "If Home Assistant restarted or recorder history was purged, this current-state timestamp may be the best retained signal. "
            "Note: the shore-power sensor has a 30-minute off delay."
        )
    return (
        f"Shore power is currently {current}, last changed at {current_changed}. "
        f"Home Assistant returned {summary.get('history_events_returned', 0)} history events in the last "
        f"{summary.get('lookback_days', 30)} days."
    )


def answer_from_battery_voltage(context: dict[str, Any]) -> str | None:
    kind = context.get("question_type", {})
    if not kind.get("battery_voltage_history") or kind.get("complex_history"):
        return None
    summary = context.get("battery_voltage", {})
    if not summary:
        error = context.get("battery_voltage_error")
        if error:
            return f"I could not compute battery voltage history from InfluxDB: {error}"
        return None
    samples = int(summary.get("samples") or 0)
    if samples <= 0:
        return f"I found no retained battery-voltage samples for {summary.get('label', 'the requested period')}."

    def fmt(value: Any, digits: int = 2) -> str:
        if not isinstance(value, (int, float)):
            return "unknown"
        return f"{value:.{digits}f}".rstrip("0").rstrip(".")

    voltage = summary.get("voltage", {})
    soc = summary.get("smartshunt_soc", {})
    thresholds = summary.get("thresholds", {})
    agm = summary.get("agm_voltage_soc_estimate", {})
    lines = [
        f"Battery voltage over {summary.get('label', 'the retained period')}: "
        f"min {fmt(voltage.get('min'))} V, avg {fmt(voltage.get('avg'))} V, "
        f"max {fmt(voltage.get('max'))} V, latest {fmt(voltage.get('latest'))} V."
    ]
    if soc.get("count"):
        lines.append(
            f"SmartShunt SOC: {fmt(soc.get('min'), 1)}-{fmt(soc.get('max'), 1)}%, "
            f"latest {fmt(soc.get('latest'), 1)}%."
        )

    below_118 = int(thresholds.get("below_11_8_samples") or 0)
    below_50 = int(thresholds.get("below_12_05_samples") or 0)
    if below_118 or below_50:
        lines.append(f"Low-voltage flags: {below_118} hourly samples below 11.8 V; {below_50} below 12.05 V.")
    else:
        lines.append("No hourly samples were below 11.8 V or the 50% AGM resting reference of 12.05 V.")

    charging_samples = int(thresholds.get("charging_or_float_samples") or 0)
    if charging_samples > samples / 2:
        lines.append("Most samples look like charging/float, so SmartShunt SOC is the better level signal than the AGM voltage chart.")
    elif isinstance(voltage.get("latest"), (int, float)) and float(voltage["latest"]) >= 13.0:
        lines.append("Latest voltage is at or above the AGM 100% resting reference; treat the chart as a reference, not a precise SOC reading.")
    elif agm.get("latest_voltage_percent") is not None:
        lines.append(f"AGM resting-voltage estimate from latest voltage: about {fmt(agm.get('latest_voltage_percent'), 1)}%.")
    return "\n".join(lines)


def answer_from_fuel_balance(context: dict[str, Any]) -> str | None:
    kind = context.get("question_type", {})
    if not kind.get("fuel_balance") or kind.get("fuel_balance_complex"):
        return None
    summaries = [
        ("24h", context.get("fuel_balance_24h", {})),
        ("7d", context.get("fuel_balance_7d", {})),
    ]
    lines = ["Fuel balance summary, running samples only:"]
    found = False
    def signed(value: Any, digits: int = 1) -> str:
        return f"{value:+.{digits}f}" if isinstance(value, (int, float)) else "unknown"

    for label, summary in summaries:
        running_minutes = int(summary.get("running_minutes") or 0)
        if running_minutes <= 0:
            lines.append(f"- {label}: no engine-running samples found.")
            continue
        found = True
        averages = summary.get("averages", {})
        derived = summary.get("derived", {})
        diff = derived.get("starboard_minus_port_fuel_gph")
        pct = derived.get("starboard_fuel_percent_vs_port")
        rpm_diff = derived.get("starboard_minus_port_rpm")
        total_diff = derived.get("starboard_minus_port_total_gallons")
        direction = "more" if diff is not None and diff > 0 else "less" if diff is not None and diff < 0 else "about the same"
        diff_text = f"{abs(diff):.3f}" if isinstance(diff, (int, float)) else "unknown"
        total_text = signed(total_diff, 2)
        lines.append(
            f"- {label}: {running_minutes} running minutes, confidence {summary.get('confidence')}. "
            f"Port avg {averages.get('port_fuel_gph')} gal/h at {averages.get('port_rpm')} RPM; "
            f"starboard avg {averages.get('starboard_fuel_gph')} gal/h at {averages.get('starboard_rpm')} RPM. "
            f"Starboard used {diff_text} gal/h {direction} than port"
            f"{f' ({pct:+.1f}%)' if isinstance(pct, (int, float)) else ''}; "
            f"RPM delta {signed(rpm_diff)}; total delta over running minutes {total_text} gal."
        )
    if not found:
        lines.append("No comparison is possible until both engines have fuel and RPM samples while running.")
    else:
        seven_day = context.get("fuel_balance_7d", {})
        derived = seven_day.get("derived", {})
        pct = derived.get("starboard_fuel_percent_vs_port")
        rpm_delta = derived.get("starboard_minus_port_rpm")
        if isinstance(pct, (int, float)) and abs(pct) >= 5:
            if isinstance(rpm_delta, (int, float)) and abs(rpm_delta) < 50:
                lines.append(
                    "Interpretation: the fuel delta is not explained by a large RPM mismatch. "
                    "Next checks: confirm both fuel-rate sensors are calibrated, compare trim/drive angle, inspect props and running gear for fouling or damage, and compare load/boost/coolant/oil-pressure trends during the same running window."
                )
            else:
                lines.append(
                    "Interpretation: RPM differs enough that throttle/load matching should be checked before assuming a mechanical or sensor fault."
                )
    lines.append("This is a computed telemetry answer, not an LLM guess.")
    return "\n".join(lines)


def display_measurement_name(measurement: str) -> str:
    if measurement.startswith("sensor."):
        return measurement.replace("sensor.", "").replace("_", " ")
    return measurement.replace(".", " ").replace("_", " ")


def format_stat_value(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return str(value)
    if abs(value) >= 100:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    if abs(value) >= 10:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{value:.3f}".rstrip("0").rstrip(".")


def history_source_priority(bucket: str) -> int:
    return 0 if bucket == INFLUX_HISTORY_BUCKET else 1


def unit_for_measurement(measurement: str, context: dict[str, Any]) -> str:
    ha = context.get("ha_telemetry", {})
    for item in ha.get("matched", []) if isinstance(ha, dict) else []:
        if item.get("entity_id") == measurement and item.get("unit"):
            return str(item["unit"])
    if measurement.endswith("humidity") or "humidity" in measurement:
        return "%"
    if measurement.startswith("tanks.fuel.") or "fuel_tank" in measurement or measurement.endswith("currentLevel"):
        return "%"
    if "stateOfCharge" in measurement or "state_of_charge" in measurement or measurement.endswith("_soc"):
        return "%"
    if measurement.endswith("temperature") or "temperature" in measurement or "temp" in measurement:
        return "F" if measurement.startswith("propulsion.") else ""
    if measurement.startswith("environment.depth.") or "water_depth" in measurement:
        return "ft"
    if "voltage" in measurement:
        return "V"
    if "current" in measurement:
        return "A"
    if "speedOverGround" in measurement or "speedThroughWater" in measurement:
        return "kn"
    if "heading" in measurement or "courseOverGround" in measurement or "attitude" in measurement:
        return "deg"
    if measurement == "navigation.rateOfTurn":
        return "deg/min"
    if "pressure" in measurement.lower():
        return "psi" if measurement.startswith("propulsion.") else "inHg"
    if measurement.endswith(".revolutions"):
        return "RPM"
    if measurement.endswith(".fuel.rate"):
        return "gal/h"
    if measurement.endswith(".runTime") or measurement.endswith(".timeRemaining"):
        return "h"
    if measurement.endswith((".engineLoad", ".engineTorque", ".trimState")):
        return "%"
    if measurement.endswith(".power"):
        return "W"
    return ""


def answer_from_telemetry_overview(context: dict[str, Any]) -> str | None:
    kind = context.get("question_type", {})
    if not kind.get("telemetry_overview"):
        return None
    ha = context.get("ha_telemetry", {})
    history = context.get("influx_history", {})
    if not isinstance(ha, dict) or not isinstance(history, dict):
        return None
    buckets = history.get("buckets") or {}
    history_bucket = buckets.get(INFLUX_HISTORY_BUCKET, {}) if isinstance(buckets, dict) else {}
    homeassistant = buckets.get(INFLUX_HOME_ASSISTANT_BUCKET, {}) if isinstance(buckets, dict) else {}
    lines = [
        "I can read current Home Assistant state and retained InfluxDB history.",
        (
            f"Current HA state: {ha.get('telemetry_state_count', 0)} telemetry-relevant entities "
            f"out of {ha.get('total_states', 0)} total states."
        ),
        (
            "Retained InfluxDB history: "
            f"{history_bucket.get('measurement_count', 0)} SignalK/downsample measurements in `{INFLUX_HISTORY_BUCKET}`, "
            f"{homeassistant.get('measurement_count', 0)} Home Assistant measurements in `{INFLUX_HOME_ASSISTANT_BUCKET}`."
        ),
    ]
    sample_measurements = []
    for payload in [history_bucket, homeassistant]:
        if isinstance(payload, dict):
            sample_measurements.extend(payload.get("matched_measurements") or [])
    if sample_measurements:
        lines.append("Examples: " + ", ".join(sample_measurements[:10]) + ".")
    return "\n".join(lines)


def answer_from_generic_history(context: dict[str, Any]) -> str | None:
    kind = context.get("question_type", {})
    if not kind.get("generic_history") or kind.get("telemetry_overview") or kind.get("complex_history"):
        return None
    history = context.get("influx_history", {})
    if not isinstance(history, dict):
        error = context.get("influx_history_error")
        return f"I could not read retained InfluxDB history: {error}" if error else None
    rows: list[tuple[str, str, dict[str, Any]]] = []
    for bucket, payload in (history.get("buckets") or {}).items():
        if not isinstance(payload, dict):
            continue
        summary = payload.get("numeric_summary")
        if not isinstance(summary, dict):
            continue
        for measurement, stats in summary.items():
            if not isinstance(stats, dict):
                continue
            if measurement.startswith(("automation.", "script.", "input_text.")):
                continue
            rows.append((str(bucket), str(measurement), stats))
    if any(bucket == INFLUX_HISTORY_BUCKET for bucket, _measurement, _stats in rows):
        rows = [
            (bucket, measurement, stats)
            for bucket, measurement, stats in rows
            if not (
                bucket == INFLUX_HOME_ASSISTANT_BUCKET
                and int(stats.get("count") or 0) <= 2
                and all(safe_float(stats.get(key)) == 0.0 for key in ["min", "avg", "max", "last"])
            )
        ]
    if not rows:
        matched = []
        for payload in (history.get("buckets") or {}).values():
            if isinstance(payload, dict):
                matched.extend(payload.get("matched_measurements") or [])
        if matched:
            ha = context.get("ha_telemetry", {})
            current_lines = []
            if isinstance(ha, dict):
                for item in ha.get("matched", [])[:4]:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("friendly_name") or item.get("entity_id")
                    unit = f" {item.get('unit')}" if item.get("unit") else ""
                    current_lines.append(f"{name}: {item.get('state')}{unit}")
            if current_lines:
                return (
                    "I found matching retained measurements, but no numeric samples to summarize for that window. "
                    "Current matching state: "
                    + "; ".join(current_lines)
                    + "."
                )
            return (
                "I found matching retained measurements, but no numeric samples to summarize for that window. "
                "Matching measurements: "
                + ", ".join(str(item) for item in matched[:6])
                + "."
            )
        return "I did not find matching retained history for that question."

    tokens = set(str(item) for item in context.get("match_tokens", []))
    rows.sort(key=lambda item: (-match_score(item[1], tokens), history_source_priority(item[0]), measurement_priority(item[1]), item[1]))
    label = str(history.get("label") or f"last {int(history.get('lookback_days', 7) or 7)} days")
    lines = [f"Retained history for {label} ({history.get('start_local')} to {history.get('stop_local')}):"]
    for bucket, measurement, stats in rows[:6]:
        unit = unit_for_measurement(measurement, context)
        suffix = f" {unit}" if unit else ""
        lines.append(
            f"- {display_measurement_name(measurement)}: "
            f"min {format_stat_value(stats.get('min'))}{suffix}, avg {format_stat_value(stats.get('avg'))}{suffix}, "
            f"max {format_stat_value(stats.get('max'))}{suffix}, latest {format_stat_value(stats.get('last'))}{suffix} "
            f"({stats.get('count')} samples from {bucket}; min at {stats.get('min_time_local')}, max at {stats.get('max_time_local')})."
        )
    if len(rows) > 6:
        lines.append(f"{len(rows) - 6} more matching numeric measurements were available.")
    return "\n".join(lines)


def answer_from_event_history(context: dict[str, Any]) -> str | None:
    plan = context.get("query_plan", {})
    if plan.get("operation") not in {"event_count", "transition", "duration"}:
        return None
    payload = context.get("ha_event_history")
    entities = payload.get("entities", {}) if isinstance(payload, dict) else {}
    usable = [(entity_id, item) for entity_id, item in entities.items() if isinstance(item, dict) and item.get("transitions")]
    if not usable:
        return None
    entity_id, item = usable[0]
    name = item.get("friendly_name") or entity_id
    transitions = item.get("transitions", [])
    on_events = [event for event in transitions if str(event.get("state", "")).lower() in {"on", "running", "active", "open"}]
    label = payload.get("label", "the requested window")
    if plan.get("operation") == "event_count":
        return f"{name} activated {len(on_events)} time{'s' if len(on_events) != 1 else ''} during {label}, based on retained Home Assistant transitions."
    if plan.get("operation") == "transition":
        event = transitions[-1]
        return f"The last retained {name} transition during {label} was to {event.get('state')} at {event.get('time_local')}."
    total_seconds = 0.0
    stop = parse_influx_time((plan.get("window") or {}).get("stop_utc")) or dt.datetime.now(dt.timezone.utc)
    for index, event in enumerate(transitions):
        if str(event.get("state", "")).lower() not in {"on", "running", "active", "open"}:
            continue
        started = parse_influx_time(event.get("time_utc"))
        ended = parse_influx_time(transitions[index + 1].get("time_utc")) if index + 1 < len(transitions) else stop
        if started and ended and ended >= started:
            total_seconds += (ended - started).total_seconds()
    return f"{name} was active for approximately {total_seconds / 3600:.2f} hours during {label}, based on retained Home Assistant transitions."


def collect_context(message: str, history: list[dict[str, str]] | None = None, prior_query_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    started = time.monotonic()
    conversation = sanitize_conversation_history(history)
    query = effective_question(message, conversation)
    kind = classify_question(query)
    context: dict[str, Any] = {
        "question_type": kind,
        "query_plan": kind.get("query_plan", {}),
        "match_tokens": sorted(message_tokens(query)),
        "boat_facts": load_boat_facts(),
        "context_strategy": {
            "tier_0": "stable boat facts",
            "tier_1": "live telemetry and Home Assistant state when relevant",
            "tier_2": "InfluxDB history only for history/fuel questions",
            "tier_3": "local docs for support/runbook context",
        },
    }
    if prior_query_plan:
        context["prior_query_plan"] = prior_query_plan
    if conversation:
        context["conversation"] = conversation
    if query != message:
        context["interpreted_question"] = query
    if should_read_live_telemetry(kind):
        context["current_telemetry"] = current_telemetry_snapshot(query)
    entity_ids = ha_entity_ids_for_question(kind)
    if entity_ids:
        context["ha_states"] = get_ha_states(entity_ids)
    if should_search_docs(kind, query):
        context["local_docs"] = search_local_docs(query, limit=4)
    if kind["engine_run_history"]:
        try:
            direction = str(kind.get("engine_run_direction") or "first")
            context["engine_run_history"] = engine_run_history_summary(direction=direction)
        except Exception as exc:
            context["engine_run_history_error"] = str(exc)
    if kind["fuel_usage"]:
        try:
            if not kind.get("fuel_usage_needs_window"):
                window = resolve_fuel_usage_window(query)
                context["fuel_usage"] = fuel_usage_summary(window)
        except Exception as exc:
            context["fuel_usage_error"] = str(exc)
    if kind["fuel_economy"]:
        try:
            if kind.get("fuel_economy_needs_window"):
                pass
            elif any(
                term in query.lower()
                for term in [
                    "today",
                    "yesterday",
                    "weekend",
                    "last 24",
                    "past 24",
                    "last 7",
                    "past 7",
                    "this week",
                    "last week",
                    "this year",
                    "year to date",
                    "ytd",
                    "annual",
                    "season",
                    "last 30",
                    "past 30",
                    "30d",
                    "last 90",
                    "past 90",
                    "90d",
                ]
            ):
                context["fuel_economy"] = fuel_economy_summary(
                    window=resolve_fuel_usage_window(query),
                    rpm_band=resolve_rpm_band(query),
                    require_both_engines="both" in query.lower(),
                )
            else:
                context["fuel_economy"] = fuel_economy_summary(
                    rpm_band=resolve_rpm_band(query),
                    require_both_engines="both" in query.lower(),
                )
        except Exception as exc:
            context["fuel_economy_error"] = str(exc)
    if kind["shore_power_history"] and not kind.get("solar"):
        try:
            context["shore_power_history"] = shore_power_history_summary(query)
        except Exception as exc:
            context["shore_power_history_error"] = str(exc)
    if kind.get("solar"):
        try:
            context["solar_inference"] = solar_inference_summary(query)
        except Exception as exc:
            context["solar_inference_error"] = str(exc)
    if kind["battery_voltage_history"] and not kind.get("complex_history"):
        try:
            context["battery_voltage"] = battery_voltage_summary(days=resolve_battery_history_days(query))
        except Exception as exc:
            context["battery_voltage_error"] = str(exc)
    if should_collect_ha_telemetry(kind):
        try:
            context["ha_telemetry"] = ha_telemetry_context(query)
        except Exception as exc:
            context["ha_telemetry_error"] = str(exc)
    purpose_history = any(
        kind.get(key)
        for key in [
            "engine_run_history",
            "fuel_balance",
            "fuel_economy",
            "fuel_usage",
            "shore_power_history",
            "battery_voltage_history",
            "solar",
        ]
    )
    if kind.get("telemetry_overview") or (
        kind.get("generic_history")
        and not kind.get("solar")
        and (kind.get("complex_history") or not purpose_history)
    ):
        try:
            context["influx_history"] = generic_influx_history_summary(query)
            focus = complex_answer_focus(query, context["influx_history"])
            if focus:
                context["answer_focus"] = focus
        except Exception as exc:
            context["influx_history_error"] = str(exc)
    event_operation = kind.get("query_plan", {}).get("operation") in {"event_count", "transition", "duration"}
    if (kind.get("complex_history") or event_operation) and not kind.get("solar") and set(kind.get("concepts", [])).intersection(
        {"alarm", "bilge", "generator", "service", "shore_power", "trip"}
    ):
        try:
            context["ha_event_history"] = ha_event_history_context(query)
        except Exception as exc:
            context["ha_event_history_error"] = str(exc)
    if kind["fuel_balance"]:
        try:
            context["fuel_balance_24h"] = fuel_balance_summary(24)
            context["fuel_balance_7d"] = fuel_balance_summary(24 * 7)
        except Exception as exc:
            context["fuel_balance_error"] = str(exc)
    context["context_profile"] = {
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "ha_entities": len(entity_ids),
        "docs": len(context.get("local_docs", [])),
        "live_telemetry": "current_telemetry" in context,
        "history": any(
            key in context
            for key in [
                "propulsion_24h",
                "propulsion_7d",
                "engine_run_history",
                "engine_first_run",
                "fuel_economy",
                "fuel_usage",
                "shore_power_history",
                "battery_voltage",
                "solar_inference",
                "ha_telemetry",
                "influx_history",
                "ha_event_history",
                "fuel_balance_24h",
                "fuel_balance_7d",
            ]
        ),
    }
    return context


def configured_provider() -> str:
    provider = (setting_value("BOAT_CHAT_PROVIDER", "") or "").strip().lower()
    if provider:
        return provider
    if setting_value("OPENAI_API_KEY"):
        return "openai"
    if setting_value("AI_GATEWAY_API_KEY") or setting_value("VERCEL_OIDC_TOKEN"):
        return "vercel"
    if setting_value("AWS_ACCESS_KEY_ID") and setting_value("AWS_SECRET_ACCESS_KEY"):
        return "bedrock"
    if setting_value("GOOGLE_API_KEY") or setting_value("GEMINI_API_KEY") or setting_value("GOOGLE_CLOUD_PROJECT"):
        return "google"
    if setting_value("OLLAMA_HOST"):
        return "ollama"
    if setting_value("BOAT_CHAT_BASE_URL") and setting_value("BOAT_CHAT_API_KEY"):
        return "openai_compatible"
    return "local"


def configured_fallback_provider() -> str:
    return (setting_value("BOAT_CHAT_FALLBACK_PROVIDER", "") or "").strip().lower()


# Curated starting points for the settings UI model dropdowns. Free-text entry
# stays available in the UI, so these only need to cover the common choices.
STATIC_MODEL_SUGGESTIONS: dict[str, list[str]] = {
    "codex_cli": [DEFAULT_CODEX_MODEL, "gpt-5.4", "gpt-5.5-mini", "gpt-5.5"],
    "claude_cli": ["haiku", "sonnet", "opus"],
    "openai": [DEFAULT_OPENAI_MODEL, "gpt-5.5-mini", "gpt-5.4", "gpt-5.4-mini"],
    "vercel": [DEFAULT_VERCEL_MODEL, "openai/gpt-5.5-mini", "anthropic/claude-haiku-4-5", "google/gemini-2.5-flash"],
    "google": [DEFAULT_GOOGLE_MODEL, "gemini-2.5-pro"],
    "bedrock": [],
    "openai_compatible": [],
    "local": [],
}


def ollama_installed_models() -> list[str]:
    base_url = normalize_http_url(setting_value("OLLAMA_HOST", DEFAULT_OLLAMA_URL) or DEFAULT_OLLAMA_URL).rstrip("/")
    try:
        data = http_get_json(f"{base_url}/api/tags", timeout=4)
        return sorted({str(item.get("name")) for item in data.get("models", []) if item.get("name")})
    except Exception:
        return []


def resolved_model(provider: str, configured: str | None, role: str = "primary") -> str:
    """Mirror the provider call paths to report which model a role actually uses."""
    provider = (provider or "").strip().lower()
    model = (configured or "").strip()
    if provider == "codex_cli":
        selected = model or setting_value("BOAT_CHAT_CODEX_MODEL") or DEFAULT_CODEX_MODEL
        return DEFAULT_CODEX_MODEL if ("/" in selected or ":" in selected) else selected
    if provider == "claude_cli":
        return model or setting_value("BOAT_CHAT_CLAUDE_MODEL") or "cli-default"
    if provider == "openai":
        return model or DEFAULT_OPENAI_MODEL
    if provider == "vercel":
        return model or DEFAULT_VERCEL_MODEL
    if provider in ("google", "vertex", "gemini"):
        return model or DEFAULT_GOOGLE_MODEL
    if provider == "ollama":
        if role == "fallback":
            return model or DEFAULT_OLLAMA_FALLBACK_MODEL
        return model or DEFAULT_OLLAMA_MODEL
    return model


def active_models() -> dict[str, str]:
    provider = configured_provider()
    fallback = configured_fallback_provider()
    return {
        "primary_provider": provider,
        "primary_model": resolved_model(provider, setting_value("BOAT_CHAT_MODEL"), role="primary"),
        "fallback_provider": fallback,
        "fallback_model": resolved_model(fallback, setting_value("BOAT_CHAT_FALLBACK_MODEL"), role="fallback") if fallback else "",
    }


def readiness_status() -> dict[str, Any]:
    memory = memory_index.status()
    cache = telemetry_cache.status()
    indexer = next((item for item in cache.get("items", []) if item.get("category") == "indexer" and item.get("key") == "last_run"), None)
    cache_age = indexer.get("age_seconds") if indexer else None
    provider = configured_provider()
    layers = {
        "process": {"ready": True},
        "memory": {"ready": bool(memory.get("exists") and memory.get("chunks", 0) > 0), "chunks": memory.get("chunks", 0)},
        "telemetry_cache": {"ready": cache_age is not None and int(cache_age) <= 15 * 60, "age_seconds": cache_age},
        "answer_provider": {"ready": provider in PROVIDER_OPTIONS, "provider": provider},
    }
    return {"ready": all(layer["ready"] for layer in layers.values()), "layers": layers}


def experience_status() -> dict[str, Any]:
    cached = telemetry_cache.get_summary("latest_context", "boat_status", max_age_seconds=30 * 60) or {}
    values = (cached.get("current_telemetry") or {}).get("values", {})
    ha = cached.get("ha_states") or {}
    state = lambda entity, default="unknown": str((ha.get(entity) or {}).get("state", default))
    port_rpm = safe_float(values.get("propulsion.port.revolutions")) or 0.0
    starboard_rpm = safe_float(values.get("propulsion.starboard.revolutions")) or 0.0
    underway = state("input_boolean.underway_mode", "off") == "on"
    mode = "Underway" if underway else "Engines running" if max(port_rpm, starboard_rpm) >= ENGINE_RUNNING_RPM else "Docked"
    cards = [
        {"id":"health","label":"Boat","value":state("sensor.boat_health_summary","Unknown"),"tone":"ok" if state("binary_sensor.boat_ok","off")=="on" else "danger","prompt":"Is the boat healthy right now?"},
        {"id":"mode","label":"Mode","value":mode,"tone":"normal","prompt":"Give me a current boat status briefing"},
        {"id":"shore","label":"Shore","value":"Charging" if state("binary_sensor.shore_power_connected","off")=="on" else "Off","tone":"ok" if state("binary_sensor.shore_power_connected","off")=="on" else "warning","prompt":"What is the current shore power status?"},
        {"id":"battery","label":"Battery","value":f"{safe_float(values.get('electrical.batteries.shunt.capacity.stateOfCharge')) or 0:g}% · {safe_float(values.get('electrical.batteries.shunt.voltage')) or 0:.2f} V","tone":"ok","prompt":"Tell me about the battery right now"},
        {"id":"engines","label":"Engines","value":f"{port_rpm:.0f} / {starboard_rpm:.0f} RPM","tone":"normal","prompt":"Compare both engines right now"},
        {"id":"fuel","label":"Fuel","value":f"{safe_float(values.get('tanks.fuel.0.currentLevel')) or 0:g}% / {safe_float(values.get('tanks.fuel.1.currentLevel')) or 0:g}%","tone":"normal","prompt":"How much fuel is left?"},
    ]
    cache = cached.get("cache") or {}
    return {"cards":cards,"watch_summary":state("sensor.boat_watch_summary","Unavailable"),"age_seconds":cache.get("age_seconds"),"source":"cached live boat status"}


def experience_insights() -> dict[str, Any]:
    cached = telemetry_cache.get_summary("latest_context", "boat_status", max_age_seconds=30 * 60) or {}
    values = (cached.get("current_telemetry") or {}).get("values", {})
    ha = cached.get("ha_states") or {}
    state = lambda entity, default="unknown": str((ha.get(entity) or {}).get("state", default))
    alerts = []
    alert_sources = [
        ("Boat health", state("sensor.boat_health_summary", "Unknown")),
        ("Boat watch", state("sensor.boat_watch_summary", "Unavailable")),
        ("Engine alarms", state("sensor.engine_alarm_status", "Unknown")),
        ("Weather risk", state("sensor.weather_risk_level", "Unknown")),
        ("System audit", state("sensor.audit_health_summary", "Unknown")),
    ]
    normal = {"ok", "clear", "normal", "none", "off", "healthy", "unavailable", "unknown"}
    for label, value in alert_sources:
        severity = "ok" if value.strip().lower() in normal else "warning"
        alerts.append({"label": label, "value": value, "severity": severity})
    latitude = safe_float(values.get("navigation.position.latitude"))
    longitude = safe_float(values.get("navigation.position.longitude"))
    position = None
    if latitude is not None and longitude is not None:
        position = {"latitude": latitude, "longitude": longitude, "label": f"{latitude:.5f}, {longitude:.5f}"}
    trip = last_trip_window()
    trip_payload = None
    if trip:
        duration_minutes = int((trip["stop"] - trip["start"]).total_seconds() / 60) if isinstance(trip.get("start"), dt.datetime) and isinstance(trip.get("stop"), dt.datetime) else None
        trip_payload = {key: trip.get(key) for key in ("label", "start_local", "stop_local", "trip_summary")}
        trip_payload["duration_minutes"] = duration_minutes
    ais = cached.get("ais") if isinstance(cached.get("ais"), dict) else current_ais_context(limit=12)
    nearby = []
    for target in ais.get("targets", []):
        nearby.append({key: target.get(key) for key in ("id", "name", "distance_nm", "bearing_deg", "speed_kn", "course_deg", "position_age_minutes", "position_stale") if target.get(key) is not None})
    maintenance = session_store.list_maintenance()
    today = dt.datetime.now(LOCAL_TZ).date()
    for task in maintenance:
        task["due_status"] = "complete" if task.get("completed") else "none"
        try:
            due = dt.date.fromisoformat(str(task.get("due_date") or ""))
            if not task.get("completed"):
                task["due_status"] = "overdue" if due < today else "soon" if (due - today).days <= 14 else "scheduled"
        except ValueError:
            pass
    return {
        "alerts": alerts,
        "alert_count": sum(item["severity"] != "ok" for item in alerts),
        "trip": trip_payload,
        "position": position,
        "ais": {"targets": nearby, "target_count": ais.get("target_count", 0), "error": ais.get("error")},
        "maintenance": maintenance,
        "maintenance_counts": {"open": sum(not item.get("completed") for item in maintenance), "overdue": sum(item.get("due_status") == "overdue" for item in maintenance)},
        "generated_at": dt.datetime.now(LOCAL_TZ).isoformat(),
    }


def capabilities_payload() -> dict[str, Any]:
    return {"groups":[
        {"name":"Current status","prompts":["Give me a current boat status briefing","Where is the boat?","Is anything stale?"]},
        {"name":"Engines & fuel","prompts":["Compare both engines right now","How much fuel did I use this weekend?","Graph engine temperatures during the last trip"]},
        {"name":"Electrical","prompts":["Battery voltage over the past 2 weeks","When did shore power turn off?","How much did solar contribute last week?"]},
        {"name":"Safety & history","prompts":["Did the bilge pump run last night?","What happened during the last trip?","Show active warnings"]},
    ],"limitations":["Solar production is inferred until a dedicated controller is connected.","Mechanical diagnoses require inspection; telemetry shows observations, not certainty."]}


def answer_experience(context: dict[str, Any]) -> dict[str, Any]:
    plan=context.get("query_plan") or {}; profile=context.get("context_profile") or {}; window=plan.get("window") or {}; evidence=[]
    if profile.get("live_telemetry"): evidence.append({"label":"Live telemetry","detail":"SignalK current vessel state"})
    if profile.get("ha_entities"): evidence.append({"label":"Home Assistant","detail":f"{profile['ha_entities']} selected entities"})
    if profile.get("history"): evidence.append({"label":"Retained history","detail":window.get("label") or "historical telemetry"})
    if window: evidence.append({"label":"Time window","detail":f"{window.get('start_local')} to {window.get('stop_local')}"})
    if plan.get("filters"): evidence.append({"label":"Filters","detail":", ".join(f"{key.replace('_',' ')} {value}" for key,value in plan["filters"].items())})
    signals=set(plan.get("signals") or []); followups=[]
    if signals.intersection({"engine","rpm","temperature","fuel_rate"}): followups.extend(["Compare port and starboard","Show the last trip"])
    if "battery" in signals: followups.extend(["Graph battery voltage","What happened at the lowest voltage?"])
    if "ais" in signals: followups.append("Which vessel is closest?")
    if plan.get("historical"): followups.append("Compare it with the previous period")
    if not followups: followups=["Give me a current boat status briefing","What can you help me with?"]
    metrics=[]; charts=[]
    battery=context.get("battery_voltage") or {}
    if battery.get("voltage"):
        stats=battery["voltage"]; metrics.append({"label":"Battery voltage","unit":"V","min":stats.get("min"),"avg":stats.get("avg"),"max":stats.get("max"),"latest":stats.get("latest")})
    history=context.get("influx_history") or {}
    for bucket in (history.get("buckets") or {}).values():
        if not isinstance(bucket,dict): continue
        charts.extend(bucket.get("chart_series") or [])
        units=bucket.get("units") or {}
        for measurement,stats in list((bucket.get("numeric_summary") or {}).items())[:3]:
            metrics.append({"label":display_measurement_name(measurement),"unit":units.get(measurement) or unit_for_measurement(measurement,context),"min":stats.get("min"),"avg":stats.get("avg"),"max":stats.get("max"),"latest":stats.get("latest")})
    return {"evidence":evidence,"followups":list(dict.fromkeys(followups))[:3],"metrics":metrics[:4],"charts":charts[:3],"calculation":{"operation":plan.get("operation"),"signals":plan.get("signals",[])},"elapsed_ms":profile.get("elapsed_ms")}


def model_catalog() -> dict[str, Any]:
    suggestions = {key: list(values) for key, values in STATIC_MODEL_SUGGESTIONS.items()}
    installed = ollama_installed_models()
    suggestions["ollama"] = installed or [DEFAULT_OLLAMA_FALLBACK_MODEL, DEFAULT_OLLAMA_MODEL]
    return {
        "suggestions": suggestions,
        "ollama_installed": installed,
        # Which env key holds the primary model for each provider; anything
        # not listed uses BOAT_CHAT_MODEL. Fallbacks always use
        # BOAT_CHAT_FALLBACK_MODEL.
        "model_setting_key": {
            "codex_cli": "BOAT_CHAT_CODEX_MODEL",
            "claude_cli": "BOAT_CHAT_CLAUDE_MODEL",
        },
        "active": active_models(),
    }


def model_call_error(message: str, context: dict[str, Any], reason: str, raise_on_error: bool) -> str:
    if raise_on_error:
        raise ModelCallError(reason)
    return fallback_answer(message, context, reason)


def fallback_answer(message: str, context: dict[str, Any], reason: str | None = None) -> str:
    if reason:
        return "I gathered the available boat data, but the answer model is temporarily unavailable. Please try again shortly."
    return "I gathered the available boat data, but no answer model is configured."


def compact_context(context: dict[str, Any], doc_limit: int = 4) -> dict[str, Any]:
    # question_type and context_strategy are routing metadata, not evidence;
    # excluding them saves prompt tokens without changing answers.
    compact = {
        "boat_facts": context.get("boat_facts"),
        "conversation": context.get("conversation"),
        "interpreted_question": context.get("interpreted_question"),
        "query_plan": context.get("query_plan"),
        "prior_query_plan": context.get("prior_query_plan"),
    }
    question_type = context.get("question_type", {})
    current_first = bool(question_type.get("current_request")) and "freshness" not in set(question_type.get("concepts", []))
    if current_first:
        compact["current_telemetry"] = context.get("current_telemetry")
        compact["ha_states"] = context.get("ha_states")
    if "answer_focus" in context:
        compact["answer_focus"] = context["answer_focus"]
    for key in [
        "influx_history",
        "influx_history_error",
        "ha_event_history",
        "ha_event_history_error",
        "propulsion_24h",
        "propulsion_7d",
        "propulsion_error",
        "engine_run_history",
        "engine_run_history_error",
        "engine_first_run",
        "engine_first_run_error",
        "fuel_usage",
        "fuel_usage_error",
        "fuel_economy",
        "fuel_economy_error",
        "shore_power_history",
        "shore_power_history_error",
        "solar_inference",
        "solar_inference_error",
        "battery_voltage",
        "battery_voltage_error",
        "ha_telemetry",
        "ha_telemetry_error",
        "fuel_balance_24h",
        "fuel_balance_7d",
        "fuel_balance_error",
    ]:
        if key in context:
            compact[key] = context[key]
    if not current_first:
        compact["current_telemetry"] = context.get("current_telemetry")
        compact["ha_states"] = context.get("ha_states")
    docs = []
    for item in context.get("local_docs", [])[:doc_limit]:
        docs.append(
            {
                "path": item.get("path"),
                "line": item.get("line"),
                "title": item.get("title"),
                "excerpt": str(item.get("excerpt", ""))[:1200],
            }
        )
    compact["local_docs"] = docs
    return compact


def build_prompt(message: str, context: dict[str, Any], max_chars: int = 50000) -> str:
    # Context comes first and the question last: local models (Ollama) reuse the
    # KV cache for a shared prompt prefix, so keeping the stable content up
    # front makes repeat questions much faster. Compact separators cut ~25% of
    # the JSON tokens versus indent=2.
    prompt_context = compact_context(context)
    context_json = serialize_context_with_budget(prompt_context, max_chars)
    mode=context.get("answer_mode","concise")
    instruction={"concise":"Keep the answer concise.","explain":"Explain the evidence and calculation clearly.","diagnose":"Organize the answer as observations, likely causes, and checks.","checklist":"Return a short actionable checklist."}.get(mode,"Keep the answer concise.")
    return "Boat context JSON:\n" + context_json + f"\n\nResponse mode: {mode}. {instruction}" + "\n\nQuestion:\n" + message


def serialize_context_with_budget(context: dict[str, Any], max_chars: int) -> str:
    """Return valid JSON while dropping low-value evidence as whole records."""
    working = json.loads(json.dumps(context, default=str))
    encoded = json.dumps(working, separators=(",", ":"))
    if len(encoded) <= max_chars:
        return encoded
    working["conversation"] = (working.get("conversation") or [])[-2:]
    working["local_docs"] = (working.get("local_docs") or [])[:2]
    for _ in range(20):
        encoded = json.dumps(working, separators=(",", ":"))
        if len(encoded) <= max_chars:
            return encoded
        lists: list[tuple[int, dict[str, Any], str]] = []
        def find_lists(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if isinstance(value, list) and len(value) > 1:
                        lists.append((len(json.dumps(value, default=str)), node, key))
                    else:
                        find_lists(value)
        find_lists(working)
        if not lists:
            break
        _, parent, key = max(lists, key=lambda item: item[0])
        parent[key] = parent[key][:max(1, len(parent[key]) // 2)]
    minimal = {key: working.get(key) for key in ("boat_facts", "interpreted_question", "query_plan", "answer_focus") if working.get(key) is not None}
    return json.dumps(minimal, separators=(",", ":"))


def context_char_budget(context: dict[str, Any]) -> int:
    configured = int(setting_value("BOAT_CHAT_CONTEXT_CHARS", "12000") or "12000")
    if context.get("question_type", {}).get("complex_history"):
        return max(configured, 18000)
    return configured


def build_cli_prompt(message: str, context: dict[str, Any]) -> str:
    max_chars = context_char_budget(context)
    return (
        "You are Boat Chat's final-response model. The Boat Chat application has already gathered all allowed "
        "local context and telemetry. Do not run shell commands, browse, edit files, or inspect the filesystem. "
        "Use only the supplied prompt and context. Return only the user-facing answer.\n\n"
        "System rules:\n"
        + system_prompt()
        + "\n\n"
        + build_prompt(message, context, max_chars=max_chars)
    )


def extract_chat_completion_text(response: dict[str, Any]) -> str:
    choices = response.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [part.get("text", "") for part in content if isinstance(part, dict)]
            return "\n".join(text for text in texts if text)
    return json.dumps(response, indent=2)[:12000]


def call_openai_compatible(
    message: str,
    context: dict[str, Any],
    *,
    provider_name: str,
    base_url: str,
    api_key: str,
    model: str,
    raise_on_error: bool = False,
) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": build_prompt(message, context)},
        ],
        "temperature": 0.2,
        "max_tokens": int(setting_value("BOAT_CHAT_MAX_TOKENS", "1200") or "1200"),
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = base_url.rstrip("/") + "/chat/completions"
    try:
        response = http_post_json(url, payload, headers=headers, timeout=90)
        return extract_chat_completion_text(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        return model_call_error(message, context, f"{provider_name} API error {exc.code}: {body}", raise_on_error)
    except Exception as exc:
        return model_call_error(message, context, f"{provider_name} API call failed: {exc}", raise_on_error)


def call_openai(
    message: str,
    context: dict[str, Any],
    *,
    model: str | None = None,
    raise_on_error: bool = False,
) -> str:
    api_key = setting_value("OPENAI_API_KEY")
    if not api_key:
        return model_call_error(message, context, "OpenAI is selected but OPENAI_API_KEY is not configured.", raise_on_error)
    model = model or setting_value("BOAT_CHAT_MODEL", DEFAULT_OPENAI_MODEL) or DEFAULT_OPENAI_MODEL
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": build_prompt(message, context)},
        ],
    }
    if (setting_value("BOAT_CHAT_WEB_SEARCH", "false") or "false").lower() in {"1", "true", "yes", "on"}:
        payload["tools"] = [{"type": "web_search"}]
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        response = http_post_json("https://api.openai.com/v1/responses", payload, headers=headers, timeout=90)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        return model_call_error(message, context, f"OpenAI API error {exc.code}: {body}", raise_on_error)
    except Exception as exc:
        return model_call_error(message, context, f"OpenAI API call failed: {exc}", raise_on_error)
    if "output_text" in response:
        return response["output_text"]
    texts: list[str] = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if text:
                texts.append(text)
    return "\n".join(texts) if texts else json.dumps(response, indent=2)[:12000]


def sign_aws_v4(method: str, url: str, body: bytes, region: str, service: str = "bedrock") -> dict[str, str]:
    access_key = setting_value("AWS_ACCESS_KEY_ID", "") or ""
    secret_key = setting_value("AWS_SECRET_ACCESS_KEY", "") or ""
    session_token = setting_value("AWS_SESSION_TOKEN", "") or ""
    if not access_key or not secret_key:
        raise RuntimeError("AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are required for Bedrock.")

    parsed = urllib.parse.urlparse(url)
    now = dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    canonical_uri = parsed.path or "/"
    canonical_querystring = parsed.query
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {
        "content-type": "application/json",
        "host": parsed.netloc,
        "x-amz-date": amz_date,
    }
    if session_token:
        headers["x-amz-security-token"] = session_token
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{key}:{headers[key]}\n" for key in sorted(headers))
    canonical_request = "\n".join(
        [method, canonical_uri, canonical_querystring, canonical_headers, signed_headers, payload_hash]
    )
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, credential_scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )

    def hmac_sha256(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    signing_key = hmac_sha256(
        hmac_sha256(hmac_sha256(hmac_sha256(("AWS4" + secret_key).encode(), date_stamp), region), service),
        "aws4_request",
    )
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = (
        "AWS4-HMAC-SHA256 "
        f"Credential={access_key}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {**{key.title(): value for key, value in headers.items()}, "Authorization": authorization}


def call_bedrock(
    message: str,
    context: dict[str, Any],
    *,
    model: str | None = None,
    raise_on_error: bool = False,
) -> str:
    region = setting_value("AWS_REGION") or setting_value("AWS_DEFAULT_REGION")
    model = model or setting_value("BOAT_CHAT_MODEL")
    if not region:
        return model_call_error(message, context, "Bedrock is selected but AWS_REGION or AWS_DEFAULT_REGION is not set.", raise_on_error)
    if not model:
        return model_call_error(message, context, "Bedrock is selected but BOAT_CHAT_MODEL is not set to a Bedrock model ID.", raise_on_error)

    payload = {
        "system": [{"text": system_prompt()}],
        "messages": [{"role": "user", "content": [{"text": build_prompt(message, context)}]}],
        "inferenceConfig": {
            "temperature": 0.2,
            "maxTokens": int(setting_value("BOAT_CHAT_MAX_TOKENS", "1200") or "1200"),
        },
    }
    body = json.dumps(payload).encode("utf-8")
    model_path = urllib.parse.quote(model, safe="")
    url = f"https://bedrock-runtime.{region}.amazonaws.com/model/{model_path}/converse"
    try:
        headers = sign_aws_v4("POST", url, body, region)
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="ignore")
        return model_call_error(message, context, f"Bedrock API error {exc.code}: {body_text}", raise_on_error)
    except Exception as exc:
        return model_call_error(message, context, f"Bedrock API call failed: {exc}", raise_on_error)

    texts: list[str] = []
    for block in data.get("output", {}).get("message", {}).get("content", []):
        text = block.get("text")
        if text:
            texts.append(text)
    return "\n".join(texts) if texts else json.dumps(data, indent=2)[:12000]


def google_access_token() -> str | None:
    token = setting_value("GOOGLE_OAUTH_ACCESS_TOKEN") or setting_value("GOOGLE_CLOUD_ACCESS_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        return None


def extract_google_text(response: dict[str, Any]) -> str:
    texts: list[str] = []
    for candidate in response.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            text = part.get("text")
            if text:
                texts.append(text)
    return "\n".join(texts) if texts else json.dumps(response, indent=2)[:12000]


def call_google(
    message: str,
    context: dict[str, Any],
    *,
    model: str | None = None,
    raise_on_error: bool = False,
) -> str:
    model = model or setting_value("BOAT_CHAT_MODEL", DEFAULT_GOOGLE_MODEL) or DEFAULT_GOOGLE_MODEL
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt()}]},
        "contents": [{"role": "user", "parts": [{"text": build_prompt(message, context)}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": int(setting_value("BOAT_CHAT_MAX_TOKENS", "1200") or "1200"),
        },
    }
    api_key = setting_value("GOOGLE_API_KEY") or setting_value("GEMINI_API_KEY")
    project = setting_value("GOOGLE_CLOUD_PROJECT")
    location = setting_value("GOOGLE_CLOUD_LOCATION", "us-central1") or "us-central1"
    try:
        if api_key:
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                + urllib.parse.quote(model, safe="")
                + ":generateContent?key="
                + urllib.parse.quote(api_key)
            )
            response = http_post_json(url, payload, timeout=90)
        elif project:
            token = google_access_token()
            if not token:
                return model_call_error(
                    message,
                    context,
                    "Google Vertex AI is selected but no access token is available. Set GOOGLE_CLOUD_ACCESS_TOKEN or install/login with gcloud.",
                    raise_on_error,
                )
            url = (
                f"https://{location}-aiplatform.googleapis.com/v1/projects/"
                + urllib.parse.quote(project, safe="")
                + f"/locations/{urllib.parse.quote(location, safe='')}/publishers/google/models/"
                + urllib.parse.quote(model, safe="")
                + ":generateContent"
            )
            response = http_post_json(url, payload, headers={"Authorization": f"Bearer {token}"}, timeout=90)
        else:
            return model_call_error(
                message,
                context,
                "Google is selected but GOOGLE_API_KEY/GEMINI_API_KEY or GOOGLE_CLOUD_PROJECT is not configured.",
                raise_on_error,
            )
        return extract_google_text(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        return model_call_error(message, context, f"Google API error {exc.code}: {body}", raise_on_error)
    except Exception as exc:
        return model_call_error(message, context, f"Google API call failed: {exc}", raise_on_error)


def call_ollama(
    message: str,
    context: dict[str, Any],
    *,
    model: str | None = None,
    raise_on_error: bool = False,
) -> str:
    model = model or setting_value("BOAT_CHAT_MODEL", DEFAULT_OLLAMA_MODEL) or DEFAULT_OLLAMA_MODEL
    base_url = normalize_http_url(setting_value("OLLAMA_HOST", DEFAULT_OLLAMA_URL) or DEFAULT_OLLAMA_URL).rstrip("/")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt()
                + " Be concise. For health questions, answer from ha_states first, especially boat_health_summary, boat_ok, and boat_watch_summary.",
            },
            {
                "role": "user",
                "content": build_prompt(
                    message,
                    context,
                    max_chars=context_char_budget(context),
                ),
            },
        ],
        "stream": False,
        # keep_alive -1 keeps the model resident in RAM (~2 GB for a 3B Q4),
        # avoiding a ~12 s cold reload on every fallback call. num_ctx is
        # pinned so a larger BOAT_CHAT_CONTEXT_CHARS can never silently
        # truncate the front of the prompt at Ollama's default window.
        "keep_alive": -1,
        "options": {
            "temperature": 0.2,
            "num_predict": int(setting_value("BOAT_CHAT_MAX_TOKENS", "1200") or "1200"),
            "num_ctx": int(setting_value("BOAT_CHAT_OLLAMA_NUM_CTX", "8192") or "8192"),
        },
    }
    try:
        response = http_post_json(f"{base_url}/api/chat", payload, timeout=180)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        return model_call_error(message, context, f"Ollama API error {exc.code}: {body}", raise_on_error)
    except Exception as exc:
        return model_call_error(
            message,
            context,
            f"Ollama API call failed: {exc}. Is Ollama running at {base_url} and is model {model!r} pulled?",
            raise_on_error,
        )
    content = response.get("message", {}).get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(response.get("response"), str) and response["response"].strip():
        return response["response"]
    return json.dumps(response, indent=2)[:12000]


def call_codex_cli(
    message: str,
    context: dict[str, Any],
    *,
    model: str | None = None,
    raise_on_error: bool = False,
) -> str:
    codex_bin = setting_value("BOAT_CHAT_CODEX_BIN", "codex") or "codex"
    selected_model = model or setting_value("BOAT_CHAT_CODEX_MODEL") or DEFAULT_CODEX_MODEL
    if selected_model and ("/" in selected_model or ":" in selected_model):
        selected_model = DEFAULT_CODEX_MODEL
    effort = (setting_value("BOAT_CHAT_CODEX_EFFORT", DEFAULT_CODEX_EFFORT) or DEFAULT_CODEX_EFFORT).strip().lower()
    if effort not in {"minimal", "low", "medium", "high", "xhigh"}:
        effort = DEFAULT_CODEX_EFFORT
    if effort == "minimal":
        effort = "low"
    timeout = int(setting_value("BOAT_CHAT_CLI_TIMEOUT", str(DEFAULT_CLI_TIMEOUT_SECONDS)) or str(DEFAULT_CLI_TIMEOUT_SECONDS))
    prompt = build_cli_prompt(message, context)

    try:
        with tempfile.TemporaryDirectory(prefix="boat-chat-codex-") as tmpdir:
            output_path = str(Path(tmpdir) / "last-message.txt")
            cmd = [
                codex_bin,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--ignore-rules",
                "--disable",
                "shell_tool",
                "-m",
                selected_model,
                "-c",
                f'model_reasoning_effort="{effort}"',
                "-c",
                'model_verbosity="low"',
                "-c",
                'web_search="disabled"',
                "-c",
                'approval_policy="never"',
                "--output-last-message",
                output_path,
                "-",
            ]
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                cwd=tmpdir,
                timeout=timeout,
                check=False,
            )
            final = ""
            try:
                final = Path(output_path).read_text(errors="ignore").strip()
            except Exception:
                final = ""
            if result.returncode != 0:
                error = (result.stderr or result.stdout or "").strip()[-2000:]
                return model_call_error(message, context, f"Codex CLI failed: {error}", raise_on_error)
            return final or result.stdout.strip() or "(Codex CLI returned no final answer.)"
    except FileNotFoundError:
        return model_call_error(message, context, f"Codex CLI binary not found: {codex_bin}", raise_on_error)
    except subprocess.TimeoutExpired:
        return model_call_error(message, context, f"Codex CLI timed out after {timeout} seconds.", raise_on_error)
    except Exception as exc:
        return model_call_error(message, context, f"Codex CLI call failed: {exc}", raise_on_error)


def call_claude_cli(
    message: str,
    context: dict[str, Any],
    *,
    model: str | None = None,
    raise_on_error: bool = False,
) -> str:
    claude_bin = setting_value("BOAT_CHAT_CLAUDE_BIN", "claude") or "claude"
    selected_model = model or setting_value("BOAT_CHAT_CLAUDE_MODEL") or ""
    effort = (setting_value("BOAT_CHAT_CLAUDE_EFFORT", DEFAULT_CLAUDE_EFFORT) or DEFAULT_CLAUDE_EFFORT).strip().lower()
    if effort not in {"low", "medium", "high", "xhigh", "max"}:
        effort = DEFAULT_CLAUDE_EFFORT
    timeout = int(setting_value("BOAT_CHAT_CLI_TIMEOUT", str(DEFAULT_CLI_TIMEOUT_SECONDS)) or str(DEFAULT_CLI_TIMEOUT_SECONDS))
    max_budget = (setting_value("BOAT_CHAT_CLAUDE_MAX_BUDGET_USD", "") or "").strip()
    prompt = build_cli_prompt(message, context)

    cmd = [
        claude_bin,
        "-p",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--tools",
        "",
        "--effort",
        effort,
    ]
    if selected_model:
        cmd.extend(["--model", selected_model])
    if max_budget:
        cmd.extend(["--max-budget-usd", max_budget])
    cmd.extend(["--", prompt])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd="/tmp",
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            error = (result.stderr or result.stdout or "").strip()[-2000:]
            return model_call_error(message, context, f"Claude CLI failed: {error}", raise_on_error)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = {}
        answer = payload.get("result") if isinstance(payload, dict) else None
        if isinstance(answer, str) and answer.strip():
            return answer.strip()
        return result.stdout.strip() or "(Claude CLI returned no final answer.)"
    except FileNotFoundError:
        return model_call_error(message, context, f"Claude CLI binary not found: {claude_bin}", raise_on_error)
    except subprocess.TimeoutExpired:
        return model_call_error(message, context, f"Claude CLI timed out after {timeout} seconds.", raise_on_error)
    except Exception as exc:
        return model_call_error(message, context, f"Claude CLI call failed: {exc}", raise_on_error)


def call_provider(
    provider: str,
    message: str,
    context: dict[str, Any],
    *,
    model: str | None = None,
    raise_on_error: bool = False,
    fallback: bool = False,
) -> str:
    provider = provider.strip().lower()
    if provider == "local":
        return fallback_answer(message, context)
    if provider == "codex_cli":
        return call_codex_cli(message, context, model=model, raise_on_error=raise_on_error)
    if provider == "claude_cli":
        return call_claude_cli(message, context, model=model, raise_on_error=raise_on_error)
    if provider == "openai":
        return call_openai(message, context, model=model, raise_on_error=raise_on_error)
    if provider == "vercel":
        api_key = setting_value("AI_GATEWAY_API_KEY") or setting_value("VERCEL_OIDC_TOKEN")
        if not api_key:
            return model_call_error(
                message,
                context,
                "Vercel AI Gateway is selected but AI_GATEWAY_API_KEY or VERCEL_OIDC_TOKEN is not set.",
                raise_on_error,
            )
        return call_openai_compatible(
            message,
            context,
            provider_name="Vercel AI Gateway",
            base_url=setting_value("BOAT_CHAT_BASE_URL", "https://ai-gateway.vercel.sh/v1") or "https://ai-gateway.vercel.sh/v1",
            api_key=api_key,
            model=model or setting_value("BOAT_CHAT_MODEL", DEFAULT_VERCEL_MODEL) or DEFAULT_VERCEL_MODEL,
            raise_on_error=raise_on_error,
        )
    if provider == "openai_compatible":
        api_key = setting_value("BOAT_CHAT_API_KEY")
        base_url = setting_value("BOAT_CHAT_BASE_URL")
        model = model or setting_value("BOAT_CHAT_MODEL")
        if not api_key or not base_url or not model:
            return model_call_error(
                message,
                context,
                "OpenAI-compatible mode requires BOAT_CHAT_API_KEY, BOAT_CHAT_BASE_URL, and BOAT_CHAT_MODEL.",
                raise_on_error,
            )
        return call_openai_compatible(
            message,
            context,
            provider_name="OpenAI-compatible provider",
            base_url=base_url,
            api_key=api_key,
            model=model,
            raise_on_error=raise_on_error,
        )
    if provider == "bedrock":
        return call_bedrock(message, context, model=model, raise_on_error=raise_on_error)
    if provider in ("google", "vertex", "gemini"):
        return call_google(message, context, model=model, raise_on_error=raise_on_error)
    if provider == "ollama":
        ollama_model = model or setting_value("BOAT_CHAT_MODEL", DEFAULT_OLLAMA_MODEL) or DEFAULT_OLLAMA_MODEL
        if fallback and not model:
            ollama_model = setting_value("BOAT_CHAT_FALLBACK_MODEL", DEFAULT_OLLAMA_FALLBACK_MODEL) or DEFAULT_OLLAMA_FALLBACK_MODEL
        return call_ollama(message, context, model=ollama_model, raise_on_error=raise_on_error)
    return model_call_error(message, context, f"Unsupported BOAT_CHAT_PROVIDER={provider!r}.", raise_on_error)


def call_model(message: str, context: dict[str, Any]) -> str:
    provider = configured_provider()
    try:
        return call_provider(
            provider,
            message,
            context,
            model=setting_value("BOAT_CHAT_MODEL"),
            raise_on_error=True,
        )
    except ModelCallError as primary_error:
        fallback_provider = configured_fallback_provider()
        if fallback_provider:
            try:
                answer = call_provider(
                    fallback_provider,
                    message,
                    context,
                    model=setting_value("BOAT_CHAT_FALLBACK_MODEL"),
                    raise_on_error=True,
                    fallback=True,
                )
                return (
                    f"{answer}\n\n"
                    f"Provider note: primary {provider} failed, so this answer used fallback "
                    f"{fallback_provider}/{setting_value('BOAT_CHAT_FALLBACK_MODEL', DEFAULT_OLLAMA_FALLBACK_MODEL) or DEFAULT_OLLAMA_FALLBACK_MODEL}."
                )
            except ModelCallError as fallback_error:
                return fallback_answer(
                    message,
                    context,
                    f"Primary provider {provider} failed: {primary_error}. Fallback provider {fallback_provider} failed: {fallback_error}.",
                )
        return fallback_answer(message, context, f"Primary provider {provider} failed: {primary_error}.")


class BoatChatHandler(BaseHTTPRequestHandler):
    server_version = "BoatChat/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args))

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, default=json_default).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Client gave up waiting (common during slow model calls); nothing to send to.
            self.log_message("client disconnected before response was written")

    def settings_write_allowed(self) -> tuple[bool, str]:
        try:
            addr = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False, "unrecognized client address"
        if not (addr.is_loopback or addr.is_private):
            return False, "settings can only be changed from the local network"
        token = (setting_value("BOAT_CHAT_SETTINGS_TOKEN") or "").strip()
        if token:
            provided = (self.headers.get("X-Boat-Chat-Token") or "").strip()
            if not hmac.compare_digest(provided, token):
                return False, "invalid or missing X-Boat-Chat-Token header"
        return True, ""

    def access_allowed(self) -> bool:
        token = (setting_value("BOAT_CHAT_ACCESS_TOKEN") or "").strip()
        if not token:
            return True
        provided = (self.headers.get("X-Boat-Chat-Token") or "").strip()
        return hmac.compare_digest(provided, token)

    def sensitive_access_allowed(self) -> bool:
        token = (setting_value("BOAT_CHAT_ACCESS_TOKEN") or "").strip()
        provided = (self.headers.get("X-Boat-Chat-Token") or "").strip()
        return bool(token) and hmac.compare_digest(provided, token)

    def rate_limit_allowed(self) -> bool:
        now = time.monotonic()
        address = str(self.client_address[0])
        with RATE_LIMIT_LOCK:
            recent = [stamp for stamp in RATE_LIMIT_REQUESTS.get(address, []) if now - stamp < RATE_LIMIT_WINDOW_SECONDS]
            if len(recent) >= RATE_LIMIT_MAX_REQUESTS:
                RATE_LIMIT_REQUESTS[address] = recent
                return False
            recent.append(now)
            RATE_LIMIT_REQUESTS[address] = recent
            return True

    def safe_context(self, context: dict[str, Any]) -> dict[str, Any]:
        debug = (self.headers.get("X-Boat-Chat-Debug") or "").lower() == "true"
        configured = (setting_value("BOAT_CHAT_ACCESS_TOKEN") or setting_value("BOAT_CHAT_SETTINGS_TOKEN") or "").strip()
        provided = (self.headers.get("X-Boat-Chat-Token") or "").strip()
        if debug and configured and hmac.compare_digest(provided, configured):
            return context
        return {"query_plan": context.get("query_plan", {}), "context_profile": context.get("context_profile", {})}

    def read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > MAX_REQUEST_BODY:
            raise ValueError(f"request body must be at most {MAX_REQUEST_BODY} bytes")
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            raise ValueError("JSON object is required")
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

    def static_content_type(self, target: Path) -> str:
        if target.suffix == ".html":
            return "text/html; charset=utf-8"
        if target.suffix == ".css":
            return "text/css; charset=utf-8"
        if target.suffix == ".js":
            return "application/javascript; charset=utf-8"
        if target.suffix == ".svg":
            return "image/svg+xml"
        return "text/plain; charset=utf-8"

    def send_static(self, target: Path | None, include_body: bool = True) -> None:
        if target is None or not target.exists() or not target.is_file():
            self.send_error(404)
            return
        content = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", self.static_content_type(target))
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'")
        self.end_headers()
        if include_body:
            self.wfile.write(content)

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            readiness = readiness_status()
            self.send_json(
                200,
                {
                    "ok": True,
                    "ready": readiness["ready"],
                    "readiness": readiness,
                    "provider": configured_provider(),
                    "fallback_provider": configured_fallback_provider(),
                    "models": active_models(),
                    "settings_token_required": bool((setting_value("BOAT_CHAT_SETTINGS_TOKEN") or "").strip()),
                    "memory": memory_index.status() if self.access_allowed() else {"available": memory_index.db_path().exists()},
                    "telemetry_cache": telemetry_cache.status() if self.access_allowed() else {"available": True},
                    "sessions": session_store.status() if self.access_allowed() else {"available": True},
                },
            )
            return
        elif path == "/api/settings":
            self.send_json(200, public_settings())
            return
        elif path == "/api/models":
            self.send_json(200, model_catalog())
            return
        elif path == "/api/status":
            if not self.sensitive_access_allowed():
                self.send_json(401, {"error":"live status requires BOAT_CHAT_ACCESS_TOKEN"}); return
            self.send_json(200, experience_status()); return
        elif path == "/api/insights":
            if not self.sensitive_access_allowed():
                self.send_json(401, {"error":"insights require BOAT_CHAT_ACCESS_TOKEN"}); return
            self.send_json(200, experience_insights()); return
        elif path == "/api/capabilities":
            self.send_json(200, capabilities_payload()); return
        else:
            self.send_static(self.static_target(path))

    def do_HEAD(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in {"/health", "/api/settings"}:
            self.send_response(200)
            self.end_headers()
            return
        self.send_static(self.static_target(path), include_body=False)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/settings":
            try:
                allowed, reason = self.settings_write_allowed()
                if not allowed:
                    self.send_json(403, {"error": reason})
                    return
                payload = self.read_json_body()
                settings = payload.get("settings", payload)
                if not isinstance(settings, dict):
                    self.send_json(400, {"error": "settings object is required"})
                    return
                self.send_json(200, update_settings(settings))
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception as exc:
                self.send_json(500, {"error": str(exc)})
            return
        if path == "/api/maintenance":
            if not self.sensitive_access_allowed():
                self.send_json(401, {"error":"maintenance requires BOAT_CHAT_ACCESS_TOKEN"}); return
            if not self.rate_limit_allowed():
                self.send_json(429, {"error":"Too many requests; try again in a minute"}); return
            try:
                self.send_json(200, {"task": session_store.save_maintenance(self.read_json_body())})
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
            return
        if path in {"/api/feedback", "/api/session/clear"}:
            if not self.access_allowed():
                self.send_json(401, {"error": "invalid or missing X-Boat-Chat-Token header"}); return
            if not self.rate_limit_allowed():
                self.send_json(429, {"error": "Too many requests; try again in a minute"}); return
            try:
                payload = self.read_json_body()
                if path == "/api/feedback":
                    session_store.record_feedback(str(payload.get("request_id", "")), str(payload.get("session_id", "")), str(payload.get("rating", "")), str(payload.get("note", "")))
                else:
                    session_store.clear_session(str(payload.get("session_id", "")))
                self.send_json(200, {"ok": True})
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
            return
        if path != "/api/chat":
            self.send_error(404)
            return
        if not self.access_allowed():
            self.send_json(401, {"error": "invalid or missing X-Boat-Chat-Token header"})
            return
        if not self.rate_limit_allowed():
            self.send_json(429, {"error": "Too many requests; try again in a minute"})
            return
        if not CHAT_SEMAPHORE.acquire(blocking=False):
            self.send_json(429, {"error": "Boat Chat is busy; try again shortly"})
            return
        started = time.monotonic()
        request_id = hashlib.sha256(f"{time.time_ns()}:{self.client_address[0]}".encode()).hexdigest()[:12]
        try:
            payload = self.read_json_body()
            message = str(payload.get("message", "")).strip()
            if not message:
                self.send_json(400, {"error": "message is required"})
                return
            session_id = session_store.normalize_session_id(payload.get("session_id"))
            stored = session_store.get_session(session_id) if session_id else {"turns": [], "query_plan": {}}
            history = sanitize_conversation_history(payload.get("history")) or sanitize_conversation_history(stored.get("turns"))
            context = collect_context(message, history=history, prior_query_plan=stored.get("query_plan"))
            answer_mode=str(payload.get("answer_mode","concise")).lower()
            context["answer_mode"] = answer_mode if answer_mode in {"concise","explain","diagnose","checklist"} else "concise"
            answer = (
                answer_from_clarification(context)
                or answer_from_ais_freshness(context)
                or answer_from_freshness(context)
                or answer_from_facts(message, context.get("boat_facts", {}))
                or answer_from_briefing(message, context)
                or answer_from_health_state(message, context)
                or answer_from_engine_history(context)
                or answer_from_fuel_usage(context)
                or answer_from_fuel_economy(context)
                or answer_from_solar_hardware(context)
                or answer_from_solar_inference(context)
                or answer_from_shore_power_history(context)
                or answer_from_battery_voltage(context)
                or answer_from_fuel_balance(context)
                or answer_from_telemetry_overview(context)
                or answer_from_event_history(context)
                or answer_from_generic_history(context)
                or call_model(message, context)
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            session_store.append_exchange(session_id, message, answer, context.get("query_plan", {}))
            session_store.record_request(request_id, session_id, hashlib.sha256(message.encode()).hexdigest(), context.get("query_plan", {}), context.get("context_profile", {}), elapsed_ms, "answered")
            self.log_message("request_id=%s elapsed_ms=%s signals=%s history=%s", request_id, elapsed_ms, context.get("query_plan", {}).get("signals"), context.get("query_plan", {}).get("historical"))
            self.send_json(200, {"answer":answer,"context":self.safe_context(context),"experience":answer_experience(context),"request_id":request_id})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})
        finally:
            CHAT_SEMAPHORE.release()


def main() -> None:
    host = setting_value("BOAT_CHAT_HOST", DEFAULT_HOST) or DEFAULT_HOST
    port = int(setting_value("BOAT_CHAT_PORT", str(DEFAULT_PORT)) or str(DEFAULT_PORT))
    server = ThreadingHTTPServer((host, port), BoatChatHandler)
    print(f"Boat Chat listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
