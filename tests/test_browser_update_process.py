#!/usr/bin/env python3
"""Process-runner tests for bounded browser-update helper commands."""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_updates():
    spec = importlib.util.spec_from_file_location("process_updates", ROOT / "control-panel" / "browser_updates.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProcessRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.updates = load_updates()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="vesselstack-process-test-")
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def operation(self) -> dict[str, object]:
        return {"id": 1, "label": "preflight", "state": "running", "output": []}

    def test_high_volume_output_is_bounded_and_keeps_tail(self) -> None:
        marker = "TAIL-MARKER"
        code = "import sys; sys.stdout.write('x'*100000 + '\\n' + 'TAIL-MARKER\\n'); sys.stdout.flush()"
        operation = self.operation()
        result = self.updates.run_command([sys.executable, "-c", code], self.root, operation, timeout=3)
        self.assertEqual(result.returncode, 0)
        self.assertIn(marker, result.stdout)
        self.assertLessEqual(len(result.stdout.encode()), self.updates.MAX_OUTPUT)
        self.assertIn(marker, operation["output"][-1])

    def test_update_children_never_wait_for_a_terminal(self) -> None:
        # The panel cannot answer prompts, so every child it launches runs non-interactively.
        source = (ROOT / "control-panel" / "browser_updates.py").read_text()
        self.assertRegex(source, r'child_env = dict\(os\.environ,[^\n]*VESSELSTACK_NONINTERACTIVE="true"')
        self.assertIn("install_env = dict(child_env)", source)

    def test_nonzero_child_preserves_actionable_tail_and_never_completes(self) -> None:
        code = "import sys; print('backup mount is required before preflight'); print('actionable failure'); sys.exit(7)"
        operation = self.operation()
        with self.assertRaises(self.updates.subprocess.CalledProcessError) as raised:
            self.updates.run_command([sys.executable, "-c", code], self.root, operation, timeout=3)
        self.assertEqual(raised.exception.returncode, 7)
        self.assertIn("backup mount", raised.exception.output)
        self.assertIn("actionable failure", operation["output"])
        self.assertEqual(operation["state"], "running")

    def test_timeout_terminates_and_reaps_child_process_group(self) -> None:
        code = "import time; print('started before timeout', flush=True); time.sleep(10)"
        operation = self.operation()
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "command timed out"):
            self.updates.run_command([sys.executable, "-c", code], self.root, operation, timeout=0.1)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(operation["state"], "running")
        self.assertTrue(operation["output"] and "command timed out" in operation["output"][-1])

    def test_timeout_kills_term_ignoring_grandchild(self) -> None:
        pid_file = self.root / "grandchild.pid"
        parent_meta = self.root / "parent.meta"
        grandchild = self.root / "grandchild.py"
        grandchild.write_text("import os, signal, sys, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nopen(sys.argv[1], 'w').write(f'{os.getpid()} {os.getpgid(0)}')\ntime.sleep(10)\n")
        parent = self.root / "parent.py"
        parent.write_text("import os, subprocess, sys, time\nopen(sys.argv[3], 'w').write(f'{os.getpid()} {os.getpgid(0)}')\nsubprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\nprint('grandchild-started', flush=True)\ntime.sleep(10)\n")
        operation = self.operation()
        with self.assertRaisesRegex(RuntimeError, "command timed out"):
            self.updates.run_command([sys.executable, str(parent), str(grandchild), str(pid_file), str(parent_meta)], self.root, operation, timeout=0.2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not pid_file.exists():
            time.sleep(0.02)
        self.assertTrue(pid_file.exists())
        self.assertTrue(parent_meta.exists())
        grandchild, child_group = (int(value) for value in pid_file.read_text().split())
        parent_pid, parent_group = (int(value) for value in parent_meta.read_text().split())
        self.assertEqual(child_group, parent_group)
        state = ""
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            probe = subprocess.run(["ps", "-o", "stat=", "-p", str(grandchild)], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
            state = probe.stdout.strip()
            if not state or state.startswith("Z"):
                break
            time.sleep(0.02)
        if state and not state.startswith("Z"):
            try:
                os.kill(grandchild, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.fail(f"SIGTERM-ignoring child remained alive after process-group timeout: ps state={state!r}, parent={parent_pid}")


if __name__ == "__main__":
    unittest.main()
