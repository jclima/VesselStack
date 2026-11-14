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
        self.mono = 0.0
        self.http_cost = 0.0
        self.uid = 1000
        self.ha_error = False
        self.ha_status = None        # forced HTTP status for every Home Assistant state read
        self.influx_status = 200     # forced HTTP status for every InfluxDB query

    def now(self):
        return self.clock

    def monotonic(self):
        return self.mono

    def euid(self):
        return self.uid

    def http(self, method, url, headers=None, body=None, timeout=10):
        self.requests.append((method, url))
        self.mono += self.http_cost
        if "api.telegram.org" in url:
            if self.telegram_error:
                raise OSError("timed out")
            return self.telegram_status, b'{"ok": false, "description": "Bad Request"}'
        if url.startswith("https://hc.example.org") or url.startswith("https://hc2.example.org"):
            if self.heartbeat_error:
                raise OSError("down")
            return 200, b"OK"
        if "/api/v2/query" in url:
            if not self.influx_up:
                raise OSError("refused")
            if self.influx_status != 200:
                return self.influx_status, b""
            query = json.loads(body)["query"]
            if "electrical" in query:
                if self.voltage is None:
                    return 200, b""
                return 200, (",result,table,_time,_value,_field,_measurement,self\n,,0,2027-01-01T00:00:00Z,%s,value,electrical.batteries.house.voltage,true\n" % self.voltage).encode()
            stamp = alerts.dt.datetime.fromtimestamp(self.clock - self.point_age, alerts.dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return 200, (",result,table,_time,_value,_field,_measurement\n,,0,%s,1,value,x\n" % stamp).encode()
        if "/api/states/" in url:
            if self.ha_error:
                raise OSError("ha down")
            if self.ha_status:
                return self.ha_status, b""
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
        h.io.ha = {"binary_sensor.bilge": "on", "binary_sensor.shore": "off"}
        h.run()
        self.assertEqual(len(h.texts()), 1)                  # bilge: first poll; shore still held 60 s
        self.assertIn("Bilge", h.texts()[0])
        h.run(60)
        self.assertEqual(len(h.texts()), 2)
        self.assertIn("Shore power lost", h.texts()[1])
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
        h.run(200)
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

    # ---- review fixes
    def bilge(self, **extra):
        return Harness(self, ALERT_BILGE_ENTITY="binary_sensor.bilge", HOME_ASSISTANT_TOKEN="t-12345", **extra)

    def test_bilge_single_poll_alerts_immediately(self):
        h = self.bilge()
        h.io.ha = {"binary_sensor.bilge": "on"}
        h.run()
        self.assertEqual(len(h.texts()), 1)
        self.assertIn("Bilge alarm", h.texts()[0])

    def test_bilge_offline_is_not_an_all_clear(self):
        h = self.bilge()
        h.io.ha = {"binary_sensor.bilge": "on"}
        h.run()
        for state in ("unavailable", "unknown", ""):
            h.io.ha = {"binary_sensor.bilge": state}
            h.run(60)
            self.assertIn("bilge", h.state()["alerts"])
            self.assertFalse(any("off again" in t for t in h.texts()))
        h.io.ha_error = True                                  # HA down: also not an all-clear
        h.run(60)
        self.assertIn("bilge", h.state()["alerts"])
        h.io.ha_error = False
        h.io.ha = {"binary_sensor.bilge": "unavailable"}
        h.run(300)
        self.assertTrue(any("sensor not reporting" in t for t in h.texts()))
        self.assertIn("bilge", h.state()["alerts"])
        h.io.ha = {"binary_sensor.bilge": "off"}
        h.run(60)
        self.assertNotIn("bilge", h.state()["alerts"])
        self.assertTrue(any("off again" in t for t in h.texts()))

    def test_not_reporting_waits_five_minutes_and_404_warns_in_doctor(self):
        h = self.bilge()
        h.run()                                               # entity absent: 404
        h.run(240)
        self.assertEqual(h.texts(), [])
        h.run(60)
        self.assertEqual(len(h.texts()), 1)
        self.assertIn("not reporting", h.texts()[0])
        self.assertIn("not found", h.texts()[0])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            alerts.cmd_doctor(settings(ALERT_BILGE_ENTITY="binary_sensor.bilge", HOME_ASSISTANT_TOKEN="t-12345"), h.io, h.state_path, print)
        self.assertIn("entity not found", out.getvalue())

    def test_shore_unavailable_hold_and_wording(self):
        h = Harness(self, ALERT_SHORE_POWER_ENTITY="binary_sensor.shore", HOME_ASSISTANT_TOKEN="t-12345")
        h.io.ha = {"binary_sensor.shore": "unavailable"}
        h.run()
        h.run(240)
        self.assertEqual(h.texts(), [])
        h.run(60)
        self.assertEqual(len(h.texts()), 1)
        self.assertIn("sensor not reporting", h.texts()[0])
        self.assertNotIn("lost", h.texts()[0])

    def test_doctor_warns_entity_without_token(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as data, contextlib.redirect_stdout(out):
            env = {**settings(ALERT_SHORE_POWER_ENTITY="binary_sensor.shore"), "VESSELSTACK_DATA": data}
            alerts.main(["doctor"], env, FakeIo())
        self.assertIn("HOME_ASSISTANT_TOKEN is empty", out.getvalue())

    def pings(self, h):
        return [u for m, u in h.io.requests if u.startswith("https://hc.example.org")]

    def test_bilge_without_token_raises_not_working_alert_and_heartbeat_fails(self):
        h = Harness(self, ALERT_BILGE_ENTITY="binary_sensor.bilge", ALERT_HEARTBEAT_URL=HEARTBEAT)   # no HOME_ASSISTANT_TOKEN
        h.run()
        h.run(240)
        self.assertEqual(h.texts(), [])                       # held 5 minutes like "not reporting"
        self.assertEqual(self.pings(h)[-1], HEARTBEAT)
        h.run(60)
        self.assertEqual(len(h.texts()), 1)
        self.assertIn("Bilge alert is not working: Home Assistant token is missing or was rejected, so binary_sensor.bilge cannot be checked.", h.texts()[0])
        self.assertIn("Set a new long-lived access token in Control Panel settings.", h.texts()[0])
        self.assertEqual(self.pings(h)[-1], HEARTBEAT + "/fail")
        h.run(60)
        self.assertEqual(len(h.texts()), 1)                   # de-duplicated
        h.run(60 * 61)
        self.assertEqual(len(h.texts()), 2)                   # repeats on ALERT_REPEAT_MINUTES
        self.assertIn("bilge-auth", h.state()["alerts"])
        self.assertNotIn("bilge", h.state()["alerts"])

    def test_ha_401_and_403_raise_not_working_alert(self):
        for status in (401, 403):
            h = Harness(self, ALERT_SHORE_POWER_ENTITY="binary_sensor.shore", HOME_ASSISTANT_TOKEN="t-12345", ALERT_HEARTBEAT_URL=HEARTBEAT)
            h.io.ha_status = status
            h.run()
            h.run(300)
            self.assertEqual(len(h.texts()), 1, status)
            self.assertIn("Shore power alert is not working", h.texts()[0])
            self.assertNotIn("t-12345", h.texts()[0])
            self.assertEqual(self.pings(h)[-1], HEARTBEAT + "/fail")

    def test_active_bilge_alert_survives_rejected_token_and_recovery_clears_both(self):
        h = self.bilge(ALERT_HEARTBEAT_URL=HEARTBEAT)
        h.io.ha = {"binary_sensor.bilge": "on"}
        h.run()
        self.assertEqual(len(h.texts()), 1)
        h.io.ha_status = 401
        h.run(60)
        h.run(300)
        self.assertIn("bilge", h.state()["alerts"])           # not cleared by a token failure
        self.assertFalse(any("off again" in t for t in h.texts()))
        self.assertTrue(any("Bilge alert is not working" in t for t in h.texts()))
        self.assertEqual(self.pings(h)[-1], HEARTBEAT + "/fail")
        h.io.ha_status = None
        h.io.ha = {"binary_sensor.bilge": "off"}
        h.run(60)
        self.assertEqual(h.state()["alerts"], {})
        self.assertTrue(any("Bilge alert is working again: binary_sensor.bilge can be read." in t for t in h.texts()))
        self.assertTrue(any("off again" in t for t in h.texts()))
        count = len(h.texts())
        h.run(60)
        self.assertEqual(len(h.texts()), count)               # clear message only once
        self.assertEqual(self.pings(h)[-1], HEARTBEAT)

    def test_transient_ha_failures_do_not_raise_not_working_alert(self):
        h = self.bilge(ALERT_HEARTBEAT_URL=HEARTBEAT)
        h.io.ha_error = True
        h.run()
        h.run(600)
        h.io.ha_error, h.io.ha_status = False, 503
        h.run(600)
        self.assertEqual(h.texts(), [])
        self.assertEqual(h.state()["alerts"], {})
        self.assertEqual(self.pings(h)[-1], HEARTBEAT)

    def test_recovery_before_hold_sends_nothing(self):
        h = Harness(self, ALERT_BILGE_ENTITY="binary_sensor.bilge")
        h.run()
        h.run(60, HOME_ASSISTANT_TOKEN="t-12345")
        self.assertEqual(h.texts(), [])
        self.assertEqual(h.state()["alerts"].get("bilge-auth"), None)

    def test_influx_rejected_token_raises_alert_and_recovers(self):
        h = Harness(self, ALERT_BATTERY_LOW_V="12.0", ALERT_HEARTBEAT_URL=HEARTBEAT)
        h.io.influx_status = 401
        h.run()
        h.run(299)
        self.assertEqual(h.texts(), [])
        self.assertEqual(self.pings(h)[-1], HEARTBEAT)
        h.run(1)
        self.assertEqual(len(h.texts()), 1)
        self.assertTrue(h.texts()[0].startswith("Battery and stale-data alerts are not working: InfluxDB rejected the read token."))
        self.assertEqual(self.pings(h)[-1], HEARTBEAT + "/fail")
        h.io.influx_status = 503                              # transient: alert kept, nothing new
        h.run(60)
        self.assertEqual(len(h.texts()), 1)
        h.io.influx_status = 200
        h.run(60)
        self.assertEqual(len(h.texts()), 2)
        self.assertIn("working again", h.texts()[1])
        self.assertEqual(h.state()["alerts"], {})
        self.assertEqual(self.pings(h)[-1], HEARTBEAT)

    def test_influx_rejected_token_ignored_when_no_influx_check_configured(self):
        h = Harness(self, SIGNALK_MODE="existing")
        h.io.influx_status = 403
        h.run()
        h.run(600)
        self.assertEqual(h.texts(), [])

    def test_clock_backwards_clamps_and_boot_change_reannounces(self):
        h = Harness(self, ALERT_BATTERY_LOW_V="12.0")
        h.io.voltage = 11.0
        h.run()
        h.run(120)
        self.assertEqual(len(h.texts()), 1)
        state = h.state()
        state["alerts"]["battery:electrical.batteries.house.voltage"]["last_sent"] = alerts.iso(T0 + 10 * 86400)
        h.state_path.write_text(json.dumps(state))
        h.run(60)                                             # last_sent in the future is clamped to now: not re-sent yet
        self.assertEqual(len(h.texts()), 1)
        self.assertLessEqual(alerts.parse_iso(h.state()["alerts"]["battery:electrical.batteries.house.voltage"]["last_sent"]), h.io.clock)
        h.io.boot = "boot-2"
        h.run(60)
        self.assertTrue(any("restarted" in t for t in h.texts()))
        self.assertEqual(sum("is low" in t for t in h.texts()), 2)   # announced again after the restart

    def test_events_pruned_by_time_not_dict_order(self):
        h = Harness(self)
        h.run()
        state = h.state()
        state["events"] = {f"zz-{i:03d}": alerts.iso(T0 - 1000 + i) for i in range(60)}
        state["events"]["aa-newest"] = alerts.iso(T0 + 500)
        h.state_path.write_text(json.dumps(state))
        h.run(60)
        events = h.state()["events"]
        self.assertEqual(len(events), 50)
        self.assertIn("aa-newest", events)
        self.assertNotIn("zz-000", events)

    def test_heartbeat_fail_when_telegram_fails_and_ok_during_backup(self):
        h = Harness(self, ALERT_HEARTBEAT_URL=HEARTBEAT)
        h.io.telegram_error = True
        h.io.backup = {"result": "failed", "exit_status": 1, "finished_at": "2027-01-01T03:30:00Z"}
        h.run()
        pings = [u for m, u in h.io.requests if u.startswith("https://hc.example.org")]
        self.assertEqual(pings[-1], HEARTBEAT + "/fail")
        h2 = Harness(self, ALERT_HEARTBEAT_URL=HEARTBEAT)
        h2.io.down = {"vesselstack-backup.service"}
        h2.run()
        pings = [u for m, u in h2.io.requests if u.startswith("https://hc.example.org")]
        self.assertEqual(pings, [HEARTBEAT])
        h3 = Harness(self, ALERT_HEARTBEAT_URL=HEARTBEAT, TELEGRAM_BOT_TOKEN="")
        h3.run(TELEGRAM_BOT_TOKEN="")
        pings = [u for m, u in h3.io.requests if u.startswith("https://hc.example.org")]
        self.assertEqual(pings[-1], HEARTBEAT + "/fail")

    def test_heartbeat_fail_url_keeps_query(self):
        h = Harness(self, ALERT_HEARTBEAT_URL="https://hc2.example.org/ping/abc/?create=1&x=2")
        h.io.telegram_error = True
        h.io.backup = {"result": "failed", "exit_status": 1, "finished_at": "2027-01-01T03:30:00Z"}
        h.run()
        pings = [u for m, u in h.io.requests if u.startswith("https://hc2.example.org")]
        self.assertEqual(pings[-1], "https://hc2.example.org/ping/abc/fail?create=1&x=2")

    def test_deadline_skips_work_and_saves_after_each_delivery(self):
        h = Harness(self, ALERT_BATTERY_LOW_V="12.0")
        h.io.voltage = 11.0
        h.run()
        h.io.http_cost = 40.0                                 # each request burns 40 s of the 90 s budget
        h.io.down = {"vesselstack-chat.service"}
        h.run(200)
        self.assertGreater(h.state()["last_run_skipped"], 0)
        self.assertIn("deadline", h.err.getvalue())
        self.assertEqual(h.state()["last_run_ok"], True)

    def test_orphaned_entries_are_pruned(self):
        h = Harness(self, ALERT_BATTERY_LOW_V="12.0")
        h.io.voltage = 11.0
        h.run()
        h.run(120)
        self.assertTrue(h.state()["alerts"])
        h.run(60, ALERT_BATTERY_LOW_V="")
        self.assertEqual(h.state()["alerts"], {})

    def test_battery_data_missing_message(self):
        h = Harness(self, ALERT_BATTERY_LOW_V="12.0")
        h.io.voltage = 11.0
        h.run()
        h.run(120)
        h.io.voltage = None                                   # series vanished
        h.run(60)
        self.assertFalse(any("data is missing" in t for t in h.texts()))
        h.run(15 * 60)
        self.assertTrue(any("data is missing" in t for t in h.texts()))

    def test_reactivation_after_failed_clear_restarts(self):
        h = Harness(self, ALERT_BATTERY_LOW_V="12.0")
        h.io.voltage = 11.0
        h.run()
        h.run(120)
        h.io.voltage, h.io.telegram_error = 13.0, True
        h.run(60)                                             # clear fails, entry kept
        h.io.voltage, h.io.telegram_error = 11.0, False
        h.run(60)
        self.assertEqual(h.state()["alerts"]["battery:electrical.batteries.house.voltage"]["last_sent"], "")

    def test_dispatcher_health_in_doctor(self):
        h = Harness(self)
        h.run()
        env = settings()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            h.io.clock += 60
            alerts.cmd_doctor(env, h.io, h.state_path, print)
        self.assertEqual(out.getvalue().count("dispatcher"), 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            h.io.clock += 600
            alerts.cmd_doctor(env, h.io, h.state_path, print)
        self.assertIn("more than 5 minutes", out.getvalue())

    def test_unexpected_failure_is_recorded_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as data:
            fake = FakeIo()
            fake.http = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
            env = {**settings(), "VESSELSTACK_DATA": data}
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(alerts.main(["run"], env, fake), 0)
            state = json.loads((Path(data) / "alerts" / "state.json").read_text())
            self.assertFalse(state["last_run_ok"])
            self.assertEqual(state["last_error_class"], "RuntimeError")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                alerts.cmd_doctor(settings(), fake, Path(data) / "alerts" / "state.json", print)
            self.assertIn("last alerts run failed", out.getvalue())

    def test_refuses_root(self):
        fake = FakeIo()
        fake.uid = 0
        with tempfile.TemporaryDirectory() as data, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(alerts.main(["run"], {**settings(), "VESSELSTACK_DATA": data}, fake), 1)
            self.assertFalse((Path(data) / "alerts").exists())
        self.assertIn("refusing", err.getvalue())

    def test_status_and_test_exit_one_on_exception(self):
        with tempfile.TemporaryDirectory() as data:
            (Path(data) / "alerts").mkdir()
            (Path(data) / "alerts" / "state.json").write_text("{}")
            fake = FakeIo()
            fake.now = lambda: (_ for _ in ()).throw(RuntimeError("x"))
            env = {**settings(), "VESSELSTACK_DATA": data}
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(alerts.main(["test"], env, fake), 1)

    def test_state_lock_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as data:
            alerts_dir = Path(data) / "alerts"
            alerts_dir.mkdir()
            (alerts_dir / "state.lock").symlink_to(Path(data) / "elsewhere")
            with self.assertRaises(OSError):
                alerts.open_lock(alerts_dir / "state.json")

    def test_status_does_not_rename_corrupt_state(self):
        with tempfile.TemporaryDirectory() as data:
            d = Path(data) / "alerts"
            d.mkdir()
            (d / "state.json").write_text("{bad")
            with contextlib.redirect_stdout(io.StringIO()):
                alerts.main(["status"], {**settings(), "VESSELSTACK_DATA": data}, FakeIo())
            self.assertTrue((d / "state.json").exists())
            self.assertFalse((d / "state.json.corrupt").exists())

    def test_override_file_supplies_bot_token(self):
        with tempfile.TemporaryDirectory() as directory:
            override = Path(directory) / "boat-chat.env"
            override.write_text("TELEGRAM_BOT_TOKEN='abc:override'\nOTHER=1\n")
            merged = alerts.load_settings({"BOAT_CHAT_ENV": str(override), "TELEGRAM_BOT_TOKEN": "old"}, [])
            self.assertEqual(merged["TELEGRAM_BOT_TOKEN"], "abc:override")
            self.assertNotIn("OTHER", merged)


if __name__ == "__main__":
    unittest.main()
