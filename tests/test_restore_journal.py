"""Unit tests for scripts/lib/restore_journal.py (the restore journal's state record)."""
from pathlib import Path
from unittest import mock
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

HELPER = Path(__file__).resolve().parents[1] / "scripts/lib/restore_journal.py"


def load_module():
    spec = importlib.util.spec_from_file_location("restore_journal_under_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setUpModule():
    # The older tests compare whole state files: point the helper at a boot id file that does not exist
    # (so no boot_id is written) instead of the host's /proc. BootIdTests choose their own file.
    global _MISSING
    _MISSING = tempfile.TemporaryDirectory()
    os.environ["VESSELSTACK_BOOT_ID_FILE"] = str(Path(_MISSING.name) / "no-such-boot-id")


def tearDownModule():
    os.environ.pop("VESSELSTACK_BOOT_ID_FILE", None)
    _MISSING.cleanup()


def run(*args):
    return subprocess.run([sys.executable, "-I", str(HELPER), *args], capture_output=True, text=True)


class RestoreJournalTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.dir = Path(self.td.name)

    def test_set_creates_state_and_get_reads_it(self):
        self.assertEqual(run("set", str(self.dir), "phase=prepared", "id=ab12").returncode, 0)
        self.assertEqual((self.dir / "state").read_text(), "phase=prepared\nid=ab12\n")
        self.assertEqual(oct((self.dir / "state").stat().st_mode & 0o777), "0o600")
        result = run("get", str(self.dir), "phase")
        self.assertEqual((result.returncode, result.stdout), (0, "prepared\n"))
        self.assertFalse((self.dir / "state.tmp").exists())

    def test_set_updates_only_the_named_keys_and_keeps_order(self):
        run("set", str(self.dir), "a=1", "b=2")
        run("set", str(self.dir), "b=3", "c=4")
        self.assertEqual((self.dir / "state").read_text(), "a=1\nb=3\nc=4\n")
        self.assertEqual(run("dump", str(self.dir)).stdout, "a=1\nb=3\nc=4\n")

    def test_value_may_contain_equals_signs_spaces_and_be_empty(self):
        run("set", str(self.dir), "last_error=x = y z", "empty=")
        self.assertEqual(run("get", str(self.dir), "last_error").stdout, "x = y z\n")
        self.assertEqual(run("get", str(self.dir), "empty").stdout, "\n")

    def test_missing_key_and_missing_state_file(self):
        self.assertEqual(run("get", str(self.dir), "phase").returncode, 1)
        self.assertEqual(run("dump", str(self.dir)).stdout, "")
        run("set", str(self.dir), "a=1")
        result = run("get", str(self.dir), "nope")
        self.assertEqual((result.returncode, result.stdout), (1, ""))

    def test_key_and_value_validation_changes_nothing(self):
        run("set", str(self.dir), "a=1")
        before = (self.dir / "state").read_text()
        for bad in ("Phase=x", "ph-ase=x", "1=x", "=x", "nokeyvalue", "a b=x", "a1=x"):
            result = run("set", str(self.dir), "good=1", bad)
            self.assertEqual(result.returncode, 2, bad)
            self.assertEqual((self.dir / "state").read_text(), before, bad)
        result = run("set", str(self.dir), "a=line1\nline2")
        self.assertEqual(result.returncode, 2)
        self.assertEqual((self.dir / "state").read_text(), before)
        self.assertEqual(run("get", str(self.dir), "Bad Key").returncode, 2)
        module = load_module()
        with self.assertRaises(module.StateError):
            module.validate_value("a\x00b")

    def test_state_is_never_evaluated(self):
        marker = self.dir / "pwned"
        run("set", str(self.dir), f"last_error=$(touch {marker}); `touch {marker}`")
        run("get", str(self.dir), "last_error")
        run("dump", str(self.dir))
        self.assertFalse(marker.exists())

    def test_corrupt_state_file_is_reported_and_not_overwritten(self):
        (self.dir / "state").write_text("phase=ok\nthis is not a record\n")
        result = run("set", str(self.dir), "a=1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("corrupt", result.stderr)
        self.assertEqual((self.dir / "state").read_text(), "phase=ok\nthis is not a record\n")

    def test_failed_replace_leaves_the_old_content_and_no_temp_file(self):
        module = load_module()
        module.set_values(str(self.dir), ["phase=old"])
        with mock.patch.object(module.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                module.set_values(str(self.dir), ["phase=new"])
        self.assertEqual((self.dir / "state").read_text(), "phase=old\n")
        self.assertFalse((self.dir / "state.tmp").exists())

    def test_failed_write_before_replace_leaves_the_old_content(self):
        module = load_module()
        module.set_values(str(self.dir), ["phase=old"])
        with mock.patch.object(module.os, "fsync", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                module.set_values(str(self.dir), ["phase=new"])
        self.assertEqual((self.dir / "state").read_text(), "phase=old\n")
        self.assertFalse((self.dir / "state.tmp").exists())

    def test_file_and_directory_are_fsynced_after_the_replace(self):
        module = load_module()
        calls = []
        real_fsync, real_replace = os.fsync, os.replace
        with mock.patch.object(module.os, "fsync", side_effect=lambda fd: (calls.append("fsync"), real_fsync(fd))[1]), \
             mock.patch.object(module.os, "replace", side_effect=lambda a, b: (calls.append("replace"), real_replace(a, b))[1]):
            module.set_values(str(self.dir), ["phase=x"])
        self.assertEqual(calls, ["fsync", "replace", "fsync"])

    def test_inode_reports_dev_ino_without_following_a_final_symlink(self):
        target = self.dir / "target"; target.mkdir()
        link = self.dir / "link"; link.symlink_to(target)
        st, lst = os.stat(target), os.lstat(link)
        self.assertEqual(run("inode", str(target)).stdout, f"{st.st_dev}:{st.st_ino}\n")
        self.assertEqual(run("inode", str(link)).stdout, f"{lst.st_dev}:{lst.st_ino}\n")
        self.assertNotEqual(run("inode", str(link)).stdout, run("inode", str(target)).stdout)
        self.assertEqual(run("inode", str(self.dir / "missing")).stdout, "absent\n")
        dangling = self.dir / "dangling"; dangling.symlink_to(self.dir / "nowhere")
        self.assertRegex(run("inode", str(dangling)).stdout, r"^\d+:\d+\n$")

    def test_inode_survives_a_rename(self):
        tree = self.dir / "a"; tree.mkdir()
        before = run("inode", str(tree)).stdout
        os.rename(tree, self.dir / "b")
        self.assertEqual(run("inode", str(self.dir / "b")).stdout, before)
        self.assertEqual(run("inode", str(tree)).stdout, "absent\n")

    def test_fsync_dir(self):
        self.assertEqual(run("fsync-dir", str(self.dir)).returncode, 0)
        self.assertEqual(run("fsync-dir", str(self.dir), str(self.dir)).returncode, 0)
        self.assertNotEqual(run("fsync-dir", str(self.dir / "missing")).returncode, 0)

    # ---- duplicate keys ----
    def test_a_repeated_key_makes_the_state_corrupt_for_get_dump_and_set(self):
        (self.dir / "state").write_text("phase=prepared\nid=1\nphase=committed\n")
        for args in (("get", str(self.dir), "phase"), ("dump", str(self.dir)), ("set", str(self.dir), "a=1")):
            result = run(*args)
            self.assertEqual(result.returncode, 2, args)
            self.assertIn("more than once", result.stderr); self.assertEqual(result.stdout, "")
        self.assertEqual((self.dir / "state").read_text(), "phase=prepared\nid=1\nphase=committed\n")

    # ---- fsync-tree ----
    def test_fsync_tree_flushes_files_and_directories_and_accepts_a_single_file(self):
        module = load_module()
        (self.dir / "a/b").mkdir(parents=True); (self.dir / "a/f").write_text("1"); (self.dir / "a/b/g").write_text("2")
        opened = []
        real_fsync = os.fsync
        stats = []
        def spy(fd):
            stats.append(os.fstat(fd).st_mode); return real_fsync(fd)
        with mock.patch.object(module.os, "fsync", side_effect=spy):
            module.fsync_tree(str(self.dir / "a"))
        import stat as stat_module
        self.assertEqual(sum(1 for m in stats if stat_module.S_ISREG(m)), 2)
        self.assertEqual(sum(1 for m in stats if stat_module.S_ISDIR(m)), 2)   # a and a/b
        self.assertEqual(run("fsync-tree", str(self.dir / "a")).returncode, 0)
        self.assertEqual(run("fsync-tree", str(self.dir / "a/f"), str(self.dir / "a/b")).returncode, 0)

    def test_fsync_tree_does_not_follow_symlinks_and_skips_special_files(self):
        module = load_module()
        outside = self.dir / "outside"; outside.mkdir(); (outside / "secret").write_text("x")
        tree = self.dir / "tree"; tree.mkdir()
        (tree / "dirlink").symlink_to(outside, target_is_directory=True)
        (tree / "filelink").symlink_to(outside / "secret")
        (tree / "dangling").symlink_to(self.dir / "nowhere")
        os.mkfifo(tree / "pipe")          # opening a FIFO for reading would block forever
        opened = []
        real_open = os.open
        def spy(path, flags, *args, **kwargs):
            opened.append(str(path)); return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(module.os, "open", side_effect=spy):
            module.fsync_tree(str(tree))
        self.assertEqual(opened, [str(tree)])
        self.assertEqual(run("fsync-tree", str(tree)).returncode, 0)

    def test_fsync_tree_refuses_a_symlink_as_the_top_and_reports_a_missing_path(self):
        link = self.dir / "link"; link.symlink_to(self.dir)
        result = run("fsync-tree", str(link))
        self.assertEqual(result.returncode, 1); self.assertIn("symbolic link", result.stderr)
        result = run("fsync-tree", str(self.dir / "missing"))
        self.assertEqual(result.returncode, 1); self.assertIn("missing", result.stderr)
        self.assertEqual(run("fsync-tree").returncode, 2)

    def test_fsync_tree_tolerates_filesystems_without_directory_fsync_but_not_other_errors(self):
        module = load_module()
        (self.dir / "t").mkdir(); (self.dir / "t/f").write_text("x")
        real_fsync = os.fsync
        import stat as stat_module
        def unsupported(fd):
            if stat_module.S_ISDIR(os.fstat(fd).st_mode): raise OSError(module.errno.EINVAL, "no directory fsync")
            return real_fsync(fd)
        with mock.patch.object(module.os, "fsync", side_effect=unsupported):
            module.fsync_tree(str(self.dir / "t"))
        def broken(fd): raise OSError(module.errno.EIO, "disk")
        with mock.patch.object(module.os, "fsync", side_effect=broken):
            with self.assertRaises(OSError):
                module.fsync_tree(str(self.dir / "t"))

    # ---- identity that survives a reboot ----
    def test_ino_prints_only_the_inode_number(self):
        target = self.dir / "target"; target.mkdir()
        self.assertEqual(run("ino", str(target)).stdout, f"{os.stat(target).st_ino}\n")
        self.assertEqual(run("ino", str(self.dir / "missing")).stdout, "absent\n")
        link = self.dir / "link"; link.symlink_to(target)
        self.assertEqual(run("ino", str(link)).stdout, f"{os.lstat(link).st_ino}\n")
        self.assertEqual(run("ino").returncode, 2)

    def test_same_fs_compares_a_path_with_its_parent_directory(self):
        (self.dir / "tree").mkdir()
        self.assertEqual(run("same-fs", str(self.dir / "tree")).stdout, "same\n")
        self.assertEqual(run("same-fs", str(self.dir / "missing")).stdout, "absent\n")
        module = load_module()
        real_lstat = os.lstat
        parent = str(self.dir)
        def fake(path, *args, **kwargs):
            st = real_lstat(path, *args, **kwargs)
            if str(path) == str(self.dir / "tree"):
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1, st.st_nlink, st.st_uid, st.st_gid, st.st_size, 0, 0, 0))
            return st
        with mock.patch.object(module.os, "lstat", side_effect=fake):
            self.assertEqual(module.same_fs(str(self.dir / "tree")), "different")
            self.assertEqual(module.same_fs(parent), "same")

    def test_a_regular_file_in_the_way_is_reported_as_notdir_not_as_an_error(self):
        (self.dir / "afile").write_text("x")
        self.assertEqual(run("same-fs", str(self.dir / "afile")).stdout, "notdir\n")
        result = run("inspect", str(self.dir / "afile"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split()[1], "notdir")
        self.assertEqual(result.stderr, "")

    def test_resolve_follows_symbolic_links_and_accepts_missing_paths(self):
        real = self.dir / "real"; real.mkdir()
        (self.dir / "link").symlink_to(real, target_is_directory=True)
        result = run("resolve", str(self.dir / "link" / "x"), str(self.dir / "nothing"))
        self.assertEqual(result.stdout.splitlines(), [os.path.realpath(real / "x"), os.path.realpath(self.dir / "nothing")])
        self.assertEqual(run("resolve").returncode, 2)

    def test_inspect_gives_the_inode_and_the_filesystem_check_in_one_call(self):
        (self.dir / "tree").mkdir()
        st = os.lstat(self.dir / "tree")
        self.assertEqual(run("inspect", str(self.dir / "tree")).stdout, f"{st.st_dev}:{st.st_ino} same\n")
        self.assertEqual(run("inspect", str(self.dir / "missing")).stdout, "absent absent\n")

    def test_same_fs_uses_the_real_parent_not_the_one_named_in_the_path(self):
        # /srv -> /mnt/ssd/srv: the tree is compared with the directory that really holds it (PATH/..)
        real = self.dir / "real"; (real / "tree").mkdir(parents=True)
        (self.dir / "link").symlink_to(real, target_is_directory=True)
        self.assertEqual(run("same-fs", str(self.dir / "link" / "tree")).stdout, "same\n")
        self.assertEqual(run("inspect", str(self.dir / "link" / "tree")).stdout.split()[1], "same")
        module = load_module()
        real_stat = os.stat
        seen = []
        def fake(path, *args, **kwargs):
            seen.append(str(path))
            st = real_stat(path, *args, **kwargs)
            if str(path).endswith("/.."):
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1, st.st_nlink, st.st_uid, st.st_gid, st.st_size, 0, 0, 0))
            return st
        with mock.patch.object(module.os, "stat", side_effect=fake):
            self.assertEqual(module.same_fs(str(self.dir / "link" / "tree")), "different")
        self.assertIn(str(self.dir / "link" / "tree" / ".."), seen)

    # ---- rename: "mv -T" without the copy fallback ----
    def test_rename_moves_to_a_new_name_or_onto_an_empty_directory(self):
        (self.dir / "a").mkdir(); (self.dir / "a" / "f").write_text("x")
        self.assertEqual(run("rename", str(self.dir / "a"), str(self.dir / "b")).returncode, 0)
        self.assertEqual((self.dir / "b" / "f").read_text(), "x"); self.assertFalse((self.dir / "a").exists())
        (self.dir / "empty").mkdir()
        self.assertEqual(run("rename", str(self.dir / "b"), str(self.dir / "empty")).returncode, 0)
        self.assertEqual((self.dir / "empty" / "f").read_text(), "x")
        self.assertFalse((self.dir / "empty" / "b").exists(), "must not be nested into the directory")

    def test_rename_refuses_a_destination_that_is_not_absent_or_an_empty_directory(self):
        (self.dir / "a").mkdir()
        (self.dir / "full").mkdir(); (self.dir / "full" / "f").write_text("keep")
        (self.dir / "file").write_text("keep")
        (self.dir / "target").mkdir(); (self.dir / "link").symlink_to(self.dir / "target", target_is_directory=True)
        for name in ("full", "file", "link"):
            with self.subTest(name):
                result = run("rename", str(self.dir / "a"), str(self.dir / name))
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertTrue((self.dir / "a").is_dir(), "the source stays")
        self.assertEqual((self.dir / "full" / "f").read_text(), "keep")
        self.assertEqual((self.dir / "file").read_text(), "keep")
        self.assertEqual(os.listdir(self.dir / "target"), [])
        self.assertEqual(run("rename", str(self.dir / "missing"), str(self.dir / "x")).returncode, 1)
        (self.dir / "plain").write_text("p"); (self.dir / "emptydir").mkdir()
        self.assertEqual(run("rename", str(self.dir / "plain"), str(self.dir / "emptydir")).returncode, 1)
        self.assertEqual(run("rename", str(self.dir / "a")).returncode, 2)

    def test_rename_across_filesystems_fails_and_never_copies(self):
        module = load_module()
        (self.dir / "a").mkdir(); (self.dir / "a" / "f").write_text("x")
        def exdev(src, dst):
            raise OSError(module.errno.EXDEV, "Invalid cross-device link", src)
        with mock.patch.object(module.os, "rename", side_effect=exdev):
            with self.assertRaises(OSError) as caught:
                module.rename_tree(str(self.dir / "a"), str(self.dir / "b"))
            self.assertEqual(caught.exception.errno, module.errno.EXDEV)
            self.assertEqual(module.main(["x", "rename", str(self.dir / "a"), str(self.dir / "b")]), 1)
        self.assertEqual((self.dir / "a" / "f").read_text(), "x", "the source is untouched")
        self.assertFalse((self.dir / "b").exists(), "nothing was copied")
        self.assertNotIn("shutil", HELPER.read_text(), "the helper has no copy code to fall back on")

    def test_usage_errors(self):
        self.assertEqual(run().returncode, 2)
        self.assertEqual(run("set", str(self.dir)).returncode, 2)
        self.assertEqual(run("frobnicate").returncode, 2)

class BootIdTests(unittest.TestCase):
    BOOT_A = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    BOOT_B = "11111111-2222-4333-8444-555555555555"

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.dir = Path(self.td.name) / "journal"
        self.dir.mkdir()
        self.boot_file = Path(self.td.name) / "boot_id"
        self.boot_file.write_text(self.BOOT_A + "\n")

    def run_set(self, *assignments):
        env = dict(os.environ, VESSELSTACK_BOOT_ID_FILE=str(self.boot_file))
        return subprocess.run([sys.executable, "-I", str(HELPER), "set", str(self.dir), *assignments],
                              capture_output=True, text=True, env=env)

    def state(self):
        return (self.dir / "state").read_text()

    def test_set_records_the_boot_id_and_leaves_other_keys_alone(self):
        self.assertEqual(self.run_set("phase=prepared", "last_error=").returncode, 0)
        self.assertEqual(self.state(), f"phase=prepared\nlast_error=\nboot_id={self.BOOT_A}\n")

    def test_a_new_boot_rewrites_the_boot_id(self):
        self.run_set("phase=prepared")
        self.boot_file.write_text(self.BOOT_B + "\n")
        self.run_set("phase=stopping")
        self.assertEqual(self.state(), f"phase=stopping\nboot_id={self.BOOT_B}\n")

    def test_a_missing_or_malformed_boot_id_file_removes_the_key(self):
        for name, content in (("missing", None), ("empty", ""), ("not a uuid", "hello\n"),
                              ("upper case", self.BOOT_A.upper() + "\n"), ("two lines", self.BOOT_A + "\nx\n")):
            with self.subTest(name):
                self.boot_file.write_text(self.BOOT_A + "\n")
                self.assertEqual(self.run_set("phase=prepared").returncode, 0)
                self.assertIn("boot_id=", self.state())
                if content is None:
                    self.boot_file.unlink()
                else:
                    self.boot_file.write_text(content)
                self.assertEqual(self.run_set("phase=stopping").returncode, 0)
                self.assertEqual(self.state(), "phase=stopping\n")

    def test_an_explicit_boot_id_is_refused_and_changes_nothing(self):
        self.run_set("phase=prepared")
        before = self.state()
        result = self.run_set(f"boot_id={self.BOOT_B}", "phase=stopping")
        self.assertEqual(result.returncode, 2)
        self.assertIn("boot_id", result.stderr)
        self.assertEqual(self.state(), before)

    def test_the_default_source_is_the_kernel_file(self):
        module = load_module()
        self.assertEqual(module.BOOT_ID_FILE, "/proc/sys/kernel/random/boot_id")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VESSELSTACK_BOOT_ID_FILE", None)
            with mock.patch("builtins.open", side_effect=FileNotFoundError):
                self.assertIsNone(module.current_boot_id())


if __name__ == "__main__":
    unittest.main()
