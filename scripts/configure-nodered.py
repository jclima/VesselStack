#!/usr/bin/env python3
"""Prepare the optional Node-RED component (README "Node-RED (optional)").

Subcommands (all run as root by install.sh; none prints a secret):
  hash      read "PASSPHRASE\\n[EXISTING_HASH]" on stdin, print a bcrypt hash. An existing hash
            that still matches the passphrase is returned unchanged. bcrypt runs inside the
            pinned Node-RED image (bcryptjs), so the passphrase never reaches a command line.
  render    NODERED_ENABLE=true: create <data>/node-red (owner 1000:1000), seed settings.js
            only when it does not exist (the owner's edits are never overwritten) and write
            <root>/config/nodered.env (root:root 0600) with the pre-wired MQTT, InfluxDB,
            Signal K and Home Assistant values. NODERED_ENABLE=false: remove nodered.env.
  token     create (or keep) the read-only InfluxDB token scoped to the raw and history
            buckets and store it in nodered.env. NODERED_ENABLE=false: revoke it.
            Exit 10 when nodered.env changed.
  palette   preinstall the pinned palette packages into <data>/node-red (npm runs inside the
            pinned image as uid 1000 with --ignore-scripts). Skipped with a WARN when offline.

Settings come from the environment file given by --env-file (written by the installer).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
READ_DESCRIPTION = "vesselstack-nodered-reader"
MQTT_USER = "vesselstack-nodered"
NODERED_UID = NODERED_GID = 1000
IMAGE_RE = re.compile(r"^\s*image:\s*(nodered/node-red:\S+)\s*$", re.M)
HASH_RE = re.compile(r"^\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}$")
BUCKET_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
PALETTE = ("node-red-contrib-home-assistant-websocket@0.83.0", "@flowfuse/node-red-dashboard@1.36.1")
EXIT_CHANGED = 10
HASH_JS = (
    "const bc=require('bcryptjs');let d='';process.stdin.setEncoding('utf8');"
    "process.stdin.on('data',c=>d+=c).on('end',()=>{const a=d.split('\\n');const p=a[0]||'';const h=(a[1]||'').trim();"
    "if(!p){process.exit(2)}process.stdout.write(h&&bc.compareSync(p,h)?h:bc.hashSync(p,10))})"
)


class Failure(Exception):
    pass


def _datapath():
    spec = importlib.util.spec_from_file_location("vs_signalk_datapath", HERE / "configure-signalk-datapath.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find_image(compose: Path | None) -> str:
    candidates = [compose] if compose else [HERE.parent / "templates" / "compose.yml", Path("/opt/vesselstack/compose.yml")]
    for path in candidates:
        try:
            match = IMAGE_RE.search(Path(path).read_text(encoding="utf-8"))
        except OSError:
            continue
        if match:
            return match.group(1)
    raise Failure("cannot find the pinned Node-RED image in compose.yml")


def docker(args, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=False, capture_output=True, text=True, **kwargs)


def ensure_image(image: str, offline: bool) -> None:
    if docker(["image", "inspect", image]).returncode == 0:
        return
    if offline:
        raise Failure(f"the Node-RED image {image} is not loaded and this is an offline install; load it with 'docker load'")
    if docker(["pull", "--quiet", image]).returncode != 0:
        raise Failure(f"cannot pull {image}; connect to the internet once, then run the installer again")


def cmd_hash(args) -> int:
    image = find_image(args.compose and Path(args.compose))
    ensure_image(image, args.offline)
    data = sys.stdin.read()
    proc = subprocess.run(
        ["docker", "run", "--rm", "-i", "--network", "none", "--cap-drop", "ALL",
         "--security-opt", "no-new-privileges", "--entrypoint", "node", image, "-e", HASH_JS],
        input=data, capture_output=True, text=True, check=False)
    out = proc.stdout.strip()
    if proc.returncode != 0 or not HASH_RE.fullmatch(out):
        raise Failure("could not hash the Node-RED passphrase")
    print(out)
    return 0


def quote(value: str) -> str:
    if any(ch in value for ch in "'\n\r\x00"):
        raise Failure("a Node-RED value contains a character that cannot be written to nodered.env")
    return "'" + value + "'"


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line.strip())
        if match:
            v = match.group(2)
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            values[match.group(1)] = v
    return values


def host_for_container(url: str, scheme_ws: bool = False) -> str:
    """Loopback addresses mean the host, which the container reaches as host.docker.internal."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0", ""):
        port = f":{parsed.port}" if parsed.port else ""
        netloc = "host.docker.internal" + port
        return urllib.parse.urlunparse(parsed._replace(netloc=netloc))
    return url


