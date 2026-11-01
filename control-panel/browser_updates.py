#!/usr/bin/env python3
"""Bounded, source-locked browser update support.

This module deliberately contains no user supplied URL or command path.  The
browser may select a GitHub release id, but all release metadata and assets are
resolved again by the privileged helper before any backup or installation.
"""
from __future__ import annotations

import hashlib
import fcntl
import ipaddress
import shlex
import errno
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPOSITORY = "jclima/VesselStack"
API_RELEASES = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=30"
API_RELEASE_BASE = f"https://api.github.com/repos/{REPOSITORY}/releases"
ASSET_HOST = "github.com"
SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?$")
ASSET_RE = re.compile(r"^vesselstack-(\d+\.\d+\.\d+)\.tar\.gz$")
MAX_DOWNLOAD = 512 * 1024 * 1024
MAX_OUTPUT = 32 * 1024
MAX_METADATA = 2 * 1024 * 1024
MAX_MEMBERS = 10000
MAX_ARCHIVE_EXPANDED = 1024 * 1024 * 1024
FIXED_CTL = "/usr/local/sbin/vesselstackctl"
OPERATION_FILE_NAME = "browser-update.json"
PROCESS_BOOT_ID = uuid.uuid4().hex


def _require_trusted_path(path: Path) -> Path:
    """Require an absolute path whose existing ancestors are root-owned (or us)."""
    raw = os.fspath(path)
    if any(part == ".." for part in Path(raw).parts):
        raise ValueError("path traversal is not allowed")
    absolute = Path(os.path.abspath(path))
    current = absolute
    euid = os.geteuid()
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            current = current.parent
            if current == current.parent:
                break
            continue
        if current == Path("/var") and info.st_mode & 0o120000 == 0o120000:
            current = current.parent
            continue
        if current.is_symlink() or info.st_uid not in {0, euid} or info.st_mode & 0o022:
            raise ValueError("untrusted state path")
        if current == current.parent:
            break
        current = current.parent
    return absolute


def _read_json_private(path: Path, limit: int) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(_require_trusted_path(path), flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("private state file is invalid")
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise ValueError("private state file is too large")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("private state must be an object")
    return value


def version_tuple(value: str) -> tuple[int, int, int] | None:
    match = SEMVER.fullmatch(str(value).strip())
    return tuple(int(part) for part in match.groups()) if match else None


def version_text(value: str) -> str:
    parsed = version_tuple(value)
    if not parsed:
        raise ValueError("release version must be numeric semver")
    return ".".join(str(part) for part in parsed)


def release_snapshot(release_id: int, channel: str, root: Path) -> dict[str, Any]:
    if not isinstance(release_id, int) or isinstance(release_id, bool) or release_id <= 0: raise ValueError("invalid release id")
    if channel not in {"stable", "lab"}: raise ValueError("invalid channel")
    item = _json_get(f"{API_RELEASE_BASE}/{release_id}")
    if not isinstance(item, dict) or item.get("draft") is True or not isinstance(item.get("id"), int) or item["id"] != release_id: raise ValueError("invalid release snapshot")
    tag = item.get("tag_name", "")
    if not isinstance(tag, str) or not re.fullmatch(r"v\d+\.\d+\.\d+", tag): raise ValueError("release tag must be exact numeric semver")
    version = version_text(tag); current = version_tuple(installed_version(root))
    if (version_tuple(version) or (0, 0, 0)) <= (current or (0, 0, 0)) or (channel == "stable" and item.get("prerelease") is True): raise ValueError("release is not eligible")
    published = item.get("published_at"); updated = item.get("updated_at")
    if not isinstance(published, str) or not published or not isinstance(updated, str) or not updated: raise ValueError("release timestamps required")
    assets = item.get("assets")
    if not isinstance(assets, list): raise ValueError("release assets required")
    names = {f"vesselstack-{version}.tar.gz": "archive", f"vesselstack-{version}.tar.gz.sha256": "checksum"}; normalized = {}
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") not in names: continue
        filename = asset["name"]
        key = names[filename]
        if key in normalized or not isinstance(asset.get("id"), int) or isinstance(asset.get("id"), bool) or asset["id"] <= 0 or not isinstance(asset.get("size"), int) or isinstance(asset.get("size"), bool) or asset["size"] <= 0 or asset["size"] > (MAX_DOWNLOAD if key == "archive" else 4096) or not isinstance(asset.get("updated_at"), str): raise ValueError("invalid or duplicate release asset")
        digest = asset.get("digest")
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest)): raise ValueError("invalid asset digest")
        normalized[key] = {"id": asset["id"], "name": filename, "size": asset["size"], "updated_at": asset["updated_at"], "digest": digest, "url": f"https://github.com/{REPOSITORY}/releases/download/{tag}/{filename}"}
    if len(normalized) != 2: raise ValueError("fixed release assets are required")
    return {"id": release_id, "tag": tag, "version": version, "name": str(item.get("name") or tag), "prerelease": bool(item.get("prerelease")), "published_at": published, "updated_at": updated, "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{tag}", "assets": {"archive": normalized["archive"], "checksum": normalized["checksum"]}}


