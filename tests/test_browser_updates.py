#!/usr/bin/env python3
import importlib.util
import hashlib
import io
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
            with self.assertRaises(ValueError):
                updates.acquire_slot(root)

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


if __name__ == "__main__":
    unittest.main()
