"""Behavioural tests for scripts/bootstrap-influx.sh against a fake docker/influx."""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/bootstrap-influx.sh"
TOKEN = "super-secret-admin-token-0123456789"

FAKE_DOCKER = r'''#!/bin/bash
# Fake docker: logs argv, answers influx CLI calls from files in $FAKE_STATE.
printf '%s\n' "$*" >> "$FAKE_STATE/argv.log"
[ -z "${INFLUX_TOKEN:-}" ] || printf '%s\n' "$INFLUX_TOKEN" > "$FAKE_STATE/token-seen"
case "$1" in
    inspect) echo true; exit 0 ;;
    cp) cp "$2" "$FAKE_STATE/task.flux"; exit 0 ;;
esac
[ "$1" = exec ] || exit 0
shift
while [ "$1" = -e ]; do shift 2; done
shift   # container name
[ "$1" = rm ] && exit 0
shift   # "influx"
sub="$1 ${2:-}"
case "$sub" in
    "ping ")
        n=$(cat "$FAKE_STATE/ping-count" 2>/dev/null || echo 0); echo $((n+1)) > "$FAKE_STATE/ping-count"
        if [ "$n" -ge "${FAKE_PING_FAIL:-0}" ]; then exit 0; else exit 1; fi ;;
    "bucket list")
        if [ -e "$FAKE_STATE/list-fails" ]; then echo "Error: boom" >&2; exit 1; fi
        cat "$FAKE_STATE/buckets.json" ;;
    "bucket create") echo "$*" >> "$FAKE_STATE/created"; exit 0 ;;
    "bucket update") echo "$*" >> "$FAKE_STATE/updated"; exit 0 ;;
    "task list") cat "$FAKE_STATE/tasks.json" ;;
    "task create") echo "$*" >> "$FAKE_STATE/task-created"; exit 0 ;;
    "task update") echo "$*" >> "$FAKE_STATE/task-updated"; exit 0 ;;
esac
exit 0
'''