def assert_snapshot_unchanged(saved: dict[str, Any], fresh: dict[str, Any]) -> None:
    if (saved.get("id"), saved.get("tag"), saved.get("version"), saved.get("prerelease"), saved.get("updated_at")) != (fresh.get("id"), fresh.get("tag"), fresh.get("version"), fresh.get("prerelease"), fresh.get("updated_at")): raise ValueError("release snapshot changed")
    for key in ("archive", "checksum"):
        a, b = saved.get("assets", {}).get(key, {}), fresh.get("assets", {}).get(key, {})
        if tuple(a.get(field) for field in ("id", "name", "size", "updated_at", "digest")) != tuple(b.get(field) for field in ("id", "name", "size", "updated_at", "digest")): raise ValueError("release asset snapshot changed")


def installed_version(root: Path, env: dict[str, str] | None = None) -> str:
    candidates = [root / "config" / "installed-version", root / "installer" / "VERSION", root / "VERSION"]
    candidate = ""
    for marker in candidates:
        try:
            candidate = marker.read_text().strip()
            if candidate:
                break
        except OSError:
            continue
    if not candidate:
        candidate = "0.0.0"
    return version_text(candidate)


def running_identity(root: Path | None = None) -> dict[str, str]:
    boot = "unknown"
    boot = PROCESS_BOOT_ID
    version = ""
    if root is not None:
        for marker in (root / "installer" / "VERSION", root / "VERSION"):
            try:
                version = marker.read_text().strip()
                if version:
                    break
            except OSError:
                continue
    if not version:
        version = "development"
    return {"running_version": version or "development", "boot_id": boot}


class _NoPanelRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("panel health redirects are not allowed")


def _panel_health_url(root: Path) -> str:
    values = {}
    try:
        for line in (root / "config" / "control-panel.env").read_text().splitlines():
            if "=" in line:
                key, raw = line.split("=", 1); values[key] = shlex.split(raw)[0] if raw else ""
    except OSError:
        pass
    host = values.get("CONTROL_PANEL_HOST", "127.0.0.1")
    try:
        address = ipaddress.ip_address(host)
        if address.is_unspecified: host = "127.0.0.1" if address.version == 4 else "::1"
        hostpart = f"[{host}]" if address.version == 6 else host
    except ValueError:
        raise ValueError("control panel host must be an IP address")
    port = values.get("CONTROL_PANEL_PORT", "8780")
    if not port.isdigit() or not 1 <= int(port) <= 65535: raise ValueError("invalid control panel port")
    return f"http://{hostpart}:{port}/health"


def _probe_panel_health(root: Path, timeout: float = 1.0) -> dict[str, str]:
    request = urllib.request.Request(_panel_health_url(root), headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoPanelRedirect())
    with opener.open(request, timeout=timeout) as response:
        if getattr(response, "status", 200) != 200: raise ValueError("panel health status was not 200")
        body = response.read(4097)
        if len(body) > 4096: raise ValueError("panel health response is too large")
        data = json.loads(body)
    if not isinstance(data, dict) or data.get("ok") is not True or not re.fullmatch(r"[0-9a-f]{32}", str(data.get("boot_id", ""))) or not re.fullmatch(r"\d+\.\d+\.\d+", str(data.get("version", ""))):
        raise ValueError("invalid panel health response")
    return {"boot_id": str(data["boot_id"]), "running_version": str(data["version"])}


