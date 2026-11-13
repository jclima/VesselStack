#!/usr/bin/env python3
"""Configure the Signal K -> InfluxDB data path for managed (docker/native) servers.

Three idempotent steps, each skipped when it is already done:
  1. install the pinned signalk-to-influxdb2 plugin into the Signal K data directory
     (skipped, with a warning, when npm would remove packages that package.json
     does not list, because that would delete the owner's other plugins);
  2. when SocketCAN is enabled, add a NMEA 2000 provider for the interface to
     settings.json (a backup is taken first; other entries are never touched);
  3. write the plugin's configuration with a new, narrowly scoped InfluxDB token
     (read this organization, read+write the raw bucket only, read DBRP mappings),
     but only when that file does not exist (the owner's file is never overwritten).
     The DBRP mapping the plugin needs is created first when it is missing.

Privilege model: this runs as root, but root only reads the environment file, calls the
InfluxDB API and starts npm through its existing unprivileged paths (docker --user, or a
child that drops to the Signal K account). Everything inside <data>/signalk, a directory the
Signal K account owns and can write, happens in a forked child that has first dropped to that
uid/gid (setgroups, setgid, setuid) and uses O_NOFOLLOW / dir_fd-relative operations. The
writer token reaches that child over a pipe, never argv or the environment. No path-based
chown or other symlink-following root operation touches that tree.

Optional marine essentials bundle (SIGNALK_MARINE_BUNDLE=true, README "Marine essentials"):
  4. install the pinned anchor alarm, logbook and autostate plugins with the same npm rules;
  5. write a plugin configuration for the NMEA 0183 converter (a few basic sentences), the
     anchor alarm, the logbook and autostate, each ONLY when its file does not exist;
  6. make sure Signal K's NMEA 0183 TCP server (interface "nmea-tcp", port 10110, on by default)
     is not switched off in settings.json: only that one key is touched, and only when it is
     explicitly false. KIP, Freeboard-SK and the converter ship inside the Signal K server.
With --bundle-only only steps 4 to 6 run (the Signal K -> InfluxDB steps are skipped).

Exit status: 0 nothing (more) to do, 10 something changed (or would change under
--check), 11 the plugin install was skipped by the npm prune guard (nothing was
changed), anything else is an error. The admin token is read from the environment
file and is never printed or placed on a command line.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PLUGIN = "signalk-to-influxdb2"
PLUGIN_VERSION = "2.3.0"
BUNDLE_PACKAGES = (
    ("signalk-anchoralarm-plugin", "2.0.1"),
    ("@meri-imperiumi/signalk-logbook", "0.15.1"),
    ("@meri-imperiumi/signalk-autostate", "0.6.4"),  # the logbook "requires" it for automatic entries
)
# Plugin ids (read from the packages) -> configuration. The NMEA 0183 converter ships with the
# Signal K server and is not enabled by default; the others are, but an explicit file is visible
# in the admin UI and lets 'vesselstackctl doctor' tell "configured" from "never set up".
NMEA0183_SENTENCES = ("RMC", "GGA", "GLL", "VTG", "ZDA", "DBT", "DPT", "MWVR", "HDM", "HDT", "MTW", "XDRBaro")
BUNDLE_CONFIGS = {
    "sk-to-nmea0183": {"enabled": True, "configuration": {name: True for name in NMEA0183_SENTENCES}},
    "anchoralarm": {"enabled": True, "configuration": {}},
    "signalk-logbook": {"enabled": True, "configuration": {}},
    "signalk-autostate": {"enabled": True, "configuration": {}},
}
TCP_INTERFACE = "nmea-tcp"
WRITER_DESCRIPTION = "vesselstack-signalk-writer"
IMAGE_REPOSITORY = "cr.signalk.io/signalk/signalk-server"
IMAGE_CONFIG_DIR = "/home/node/.signalk"
EXIT_CHANGED = 10
EXIT_SKIPPED = 11
BUCKET_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
IFACE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,14}$")
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
NPM_ARGS = ["install", "--save-exact", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund", "--no-package-lock"]
NPM_TIMEOUT = 600

CHILD_TIMEOUT = 120
CONFIG_DIR = "plugin-config-data"

# Seams for the tests.
fchown = os.fchown


def _drop_privileges(uid: int, gid: int) -> None:
    """Become uid:gid for good (supplementary groups first, then gid, then uid) and prove it."""
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    if (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) != (uid, uid, gid, gid):
        raise RuntimeError("privileges were not dropped")
    if uid != 0:
        try:
            os.setuid(0)
        except PermissionError:
            return
        raise RuntimeError("privileges can be regained")


drop_privileges = _drop_privileges
_phase = ["starting"]


class StepFailed(Exception):
    def __init__(self, step: str):
        super().__init__(step)
        self.step = step


class SettingsUnusable(Exception):
    pass


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def fail(step: str) -> "NoReturn":
    # Only the failing step is named: nothing here may carry a secret.
    raise StepFailed(step)


def read_env(path: Path) -> dict[str, str]:
    try:
        text = path.read_text()
    except OSError:
        fail("reading the environment file")
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line.strip())
        if not match:
            continue
        value = match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[match.group(1)] = value
    return values


# --- File access relative to an open directory (the Signal K data directory) ----

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def open_dir(name, dir_fd=None) -> int:
    return os.open(name, DIR_FLAGS, dir_fd=dir_fd)


def read_file_at(dir_fd: int, parts: list[str], limit: int = 1 << 24) -> bytes:
    """Read a regular file below dir_fd, refusing symbolic links at every component."""
    fds = []
    try:
        current = dir_fd
        for part in parts[:-1]:
            current = open_dir(part, current)
            fds.append(current)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=current)
        fds.append(fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        return os.read(fd, limit)
    finally:
        for fd in fds:
            os.close(fd)


def write_atomic_at(dir_fd: int, name: str, data: bytes, mode: int, noreplace: bool = False) -> None:
    """Create name below dir_fd (owned by whoever runs this) via a temporary file."""
    tmp = f".{name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=dir_fd)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        if noreplace:
            os.link(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)  # fails when it appeared meanwhile
        else:
            os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    finally:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
    os.fsync(dir_fd)


def write_atomic_path(path: Path, data: bytes, mode: int) -> None:
    """For root-controlled folders only (the doctor status file)."""
    dir_fd = open_dir(str(path.parent))
    try:
        write_atomic_at(dir_fd, path.name, data, mode)
    finally:
        os.close(dir_fd)


def as_service_user(args, work, payload: dict) -> dict:
    """Run work(args, payload, dir_fd) in a forked child that has dropped to the Signal K uid/gid.

    dir_fd is <data>/signalk, opened here (as root, without following a link in the last
    component) because the data folder above it is typically not traversable by the Signal K
    account; the child then works only relative to that descriptor.

    The payload (which may hold the writer token) goes in over a pipe; the result comes
    back as JSON over another. StepFailed in the child becomes StepFailed here.
    """
    try:
        dir_fd = open_dir(str(args.data / "signalk"))
    except OSError:
        fail("locating the Signal K data directory")
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child: never returns into the caller's code
        status = 70
        try:
            os.close(in_w)
            os.close(out_r)
            drop_privileges(args.uid, args.gid)
            with os.fdopen(in_r, "rb") as source:
                request = json.loads(source.read() or b"{}")
            try:
                result = {"ok": work(args, request, dir_fd)}
            except StepFailed as exc:
                result = {"fail": exc.step}
            with os.fdopen(out_w, "wb") as sink:
                sink.write(json.dumps(result).encode())
            status = 0
        except BaseException:
            pass
        finally:
            os._exit(status)
    os.close(in_r)
    os.close(out_w)
    chunks = []
    try:
        with os.fdopen(in_w, "wb") as sink:
            sink.write(json.dumps(payload).encode())
        deadline = time.monotonic() + CHILD_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([out_r], [], [], remaining)[0]:
                os.kill(pid, signal.SIGKILL)
                break
            chunk = os.read(out_r, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(out_r)
        os.close(dir_fd)
        os.waitpid(pid, 0)
    try:
        result = json.loads(b"".join(chunks))
    except ValueError:
        fail(_phase[0] + " (the unprivileged helper did not finish)")
    if "fail" in result:
        fail(result["fail"])
    return result["ok"]


# --- Step 1: the plugin --------------------------------------------------------

def version_tuple(text: str):
    parts = re.match(r"^(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(p) for p in parts.groups()) if parts else None


def plugin_needed(dir_fd: int, name: str = PLUGIN, pinned: str = PLUGIN_VERSION) -> bool:
    try:
        raw = read_file_at(dir_fd, ["node_modules", *name.split("/"), "package.json"])
    except FileNotFoundError:
        return True
    except OSError:
        return False  # a link or odd file: leave the owner's copy alone
    try:
        installed = version_tuple(str(json.loads(raw).get("version", "")))
    except (ValueError, AttributeError):
        return False  # unreadable: leave the owner's copy alone
    if installed is None:
        return False
    return installed < version_tuple(pinned)


def npm_command(args, env: dict[str, str], signalk: Path, extra: list[str], cache: str | None, name: str | None = None,
                specs: list[str] | None = None):
    """Return (argv, subprocess kwargs) for npm in the Signal K data directory."""
    specs = specs or [f"{PLUGIN}@{PLUGIN_VERSION}"]
    if args.mode == "docker":
        tag = env.get("SIGNALK_VERSION", "")
        if not TAG_RE.fullmatch(tag):
            fail("reading SIGNALK_VERSION")
        if ":" in str(signalk) or "," in str(signalk):
            fail("validating the Signal K data directory (unsupported characters)")
        cmd = ["docker", "run", "--rm", "--name", name or f"vesselstack-npm-{os.getpid()}", "--pull", "never", "--cap-drop", "ALL", "--user", f"{args.uid}:{args.gid}",
               "-e", "HOME=/tmp", "-e", "npm_config_cache=/tmp/.npm",
               "-v", f"{signalk}:{IMAGE_CONFIG_DIR}", "-w", IMAGE_CONFIG_DIR, "--entrypoint", "npm",
               f"{IMAGE_REPOSITORY}:{tag}", *NPM_ARGS, *extra, *specs]
        return cmd, {}
    cmd = ["npm", *NPM_ARGS, *extra, "--prefix", str(signalk), "--cache", cache, *specs]
    environment = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": cache}
    return cmd, {"cwd": signalk, "env": environment, "user": args.uid, "group": args.gid, "extra_groups": []}


def run_npm(cmd: list[str], container: str | None = None, **kwargs) -> str:
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=NPM_TIMEOUT, **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        if container:
            # Killing the docker client leaves the container running; remove it.
            try:
                subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=60)
            except (OSError, subprocess.SubprocessError):
                pass
        fail("installing the plugin")
    output = result.stdout.decode("utf-8", "replace")
    if result.returncode != 0:
        for line in output.strip().splitlines()[-5:]:
            print(f"  npm: {line}", file=sys.stderr)
        fail("installing the plugin")
    return output


def npm_runner(args, env: dict[str, str], signalk: Path, extra: list[str], specs: list[str] | None = None) -> str:
    if args.mode == "docker":
        name = f"vesselstack-npm-{os.getpid()}"
        cmd, kwargs = npm_command(args, env, signalk, extra, None, name, specs)
        return run_npm(cmd, container=name, **kwargs)
    with tempfile.TemporaryDirectory(prefix="vesselstack-npm-") as cache:
        cache_fd = open_dir(cache)  # created by us, 0700: hand it to the service account by descriptor
        try:
            fchown(cache_fd, args.uid, args.gid)
            os.fchmod(cache_fd, 0o700)
        finally:
            os.close(cache_fd)
        cmd, kwargs = npm_command(args, env, signalk, extra, cache, specs=specs)
        return run_npm(cmd, **kwargs)


def removals_from(output: str):
    """Names npm says it would remove, or None when its report cannot be understood.

    `npm install --dry-run --json` prints human "add/remove" lines first and the JSON
    report after them; the report starts at the first line that begins with a brace.
    """
    names: set[str] = set()
    for line in output.splitlines():
        match = re.fullmatch(r"remove (\S+) \S+", line.strip())
        if match:
            names.add(match.group(1))
    brace = re.search(r"^\{", output, re.MULTILINE)
    if not brace:
        return None
    try:
        report = json.JSONDecoder().raw_decode(output[brace.start():])[0]
    except ValueError:
        return None
    if not isinstance(report, dict):
        return None
    for entry in report.get("remove") or []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            names.add(entry["name"])
    removed = report.get("removed")
    if isinstance(removed, int) and removed > len(names):
        names.add("(unnamed packages)")
    return sorted(names)


def prune_guard(args, env: dict[str, str], signalk: Path, specs: list[str] | None = None) -> list[str]:
    """Packages a real install would remove; an empty list means it is safe to install."""
    removals = removals_from(npm_runner(args, env, signalk, ["--dry-run", "--json"], specs))
    return ["(the npm report could not be read)"] if removals is None else removals


def record_skip(args, packages: list[str], names: str = PLUGIN) -> None:
    print(f"Signal K data path: WARN: skipped installing {names}: npm would remove {', '.join(packages)} "
          f"from {args.data / 'signalk' / 'node_modules'} because package.json does not list them.", file=sys.stderr)
    print("Signal K data path: WARN: add those packages to the dependencies in that package.json (or delete "
          "them if unused), then rerun the installer.", file=sys.stderr)
    if args.status_file:
        stamp = now().strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            write_atomic_path(args.status_file, f"{stamp} {','.join(packages)}\n".encode(), 0o644)
        except OSError:
            pass


def clear_skip(args) -> None:
    if args.status_file:
        try:
            args.status_file.unlink()
        except OSError:
            pass


# --- Step 2: the NMEA 2000 provider -------------------------------------------

def provider_for(interface: str) -> dict:
    return {
        "id": f"vesselstack-n2k-{interface}",
        "enabled": True,
        "pipeElements": [{
            "type": "providers/simple",
            "options": {
                "logging": False,
                "type": "NMEA2000",
                "subOptions": {"type": "canbus-canboatjs", "interface": interface},
            },
        }],
    }


def uses_interface(provider, interface: str) -> bool:
    if not isinstance(provider, dict):
        return False
    if provider.get("id") == f"vesselstack-n2k-{interface}":
        return True
    for element in provider.get("pipeElements") or []:
        options = element.get("options") if isinstance(element, dict) else None
        sub = options.get("subOptions") if isinstance(options, dict) else None
        if isinstance(sub, dict) and sub.get("interface") == interface:
            return True
    return False


def read_settings(dir_fd: int):
    """Return (document, raw bytes, mode), or None when there is no settings.json.

    One read through one descriptor: the bytes parsed are the bytes backed up and the mode
    is that of the same open file. Raises SettingsUnusable for anything that must not be replaced.
    """
    try:
        fd = os.open("settings.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise SettingsUnusable("a link or unreadable file")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SettingsUnusable("not a regular file")
        raw = os.read(fd, 1 << 24)
    finally:
        os.close(fd)
    try:
        document = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise SettingsUnusable("not valid JSON")
    if not isinstance(document, dict) or not isinstance(document.get("pipedProviders", []), list):
        raise SettingsUnusable("unexpected structure")
    return document, raw, stat.S_IMODE(info.st_mode)


def provider_state(dir_fd: int, interface: str) -> str:
    """needed | present | unusable"""
    try:
        loaded = read_settings(dir_fd)
    except SettingsUnusable:
        return "unusable"
    providers = loaded[0].get("pipedProviders", []) if loaded else []
    return "present" if any(uses_interface(p, interface) for p in providers) else "needed"


def add_provider(args, dir_fd: int, interface: str):
    """Returns True when added, False when not needed, None when settings.json is unusable."""
    try:
        loaded = read_settings(dir_fd)
    except SettingsUnusable:
        return None
    document, raw, mode = loaded if loaded else ({}, None, 0o644)
    providers = document.setdefault("pipedProviders", [])
    if any(uses_interface(p, interface) for p in providers):
        return False
    providers.append(provider_for(interface))
    backup_and_write_settings(dir_fd, document, raw, mode)
    return True


def backup_and_write_settings(dir_fd: int, document: dict, raw, mode: int) -> None:
    if raw is not None:
        stamp = now().strftime("%Y%m%dT%H%M%SZ")
        try:
            write_atomic_at(dir_fd, f"settings.json.vesselstack-{stamp}.bak", raw, 0o600, noreplace=True)
        except OSError:
            fail("backing up settings.json")
    try:
        write_atomic_at(dir_fd, "settings.json", (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8"), mode)
    except OSError:
        fail("writing settings.json")


def tcp_state(dir_fd: int) -> str:
    """ok (on by default or on) | needed (explicitly off) | unusable"""
    try:
        loaded = read_settings(dir_fd)
    except SettingsUnusable:
        return "unusable"
    interfaces = loaded[0].get("interfaces") if loaded else None
    if interfaces is None:
        return "ok"
    if not isinstance(interfaces, dict):
        return "unusable"
    return "needed" if interfaces.get(TCP_INTERFACE) is False else "ok"


def enable_tcp(args, dir_fd: int):
    """Set interfaces["nmea-tcp"] to true only when it is explicitly false; True when changed, None when unusable."""
    state = tcp_state(dir_fd)
    if state != "needed":
        return None if state == "unusable" else False
    document, raw, mode = read_settings(dir_fd)
    document["interfaces"][TCP_INTERFACE] = True
    backup_and_write_settings(dir_fd, document, raw, mode)
    return True


# --- Step 3: the plugin configuration -----------------------------------------

def influx_base(env: dict[str, str]) -> str:
    port = env.get("INFLUXDB_PORT", "8086")
    if not re.fullmatch(r"[0-9]{1,5}", port):
        fail("reading INFLUXDB_PORT")
    bind = env.get("VESSELSTACK_DATA_BIND_ADDRESS", "127.0.0.1")
    host = "127.0.0.1" if bind in ("", "0.0.0.0") else bind
    if not re.fullmatch(r"[0-9.]{7,15}", host):
        host = "127.0.0.1"
    return f"http://{host}:{port}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Every request here carries an InfluxDB token: never follow it to another place."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError(f"redirect refused ({code})")


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def http_json(method: str, url: str, token: str, body=None, timeout: float = 15):
    headers = {"Authorization": f"Token {token}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with _OPENER.open(request, timeout=timeout) as response:
        raw = response.read(1 << 20)
    return json.loads(raw) if raw else {}


def writer_permissions(org_id: str, bucket_id: str) -> list[dict]:
    """Exactly what the plugin needs and nothing more (see the README for the reasoning)."""
    bucket = {"type": "buckets", "id": bucket_id, "orgID": org_id}
    return [
        {"action": "read", "resource": {"type": "orgs", "id": org_id}},
        {"action": "read", "resource": bucket},
        {"action": "write", "resource": bucket},
        {"action": "read", "resource": {"type": "dbrp", "orgID": org_id}},
    ]


def ensure_dbrp(base: str, admin: str, org_id: str, bucket_id: str, bucket: str) -> None:
    """The plugin reads (and, when absent, creates) a v1 database/retention-policy mapping."""
    q = urllib.parse.quote
    try:
        listing = http_json("GET", f"{base}/api/v2/dbrps?orgID={q(org_id)}&bucketID={q(bucket_id)}", admin)
    except (urllib.error.URLError, OSError, ValueError):
        fail("reading the DBRP mappings")
    if listing.get("content"):
        return
    try:
        http_json("POST", f"{base}/api/v2/dbrps", admin, {
            "orgID": org_id, "bucketID": bucket_id, "database": bucket,
            "retention_policy": "default", "default": True,
        })
    except (urllib.error.URLError, OSError, ValueError):
        fail("creating the DBRP mapping")


def create_writer_token(env: dict[str, str]):
    """Returns (token, authorization id)."""
    base = influx_base(env)
    admin, org, bucket = env.get("INFLUXDB_TOKEN", ""), env.get("INFLUXDB_ORG", ""), env.get("INFLUXDB_RAW_BUCKET", "")
    if not admin or not org:
        fail("reading the InfluxDB settings")
    if not BUCKET_RE.fullmatch(bucket):
        fail("validating INFLUXDB_RAW_BUCKET")
    q = urllib.parse.quote
    try:
        found = http_json("GET", f"{base}/api/v2/buckets?name={q(bucket)}&org={q(org)}", admin).get("buckets", [])
    except (urllib.error.URLError, OSError, ValueError):
        fail("looking up the raw bucket")
    match = next((b for b in found if b.get("name") == bucket), None)
    if not match or not match.get("id") or not match.get("orgID"):
        fail("looking up the raw bucket (not found)")
    org_id, bucket_id = match["orgID"], match["id"]
    ensure_dbrp(base, admin, org_id, bucket_id, bucket)
    try:
        listing = http_json("GET", f"{base}/api/v2/authorizations?orgID={q(org_id)}", admin).get("authorizations", [])
        for auth in listing:
            if auth.get("description") == WRITER_DESCRIPTION and auth.get("id"):
                http_json("DELETE", f"{base}/api/v2/authorizations/{q(auth['id'])}", admin)
    except (urllib.error.URLError, OSError, ValueError):
        fail("removing the previous writer token")
    try:
        created = http_json("POST", f"{base}/api/v2/authorizations", admin, {
            "orgID": org_id,
            "description": WRITER_DESCRIPTION,
            "permissions": writer_permissions(org_id, bucket_id),
        })
    except (urllib.error.URLError, OSError, ValueError):
        fail("creating the writer token")
    token = created.get("token") if isinstance(created, dict) else None
    if not isinstance(token, str) or not token:
        fail("creating the writer token (no token returned)")
    return token, created.get("id")


def revoke_writer_token(env: dict[str, str], auth_id) -> None:
    """Best effort: a token whose configuration could not be written helps nobody."""
    if not auth_id:
        return
    try:
        http_json("DELETE", f"{influx_base(env)}/api/v2/authorizations/{urllib.parse.quote(str(auth_id))}",
                  env.get("INFLUXDB_TOKEN", ""))
    except (urllib.error.URLError, OSError, ValueError, StepFailed):
        pass


def plugin_config(env: dict[str, str], token: str) -> dict:
    return {
        "enabled": True,
        "configuration": {
            "influxes": [{
                "url": influx_base(env),
                "token": token,
                "org": env["INFLUXDB_ORG"],
                "bucket": env["INFLUXDB_RAW_BUCKET"],
                "onlySelf": True,
                "resolution": 1000,
                "writeOptions": {"batchSize": 1000, "flushInterval": 10000, "maxRetries": 3},
            }],
        },
    }


def inspect_work(args, request: dict, dir_fd: int) -> dict:
    """Unprivileged: what is there and what is missing. Changes nothing."""
    datapath = request.get("datapath", True)
    state = {"need_plugin": datapath and plugin_needed(dir_fd), "provider": "none", "need_config": False,
             "bundle_packages": [], "bundle_configs": [], "tcp": "ok"}
    if request.get("interface"):
        state["provider"] = provider_state(dir_fd, request["interface"])
    if request.get("bundle"):
        state["bundle_packages"] = [f"{n}@{v}" for n, v in BUNDLE_PACKAGES if plugin_needed(dir_fd, n, v)]
        state["tcp"] = tcp_state(dir_fd)
        for plugin_id in BUNDLE_CONFIGS:
            try:
                os.lstat(f"{CONFIG_DIR}/{plugin_id}.json", dir_fd=dir_fd)
            except FileNotFoundError:
                state["bundle_configs"].append(plugin_id)
            except OSError:
                fail("checking the plugin configuration")
    if datapath:
        try:
            os.lstat(f"{CONFIG_DIR}/{PLUGIN}.json", dir_fd=dir_fd)
        except FileNotFoundError:
            state["need_config"] = True
        except OSError:
            fail("checking the plugin configuration")
    if state["need_config"] or state["bundle_configs"]:
        # Everything a refusal would be based on, before a token is created.
        try:
            info = os.lstat(CONFIG_DIR, dir_fd=dir_fd)
        except FileNotFoundError:
            if not os.access(".", os.W_OK | os.X_OK, dir_fd=dir_fd):
                fail("writing the plugin configuration (the directory is not writable)")
        except OSError:
            fail("writing the plugin configuration")
        else:
            if not stat.S_ISDIR(info.st_mode):
                fail("writing the plugin configuration (refusing a symbolic link or file)")
    return state


def apply_work(args, request: dict, dir_fd: int) -> dict:
    """Unprivileged: the plugin configuration (with the token from the pipe) and the provider."""
    result = {"config_written": False, "provider": "none", "bundle_written": [], "tcp": "ok"}
    configs = {}
    if request.get("config") is not None:
        configs[PLUGIN] = request["config"]
    for plugin_id in request.get("bundle_configs") or []:
        configs[plugin_id] = BUNDLE_CONFIGS[plugin_id]
    if configs:
        _phase[0] = "writing the plugin configuration"
        created = False
        try:
            os.mkdir(CONFIG_DIR, 0o755, dir_fd=dir_fd)
            created = True
        except FileExistsError:
            pass
        except OSError:
            fail("writing the plugin configuration")
        try:
            config_fd = open_dir(CONFIG_DIR, dir_fd)
        except OSError:
            fail("writing the plugin configuration (refusing a symbolic link or file)")
        try:
            if created:
                os.fchmod(config_fd, 0o755)
                os.fsync(dir_fd)
            for plugin_id, config in configs.items():
                data = (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
                try:
                    write_atomic_at(config_fd, f"{plugin_id}.json", data, 0o600, noreplace=True)
                except FileExistsError:
                    continue
                except OSError:
                    fail("writing the plugin configuration")
                if plugin_id == PLUGIN:
                    result["config_written"] = True
                else:
                    result["bundle_written"].append(plugin_id)
        finally:
            os.close(config_fd)
    if request.get("tcp"):
        _phase[0] = "enabling the NMEA 0183 TCP server"
        enabled = enable_tcp(args, dir_fd)
        result["tcp"] = "unusable" if enabled is None else ("enabled" if enabled else "ok")
    if request.get("interface"):
        _phase[0] = "adding the NMEA 2000 provider"
        added = add_provider(args, dir_fd, request["interface"])
        result["provider"] = "unusable" if added is None else ("added" if added else "present")
    return result


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("existing", "docker", "native"), required=True)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--uid", required=True, type=int)
    parser.add_argument("--gid", required=True, type=int)
    parser.add_argument("--status-file", type=Path, help="records a skipped plugin install for 'vesselstackctl doctor'")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--bundle-only", action="store_true", help="only the marine essentials bundle (no InfluxDB steps)")
    args = parser.parse_args(argv)
    if args.mode == "existing":
        return 0
    if args.uid <= 0 or args.gid <= 0:
        fail("validating the service uid/gid")
    _phase[0] = "reading the environment file"
    env = read_env(args.env_file)
    signalk = args.data / "signalk"
    bundle = env.get("SIGNALK_MARINE_BUNDLE") == "true"
    datapath = not args.bundle_only
    if not datapath:
        args.status_file = None  # the doctor's skip record belongs to the data path
        if not bundle:
            return 0
    interface = None
    if datapath and env.get("SOCKETCAN_ENABLE") == "true":
        interface = env.get("SOCKETCAN_INTERFACE", "")
        if not IFACE_RE.fullmatch(interface):
            fail("reading SOCKETCAN_INTERFACE")

    # Unprivileged look at what is there; every refusal that can be known happens here,
    # before an InfluxDB token exists.
    _phase[0] = "inspecting the Signal K data directory"
    state = as_service_user(args, inspect_work, {"interface": interface, "datapath": datapath, "bundle": bundle})
    need_plugin = state["need_plugin"]
    need_provider = state["provider"] == "needed"
    need_config = state["need_config"]
    bundle_configs = state["bundle_configs"]
    need_tcp = state["tcp"] == "needed"
    specs = ([f"{PLUGIN}@{PLUGIN_VERSION}"] if need_plugin else []) + state["bundle_packages"]
    if specs:
        # Checked before anything is changed, also under --check, so the installer
        # does not stop Signal K for a run that would only be skipped.
        _phase[0] = "checking what npm would remove"
        skipped = prune_guard(args, env, signalk, specs)
        if skipped:
            record_skip(args, skipped, ", ".join(spec.rsplit("@", 1)[0] for spec in specs))
            return EXIT_SKIPPED
    if args.check:
        return EXIT_CHANGED if (specs or need_provider or need_config or bundle_configs or need_tcp) else 0

    changed = False
    if specs:
        _phase[0] = "installing the plugin"
        npm_runner(args, env, signalk, [], specs)
        print(f"Signal K data path: installed {', '.join(specs)}")
        changed = True
    clear_skip(args)
    token = auth_id = None
    if need_config:
        _phase[0] = "creating the InfluxDB writer token"
        token, auth_id = create_writer_token(env)
    request = {"interface": interface if need_provider else None,
               "config": plugin_config(env, token) if token else None,
               "bundle_configs": bundle_configs, "tcp": need_tcp}
    result = {"config_written": False, "provider": "none", "bundle_written": [], "tcp": "ok"}
    if token or need_provider or bundle_configs or need_tcp:
        _phase[0] = "writing the Signal K configuration"
        try:
            result = as_service_user(args, apply_work, request)
        except StepFailed:
            revoke_writer_token(env, auth_id)
            raise
        if token and not result["config_written"]:
            revoke_writer_token(env, auth_id)  # the file appeared meanwhile: this token is unused
        if result["config_written"]:
            print(f"Signal K data path: wrote the {PLUGIN} configuration (scoped token for bucket {env['INFLUXDB_RAW_BUCKET']})")
            changed = True
        for plugin_id in result["bundle_written"]:
            print(f"Signal K data path: wrote the {plugin_id} plugin configuration (marine essentials)")
            changed = True
        if result["tcp"] == "enabled":
            print(f"Signal K data path: switched the NMEA 0183 TCP server ({TCP_INTERFACE}) on in settings.json")
            changed = True
        if result["provider"] == "added":
            print(f"Signal K data path: added NMEA 2000 provider vesselstack-n2k-{interface}")
            changed = True
    if state["tcp"] == "unusable" or result["tcp"] == "unusable":
        print("Signal K data path: WARN: settings.json is not valid JSON (or is a link or unexpected structure), "
              "so the NMEA 0183 TCP server setting was not checked and the file was left unchanged.", file=sys.stderr)
    if state["provider"] == "unusable" or (need_provider and result["provider"] == "unusable"):
        print("Signal K data path: WARN: settings.json is not valid JSON (or is a link or unexpected structure), "
              f"so the NMEA 2000 provider for {interface} was not added and the file was left unchanged. "
              "Fix it or add the provider under Signal K > Server > Connections.", file=sys.stderr)
    return EXIT_CHANGED if changed else 0


def main(argv=None) -> int:
    try:
        return run(argv)
    except StepFailed as exc:
        print(f"Signal K data path: {exc.step} failed", file=sys.stderr)
    except Exception:
        # Only the step: no exception text, which could carry a path or a value.
        print(f"Signal K data path: {_phase[0]} failed (unexpected error)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
