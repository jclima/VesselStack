import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("vesselstack_alerts", ROOT / "scripts" / "vesselstack-alerts.py")
alerts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alerts)

TOKEN = "123456:SECRET-bot-token-value"
HEARTBEAT = "https://hc.example.org/ping/secret-uuid-1234"
CHAT = "987654321"
T0 = 1_800_000_000.0


class FakeIo(alerts.Io):
    def __init__(self):
        self.clock = T0
        self.requests = []
        self.telegram_status = 200
        self.telegram_error = False
        self.voltage = None          # volts for house battery, None = no point
        self.point_age = 30          # seconds since newest self point
        self.influx_up = True
        self.ha = {}                 # entity -> state
        self.active = set()          # inactive units are those NOT here; default all active
        self.down = set()
        self.boot = "boot-1"
        self.shutdown = "unknown"
        self.backup = None
        self.heartbeat_error = False

    def now(self):
        return self.clock

    def http(self, method, url, headers=None, body=None, timeout=10):
        self.requests.append((method, url))
        if "api.telegram.org" in url:
            if self.telegram_error:
                raise OSError("timed out")
            return self.telegram_status, b'{"ok": false, "description": "Bad Request"}'
        if url.startswith("https://hc.example.org"):
            if self.heartbeat_error:
                raise OSError("down")
            return 200, b"OK"
        if "/api/v2/query" in url:
            if not self.influx_up:
                raise OSError("refused")
            query = json.loads(body)["query"]
            if "electrical" in query:
                if self.voltage is None:
                    return 200, b""
                return 200, (",result,table,_time,_value,_field,_measurement,self\n,,0,2027-01-01T00:00:00Z,%s,value,electrical.batteries.house.voltage,true\n" % self.voltage).encode()
            stamp = alerts.dt.datetime.fromtimestamp(self.clock - self.point_age, alerts.dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return 200, (",result,table,_time,_value,_field,_measurement\n,,0,%s,1,value,x\n" % stamp).encode()
        if "/api/states/" in url:
            entity = url.rsplit("/", 1)[1]
            if entity not in self.ha:
                return 404, b""
            return 200, json.dumps({"state": self.ha[entity]}).encode()
        if url.endswith("/health") and not self.influx_up:
            raise OSError("refused")
        return 200, b"ok"

    def unit_active(self, unit):
        return unit not in self.down

    def backup_running(self):
        return "vesselstack-backup.service" in self.down

    def boot_id(self):
        return self.boot

    def previous_shutdown(self):
        return self.shutdown

    def read_json(self, path):
        return self.backup

    def telegram_texts(self):
        return [m for m in self.sent]

    @property
    def sent(self):
        return [u for m, u in self.requests if "api.telegram.org" in u]


def settings(**extra):
    base = {"ALERTS_ENABLE": "true", "ALERT_TELEGRAM_CHAT_IDS": CHAT, "TELEGRAM_BOT_TOKEN": TOKEN,
            "SIGNALK_MODE": "docker", "SIGNALK_DATAPATH_AUTOCONFIG": "true", "TELEMETRY_INDEXER_ENABLE": "false",
            "ALERT_REPEAT_MINUTES": "60", "ALERT_STALE_DATA_MINUTES": "15"}
    base.update(extra)
    return base


class Harness:
    def __init__(self, test, **extra):
        self.dir = Path(tempfile.mkdtemp())
        test.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.io, self.extra = FakeIo(), extra
        self.state_path = self.dir / "alerts" / "state.json"
        self.err = io.StringIO()
        self.messages = []
        self.telegram_bodies = []
        orig = self.io.http

        def capture(method, url, headers=None, body=None, timeout=10):
            if "api.telegram.org" in url and body:
                self.telegram_bodies.append(json.loads(body)["text"])
            return orig(method, url, headers, body, timeout)
        self.io.http = capture

    def run(self, advance=0, **extra):
        self.io.clock += advance
        merged = settings(**{**self.extra, **extra})
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = alerts.run(merged, self.io, self.state_path, lambda m: stderr.write(m + "\n"), None)
        self.err.write(stderr.getvalue())
        self.out = stdout.getvalue()
        return code

    def state(self):
        return json.loads(self.state_path.read_text())

    def texts(self):
        return list(self.telegram_bodies)


class AlertTests(unittest.TestCase):
    def test_disabled_does_nothing(self):
        h = Harness(self)
        h.run(ALERTS_ENABLE="false")
        self.assertEqual(h.io.requests, [])
        self.assertFalse(h.state_path.exists())

    def test_battery_hold_down_then_alert_repeat_and_cleared(self):
        h = Harness(self, ALERT_BATTERY_LOW_V="12.0")
        h.io.voltage = 11.5
        h.run()
        self.assertEqual(h.texts(), [])                      # first seen, held down
        h.run(60)
        self.assertEqual(h.texts(), [])                      # 60 s < 120 s
        h.run(60)
        self.assertEqual(len(h.texts()), 1)
        self.assertIn("11.50 V", h.texts()[0])
        self.assertTrue(h.texts()[0].endswith(alerts.DISCLAIMER))
        h.run(60 * 30)
        self.assertEqual(len(h.texts()), 1)                  # not repeated yet
        h.run(60 * 31)
        self.assertEqual(len(h.texts()), 2)                  # repeat after 60 min
        h.io.voltage = 12.8
        h.run(60)
        self.assertEqual(len(h.texts()), 3)
        self.assertIn("recovered", h.texts()[2])
        h.run(60)
        self.assertEqual(len(h.texts()), 3)                  # cleared message only once
        self.assertEqual(h.state()["alerts"], {})

    def test_battery_recovers_before_hold_down_sends_nothing(self):
        h = Harness(self, ALERT_BATTERY_LOW_V="12.0")
        h.io.voltage = 11.0
        h.run()
        h.io.voltage = 13.0
        h.run(60)
        self.assertEqual(h.texts(), [])
        self.assertEqual(h.state()["alerts"], {})

    def test_blank_threshold_means_alert_off(self):
        h = Harness(self)
        h.io.voltage = 1.0
        h.run()
        h.run(300)
        self.assertEqual(h.texts(), [])

    def test_stale_data(self):
        h = Harness(self)
        h.io.point_age = 20 * 60
        h.run()
        self.assertEqual(len(h.texts()), 1)
        self.assertIn("15 minutes", h.texts()[0])
        h.io.point_age = 10
        h.run(60)
        self.assertIn("flowing again", h.texts()[-1])

    def test_stale_not_checked_without_datapath(self):
        h = Harness(self, SIGNALK_MODE="existing")
        h.io.point_age = 99999
        h.run()
        self.assertEqual(h.texts(), [])

    def test_bilge_and_shore_power_entities(self):
        h = Harness(self, ALERT_BILGE_ENTITY="binary_sensor.bilge", ALERT_SHORE_POWER_ENTITY="binary_sensor.shore",
                    HOME_ASSISTANT_TOKEN="ha-token-1234")
        h.io.ha = {"binary_sensor.bilge": "on", "binary_sensor.shore": "unavailable"}
        h.run()
        self.assertEqual(h.texts(), [])
        h.run(60)
        self.assertEqual(len(h.texts()), 2)
        self.assertTrue(any("Bilge" in t for t in h.texts()) and any("Shore power" in t for t in h.texts()))
        h.io.ha = {"binary_sensor.bilge": "off", "binary_sensor.shore": "on"}
        h.run(60)
        self.assertEqual(len(h.texts()), 4)

    def test_component_down_is_held_down_and_cleared(self):
        h = Harness(self)
        h.io.down = {"vesselstack-chat.service"}
        h.run()
        h.run(120)
        self.assertEqual(h.texts(), [])
        h.run(60)
        self.assertEqual(len(h.texts()), 1)
        self.assertIn("vesselstack-chat.service is down", h.texts()[0])
        h.io.down = set()
        h.run(60)
        self.assertIn("back up", h.texts()[-1])

    def test_influx_down_probe(self):
        h = Harness(self)
        h.io.influx_up = False
        h.run()
        h.run(200)
        self.assertTrue(any("InfluxDB is down" in t for t in h.texts()))

    def test_stack_checks_paused_during_backup(self):
        h = Harness(self)
        h.io.down = {"vesselstack-backup.service"}
        h.run()
        h.run(600)
        self.assertEqual(h.texts(), [])

    def test_boot_change(self):
        h = Harness(self)
        h.run()
        self.assertEqual(h.texts(), [])                      # first run just records the boot id
        h.io.boot, h.io.shutdown = "boot-2", "unclean"
        h.run(60)
        self.assertEqual(len(h.texts()), 1)
        self.assertIn("restarted", h.texts()[0])
        self.assertIn("not clean", h.texts()[0])
        h.run(60)
        self.assertEqual(len(h.texts()), 1)

    def test_boot_change_unknown_shutdown(self):
        h = Harness(self)
        h.run()
        h.io.boot = "boot-2"
        h.run(60)
        self.assertIn("unknown", h.texts()[0])

    def test_backup_failure_alerts_once_per_finished_at(self):
        h = Harness(self)
        h.io.backup = {"result": "failed", "exit_status": 1, "finished_at": "2027-01-01T03:30:00Z"}
        h.run()
        h.run(60)
        self.assertEqual(len(h.texts()), 1)
        self.assertIn("backup failed", h.texts()[0])
        h.io.backup = {"result": "failed", "exit_status": 1, "finished_at": "2027-01-02T03:30:00Z"}
        h.run(60)
        self.assertEqual(len(h.texts()), 2)
        h.io.backup = {"result": "skipped_busy", "exit_status": 75, "finished_at": "2027-01-03T03:30:00Z"}
        h.run(60)
        self.assertEqual(len(h.texts()), 2)

    def test_stale_backup(self):
        h = Harness(self, VESSELSTACK_BACKUP_SCHEDULE="daily")
        old = alerts.iso(T0 - 3 * 86400)
        h.io.backup = {"result": "success", "finished_at": old, "last_success_at": old}
        h.run()
        self.assertEqual(len(h.texts()), 1)
        self.assertIn("No successful scheduled backup", h.texts()[0])
        fresh = alerts.iso(T0 - 3600)
        h2 = Harness(self, VESSELSTACK_BACKUP_SCHEDULE="daily")
        h2.io.backup = {"result": "success", "finished_at": fresh, "last_success_at": fresh}
        h2.run()
        self.assertEqual(h2.texts(), [])
        h3 = Harness(self, VESSELSTACK_BACKUP_SCHEDULE="off")
        h3.io.backup = {"result": "success", "finished_at": old, "last_success_at": old}
        h3.run()
        self.assertEqual(h3.texts(), [])

    def test_heartbeat_success_and_fail(self):
        h = Harness(self, ALERT_HEARTBEAT_URL=HEARTBEAT)
        h.run()
        pings = [u for m, u in h.io.requests if u.startswith("https://hc.example.org")]
        self.assertEqual(pings, [HEARTBEAT])
        h.io.down = {"vesselstack-chat.service"}
        h.run(60)
        pings = [u for m, u in h.io.requests if u.startswith("https://hc.example.org")]
        self.assertEqual(pings[-1], HEARTBEAT + "/fail")
        h.io.heartbeat_error = True
        h.run(60)
        self.assertNotIn(HEARTBEAT, h.err.getvalue())

    def test_telegram_failure_keeps_state_and_retries(self):
        h = Harness(self, ALERT_BILGE_ENTITY="binary_sensor.bilge", HOME_ASSISTANT_TOKEN="t-12345")
        h.io.ha = {"binary_sensor.bilge": "on"}
        h.io.telegram_error = True
        h.run()
        self.assertEqual(h.run(60), 0)
        state = h.state()
        self.assertEqual(state["alerts"]["bilge"]["last_sent"], "")
        self.assertFalse(state["last_send"]["ok"])
        h.io.telegram_error = False
        h.run(60)
        self.assertTrue(h.state()["alerts"]["bilge"]["last_sent"])
        self.assertTrue(h.state()["last_send"]["ok"])

    def test_telegram_http_error_recorded(self):
        h = Harness(self)
        h.io.telegram_status = 403
        h.io.backup = {"result": "failed", "exit_status": 1, "finished_at": "2027-01-01T03:30:00Z"}
        h.run()
        self.assertIn("403", h.state()["last_send"]["error"])
        self.assertNotIn("backup", h.state()["events"])

    def test_only_sendmessage_is_used(self):
        h = Harness(self)
        h.io.backup = {"result": "failed", "exit_status": 1, "finished_at": "2027-01-01T03:30:00Z"}
        h.run()
        self.assertTrue(h.io.sent)
        self.assertTrue(all(u.endswith("/sendMessage") for u in h.io.sent))
        self.assertFalse(any("getUpdates" in u for _, u in h.io.requests))

    def test_secrets_never_printed(self):
        h = Harness(self, ALERT_HEARTBEAT_URL=HEARTBEAT, ALERT_BATTERY_LOW_V="12", HOME_ASSISTANT_TOKEN="ha-token-1234")
        h.io.telegram_error = True
        h.io.voltage = 10.0
        h.io.down = {"vesselstack-chat.service"}
        h.run()
        h.run(300)
        h.io.heartbeat_error = True
        h.run(60)
        combined = h.err.getvalue() + h.out
        for secret in (TOKEN, HEARTBEAT, CHAT, "ha-token-1234", "SECRET-bot-token"):
            self.assertNotIn(secret, combined)

    def test_main_status_and_doctor_never_print_secrets(self):
        with tempfile.TemporaryDirectory() as data:
            env = {**settings(ALERT_HEARTBEAT_URL=HEARTBEAT, ALERT_BATTERY_LOW_V="12"), "VESSELSTACK_DATA": data}
            for command in ("status", "doctor"):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(alerts.main([command], env, FakeIo()), 0)
                for secret in (TOKEN, HEARTBEAT, CHAT):
                    self.assertNotIn(secret, out.getvalue() + err.getvalue())

    def test_test_command_reports_failure_without_secrets_and_records_it(self):
        with tempfile.TemporaryDirectory() as data:
            fake = FakeIo()
            fake.telegram_error = True
            env = {**settings(), "VESSELSTACK_DATA": data}
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(alerts.main(["test"], env, fake), 1)
            self.assertIn("FAILED", out.getvalue())
            self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
            last = json.loads((Path(data) / "alerts" / "state.json").read_text())["last_send"]
            self.assertFalse(last["ok"])
            fake.telegram_error = False
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(alerts.main(["test"], env, fake), 0)

    def test_doctor_warnings_and_exit_status(self):
        with tempfile.TemporaryDirectory() as data:
            env = {"ALERTS_ENABLE": "true", "VESSELSTACK_DATA": data}
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(alerts.main(["doctor"], env, FakeIo()), 0)
            self.assertIn("ALERT_TELEGRAM_CHAT_IDS", out.getvalue())
            self.assertIn("bot token", out.getvalue())
            self.assertTrue(all(line.startswith("WARN") for line in out.getvalue().splitlines()))

    def test_state_is_atomic_and_private_and_corrupt_state_recovers(self):
        h = Harness(self, ALERT_BATTERY_LOW_V="12")
        h.io.voltage = 11
        h.run()
        self.assertEqual(h.state_path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(h.state_path.with_name("state.json.tmp").exists())
        h.state_path.write_text("{not json")
        self.assertEqual(h.run(60), 0)
        self.assertTrue(h.state_path.with_name("state.json.corrupt").exists())

    def test_override_file_supplies_bot_token(self):
        with tempfile.TemporaryDirectory() as directory:
            override = Path(directory) / "boat-chat.env"
            override.write_text("TELEGRAM_BOT_TOKEN='abc:override'\nOTHER=1\n")
            merged = alerts.load_settings({"BOAT_CHAT_ENV": str(override), "TELEGRAM_BOT_TOKEN": "old"}, [])
            self.assertEqual(merged["TELEGRAM_BOT_TOKEN"], "abc:override")
            self.assertNotIn("OTHER", merged)


if __name__ == "__main__":
    unittest.main()
