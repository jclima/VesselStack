#!/usr/bin/env python3
"""Durable state record for `vesselstackctl restore` (standard library only).

A restore keeps a journal directory.  Its `state` file is plain KEY=VALUE lines
that are read and written here and are never sourced or evaluated by a shell.

  set DIR KEY=VALUE [KEY=VALUE ...]  update keys; atomic, fsynced replace of DIR/state
  get DIR KEY                        print one value (exit 1, no output, when absent)
  dump DIR                           print every KEY=VALUE line
  fsync-dir PATH [PATH ...]          fsync directories (after a rename)
  fsync-tree PATH [PATH ...]         fsync every regular file and directory under each PATH (a
                                     file is accepted too); symlinks are never followed, special
                                     files are skipped
  inode PATH                         print dev:ino of PATH itself (a final symlink is
                                     not followed), or "absent"
  ino PATH                           print only the inode number of PATH itself, or "absent"
                                     (the device number can change across a reboot)
  inspect PATH                       print "<dev:ino|absent> <same|different|absent>": inode and
                                     same-fs of PATH in one call
  same-fs PATH                       print "same" when PATH is on the same filesystem as its real parent
                                     directory (PATH/.., so a symbolic link in an ancestor does not
                                     matter), "different" when not (a mount point), "absent", or
                                     "notdir" when PATH is something other than a directory or a link
  resolve PATH [PATH ...]            print the fully resolved form of each PATH (symbolic links followed,
                                     the path need not exist), one per line
  rename FROM TO                     rename(2) with the semantics of "mv -T": TO must not exist or be
                                     an empty directory, otherwise it fails. Never copies: a rename
                                     across filesystems fails with an error

Every `set` also records boot_id=<the running boot> (the kernel's random boot id, read from
/proc/sys/kernel/random/boot_id, or from the file named by VESSELSTACK_BOOT_ID_FILE when that is
set, which is how the tests choose it). When that file is missing, unreadable or not a UUID, no boot_id is
written and an existing one is removed, so an id from an earlier boot never looks current. A caller
cannot pass boot_id=: the helper sets it.

A state file with a repeated key is corrupt: get, dump and set refuse it.

Exit status: 0 success, 1 key absent / operational error, 2 usage or invalid input.
"""
from __future__ import annotations

import errno
import os
import re
import stat
import sys

KEY_RE = re.compile(r"^[a-z_]+$")
BOOT_ID_KEY = "boot_id"
BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"
BOOT_ID_ENV = "VESSELSTACK_BOOT_ID_FILE"
BOOT_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
STATE_NAME = "state"
TMP_NAME = "state.tmp"


class StateError(Exception):
    """Invalid input or an unreadable/corrupt state file."""


def validate_key(key: str) -> str:
    if not KEY_RE.match(key):
        raise StateError(f"invalid key: {key!r} (use lowercase letters and underscores)")
    return key


def validate_value(value: str) -> str:
    if "\n" in value or "\x00" in value:
        raise StateError("a value must not contain a newline or NUL")
    return value


def fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_state(directory: str) -> dict[str, str]:
    """Return the recorded keys; an absent state file is an empty record."""
    path = os.path.join(directory, STATE_NAME)
    try:
        with open(path, encoding="utf-8", errors="surrogateescape", newline="") as handle:
            text = handle.read()
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for line in text.split("\n"):
        if not line:
            continue
        key, sep, value = line.partition("=")
        if not sep or not KEY_RE.match(key):
            raise StateError(f"corrupt state file {path}: {line[:60]!r}")
        if key in values:
            raise StateError(f"corrupt state file {path}: key {key!r} appears more than once")
        values[key] = value
    return values


def write_state(directory: str, values: dict[str, str]) -> None:
    """Replace DIR/state atomically: either the old or the new content survives a crash."""
    tmp = os.path.join(directory, TMP_NAME)
    final = os.path.join(directory, STATE_NAME)
    payload = "".join(f"{key}={value}\n" for key, value in values.items())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    fsync_dir(directory)


def current_boot_id() -> str | None:
    """The id of the running boot, or None when it cannot be read or is not a UUID."""
    path = os.environ.get(BOOT_ID_ENV) or BOOT_ID_FILE
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            text = handle.read(256).strip()
    except OSError:
        return None
    return text if BOOT_ID_RE.match(text) else None


