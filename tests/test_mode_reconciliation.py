import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ModeReconciliationTests(unittest.TestCase):
    def run_case(self, mode, *, ais="false", socketcan="false", fail_inspect=False):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); root = base / "opt" / "vesselstack"; system = base / "system"
            (root / "config").mkdir(parents=True); (root / "compose.yml").write_text("services: {}\n")
            (system / "etc/systemd/system").mkdir(parents=True)
            for unit in ("vesselstack-signalk.service", "vesselstack-socketcan.service"):
                (system / "etc/systemd/system" / unit).write_text("")
            env_file = root / "config/vesselstack.env"
            env_file.write_text(f"VESSELSTACK_ROOT={root}\nSIGNALK_MODE={mode}\nAIS_ENABLE={ais}\nSOCKETCAN_ENABLE={socketcan}\n")
            bindir = base / "bin"; bindir.mkdir(); log = base / "calls.log"
            fake = bindir / "systemctl"
            fake.write_text("#!/bin/sh\necho systemctl $* >> \"$LOG\"\nif [ \"$1\" = show ]; then [ \"${FAIL_INSPECT:-}\" = 1 ] && exit 1; echo loaded; fi\n")
            fake.chmod(0o755)
            docker = bindir / "docker"
            docker.write_text("#!/bin/sh\necho docker $* >> \"$LOG\"\ncase \" $* \" in *' ps -q '*) echo fake-id ;; esac\n")
            docker.chmod(0o755)
            runenv = dict(os.environ, PATH=str(bindir), LOG=str(log), VESSELSTACK_SYSTEM_ROOT=str(system))
            if fail_inspect: runenv["FAIL_INSPECT"] = "1"
            result = subprocess.run([str(ROOT / "scripts/reconcile-lifecycle.sh"), str(env_file), "apply"], env=runenv, capture_output=True, text=True)
            return result, log.read_text() if log.exists() else ""

    def test_docker_stops_native_and_socketcan_when_disabled(self):
        result, log = self.run_case("docker")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("systemctl stop vesselstack-signalk.service", log)
        self.assertIn("systemctl disable vesselstack-signalk.service", log)
        self.assertIn("systemctl stop vesselstack-socketcan.service", log)

    def test_native_stops_compose_signalk(self):
        result, log = self.run_case("native")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("docker compose", log); self.assertIn("stop signalk", log)

    def test_existing_stops_both(self):
        result, log = self.run_case("existing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("vesselstack-signalk.service", log); self.assertIn("stop signalk", log)

    def test_ais_and_socketcan_enabled_are_not_stopped(self):
        result, log = self.run_case("existing", ais="true", socketcan="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ais-catcher", log); self.assertNotIn("vesselstack-socketcan.service", log)

    def test_probe_failure_is_not_success(self):
        result, _ = self.run_case("existing", fail_inspect=True)
        self.assertNotEqual(result.returncode, 0)
    def test_fresh_missing_config_is_a_noop_without_service_probes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "opt" / "vesselstack"
            env = dict(os.environ, VESSELSTACK_SYSTEM_ROOT=str(Path(td) / "system"), PATH=str(Path(td) / "missing-bin"))
            result = subprocess.run([str(ROOT / "scripts/reconcile-lifecycle.sh"), str(root / "config" / "vesselstack.env"), "apply"], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
