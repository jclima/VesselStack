import contextlib
import importlib.util
import io
import json
import re
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
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
        self.real_http_json = dp.http_json
        self.dropped = self.root / "dropped.log"  # written by the forked child, outside the Signal K tree
        for target, value in (("fchown", lambda *a: None), ("http_json", self.influx), ("now", dp.now),
                              ("drop_privileges", self.fake_drop)):
            patcher = mock.patch.object(dp, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(dp.subprocess, "run", self.fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_drop(self, uid, gid):
        # Unprivileged stand-in for dropping to the Signal K account: record that it ran in the child.
        with open(self.dropped, "a") as log:
            log.write(f"{os.getpid()} {uid} {gid}\n")

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
        if cmd[:3] == ["docker", "rm", "-f"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"")
        dry = "--dry-run" in cmd
        if dry:
            return subprocess.CompletedProcess(cmd, 0, stdout=self.dry_run_output.encode())
        target = Path(cmd[cmd.index("-v") + 1].split(":")[0]) if "-v" in cmd else Path(cmd[cmd.index("--prefix") + 1])
        specs = [a.rsplit("@", 1) for a in cmd if re.fullmatch(r"@?[a-z0-9][a-z0-9/._-]*@\d+\.\d+\.\d+", a)]
        for name, version in specs:
            plugin = target / "node_modules" / name
            plugin.mkdir(parents=True)
            (plugin / "package.json").write_text(json.dumps({"name": name, "version": version}))
        (target / "package.json").write_text(json.dumps({"dependencies": dict(specs)}))
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

    def test_unparsable_or_linked_settings_skip_only_the_provider_step(self):
        self.install_plugin()
        path = self.signalk / "settings.json"
        path.write_text("{not json")
        self.assertEqual(self.run_main(), dp.EXIT_CHANGED)  # the plugin configuration was still written
        self.assertEqual(path.read_text(), "{not json")
        self.assertIn("WARN", self.err)
        self.assertIn("settings.json", self.err)
        self.assertTrue(self.config_file().exists())
        self.assertEqual(list(self.signalk.glob("settings.json.vesselstack-*.bak")), [])
        self.config_file().unlink()
        path.unlink()
        target = self.root / "elsewhere.json"
        target.write_text("{}")
        path.symlink_to(target)
        self.assertEqual(self.run_main(), dp.EXIT_CHANGED)
        self.assertEqual(target.read_text(), "{}")
        self.assertTrue(path.is_symlink())
        self.assertIn("WARN", self.err)
        self.assertTrue(self.config_file().exists())

    def test_non_ascii_settings_are_kept_readable(self):
        self.install_plugin()
        path = self.signalk / "settings.json"
        path.write_text(json.dumps({"vessel": "S\u00fcdwind \u2693"}, ensure_ascii=False), encoding="utf-8")
        self.run_main()
        text = path.read_text(encoding="utf-8")
        self.assertIn("S\u00fcdwind \u2693", text)
        self.assertNotIn("\\u00fc", text)

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

    # --- privilege drop, ordering, redirects, cleanup -------------------------------

    def test_file_work_runs_in_a_child_that_dropped_privileges_and_the_token_stays_off_argv_and_env(self):
        seen = []
        original = dp.as_service_user

        def spy(args, work, payload):
            seen.append((work.__name__, json.dumps(payload), WRITER in " ".join(sys.argv), any(WRITER in v for v in os.environ.values())))
            return original(args, work, payload)

        with mock.patch.object(dp, "as_service_user", spy):
            self.assertEqual(self.run_main(), dp.EXIT_CHANGED)
        self.assertEqual([name for name, *_ in seen], ["inspect_work", "apply_work"])
        self.assertIn(WRITER, seen[1][1])  # it travels in the payload (a pipe) ...
        self.assertFalse(any(argv or environ for _, _, argv, environ in seen))  # ... never argv or the environment
        self.assertNotIn(ADMIN, json.dumps([payload for _, payload, *_ in seen]))
        lines = [line.split() for line in self.dropped.read_text().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(pid != str(os.getpid()) and (uid, gid) == ("1000", "1000") for pid, uid, gid in lines))

    def test_drop_order_is_groups_then_gid_then_uid_and_is_verified(self):
        manager = mock.Mock()
        with mock.patch.object(dp.os, "setgroups", manager.setgroups), mock.patch.object(dp.os, "setgid", manager.setgid), \
                mock.patch.object(dp.os, "setuid", manager.setuid), mock.patch.object(dp.os, "getuid", return_value=1000), \
                mock.patch.object(dp.os, "geteuid", return_value=1000), mock.patch.object(dp.os, "getgid", return_value=1000), \
                mock.patch.object(dp.os, "getegid", return_value=1000):
            manager.setuid.side_effect = [None, PermissionError()]  # the attempt to regain root must fail
            dp._drop_privileges(1000, 1000)
        self.assertEqual([c[0] for c in manager.mock_calls], ["setgroups", "setgid", "setuid", "setuid"])
        self.assertEqual(manager.setgroups.call_args.args, ([],))
        with mock.patch.object(dp.os, "setgroups"), mock.patch.object(dp.os, "setgid"), mock.patch.object(dp.os, "setuid"), \
                mock.patch.object(dp.os, "getuid", return_value=0):
            with self.assertRaises(RuntimeError):
                dp._drop_privileges(1000, 1000)

    def test_refusals_are_found_before_a_token_is_created(self):
        self.install_plugin()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.signalk / "plugin-config-data").symlink_to(elsewhere)
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.influx.calls, [])
        self.assertEqual(list(elsewhere.iterdir()), [])
        (self.signalk / "plugin-config-data").unlink()
        (self.signalk / "plugin-config-data").write_text("a file, not a directory")
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.influx.calls, [])

    def test_a_token_whose_configuration_cannot_be_written_is_revoked(self):
        self.install_plugin()
        real = dp.write_atomic_at

        def broken(dir_fd, name, *rest, **kwargs):
            if name == "signalk-to-influxdb2.json":
                raise OSError("disk full")
            return real(dir_fd, name, *rest, **kwargs)

        with mock.patch.object(dp, "write_atomic_at", broken):
            self.assertEqual(self.run_main(), 1)
        self.assertIn("writing the plugin configuration failed", self.err)
        self.assertEqual([p.rsplit("/", 1)[1] for p in self.influx.paths("DELETE")][-1], "newauth")
        self.assertNotIn(WRITER, self.err)

    def test_redirects_are_refused_and_the_token_is_not_forwarded(self):
        hits = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            log_message = lambda *a: None

        target = HTTPServer(("127.0.0.1", 0), Target)

        class Redirector(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(307)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/stolen")
                self.end_headers()

            log_message = lambda *a: None

        redirector = HTTPServer(("127.0.0.1", 0), Redirector)
        for server in (target, redirector):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        with self.assertRaises(urllib.error.URLError):
            self.real_http_json("GET", f"http://127.0.0.1:{redirector.server_port}/api/v2/buckets", ADMIN)
        self.assertEqual(hits, [])

    def test_docker_npm_is_named_and_removed_when_it_times_out(self):
        def timing_out(cmd, **kwargs):
            self.npm_calls.append((list(cmd), kwargs))
            if cmd[:3] == ["docker", "rm", "-f"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=b"")
            if "--dry-run" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=self.dry_run_output.encode())
            raise subprocess.TimeoutExpired(cmd, 1)

        with mock.patch.object(dp.subprocess, "run", timing_out):
            self.assertEqual(self.run_main(), 1)
        install = [cmd for cmd, _ in self.npm_calls if cmd[0] == "docker" and cmd[1] == "run" and "--dry-run" not in cmd][0]
        name = install[install.index("--name") + 1]
        self.assertEqual(name, f"vesselstack-npm-{os.getpid()}")
        self.assertEqual(self.npm_calls[-1][0], ["docker", "rm", "-f", name])
        self.assertEqual(self.influx.calls, [])

    def test_an_unexpected_error_names_only_the_step(self):
        with mock.patch.object(dp, "prune_guard", side_effect=ValueError("/secret/path " + ADMIN)):
            self.assertEqual(self.run_main(), 1)
        self.assertIn("checking what npm would remove failed", self.err)
        self.assertNotIn("/secret/path", self.err + self.out)
        self.assertNotIn(ADMIN, self.err + self.out)

    # --- the optional marine essentials bundle --------------------------------------

    def real_specs(self):
        real = [cmd for cmd, _ in self.npm_calls if "--dry-run" not in cmd][0]
        return [a for a in real if re.fullmatch(r"@?[a-z0-9][a-z0-9/._-]*@\d+\.\d+\.\d+", a)]

    def bundle_config(self, plugin_id):
        return self.signalk / "plugin-config-data" / f"{plugin_id}.json"

    def test_bundle_off_installs_and_writes_nothing_extra(self):
        self.write_env(SIGNALK_MARINE_BUNDLE="false")
        self.run_main()
        self.assertEqual(self.real_specs(), ["signalk-to-influxdb2@2.3.0"])
        self.assertEqual(sorted(p.name for p in (self.signalk / "plugin-config-data").iterdir()), ["signalk-to-influxdb2.json"])
        self.assertNotIn("interfaces", self.settings())

    def test_bundle_on_installs_pinned_packages_and_writes_missing_configs_once(self):
        self.write_env(SIGNALK_MARINE_BUNDLE="true")
        (self.signalk / "plugin-config-data").mkdir()
        owner = '{"enabled": false, "configuration": {"mine": 1}}\n'
        self.bundle_config("anchoralarm").write_text(owner)
        self.assertEqual(self.run_main(), dp.EXIT_CHANGED)
        self.assertEqual(self.real_specs(), ["signalk-to-influxdb2@2.3.0", "signalk-anchoralarm-plugin@2.0.1",
                                             "@meri-imperiumi/signalk-logbook@0.15.1", "@meri-imperiumi/signalk-autostate@0.6.4"])
        for flag in ("--ignore-scripts", "--save-exact"):
            self.assertIn(flag, [c for c, _ in self.npm_calls if "--dry-run" not in c][0])
        self.assertEqual(self.bundle_config("anchoralarm").read_text(), owner)  # never overwritten
        nmea = json.loads(self.bundle_config("sk-to-nmea0183").read_text())
        self.assertTrue(nmea["enabled"])
        for sentence in ("RMC", "VTG", "DBT", "MWVR", "HDM", "XDRBaro"):
            self.assertIs(nmea["configuration"][sentence], True)
        for plugin_id in ("signalk-logbook", "signalk-autostate"):
            self.assertTrue(json.loads(self.bundle_config(plugin_id).read_text())["enabled"])
        self.assertEqual(self.bundle_config("sk-to-nmea0183").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("interfaces", self.settings())  # the TCP server is on by default: no edit
        calls, before = len(self.npm_calls), {p: p.read_bytes() for p in self.signalk.rglob("*") if p.is_file() and "node_modules" not in p.parts}
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(self.npm_calls), calls)
        self.assertEqual(before, {p: p.read_bytes() for p in self.signalk.rglob("*") if p.is_file() and "node_modules" not in p.parts})

    def test_bundle_check_reports_pending_work_and_changes_nothing(self):
        self.write_env(SIGNALK_MARINE_BUNDLE="true")
        self.assertEqual(self.run_main("--check"), dp.EXIT_CHANGED)
        self.assertFalse(self.bundle_config("anchoralarm").exists())
        self.assertTrue(all("--dry-run" in cmd for cmd, _ in self.npm_calls))

    def test_bundle_is_ignored_in_existing_mode(self):
        self.write_env(SIGNALK_MARINE_BUNDLE="true")
        self.assertEqual(self.run_main(mode="existing"), 0)
        self.assertEqual(self.npm_calls, [])
        self.assertEqual(list(self.signalk.iterdir()), [])

    def test_bundle_only_skips_the_influxdb_steps(self):
        self.write_env(SIGNALK_MARINE_BUNDLE="true")
        status = self.root / "datapath-skipped"
        self.assertEqual(self.run_main("--bundle-only", "--status-file", str(status)), dp.EXIT_CHANGED)
        self.assertEqual(self.influx.calls, [])
        self.assertEqual(self.real_specs(), ["signalk-anchoralarm-plugin@2.0.1", "@meri-imperiumi/signalk-logbook@0.15.1",
                                             "@meri-imperiumi/signalk-autostate@0.6.4"])
        self.assertFalse(self.config_file().exists())
        self.assertFalse((self.signalk / "settings.json").exists())  # no CAN provider either
        self.write_env(SIGNALK_MARINE_BUNDLE="false")
        self.assertEqual(self.run_main("--bundle-only"), 0)

    def test_bundle_packages_are_covered_by_the_prune_guard(self):
        self.write_env(SIGNALK_MARINE_BUNDLE="true")
        self.dry_run_output = '{"added": 1, "removed": 1, "remove": [{"name": "their-plugin"}]}\n'
        status = self.root / "datapath-skipped"
        self.assertEqual(self.run_main("--bundle-only", "--status-file", str(status)), dp.EXIT_SKIPPED)
        self.assertEqual(len(self.npm_calls), 1)
        self.assertIn("signalk-anchoralarm-plugin@2.0.1", self.npm_calls[0][0]); self.assertIn("skipped installing signalk-anchoralarm-plugin", self.err)
        self.assertIn("their-plugin", self.err)
        self.assertFalse(status.exists())
        self.assertFalse(self.bundle_config("anchoralarm").exists())

    def test_tcp_server_setting_is_touched_only_when_explicitly_off_and_only_its_key(self):
        self.write_env(SIGNALK_MARINE_BUNDLE="true", SOCKETCAN_ENABLE="false")
        original = {"interfaces": {"nmea-tcp": False, "plugins": True}, "ssl": False, "pipedProviders": []}
        (self.signalk / "settings.json").write_text(json.dumps(original))
        self.assertEqual(self.run_main(), dp.EXIT_CHANGED)
        expected = json.loads(json.dumps(original))
        expected["interfaces"]["nmea-tcp"] = True
        self.assertEqual(self.settings(), expected)
        backups = list(self.signalk.glob("settings.json.vesselstack-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), original)
        # Explicitly on, or absent: left byte for byte alone.
        for document in ({"interfaces": {"nmea-tcp": True}}, {"interfaces": {"plugins": True}}, {"ssl": False}):
            for bak in self.signalk.glob("settings.json.vesselstack-*.bak"):
                bak.unlink()
            raw = json.dumps(document)
            (self.signalk / "settings.json").write_text(raw)
            self.run_main()
            self.assertEqual((self.signalk / "settings.json").read_text(), raw)
            self.assertEqual(list(self.signalk.glob("settings.json.vesselstack-*.bak")), [])

    def test_unusable_settings_are_left_alone_with_a_warning(self):
        self.write_env(SIGNALK_MARINE_BUNDLE="true", SOCKETCAN_ENABLE="false")
        (self.signalk / "settings.json").write_text("{not json")
        self.run_main()
        self.assertEqual((self.signalk / "settings.json").read_text(), "{not json")
        self.assertIn("NMEA 0183 TCP server setting was not checked", self.err)

    def test_unreadable_plugin_is_skipped_with_a_warning_naming_package_path_and_error(self):
        err = io.StringIO()
        denied = PermissionError(13, "Permission denied")
        with mock.patch.object(dp, "read_file_at", side_effect=denied), contextlib.redirect_stderr(err):
            self.assertFalse(dp.plugin_needed(-1, "anchoralarm", "2.0.1", root=Path("/data/signalk")))
        text = err.getvalue()
        self.assertEqual(text.count("\n"), 1)
        self.assertIn("WARN: skipped anchoralarm", text)
        self.assertIn("/data/signalk/node_modules/anchoralarm/package.json", text)
        self.assertIn("permission denied", text)
        self.assertIn("ownership of Signal K's node_modules", text)

    def test_missing_plugin_is_needed_and_readable_one_is_silent(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with mock.patch.object(dp, "read_file_at", side_effect=FileNotFoundError()):
                self.assertTrue(dp.plugin_needed(-1))
            with mock.patch.object(dp, "read_file_at", return_value=json.dumps({"version": dp.PLUGIN_VERSION}).encode()):
                self.assertFalse(dp.plugin_needed(-1))
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
