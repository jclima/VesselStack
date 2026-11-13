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
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PLUGIN = "signalk-to-influxdb2"
PLUGIN_VERSION = "2.3.0"
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

# Seams for the tests.
chown = os.chown


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def fail(step: str) -> "NoReturn":
    # Only the failing step is named: nothing here may carry a secret.
    print(f"Signal K data path: {step} failed", file=sys.stderr)
    raise SystemExit(1)


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


def refuse_symlink(path: Path, step: str) -> None:
    if path.is_symlink():
        fail(f"{step} (refusing a symbolic link)")


# --- Step 1: the plugin --------------------------------------------------------

def version_tuple(text: str):
    parts = re.match(r"^(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(p) for p in parts.groups()) if parts else None


def plugin_needed(signalk: Path) -> bool:
    manifest = signalk / "node_modules" / PLUGIN / "package.json"
    if not manifest.is_file():
        return True
    try:
        installed = version_tuple(str(json.loads(manifest.read_text()).get("version", "")))
    except (OSError, ValueError):
        return False  # unreadable: leave the owner's copy alone
    if installed is None:
        return False
    return installed < version_tuple(PLUGIN_VERSION)


def npm_command(args, env: dict[str, str], signalk: Path, extra: list[str], cache: str | None):
    """Return (argv, subprocess kwargs) for npm in the Signal K data directory."""
    spec = f"{PLUGIN}@{PLUGIN_VERSION}"
    if args.mode == "docker":
        tag = env.get("SIGNALK_VERSION", "")
        if not TAG_RE.fullmatch(tag):
            fail("reading SIGNALK_VERSION")
        if ":" in str(signalk) or "," in str(signalk):
            fail("validating the Signal K data directory (unsupported characters)")
        cmd = ["docker", "run", "--rm", "--pull", "never", "--cap-drop", "ALL", "--user", f"{args.uid}:{args.gid}",
               "-e", "HOME=/tmp", "-e", "npm_config_cache=/tmp/.npm",
               "-v", f"{signalk}:{IMAGE_CONFIG_DIR}", "-w", IMAGE_CONFIG_DIR, "--entrypoint", "npm",
               f"{IMAGE_REPOSITORY}:{tag}", *NPM_ARGS, *extra, spec]
        return cmd, {}
    cmd = ["npm", *NPM_ARGS, *extra, "--prefix", str(signalk), "--cache", cache, spec]
    environment = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": cache}
    return cmd, {"cwd": signalk, "env": environment, "user": args.uid, "group": args.gid, "extra_groups": []}


def run_npm(cmd: list[str], **kwargs) -> str:
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=NPM_TIMEOUT, **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        fail("installing the plugin")
    output = result.stdout.decode("utf-8", "replace")
    if result.returncode != 0:
        for line in output.strip().splitlines()[-5:]:
            print(f"  npm: {line}", file=sys.stderr)
        fail("installing the plugin")
    return output


def npm_runner(args, env: dict[str, str], signalk: Path, extra: list[str]) -> str:
    if args.mode == "docker":
        cmd, kwargs = npm_command(args, env, signalk, extra, None)
        return run_npm(cmd, **kwargs)
    with tempfile.TemporaryDirectory(prefix="vesselstack-npm-") as cache:
        chown(cache, args.uid, args.gid)
        os.chmod(cache, 0o700)
        cmd, kwargs = npm_command(args, env, signalk, extra, cache)
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


def prune_guard(args, env: dict[str, str], signalk: Path) -> list[str]:
    """Packages a real install would remove; an empty list means it is safe to install."""
    removals = removals_from(npm_runner(args, env, signalk, ["--dry-run", "--json"]))
    return ["(the npm report could not be read)"] if removals is None else removals


def record_skip(args, packages: list[str]) -> None:
    print(f"Signal K data path: WARN: skipped installing {PLUGIN}: npm would remove {', '.join(packages)} "
          f"from {args.data / 'signalk' / 'node_modules'} because package.json does not list them.", file=sys.stderr)
    print("Signal K data path: WARN: add those packages to the dependencies in that package.json (or delete "
          "them if unused), then rerun the installer.", file=sys.stderr)
    if args.status_file:
        stamp = now().strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            write_atomic(args.status_file, f"{stamp} {','.join(packages)}\n".encode(), 0o644, 0, 0)
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


def load_settings(path: Path):
    """Return (document, existed). A document that cannot be parsed is never replaced."""
    if not path.exists():
        return {}, False
    refuse_symlink(path, "reading settings.json")
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        fail("reading settings.json (it is not valid JSON; leaving it unchanged)")
    if not isinstance(document, dict) or not isinstance(document.get("pipedProviders", []), list):
        fail("reading settings.json (unexpected structure; leaving it unchanged)")
    return document, True


def provider_needed(signalk: Path, env: dict[str, str]) -> bool:
    if env.get("SOCKETCAN_ENABLE") != "true":
        return False
    interface = env.get("SOCKETCAN_INTERFACE", "")
    if not IFACE_RE.fullmatch(interface):
        fail("reading SOCKETCAN_INTERFACE")
    document, _ = load_settings(signalk / "settings.json")
    return not any(uses_interface(p, interface) for p in document.get("pipedProviders", []))


def write_atomic(path: Path, data: bytes, mode: int, uid: int, gid: int, noreplace: bool = False) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        try:
            os.write(fd, data)
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        chown(tmp, uid, gid)
        if noreplace:
            os.link(tmp, path)  # fails when the file appeared meanwhile
        else:
            os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def add_provider(args, env: dict[str, str], signalk: Path) -> bool:
    interface = env["SOCKETCAN_INTERFACE"]
    path = signalk / "settings.json"
    document, existed = load_settings(path)
    providers = document.setdefault("pipedProviders", [])
    if any(uses_interface(p, interface) for p in providers):
        return False
    mode = 0o644
    if existed:
        mode = path.stat().st_mode & 0o777
        stamp = now().strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"settings.json.vesselstack-{stamp}.bak")
        try:
            write_atomic(backup, path.read_bytes(), 0o600, args.uid, args.gid, noreplace=True)
        except OSError:
            fail("backing up settings.json")
    providers.append(provider_for(interface))
    try:
        write_atomic(path, (json.dumps(document, indent=2) + "\n").encode(), mode, args.uid, args.gid)
    except OSError:
        fail("writing settings.json")
    print(f"Signal K data path: added NMEA 2000 provider vesselstack-n2k-{interface}")
    return True


