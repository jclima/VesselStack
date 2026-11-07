"""Optional on-Pi firmware build and flashing for VesselStack displays (stdlib only).

``build`` renders the display's ESPHome project (``render.py``) and compiles it in a
one-shot ``docker run --rm`` container of the single pinned image
(``displays/esphome/IMAGE``). ``flash`` uploads exactly the bytes recorded in the
build manifest, with ``esphome upload --file`` (serial: the factory image at 0x0;
OTA: the app image); it never compiles and never pulls the image.

There is no Compose service, so ``compose up`` cannot start ESPHome. Nothing is
passed on a command line that is a secret: the OTA/Wi-Fi/MQTT secrets stay in the
root-owned ``secrets.yaml`` that ESPHome reads inside the container.

Layout under ``$VESSELSTACK_ROOT/build/displays/<id>/`` (root:root, 0700/0600)::

    project/                           the rendered ESPHome project (render.py): the ONLY
                                       directory the compile container mounts (as /config)
    project.prev/                      the project before it
    firmware/                          newest build: factory + ota image, manifest.json
    firmware.prev/                     the build before it (rollback)

The stored firmware sits beside ``project/``, never inside it, so a compromised compile
container (which can write to its ``/config`` mount) cannot rewrite the images or the
manifests that ``flash`` trusts. ``render`` replaces only ``project/``. Because that container can write to ``project/``,
``flash`` never mounts it: it re-renders the project into a private staging dir.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lifecycle  # noqa: E402
import mqtt_auth  # noqa: E402
import render  # noqa: E402
from render import RenderError  # noqa: E402

CACHE_VOLUME = "vesselstack-esphome-cache"
FIRMWARE_DIR = "firmware"
PREVIOUS_DIR = "firmware.prev"
NEW_DIR = "firmware.new"
MANIFEST_NAME = "manifest.json"
FACTORY_NAME = "firmware.factory.bin"
OTA_NAME = "firmware.ota.bin"
ARTIFACTS = (FACTORY_NAME, OTA_NAME)
GIB = 1024 ** 3
MIN_FREE_FIRST_BUILD = 3 * GIB
MIN_FREE_WARM_BUILD = 1 * GIB
DOCKER_ROOT_FALLBACK = "/var/lib/docker"
# Both containers run as root inside the container (the image default) but need no
# privileges: no capabilities and no setuid escalation.
CONTAINER_FLAGS = ["--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
SERIAL_REAL_RE = re.compile(r"^/dev/tty[A-Za-z0-9_.-]+$")
SERIAL_BY_ID_PREFIX = "/dev/serial/by-id/"
HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
RESERVED_TARGETS = {"bootsel", "mqtt", "mqttip"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_EXCLUDED = ("secrets.yaml", "README.txt")
PULL_NOTICE = ("the ESPHome image is not present locally: pulling it now (about 250 MB to "
               "download, 1.2 GB on disk) and then downloading the ESP-IDF toolchain "
               "(about 2 GB into the '%s' volume). The first build can take 10-15 minutes "
               "on a Pi 5 (estimate).")


def default_stream_runner(argv: list[str]) -> int:
    """Run a command with its output going straight to the terminal; return its status."""
    return subprocess.run(argv, check=False, stdin=subprocess.DEVNULL).returncode


# --- validation -----------------------------------------------------------------

def validate_serial(device: str, lstat=os.lstat, realpath=os.path.realpath) -> str:
    """Return the real character device for ``device`` or raise RenderError (code 2).

    Accepts an absolute, normalised path under /dev that is a character device named
    ``/dev/tty*``, or a symlink below ``/dev/serial/by-id/`` that resolves to one.
    Any other symlink is refused.
    """
    if (not isinstance(device, str) or "\0" in device or not device.startswith("/dev/")
            or os.path.normpath(device) != device):
        raise RenderError("--serial must be a normalised absolute path under /dev, for example "
                          "/dev/ttyACM0 or /dev/serial/by-id/usb-...", 2)
    try:
        info = lstat(device)
    except OSError as exc:
        raise RenderError(f"cannot use serial device {device}: {exc.strerror}", 2) from exc
    target = device
    if stat.S_ISLNK(info.st_mode):
        if not device.startswith(SERIAL_BY_ID_PREFIX):
            raise RenderError(f"refusing serial device {device}: it is a symbolic link "
                              f"(only {SERIAL_BY_ID_PREFIX}* links are accepted)", 2)
        target = realpath(device)
        if not SERIAL_REAL_RE.fullmatch(target):
            raise RenderError(f"refusing serial device {device}: it resolves to {target}, "
                              "not a /dev/tty* device", 2)
        try:
            info = lstat(target)
        except OSError as exc:
            raise RenderError(f"cannot use serial device {target}: {exc.strerror}", 2) from exc
        if stat.S_ISLNK(info.st_mode):
            raise RenderError(f"refusing serial device {target}: it is a symbolic link", 2)
    elif not SERIAL_REAL_RE.fullmatch(device):
        raise RenderError(f"refusing serial device {device}: the name must be /dev/tty*", 2)
    if not stat.S_ISCHR(info.st_mode):
        raise RenderError(f"refusing serial device {target}: it is not a character device", 2)
    return target


def validate_host(host: str) -> str:
    """An IP address or an RFC 1123 host name; never a URL, path or option."""
    if not isinstance(host, str) or not host or len(host) > 253:
        raise RenderError("--ota needs an IP address or host name", 2)
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = host.rstrip(".").split(".")
    if (not all(HOST_LABEL_RE.fullmatch(label) for label in labels)
            or labels[-1].isdigit()):
        raise RenderError(f"--ota {host!r} is not an IP address or a host name "
                          "(letters, digits and hyphens in dot-separated labels; no URL)", 2)
    if host.casefold() in RESERVED_TARGETS or host.startswith("COM"):
        raise RenderError(f"--ota {host!r} is reserved by ESPHome; use the display's IP address", 2)
    return host


# --- guarded directory access -----------------------------------------------------

def _enforce_owner() -> bool:
    return render._enforce_owner()


def _open_existing(parent_fd: int, name: str, label: str) -> int:
    """Open an existing subdirectory without following symlinks (root-owned when root)."""
    try:
        info = os.lstat(name, dir_fd=parent_fd)
    except FileNotFoundError:
        raise RenderError(f"{label} does not exist") from None
    if stat.S_ISLNK(info.st_mode):
        raise RenderError(f"refusing to use {label}: it is a symbolic link")
    if not stat.S_ISDIR(info.st_mode):
        raise RenderError(f"refusing to use {label}: it is not a directory")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise RenderError(f"refusing to use {label}: {exc.strerror}") from exc
    now = os.fstat(fd)
    if (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino):
        os.close(fd)
        raise RenderError(f"{label} changed while it was being opened")
    if _enforce_owner() and now.st_uid != 0:
        os.close(fd)
        raise RenderError(f"refusing to use {label}: not owned by root (uid {now.st_uid})")
    return fd


class DisplayDir:
    """``$ROOT/build/displays/<id>`` as no-follow directory descriptors."""

    def __init__(self, root: Path, display_id: str, create: bool):
        self.root = Path(root)
        self.display_id = display_id
        self.create = create
        self.fds: list[int] = []
        self.parent_fd = -1
        self.display_fd = -1

    @property
    def path(self) -> Path:
        return self.root / render.BUILD_DIR_NAME / "displays" / self.display_id

    def __enter__(self):
        try:
            root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        except OSError as exc:
            raise RenderError(f"cannot open {self.root}: {exc.strerror}") from exc
        self.fds.append(root_fd)
        try:
            opener = render._open_dir if self.create else _open_existing
            build_label = str(self.root / render.BUILD_DIR_NAME)
            try:
                build_fd = opener(root_fd, render.BUILD_DIR_NAME, build_label)
            except RenderError as exc:
                if self.create:
                    raise
                raise RenderError(f"{build_label} does not exist; run "
                                  f"'vesselstackctl display build {self.display_id}' first") from exc
            self.fds.append(build_fd)
            self.parent_fd = opener(build_fd, "displays", build_label + "/displays")
            self.fds.append(self.parent_fd)
            try:
                self.display_fd = opener(self.parent_fd, self.display_id, str(self.path))
            except RenderError as exc:
                if self.create:
                    raise
                raise RenderError(f"{self.path} does not exist; run "
                                  f"'vesselstackctl display build {self.display_id}' first") from exc
            self.fds.append(self.display_fd)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        for fd in reversed(self.fds):
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds = []
        return False


def _read_nofollow(parent_fd: int, name: str, label: str) -> bytes:
    try:
        data = mqtt_auth._read_regular(name, parent_fd)
    except (OSError, mqtt_auth.MqttAuthError) as exc:
        raise RenderError(f"refusing to read {label}: {getattr(exc, 'strerror', None) or exc}") from exc
    if data is None:
        raise RenderError(f"{label} is missing")
    return data


def _read_at(display_fd: int, relative: list[str], label: str) -> bytes:
    """Read a regular file below ``display_fd`` following no symlink at any step."""
    opened = []
    try:
        current = display_fd
        for part in relative[:-1]:
            current = _open_existing(current, part, f"{label} ({part})")
            opened.append(current)
        return _read_nofollow(current, relative[-1], label)
    finally:
        for fd in opened:
            os.close(fd)


# --- helpers -----------------------------------------------------------------------

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def image_digest(image: str) -> str:
    return image.split("@", 1)[1]


def source_hash(files: dict, display_id: str) -> str:
    """sha256 over the rendered project, secrets and the README excluded."""
    digest = hashlib.sha256()
    for path in sorted(files):
        relative = path[len(display_id) + 1:]
        if relative in SOURCE_EXCLUDED:
            continue
        digest.update(relative.encode("utf-8") + b"\0" + sha256_hex(files[path]).encode("ascii") + b"\n")
    return digest.hexdigest()


def secrets_fingerprint(files: dict, display_id: str) -> str:
    """sha256 of the rendered secrets.yaml (Wi-Fi, MQTT, OTA and API secrets, MQTT host)."""
    return sha256_hex(files[f"{display_id}/secrets.yaml"])


def vesselstack_version(root: Path, env: dict | None = None) -> str:
    candidates = []
    if env and env.get("VESSELSTACK_VERSION"):
        return env["VESSELSTACK_VERSION"].strip()
    candidates = [Path(root) / "installer" / "VERSION", Path(root) / "VERSION",
                  render.MODULE_DIR.parent / "VERSION"]
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    return "unknown"


def _docker(ctx, argv: list[str]):
    try:
        return ctx.runner(argv)
    except FileNotFoundError:
        raise RenderError("docker is not installed or not on PATH") from None


def _image_present(ctx, image: str) -> bool:
    return _docker(ctx, ["docker", "image", "inspect", image]).returncode == 0


def _volume_present(ctx, name: str = CACHE_VOLUME) -> bool:
    return _docker(ctx, ["docker", "volume", "inspect", name]).returncode == 0


def _check_mount_path(path: Path) -> str:
    text = str(path)
    if ":" in text or "," in text or "\n" in text:
        raise RenderError(f"{text} contains a character Docker cannot mount safely (: , or a "
                          "line break); use a different VESSELSTACK_ROOT")
    return text


def docker_root_dir(ctx) -> str | None:
    result = _docker(ctx, ["docker", "info", "--format", "{{.DockerRootDir}}"])
    if result.returncode != 0:
        return None
    text = (result.stdout or "").strip()
    return text if text.startswith("/") else None


def free_bytes(ctx, statvfs=os.statvfs) -> tuple[int, str]:
    """Free bytes where Docker stores data (best effort) and the path that was measured."""
    candidates = []
    reported = docker_root_dir(ctx)
    if reported:
        candidates.append(reported)
    candidates.append(DOCKER_ROOT_FALLBACK)
    candidates.append(str(ctx.root))  # last resort: the build tree's own filesystem
    for path in candidates:
        try:
            info = statvfs(path)
        except OSError:
            continue
        return info.f_bavail * info.f_frsize, path
    raise RenderError("cannot determine the free disk space; refusing to build")


def preflight_disk(ctx, cache_present: bool, statvfs=os.statvfs) -> None:
    needed = MIN_FREE_WARM_BUILD if cache_present else MIN_FREE_FIRST_BUILD
    free, where = free_bytes(ctx, statvfs)
    if free < needed:
        what = ("the toolchain cache is missing, so the first build downloads about 2 GB"
                if not cache_present else "each build needs about 600 MB of scratch space")
        raise RenderError(
            f"not enough free disk space on {where}: {free / GIB:.1f} GB free, "
            f"{needed / GIB:.0f} GB needed ({what}). Free some space, or build on a laptop with "
            "'vesselstackctl display export' instead")


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _manifest_bytes(manifest: dict) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


# --- build -------------------------------------------------------------------------

def _write_firmware_dir(dd: DisplayDir, files: dict, owner, rotate: bool = True) -> None:
    """Write ``firmware.new`` then rotate: firmware.prev <- firmware, firmware <- new.

    With ``rotate=False`` the current ``firmware/`` is discarded instead of becoming
    ``firmware.prev`` (used when it was never flashed, so ``firmware.prev`` stays the image
    the device last ran).
    """
    parent = dd.display_fd
    render._remove_tree(parent, NEW_DIR)
    new_fd = render._open_dir(parent, NEW_DIR, f"{dd.path}/{NEW_DIR}")
    try:
        for name in sorted(files):
            mqtt_auth._write_new(name, files[name], new_fd, render.FILE_MODE, owner)
        os.fsync(new_fd)
    except BaseException:
        os.close(new_fd)
        render._remove_tree(parent, NEW_DIR)
        raise
    os.close(new_fd)
    if rotate:
        render._remove_tree(parent, PREVIOUS_DIR)
        if render._exists(parent, FIRMWARE_DIR):
            os.rename(FIRMWARE_DIR, PREVIOUS_DIR, src_dir_fd=parent, dst_dir_fd=parent)
    else:
        render._remove_tree(parent, FIRMWARE_DIR)
    os.rename(NEW_DIR, FIRMWARE_DIR, src_dir_fd=parent, dst_dir_fd=parent)


def build(ctx, display_id: str, *, warn=None, **kw) -> Path:
    """Render, compile in a one-shot container and store the firmware with a manifest.

    Holds the per-display build lock from the render to the end of the rotation, so a
    concurrent render, export, build or flash of the same display cannot delete, rename
    or half-read what this build is working on.
    """
    lifecycle._require_root(ctx, "build")
    warn = warn or (lambda text: print(text, file=sys.stderr))
    with render.build_lock(ctx, display_id, notify=warn):
        return _build_locked(ctx, display_id, warn=warn, **kw)


def _build_locked(ctx, display_id: str, *, stream=default_stream_runner, statvfs=os.statvfs,
                  monotonic=time.monotonic, now=_utc_now, warn, rendered=None, rotate=True) -> Path:
    image = render.pinned_image()
    if rendered is None:
        rendered = render.collect(ctx, display_id)  # fails when Wi-Fi is not configured
    for message in rendered.warnings:
        warn(f"warning: {message}")
    source = source_hash(rendered.files, display_id)
    device_name = render.firmware_name(display_id)
    cache_present = _volume_present(ctx)
    image_present = _image_present(ctx, image)
    preflight_disk(ctx, cache_present, statvfs)
    project = render.write_project(ctx, rendered)
    mount = _check_mount_path(project)
    container_dir = project.parent
    if not image_present:
        ctx.out("note: " + PULL_NOTICE % CACHE_VOLUME)
    elif not cache_present:
        ctx.out(f"note: the '{CACHE_VOLUME}' volume does not exist yet: the first build downloads the "
                "ESP-IDF toolchain (about 2 GB); estimated 10-15 minutes on a Pi 5")
    argv = ["docker", "run", "--rm", "--pull", "missing", *CONTAINER_FLAGS,
            "-v", f"{mount}:/config", "-v", f"{CACHE_VOLUME}:/cache",
            image, "compile", f"/config/{display_id}.yaml"]
    ctx.out(f"Compiling {display_id} with {image.split('@', 1)[0]} (output follows)")
    started = monotonic()
    status = stream(argv)
    elapsed = monotonic() - started
    if status != 0:
        raise RenderError(f"the ESPHome compile failed (exit status {status}) after "
                          f"{elapsed:.0f} s; no firmware was stored. Scroll up for the compiler output")
    owner = mqtt_auth._owner((0, 0))
    with DisplayDir(ctx.root, display_id, create=False) as dd:
        artifacts = {}
        for name in ARTIFACTS:
            relative = [render.PROJECT_DIR, ".esphome", "build", device_name, "build", name]
            try:
                artifacts[name] = _read_at(dd.display_fd, relative, "/".join(relative))
            except RenderError as exc:
                raise RenderError(f"the compile succeeded but did not produce {name}: {exc}") from exc
            if not artifacts[name]:
                raise RenderError(f"the compile produced an empty {name}")
        built = now().astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest = {
            "schema": 1,
            "display": display_id,
            "device_name": device_name,
            "image": image,
            "image_digest": image_digest(image),
            "esphome_version": render.esphome_version(),
            "vesselstack_version": vesselstack_version(ctx.root, ctx.env),
            "source_sha256": source,
            # Fingerprint of the secrets.yaml compiled in (never the secrets themselves):
            # flash refuses firmware whose credentials are no longer the current ones.
            "secrets_sha256": secrets_fingerprint(rendered.files, display_id),
            "built_at_utc": built,
            "files": {name: {"sha256": sha256_hex(data), "size": len(data)}
                      for name, data in artifacts.items()},
        }
        files = dict(artifacts)
        files[MANIFEST_NAME] = _manifest_bytes(manifest)
        _write_firmware_dir(dd, files, owner, rotate)
        try:  # the ~600 MB scratch tree is no longer needed
            project_fd = _open_existing(dd.display_fd, render.PROJECT_DIR, str(project))
            try:
                render._remove_tree(project_fd, ".esphome")
            finally:
                os.close(project_fd)
        except (OSError, RenderError) as exc:
            warn(f"warning: could not remove the build scratch tree .esphome: {exc}")
    ctx.out(f"Firmware stored in {container_dir / FIRMWARE_DIR} (built in {elapsed:.0f} s)")
    ctx.out(f"  {FACTORY_NAME}  sha256 {manifest['files'][FACTORY_NAME]['sha256']}")
    ctx.out(f"  {OTA_NAME}      sha256 {manifest['files'][OTA_NAME]['sha256']}")
    ctx.out("Flash it with 'vesselstackctl display flash %s --serial /dev/serial/by-id/... "
            "| --ota HOST'" % display_id)
    return container_dir / FIRMWARE_DIR


# --- flash -------------------------------------------------------------------------

def load_firmware(dd: DisplayDir, directory: str, name: str) -> tuple[bytes, dict]:
    """Read ``name`` from the firmware directory and verify it against manifest.json."""
    label = f"{dd.path}/{directory}"
    try:
        fw_fd = _open_existing(dd.display_fd, directory, label)
    except RenderError as exc:
        if directory == PREVIOUS_DIR:
            raise RenderError("there is no previous firmware to roll back to") from exc
        raise RenderError(f"no stored firmware for {dd.display_id}: run "
                          f"'vesselstackctl display build {dd.display_id}' first") from exc
    try:
        try:
            manifest = json.loads(_read_nofollow(fw_fd, MANIFEST_NAME, f"{label}/{MANIFEST_NAME}"))
        except ValueError as exc:
            raise RenderError(f"{label}/{MANIFEST_NAME} is not valid JSON; run "
                              f"'vesselstackctl display build {dd.display_id}' again") from exc
        entry = (manifest.get("files") or {}).get(name) if isinstance(manifest, dict) else None
        if (not isinstance(entry, dict) or not SHA256_RE.fullmatch(str(entry.get("sha256", "")))
                or not isinstance(entry.get("size"), int)):
            raise RenderError(f"{label}/{MANIFEST_NAME} has no valid entry for {name}")
        data = _read_nofollow(fw_fd, name, f"{label}/{name}")
    finally:
        os.close(fw_fd)
    if len(data) != entry["size"] or sha256_hex(data) != entry["sha256"]:
        raise RenderError(f"{label}/{name} does not match its recorded sha256/size in "
                          f"{MANIFEST_NAME}; refusing to flash. Run "
                          f"'vesselstackctl display build {dd.display_id}' again")
    return data, manifest


FLASH_STAGED_NAME = "fw.bin"
# `esphome upload` wants to write .esphome/ (storage sidecar) and a platformio cache. The
# project is mounted read-only, so give it throwaway tmpfs locations instead. Verified with
# the pinned image: an OTA attempt against an unreachable host runs exactly as with a
# writable /config, without the "Read-only file system" warnings.
UPLOAD_SCRATCH_FLAGS = ["--tmpfs", "/tmp", "--tmpfs", "/cache",
                        "-e", "ESPHOME_DATA_DIR=/tmp/esphome-data"]


STAGED_CONFIG_NAME = "config"


@contextlib.contextmanager
def staged_firmware(ctx, data: bytes, expected_sha256: str, rendered=None):
    """Copy verified firmware bytes into a fresh root-only temp dir; yield (file, config dir).

    The container reads this copy, not the stored file, so nothing can swap the bytes
    between verification and upload. The temp dir lives under ``$ROOT/build/`` (root-only),
    not in a shared /tmp, and is removed afterwards.

    With ``rendered`` the freshly re-rendered project is written to ``config/`` beside the
    firmware copy (also 0700/0600, no-follow) and its path is the second yielded value
    (None without ``rendered``). ``project/`` is never trusted for flashing: the compile
    container could write to it.
    """
    build_dir = ctx.root / render.BUILD_DIR_NAME
    tmp = Path(tempfile.mkdtemp(prefix=".flash-", dir=build_dir))
    try:
        os.chmod(tmp, 0o700)
        staged = tmp / FLASH_STAGED_NAME
        config_path = None
        fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            mqtt_auth._write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        fd = os.open(staged, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            copy = b"".join(iter(lambda: os.read(fd, 65536), b""))
        finally:
            os.close(fd)
        if sha256_hex(copy) != expected_sha256:
            raise RenderError("the staged firmware copy does not match its recorded sha256; "
                              "refusing to flash")
        _check_mount_path(staged)
        if rendered is not None:
            config = tmp / STAGED_CONFIG_NAME
            os.mkdir(config, 0o700)
            top_fd = os.open(config, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                render._write_files(top_fd, rendered.files, rendered.display_id,
                                    mqtt_auth._owner((0, 0)))
            finally:
                os.close(top_fd)
            config_path = _check_mount_path(config)
        yield staged, config_path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def flash(ctx, display_id: str, *, serial: str | None = None, ota: str | None = None,
          previous: bool = False, stream=default_stream_runner, lstat=os.lstat,
          realpath=os.path.realpath, notify=None, force: bool = False) -> None:
    """Upload the stored firmware; never compiles and never pulls an image.

    Holds the per-display build lock for the whole run, so a build cannot rotate the
    firmware directories while the bytes are being verified, copied and uploaded.
    """
    lifecycle._require_root(ctx, "flash")
    if bool(serial) == bool(ota):
        raise RenderError("choose exactly one of --serial DEVICE or --ota HOST", 2)
    device = validate_serial(serial, lstat, realpath) if serial else None
    host = validate_host(ota) if ota else None
    with render.build_lock(ctx, display_id, notify=notify):
        info = _flash_locked(ctx, display_id, device=device, host=host, previous=previous,
                             stream=stream, serial=serial, force=force)
        record_flash(ctx, display_id, info, "serial" if serial else "ota", device or host,
                     warn=notify or (lambda text: print(text, file=sys.stderr)))


def check_credentials(manifest: dict, display_id: str, previous: bool, current, reason=None) -> None:
    """Refuse firmware that embeds credentials the Pi no longer uses.

    The source hash deliberately excludes secrets.yaml, so after ``rotate``, ``wifi`` or a
    remove-then-add the stored images still carry the old MQTT/OTA/Wi-Fi secrets and the
    device would never connect. Compare the fingerprint recorded at build time with
    ``current``, what the current credentials render to (None when they cannot be
    rendered, with ``reason``).
    """
    recorded = manifest.get("secrets_sha256")
    if recorded and current and recorded == current:
        return
    if recorded is None:
        why = "it was built before credentials were tracked in the manifest"
    elif current is None:
        why = f"the current credentials cannot be rendered ({reason})"
    else:
        why = ("the credentials changed since it was built (display rotate, display wifi, or "
               "the display was removed and added again)")
    if previous:
        raise RenderError(
            f"refusing to flash the rollback firmware: {why}. This older build may carry "
            "credentials that are revoked or outdated; the device would run it but fail to "
            "join Wi-Fi or be refused by the broker. Pass --force only if that is what you "
            "want (for example you restored the old credentials)")
    raise RenderError(
        f"refusing to flash: {why}, so this firmware would not connect or accept updates. "
        f"Run 'vesselstackctl display build {display_id}' first, or pass --force to flash it anyway")


def check_source(manifest: dict, display_id: str, previous: bool, current: str) -> None:
    """Refuse to pair firmware with a configuration other than the one it was built from."""
    if manifest.get("source_sha256") == current:
        return
    which = "the rollback firmware was built from a different configuration, and " if previous else ""
    raise RenderError(
        f"refusing to flash: configuration changed since this firmware was built; run "
        f"'vesselstackctl display build {display_id}' ({which}the upload would use today's "
        "project, not the one that was compiled). Pass --force to flash it anyway")


def _flash_locked(ctx, display_id: str, *, device, host, previous: bool, stream, serial,
                  force: bool = False) -> dict:
    image = render.pinned_image()
    directory = PREVIOUS_DIR if previous else FIRMWARE_DIR
    name = FACTORY_NAME if serial else OTA_NAME
    with DisplayDir(ctx.root, display_id, create=False) as dd:
        if render._exists(dd.display_fd, f"{display_id}.yaml"):
            raise RenderError(f"{dd.path} still has the old directory layout: run "
                              f"'vesselstackctl display render {display_id}' once to migrate it "
                              "(stored firmware is kept)")
        data, manifest = load_firmware(dd, directory, name)
    # project/ was writable by the compile container, so it is never mounted for the
    # upload: re-render the project from displays.json + registry + secrets (deterministic)
    # into a fresh private staging dir and mount that instead.
    reason = None
    rendered = None
    try:
        rendered = render.collect(ctx, display_id)  # may generate a missing OTA password
    except (RenderError, lifecycle.LifecycleError) as exc:
        reason = str(exc)
    if not force:
        current = secrets_fingerprint(rendered.files, display_id) if rendered else None
        check_credentials(manifest, display_id, previous, current, reason)
        check_source(manifest, display_id, previous, source_hash(rendered.files, display_id))
    elif rendered is None:
        raise RenderError(f"cannot render the project to flash from: {reason}")
    if not _image_present(ctx, image):
        raise RenderError(f"the ESPHome image {image.split('@', 1)[0]} is not present locally and "
                          "flashing never pulls it; run "
                          f"'vesselstackctl display build {display_id}' first")
    label = "rollback firmware" if previous else "firmware"
    with staged_firmware(ctx, data, manifest["files"][name]["sha256"], rendered) as (staged, config):
        argv = ["docker", "run", "--rm", "--pull", "never", *CONTAINER_FLAGS, *UPLOAD_SCRATCH_FLAGS]
        if device:  # USB: the device is passed in; the container needs no network at all
            argv += ["--network", "none", "--device", device]
        else:  # OTA must reach the display on the LAN
            argv += ["--network", "host"]
        argv += ["-v", f"{config}:/config:ro", "-v", f"{staged}:/fw.bin:ro",
                 image, "upload", f"/config/{display_id}.yaml",
                 "--device", device or host, "--file", "/fw.bin"]
        ctx.out(f"Flashing {label} built {manifest.get('built_at_utc', 'at an unknown time')} "
                f"({name}, sha256 {manifest['files'][name]['sha256'][:16]}...) to "
                f"{device or host} {'over USB serial' if device else 'over the air'}")
        status = stream(argv)
    if status != 0:
        hint = ("Check the cable and that the board is in download mode (hold BOOT, tap RESET)."
                if device else
                "Check that the display is online and reachable on the LAN. After 'display rotate' "
                "and a re-render or rebuild the project carries a new OTA password that the "
                "device does not know yet: flash over USB instead.")
        raise RenderError(f"the upload failed (exit status {status}). {hint}")
    ctx.out("Upload finished")
    return {"image_sha256": manifest["files"][name]["sha256"],
            "source_sha256": manifest.get("source_sha256"),
            "secrets_sha256": manifest.get("secrets_sha256"),
            "built_at_utc": manifest.get("built_at_utc"),
            "version": manifest.get("vesselstack_version")}


# --- last-flash.json -----------------------------------------------------------------

LAST_FLASH_NAME = "last-flash.json"
LAST_FLASH_ENTRY_KEYS = ("method", "target", "image_sha256", "source_sha256", "secrets_sha256",
                         "at", "version", "built_at_utc")


def _clean_flash_entry(entry) -> dict | None:
    """One ``current``/``previous`` record with only the known string fields, or None."""
    if not isinstance(entry, dict) or entry.get("method") not in ("serial", "ota"):
        return None
    if not SHA256_RE.fullmatch(str(entry.get("image_sha256", ""))):
        return None
    return {key: entry[key] for key in LAST_FLASH_ENTRY_KEYS
            if isinstance(entry.get(key), str) and len(entry[key]) <= 300}


def read_last_flash(dd: DisplayDir) -> dict | None:
    """``{current, previous}`` from last-flash.json, or None when absent or unusable."""
    try:
        data = json.loads(_read_at(dd.display_fd, [LAST_FLASH_NAME], str(dd.path)))
    except (RenderError, ValueError, OSError):
        return None
    current = _clean_flash_entry(data.get("current")) if isinstance(data, dict) else None
    if current is None:
        return None
    return {"current": current, "previous": _clean_flash_entry(data.get("previous"))}


def firmware_image_hashes(dd: DisplayDir, directory: str) -> set:
    """The recorded image sha256 values of firmware/ or firmware.prev/ (empty when unreadable)."""
    try:
        manifest = json.loads(_read_at(dd.display_fd, [directory, MANIFEST_NAME], str(dd.path)))
        files = manifest.get("files") or {}
        return {files[name]["sha256"] for name in ARTIFACTS
                if isinstance(files.get(name), dict) and SHA256_RE.fullmatch(str(files[name].get("sha256")))}
    except (RenderError, ValueError, OSError, AttributeError):
        return set()


def record_flash(ctx, display_id: str, info: dict, method: str, target: str, *, warn,
                 now=_utc_now) -> None:
    """Record a SUCCESSFUL upload in last-flash.json (root 0600, atomic, no-follow).

    The caller holds the build lock. A failed upload records nothing. The previous
    ``current`` becomes ``previous`` unless the same image is flashed again. A write
    failure is a warning, not an error: the device already runs the new image.
    """
    try:
        with DisplayDir(ctx.root, display_id, create=False) as dd:
            existing = read_last_flash(dd)
            entry = {"method": method, "target": target, "image_sha256": info["image_sha256"],
                     "source_sha256": info.get("source_sha256") or "",
                     "secrets_sha256": info.get("secrets_sha256") or "",
                     "at": now().astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "version": info.get("version") or "unknown",
                     "built_at_utc": info.get("built_at_utc") or ""}
            entry = {key: value for key, value in entry.items() if value != ""}
            previous = None
            if existing:
                if existing["current"]["image_sha256"] == entry["image_sha256"]:
                    previous = existing["previous"]
                else:
                    previous = existing["current"]
            data = (json.dumps({"schema": 1, "current": entry, "previous": previous},
                               indent=2, sort_keys=True) + "\n").encode("utf-8")
            tmp = LAST_FLASH_NAME + ".tmp"
            mqtt_auth._write_new(tmp, data, dd.display_fd, render.FILE_MODE, mqtt_auth._owner((0, 0)))
            os.rename(tmp, LAST_FLASH_NAME, src_dir_fd=dd.display_fd, dst_dir_fd=dd.display_fd)
    except (RenderError, OSError, mqtt_auth.MqttAuthError) as exc:
        warn(f"warning: the upload worked but {LAST_FLASH_NAME} could not be written: "
             f"{getattr(exc, 'strerror', None) or exc}")


# --- deploy (build if needed + upload, one lock) -------------------------------------

REDACTED = "[redacted]"
TAIL_LINES = 200


def make_redactor(values):
    """Return ``redact(text)`` replacing every secret value (and its JSON/URL/shell-quoted
    spelling) with ``[redacted]``, longest first."""
    import shlex
    import urllib.parse
    spellings = set()
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        spellings.add(value)
        spellings.add(json.dumps(value)[1:-1])
        spellings.add(urllib.parse.quote(value, safe=""))
        quoted = shlex.quote(value)
        if quoted.startswith("'") and quoted.endswith("'"):
            spellings.add(quoted[1:-1])
    ordered = sorted((v for v in spellings if v), key=len, reverse=True)

    def redact(text: str) -> str:
        for value in ordered:
            text = text.replace(value, REDACTED)
        return text
    return redact


def default_line_runner(argv: list[str], emit, popen=subprocess.Popen) -> int:
    """Run ``argv`` and hand every output line (stdout and stderr merged) to ``emit``."""
    proc = popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for raw in iter(proc.stdout.readline, b""):
            for part in re.split(r"\r+|\n", raw.decode("utf-8", "replace")):
                if part:
                    emit(part)
    finally:
        proc.stdout.close()
    return proc.wait()


def fix_hints(message: str, output: str, method: str, display_id: str) -> list[str]:
    """Plain-language 'To fix' lines for a known failure (never contains a secret)."""
    text = (message + "\n" + output).lower()
    usb = (f"sudo vesselstackctl display deploy {display_id} --serial /dev/serial/by-id/usb-... "
           "(plug the display into the Pi with a data USB cable)")
    if "another build/flash" in text:
        return ["Another build or upload for this display is running. Wait for it to finish, then try again."]
    if "docker is not installed" in text:
        return ["Docker is not installed on this device. Install Docker (see the README), then try again."]
    if "not enough free disk space" in text:
        return ["Free some disk space (the first build needs about 3 GB free, later builds 1 GB), then try again."]
    if "no wi-fi credentials" in text:
        return ["Store the Wi-Fi network first: sudo vesselstackctl display wifi"]
    if "no mqtt password" in text:
        return ["Create the display's MQTT password first: sudo vesselstackctl display sync-mqtt"]
    if "unable to find image" in text or "pull access denied" in text or (
            "pull" in text and ("no such host" in text or "dial tcp" in text or "timeout" in text)):
        return ["The ESPHome image could not be downloaded (about 250 MB). Connect this device to the "
                "internet and try again."]
    if "not present locally" in text:
        return [f"The ESPHome image is missing. Connect this device to the internet and run Apply again "
                f"(or: sudo vesselstackctl display build {display_id})."]
    if "the esphome compile failed" in text:
        return ["The firmware did not compile. Open Details for the compiler output and try again; if it keeps "
                "failing, report the output (secrets are hidden)."]
    if method not in ("ota", "serial") or "the upload failed" not in text:
        return []
    seen = output.lower()  # the tool's own output; the error message above carries generic advice
    if method == "ota":
        if "authenticat" in seen:
            return ["The display refused the update because its stored credentials differ (they were changed "
                    f"after it was last flashed). Flash it over USB once: {usb}"]
        if "not enough space" in seen or "no space left" in seen or "too large" in seen:
            return [f"The display has no room for this firmware over Wi-Fi. Flash it over USB: {usb}"]
        return ["Check that the display is powered on and connected to Wi-Fi, then try again. If you used "
                "a name ending in .local, try the display's IP address instead.",
                f"The first flash of a new display must be over USB: {usb}"]
    return ["Check the USB cable (it must carry data, not only power) and that the board is in download "
            "mode (hold BOOT, tap RESET), and that no other program is using the port, then try again."]


def deploy(ctx, display_id: str, *, serial: str | None = None, ota: str | None = None,
           progress: bool = False, run_lines=default_line_runner, statvfs=os.statvfs,
           monotonic=time.monotonic, now=_utc_now, lstat=os.lstat, realpath=os.path.realpath,
           notify=None) -> None:
    """Build when needed and upload, holding ONE build lock for the whole run.

    Stages (printed as ``STAGE: name`` with ``progress``): render, compile, upload, done.
    The compile is skipped when the stored firmware already matches the current source and
    credentials (so a retry after a failed upload does not rebuild). A build replaces
    ``firmware/`` without rotating it to ``firmware.prev`` when ``firmware/`` was never
    flashed, so ``firmware.prev`` stays the image the device last ran. Everything the
    ESPHome tools print is redacted of secret values before it is shown.
    """
    lifecycle._require_root(ctx, "deploy")
    if bool(serial) == bool(ota):
        raise RenderError("choose exactly one of --serial DEVICE or --ota HOST", 2)
    device = validate_serial(serial, lstat, realpath) if serial else None
    host = validate_host(ota) if ota else None
    method = "serial" if serial else "ota"
    warn = notify or (lambda text: print(text, file=sys.stderr, flush=True))
    tail: list = []
    redact = make_redactor([])
    say_out = ctx.out

    def say(text: str) -> None:
        say_out(text)
        try:
            sys.stdout.flush()
        except (OSError, ValueError):
            pass

    def stage(name: str) -> None:
        if progress:
            say(f"STAGE: {name}")

    def emit(line: str) -> None:
        line = redact(line)
        tail.append(line)
        del tail[:-TAIL_LINES]
        say(line)

    def stream(argv: list[str]) -> int:
        return run_lines(argv, emit)

    ctx = _with_out(ctx, say)
    try:
        with render.build_lock(ctx, display_id, notify=warn):
            stage("render")
            rendered = render.collect(ctx, display_id)
            redact = make_redactor(rendered.secret_values)
            source = source_hash(rendered.files, display_id)
            fingerprint = secrets_fingerprint(rendered.files, display_id)
            skip, rotate = _plan_build(ctx, display_id, source, fingerprint)
            stage("compile")
            if skip:
                say("note: the stored firmware already matches the current settings and credentials; "
                    "skipping the compile")
            else:
                _build_locked(ctx, display_id, stream=stream, statvfs=statvfs, monotonic=monotonic,
                              now=now, warn=warn, rendered=rendered, rotate=rotate)
            stage("upload")
            info = _flash_locked(ctx, display_id, device=device, host=host, previous=False,
                                 stream=stream, serial=serial)
            record_flash(ctx, display_id, info, method, device or host, warn=warn, now=now)
            stage("done")
            say(f"Deployed to {device or host} ({'USB serial' if device else 'Wi-Fi'}).")
    except (RenderError, lifecycle.LifecycleError) as exc:
        for hint in fix_hints(str(exc), "\n".join(tail), method, display_id):
            say(f"To fix: {hint}")
        raise


def _with_out(ctx, out):
    import dataclasses
    return dataclasses.replace(ctx, out=out)


def _plan_build(ctx, display_id: str, source: str, fingerprint: str) -> tuple[bool, bool]:
    """``(skip_compile, rotate)`` for a deploy, read under the build lock.

    Skip when firmware/ is intact, built from this source and these credentials, and the
    pinned image is present. Rotate firmware/ into firmware.prev unless last-flash.json
    exists and shows that the device does not run firmware/ (it was never flashed).
    """
    try:
        with DisplayDir(ctx.root, display_id, create=False) as dd:
            last = read_last_flash(dd)
            hashes = firmware_image_hashes(dd, FIRMWARE_DIR)
            skip = False
            if hashes:
                try:
                    _data, manifest = load_firmware(dd, FIRMWARE_DIR, OTA_NAME)
                    load_firmware(dd, FIRMWARE_DIR, FACTORY_NAME)
                    skip = (manifest.get("source_sha256") == source
                            and manifest.get("secrets_sha256") == fingerprint)
                except RenderError:
                    skip = False
            rotate = True
            if last is not None and hashes:
                rotate = last["current"]["image_sha256"] in hashes
    except RenderError:
        return False, True
    if skip and not _image_present(ctx, render.pinned_image()):
        skip = False
    return skip, rotate