@unittest.skipUnless(shutil.which("jq"), "jq is required by the bootstrap script")
class BootstrapInfluxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state = root / "state"
        self.state.mkdir()
        bin_dir = root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
        self.env_file = root / "vesselstack.env"
        self.write_env()
        self.write_buckets(["signalk", "homeassistant", "signalk_1m", "ais"])
        (self.state / "tasks.json").write_text("[]")
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_STATE=str(self.state),
                        INFLUXDB_RETRY_DELAY="0", INFLUXDB_RETRIES="3", INFLUXDB_READY_TIMEOUT="3")

    def write_env(self, retention=""):
        self.env_file.write_text(
            f"INFLUXDB_TOKEN={TOKEN}\nINFLUXDB_ORG=org\nINFLUXDB_CONTAINER_NAME=influx-test\n"
            "INFLUXDB_RAW_BUCKET=signalk\nINFLUXDB_HISTORY_BUCKET=signalk_1m\n"
            "INFLUXDB_HOME_ASSISTANT_BUCKET=homeassistant\nINFLUXDB_AIS_BUCKET=ais\n"
            f"INFLUXDB_RAW_RETENTION={retention}\n")

    def write_buckets(self, names):
        (self.state / "buckets.json").write_text(json.dumps([{"id": f"id-{n}", "name": n} for n in names]))

    def run_script(self, **extra):
        return subprocess.run(["bash", str(SCRIPT), str(self.env_file)], env={**self.env, **extra},
                              text=True, capture_output=True, timeout=60)

    def read(self, name):
        path = self.state / name
        return path.read_text() if path.exists() else ""

    def test_token_is_passed_by_environment_never_on_argv(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read("token-seen").strip(), TOKEN)
        self.assertNotIn(TOKEN, self.read("argv.log"))
        self.assertNotIn("--token", self.read("argv.log"))

    def test_waits_for_readiness_then_proceeds(self):
        result = self.run_script(FAKE_PING_FAIL="3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(int(self.read("ping-count")), 4)

    def test_never_ready_fails_before_any_change(self):
        result = self.run_script(FAKE_PING_FAIL="1000", INFLUXDB_READY_TIMEOUT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not become healthy", result.stderr)
        self.assertEqual(self.read("created"), "")

    def test_failed_bucket_list_is_an_error_not_an_empty_result(self):
        self.write_buckets([])  # even if the file looked empty, a failing list must not create
        (self.state / "list-fails").touch()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not list InfluxDB buckets", result.stderr)
        self.assertEqual(self.read("created"), "")
        self.assertGreaterEqual(self.read("argv.log").count("bucket list"), 3)  # retried

    def test_missing_buckets_are_created_only_after_a_successful_listing(self):
        self.write_buckets(["signalk"])
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        created = self.read("created")
        for name in ("homeassistant", "signalk_1m", "ais"):
            self.assertIn(f"--name {name}", created)
        self.assertNotIn("--name signalk ", created + " ")

    def test_existing_raw_retention_is_untouched_unless_configured(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read("updated"), "")
        self.assertIn("left unchanged", result.stdout)

    def test_configured_raw_retention_is_applied_to_existing_bucket(self):
        self.write_env(retention="30d")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--id id-signalk --retention 30d", self.read("updated"))

    def test_missing_raw_bucket_fails_clearly(self):
        self.write_buckets(["homeassistant"])
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("raw bucket signalk does not exist", result.stderr)

    def test_downsample_task_filters_to_numeric_fields_before_mean(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        flux = self.read("task.flux")
        self.assertIn('import "types"', flux)
        self.assertLess(flux.index("types.isType"), flux.index("aggregateWindow"))
        for kind in ("float", "int", "uint"):
            self.assertIn(f'type: "{kind}"', flux)
        self.assertIn("task-created", os.listdir(self.state))

    def test_outdated_task_is_updated_and_current_task_left_alone(self):
        old = [{"id": "task1", "name": "vesselstack-downsample-signalk", "flux": "from(bucket: \"signalk\") |> aggregateWindow(fn: mean)"}]
        (self.state / "tasks.json").write_text(json.dumps(old))
        self.assertEqual(self.run_script().returncode, 0)
        self.assertIn("--id task1", self.read("task-updated"))
        self.assertEqual(self.read("task-created"), "")
        current = [{"id": "task1", "name": "vesselstack-downsample-signalk", "flux": "// vesselstack-task-version: 2\n"}]
        (self.state / "tasks.json").write_text(json.dumps(current))
        (self.state / "task-updated").unlink()
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.read("task-updated"), "")


MOSQUITTO = ROOT / "scripts/mosquitto-credentials.sh"
FAKE_MOSQUITTO_DOCKER = r'''#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_STATE/docker.log"
for argument in "$@"; do
    case "$argument" in
        *:/mosquitto/data) printf 'hash\n' > "${argument%:/mosquitto/data}/passwords" ;;
    esac
done
'''


@unittest.skipUnless(shutil.which("sha256sum"), "sha256sum is required by the Mosquitto helper")
class MosquittoCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "config").mkdir()
        (root / "state").mkdir()
        (root / "bin").mkdir()
        docker = root / "bin/docker"
        docker.write_text(FAKE_MOSQUITTO_DOCKER)
        docker.chmod(0o755)
        self.root = root
        self.env_file = root / "vesselstack.env"
        self.write_env("first-password")
        self.env = dict(os.environ, PATH=f"{root / 'bin'}:{os.environ['PATH']}", FAKE_STATE=str(root / "state"))

    def write_env(self, password):
        self.env_file.write_text(f"VESSELSTACK_ROOT={self.root}\nVESSELSTACK_DATA={self.root / 'data'}\n"
                                 f"MQTT_USERNAME=ha\nMQTT_PASSWORD={password}\n")

    def run_helper(self):
        return subprocess.run(["bash", str(MOSQUITTO), str(self.env_file)], env=self.env, text=True, capture_output=True)

    def docker_calls(self):
        log = self.root / "state/docker.log"
        return log.read_text().splitlines() if log.exists() else []

    def test_create_then_skip_then_update_without_truncating(self):
        self.assertEqual(self.run_helper().returncode, 0)
        calls = self.docker_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("mosquitto_passwd -b -c", calls[0])
        self.assertEqual(self.run_helper().returncode, 0)  # unchanged -> no docker call at all
        self.assertEqual(len(self.docker_calls()), 1)
        self.write_env("second-password")
        self.assertEqual(self.run_helper().returncode, 0)
        calls = self.docker_calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("mosquitto_passwd -b ", calls[1])
        self.assertNotIn(" -c ", calls[1])  # existing file is updated in place, never reset
        self.assertEqual((self.root / "config/.mqtt-credentials.sha256").stat().st_mode & 0o777, 0o600)

    def test_refuses_ungenerated_password(self):
        self.write_env("GENERATE")
        self.assertNotEqual(self.run_helper().returncode, 0)
        self.assertEqual(self.docker_calls(), [])


if __name__ == "__main__":
    unittest.main()
