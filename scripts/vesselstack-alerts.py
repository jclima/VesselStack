#!/usr/bin/env python3
"""VesselStack opt-in alert dispatcher (README "Alerts").

Run by vesselstack-alerts.timer once a minute (oneshot) as the VesselStack service
user.  It only SENDS Telegram messages (sendMessage) - it never calls getUpdates, so
it cannot collide with the Boat Chat bot that already polls the same bot token.

Subcommands: run (default), test, status, doctor.  Standard library only.
Secrets (bot token, heartbeat URL, chat ids) are never written to stdout/stderr.
Exit status is 0 for any network or Telegram failure so the timer keeps going.
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
ENTITY_HOLD_S = 60
DOWN_HOLD_S = 180
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


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("alerts"), dict):
            base = empty_state()
            base.update(data)
            return base
    except (OSError, ValueError):
        pass
    if path.exists():
        try:
            os.replace(path, path.with_name(path.name + ".corrupt"))
        except OSError:
            pass
    return empty_state()


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
    def __init__(self, io: Io, settings: dict[str, str]):
        self.io, self.s = io, settings
        self.influx_url = (settings.get("INFLUXDB_URL") or "http://127.0.0.1:8086").rstrip("/")
        self.bucket = settings.get("INFLUXDB_RAW_BUCKET") or "signalk"

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

    def newest_self_point_age(self, now: float, window_minutes: int) -> float | None:
        """Age in seconds of the newest self point (window_minutes if there is none), None when unknown."""
        query = (f'from(bucket: "{self.bucket}") |> range(start: -{window_minutes}m) '
                 '|> filter(fn: (r) => r._field == "value" and r.self == "true") |> group() |> sort(columns: ["_time"]) |> last()')
        rows = self.flux(query)
        if rows is None:
            return None
        newest = None
        for row in rows:
            try:
                stamp = dt.datetime.fromisoformat(row["_time"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError):
                continue
            newest = stamp if newest is None else max(newest, stamp)
        return float(window_minutes * 60) if newest is None else max(0.0, now - newest)

    def ha_state(self, entity: str) -> str | None:
        token = self.s.get("HOME_ASSISTANT_TOKEN", "")
        if not token:
            return None
        base = (self.s.get("HOME_ASSISTANT_URL") or "http://127.0.0.1:8123").rstrip("/")
        try:
            status, body = self.io.http("GET", f"{base}/api/states/{urllib.parse.quote(entity)}", {"Authorization": f"Bearer {token}"}, None, 10)
        except OSError:
            return None
        if status == 404:
            return "unavailable"
        if status != 200:
            return None
        try:
            return str(json.loads(body.decode()).get("state", "")).lower()
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


def component_checks(io: Io, settings: dict[str, str], probes: Probes) -> dict[str, bool]:
    """component name -> is it up. Names are fixed labels, never URLs."""
    off, results = disabled(settings), {}
    if "influxdb" not in off:
        results["InfluxDB"] = probes.http_up(f"{probes.influx_url}/health")
    if "homeassistant" not in off:
        results["Home Assistant"] = probes.http_up((settings.get("HOME_ASSISTANT_URL") or "http://127.0.0.1:8123").rstrip("/") + "/")
    if settings.get("SIGNALK_MODE", "existing") in {"docker", "native"}:
        results["Signal K"] = probes.http_up((settings.get("SIGNALK_URL") or "http://127.0.0.1:3000").rstrip("/") + "/signalk")
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
        results[unit] = io.unit_active(unit)
    return results


# ---------------------------------------------------------------- the run

class Cond:
    __slots__ = ("active", "text", "clear", "hold")

    def __init__(self, active: bool, text: str, clear: str, hold: int = 0):
        self.active, self.text, self.clear, self.hold = active, text, clear, hold


def evaluate(io: Io, settings: dict[str, str], probes: Probes, state: dict[str, Any], now: float):
    """Returns (conditions, events, component_problem, heartbeat_ok_to_send)."""
    conds: dict[str, Cond] = {}
    events: list[tuple[str, str]] = []  # (dedupe key, text)
    problem = False
    skip_stack = io.backup_running()  # the stack is stopped on purpose during a backup

    if not skip_stack:
        for name, up in component_checks(io, settings, probes).items():
            conds[f"down:{name}"] = Cond(not up, f"{name} is down.", f"{name} is back up.", DOWN_HOLD_S)
            problem = problem or not up

    low = decimal(settings.get("ALERT_BATTERY_LOW_V", ""))
    if low is not None and not skip_stack:
        volts = probes.battery_voltages()
        if volts is not None:
            for key, value in volts.items():
                short = key.removeprefix("electrical.batteries.").removesuffix(".voltage")
                conds[f"battery:{key}"] = Cond(value < low, f"Battery '{short}' is low: {value:.2f} V (alert threshold {low:g} V).",
                                               f"Battery '{short}' voltage recovered: {value:.2f} V.", BATTERY_HOLD_S)

    datapath = settings.get("SIGNALK_MODE", "existing") != "existing" and settings.get("SIGNALK_DATAPATH_AUTOCONFIG", "true") == "true" \
        and "influxdb" not in disabled(settings)
    stale_min = positive_int(settings.get("ALERT_STALE_DATA_MINUTES", "15"), 15)
    if datapath and not skip_stack:
        age = probes.newest_self_point_age(now, max(stale_min * 2, 30))
        if age is not None:
            conds["stale"] = Cond(age > stale_min * 60, f"No Signal K data has been logged for over {stale_min} minutes.", "Signal K data is flowing again.")

    for key, entity, bad, wording in (
        ("bilge", settings.get("ALERT_BILGE_ENTITY", ""), {"on"}, "Bilge alarm: {e} is on."),
        ("shore", settings.get("ALERT_SHORE_POWER_ENTITY", ""), {"off", "unavailable"}, "Shore power lost: {e} is {s}."),
    ):
        if not entity:
            continue
        current = probes.ha_state(entity)
        if current is None:
            continue
        conds[key] = Cond(current in bad, wording.format(e=entity, s=current), f"{entity} is back to normal ({current}).", ENTITY_HOLD_S)

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
    if isinstance(record, dict):
        if record.get("result") == "failed" and record.get("finished_at"):
            status = re.sub(r"[^0-9A-Za-z_-]", "", str(record.get("exit_status", "")))
            events.append((f"backup-failed:{record['finished_at']}", f"The scheduled backup failed (exit status {status or 'unknown'}). Run: sudo vesselstackctl doctor"))
        interval = {"daily": 86400, "weekly": 604800}.get(settings.get("VESSELSTACK_BACKUP_SCHEDULE", "off"))
        success = parse_iso(record.get("last_success_at"))
        if interval and success is not None:
            conds["backup-stale"] = Cond(now - success > 2 * interval, f"No successful scheduled backup since {record['last_success_at']}.", "Scheduled backups are succeeding again.")
    return conds, events, problem, not skip_stack


def run(settings: dict[str, str], io: Io, state_path: Path, log, out) -> int:
    scrub = Scrubber(settings)
    if settings.get("ALERTS_ENABLE", "false") != "true":
        return 0
    state = load_state(state_path)
    now = io.now()
    probes, telegram = Probes(io, settings), Telegram(io, settings, scrub, log)
    repeat = positive_int(settings.get("ALERT_REPEAT_MINUTES", "60"), 60) * 60
    conds, events, problem, check_heartbeat = evaluate(io, settings, probes, state, now)
    sends = failures = 0

    def deliver(text: str) -> bool:
        nonlocal sends, failures
        ok, error = telegram.send(text)
        sends += 1
        state["last_send"] = {"ok": ok, "at": iso(now), "error": "" if ok else error}
        if not ok:
            failures += 1
            log(f"alert send failed: {error}")
        return ok

    for key, cond in sorted(conds.items()):
        entry = state["alerts"].get(key)
        if cond.active:
            if entry is None:
                entry = state["alerts"][key] = {"first_seen": iso(now), "last_sent": "", "message": cond.text}
            entry["message"] = cond.text
            first = parse_iso(entry["first_seen"]) or now
            last = parse_iso(entry.get("last_sent"))
            if now - first >= cond.hold and (last is None or now - last >= repeat):
                if deliver(cond.text):
                    entry["last_sent"] = iso(now)
        elif entry is not None:
            if entry.get("last_sent"):
                if deliver(cond.clear):
                    del state["alerts"][key]
            else:
                del state["alerts"][key]

    done = state.setdefault("events", {})
    for key, text in events:
        if key in done:
            continue
        if deliver(text):
            done[key] = iso(now)
            if key.startswith("boot:"):
                state["boot_id"] = key.split(":", 1)[1]
    for key in list(done)[:-50]:
        del done[key]
    boot = io.boot_id()
    if boot and not any(k.startswith("boot:") for k, _ in events):
        state["boot_id"] = state.get("boot_id") or boot

    save_state(state_path, state)

    url = settings.get("ALERT_HEARTBEAT_URL", "").strip()
    if url.startswith("https://") and check_heartbeat:
        target = url.rstrip("/") + ("/fail" if problem else "")
        try:
            io.http("GET", target, {}, None, 10)
        except OSError as exc:
            log(scrub(f"heartbeat request failed ({exc})"))
    if failures:
        log(f"{failures} of {sends} alert message(s) could not be sent; will retry")
    return 0


def cmd_test(settings: dict[str, str], io: Io, state_path: Path, log, out) -> int:
    scrub = Scrubber(settings)
    ok, error = Telegram(io, settings, scrub, log).send("VesselStack test alert: Telegram alerts are working.")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(state_path.with_name("state.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state(state_path)
        state["last_send"] = {"ok": ok, "at": iso(io.now()), "error": "" if ok else error, "test": True}
        save_state(state_path, state)
    finally:
        lock.close()
    out(f"Test alert sent." if ok else f"Test alert FAILED: {error}")
    return 0 if ok else 1


def cmd_status(settings: dict[str, str], io: Io, state_path: Path, out) -> int:
    state = load_state(state_path) if state_path.exists() else empty_state()
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
    return 0


def cmd_doctor(settings: dict[str, str], state_path: Path, out) -> int:
    """Read-only. Prints WARN lines only; never fails."""
    if settings.get("ALERTS_ENABLE", "false") != "true":
        return 0
    if not chat_ids(settings):
        out("WARN alerts are enabled but ALERT_TELEGRAM_CHAT_IDS is empty. Fix: set it in the Control Panel settings or vesselstack.env and rerun the installer")
    if not settings.get("TELEGRAM_BOT_TOKEN", "").strip():
        out("WARN alerts are enabled but no Telegram bot token is set. Fix: add it in Boat Chat Settings")
    last = (load_state(state_path).get("last_send") or {}) if state_path.exists() else {}
    if last and not last.get("ok"):
        out(f"WARN the last alert message could not be sent ({last.get('error') or 'unknown error'}) at {last.get('at')}. Try: sudo vesselstackctl alerts test")
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

    try:
        if args.command == "status":
            return cmd_status(settings, io, state_path, out)
        if args.command == "doctor":
            return cmd_doctor(settings, state_path, out)
        if args.command == "test":
            return cmd_test(settings, io, state_path, log, out)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        lock = open(state_path.with_name("state.lock"), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return run(settings, io, state_path, log, out)
        finally:
            lock.close()
    except Exception as exc:  # never crash-loop the timer
        log(f"unexpected {type(exc).__name__}; will retry next run")
        return 0


if __name__ == "__main__":
    sys.exit(main())
