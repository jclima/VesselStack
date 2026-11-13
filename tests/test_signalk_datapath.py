import contextlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

HELPER = Path(__file__).parents[1] / "scripts/configure-signalk-datapath.py"
spec = importlib.util.spec_from_file_location("configure_signalk_datapath", HELPER)
dp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dp)

ADMIN = "ADMIN-TOKEN-SECRET-0123456789"
WRITER = "WRITER-TOKEN-SECRET-9876543210"
ORG_ID, BUCKET_ID = "orgid0123456789ab", "bucketid012345678"


class FakeInflux:
    """Records every request and answers like InfluxDB 2.x for the paths the script uses."""

    def __init__(self, dbrps=(), old_writers=(), fail_on=None):
        self.calls = []
        self.dbrps = list(dbrps)
        self.authorizations = [{"id": "keepme", "description": "someone else's token"}, *old_writers]
        self.fail_on = fail_on

    def __call__(self, method, url, token, body=None, timeout=15):
        self.calls.append((method, url.split("//", 1)[1].split("/", 1)[1], token, body))
        if self.fail_on and self.fail_on in url:
            raise urllib.error.URLError("boom")
        if "/api/v2/buckets" in url:
            return {"buckets": [{"id": BUCKET_ID, "orgID": ORG_ID, "name": "boatraw"}]}
        if "/api/v2/dbrps" in url and method == "GET":
            return {"content": self.dbrps}
        if "/api/v2/dbrps" in url and method == "POST":
            return {"id": "dbrp1"}
        if "/api/v2/authorizations" in url and method == "GET":
            return {"authorizations": self.authorizations}
        if "/api/v2/authorizations" in url and method == "DELETE":
            return {}
        if "/api/v2/authorizations" in url and method == "POST":
            return {"token": WRITER, "id": "newauth"}
        raise AssertionError(f"unexpected request {method} {url}")

    def paths(self, method):
        return [path for m, path, _, _ in self.calls if m == method]


class DatapathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.signalk = self.data / "signalk"
        self.signalk.mkdir(parents=True)
        self.env_file = self.root / "vesselstack.env"
        self.write_env()
        self.npm_calls = []
        self.dry_run_output = '{\n  "added": 3,\n  "removed": 0,\n  "remove": []\n}\n'
        self.influx = FakeInflux()
        for target, value in (("chown", lambda *a: None), ("http_json", self.influx), ("now", dp.now)):
            patcher = mock.patch.object(dp, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(dp.subprocess, "run", self.fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_env(self, **overrides):
        values = {
            "SIGNALK_VERSION": "v2.27.0", "SOCKETCAN_ENABLE": "true", "SOCKETCAN_INTERFACE": "can0",
            "INFLUXDB_PORT": "8086", "INFLUXDB_ORG": "vessel", "INFLUXDB_RAW_BUCKET": "boatraw",
            "INFLUXDB_TOKEN": ADMIN, "VESSELSTACK_DATA_BIND_ADDRESS": "127.0.0.1",
        }
        values.update(overrides)
        self.env_file.write_text("".join(f'{k}="{v}"\n' for k, v in values.items()))

    def fake_run(self, cmd, **kwargs):
        self.npm_calls.append((list(cmd), kwargs))
        dry = "--dry-run" in cmd
        if dry:
            return subprocess.CompletedProcess(cmd, 0, stdout=self.dry_run_output.encode())
        target = Path(cmd[cmd.index("-v") + 1].split(":")[0]) if "-v" in cmd else Path(cmd[cmd.index("--prefix") + 1])
        plugin = target / "node_modules" / dp.PLUGIN
        plugin.mkdir(parents=True)
        (plugin / "package.json").write_text(json.dumps({"name": dp.PLUGIN, "version": dp.PLUGIN_VERSION}))
        (target / "package.json").write_text(json.dumps({"dependencies": {dp.PLUGIN: dp.PLUGIN_VERSION}}))
        return subprocess.CompletedProcess(cmd, 0, stdout=b"added 108 packages\n")

    def run_main(self, *extra, mode="docker"):
        argv = ["--mode", mode, "--data", str(self.data), "--env-file", str(self.env_file), "--uid", "1000", "--gid", "1000", *extra]
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = dp.main(argv)
        except SystemExit as exc:
            code = exc.code
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def settings(self):
        return json.loads((self.signalk / "settings.json").read_text())

    def config_file(self):
        return self.signalk / "plugin-config-data" / "signalk-to-influxdb2.json"

    def install_plugin(self):
        plugin = self.signalk / "node_modules" / dp.PLUGIN
        plugin.mkdir(parents=True, exist_ok=True)
        (plugin / "package.json").write_text(json.dumps({"version": dp.PLUGIN_VERSION}))

    # --- modes and idempotence ------------------------------------------------------

    def test_existing_mode_is_a_noop(self):
        self.assertEqual(self.run_main(mode="existing"), 0)
        self.assertEqual(self.npm_calls, [])
        self.assertEqual(self.influx.calls, [])
        self.assertEqual(list(self.signalk.iterdir()), [])

    def test_full_run_then_second_run_changes_nothing(self):
        self.assertEqual(self.run_main(), dp.EXIT_CHANGED)
        self.assertTrue((self.signalk / "node_modules" / dp.PLUGIN / "package.json").is_file())
        interface = self.settings()["pipedProviders"][0]["pipeElements"][0]["options"]["subOptions"]["interface"]
        self.assertEqual(interface, "can0")
        config = json.loads(self.config_file().read_text())
        entry = config["configuration"]["influxes"][0]
        self.assertTrue(config["enabled"])
        self.assertEqual((entry["token"], entry["org"], entry["bucket"], entry["url"]), (WRITER, "vessel", "boatraw", "http://127.0.0.1:8086"))
        self.assertTrue(entry["onlySelf"])
        calls_before, http_before = len(self.npm_calls), len(self.influx.calls)
        before = {p: p.read_bytes() for p in self.signalk.rglob("*") if p.is_file() and "node_modules" not in p.parts}
        self.assertEqual(self.run_main(), 0)
        self.assertEqual((len(self.npm_calls), len(self.influx.calls)), (calls_before, http_before))
        after = {p: p.read_bytes() for p in self.signalk.rglob("*") if p.is_file() and "node_modules" not in p.parts}
        self.assertEqual(before, after)

    def test_check_reports_pending_work_without_changing_anything(self):
        self.assertEqual(self.run_main("--check"), dp.EXIT_CHANGED)
        self.assertEqual(self.influx.calls, [])
        self.assertFalse((self.signalk / "settings.json").exists())
        self.assertFalse(self.config_file().exists())
        # Only the read-only dry run happened.
        self.assertTrue(all("--dry-run" in cmd for cmd, _ in self.npm_calls))
        self.run_main()
        self.assertEqual(self.run_main("--check"), 0)

    # --- the docker and native npm invocations --------------------------------------

    def test_docker_install_runs_unprivileged_in_the_pinned_image(self):
        self.run_main()
        real = [cmd for cmd, _ in self.npm_calls if "--dry-run" not in cmd][0]
        joined = " ".join(real)
        self.assertIn("--pull never", joined)
        self.assertIn("--cap-drop ALL", joined)
        self.assertIn("--user 1000:1000", joined)
        self.assertIn("cr.signalk.io/signalk/signalk-server:v2.27.0", joined)
        self.assertIn("signalk-to-influxdb2@2.3.0", real)
        for flag in ("--ignore-scripts", "--no-package-lock", "--omit=dev", "--save-exact"):
            self.assertIn(flag, real)

    def test_native_install_runs_npm_as_the_service_user_with_a_private_cache(self):
        self.assertEqual(self.run_main(mode="native"), dp.EXIT_CHANGED)
        dry_cmd, dry_kwargs = self.npm_calls[0]
        real_cmd, real_kwargs = self.npm_calls[1]
        self.assertIn("--dry-run", dry_cmd)
        self.assertEqual(real_cmd[0], "npm")
        self.assertEqual(real_cmd[real_cmd.index("--prefix") + 1], str(self.signalk))
        self.assertEqual((real_kwargs["user"], real_kwargs["group"], real_kwargs["extra_groups"]), (1000, 1000, []))
        self.assertEqual(real_kwargs["cwd"], self.signalk)
        self.assertEqual(real_kwargs["env"]["HOME"], real_cmd[real_cmd.index("--cache") + 1])

    # --- the npm prune guard --------------------------------------------------------

    def test_prune_guard_skips_everything_when_npm_would_remove_packages(self):
        self.dry_run_output = (
            "add moment 2.31.0\nremove their-plugin 1.2.3\nremove @scope/other 4.5.6\n"
            '{\n  "added": 1,\n  "removed": 2,\n  "remove": [{"name": "their-plugin"}, {"name": "@scope/other"}]\n}\n'
        )
        status = self.root / "datapath-skipped"
        self.assertEqual(self.run_main("--status-file", str(status)), dp.EXIT_SKIPPED)
        self.assertEqual(len(self.npm_calls), 1)
        self.assertIn("--dry-run", self.npm_calls[0][0])
        self.assertIn("their-plugin", self.err)
        self.assertIn("@scope/other", self.err)
        self.assertIn("WARN", self.err)
        self.assertIn("package.json", self.err)
        self.assertEqual(self.influx.calls, [])
        self.assertEqual(list(self.signalk.iterdir()), [])
        self.assertIn("their-plugin", status.read_text())
        self.assertEqual(self.run_main("--check", "--status-file", str(status)), dp.EXIT_SKIPPED)

    def test_prune_guard_skips_when_the_report_cannot_be_read(self):
        self.dry_run_output = "npm exploded in a way nobody planned for\n"
        self.assertEqual(self.run_main(), dp.EXIT_SKIPPED)
        self.assertEqual(len(self.npm_calls), 1)

    def test_prune_guard_clears_the_status_file_once_the_install_is_safe(self):
        status = self.root / "datapath-skipped"
        status.write_text("old skipped\n")
        self.assertEqual(self.run_main("--status-file", str(status)), dp.EXIT_CHANGED)
        self.assertFalse(status.exists())

    def test_removals_parser(self):
        self.assertEqual(dp.removals_from('add a 1.0.0\n{\n  "added": 1,\n  "removed": 0,\n  "remove": []\n}'), [])
        self.assertEqual(dp.removals_from("remove x 1.0.0\n" + '{"removed": 1, "remove": [{"name": "x"}]}'), ["x"])
        self.assertEqual(dp.removals_from('{"removed": 2, "remove": []}'), ["(unnamed packages)"])
        self.assertIsNone(dp.removals_from("no report"))
        self.assertIsNone(dp.removals_from("{broken"))

    def test_npm_failure_is_an_error_and_changes_nothing(self):
        def broken(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout=b"npm ERR! network\n")
        with mock.patch.object(dp.subprocess, "run", broken):
            self.assertEqual(self.run_main(), 1)
        self.assertEqual(list(self.signalk.iterdir()), [])
        self.assertEqual(self.influx.calls, [])

    # --- settings.json and the provider ---------------------------------------------

    def test_provider_is_added_once_with_a_backup_and_other_entries_untouched(self):
        self.install_plugin()
        original = {"interfaces": {"plugins": True}, "pipedProviders": [{"id": "mine", "enabled": True, "pipeElements": [{"type": "providers/simple", "options": {"type": "NMEA0183", "subOptions": {"type": "serial", "device": "/dev/ttyUSB0"}}}]}]}
        path = self.signalk / "settings.json"
        path.write_text(json.dumps(original))
        os.chmod(path, 0o640)
        self.assertEqual(self.run_main(), dp.EXIT_CHANGED)
        document = self.settings()
        self.assertEqual(document["interfaces"], original["interfaces"])
        self.assertEqual(document["pipedProviders"][0], original["pipedProviders"][0])
        self.assertEqual([p["id"] for p in document["pipedProviders"]], ["mine", "vesselstack-n2k-can0"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        backups = list(self.signalk.glob("settings.json.vesselstack-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), original)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(self.settings()["pipedProviders"]), 2)
        self.assertEqual(len(list(self.signalk.glob("settings.json.vesselstack-*.bak"))), 1)

    def test_a_provider_that_already_uses_the_interface_is_left_alone(self):
        self.install_plugin()
        owner = {"id": "my-can", "enabled": False, "pipeElements": [{"type": "providers/simple", "options": {"type": "NMEA2000", "subOptions": {"type": "canbus-canboatjs", "interface": "can0"}}}]}
        path = self.signalk / "settings.json"
        path.write_text(json.dumps({"pipedProviders": [owner]}))
        before = path.read_bytes()
        self.run_main()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.signalk.glob("settings.json.vesselstack-*.bak")), [])

    def test_no_provider_without_socketcan(self):
        self.write_env(SOCKETCAN_ENABLE="false")
        self.run_main()
        self.assertFalse((self.signalk / "settings.json").exists())
        self.assertTrue(self.config_file().exists())

    def test_unparsable_or_linked_settings_are_never_replaced(self):
        self.install_plugin()
        path = self.signalk / "settings.json"
        path.write_text("{not json")
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(path.read_text(), "{not json")
        path.unlink()
        target = self.root / "elsewhere.json"
        target.write_text("{}")
        path.symlink_to(target)
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(target.read_text(), "{}")

    def test_bad_interface_name_is_rejected(self):
        self.install_plugin()
        self.write_env(SOCKETCAN_INTERFACE="can0; rm -rf /")
        self.assertEqual(self.run_main(), 1)
        self.assertFalse((self.signalk / "settings.json").exists())

    # --- the plugin configuration and the InfluxDB token ----------------------------

    def test_existing_plugin_configuration_is_never_touched(self):
        self.install_plugin()
        self.config_file().parent.mkdir()
        owner = '{"enabled": false, "configuration": {"influxes": []}}'
        self.config_file().write_text(owner)
        self.write_env(SOCKETCAN_ENABLE="false")
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.config_file().read_text(), owner)
        self.assertEqual(self.influx.calls, [])

    def test_file_modes(self):
        self.run_main()
        self.assertEqual(stat.S_IMODE(self.config_file().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config_file().parent.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((self.signalk / "settings.json").stat().st_mode), 0o644)
        self.assertEqual([p.name for p in self.signalk.rglob(".*.tmp")], [])

    def test_tokens_never_reach_stdout_stderr_or_a_command_line(self):
        self.run_main()
        for text in (self.out, self.err):
            self.assertNotIn(ADMIN, text)
            self.assertNotIn(WRITER, text)
        for cmd, kwargs in self.npm_calls:
            self.assertNotIn(ADMIN, " ".join(cmd))
            self.assertNotIn(WRITER, " ".join(cmd))
            self.assertNotIn(ADMIN, json.dumps(kwargs.get("env", {})))
        # The admin token only ever authenticates requests; it is not part of any URL or body.
        for method, path, token, body in self.influx.calls:
            self.assertEqual(token, ADMIN)
            self.assertNotIn(ADMIN, path)
            self.assertNotIn(ADMIN, json.dumps(body))

    def test_failures_do_not_leak_tokens(self):
        self.influx.fail_on = "/api/v2/authorizations"
        self.install_plugin()
        self.assertEqual(self.run_main(), 1)
        for text in (self.out, self.err):
            self.assertNotIn(ADMIN, text)
            self.assertNotIn(WRITER, text)
        self.assertFalse(self.config_file().exists())

    def test_dbrp_mapping_is_created_only_when_absent(self):
        self.install_plugin()
        self.run_main()
        posts = [call for call in self.influx.calls if call[0] == "POST" and "/dbrps" in call[1]]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][3], {"orgID": ORG_ID, "bucketID": BUCKET_ID, "database": "boatraw", "retention_policy": "default", "default": True})
        # Mapping created before the token that depends on it.
        order = [(m, p.split("?")[0]) for m, p, _, _ in self.influx.calls]
        self.assertLess(order.index(("POST", "api/v2/dbrps")), order.index(("POST", "api/v2/authorizations")))

    def test_existing_dbrp_mapping_is_not_recreated(self):
        self.install_plugin()
        self.influx.dbrps = [{"id": "m1", "database": "boatraw", "retention_policy": "autogen", "default": True, "virtual": True, "bucketID": BUCKET_ID, "orgID": ORG_ID}]
        self.run_main()
        self.assertEqual([c for c in self.influx.calls if c[0] == "POST" and "/dbrps" in c[1]], [])
        self.assertTrue(self.config_file().exists())

    def test_writer_authorization_scope_is_exactly_what_the_plugin_needs(self):
        self.install_plugin()
        self.influx.authorizations.append({"id": "old1", "description": dp.WRITER_DESCRIPTION})
        self.run_main()
        created = [c for c in self.influx.calls if c[0] == "POST" and c[1] == "api/v2/authorizations"]
        self.assertEqual(len(created), 1)
        body = created[0][3]
        self.assertEqual(body["description"], "vesselstack-signalk-writer")
        self.assertEqual(body["orgID"], ORG_ID)
        bucket = {"type": "buckets", "id": BUCKET_ID, "orgID": ORG_ID}
        self.assertEqual(
            sorted(body["permissions"], key=json.dumps),
            sorted([
                {"action": "read", "resource": {"type": "orgs", "id": ORG_ID}},
                {"action": "read", "resource": bucket},
                {"action": "write", "resource": bucket},
                {"action": "read", "resource": {"type": "dbrp", "orgID": ORG_ID}},
            ], key=json.dumps),
        )
        self.assertEqual(set(body), {"orgID", "description", "permissions"})
        # Only the previous VesselStack writer is removed.
        self.assertEqual([p.rsplit("/", 1)[1] for p in self.influx.paths("DELETE")], ["old1"])

    def test_raw_bucket_name_is_validated_before_any_request(self):
        for bad in ("a b", "a/b", "x" * 65, "", 'a"b', "a;b"):
            self.influx.calls.clear()
            self.write_env(INFLUXDB_RAW_BUCKET=bad)
            self.install_plugin()
            self.assertEqual(self.run_main(), 1, bad)
            self.assertEqual(self.influx.calls, [], bad)
        self.assertFalse(self.config_file().exists())

    def test_influx_address_follows_the_published_bind_address(self):
        self.write_env(VESSELSTACK_DATA_BIND_ADDRESS="192.0.2.10", INFLUXDB_PORT="18086")
        self.install_plugin()
        self.run_main()
        entry = json.loads(self.config_file().read_text())["configuration"]["influxes"][0]
        self.assertEqual(entry["url"], "http://192.0.2.10:18086")
        self.write_env(VESSELSTACK_DATA_BIND_ADDRESS="0.0.0.0")
        self.assertEqual(dp.influx_base(dp.read_env(self.env_file)), "http://127.0.0.1:8086")


if __name__ == "__main__":
    unittest.main()
