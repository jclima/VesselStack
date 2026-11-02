import base64
import hashlib
import importlib.util
import io
import json
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HELPER = Path(__file__).parents[1] / "scripts/preload-signalk-vesseltwin.py"
spec = importlib.util.spec_from_file_location("preload_signalk_vesseltwin", HELPER)
preload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preload)


def archive(members):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as bundle:
        for name, data, kind in members:
            entry = tarfile.TarInfo(name)
            if kind == "dir":
                entry.type = tarfile.DIRTYPE
            elif kind == "symlink":
                entry.type = tarfile.SYMTYPE
                entry.linkname = data
            elif kind == "hardlink":
                entry.type = tarfile.LNKTYPE
                entry.linkname = data
            else:
                entry.size = len(data)
            bundle.addfile(entry, None if kind in ("dir", "symlink", "hardlink") else io.BytesIO(data))
    return output.getvalue()


def valid_archive(extra=()):
    return archive([
        ("package/", b"", "dir"),
        ("package/package.json", b'{"name":"signalk-vesseltwin","version":"0.2.3"}', "file"),
        *extra,
    ])


class PreloadTests(unittest.TestCase):
    def setUp(self):
        self.original_hash = preload.SHA512

    def tearDown(self):
        preload.SHA512 = self.original_hash
        os.chdir(Path(__file__).parents[1])

    def pin(self, raw):
        preload.SHA512 = base64.b64encode(hashlib.sha512(raw).digest()).decode()

    def data_tree(self, root):
        (root / "signalk" / "node_modules").mkdir(parents=True)

    def run_helper(self, data, *extra):
        return subprocess.run([sys.executable, str(HELPER), "--mode", "docker", "--data", str(data), *extra], text=True, capture_output=True)

    def test_offline_missing_fails_without_creating_data(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "data"
            self.assertNotEqual(self.run_helper(data, "--offline", "--check").returncode, 0)
            self.assertFalse(data.exists())

    def test_offline_existing_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "data/signalk/node_modules/signalk-vesseltwin"
            target.mkdir(parents=True)
            (target / "package.json").write_text('{"name":"signalk-vesseltwin","version":"9.9.9"}')
            result = self.run_helper(Path(temp) / "data", "--offline", "--check")
            self.assertEqual(result.returncode, 0, result.stderr)

    def publish(self, raw, root, rename=None):
        self.pin(raw)
        args = types.SimpleNamespace(data=root, uid=os.getuid(), gid=os.getgid())
        fallback = (lambda source, destination, _fd: os.rename(source, destination))
        with mock.patch.object(preload, "rename_noreplace", rename or fallback), mock.patch.object(preload.os, "geteuid", return_value=501):
            return preload.publish(raw, args, root / "signalk/node_modules/signalk-vesseltwin")

    def test_prepare_then_publish_valid_archive_and_empty_file(self):
        raw = valid_archive((("package/empty.txt", b"", "file"),))
        self.pin(raw)
        with tempfile.TemporaryDirectory() as temp:
            root, stage = Path(temp) / "data", Path(temp) / "stage"
            self.data_tree(root)
            stage.mkdir()
            (Path(temp) / "input.tgz").write_bytes(raw)
            with mock.patch.object(preload, "fetch_archive", return_value=raw), mock.patch.object(sys, "argv", ["preload", "--mode", "docker", "--data", str(root), "--stage", str(stage), "--prepare"]):
                preload.main()
            self.publish((stage / "plugin.tgz").read_bytes(), root)
            target = root / "signalk/node_modules/signalk-vesseltwin"
            self.assertTrue((target / "empty.txt").is_file())
            self.assertEqual((target / "empty.txt").read_bytes(), b"")

    def test_hash_mismatch_rejects_without_writes(self):
        raw = valid_archive()
        preload.SHA512 = "wrong"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.data_tree(root)
            with self.assertRaises(SystemExit):
                preload.validate(raw)
            self.assertFalse((root / "signalk/node_modules/signalk-vesseltwin").exists())

    def test_rejects_traversal_link_duplicate_and_dependencies(self):
        cases = [
            valid_archive((("package/../escape", b"x", "file"),)),
            valid_archive((("package/link", "/etc/passwd", "symlink"),)),
            archive([("package/", b"", "dir"), ("package/package.json", b'{"name":"signalk-vesseltwin","version":"0.2.3"}', "file"), ("package/a", b"1", "file"), ("package/a", b"2", "file")]),
            archive([("package/", b"", "dir"), ("package/package.json", b'{"name":"signalk-vesseltwin","version":"0.2.3","dependencies":{"x":"1"}}', "file")]),
        ]
        for raw in cases:
            with self.subTest():
                self.pin(raw)
                with self.assertRaises(SystemExit):
                    preload.validate(raw)

    def test_existing_valid_plugin_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "signalk/node_modules/signalk-vesseltwin"
            target.mkdir(parents=True)
            (target / "package.json").write_text('{"name":"signalk-vesseltwin","version":"9.9.9"}')
            self.assertTrue(preload.existing(target))
            self.assertEqual(json.loads((target / "package.json").read_text())["version"], "9.9.9")

    def test_publish_missing_stage_never_fetches(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(preload, "fetch_archive", side_effect=AssertionError("network used")):
                with self.assertRaises(SystemExit):
                    preload.load_staged(Path(temp) / "missing")

    def test_no_replace_collision_validates_existing_winner(self):
        raw = valid_archive()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.data_tree(root)
            target = root / "signalk/node_modules/signalk-vesseltwin"

            def collide(source, destination, fd):
                target.mkdir()
                (target / "package.json").write_text('{"name":"signalk-vesseltwin","version":"9.9.9"}')
                raise FileExistsError(destination)

            self.assertFalse(self.publish(raw, root, collide))
            self.assertEqual(json.loads((target / "package.json").read_text())["version"], "9.9.9")

    def test_publish_reports_whether_it_installed_something(self):
        raw = valid_archive()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.data_tree(root)
            self.assertTrue(self.publish(raw, root))
            self.assertFalse(self.publish(raw, root))

    def test_installer_restarts_docker_signalk_only_after_fresh_install(self):
        script = (Path(__file__).parents[1] / "install.sh").read_text()
        self.assertIn("|| preload_rc=$?", script)
        self.assertIn("10) SIGNALK_PLUGIN_INSTALLED=1", script)
        self.assertIn('[ "$SIGNALK_MODE" = docker ] && [ "$SIGNALK_PLUGIN_INSTALLED" -eq 1 ]', script)
        self.assertIn("compose restart signalk", script)
        self.assertEqual(preload.EXIT_INSTALLED, 10)

    def test_main_exit_status_signals_fresh_install_only(self):
        raw = valid_archive()
        self.pin(raw)
        with tempfile.TemporaryDirectory() as temp:
            root, stage = Path(temp) / "data", Path(temp) / "stage"
            self.data_tree(root)
            stage.mkdir()
            (stage / "plugin.tgz").write_bytes(raw)
            argv = ["x", "--mode", "docker", "--data", str(root), "--stage", str(stage), "--publish"]
            fallback = lambda source, destination, _fd: os.rename(source, destination)
            with mock.patch.object(preload.sys, "argv", argv), mock.patch.object(preload, "rename_noreplace", fallback), \
                    mock.patch.object(preload.os, "geteuid", return_value=501):
                self.assertEqual(preload.main(), preload.EXIT_INSTALLED)
                self.assertEqual(preload.main(), 0)

    def test_publish_creates_package_json_dependency(self):
        raw = valid_archive()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.data_tree(root)
            self.publish(raw, root)
            data = json.loads((root / "signalk/package.json").read_text())
            self.assertEqual(data["dependencies"], {"signalk-vesseltwin": "0.2.3"})
            self.assertEqual(os.listdir(root / "signalk"), ["node_modules", "package.json"])

    def test_publish_preserves_package_json_keys_and_is_idempotent(self):
        raw = valid_archive()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.data_tree(root)
            path = root / "signalk/package.json"
            path.write_text(json.dumps({"name": "cfg", "custom": {"a": 1}, "dependencies": {"other": "^1.0.0"}}))
            os.chmod(path, 0o640)
            self.publish(raw, root)
            data = json.loads(path.read_text())
            self.assertEqual(data["custom"], {"a": 1})
            self.assertEqual(data["dependencies"], {"other": "^1.0.0", "signalk-vesseltwin": "0.2.3"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            # A second pass (plugin now present, dependency listed) changes nothing.
            before = path.read_text()
            preload.ensure_dependency(os.open(root / "signalk", os.O_RDONLY | os.O_DIRECTORY))
            self.assertEqual(path.read_text(), before)

    def test_ensure_dependency_keeps_existing_entry_and_ignores_corrupt_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                path = root / "package.json"
                path.write_text('{"dependencies":{"signalk-vesseltwin":"^0.3.0"}}')
                preload.ensure_dependency(fd)
                self.assertEqual(json.loads(path.read_text())["dependencies"]["signalk-vesseltwin"], "^0.3.0")
                path.write_text("{not json")
                preload.ensure_dependency(fd)
                self.assertEqual(path.read_text(), "{not json")
                self.assertEqual(sorted(os.listdir(root)), ["package.json"])
            finally:
                os.close(fd)

    @unittest.skipUnless(platform.system() == "Linux", "renameat2 is Linux-only")
    def test_real_linux_noreplace_publication(self):
        raw = valid_archive()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.data_tree(root)
            self.pin(raw)
            args = types.SimpleNamespace(data=root, uid=os.getuid(), gid=os.getgid())
            with mock.patch.object(preload.os, "geteuid", return_value=1000):
                return preload.publish(raw, args, root / "signalk/node_modules/signalk-vesseltwin")
            self.assertTrue((root / "signalk/node_modules/signalk-vesseltwin/package.json").exists())

    @unittest.skipUnless(platform.system() == "Linux" and os.geteuid() == 0, "requires Linux root")
    def test_root_drop_custom_group_parent(self):
        # The helper drops the child permanently; keep the unittest runner root-owned.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.data_tree(root)
            raw = valid_archive()
            self.pin(raw)
            sentinel = root / "root-sentinel"
            sentinel.write_text("unchanged"); os.chmod(sentinel, 0o600)
            os.chown(root, 0, 2001); os.chmod(root, 0o750)
            os.chown(root / "signalk", 1000, 1000); os.chmod(root / "signalk", 0o750)
            os.chown(root / "signalk/node_modules", 1000, 1000); os.chmod(root / "signalk/node_modules", 0o750)
            code = ("import base64, importlib.util, os, pathlib, types; "
                    f"s=importlib.util.spec_from_file_location('p', {str(HELPER)!r}); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                    "m.SHA512=os.environ['PIN']; raw=base64.b64decode(os.environ['RAW']); "
                    f"m.publish(raw, types.SimpleNamespace(data=pathlib.Path({str(root)!r}), uid=1000, gid=1000), pathlib.Path({str(root / 'signalk/node_modules/signalk-vesseltwin')!r})); "
                    f"s=pathlib.Path({str(sentinel)!r}); "
                    "\ntry: s.write_text('changed')\nexcept PermissionError: pass\nelse: raise SystemExit('dropped user wrote root sentinel')")
            result = subprocess.run([sys.executable, "-c", code], env={**os.environ, "PIN": preload.SHA512, "RAW": base64.b64encode(raw).decode()}, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            target = root / "signalk/node_modules/signalk-vesseltwin"
            self.assertEqual(target.stat().st_uid, 1000)
            self.assertEqual(sentinel.read_text(), "unchanged")


if __name__ == "__main__":
    unittest.main()
