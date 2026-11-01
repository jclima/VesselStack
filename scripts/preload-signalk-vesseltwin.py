#!/usr/bin/env python3
"""Safely preload the pinned VesselTwin Signal K plugin for managed servers."""
from __future__ import annotations
import argparse, base64, ctypes, errno, hashlib, io, json, os, shutil, stat, sys, tarfile, tempfile, urllib.request
from pathlib import Path

URL = "https://registry.npmjs.org/signalk-vesseltwin/-/signalk-vesseltwin-0.2.1.tgz"
SHA512 = "VDrKGAtLUuk4kYhS2O03nfONKzKgMb/443eS/rQkLqSpCLv0HONb8UiHR+H9gV7xRoq0eX+rhrQwU8k/AcPvrg=="
NAME, VERSION = "signalk-vesseltwin", "0.2.1"
MAX_ARCHIVE, MAX_UNPACKED, MAX_MEMBERS = 2 * 1024 * 1024, 8 * 1024 * 1024, 2000

def fail(message: str) -> "NoReturn":
    print(f"VesselTwin preload: {message}", file=sys.stderr); raise SystemExit(1)

def checked(raw: bytes) -> bytes:
    if len(raw) > MAX_ARCHIVE: fail("archive exceeds the 2 MiB safety limit")
    if base64.b64encode(hashlib.sha512(raw).digest()).decode() != SHA512:
        fail("archive integrity check failed (expected the immutable 0.2.1 pin)")
    return raw

def validate(raw: bytes) -> list[tuple[str, bytes, bool]]:
    checked(raw); entries, seen, unpacked = [], set(), 0
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            members = tf.getmembers()
            if len(members) > MAX_MEMBERS: fail("archive contains too many members")
            for member in members:
                name, parts = member.name, Path(member.name).parts
                normalized = name.removesuffix("/")
                if ((normalized != "package" and not name.startswith("package/"))
                        or Path(name).is_absolute() or ".." in parts
                        or any(p in ("", ".") for p in parts)):
                    fail(f"archive contains an unsafe member: {name}")
                if normalized in seen: fail(f"archive contains a duplicate member: {name}")
                seen.add(normalized)
                if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                    fail(f"archive contains a link or special file: {name}")
                if member.isdir(): entries.append((normalized, b"", True)); continue
                if member.size < 0 or unpacked + member.size > MAX_UNPACKED:
                    fail("archive unpacked contents exceed the 8 MiB safety limit")
                source = tf.extractfile(member)
                if source is None: fail(f"archive member cannot be read: {name}")
                data = source.read(member.size + 1)
                if len(data) != member.size: fail(f"archive member has an invalid size: {name}")
                unpacked += len(data); entries.append((name, data, False))
    except (tarfile.TarError, OSError) as exc: fail(f"invalid plugin archive: {exc}")
    paths = {p for p, _, _ in entries}
    if "package/package.json" not in paths: fail("archive has no package.json")
    try: metadata = json.loads(next(d for p, d, _ in entries if p == "package/package.json"))
    except Exception as exc: fail(f"package.json is unreadable: {exc}")
    if metadata.get("name") != NAME or metadata.get("version") != VERSION:
        fail("package.json identity does not match signalk-vesseltwin 0.2.1")
    if metadata.get("dependencies") or metadata.get("optionalDependencies"):
        fail("pinned plugin unexpectedly declares runtime dependencies")
    return entries

def target_for(data: Path) -> Path: return data / "signalk" / "node_modules" / NAME

def read_metadata(path: Path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode): fail(f"package.json is not a regular file: {path}")
            raw = os.read(fd, 65537)
        finally: os.close(fd)
    except OSError as exc: fail(f"existing plugin has unreadable package.json: {exc}")
    if len(raw) > 65536: fail("package.json exceeds the 64 KiB safety limit")
    try: return json.loads(raw)
    except Exception as exc: fail(f"existing plugin has unreadable package.json: {exc}")

def existing(target: Path) -> bool:
    for path in (target.parent.parent, target.parent, target):
        if path.is_symlink(): fail(f"refusing symlink in plugin path: {path}")
        if path.exists() and not path.is_dir(): fail(f"plugin path component is not a directory: {path}")
    if not target.exists(): return False
    if not target.is_dir(): fail(f"existing plugin target is not a directory: {target}")
    package_json = target / "package.json"
    if package_json.is_symlink() or not package_json.is_file(): fail(f"existing plugin package.json is not a regular file: {package_json}")
    metadata = read_metadata(package_json)
    if metadata.get("name") != NAME or not isinstance(metadata.get("version"), str) or not metadata["version"]:
        fail("existing plugin package.json has the wrong identity")
    print(f"VesselTwin preload: preserving existing plugin at {target}"); return True

