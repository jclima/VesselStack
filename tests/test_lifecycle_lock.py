import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from common.lifecycle_lock import (ENV_FD, LifecycleLock, LifecycleLockBusy,
                                   LifecycleLockError, lock_path)


ROOT = Path(__file__).resolve().parents[1]


class LifecycleLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.install = self.base / "opt" / "vesselstack"
        self.install.mkdir(parents=True)
        self.system = self.base / "system"
        self.system.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_identity_is_lexically_canonical(self):
        self.assertEqual(lock_path(self.install, self.system),
                         lock_path(self.install / ".", self.system))
        # The hash is based on the supplied lexical path, not resolve(), which
        # would make identity depend on whether a path currently exists.
        self.assertNotEqual(lock_path(self.install / "missing", self.system),
                            lock_path(self.install / "other", self.system))

    def test_busy_competing_process_does_not_write_sentinel(self):
        sentinel = self.base / "sentinel"
        code = (
            "import pathlib,sys; from common.lifecycle_lock import LifecycleLock; "
            "p=pathlib.Path(sys.argv[1]); "
            "\ntry:\n LifecycleLock(sys.argv[2],sys.argv[3]).acquire()\n p.write_text('bad')\nexcept Exception as e:\n print(type(e).__name__)\n"
        )
        with LifecycleLock(self.install, self.system):
            result = subprocess.run([sys.executable, "-c", code, str(sentinel),
                                     str(self.install), str(self.system)],
                                    cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("LifecycleLockBusy", result.stdout)
        self.assertFalse(sentinel.exists())

    def test_inherited_descriptor_and_parent_ownership(self):
        with LifecycleLock(self.install, self.system) as parent:
            code = "from common.lifecycle_lock import LifecycleLock; LifecycleLock(__import__('sys').argv[1], __import__('sys').argv[2]).acquire(); print('ok')"
            child = subprocess.run([sys.executable, "-c", code, str(self.install),
                                    str(self.system)], cwd=ROOT, env=parent.child_env(),
                                   pass_fds=parent.pass_fds(), text=True,
                                   capture_output=True)
            self.assertEqual(child.stdout.strip(), "ok")
            self.assertEqual(child.returncode, 0)
            # The inherited child must not unlock the parent's open file.
            contender = subprocess.run([sys.executable, "-c", code, str(self.install),
                                        str(self.system)], cwd=ROOT,
                                       env={k: v for k, v in os.environ.items()
                                            if k != ENV_FD}, text=True,
                                       capture_output=True)
            self.assertNotEqual(contender.returncode, 0)
            self.assertIn("LifecycleLockBusy", contender.stderr + contender.stdout)

    def test_bogus_unrelated_and_closed_descriptors_are_rejected(self):
        unrelated = os.open(self.base / "unrelated", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.environ[ENV_FD] = str(unrelated)
            with self.assertRaises(LifecycleLockError):
                LifecycleLock(self.install, self.system).acquire()
        finally:
            os.close(unrelated)
            os.environ.pop(ENV_FD, None)
        fd = os.open(self.base / "closed", os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        os.environ[ENV_FD] = str(fd)
        try:
            with self.assertRaises(LifecycleLockError):
                LifecycleLock(self.install, self.system).acquire()
        finally:
            os.environ.pop(ENV_FD, None)

    def test_inherited_descriptor_must_be_at_least_three(self):
        os.environ[ENV_FD] = "2"
        try:
            with self.assertRaises(LifecycleLockError):
                LifecycleLock(self.install, self.system).acquire()
        finally:
            os.environ.pop(ENV_FD, None)

    def test_symlink_in_fixed_lock_directory_is_rejected(self):
        run = self.system / "run"
        run.mkdir()
        (run / "lock").symlink_to(self.base / "elsewhere", target_is_directory=True)
        with self.assertRaises(LifecycleLockError):
            LifecycleLock(self.install, self.system).acquire()

    def test_empty_system_root_is_absolute(self):
        path = lock_path(self.install, "")
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.parent, Path("/run/lock/vesselstack"))

    def test_cli_run_and_check_handoff(self):
        cli = ROOT / "common" / "lifecycle_lock.py"
        command = [sys.executable, str(cli), "run", "--root", str(self.install),
                   "--system-root", str(self.system), "--", sys.executable, "-c",
                   "import os,sys; os.execv(sys.executable,[sys.executable,sys.argv[1],'check','--root',sys.argv[2],'--system-root',sys.argv[3]])",
                   str(cli), str(self.install), str(self.system)]
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

        # A separate process remains excluded while the CLI command is held.
        ready = self.base / "ready"
        held = subprocess.Popen([sys.executable, str(cli), "run", "--root",
                                 str(self.install), "--system-root", str(self.system),
                                 "--", sys.executable, "-c",
                                 "import pathlib,time; pathlib.Path(__import__('sys').argv[1]).write_text('ready'); time.sleep(1)",
                                 str(ready)])
        try:
            for _ in range(50):
                if ready.exists():
                    break
                time.sleep(0.02)
            self.assertTrue(ready.exists())
            result = subprocess.run([sys.executable, str(cli), "run", "--root",
                                     str(self.install), "--system-root", str(self.system),
                                     "--", sys.executable, "-c", "raise SystemExit(9)"],
                                    cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(result.returncode, 75)
            self.assertIn("busy", result.stderr)
        finally:
            held.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
