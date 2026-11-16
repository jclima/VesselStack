import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from unittest import mock

from common.lifecycle_lock import (ENV_FD, LifecycleLock, LifecycleLockBusy,
                                   LifecycleLockError, LifecycleLockPending, lock_path)


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


RESTORE_ID = "0123456789abcdef0123456789abcdef"
CLI = ROOT / "common" / "lifecycle_lock.py"


class LifecycleLockWaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.install = self.base / "opt" / "vesselstack"
        self.install.mkdir(parents=True)
        self.system = self.base / "system"
        self.system.mkdir()

    def run_cli(self, *extra, command=None):
        command = command or [sys.executable, "-c", "print('ran')"]
        return subprocess.run([sys.executable, str(CLI), "run", "--root", str(self.install),
                               "--system-root", str(self.system), *extra, "--", *command],
                              cwd=ROOT, text=True, capture_output=True, timeout=60)

    def test_wait_succeeds_when_the_lock_is_released_in_time(self):
        holder = LifecycleLock(self.install, self.system).acquire()
        timer = threading.Timer(1.0, holder.release)
        timer.start()
        try:
            started = time.monotonic()
            result = self.run_cli("--wait", "10")
            elapsed = time.monotonic() - started
        finally:
            timer.join()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ran")
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 8)

    def test_wait_gives_up_with_75_after_the_wait(self):
        with LifecycleLock(self.install, self.system):
            started = time.monotonic()
            result = self.run_cli("--wait", "1")
            elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 75)
        self.assertIn("busy", result.stderr)
        self.assertGreaterEqual(elapsed, 0.9)

    def test_default_is_not_to_wait(self):
        with LifecycleLock(self.install, self.system):
            started = time.monotonic()
            result = self.run_cli()
            self.assertLess(time.monotonic() - started, 0.4)
        self.assertEqual(result.returncode, 75)

    def test_a_status_75_from_the_command_itself_is_never_retried(self):
        counter = self.base / "runs"
        code = "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_text(p.read_text()+'x' if p.exists() else 'x'); raise SystemExit(75)"
        result = self.run_cli("--wait", "5", command=[sys.executable, "-c", code, str(counter)])
        self.assertEqual(result.returncode, 75)
        self.assertEqual(counter.read_text(), "x")

    def test_wait_argument_is_validated(self):
        for value in ("-1", "3601", "abc", "nan", "inf", ""):
            with self.subTest(value):
                result = self.run_cli("--wait", value)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("ran", result.stdout)
        for value in ("0", "0.5", "3600"):
            with self.subTest(value):
                self.assertEqual(self.run_cli("--wait", value).returncode, 0)


class PendingRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.install = self.base / "opt" / "vesselstack"
        self.install.mkdir(parents=True)
        self.system = self.base / "system"
        self.journal = self.system / "var/lib/vesselstack/restore/current"
        env = {k: v for k, v in os.environ.items() if k not in (ENV_FD, "VESSELSTACK_RESTORE_ID")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_state(self, text):
        self.journal.mkdir(parents=True, exist_ok=True)
        (self.journal / "state").write_text(text)

    def journal_with(self, phase, restore_id=RESTORE_ID):
        self.write_state(f"format=1\nid={restore_id}\nphase={phase}\nroot=/x\n")

    def acquire(self):
        lock = LifecycleLock(self.install, self.system)
        lock.acquire()
        lock.release()

    def test_every_unfinished_phase_refuses(self):
        for phase in ("prepared", "stopping", "stopped", "data-swap", "root-swap", "externals", "reloaded",
                      "starting", "rolling-back", "rollback-failed", "abandoned", "something-new"):
            with self.subTest(phase):
                self.journal_with(phase)
                with self.assertRaises(LifecycleLockPending) as caught:
                    self.acquire()
                self.assertIsInstance(caught.exception, LifecycleLockBusy)
                self.assertIn(f"a restore is unfinished; see: sudo {self.journal}/recover status", str(caught.exception))

    def test_a_refusal_does_not_keep_the_lock(self):
        self.journal_with("prepared")
        with self.assertRaises(LifecycleLockBusy):
            self.acquire()
        shutil.rmtree(self.journal)
        self.acquire()

    def test_committed_and_rolled_back_do_not_block(self):
        for phase in ("committed", "rolled-back"):
            with self.subTest(phase):
                self.journal_with(phase)
                self.acquire()

    def test_no_journal_does_not_block(self):
        self.acquire()

    def test_the_restores_own_id_is_let_through_and_another_is_not(self):
        self.journal_with("starting")
        with mock.patch.dict(os.environ, {"VESSELSTACK_RESTORE_ID": RESTORE_ID}):
            self.acquire()
        for wrong in ("f" * 32, "", RESTORE_ID + "0", "abc"):
            with self.subTest(wrong):
                with mock.patch.dict(os.environ, {"VESSELSTACK_RESTORE_ID": wrong}):
                    with self.assertRaises(LifecycleLockPending):
                        self.acquire()

    def test_unreadable_repeated_key_and_missing_phase_count_as_pending(self):
        cases = {
            "repeated phase": f"id={RESTORE_ID}\nphase=committed\nphase=committed\n",
            "repeated other key": f"id={RESTORE_ID}\nroot=/a\nroot=/a\nphase=committed\n",
            "no phase": f"id={RESTORE_ID}\nroot=/x\n",
            "empty phase": f"id={RESTORE_ID}\nphase=\n",
            "damaged line": f"id={RESTORE_ID}\nphase=committed\ngarbage\n",
            "empty file": "",
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.write_state(text)
                with mock.patch.dict(os.environ, {"VESSELSTACK_RESTORE_ID": RESTORE_ID}):
                    with self.assertRaises(LifecycleLockPending):
                        self.acquire()
        self.write_state(f"id={RESTORE_ID}\nphase=committed\n")
        (self.journal / "state").chmod(0)
        try:
            if os.geteuid() != 0:
                with self.assertRaises(LifecycleLockPending):
                    self.acquire()
        finally:
            (self.journal / "state").chmod(0o600)

    def test_a_journal_directory_without_state_is_pending(self):
        self.journal.mkdir(parents=True)
        with self.assertRaises(LifecycleLockPending):
            self.acquire()
        with mock.patch.dict(os.environ, {"VESSELSTACK_RESTORE_ID": RESTORE_ID}):
            with self.assertRaises(LifecycleLockPending):
                self.acquire()

    def test_a_state_that_is_a_directory_is_pending(self):
        (self.journal / "state").mkdir(parents=True)
        with self.assertRaises(LifecycleLockPending):
            self.acquire()

    def test_an_inherited_descriptor_is_not_checked(self):
        with LifecycleLock(self.install, self.system) as parent:
            self.journal_with("starting")
            code = ("from common.lifecycle_lock import LifecycleLock; import sys; "
                    "LifecycleLock(sys.argv[1], sys.argv[2]).acquire(); print('ok')")
            child = subprocess.run([sys.executable, "-c", code, str(self.install), str(self.system)], cwd=ROOT,
                                   env=parent.child_env(), pass_fds=parent.pass_fds(), text=True, capture_output=True)
            self.assertEqual(child.stdout.strip(), "ok", child.stderr)

    def test_run_refuses_with_75_at_once_even_when_asked_to_wait(self):
        self.journal_with("prepared")
        started = time.monotonic()
        result = subprocess.run([sys.executable, str(CLI), "run", "--root", str(self.install), "--system-root",
                                 str(self.system), "--wait", "30", "--", sys.executable, "-c", "print('ran')"],
                                cwd=ROOT, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 75)
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("a restore is unfinished", result.stderr)
        self.assertNotIn("ran", result.stdout)

    def test_run_passes_the_restores_own_id_to_the_command(self):
        self.journal_with("starting")
        env = dict(os.environ, VESSELSTACK_RESTORE_ID=RESTORE_ID)
        result = subprocess.run([sys.executable, str(CLI), "run", "--root", str(self.install), "--system-root",
                                 str(self.system), "--", sys.executable, "-c", "print('ran')"],
                                cwd=ROOT, text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