def set_values(directory: str, assignments: list[str]) -> None:
    updates: dict[str, str] = {}
    for item in assignments:
        key, sep, value = item.partition("=")
        if not sep:
            raise StateError(f"expected KEY=VALUE, got {item!r}")
        if key == BOOT_ID_KEY:
            raise StateError("boot_id is recorded by this helper and cannot be set")
        updates[validate_key(key)] = validate_value(value)
    values = read_state(directory)
    values.update(updates)
    # Every state write goes through here, so the journal always says which boot wrote it last.
    boot_id = current_boot_id()
    if boot_id is None:
        values.pop(BOOT_ID_KEY, None)
    else:
        values[BOOT_ID_KEY] = boot_id
    write_state(directory, values)


DIR_FSYNC_UNSUPPORTED = {errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}


def fsync_tree(path: str) -> None:
    """fsync every regular file and directory at or under PATH without following symlinks."""
    top = os.lstat(path)  # a missing PATH is an error
    if stat.S_ISLNK(top.st_mode):
        raise OSError(errno.ELOOP, "refusing to follow a symbolic link", path)
    pending = [path]
    while pending:
        current = pending.pop()
        mode = os.lstat(current).st_mode
        if stat.S_ISREG(mode):
            fd = os.open(current, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        elif stat.S_ISDIR(mode):
            with os.scandir(current) as entries:
                pending.extend(entry.path for entry in entries)
            fd = os.open(current, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(fd)
            except OSError as exc:
                if exc.errno not in DIR_FSYNC_UNSUPPORTED:
                    raise
            finally:
                os.close(fd)
        # symlinks and special files (fifo, socket, device) are skipped


def ino_of(path: str) -> str:
    try:
        return str(os.lstat(path).st_ino)
    except FileNotFoundError:
        return "absent"


def same_fs(path: str) -> str:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "absent"
    if not stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
        return "notdir"  # a file (or other non-directory) is in the way
    if stat.S_ISLNK(st.st_mode):
        # a link is not a tree to swap; compare the link itself with the directory that holds it
        parent = os.stat(os.path.dirname(os.path.abspath(path)) or "/")
    else:
        # PATH/.. is the real parent directory, even when an ancestor is a symbolic link to another disk
        parent = os.stat(os.path.join(path, ".."))
    return "same" if st.st_dev == parent.st_dev else "different"


def rename_tree(src: str, dst: str) -> None:
    """os.rename with "mv -T" semantics: DST must not exist or be an empty directory. No copy fallback."""
    try:
        dst_mode = os.lstat(dst).st_mode
    except FileNotFoundError:
        dst_mode = None
    if dst_mode is not None:
        if not stat.S_ISDIR(dst_mode):
            raise OSError(errno.EEXIST, "the destination exists and is not a directory", dst)
        if not stat.S_ISDIR(os.lstat(src).st_mode):
            raise OSError(errno.ENOTDIR, "cannot replace a directory with a non-directory", dst)
        # rename(2) onto a directory succeeds only when it is empty (ENOTEMPTY otherwise)
    os.rename(src, dst)


def inspect_path(path: str) -> str:
    return f"{inode_of(path)} {same_fs(path)}"


def inode_of(path: str) -> str:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "absent"
    return f"{st.st_dev}:{st.st_ino}"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    command, args = argv[1], argv[2:]
    try:
        if command == "set" and len(args) >= 2:
            set_values(args[0], args[1:])
        elif command == "get" and len(args) == 2:
            values = read_state(args[0])
            key = validate_key(args[1])
            if key not in values:
                return 1
            print(values[key])
        elif command == "dump" and len(args) == 1:
            for key, value in read_state(args[0]).items():
                print(f"{key}={value}")
        elif command == "fsync-dir" and args:
            for path in args:
                fsync_dir(path)
        elif command == "fsync-tree" and args:
            for path in args:
                fsync_tree(path)
        elif command == "inode" and len(args) == 1:
            print(inode_of(args[0]))
        elif command == "ino" and len(args) == 1:
            print(ino_of(args[0]))
        elif command == "inspect" and len(args) == 1:
            print(inspect_path(args[0]))
        elif command == "rename" and len(args) == 2:
            rename_tree(args[0], args[1])
        elif command == "resolve" and args:
            for path in args:
                print(os.path.realpath(path))
        elif command == "same-fs" and len(args) == 1:
            print(same_fs(args[0]))
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except StateError as exc:
        print(f"restore_journal: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"restore_journal: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