# --- Step 3: the plugin configuration -----------------------------------------

def config_path(signalk: Path) -> Path:
    return signalk / "plugin-config-data" / f"{PLUGIN}.json"


def influx_base(env: dict[str, str]) -> str:
    port = env.get("INFLUXDB_PORT", "8086")
    if not re.fullmatch(r"[0-9]{1,5}", port):
        fail("reading INFLUXDB_PORT")
    bind = env.get("VESSELSTACK_DATA_BIND_ADDRESS", "127.0.0.1")
    host = "127.0.0.1" if bind in ("", "0.0.0.0") else bind
    if not re.fullmatch(r"[0-9.]{7,15}", host):
        host = "127.0.0.1"
    return f"http://{host}:{port}"


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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


def create_writer_token(env: dict[str, str]) -> str:
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
    return token


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


def write_plugin_config(args, env: dict[str, str], signalk: Path) -> bool:
    path = config_path(signalk)
    if path.exists() or path.is_symlink():
        return False
    token = create_writer_token(env)
    directory = path.parent
    refuse_symlink(directory, "writing the plugin configuration")
    try:
        if not directory.exists():
            directory.mkdir(mode=0o755)
            chown(directory, args.uid, args.gid)
        data = (json.dumps(plugin_config(env, token), indent=2) + "\n").encode()
        write_atomic(path, data, 0o600, args.uid, args.gid, noreplace=True)
    except FileExistsError:
        return False
    except OSError:
        fail("writing the plugin configuration")
    print(f"Signal K data path: wrote the {PLUGIN} configuration (scoped token for bucket {env['INFLUXDB_RAW_BUCKET']})")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("existing", "docker", "native"), required=True)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--uid", required=True, type=int)
    parser.add_argument("--gid", required=True, type=int)
    parser.add_argument("--status-file", type=Path, help="records a skipped plugin install for 'vesselstackctl doctor'")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.mode == "existing":
        return 0
    if args.uid <= 0 or args.gid <= 0:
        fail("validating the service uid/gid")
    env = read_env(args.env_file)
    signalk = args.data / "signalk"
    if not signalk.is_dir() or signalk.is_symlink():
        fail("locating the Signal K data directory")

    need_plugin = plugin_needed(signalk)
    need_provider = provider_needed(signalk, env)
    need_config = not (config_path(signalk).exists() or config_path(signalk).is_symlink())
    if need_plugin:
        # Checked before anything is changed, also under --check, so the installer
        # does not stop Signal K for a run that would only be skipped.
        skipped = prune_guard(args, env, signalk)
        if skipped:
            record_skip(args, skipped)
            return EXIT_SKIPPED
    if args.check:
        return EXIT_CHANGED if (need_plugin or need_provider or need_config) else 0

    changed = False
    if need_plugin:
        npm_runner(args, env, signalk, [])
        print(f"Signal K data path: installed {PLUGIN}@{PLUGIN_VERSION}")
        changed = True
    clear_skip(args)
    if need_provider:
        changed = add_provider(args, env, signalk) or changed
    if need_config:
        changed = write_plugin_config(args, env, signalk) or changed
    return EXIT_CHANGED if changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