def mqtt_password(root: Path) -> str:
    path = root / "config" / "displays.secrets.json"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            value = (json.load(handle).get("nodered") or {}).get("mqtt_password")
    except (OSError, ValueError, AttributeError):
        value = None
    if not isinstance(value, str) or not value:
        raise Failure("no Node-RED MQTT password yet; the installer's MQTT sync creates it")
    return value


def build_env(root: Path, env: dict[str, str], previous: dict[str, str]) -> dict[str, str]:
    def origin(url: str) -> str:  # scheme://host:port only: credentials and paths never reach the container
        parts = urllib.parse.urlparse(host_for_container(url))
        return f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    signalk = origin(env.get("SIGNALK_URL") or "http://127.0.0.1:3000")
    ws = re.sub(r"^http", "ws", signalk) + "/signalk/v1/stream?subscribe=self"
    ha = origin(env.get("HOME_ASSISTANT_URL") or "http://127.0.0.1:8123")
    for key in ("INFLUXDB_RAW_BUCKET", "INFLUXDB_HISTORY_BUCKET"):
        if not BUCKET_RE.fullmatch(env.get(key, "")):
            raise Failure(f"{key} is not a valid bucket name")
    hash_value = env.get("NODERED_ADMIN_PASSWORD_HASH", "")
    if not HASH_RE.fullmatch(hash_value):
        raise Failure("NODERED_ADMIN_PASSWORD_HASH is not a bcrypt hash")
    if not env.get("NODERED_CREDENTIAL_SECRET"):
        raise Failure("NODERED_CREDENTIAL_SECRET is empty")
    return {
        "MQTT_HOST": "mosquitto", "MQTT_PORT": "1883", "MQTT_USER": MQTT_USER,
        "MQTT_PASSWORD": mqtt_password(root),
        "INFLUX_URL": "http://influxdb:8086", "INFLUX_ORG": env.get("INFLUXDB_ORG", ""),
        "INFLUX_TOKEN": previous.get("INFLUX_TOKEN", ""),
        "INFLUX_RAW_BUCKET": env["INFLUXDB_RAW_BUCKET"], "INFLUX_HISTORY_BUCKET": env["INFLUXDB_HISTORY_BUCKET"],
        "SIGNALK_WS_URL": ws, "HOMEASSISTANT_URL": ha,
        "NODERED_ADMIN_PASSWORD_HASH": hash_value, "NODERED_CREDENTIAL_SECRET": env["NODERED_CREDENTIAL_SECRET"],
    }