def verify_restart(root: Path, old_boot_id: str, target_version: str, timeout: float = 60.0) -> dict[str, str]:
    if not old_boot_id or not target_version:
        raise ValueError("restart verification requires expected identity")
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            active = subprocess.run(["systemctl", "is-active", "vesselstack-control-panel.service"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3, check=False)
            if active.returncode != 0 or active.stdout.strip() != "active":
                raise RuntimeError("control panel unit is not active")
            identity = _probe_panel_health(root)
            if identity["boot_id"] != old_boot_id and identity["running_version"] == target_version:
                return identity
            last = identity
        except Exception as exc:
            last = exc
        time.sleep(0.1)
    raise RuntimeError(f"panel restart verification timed out: {last}")


def _validate_url(url: str, hosts: set[str]) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username or parsed.password or parsed.port or parsed.fragment:
        raise ValueError("release source is not the fixed GitHub source")
    return parsed


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl, {"api.github.com", ASSET_HOST, "release-assets.githubusercontent.com", "objects.githubusercontent.com"})
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, timeout: int = 8):
    _validate_url(url, {"api.github.com", ASSET_HOST, "release-assets.githubusercontent.com", "objects.githubusercontent.com"})
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "VesselStack-control-panel"})
    return urllib.request.build_opener(_Redirects()).open(request, timeout=timeout)


def _json_get(url: str, timeout: int = 8) -> Any:
    with _open(url, timeout=timeout) as response:
        body = response.read(MAX_METADATA + 1)
        if len(body) > MAX_METADATA:
            raise ValueError("release metadata is too large")
        return json.loads(body)


def release_catalog(channel: str, root: Path, env: dict[str, str] | None = None, running_version: str | None = None) -> dict[str, Any]:
    if channel not in {"stable", "lab"}:
        raise ValueError("channel must be stable or lab")
    current = installed_version(root, env)
    current_tuple = version_tuple(current) or (0, 0, 0)
    raw = _json_get(API_RELEASES)
    releases: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            continue
        tag = str(item.get("tag_name", ""))
        if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
            continue
        parsed = version_tuple(tag)
        if not parsed or parsed <= current_tuple:
            continue
        if item.get("draft") is True: continue
        prerelease = bool(item.get("prerelease", False))
        if channel == "stable" and prerelease:
            continue
        releases.append({"id": item["id"], "tag": tag, "version": version_text(tag), "name": str(item.get("name") or tag), "prerelease": prerelease, "published_at": item.get("published_at"), "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{tag}"})
    releases.sort(key=lambda item: version_tuple(item["version"]) or (0, 0, 0), reverse=True)
    return {"channel": channel, "installed_version": current, "running_version": running_version if running_version is not None else running_identity(root)["running_version"], "releases": releases, "checked_at": datetime.now(timezone.utc).isoformat()}


def operation_path(root: Path) -> Path:
    return root / "var" / "lib" / "vesselstack" / OPERATION_FILE_NAME


def lock_path(root: Path) -> Path:
    return root / "var" / "lib" / "vesselstack" / "browser-update.lock"


