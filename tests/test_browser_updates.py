#!/usr/bin/env python3
import importlib.util
import hashlib
import io
import json
import shutil
import tarfile
import tempfile
import unittest
import os
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("browser_updates", ROOT / "control-panel/browser_updates.py")
assert SPEC and SPEC.loader
updates = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updates)


class BrowserUpdateTests(unittest.TestCase):
    def test_state_paths_reject_traversal_and_symlink_before_filesystem_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            linked = root / "linked"
            target = root / "target"
            target.mkdir(); linked.symlink_to(target, target_is_directory=True)
            for bad_root in (root / "var" / ".." / "escape", linked / "var"):
                with self.subTest(bad_root=bad_root), mock.patch.object(updates.Path, "mkdir") as mkdir, mock.patch.object(updates.os, "open") as open_:
                    with self.assertRaises(ValueError):
                        updates.acquire_slot(bad_root)
                    mkdir.assert_not_called(); open_.assert_not_called()
            with mock.patch.object(updates.Path, "mkdir") as mkdir, mock.patch.object(updates.os, "open") as open_:
                with self.assertRaises(ValueError):
                    updates.start_detached("restart-panel", root / "var" / ".." / "escape", {"boot_id": "a" * 32})
                mkdir.assert_not_called(); open_.assert_not_called()

    def test_fixed_control_binary_guard_rejects_missing_directory_and_nonexecutable(self):
        with tempfile.TemporaryDirectory() as directory:
            ctl = Path(directory) / "vesselstackctl"
            with mock.patch.object(updates, "FIXED_CTL", str(ctl)):
                with self.assertRaises(ValueError): updates._validate_fixed_ctl()
                ctl.mkdir()
                with self.assertRaises(ValueError): updates._validate_fixed_ctl()
                ctl.rmdir(); ctl.write_text("#!/bin/sh\n"); os.chmod(ctl, 0o644)
                with self.assertRaises(ValueError): updates._validate_fixed_ctl()
    def _release_fixture(self, directory, version="0.6.0"):
        archive = Path(directory) / f"vesselstack-{version}.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            for name, data, mode in ((f"vesselstack-{version}", b"", 0o700),
                                     (f"vesselstack-{version}/scripts", b"", 0o700),
                                     (f"vesselstack-{version}/systemd", b"", 0o700),
                                     (f"vesselstack-{version}/templates", b"", 0o700),
                                     (f"vesselstack-{version}/control-panel", b"", 0o700),
                                     (f"vesselstack-{version}/VERSION", version.encode(), 0o600),
                                     (f"vesselstack-{version}/install.sh", b"#!/bin/sh\n", 0o700)):
                info = tarfile.TarInfo(name); info.uid = info.gid = 0; info.mode = mode; info.size = len(data)
                if name.endswith((version, "scripts", "systemd", "templates", "control-panel")):
                    info.type = tarfile.DIRTYPE; info.size = 0
                handle.addfile(info, io.BytesIO(data) if data else None)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksum = Path(directory) / f"vesselstack-{version}.tar.gz.sha256"
        checksum.write_text(f"{digest}  {archive.name}\n")
        return archive, checksum, digest

    def test_release_snapshot_preserves_real_asset_names_and_urls(self):
        item = {"id": 9, "tag_name": "v0.6.0", "draft": False, "prerelease": False,
                "published_at": "2026-01-01", "updated_at": "2026-01-02", "assets": [
                    {"id": 10, "name": "vesselstack-0.6.0.tar.gz", "size": 12, "updated_at": "2026-01-02"},
                    {"id": 11, "name": "vesselstack-0.6.0.tar.gz.sha256", "size": 64, "updated_at": "2026-01-02"}]}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(updates, "_json_get", return_value=item):
            result = updates.release_snapshot(9, "lab", Path(directory))
        self.assertEqual(result["assets"]["archive"]["name"], "vesselstack-0.6.0.tar.gz")
        self.assertTrue(result["assets"]["archive"]["url"].endswith("/v0.6.0/vesselstack-0.6.0.tar.gz"))

    def test_run_update_success_orders_backup_before_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive, checksum, digest = self._release_fixture(directory)
            snapshot = {"id": 9, "tag": "v0.6.0", "version": "0.6.0", "prerelease": False, "updated_at": "u",
                        "assets": {"archive": {"id": 1, "name": archive.name, "size": archive.stat().st_size, "updated_at": "a", "digest": f"sha256:{digest}", "url": "https://github.com/a"},
                                   "checksum": {"id": 2, "name": checksum.name, "size": checksum.stat().st_size, "updated_at": "a", "digest": None, "url": "https://github.com/c"}}}
            request = {"release_id": 9, "channel": "lab", "config": "/etc/vesselstack.env", "old_boot_id": "old", "release_snapshot": snapshot}
            operation = {"id": 1, "state": "running", "phase": "queued", "backup_path": None}
            calls = []
            def download(url, dest, **kwargs): shutil.copy2(archive if dest.suffix == ".gz" else checksum, dest)
            with mock.patch.object(updates, "release_snapshot", side_effect=[snapshot, snapshot]), mock.patch.object(updates, "bounded_download", side_effect=download), mock.patch.object(updates, "verify_restart", return_value={"boot_id": "new", "running_version": "0.6.0"}), mock.patch.object(updates, "_validate_fixed_ctl"), mock.patch.object(updates, "run_command", side_effect=lambda argv, *args, **kw: calls.append(argv) or mock.Mock(stdout="/backup/archive.tar.gz\n")):
                updates.run_update(request, operation, root)
            self.assertEqual(operation["state"], "succeeded")
            self.assertLess(calls.index([updates.FIXED_CTL, "backup", "--require-mounted"]), calls.index(mock.ANY) if False else next(i for i, c in enumerate(calls) if c[-1] == "--start"))
            self.assertEqual(updates.read_operation(root)["backup_path"], "/backup/archive.tar.gz")

    def test_run_update_backup_failure_never_starts_or_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive, checksum, digest = self._release_fixture(directory)
            snapshot = {"id": 9, "tag": "v0.6.0", "version": "0.6.0", "prerelease": False, "updated_at": "u", "assets": {"archive": {"id": 1, "name": archive.name, "size": archive.stat().st_size, "updated_at": "a", "digest": f"sha256:{digest}", "url": "https://github.com/a"}, "checksum": {"id": 2, "name": checksum.name, "size": checksum.stat().st_size, "updated_at": "a", "digest": None, "url": "https://github.com/c"}}}
            request = {"release_id": 9, "channel": "lab", "config": "cfg", "old_boot_id": "old", "release_snapshot": snapshot}; operation = {"id": 1, "state": "running", "backup_path": None}
            def download(url, dest, **kwargs): shutil.copy2(archive if dest.suffix == ".gz" else checksum, dest)
            def run(argv, *args, **kw):
                if argv[:2] == [updates.FIXED_CTL, "backup"]: raise RuntimeError("backup failed")
                return mock.Mock(stdout="")
            with mock.patch.object(updates, "release_snapshot", side_effect=[snapshot, snapshot]), mock.patch.object(updates, "bounded_download", side_effect=download), mock.patch.object(updates, "_validate_fixed_ctl"), mock.patch.object(updates, "run_command", side_effect=run) as invoked:
                with self.assertRaises(RuntimeError): updates.run_update(request, operation, root)
            self.assertFalse(any("--start" in call.args[0] for call in invoked.call_args_list))
            self.assertFalse(any(call.args[0][:2] == ["systemctl", "restart"] for call in invoked.call_args_list))

    def test_run_update_bad_checksum_stops_before_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive, checksum, digest = self._release_fixture(directory); checksum.write_text("0" * 64 + "  " + archive.name + "\n")
            snapshot = {"id": 9, "tag": "v0.6.0", "version": "0.6.0", "prerelease": False, "updated_at": "u", "assets": {"archive": {"id": 1, "name": archive.name, "size": archive.stat().st_size, "updated_at": "a", "digest": None, "url": "https://github.com/a"}, "checksum": {"id": 2, "name": checksum.name, "size": checksum.stat().st_size, "updated_at": "a", "digest": None, "url": "https://github.com/c"}}}
            request = {"release_id": 9, "channel": "lab", "config": "cfg", "old_boot_id": "old", "release_snapshot": snapshot}; operation = {"id": 1, "state": "running"}
            def download(url, dest, **kwargs): shutil.copy2(archive if dest.suffix == ".gz" else checksum, dest)
            with mock.patch.object(updates, "release_snapshot", side_effect=[snapshot, snapshot]), mock.patch.object(updates, "bounded_download", side_effect=download), mock.patch.object(updates.subprocess, "run") as invoked:
                with self.assertRaises(ValueError): updates.run_update(request, operation, root)
            invoked.assert_not_called()
    def test_b1_lock_and_detached_interfaces_exist(self):
        self.assertTrue(hasattr(updates, "acquire_slot"))
        self.assertTrue(hasattr(updates, "acquire_runner_lock"))
        self.assertTrue(hasattr(updates, "start_detached"))

    def test_catalog_filters_numeric_newer_releases_by_channel(self):
        payload = [
            {"id": 1, "tag_name": "v0.5.4", "name": "Stable", "prerelease": False, "published_at": "now", "html_url": "https://github.com/jclima/VesselStack/releases/1"},
            {"id": 2, "tag_name": "v0.6.0", "name": "RC", "prerelease": True},
            {"id": 3, "tag_name": "v0.5.2", "name": "Old", "prerelease": False},
            {"id": 4, "tag_name": "latest", "name": "Moving", "prerelease": False},
        ]
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(updates, "_json_get", return_value=payload):
            root = Path(directory); (root / "VERSION").write_text("0.5.3\n")
            stable = updates.release_catalog("stable", root)
            lab = updates.release_catalog("lab", root)
        self.assertEqual([item["id"] for item in stable["releases"]], [1])
        self.assertEqual([item["id"] for item in lab["releases"]], [2, 1])

    def _marker(self, root, text):
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "install-in-progress").write_text(text)

    GOOD_MARKER = "version=0.6.0\nfrom=0.5.3\nstarted=2026-01-02T03:04:05Z\nstart=0\nsource=/x\nbackup=/var/backups/vs/pre-0.6.0.tar.gz\n"

    def test_install_in_progress_reads_and_validates_the_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(updates.install_in_progress(root))
            self._marker(root, self.GOOD_MARKER + "junk line\nunknown=1\n")
            self.assertEqual(updates.install_in_progress(root), {"version": "0.6.0", "from": "0.5.3", "started": "2026-01-02T03:04:05Z", "backup": "/var/backups/vs/pre-0.6.0.tar.gz"})
            self._marker(root, "version=latest\nfrom=none\nstarted=2026-01-02T03:04:05Z\nbackup=\n")
            self.assertEqual(updates.install_in_progress(root), {"version": None, "from": "none", "started": "2026-01-02T03:04:05Z", "backup": ""})
            for bad in ("version=0.6.0\nstarted=yesterday\n", "version=0.6.0\nstarted=2026-01-02T03:04:05Z\nbackup=relative/x\n",
                        "version=0.6.0\nstarted=2026-01-02T03:04:05Z\nbackup=/a/../b\n", "version=0.6.0\nstarted=2026-01-02T03:04:05Z\nbackup=/a/$(id)\n",
                        "version=0.6.0\nstarted=2026-01-02T03:04:05Z\nbackup=/a b\n", "x" * 5000):
                with self.subTest(bad=bad[:40]):
                    self._marker(root, bad)
                    self.assertIsNone(updates.install_in_progress(root))

    def test_install_in_progress_refuses_a_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "config").mkdir()
            (root / "real").write_text(self.GOOD_MARKER)
            (root / "config" / "install-in-progress").symlink_to(root / "real")
            self.assertIsNone(updates.install_in_progress(root))

    def _snapshot_item(self, tag):
        version = tag[1:]
        return {"id": 9, "tag_name": tag, "draft": False, "prerelease": False, "published_at": "p", "updated_at": "u", "assets": [
            {"id": 10, "name": f"vesselstack-{version}.tar.gz", "size": 12, "updated_at": "u"},
            {"id": 11, "name": f"vesselstack-{version}.tar.gz.sha256", "size": 64, "updated_at": "u"}]}

    def test_marker_version_is_eligible_for_catalog_and_snapshot_only_with_the_marker(self):
        payload = [{"id": 1, "tag_name": "v0.6.0", "name": "Same", "prerelease": False}, {"id": 2, "tag_name": "v0.5.9", "name": "Older", "prerelease": False}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "config").mkdir(); (root / "config" / "installed-version").write_text("0.6.0\n")
            with mock.patch.object(updates, "_json_get", return_value=payload):
                self.assertEqual(updates.release_catalog("stable", root)["releases"], [])
            with mock.patch.object(updates, "_json_get", return_value=self._snapshot_item("v0.6.0")):
                with self.assertRaises(ValueError): updates.release_snapshot(9, "stable", root)
            self._marker(root, self.GOOD_MARKER)
            with mock.patch.object(updates, "_json_get", return_value=payload):
                releases = updates.release_catalog("stable", root)["releases"]
            self.assertEqual([(r["id"], r["resume"]) for r in releases], [(1, True)])
            with mock.patch.object(updates, "_json_get", return_value=self._snapshot_item("v0.6.0")):
                self.assertEqual(updates.release_snapshot(9, "stable", root)["version"], "0.6.0")
            with mock.patch.object(updates, "_json_get", return_value=self._snapshot_item("v0.5.9")):
                with self.assertRaises(ValueError): updates.release_snapshot(9, "stable", root)

    def test_marker_for_another_version_changes_nothing(self):
        payload = [{"id": 1, "tag_name": "v0.6.0", "name": "Same", "prerelease": False}, {"id": 3, "tag_name": "v0.7.0", "name": "New", "prerelease": False}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "config").mkdir(); (root / "config" / "installed-version").write_text("0.6.0\n")
            self._marker(root, self.GOOD_MARKER.replace("version=0.6.0", "version=0.6.1"))
            with mock.patch.object(updates, "_json_get", return_value=payload):
                releases = updates.release_catalog("stable", root)["releases"]
            self.assertEqual([(r["id"], r["resume"]) for r in releases], [(3, False)])

    def test_operation_is_private_and_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            updates.write_operation(root, {"state": "running", "phase": "queued"})
            path = updates.operation_path(root)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(updates.read_operation(root)["phase"], "queued")

    def test_process_identity_is_stable_and_not_kernel_boot_file(self):
        first = updates.running_identity()
        second = updates.running_identity()
        self.assertEqual(first, second)
        self.assertRegex(first["boot_id"], r"^[0-9a-f]{32}$")

    def test_installed_marker_does_not_change_captured_running_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "config").mkdir(); (root / "installer").mkdir()
            (root / "config" / "installed-version").write_text("0.5.4\n")
            (root / "installer" / "VERSION").write_text("0.5.3\n")
            captured = updates.running_identity(root)
            (root / "config" / "installed-version").write_text("0.5.5\n")
            self.assertEqual(updates.installed_version(root), "0.5.5")
            self.assertEqual(captured["running_version"], "0.5.3")

    def test_identity_has_no_kernel_boot_dependency(self):
        with mock.patch.object(updates.Path, "read_text", side_effect=AssertionError("kernel file read")):
            identity = updates.running_identity(None)
        self.assertEqual(len(identity["boot_id"]), 32)

    def test_detached_launch_failure_is_reported(self):
        self.assertTrue(hasattr(updates, "start_detached"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(updates.subprocess, "run", side_effect=OSError("systemd-run missing")):
                with self.assertRaises(OSError):
                    updates.start_detached(["systemd-run"], root, {"id": 1})

    def test_durable_busy_state_rejects_new_slot(self):
        self.assertTrue(hasattr(updates, "acquire_slot"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.joinpath("var/lib/vesselstack").mkdir(parents=True)
            updates.write_operation(root, {"id": 9, "state": "running", "phase": "installing"})
            runner_fd = updates.acquire_runner_lock(root)
            try:
                with self.assertRaisesRegex(ValueError, "already running"):
                    updates.acquire_slot(root)
            finally:
                updates.release_slot(runner_fd)

    def test_inactive_helper_marks_interrupted_state_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            updates.write_operation(root, {"id": 9, "state": "running", "phase": "backing-up", "backup_path": "/media/backup.tar.gz", "output": []})
            inactive = updates.subprocess.CompletedProcess([], 0, stdout="inactive")
            with mock.patch.object(updates.subprocess, "run", return_value=inactive):
                fd = updates.acquire_slot(root)
            try:
                state = updates.read_operation(root)
                self.assertEqual(state["state"], "failed")
                self.assertEqual(state["backup_path"], "/media/backup.tar.gz")
            finally:
                updates.release_slot(fd)
            with mock.patch.object(updates.subprocess, "run", return_value=inactive):
                retry_fd = updates.acquire_slot(root)
            updates.release_slot(retry_fd)

    def test_unavailable_helper_keeps_running_state_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            updates.write_operation(root, {"id": 9, "state": "running", "phase": "installing", "output": []})
            with mock.patch.object(updates.subprocess, "run", side_effect=OSError("systemctl unavailable")):
                with self.assertRaisesRegex(ValueError, "already running"):
                    updates.acquire_slot(root)
            self.assertEqual(updates.read_operation(root)["state"], "running")

    def test_safe_extract_rejects_traversal_and_links(self):
        import io, tarfile
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "bad.tar.gz"; target = Path(directory) / "out"; target.mkdir()
            with tarfile.open(archive, "w:gz") as handle:
                info = tarfile.TarInfo("../escape"); info.size = 1; handle.addfile(info, io.BytesIO(b"x"))
            with self.assertRaises(ValueError): updates.safe_extract(archive, target)


    # ---- one-shot backup choices (backup drive not mounted)

    def _run_choice(self, backup, mount="not_mounted", env_text="", supports=True, fail_on=None):
        """Runs run_update with a fake control binary; returns (operation, calls, error, mocks)."""
        directory = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, directory, True)
        root = Path(directory); archive, checksum, digest = self._release_fixture(directory)
        config = root / "vesselstack.env"; config.write_text(env_text)
        snapshot = {"id": 9, "tag": "v0.6.0", "version": "0.6.0", "prerelease": False, "updated_at": "u",
                    "assets": {"archive": {"id": 1, "name": archive.name, "size": archive.stat().st_size, "updated_at": "a", "digest": f"sha256:{digest}", "url": "https://github.com/a"},
                               "checksum": {"id": 2, "name": checksum.name, "size": checksum.stat().st_size, "updated_at": "a", "digest": None, "url": "https://github.com/c"}}}
        request = {"release_id": 9, "channel": "lab", "config": str(config), "old_boot_id": "old", "release_snapshot": snapshot}
        if backup is not None:
            request["backup"] = backup
        operation = {"id": 1, "state": "running", "phase": "queued", "backup_path": None}
        if isinstance(backup, dict):
            operation["backup_mode"] = backup.get("mode")  # as start_detached records it
        calls = []
        def download(url, dest, **kwargs): shutil.copy2(archive if dest.suffix == ".gz" else checksum, dest)
        def run(argv, root_arg=None, op=None, *args, **kw):
            calls.append(list(argv))
            if argv[-1] == "--start":
                self.start_env = dict(args[1]) if len(args) > 1 else {}
            if isinstance(op, dict):
                op["output"] = ["output of " + Path(argv[0]).name]  # like the real runner, each command replaces the log
            if fail_on and argv[:len(fail_on)] == fail_on:
                raise RuntimeError("step failed")
            return mock.Mock(stdout="/backup/archive.tar.gz\n")
        error = None
        with mock.patch.object(updates, "release_snapshot", side_effect=[snapshot, snapshot]), mock.patch.object(updates, "bounded_download", side_effect=download) as downloaded, \
                mock.patch.object(updates, "verify_restart", return_value={"boot_id": "new", "running_version": "0.6.0"}), mock.patch.object(updates, "_validate_fixed_ctl"), \
                mock.patch.object(updates, "backup_destination", return_value=("/media/backup/backups", mount)), \
                mock.patch.object(updates, "ctl_supports", return_value=supports) as supported, mock.patch.object(updates, "run_command", side_effect=run):
            try:
                updates.run_update(request, operation, root)
            except Exception as exc:  # the caller inspects it
                error = exc
        return operation, calls, error, {"downloaded": downloaded, "supported": supported, "root": root}

    def test_default_run_never_probes_or_mounts(self):
        operation, calls, error, mocks = self._run_choice(None)
        self.assertIsNone(error)
        self.assertEqual([c for c in calls if "backup" in c[1:2] or "backup-drive" in c], [[updates.FIXED_CTL, "backup", "--require-mounted"]])
        mocks["supported"].assert_not_called()
        self.assertNotIn("backup_skipped", operation)
        self.assertEqual(operation["backup_path"], "/backup/archive.tar.gz")
        _, calls, error, _ = self._run_choice({"mode": "require_mounted"})
        self.assertIsNone(error)
        self.assertIn([updates.FIXED_CTL, "backup", "--require-mounted"], calls)

    def test_installer_gets_the_preupdate_backup_path_only_when_one_was_made(self):
        with mock.patch.dict(os.environ, {"VESSELSTACK_PREUPDATE_BACKUP": "/stale/ignored.tar.gz"}):
            self._run_choice(None)
            self.assertEqual(self.start_env.get("VESSELSTACK_PREUPDATE_BACKUP"), "/backup/archive.tar.gz")
            self._run_choice({"mode": "skip", "confirm": "install-without-backup"})
            self.assertNotIn("VESSELSTACK_PREUPDATE_BACKUP", self.start_env)

    def test_mount_modes_mount_after_preflight_and_before_backup(self):
        for backup, mount_argv in (({"mode": "mount_fstab"}, [updates.FIXED_CTL, "backup-drive", "mount", "--fstab"]),
                                   ({"mode": "mount_drive", "uuid": "AB12-CD34"}, [updates.FIXED_CTL, "backup-drive", "mount", "--uuid", "AB12-CD34"])):
            with self.subTest(backup=backup):
                operation, calls, error, mocks = self._run_choice(backup)
                self.assertIsNone(error)
                backup_call = [updates.FIXED_CTL, "backup", "--require-mounted"]
                dry = next(i for i, c in enumerate(calls) if c[-1] == "--dry-run")
                self.assertLess(dry, calls.index(mount_argv))
                self.assertLess(calls.index(mount_argv), calls.index(backup_call))
                self.assertLess(calls.index(backup_call), next(i for i, c in enumerate(calls) if c[-1] == "--start"))
                mocks["supported"].assert_called_with("backup-drive")
                self.assertEqual(operation["state"], "succeeded")

    def test_mount_failure_stops_before_backup_and_install(self):
        operation, calls, error, _ = self._run_choice({"mode": "mount_drive", "uuid": "AB12-CD34"}, fail_on=[updates.FIXED_CTL, "backup-drive"])
        self.assertIsInstance(error, RuntimeError)
        self.assertFalse(any(c[:2] == [updates.FIXED_CTL, "backup"] for c in calls))
        self.assertFalse(any(c[-1] == "--start" for c in calls))
        self.assertFalse(any(c[:2] == ["systemctl", "restart"] for c in calls))

    def test_system_disk_once_uses_the_one_shot_backup_flag(self):
        operation, calls, error, mocks = self._run_choice({"mode": "system_disk_once", "confirm": "system-disk-once"})
        self.assertIsNone(error)
        self.assertIn([updates.FIXED_CTL, "backup", "--system-disk-once"], calls)
        self.assertNotIn([updates.FIXED_CTL, "backup", "--require-mounted"], calls)
        mocks["supported"].assert_called_with("--system-disk-once")
        self.assertEqual(updates.read_operation(mocks["root"]).get("backup_path"), "/backup/archive.tar.gz")

    def test_skip_installs_without_calling_backup(self):
        operation, calls, error, mocks = self._run_choice({"mode": "skip", "confirm": "install-without-backup"})
        self.assertIsNone(error)
        self.assertFalse(any(c[:2] == [updates.FIXED_CTL, "backup"] for c in calls))
        self.assertTrue(any(c[-1] == "--start" for c in calls))
        self.assertIs(operation["backup_skipped"], True)
        self.assertIsNone(operation["backup_path"])
        self.assertEqual(operation["state"], "succeeded")
        self.assertEqual(operation["backup_mode"], "skip")
        self.assertEqual(operation["output"][0], updates.BACKUP_SKIP_WARNING, "the warning survives the commands that replace the log")
        self.assertEqual(operation["output"].count(updates.BACKUP_SKIP_WARNING), 1)
        self.assertIn("backup skipped at the owner's request", updates.BACKUP_SKIP_WARNING)

    def test_skip_keeps_earlier_log_below_the_warning(self) -> None:
        snapshots = []
        real_write = updates.write_operation
        def spy(root_arg, op):
            if op.get("phase") == "backing-up":
                snapshots.append(list(op.get("output") or []))
            return real_write(root_arg, op)
        with mock.patch.object(updates, "write_operation", side_effect=spy):
            operation, calls, error, _ = self._run_choice({"mode": "skip", "confirm": "install-without-backup"})
        self.assertIsNone(error)
        self.assertEqual(snapshots[-1][0], updates.BACKUP_SKIP_WARNING)
        self.assertIn("output of install.sh", snapshots[-1][1:], "the dry-run log is kept below the warning")

    def test_worker_rechecks_the_lock_before_skip_or_system_disk(self) -> None:
        for backup in ({"mode": "skip", "confirm": "install-without-backup"}, {"mode": "system_disk_once", "confirm": "system-disk-once"}):
            with self.subTest(backup=backup):
                original = updates.run_command
                directory_holder = {}
                def locking_run(argv, root_arg=None, op=None, *args, **kw):
                    if argv[-1] == "--dry-run":
                        (directory_holder["config"]).write_text("VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=false\n")  # locked after admission
                    return mock.Mock(stdout="/backup/archive.tar.gz\n")
                directory = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, directory, True)
                root = Path(directory); archive, checksum, digest = self._release_fixture(directory)
                config = root / "vesselstack.env"; config.write_text(""); directory_holder["config"] = config
                snapshot = {"id": 9, "tag": "v0.6.0", "version": "0.6.0", "prerelease": False, "updated_at": "u",
                            "assets": {"archive": {"id": 1, "name": archive.name, "size": archive.stat().st_size, "updated_at": "a", "digest": f"sha256:{digest}", "url": "https://github.com/a"},
                                       "checksum": {"id": 2, "name": checksum.name, "size": checksum.stat().st_size, "updated_at": "a", "digest": None, "url": "https://github.com/c"}}}
                request = {"release_id": 9, "channel": "lab", "config": str(config), "old_boot_id": "old", "release_snapshot": snapshot, "backup": backup}
                operation = {"id": 1, "state": "running", "phase": "queued", "backup_path": None}
                seen = []
                def download(url, dest, **kwargs): shutil.copy2(archive if dest.suffix == ".gz" else checksum, dest)
                def run(argv, *args, **kw):
                    seen.append(list(argv)); return locking_run(argv, *args, **kw)
                with mock.patch.object(updates, "release_snapshot", side_effect=[snapshot, snapshot]), mock.patch.object(updates, "bounded_download", side_effect=download), \
                        mock.patch.object(updates, "_validate_fixed_ctl"), mock.patch.object(updates, "backup_destination", return_value=("/media/backup/backups", "not_mounted")), \
                        mock.patch.object(updates, "ctl_supports", return_value=True), mock.patch.object(updates, "run_command", side_effect=run):
                    with self.assertRaisesRegex(ValueError, "turned off"):
                        updates.run_update(request, operation, root)
                self.assertFalse(any(c[-1] == "--start" or c[:2] == [updates.FIXED_CTL, "backup"] for c in seen))

    def test_worker_refuses_invalid_or_locked_choices_before_downloading(self):
        for backup, env_text in (({"mode": "format_disk"}, ""), ({"mode": "mount_drive", "uuid": "../../dev/sda1"}, ""), ({"mode": "mount_drive"}, ""),
                                 ({"mode": "skip"}, ""), ({"mode": "skip", "confirm": "yes"}, ""), ({"mode": "mount_fstab", "device": "/dev/sda1"}, ""),
                                 ({"mode": "skip", "confirm": "install-without-backup"}, "VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=false\n"),
                                 ({"mode": "system_disk_once", "confirm": "system-disk-once"}, "export VESSELSTACK_BACKUP_ALLOW_UNMOUNTED='false'\n"),
                                 ("skip", ""), ([], "")):
            with self.subTest(backup=backup, env=env_text):
                operation, calls, error, mocks = self._run_choice(backup, env_text=env_text)
                self.assertIsInstance(error, ValueError)
                self.assertEqual(calls, [])
                mocks["downloaded"].assert_not_called()

    def test_choice_falls_back_to_the_default_when_the_drive_is_mounted_now(self):
        for backup in ({"mode": "mount_fstab"}, {"mode": "system_disk_once", "confirm": "system-disk-once"}, {"mode": "skip", "confirm": "install-without-backup"}):
            with self.subTest(backup=backup):
                operation, calls, error, _ = self._run_choice(backup, mount="mounted")
                self.assertIsNone(error)
                self.assertIn([updates.FIXED_CTL, "backup", "--require-mounted"], calls)
                self.assertFalse(any("backup-drive" in c or "--system-disk-once" in c for c in calls))
                self.assertNotIn("backup_skipped", operation)
                self.assertNotIn("backup_mode", operation, "the record never claims a handling that was not used")

    def test_older_control_binary_is_reported_before_anything_changes(self):
        for backup in ({"mode": "mount_fstab"}, {"mode": "system_disk_once", "confirm": "system-disk-once"}):
            with self.subTest(backup=backup):
                operation, calls, error, _ = self._run_choice(backup, supports=False)
                self.assertIsInstance(error, ValueError)
                self.assertIn("older than this Control Panel", str(error))
                self.assertFalse(any(c[:2] == [updates.FIXED_CTL, "backup"] or c[-1] == "--start" for c in calls))

    def test_normalize_backup_choice(self):
        normalize = updates.normalize_backup_choice
        self.assertIsNone(normalize(None, False))
        self.assertIsNone(normalize({"mode": "require_mounted"}, True))
        self.assertEqual(normalize({"mode": "mount_drive", "uuid": "AB12-CD34"}, True), {"mode": "mount_drive", "uuid": "AB12-CD34"})
        self.assertEqual(normalize({"mode": "skip", "confirm": "install-without-backup"}, False)["mode"], "skip")
        for bad in ({"mode": "require_mounted", "uuid": "x"}, {"mode": "mount_fstab", "confirm": "x"}, {"mode": "mount_drive", "uuid": "a" * 65}, {"mode": "mount_drive", "uuid": 5}, {}, {"mode": None}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize(bad, False)

    def test_backup_lock_matches_the_shell_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "vesselstack.env"
            for text, expected in (("", False), ("VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=true\n", False), ("VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=false\n", True),
                                   ("export VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=\"false\"\n", True), ("# VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=false\n", False)):
                config.write_text(text)
                self.assertIs(updates.backup_locked(config), expected, text)
            self.assertIs(updates.backup_locked(Path(directory) / "missing.env"), False)

    def test_start_detached_stores_the_choice_only_when_given(self):
        for backup in (None, {"mode": "mount_fstab"}):
            with self.subTest(backup=backup), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); snapshot = {"version": "0.6.0"}
                kwargs = {"backup": backup} if backup else {}
                with mock.patch.object(updates, "release_snapshot", return_value=snapshot), mock.patch.object(updates, "assert_snapshot_unchanged"), mock.patch.object(updates.subprocess, "run"):
                    operation = updates.start_detached("update-release", root, {"boot_id": "old", "running_version": "0.5.3"}, 9, "lab", snapshot=snapshot, **kwargs)
                request = json.loads(next((root / "var/lib/vesselstack").glob("browser-update-*/request.json")).read_text())
                self.assertEqual(request.get("backup"), backup)
                self.assertEqual(operation.get("backup_mode"), backup["mode"] if backup else None)
                self.assertEqual("backup" in request, backup is not None)


if __name__ == "__main__":
    unittest.main()