def config_dir_fd(root: Path) -> int:
    path = root / "config"
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise Failure("the configuration directory is not a plain directory")
    if os.geteuid() == 0 and (st.st_uid != 0 or st.st_mode & 0o022):
        raise Failure("the configuration directory must be owned by root and not group/world-writable")
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def write_env_file(root: Path, values: dict[str, str]) -> bool:
    """Atomically write config/nodered.env (0600). Returns True when the content changed."""
    text = "# Generated by the VesselStack installer for Node-RED; do not edit (edits are overwritten).\n" + \
        "".join(f"{k}={quote(v)}\n" for k, v in values.items())
    fd = config_dir_fd(root)
    try:
        try:
            old_fd = os.open("nodered.env", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            with os.fdopen(old_fd, "r", encoding="utf-8") as handle:
                old = handle.read()
        except FileNotFoundError:
            old = None
        if old == text:
            try:
                os.chmod("nodered.env", 0o600, dir_fd=fd, follow_symlinks=False)
            except (NotImplementedError, OSError):
                pass
            return False
        tmp = f".nodered.env.{secrets.token_hex(6)}"
        tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
        try:
            with os.fdopen(tfd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, "nodered.env", src_dir_fd=fd, dst_dir_fd=fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=fd)
            except OSError:
                pass
            raise
        return True
    finally:
        os.close(fd)


def remove_env_file(root: Path) -> None:
    fd = config_dir_fd(root)
    try:
        try:
            os.unlink("nodered.env", dir_fd=fd)
        except FileNotFoundError:
            pass
    finally:
        os.close(fd)


def ensure_data_dir(data: Path) -> int:
    """Open <data>/node-red (created owner 1000:1000, 0750 on first use); never through a symlink."""
    path = data / "node-red"
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        os.mkdir(path, 0o750)
        os.chown(path, NODERED_UID, NODERED_GID)
        st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise Failure(f"{path} is not a plain directory")
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def seed_settings(dir_fd: int, template: Path) -> bool:
    """settings.js is created only when absent: the owner's edits survive every installer run."""
    try:
        existing = os.lstat("settings.js", dir_fd=dir_fd)
        if stat.S_ISLNK(existing.st_mode):
            raise Failure("settings.js in the Node-RED data directory is a symbolic link")
        return False
    except FileNotFoundError:
        pass
    body = template.read_bytes()
    fd = os.open("settings.js", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=dir_fd)
    try:
        os.write(fd, body)
        os.fchown(fd, NODERED_UID, NODERED_GID)
        os.fchmod(fd, 0o644)
    finally:
        os.close(fd)
    return True


def cmd_render(args) -> int:
    root, data = Path(args.root), Path(args.data)
    env = read_env_file(Path(args.env_file))
    if env.get("NODERED_ENABLE") != "true":
        remove_env_file(root)
        print("Node-RED: disabled (NODERED_ENABLE=false); its data is kept")
        return 0
    dir_fd = ensure_data_dir(data)
    try:
        created = seed_settings(dir_fd, Path(args.settings_template))
    finally:
        os.close(dir_fd)
    previous = read_env_file(root / "config" / "nodered.env")
    changed = write_env_file(root, build_env(root, env, previous))
    print("Node-RED: " + ("seeded settings.js; " if created else "kept your settings.js; ")
          + ("wrote nodered.env" if changed else "nodered.env unchanged"))
    return 0


def cmd_token(args) -> int:
    root = Path(args.root)
    env = read_env_file(Path(args.env_file))
    dp = _datapath()
    enabled = env.get("NODERED_ENABLE") == "true"
    base = dp.influx_base(env)
    admin, org = env.get("INFLUXDB_TOKEN", ""), env.get("INFLUXDB_ORG", "")
    q = urllib.parse.quote

    def listing(org_id):
        return dp.http_json("GET", f"{base}/api/v2/authorizations?orgID={q(org_id)}", admin).get("authorizations", [])

    def org_id_of(bucket):
        found = dp.http_json("GET", f"{base}/api/v2/buckets?name={q(bucket)}&org={q(org)}", admin).get("buckets", [])
        match = next((b for b in found if b.get("name") == bucket), None)
        if not match or not match.get("id") or not match.get("orgID"):
            raise Failure(f"bucket {bucket} not found")
        return match["orgID"], match["id"]

    previous = read_env_file(root / "config" / "nodered.env")
    raw, hist = env.get("INFLUXDB_RAW_BUCKET", ""), env.get("INFLUXDB_HISTORY_BUCKET", "")
    try:
        if not enabled:
            org_id, _ = org_id_of(raw)
            for auth in listing(org_id):
                if auth.get("description") == READ_DESCRIPTION and auth.get("id"):
                    dp.http_json("DELETE", f"{base}/api/v2/authorizations/{q(auth['id'])}", admin)
            print("Node-RED: revoked its InfluxDB token (if any)")
            return 0
        if not (admin and org and BUCKET_RE.fullmatch(raw) and BUCKET_RE.fullmatch(hist)):
            raise Failure("the InfluxDB settings are incomplete")
        org_id, raw_id = org_id_of(raw)
        _, hist_id = org_id_of(hist)
        dp.ensure_dbrp(base, admin, org_id, raw_id, raw)
        dp.ensure_dbrp(base, admin, org_id, hist_id, hist)
        existing = [a for a in listing(org_id) if a.get("description") == READ_DESCRIPTION]
        keep = previous.get("INFLUX_TOKEN", "")
        if keep and any(a.get("token") == keep and a.get("status", "active") == "active" for a in existing) and len(existing) == 1:
            print("Node-RED: InfluxDB read-only token is current")
            return 0
        for auth in existing:
            if auth.get("id"):
                dp.http_json("DELETE", f"{base}/api/v2/authorizations/{q(auth['id'])}", admin)
        perms = [{"action": "read", "resource": {"type": "orgs", "id": org_id}},
                 {"action": "read", "resource": {"type": "dbrp", "orgID": org_id}}]
        for bid in (raw_id, hist_id):
            perms.append({"action": "read", "resource": {"type": "buckets", "id": bid, "orgID": org_id}})
        created = dp.http_json("POST", f"{base}/api/v2/authorizations", admin,
                               {"orgID": org_id, "description": READ_DESCRIPTION, "permissions": perms})
        token = created.get("token") if isinstance(created, dict) else None
        if not isinstance(token, str) or not token:
            raise Failure("InfluxDB returned no token")
    except (urllib.error.URLError, OSError, ValueError, dp.StepFailed) as exc:
        raise Failure("could not create the Node-RED InfluxDB token (is InfluxDB running?)") from exc
    values = build_env(root, env, {"INFLUX_TOKEN": token})
    changed = write_env_file(root, values)
    print("Node-RED: created a read-only InfluxDB token for the raw and history buckets")
    return EXIT_CHANGED if changed else 0


def cmd_palette(args) -> int:
    data = Path(args.data) / "node-red"
    image = find_image(args.compose and Path(args.compose))
    try:
        package = json.loads((data / "package.json").read_text())
        deps = package.get("dependencies", {}) if isinstance(package, dict) else {}
    except (OSError, ValueError):
        deps = {}
    missing = [p for p in PALETTE if p.rsplit("@", 1)[0] not in deps]
    if not missing:
        print("Node-RED: palette packages already installed")
        return 0
    if args.offline:
        print("WARN: offline install: the Node-RED palette packages were not installed; install them from the editor "
              "(Manage palette) or rerun the installer online.", file=sys.stderr)
        return 0
    ensure_image(image, False)
    script = ('set -e; cd /data; [ -f package.json ] || printf \'{"name":"vesselstack-node-red","version":"1.0.0","private":true}\' '
              '> package.json; npm install --ignore-scripts --save-exact --no-audit --no-fund --loglevel=error "$@"')
    proc = docker(["run", "--rm", "--user", f"{NODERED_UID}:{NODERED_GID}", "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges", "-e", "npm_config_cache=/tmp/npm-cache", "--tmpfs", "/tmp:rw,size=256m",
                   "-v", f"{data}:/data", "--entrypoint", "sh", image, "-c", script, "sh", *missing])
    if proc.returncode != 0:
        print("WARN: could not install the Node-RED palette packages (npm registry unreachable?); "
              "install them from the editor (Manage palette) or rerun the installer.", file=sys.stderr)
        return 0
    print("Node-RED: installed " + ", ".join(missing))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("hash", cmd_hash), ("render", cmd_render), ("token", cmd_token), ("palette", cmd_palette)):
        p = sub.add_parser(name)
        p.add_argument("--root", default="/opt/vesselstack")
        p.add_argument("--data", default="/opt/vesselstack-data")
        p.add_argument("--env-file", default="")
        p.add_argument("--compose", default="")
        p.add_argument("--settings-template", default=str(HERE.parent / "templates" / "node-red" / "settings.js"))
        p.add_argument("--offline", action="store_true")
        p.set_defaults(func=func)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.env_file:
        args.env_file = str(Path(args.root) / "config" / "vesselstack.env")
    try:
        return args.func(args)
    except Failure as exc:
        print(f"Node-RED: {exc}", file=sys.stderr)
    except OSError as exc:
        print(f"Node-RED: {type(exc).__name__} ({exc.filename or 'file operation'})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
