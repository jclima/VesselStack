#!/usr/bin/env python3
"""VesselStack opt-in alert dispatcher (README "Alerts").

Run by vesselstack-alerts.timer once a minute (oneshot) as the VesselStack service
user.  It only SENDS Telegram messages (sendMessage) - it never calls getUpdates, so
it cannot collide with the Boat Chat bot that already polls the same bot token.

Subcommands: run (default), test, status, doctor.  Standard library only.
Secrets (bot token, heartbeat URL, chat ids) are never written to stdout/stderr.
Exit status of `run` is 0 for any network, Telegram or internal failure so the timer keeps
going (an internal failure is recorded in the state file and shown by `doctor`).  The script
refuses to run as root.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import io
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DISCLAIMER = "VesselStack alerts are a convenience, not a sole alarm system."
BATTERY_HOLD_S = 120
BILGE_HOLD_S = 0          # a bilge-on reading is announced on the first poll (cycles can be shorter than a minute)
SHORE_HOLD_S = 60
NOT_REPORTING_HOLD_S = 300
DOWN_HOLD_S = 180
RUN_DEADLINE_S = 90
STALE_RANGE_CAP_MIN = 60
ENTITY_RE = re.compile(r"[a-z_]+\.[A-Za-z0-9_]+")
BACKUP_LAST_FILE = "/var/lib/vesselstack/backup-last.json"
OVERRIDE_KEYS = {
    "TELEGRAM_BOT_TOKEN", "HOME_ASSISTANT_TOKEN", "HOME_ASSISTANT_URL", "INFLUXDB_URL",
    "INFLUXDB_TOKEN", "INFLUXDB_ORG", "SIGNALK_URL",
}
SECRET_KEYS = ("TELEGRAM_BOT_TOKEN", "ALERT_HEARTBEAT_URL", "HOME_ASSISTANT_TOKEN", "INFLUXDB_TOKEN")


# ---------------------------------------------------------------- settings

def parse_env_file(path: str | Path, keys: set[str] | None = None) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = Path(path).read_text(errors="ignore").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if keys is not None and key not in keys:
            continue
        try:
            parts = shlex.split(value, comments=False, posix=True)
            values[key] = " ".join(parts) if parts else ""
        except ValueError:
            values[key] = value.strip().strip("'\"")
    return values


def load_settings(environ: dict[str, str], env_files: list[str]) -> dict[str, str]:
    """Environment, then --env-file files, then the Boat Chat settings-page override
    (which wins for the keys it owns, exactly like Boat Chat itself)."""
    settings = dict(environ)
    for path in env_files:
        settings.update(parse_env_file(path))
    override = environ.get("BOAT_CHAT_ENV") or ""
    if override:
        settings.update(parse_env_file(override, OVERRIDE_KEYS))
    return settings


def chat_ids(settings: dict[str, str]) -> list[str]:
    return [item for item in re.split(r"[\s,;]+", settings.get("ALERT_TELEGRAM_CHAT_IDS", "")) if item]


def decimal(value: str) -> float | None:
    try:
        return float(value.strip()) if value and value.strip() else None
    except ValueError:
        return None


def positive_int(value: str, default: int) -> int:
    try:
        number = int(str(value).strip())
        return number if number > 0 else default
    except ValueError:
        return default


class Scrubber:
    """Removes secret values from any text before it can be printed."""

    def __init__(self, settings: dict[str, str]):
        values = [settings.get(key, "") for key in SECRET_KEYS] + chat_ids(settings)
        self.values = sorted({v for v in values if v and len(v) >= 3}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for value in self.values:
            text = text.replace(value, "***")
        text = re.sub(r"/bot[0-9]+:[A-Za-z0-9_-]+", "/bot***", text)
        return re.sub(r"https?://\S+", "<url>", text)


# ---------------------------------------------------------------- I/O boundary (faked in tests)

class Io:
    def now(self) -> float:
        return time.time()

    def http(self, method: str, url: str, headers: dict[str, str] | None = None, body: bytes | None = None, timeout: float = 10) -> tuple[int, bytes]:
        """Returns (status, body). HTTP error statuses are returned; connection problems raise OSError."""
        request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read(1 << 20)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read(1 << 16)
            except OSError:
                return exc.code, b""
        except urllib.error.URLError as exc:
            raise OSError(type(exc.reason).__name__ if not isinstance(exc.reason, str) else "url error") from None

    def monotonic(self) -> float:
        return time.monotonic()

    def euid(self) -> int:
        return os.geteuid()

    def unit_active(self, unit: str) -> bool:
        try:
            return subprocess.run(["systemctl", "is-active", "--quiet", unit], timeout=10, check=False).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return True  # cannot tell: do not raise a false alarm

    def backup_running(self) -> bool:
        try:
            return subprocess.run(["systemctl", "is-active", "--quiet", "vesselstack-backup.service"], timeout=10, check=False).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def boot_id(self) -> str:
        try:
            return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except OSError:
            return ""

    def previous_shutdown(self) -> str:
        """'clean', 'unclean' or 'unknown' from the previous boot's journal tail."""
        try:
            out = subprocess.run(["journalctl", "-b", "-1", "-q", "--no-pager", "-n", "60", "-o", "cat"],
                                 capture_output=True, text=True, timeout=20, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        if not out.strip():
            return "unknown"
        if re.search(r"Journal stopped|Reached target.*(Shutdown|Reboot|Power-Off|Halt)|systemd-shutdown", out, re.I):
            return "clean"
        return "unclean"

    def read_json(self, path: str) -> Any:
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None


# ---------------------------------------------------------------- state

def empty_state() -> dict[str, Any]:
    return {"version": 1, "alerts": {}, "events": {}, "boot_id": "", "last_send": {}}


def read_text_nofollow(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        return handle.read(8 << 20)


def load_state(path: Path, repair: bool = True) -> dict[str, Any]:
    """repair=False is read-only (status/doctor, which do not hold the lock): a corrupt file is ignored, not renamed."""
    try:
        data = json.loads(read_text_nofollow(path))
        if isinstance(data, dict) and isinstance(data.get("alerts"), dict):
            base = empty_state()
            base.update(data)
            if not isinstance(base.get("events"), dict):
                base["events"] = {}
            return base
    except (OSError, ValueError):
        pass
    if repair and os.path.lexists(path):
        try:
            os.replace(path, path.with_name(path.name + ".corrupt"))
        except OSError:
            pass
    return empty_state()


def open_lock(state_path: Path):
    """Opens (creating) state.lock inside the alerts directory without following symlinks."""
    directory = state_path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open("state.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    return os.fdopen(fd, "w")


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, sort_keys=True, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def clamp_state(state: dict[str, Any], now: float) -> None:
    """A stored timestamp in the future (the clock went backwards) would silence alerts: pull it back to now."""
    def fix(container: dict[str, Any], key: str) -> None:
        stamp = parse_iso(container.get(key))
        if stamp is not None and stamp > now:
            container[key] = iso(now)
    for entry in state["alerts"].values():
        if isinstance(entry, dict):
            for key in ("first_seen", "last_sent", "missing_since"):
                fix(entry, key)
    for key in ("last_run_at", "last_data_at"):
        fix(state, key)
    if isinstance(state.get("last_send"), dict):
        fix(state["last_send"], "at")
    for key in list(state["events"]):
        fix(state["events"], key)


def iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------- Telegram

class Telegram:
    def __init__(self, io: Io, settings: dict[str, str], scrub: Scrubber, log):
        self.io, self.token, self.ids, self.scrub, self.log = io, settings.get("TELEGRAM_BOT_TOKEN", "").strip(), chat_ids(settings), scrub, log

    def ready(self) -> str:
        if not self.token:
            return "no Telegram bot token (set it in Boat Chat Settings)"
        if not self.ids:
            return "no chat ids (ALERT_TELEGRAM_CHAT_IDS)"
        return ""

    def send(self, text: str) -> tuple[bool, str]:
        """Sends to every chat id. True when at least one chat received it."""
        problem = self.ready()
        if problem:
            return False, problem
        delivered, errors = 0, []
        message = f"{text}\n\n{DISCLAIMER}"
        for chat_id in self.ids:
            payload = json.dumps({"chat_id": chat_id, "text": message, "disable_web_page_preview": True}).encode()
            try:
                status, body = self.io.http("POST", f"https://api.telegram.org/bot{self.token}/sendMessage",
                                            {"Content-Type": "application/json"}, payload, 15)
            except OSError as exc:
                errors.append(self.scrub(f"network error ({exc})"))
                continue
            if status == 200:
                delivered += 1
            else:
                detail = ""
                try:
                    detail = str(json.loads(body.decode("utf-8", "ignore")).get("description", ""))[:120]
                except ValueError:
                    pass
                errors.append(self.scrub(f"Telegram HTTP {status} {detail}".strip()))
        return (delivered > 0), "; ".join(errors[:3])


# ---------------------------------------------------------------- probes

def flux_rows(text: str) -> list[dict[str, str]]:
    rows, header = [], None
    for record in csv.reader(io.StringIO(text)):
        if not record or record[0].startswith("#"):
            continue
        if "_value" in record and "_time" in record:
            header = record
            continue
        if header and len(record) == len(header):
            rows.append(dict(zip(header, record)))
    return rows


class Probes:
    def __init__(self, io: Io, settings: dict[str, str], deadline: float | None = None):
        self.io, self.s, self.deadline, self.skipped = io, settings, deadline, 0
        self.influx_url = (settings.get("INFLUXDB_URL") or "http://127.0.0.1:8086").rstrip("/")
        self.bucket = settings.get("INFLUXDB_RAW_BUCKET") or "signalk"

    def expired(self) -> bool:
        """True once the per-run deadline has passed; the caller then skips that probe (and it is counted)."""
        if self.deadline is not None and self.io.monotonic() > self.deadline:
            self.skipped += 1
            return True
        return False

    def flux(self, query: str) -> list[dict[str, str]] | None:
        org = urllib.parse.quote(self.s.get("INFLUXDB_ORG") or "vesselstack")
        headers = {"Authorization": f"Token {self.s.get('INFLUXDB_TOKEN', '')}", "Accept": "text/csv", "Content-Type": "application/json"}
        try:
            status, body = self.io.http("POST", f"{self.influx_url}/api/v2/query?org={org}", headers,
                                        json.dumps({"query": query, "type": "flux"}).encode(), 25)
        except OSError:
            return None
        return flux_rows(body.decode("utf-8", "ignore")) if status == 200 else None

    def battery_voltages(self) -> dict[str, float] | None:
        """Newest self voltage per electrical.batteries.*.voltage in the last 10 minutes."""
        query = (f'from(bucket: "{self.bucket}") |> range(start: -10m) '
                 '|> filter(fn: (r) => r._measurement =~ /^electrical\\.batteries\\..*\\.voltage$/ and r._field == "value" and r.self == "true") '
                 '|> last()')
        rows = self.flux(query)
        if rows is None:
            return None
        result: dict[str, float] = {}
        for row in rows:
            try:
                result[row["_measurement"]] = float(row["_value"])
            except (KeyError, ValueError):
                continue
        return result

    def newest_self_point(self, window_minutes: int) -> tuple[bool, float | None]:
        """(known, newest epoch or None when the window holds no point). Newest of the per-series last() rows."""
        query = (f'from(bucket: "{self.bucket}") |> range(start: -{window_minutes}m) '
                 '|> filter(fn: (r) => r._field == "value" and r.self == "true") |> last()')
        rows = self.flux(query)
        if rows is None:
            return False, None
        newest = None
        for row in rows:
            try:
                stamp = dt.datetime.fromisoformat(row["_time"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError):
                continue
            newest = stamp if newest is None else max(newest, stamp)
        return True, newest

    def ha_state(self, entity: str) -> str | None:
        """Lower-case state; 'missing' for HTTP 404; None when it cannot be read (no token, HA down, other error)."""
        token = self.s.get("HOME_ASSISTANT_TOKEN", "")
        if not token or not ENTITY_RE.fullmatch(entity):
            return None
        base = (self.s.get("HOME_ASSISTANT_URL") or "http://127.0.0.1:8123").rstrip("/")
        try:
            status, body = self.io.http("GET", f"{base}/api/states/{urllib.parse.quote(entity, safe='')}", {"Authorization": f"Bearer {token}"}, None, 10)
        except OSError:
            return None
        if status == 404:
            return "missing"
        if status != 200:
            return None
        try:
            return str(json.loads(body.decode()).get("state", "")).strip().lower()
        except ValueError:
            return None

    def http_up(self, url: str) -> bool:
        try:
            status, _ = self.io.http("GET", url, {}, None, 8)
        except OSError:
            return False
        return status < 500  # 401/403 still prove the service answers


def disabled(settings: dict[str, str]) -> set[str]:
    return {item.strip() for item in settings.get("VESSELSTACK_DISABLED_COMPONENTS", "").split(",") if item.strip()}


def component_plan(io: Io, settings: dict[str, str], probes: Probes) -> list[tuple[str, Any]]:
    """[(component name, probe callable returning is-it-up)]. Names are fixed labels, never URLs."""
    off, plan = disabled(settings), []
    if "influxdb" not in off:
        plan.append(("InfluxDB", lambda: probes.http_up(f"{probes.influx_url}/health")))
    if "homeassistant" not in off:
        plan.append(("Home Assistant", lambda: probes.http_up((settings.get("HOME_ASSISTANT_URL") or "http://127.0.0.1:8123").rstrip("/") + "/")))
    if settings.get("SIGNALK_MODE", "existing") in {"docker", "native"}:
        plan.append(("Signal K", lambda: probes.http_up((settings.get("SIGNALK_URL") or "http://127.0.0.1:3000").rstrip("/") + "/signalk")))
    units = ["vesselstack-chat.service", "vesselstack-control-panel.service"]
    flag = lambda key, default="false": settings.get(key, default) == "true"  # noqa: E731
    if flag("TELEGRAM_ENABLE"):
        units.append("vesselstack-chat-telegram.service")
    if flag("TELEMETRY_INDEXER_ENABLE", "true"):
        units += ["vesselstack-telemetry-sample.timer", "vesselstack-telemetry-indexer.timer"]
    if flag("DISPLAYS_ENABLE"):
        units.append("vesselstack-displays-bridge.service")
    if settings.get("SIGNALK_MODE") == "native":
        units.append("vesselstack-signalk.service")
    if flag("SOCKETCAN_ENABLE"):
        units.append("vesselstack-socketcan.service")
    if settings.get("VESSELSTACK_BACKUP_SCHEDULE", "off") in {"daily", "weekly"}:
        units.append("vesselstack-backup.timer")
    for unit in units:
        plan.append((unit, lambda unit=unit: io.unit_active(unit)))
    return plan


# ---------------------------------------------------------------- the run

class Cond:
    """keep=True means "cannot tell": an existing entry is left unchanged (and still repeats), never cleared."""
    __slots__ = ("active", "text", "clear", "hold", "keep")

    def __init__(self, active: bool, text: str, clear: str, hold: int = 0, keep: bool = False):
        self.active, self.text, self.clear, self.hold, self.keep = active, text, clear, hold, keep


def battery_short(key: str) -> str:
    return key.removeprefix("electrical.batteries.").removesuffix(".voltage")


def evaluate(io: Io, settings: dict[str, str], probes: Probes, state: dict[str, Any], now: float):
    """Returns (conditions, events, configured key families, known component names or None while paused)."""
    conds: dict[str, Cond] = {}
    events: list[tuple[str, str]] = []  # (dedupe key, text)
    families: set[str] = set()
    known_down: set[str] | None = None
    # Only the component-down and stale-data checks pause during a backup (the stack is stopped on purpose).
    paused = io.backup_running()
    alerts_state = state["alerts"]

    def keep(key: str) -> None:
        if key in alerts_state:
            conds[key] = Cond(True, "", "", 0, keep=True)

    if not paused:
        plan = component_plan(io, settings, probes)
        known_down = {name for name, _ in plan}
        for name, probe in plan:
            if probes.expired():
                continue
            up = probe()
            conds[f"down:{name}"] = Cond(not up, f"{name} is down.", f"{name} is back up.", DOWN_HOLD_S)

    stale_min = positive_int(settings.get("ALERT_STALE_DATA_MINUTES", "15"), 15)
    low = decimal(settings.get("ALERT_BATTERY_LOW_V", ""))
    if low is not None:
        families |= {"battery", "battery-missing"}
        volts = None if probes.expired() else probes.battery_voltages()
        if volts is not None:
            for key, value in volts.items():
                short = battery_short(key)
                conds[f"battery:{key}"] = Cond(value < low, f"Battery '{short}' is low: {value:.2f} V (alert threshold {low:g} V).",
                                               f"Battery '{short}' voltage recovered: {value:.2f} V.", BATTERY_HOLD_S)
            for akey in list(alerts_state):
                if not akey.startswith("battery:"):
                    continue
                entry = alerts_state[akey]
                short = battery_short(akey.split(":", 1)[1])
                if akey.split(":", 1)[1] in volts:
                    entry.pop("missing_since", None)
                    continue
                keep(akey)  # no reading: the low-battery alert stays as it is, and keeps repeating
                since = parse_iso(entry.get("missing_since"))
                if since is None:
                    since = now
                    entry["missing_since"] = iso(now)
                if now - since >= stale_min * 60:
                    conds[f"battery-missing:{akey.split(':', 1)[1]}"] = Cond(
                        True, f"Battery '{short}' data is missing: no voltage reading for over {stale_min} minutes, so the low-battery alert cannot be updated.",
                        f"Battery '{short}' voltage readings are back.")
            for akey in list(alerts_state):
                if akey.startswith("battery-missing:") and akey not in conds:
                    conds[akey] = Cond(False, "", f"Battery '{battery_short(akey.split(':', 1)[1])}' voltage readings are back.")

    datapath = settings.get("SIGNALK_MODE", "existing") != "existing" and settings.get("SIGNALK_DATAPATH_AUTOCONFIG", "true") == "true" \
        and "influxdb" not in disabled(settings)
    if datapath:
        families.add("stale")
        if not paused and not probes.expired():
            window = min(max(stale_min * 2, 30), STALE_RANGE_CAP_MIN)
            known, newest = probes.newest_self_point(window)
            if known:
                seen = parse_iso(state.get("last_data_at"))
                if newest is not None:
                    seen = max(newest, seen or 0)
                    state["last_data_at"] = iso(seen)
                age = now - seen if seen is not None else float(window * 60)
                conds["stale"] = Cond(age > stale_min * 60, f"No Signal K data has been logged for over {stale_min} minutes.", "Signal K data is flowing again.")

    entity_404 = state.setdefault("entity_404", {})
    for key, setting, bad, good, bad_hold, bad_text, bad_clear in (
        ("bilge", "ALERT_BILGE_ENTITY", "on", "off", BILGE_HOLD_S, "Bilge alarm: {e} is on.", "Bilge sensor {e} is off again."),
        ("shore", "ALERT_SHORE_POWER_ENTITY", "off", "on", SHORE_HOLD_S, "Shore power lost: {e} is off.", "Shore power is back: {e} is on."),
    ):
        entity = settings.get(setting, "").strip()
        if not entity or not ENTITY_RE.fullmatch(entity):
            continue
        nr = f"{key}-nr"
        families |= {key, nr}
        if probes.expired():
            keep(key), keep(nr)
            continue
        current = probes.ha_state(entity)
        label = "Bilge" if key == "bilge" else "Shore power"
        nr_text = f"{label} sensor not reporting: {entity} is {current if current not in (None, 'missing') else 'not found in Home Assistant'}."
        if current is None:
            keep(key), keep(nr)          # Home Assistant unreachable or no token: cannot tell
            continue
        entity_404[key] = current == "missing"
        if current == bad:
            conds[key] = Cond(True, bad_text.format(e=entity), bad_clear.format(e=entity), bad_hold)
            conds[nr] = Cond(False, "", f"{label} sensor {entity} is reporting again.")
        elif current == good:
            conds[key] = Cond(False, "", bad_clear.format(e=entity))
            conds[nr] = Cond(False, "", f"{label} sensor {entity} is reporting again.")
        else:                              # unavailable, unknown, empty, missing: this is NOT an all-clear
            keep(key)
            conds[nr] = Cond(True, nr_text, f"{label} sensor {entity} is reporting again.", NOT_REPORTING_HOLD_S)

    # Boot change
    boot = io.boot_id()
    if boot:
        if state.get("boot_id") and state["boot_id"] != boot:
            shutdown = io.previous_shutdown()
            note = {"unclean": "The previous shutdown was not clean.", "clean": "The previous shutdown was clean."}.get(shutdown, "Whether the previous shutdown was clean is unknown.")
            events.append((f"boot:{boot}", f"The boat computer restarted. {note}"))
        elif not state.get("boot_id"):
            state["boot_id"] = boot

    # Backups
    record = io.read_json(BACKUP_LAST_FILE)
    interval = {"daily": 86400, "weekly": 604800}.get(settings.get("VESSELSTACK_BACKUP_SCHEDULE", "off"))
    if interval:
        families.add("backup-stale")
    if isinstance(record, dict):
        if record.get("result") == "failed" and record.get("finished_at"):
            status = re.sub(r"[^0-9A-Za-z_-]", "", str(record.get("exit_status", "")))
            events.append((f"backup-failed:{record['finished_at']}", f"The scheduled backup failed (exit status {status or 'unknown'}). Run: sudo vesselstackctl doctor"))
        success = parse_iso(record.get("last_success_at"))
        if interval and success is not None:
            conds["backup-stale"] = Cond(now - success > 2 * interval, f"No successful scheduled backup since {record['last_success_at']}.", "Scheduled backups are succeeding again.")
    return conds, events, families, known_down


def prune_orphans(state: dict[str, Any], families: set[str], known_down: set[str] | None) -> None:
    """Drop entries whose setting no longer exists (e.g. a threshold was blanked), so they cannot stick forever."""
    for key in list(state["alerts"]):
        family, _, rest = key.partition(":")
        if family == "down":
            if known_down is not None and rest not in known_down:
                del state["alerts"][key]
        elif family not in families:
            del state["alerts"][key]


def run(settings: dict[str, str], io: Io, state_path: Path, log, out) -> int:
    scrub = Scrubber(settings)
    if settings.get("ALERTS_ENABLE", "false") != "true":
        return 0
    state = load_state(state_path)
    now = io.now()
    clamp_state(state, now)
    deadline = io.monotonic() + RUN_DEADLINE_S
    probes, telegram = Probes(io, settings, deadline), Telegram(io, settings, scrub, log)
    repeat = positive_int(settings.get("ALERT_REPEAT_MINUTES", "60"), 60) * 60
    conds, events, families, known_down = evaluate(io, settings, probes, state, now)
    prune_orphans(state, families, known_down)
    sends = failures = skipped_sends = 0
    held_down = False

    def deliver(text: str) -> bool:
        nonlocal sends, failures, skipped_sends
        if io.monotonic() > deadline:
            skipped_sends += 1       # not a failure: it stays pending and goes out next run
            return False
        ok, error = telegram.send(text)
        sends += 1
        state["last_send"] = {"ok": ok, "at": iso(now), "error": "" if ok else error}
        if not ok:
            failures += 1
            log(f"alert send failed: {error}")
        return ok

    # Events first: a reboot clears last_sent so that still-active alerts are announced again.
    done = state["events"]
    for key, text in events:
        if key in done:
            continue
        if deliver(text):
            done[key] = iso(now)
            if key.startswith("boot:"):
                state["boot_id"] = key.split(":", 1)[1]
                for entry in state["alerts"].values():
                    entry["last_sent"] = ""
            save_state(state_path, state)

    for key, cond in sorted(conds.items()):
        entry = state["alerts"].get(key)
        if cond.keep:
            if entry is None:
                continue
            cond = Cond(True, entry.get("message", key), "", 0)
        elif cond.active:
            if entry is None:
                entry = state["alerts"][key] = {"first_seen": iso(now), "last_sent": "", "message": cond.text}
            elif entry.pop("clearing", False):
                entry["first_seen"], entry["last_sent"] = iso(now), ""   # re-activated after a failed clear: start over
            entry["message"] = cond.text
        if cond.active:
            first = parse_iso(entry["first_seen"]) or now
            last = parse_iso(entry.get("last_sent"))
            if key.startswith("down:") and now - first >= cond.hold:
                held_down = True
            if now - first >= cond.hold and (last is None or now - last >= repeat):
                if deliver(cond.text):
                    entry["last_sent"] = iso(now)
                    save_state(state_path, state)
        elif entry is not None:
            if entry.get("last_sent"):
                entry["clearing"] = True
                if deliver(cond.clear):
                    del state["alerts"][key]
                    save_state(state_path, state)
            else:
                del state["alerts"][key]

    protected = {key for key, _ in events}
    for key in sorted(done, key=lambda k: parse_iso(done[k]) or 0, reverse=True)[50:]:
        if key not in protected:
            del done[key]
    boot = io.boot_id()
    if boot and not any(k.startswith("boot:") for k, _ in events):
        state["boot_id"] = state.get("boot_id") or boot

    skipped = probes.skipped + skipped_sends
    state.update({"last_run_at": iso(now), "last_run_ok": True, "last_error_class": "", "last_run_skipped": skipped})
    save_state(state_path, state)

    url = settings.get("ALERT_HEARTBEAT_URL", "").strip()
    if url.startswith("https://"):
        # /fail only for a real problem: a held component-down alert, a failed Telegram delivery this run, or
        # alerts switched on with nowhere to send them.  A running backup is not a problem.
        problem = held_down or failures > 0 or bool(telegram.ready())
        parts = urllib.parse.urlsplit(url)
        path = parts.path.rstrip("/") + ("/fail" if problem else "")
        target = urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))
        try:
            io.http("GET", target, {}, None, 10)
        except OSError as exc:
            log(scrub(f"heartbeat request failed ({exc})"))
    if failures:
        log(f"{failures} of {sends} alert message(s) could not be sent; will retry")
    if skipped:
        log(f"run deadline reached: {skipped} check(s) or message(s) skipped until the next run")
    return 0


def record_failure(state_path: Path, io: Io, exc: Exception) -> None:
    """Best effort, called while holding the lock: lets `doctor` see that the dispatcher itself is failing."""
    try:
        state = load_state(state_path)
        state.update({"last_run_at": iso(io.now()), "last_run_ok": False, "last_error_class": type(exc).__name__})
        save_state(state_path, state)
    except Exception:  # noqa: BLE001
        pass


def cmd_test(settings: dict[str, str], io: Io, state_path: Path, log, out) -> int:
    scrub = Scrubber(settings)
    ok, error = Telegram(io, settings, scrub, log).send("VesselStack test alert: Telegram alerts are working.")
    lock = open_lock(state_path)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state(state_path)
        state["last_send"] = {"ok": ok, "at": iso(io.now()), "error": "" if ok else error, "test": True}
        save_state(state_path, state)
    finally:
        lock.close()
    out("Test alert sent." if ok else f"Test alert FAILED: {error}")
    return 0 if ok else 1


def cmd_status(settings: dict[str, str], io: Io, state_path: Path, out) -> int:
    state = load_state(state_path, repair=False)
    enabled = settings.get("ALERTS_ENABLE", "false") == "true"
    out(f"Alerts: {'enabled' if enabled else 'disabled (ALERTS_ENABLE=false)'}")
    out(f"Telegram chats configured: {len(chat_ids(settings))}")
    out("Alerts on: " + ", ".join(name for name, key in (
        ("battery", "ALERT_BATTERY_LOW_V"), ("bilge", "ALERT_BILGE_ENTITY"), ("shore power", "ALERT_SHORE_POWER_ENTITY"),
        ("heartbeat", "ALERT_HEARTBEAT_URL")) if settings.get(key, "").strip()) or "stack components, restarts, stale data, backups only")
    active = state["alerts"]
    if not active:
        out("Active alerts: none")
    for key, entry in sorted(active.items()):
        out(f"Active: {entry.get('message', key)} (since {entry.get('first_seen')}, last sent {entry.get('last_sent') or 'never'})")
    last = state.get("last_send") or {}
    if last:
        out(f"Last send: {'ok' if last.get('ok') else 'FAILED'} at {last.get('at')}" + ("" if last.get("ok") else f" - {last.get('error')}"))
    else:
        out("Last send: none yet")
    if state.get("last_run_at"):
        out(f"Last run: {'ok' if state.get('last_run_ok') else 'FAILED (' + str(state.get('last_error_class') or 'unknown') + ')'} at {state.get('last_run_at')}")
    return 0


def cmd_doctor(settings: dict[str, str], io: Io, state_path: Path, out) -> int:
    """Read-only. Prints WARN lines only; never fails."""
    if settings.get("ALERTS_ENABLE", "false") != "true":
        return 0
    if not chat_ids(settings):
        out("WARN alerts are enabled but ALERT_TELEGRAM_CHAT_IDS is empty. Fix: set it in the Control Panel settings or vesselstack.env and rerun the installer")
    if not settings.get("TELEGRAM_BOT_TOKEN", "").strip():
        out("WARN alerts are enabled but no Telegram bot token is set. Fix: add it in Boat Chat Settings")
    for key in ("ALERT_BILGE_ENTITY", "ALERT_SHORE_POWER_ENTITY"):
        entity = settings.get(key, "").strip()
        if entity and not ENTITY_RE.fullmatch(entity):
            out(f"WARN {key} is not a valid Home Assistant entity id and is ignored")
        elif entity and not settings.get("HOME_ASSISTANT_TOKEN", "").strip():
            out(f"WARN {key} is set but HOME_ASSISTANT_TOKEN is empty, so that sensor is never checked. Fix: add the Home Assistant token in Boat Chat Settings")
    state = load_state(state_path, repair=False)
    for key, missing in sorted((state.get("entity_404") or {}).items()):
        if missing:
            out(f"WARN the {key} entity was not found in Home Assistant (entity not found). Fix: check ALERT_{'BILGE' if key == 'bilge' else 'SHORE_POWER'}_ENTITY")
    last = state.get("last_send") or {}
    if last and not last.get("ok"):
        out(f"WARN the last alert message could not be sent ({last.get('error') or 'unknown error'}) at {last.get('at')}. Try: sudo vesselstackctl alerts test")
    ran = parse_iso(state.get("last_run_at"))
    if ran is None:
        out("WARN the alerts dispatcher has not run yet. Check: systemctl status vesselstack-alerts.timer")
    elif io.now() - ran > 300:
        out(f"WARN the alerts dispatcher last ran at {state.get('last_run_at')}, more than 5 minutes ago. Check: systemctl status vesselstack-alerts.timer vesselstack-alerts.service")
    if ran is not None and state.get("last_run_ok") is False:
        out(f"WARN the last alerts run failed ({state.get('last_error_class') or 'unknown error'}). Check: journalctl -u vesselstack-alerts.service")
    return 0


def main(argv: list[str] | None = None, environ: dict[str, str] | None = None, io: Io | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="run", choices=["run", "test", "status", "doctor"])
    parser.add_argument("--env-file", action="append", default=[])
    parser.add_argument("--data", default=None, help="VesselStack data directory (default $VESSELSTACK_DATA)")
    args = parser.parse_args(argv)
    environ = dict(os.environ if environ is None else environ)
    settings = load_settings(environ, args.env_file)
    data = args.data or settings.get("VESSELSTACK_DATA") or "/opt/vesselstack-data"
    state_path = Path(data) / "alerts" / "state.json"
    io = io or Io()
    scrub = Scrubber(settings)

    def log(message: str) -> None:
        sys.stderr.write(scrub(f"vesselstack-alerts: {message}") + "\n")

    def out(message: str) -> None:
        sys.stdout.write(scrub(message) + "\n")

    if io.euid() == 0:
        log("refusing to run as root; run it as the VesselStack service user (vesselstackctl alerts does this)")
        return 1
    strict = args.command in {"test", "status"}   # these report failure; run/doctor never break the timer or doctor
    try:
        if args.command == "status":
            return cmd_status(settings, io, state_path, out)
        if args.command == "doctor":
            return cmd_doctor(settings, io, state_path, out)
        if args.command == "test":
            return cmd_test(settings, io, state_path, log, out)
        lock = open_lock(state_path)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                return run(settings, io, state_path, log, out)
            except Exception as exc:  # never crash-loop the timer, but make the failure visible to doctor
                log(f"unexpected {type(exc).__name__}; will retry next run")
                record_failure(state_path, io, exc)
                return 0
        finally:
            lock.close()
    except Exception as exc:
        log(f"unexpected {type(exc).__name__}" + ("" if strict else "; will retry next run"))
        return 1 if strict else 0


if __name__ == "__main__":
    sys.exit(main())