def acquire_slot(root: Path):
    path = _require_trusted_path(lock_path(root)); _require_trusted_path(path.parent); path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = read_operation(root, strict=True)
        if current.get("state") == "running":
            try:
                probe = subprocess.run(["systemctl", "is-active", "vesselstack-browser-update.service"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3, check=False)
                unit_state = probe.stdout.strip()
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ValueError("Another operation is already running") from exc
            if unit_state not in {"inactive", "failed", "dead", "not-found"}:
                raise ValueError("Another operation is already running")
            current.update(state="failed", phase="failed", finished_at=int(time.time()), exit_code=1)
            output = list(current.get("output") or [])
            output.append("Interrupted previous browser update operation")
            current["output"] = output[-400:]
            write_operation(root, current)
        return fd
    except BlockingIOError as exc:
        os.close(fd); raise ValueError("Another operation is already running") from exc
    except Exception:
        os.close(fd); raise


def acquire_runner_lock(root: Path):
    path = _require_trusted_path(lock_path(root)); _require_trusted_path(path.parent); path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def release_slot(fd) -> None:
    os.close(fd)


def start_detached(action: str, root: Path, old_identity: dict[str, str], release_id: int | None = None, channel: str | None = None, ctl: str = FIXED_CTL, target_version: str | None = None, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    state = read_operation(root)
    if state.get("state") == "running":
        raise ValueError("Another operation is already running")
    job = root / "var" / "lib" / "vesselstack" / f"browser-update-{uuid.uuid4().hex}"
    _require_trusted_path(job.parent)
    job.mkdir(parents=True, exist_ok=False); os.chmod(job, 0o700)
    if action == "update-release":
        if not isinstance(snapshot, dict):
            if release_id is None or channel is None:
                raise ValueError("release snapshot is required")
            snapshot = release_snapshot(release_id, channel, root)
        elif release_id is not None and channel is not None:
            assert_snapshot_unchanged(snapshot, release_snapshot(release_id, channel, root))
        target_version = snapshot.get("version")
        if not isinstance(target_version, str):
            raise ValueError("release snapshot is invalid")
    target_version = target_version or running_identity(root)["running_version"]
    request = {"id": int(time.time() * 1000), "action": action, "root": str(root), "config": str(root / "config" / "vesselstack.env"), "ctl": ctl, "old_boot_id": old_identity.get("boot_id", ""), "target_version": target_version, "release_id": release_id, "channel": channel}
    if action == "update-release":
        request["release_snapshot"] = snapshot
    (job / "request.json").write_text(json.dumps(request, sort_keys=True)); os.chmod(job / "request.json", 0o600)
    shutil.copy2(Path(__file__), job / "runner.py"); os.chmod(job / "runner.py", 0o700)
    operation = {"id": request["id"], "label": action, "state": "running", "phase": "queued", "target_version": request["target_version"], "backup_path": None, "started_at": int(time.time()), "finished_at": None, "exit_code": None, "output": [], "boot_id": old_identity.get("boot_id"), "running_version": old_identity.get("running_version")}
    write_operation(root, operation)
    command = ["systemd-run", "--unit=vesselstack-browser-update", "--collect", "--property=Type=exec", "/usr/bin/python3", "-I", str(job / "runner.py"), "--worker", str(job / "request.json")]
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except Exception as exc:
        prior = list(operation.get("output") or [])
        prior.append(str(exc)[:MAX_OUTPUT])
        operation.update(state="failed", phase="failed", finished_at=int(time.time()), exit_code=1, output=prior[-400:])
        write_operation(root, operation)
        raise
    return operation


def read_operation(root: Path, strict: bool = False) -> dict[str, Any]:
    try:
        data = _read_json_private(operation_path(root), 256 * 1024)
        if not isinstance(data, dict) or data.get("state") not in {"idle", "running", "succeeded", "failed"}:
            raise ValueError("Invalid durable operation state")
        return data
    except FileNotFoundError:
        if strict:
            return {"state": "idle", "output": []}
        return {"state": "idle", "output": []}
    except (OSError, json.JSONDecodeError):
        if strict:
            raise ValueError("Malformed durable operation state")
        return {"state": "idle", "output": []}


def public_operation(root: Path) -> dict[str, Any]:
    """Read operation state and reconcile an interrupted stale worker."""
    current = read_operation(root)
    if current.get("state") != "running":
        return current
    try:
        fd = acquire_slot(root)
    except Exception:
        return current
    else:
        release_slot(fd)
        return read_operation(root)
def write_operation(root: Path, operation: dict[str, Any]) -> None:
    path = operation_path(root)
    _require_trusted_path(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        payload = json.dumps(operation, sort_keys=True).encode()
        if len(payload) > 256 * 1024:
            raise ValueError("operation state is too large")
        os.write(fd, payload)
        os.fsync(fd)
        os.close(fd)
        os.replace(temporary, path)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _release_by_id(release_id: int, channel: str, root: Path) -> dict[str, Any]:
    if not isinstance(release_id, int) or isinstance(release_id, bool) or release_id <= 0:
        raise ValueError("release_id must be a positive integer")
    catalog = release_catalog(channel, root)
    for release in catalog["releases"]:
        if release["id"] == release_id:
            return release
    raise ValueError("release is not an eligible newer release")


def _download(url: str, destination: Path, max_bytes: int = MAX_DOWNLOAD) -> None:
    _validate_url(url, {ASSET_HOST, "objects.githubusercontent.com", "release-assets.githubusercontent.com"})
    with _open(url, timeout=30) as response, destination.open("wb") as handle:
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("release asset is too large")
            handle.write(chunk)


# Public name used by the privileged update orchestration.  Keeping the
# bounded implementation in one place makes it straightforward to mock in
# admission/worker tests without introducing a second downloader.
bounded_download = _download


def run_command(argv: list[str], root: Path, operation: dict[str, Any], timeout: float,
                env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env=env, start_new_session=True)
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    tail = b""
    received = False
    last_persist = time.monotonic()
    started = last_persist
    try:
        while True:
            if time.monotonic() - started > timeout:
                try:
                    os.killpg(process.pid, 15)
                    process.wait(timeout=1)
                except Exception:
                    try:
                        os.killpg(process.pid, 9)
                    except OSError:
                        pass
                    process.wait(timeout=2)
                operation["output"] = tail.decode("utf-8", "replace").splitlines()[-400:] + [f"command timed out: {argv[0]}"]
                write_operation(root, operation)
                raise RuntimeError(f"command timed out: {argv[0]}")
            events = selector.select(min(0.1, max(0.01, timeout - (time.monotonic() - started))))
            for key, _ in events:
                data = os.read(key.fd, 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                if len(data) > MAX_OUTPUT:
                    data = data[-MAX_OUTPUT:]
                tail = (tail + data)[-MAX_OUTPUT:]
                received = True
            if process.poll() is not None and not events:
                try:
                    selector.unregister(process.stdout)
                except Exception:
                    pass
            if received and time.monotonic() - last_persist >= 0.5:
                operation["output"] = tail.decode("utf-8", "replace").splitlines()[-400:]
                write_operation(root, operation)
                last_persist = time.monotonic()
                received = False
            if process.poll() is not None and not selector.get_map():
                break
        code = process.wait()
    finally:
        try:
            os.killpg(process.pid, 9)
        except OSError:
            pass
        selector.close()
        process.stdout.close()
    output = tail.decode("utf-8", "replace")
    operation["output"] = output.splitlines()[-400:]
    if code != 0:
        write_operation(root, operation)
        raise subprocess.CalledProcessError(code, argv, output=output)
    return subprocess.CompletedProcess(argv, code, stdout=output)


def _validate_fixed_ctl() -> None:
    _require_trusted_path(Path(FIXED_CTL))
    try:
        info = os.stat(FIXED_CTL)
    except OSError as exc:
        raise ValueError("fixed control binary is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(FIXED_CTL, os.X_OK):
        raise ValueError("fixed control binary is not executable")


def safe_extract(archive: Path, destination: Path, expected_version: str | None = None) -> None:
    destination = Path(os.path.abspath(destination))
    cursor = destination
    while True:
        # macOS maps /var to /private/var; this platform alias is not a
        # user-controlled component and is safe to traverse.
        if cursor.is_symlink() and cursor != Path("/var"):
            raise ValueError("extraction destination has a symlink ancestor")
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    destination.mkdir(parents=True, exist_ok=True)
    resolved_destination = destination.resolve()
    with tarfile.open(archive, "r:gz") as handle:
        seen: set[str] = set()
        total = 0
        count = 0
        top: str | None = None
        version_member = None
        installer_member = None
        for member in handle:
            count += 1
            if count > MAX_MEMBERS:
                raise ValueError("archive contains too many entries")
            if "\\" in member.name:
                raise ValueError("unsafe archive path")
            name = member.name
            if any(ord(char) < 32 for char in name) or name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/")):
                raise ValueError("unsafe archive path")
            parts = name.rstrip("/").split("/")
            if top is None: top = parts[0]
            expected_top = f"vesselstack-{expected_version}" if expected_version else None
            if parts[0] != top or not re.fullmatch(r"vesselstack-\d+\.\d+\.\d+", top) or (expected_top and top != expected_top):
                raise ValueError("archive has unexpected top-level path")
            normalized = name.rstrip("/")
            target = (destination / name).resolve()
            if normalized in seen or target != resolved_destination and resolved_destination not in target.parents:
                raise ValueError("unsafe or duplicate archive path")
            seen.add(normalized)
            if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise ValueError("archive contains links or special files")
            if member.uid != 0 or member.gid != 0:
                raise ValueError("archive contains untrusted ownership")
            if member.mode & 0o7000 or member.mode & 0o022:
                raise ValueError("archive contains unsafe file mode")
            if member.size > MAX_DOWNLOAD:
                raise ValueError("archive member is too large")
            total += member.size
            if total > MAX_ARCHIVE_EXPANDED:
                raise ValueError("archive expands beyond the safety limit")
            if normalized == f"{top}/VERSION":
                version_member = member
            if normalized == f"{top}/install.sh":
                installer_member = member
        if version_member is None or version_member.size > 64:
            raise ValueError("archive VERSION is missing or too large")
        version_file = handle.extractfile(version_member)
        if version_file is None or version_file.read(65).decode("utf-8", "strict").strip() != expected_version:
            raise ValueError("archive VERSION does not match selected release")
        if installer_member is None or not (installer_member.mode & 0o100):
            raise ValueError("archive installer is missing or not executable")
        for member in handle.getmembers():
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                os.chmod(target, member.mode & 0o777)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, member.mode & 0o777)
            try:
                source = handle.extractfile(member)
                if source is None:
                    raise ValueError("archive file has no content")
                with os.fdopen(fd, "wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                fd = -1
            finally:
                if fd >= 0:
                    os.close(fd)
    if top is None or (expected_version and top != f"vesselstack-{expected_version}") or not (destination / top / "VERSION").is_file() or not (destination / top / "install.sh").is_file() or not (destination / top / "scripts").is_dir() or not (destination / top / "systemd").is_dir() or not (destination / top / "templates").is_dir() or not (destination / top / "control-panel").is_dir():
        raise ValueError("archive is missing required release structure")


def run_update(request: dict[str, Any], operation: dict[str, Any], root: Path) -> None:
    """Execute the reviewed update sequence for a detached worker request."""
    release_id = int(request["release_id"])
    channel = str(request["channel"])
    config = str(request["config"])
    saved = request.get("release_snapshot")
    if not isinstance(saved, dict):
        raise ValueError("release snapshot is required")
    fresh = release_snapshot(release_id, channel, root)
    assert_snapshot_unchanged(saved, fresh)
    target_version = fresh["version"]
    expected_archive = fresh["assets"]["archive"]["name"]
    expected_checksum = fresh["assets"]["checksum"]["name"]
    state_parent = root / "var" / "lib" / "vesselstack"
    _require_trusted_path(state_parent)
    state_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(state_parent)) as staging_name:
        staging = Path(staging_name)
        archive = staging / expected_archive
        checksum = staging / expected_checksum
        operation.update(phase="downloading", target_version=target_version)
        write_operation(root, operation)
        bounded_download(fresh["assets"]["archive"]["url"], archive, max_bytes=MAX_DOWNLOAD)
        bounded_download(fresh["assets"]["checksum"]["url"], checksum, max_bytes=4096)
        if archive.stat().st_size != fresh["assets"]["archive"]["size"]:
            raise ValueError("release archive size does not match metadata")
        digest_hash = hashlib.sha256()
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest_hash.update(chunk)
        actual_digest = digest_hash.hexdigest()
        checksum_lines = checksum.read_text().splitlines()
        if len(checksum_lines) != 1:
            raise ValueError("invalid release checksum sidecar")
        match = re.fullmatch(r"([0-9a-fA-F]{64})[ \t]+\*?([^ \t]+)", checksum_lines[0].strip())
        if not match or match.group(2) != expected_archive or match.group(1).lower() != actual_digest:
            raise ValueError("release checksum verification failed")
        declared_digest = fresh["assets"]["archive"].get("digest")
        if declared_digest and declared_digest.split(":", 1)[-1].lower() != actual_digest:
            raise ValueError("release asset digest verification failed")
        fresh_again = release_snapshot(release_id, channel, root)
        assert_snapshot_unchanged(saved, fresh_again)
        extract = staging / "dest"
        extract.mkdir()
        safe_extract(archive, extract, target_version)
        installer = extract / f"vesselstack-{target_version}" / "install.sh"
        if not installer.is_file():
            raise ValueError("release installer missing")

        child_env = dict(os.environ, VESSELSTACK_ROOT=str(root), VESSELSTACK_ENV=config)
        _validate_fixed_ctl()
        operation.update(phase="preflight"); write_operation(root, operation)
        run_command([str(installer), "--config", config, "--dry-run"], root, operation, 1800, child_env)
        operation.update(phase="backing-up"); write_operation(root, operation)
        backup_result = run_command([FIXED_CTL, "backup", "--require-mounted"], root, operation, 1800, child_env)
        output = (backup_result.stdout or "").splitlines()
        if not output or not output[-1].strip():
            raise ValueError("backup did not report an archive path")
        operation["backup_path"] = output[-1].strip()
        write_operation(root, operation)
        operation.update(phase="installing"); write_operation(root, operation)
        install_env = dict(child_env)
        install_env["VESSELSTACK_SKIP_PANEL_RESTART"] = "true"
        install_env["VESSELSTACK_ROOT"] = str(root)
        install_env["VESSELSTACK_ENV"] = config
        run_command([str(installer), "--config", config, "--start"], root, operation, 1800, install_env)
        operation.update(phase="restarting"); write_operation(root, operation)
        run_command(["systemctl", "restart", "vesselstack-control-panel.service"], root, operation, 60)
        operation.update(phase="verifying-restart"); write_operation(root, operation)
        identity = verify_restart(root, str(request["old_boot_id"]), target_version)
        operation.update(state="succeeded", phase="complete", finished_at=int(time.time()), exit_code=0,
                         boot_id=identity["boot_id"], running_version=identity["running_version"])


def run_worker(request_path: Path) -> None:
    request = _read_json_private(request_path, 64 * 1024)
    root = Path(str(request.get("root", "")))
    lock = acquire_runner_lock(root)
    try:
        request = _read_json_private(request_path, 64 * 1024)
        operation = read_operation(root)
        if operation.get("id") != request.get("id"):
            return
        if request.get("action") not in {"update-release", "restart-panel"}:
            raise ValueError("unsupported detached action")
        if Path(str(request.get("root"))).resolve() != root.resolve() or Path(str(request.get("config"))).resolve() != (root / "config" / "vesselstack.env").resolve() or request.get("ctl", FIXED_CTL) != FIXED_CTL:
            raise ValueError("detached request path is not fixed")
        if request.get("action") == "update-release":
            run_update(request, operation, root)
            return
        operation.update(phase="restarting"); write_operation(root, operation)
        subprocess.run(["systemctl", "restart", "vesselstack-control-panel.service"], check=True, timeout=60)
        active = subprocess.run(["systemctl", "is-active", "vesselstack-control-panel.service"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=False).stdout.strip()
        if active != "active": raise RuntimeError("control panel did not become active")
        operation.update(phase="verifying-restart"); write_operation(root, operation)
        identity = verify_restart(root, request.get("old_boot_id", ""), request.get("target_version", ""))
        operation.update(state="succeeded", phase="complete", finished_at=int(time.time()), exit_code=0, boot_id=identity["boot_id"], running_version=identity["running_version"])
    except Exception as exc:
        prior = list(operation.get("output") or [])
        prior.append(str(exc)[:MAX_OUTPUT])
        operation.update(state="failed", phase="failed", finished_at=int(time.time()), exit_code=1, output=prior[-400:])
    finally:
        write_operation(root, operation); release_slot(lock)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(); parser.add_argument("--worker"); args = parser.parse_args()
    if args.worker:
        run_worker(Path(args.worker)); raise SystemExit(0)
