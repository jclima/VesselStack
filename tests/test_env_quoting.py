"""install.sh env_line must write values every env-file reader parses identically."""

import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "install.sh").read_text()
FUNCTION = SOURCE[SOURCE.index("# env-line-begin"):SOURCE.index("# env-line-end")]

VALUES = {
    "PLAIN": "abc-1.2/x",
    "EMPTY": "",
    "SPACES": "-q -N 8100 -S 5011",
    "SHELLISH": "p@ss word $HOME !bang #hash&;|<>*?",
    "QUOTED": 'say "hi" (now)',
    "APOSTROPHE": "Captain's Pride",
}


def render(values):
    script = FUNCTION + "\n" + "\n".join(
        f"env_line {shlex.quote(k)} {shlex.quote(v)}" for k, v in values.items()
    )
    return subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True).stdout


class EnvLineTests(unittest.TestCase):
    def test_bash_source_and_shlex_round_trip(self):
        text = render(VALUES)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.env"
            path.write_text(text)
            names = " ".join(VALUES)
            out = subprocess.run(
                ["bash", "-c", f'set -e; source {shlex.quote(str(path))}; for n in {names}; do printf "%s\\0" "${{!n}}"; done'],
                check=True, capture_output=True, text=True,
            ).stdout.split("\0")[:-1]
            self.assertEqual(list(VALUES.values()), out)
        for line in text.splitlines():
            key, raw = line.split("=", 1)
            self.assertEqual(VALUES[key], (shlex.split(raw) or [""])[0])

    def test_single_quote_with_unsafe_characters_is_rejected(self):
        for value in ("it's $x", "it's \\", 'it\'s "q"', "it's `t`"):
            with self.assertRaises(subprocess.CalledProcessError):
                render({"BAD": value})

    def test_predicate_is_shared_and_names_keys_only(self):
        # The installer preflight and env_line use the same env_value_problem, and
        # the preflight reports the KEY and reason, never the value.
        script = FUNCTION + (
            'SCRIPT_DIR=. ; CONFIG_FILE=c.env\n'
            'vesselstack_env_keys() { printf "%s\\n" GOOD BAD; }\n'
            'extra_env_keys() { :; }\n'
            "GOOD=fine BAD=\"it's \\$SECRET\"\n"
            + SOURCE[SOURCE.index("env_values_preflight() {"):SOURCE.index("env_values_preflight\n")]
            + "env_values_preflight\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "VERSION").write_text("0.6.0\n")
            result = subprocess.run(["bash", "-c", script], cwd=tmp, capture_output=True, text=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("BAD:", result.stderr)
        self.assertNotIn("GOOD", result.stderr)
        self.assertNotIn("SECRET", result.stderr)
        self.assertIn("Upgrading to 0.6.0", result.stderr)

    def test_installer_checks_before_dry_run_exit_and_writes_atomically(self):
        self.assertLess(SOURCE.index("\nenv_values_preflight\n"), SOURCE.index('if [ "$DRY_RUN" -eq 1 ]; then\n    displays_preflight'))
        self.assertNotIn("install -m 0600 /dev/null", SOURCE)
        for name in ("vesselstack.env", "boat-chat.env", "control-panel.env"):
            self.assertIn(f"new_env_temp {name}", SOURCE)
            self.assertRegex(SOURCE, r'mv -f -- "\$\w+" "\$VESSELSTACK_ROOT/config/%s"' % name.replace(".", r"\."))

    def test_multiline_is_rejected(self):
        with self.assertRaises(subprocess.CalledProcessError):
            render({"BAD": "a\nb"})

    @unittest.skipUnless(shutil.which("docker"), "docker not available")
    def test_compose_reads_values_identically(self):
        probe = subprocess.run(["docker", "compose", "version"], capture_output=True)
        if probe.returncode != 0:
            self.skipTest("docker compose not available")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "x.env").write_text(render(VALUES))
            services = "".join(f"      {k}: ${{{k}}}\n" for k in VALUES)
            (tmp / "c.yml").write_text(f"services:\n  x:\n    image: busybox\n    environment:\n{services}")
            result = subprocess.run(
                ["docker", "compose", "--env-file", str(tmp / "x.env"), "-f", str(tmp / "c.yml"), "config", "--format", "json"],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                self.skipTest("docker compose config unavailable: " + result.stderr[:200])
            import json
            env = json.loads(result.stdout)["services"]["x"]["environment"]
            for key, value in VALUES.items():
                # `compose config` prints a literal $ as $$.
                self.assertEqual(value, env.get(key, "").replace("$$", "$"), key)


if __name__ == "__main__":
    unittest.main()
