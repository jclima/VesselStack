"""Process-wide lifecycle lock shared by VesselStack management surfaces.

The lock is deliberately kept outside the application and data trees.  A
descriptor may be passed to a child process, but an environment variable by
itself never grants access: the descriptor is checked against the expected
lock file before it is used.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import os
import stat
from pathlib import Path
from typing import Iterator, Mapping


ENV_FD = "VESSELSTACK_LIFECYCLE_LOCK_FD"


class LifecycleLockError(RuntimeError):
    """The lifecycle lock cannot be safely acquired."""


class LifecycleLockBusy(LifecycleLockError):
    """Another process currently owns the lifecycle lock."""


def _canonical_root(root: os.PathLike[str] | str) -> str:
    # Do not use resolve(): a path's identity must not depend on whether it
    # happens to exist (or on symlink state changing during installation).
    return os.path.normpath(os.path.abspath(os.fspath(root)))


def lock_path(install_root: os.PathLike[str] | str,
              system_root: os.PathLike[str] | str) -> Path:
    """Return the stable lock pathname for an installation root."""
    root = _canonical_root(install_root)
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()
    prefix = os.fspath(system_root) or os.sep
    return Path(os.path.abspath(prefix)) / "run" / "lock" / "vesselstack" / f"{digest}.lock"


def _check_directory(path: Path, allow_sticky: bool = False) -> None:
    st = path.lstat()
    if (stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or
            st.st_uid != os.geteuid() or
            (st.st_mode & 0o022 and not (allow_sticky and st.st_mode & stat.S_ISVTX))):
        raise LifecycleLockError(f"untrusted lock directory: {path}")


def _prepare_directory(path: Path, system_root: Path) -> None:
    probe = path
    while True:
        try:
            st = probe.lstat()
        except FileNotFoundError:
            st = None
        if st is not None and (stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode)):
            raise LifecycleLockError(f"untrusted lock directory: {probe}")
        if probe == system_root:
            break
        if probe.parent == probe or system_root not in probe.parents:
            raise LifecycleLockError(f"lock directory is outside system root: {probe}")
        probe = probe.parent
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except FileExistsError:
        # A pre-existing symlink (or non-directory) is checked and rejected
        # below; don't let pathlib's recursive mkdir obscure that result.
        pass
    # Check the complete fixed chain under system_root.  lstat() is important:
    # following a replaced run/lock/vesselstack symlink would defeat the trust
    # boundary even when the destination itself looks safe.
    current = path
    while True:
        _check_directory(current, allow_sticky=current.name == "lock")
        if current == system_root:
            break
        if current.parent == current or system_root not in current.parents:
            raise LifecycleLockError(f"lock directory is outside system root: {current}")
        current = current.parent


def _validate_fd(fd: int, expected: Path) -> None:
    try:
        fd_stat = os.fstat(fd)
        path_stat = expected.lstat()
    except (OSError, ValueError) as exc:
        raise LifecycleLockError("invalid inherited lifecycle lock descriptor") from exc
    if (not stat.S_ISREG(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode) or
            not stat.S_ISREG(fd_stat.st_mode) or fd_stat.st_uid != os.geteuid() or
            fd_stat.st_mode & 0o077 or
            (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino)):
        raise LifecycleLockError("invalid inherited lifecycle lock descriptor")


def _inherited_fd(value: str | None) -> int | None:
    if value is None:
        return None
    if not value.isdigit():
        raise LifecycleLockError("invalid inherited lifecycle lock descriptor")
    fd = int(value)
    if fd < 3:
        raise LifecycleLockError("invalid inherited lifecycle lock descriptor")
    return fd


class LifecycleLock:
    """A context-managed lifecycle lock with safe child-process handoff."""

    def __init__(self, install_root: os.PathLike[str] | str,
                 system_root: os.PathLike[str] | str):
        self.install_root = _canonical_root(install_root)
        self.system_root = os.path.abspath(os.fspath(system_root) or os.sep)
        self.path = lock_path(self.install_root, self.system_root)
        self.fd: int | None = None
        self.inherited = False

    def acquire(self) -> "LifecycleLock":
        if self.fd is not None:
            return self
        _prepare_directory(self.path.parent, Path(self.system_root))
        inherited = _inherited_fd(os.environ.get(ENV_FD))
        if inherited is not None:
            _validate_fd(inherited, self.path)
            # Never close or unlock the caller's descriptor.  The duplicate
            # shares its open-file description and therefore its flock.
            fd = os.dup(inherited)
            os.set_inheritable(fd, True)
            inherited_flag = True
        else:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(self.path, flags, 0o600)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise LifecycleLockError(f"unsafe lifecycle lock path: {self.path}") from exc
                raise
            try:
                _validate_fd(fd, self.path)
            except Exception:
                os.close(fd)
                raise
            os.set_inheritable(fd, True)
            inherited_flag = False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise LifecycleLockBusy("VesselStack lifecycle lock is busy") from exc
            raise LifecycleLockError("could not acquire lifecycle lock") from exc
        self.fd = fd
        self.inherited = inherited_flag
        return self

    def release(self) -> None:
        if self.fd is None:
            return
        os.close(self.fd)
        self.fd = None

    def __enter__(self) -> "LifecycleLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def child_env(self, env: Mapping[str, str] | None = None) -> dict[str, str]:
        if self.fd is None:
            raise LifecycleLockError("lifecycle lock is not acquired")
        result = dict(os.environ if env is None else env)
        result[ENV_FD] = str(self.fd)
        return result

    def pass_fds(self) -> tuple[int, ...]:
        if self.fd is None:
            raise LifecycleLockError("lifecycle lock is not acquired")
        return (self.fd,)


@contextlib.contextmanager
def acquire_lifecycle_lock(install_root: os.PathLike[str] | str,
                           system_root: os.PathLike[str] | str) -> Iterator[LifecycleLock]:
    with LifecycleLock(install_root, system_root) as handle:
        yield handle


def child_env(lock: LifecycleLock, env: Mapping[str, str] | None = None) -> dict[str, str]:
    return lock.child_env(env)


def pass_fds(lock: LifecycleLock) -> tuple[int, ...]:
    return lock.pass_fds()


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="lifecycle_lock")
    subparsers = parser.add_subparsers(dest="action", required=True)

    def add_lock_args(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--root", required=True, dest="install_root")
        subparser.add_argument("--system-root", default="/", dest="system_root")

    run_parser = subparsers.add_parser("run", help="run a command under the lifecycle lock")
    add_lock_args(run_parser)
    run_parser.add_argument("command", nargs=argparse.REMAINDER)

    check_parser = subparsers.add_parser("check", help="validate an inherited lifecycle lock")
    add_lock_args(check_parser)

    args = parser.parse_args(argv)
    try:
        if args.action == "check":
            if os.environ.get(ENV_FD) is None:
                raise LifecycleLockError(f"{ENV_FD} is not set")
            handle = LifecycleLock(args.install_root, args.system_root).acquire()
            handle.release()
            return 0
        command = args.command
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            parser.error("run requires COMMAND after --")
        handle = LifecycleLock(args.install_root, args.system_root).acquire()
        os.execvpe(command[0], command, handle.child_env())
    except LifecycleLockError as exc:
        print(f"lifecycle lock: {exc}", file=__import__("sys").stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(_main())
