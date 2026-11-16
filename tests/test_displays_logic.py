"""Host test of the alarm banner logic header that the display firmware includes."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADER_DIR = ROOT / "displays" / "esphome" / "app"
SOURCE = ROOT / "tests" / "fixtures" / "displays" / "logic_test.cpp"


def host_compiler():
    for name in ("c++", "g++", "clang++"):
        found = shutil.which(name)
        if found:
            return found
    return None


class BannerLogic(unittest.TestCase):
    def test_truth_table(self):
        compiler = host_compiler()
        if compiler is None:
            self.skipTest("no host C++ compiler (c++, g++ or clang++)")
        with tempfile.TemporaryDirectory(prefix="vesselstack-logic.") as work:
            binary = Path(work) / "logic_test"
            build = subprocess.run(
                [compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", f"-I{HEADER_DIR}",
                 str(SOURCE), "-o", str(binary)], capture_output=True, text=True, timeout=120)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn(" 0 failures", run.stdout)

    def test_header_has_no_framework_includes(self):
        text = (HEADER_DIR / "vesselstack_logic.h").read_text()
        includes = [line.split()[1] for line in text.splitlines() if line.startswith("#include")]
        self.assertTrue(set(includes) <= {"<stdint.h>", "<stdio.h>", "<string.h>"}, includes)


if __name__ == "__main__":
    unittest.main()