def fetch_archive(args) -> bytes:
    override = os.environ.get("VESSELSTACK_VESSELTWIN_ARCHIVE")
    try:
        if override:
            with Path(override).open("rb") as source: return source.read(MAX_ARCHIVE + 1)
        with urllib.request.urlopen(URL, timeout=args.timeout) as response: return response.read(MAX_ARCHIVE + 1)
    except Exception as exc: fail(f"could not read pinned archive: {exc}")

def load_staged(stage: Path) -> bytes:
    path = stage / "plugin.tgz"
    try:
        with path.open("rb") as source: return source.read(MAX_ARCHIVE + 1)
    except Exception as exc: fail(f"could not read staged archive: {exc}")

def rename_noreplace(source: str, dest: str, fd: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True); fn = getattr(libc, "renameat2", None)
    if fn is None: fail("atomic no-replace publish is unavailable on this Linux runtime")
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]; fn.restype = ctypes.c_int
    if fn(fd, source.encode(), fd, dest.encode(), 1):
        error = ctypes.get_errno()
        if error == errno.EEXIST: raise FileExistsError(dest)
        raise OSError(error, os.strerror(error))

def publish(raw: bytes, args, target: Path) -> None:
    entries = validate(raw); signalk = args.data / "signalk"
    try: sfd = os.open(signalk, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc: fail(f"managed SignalK directory is unavailable: {exc}")
    try:
        os.fchdir(sfd)
        if os.geteuid() == 0:
            if args.uid <= 0 or args.gid <= 0: fail("refusing zero service uid/gid")
            os.setgroups([]); os.setgid(args.gid); os.setuid(args.uid)
        try:
            nfd = os.open("node_modules", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            os.mkdir("node_modules", 0o755)
            nfd = os.open("node_modules", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchdir(nfd)
            if os.path.lexists(NAME):
                existing(Path(NAME)); return
            # Keep the extraction path relative to the already-open node_modules fd;
            # absolute temp paths would retraverse a parent the dropped uid cannot enter.
            temp = Path(Path(tempfile.mkdtemp(prefix=f".{NAME}.", dir=".")).name); leaf = temp / NAME
            try:
                for path, data, is_dir in entries:
                    dest = leaf / Path(path).relative_to("package")
                    if not is_dir:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with dest.open("xb") as stream: stream.write(data)
                        os.chmod(dest, 0o644)
                    else:
                        dest.mkdir(parents=True, exist_ok=True); os.chmod(dest, 0o755)
                try:
                    rename_noreplace(f"{temp.name.rsplit('/', 1)[-1]}/{NAME}", NAME, nfd)
                except FileExistsError:
                    existing(Path(NAME))
                else:
                    temp.rmdir()
            finally:
                if temp.exists(): shutil.rmtree(temp, ignore_errors=True)
        finally: os.close(nfd)
    except OSError as exc: fail(f"could not publish prepared plugin: {exc}")
    finally: os.close(sfd)

def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--mode", choices=("existing", "docker", "native"), required=True)
    parser.add_argument("--data", required=True, type=Path); parser.add_argument("--offline", action="store_true"); parser.add_argument("--check", action="store_true")
    parser.add_argument("--uid", type=int, default=1000); parser.add_argument("--gid", type=int, default=1000); parser.add_argument("--timeout", type=float, default=15); parser.add_argument("--stage", type=Path)
    parser.add_argument("--prepare", action="store_true"); parser.add_argument("--publish", action="store_true"); args = parser.parse_args()
    if sum((args.check, args.prepare, args.publish)) != 1: fail("exactly one of --check, --prepare, or --publish is required")
    if args.mode == "existing": return 0
    target = target_for(args.data)
    if existing(target): return 0
    if args.offline: fail(f"managed {args.mode} SignalK has no preloaded plugin; rerun online with SIGNALK_VESSELTWIN_PRELOAD=true or set it false")
    if args.check:
        validate(fetch_archive(args)); print("VesselTwin preload: pinned archive is valid and ready"); return 0
    if not args.stage: fail("--prepare/--publish requires --stage")
    if args.prepare:
        args.stage.mkdir(mode=0o700, parents=True, exist_ok=True); raw = fetch_archive(args); validate(raw)
        (args.stage / "plugin.tgz").write_bytes(checked(raw)); os.chmod(args.stage / "plugin.tgz", 0o600); return 0
    raw = load_staged(args.stage); publish(checked(raw), args, target); print(f"Preloaded {NAME}@{VERSION} at {target}"); return 0

if __name__ == "__main__": main()
