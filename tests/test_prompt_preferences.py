from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "boat-chat"))

import app  # noqa: E402
import query_planner  # noqa: E402
import memory_index  # noqa: E402


class PromptPreferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(app, "_SYSTEM_PROMPT_CACHE", {"mtime": None, "text": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_provider_payload_includes_effective_timezone_and_metric_preference(self) -> None:
        values = {"BOAT_UNITS": "metric", "BOAT_CHAT_MAX_TOKENS": "1200"}
        with mock.patch.object(app, "setting_value", side_effect=lambda key, default=None: values.get(key, default)), \
             mock.patch.object(app, "http_post_json", return_value={"choices": [{"message": {"content": "ok"}}]}) as post:
            with mock.patch.object(app, "LOCAL_TZ", ZoneInfo("Europe/Oslo")):
                app.call_openai_compatible(
                    "How much fuel did I use today?", {"question_type": {}, "influx_history": {}},
                    provider_name="test", base_url="https://example.test/v1", api_key="key", model="model",
                )
        payload = post.call_args.args[1]
        system = payload["messages"][0]["content"]
        self.assertIn("effective timezone Europe/Oslo", system)
        self.assertIn("metric units", system)

    def test_provider_payload_uses_converted_context_without_mutating_source_for_both_preferences(self) -> None:
        source = {"influx_history": {"buckets": {"history": {"units": {"propulsion.port.temperature": "F"}, "numeric_summary": {"propulsion.port.temperature": {"min": 212, "avg": 212, "max": 212, "count": 1}}}}}, "question_type": {}}
        for units, expected, expected_unit in (("metric", 100.0, "°C"), ("us_customary", 212, "F")):
            original = json.loads(json.dumps(source))
            values = {"BOAT_UNITS": units, "BOAT_CHAT_MAX_TOKENS": "1200"}
            with mock.patch.object(app, "setting_value", side_effect=lambda key, default=None: values.get(key, default)), \
                 mock.patch.object(app, "http_post_json", return_value={"choices": [{"message": {"content": "ok"}}]}) as post:
                app.call_openai_compatible("temperature in UTC", source, provider_name="test", base_url="https://example.test/v1", api_key="key", model="model")
            user = post.call_args.args[1]["messages"][1]["content"]
            context = json.loads(user.split("Boat context JSON:\n", 1)[1].split("\n\nResponse mode:", 1)[0])
            metric = context["influx_history"]["buckets"]["history"]["numeric_summary"]["propulsion.port.temperature"]["min"]
            self.assertEqual(metric, expected)
            self.assertEqual(context["influx_history"]["buckets"]["history"]["units"]["propulsion.port.temperature"], expected_unit)
            self.assertIn("temperature in UTC", user)
            self.assertEqual(source, original)

    def test_missing_preferences_default_to_utc_and_us_customary(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(app, "setting_value", return_value=None):
            with mock.patch.object(app, "LOCAL_TZ", ZoneInfo("UTC")):
                prompt = app.system_prompt()
        self.assertIn("effective timezone UTC", prompt)
        self.assertIn("us_customary units", prompt)

    def test_missing_prompt_file_uses_default_prompt(self) -> None:
        with mock.patch.object(app, "BOAT_CHAT_AGENT", Path("/path/that/does/not/exist")), mock.patch.object(app, "LOCAL_TZ", ZoneInfo("Europe/Oslo")), mock.patch.object(app, "setting_value", return_value="metric"):
            prompt = app.system_prompt()
        self.assertIn("supplied unit conversions", prompt)
        self.assertIn("effective timezone Europe/Oslo", prompt)
        self.assertIn("metric units", prompt)

    def test_startup_defaults_are_utc_without_timezone_environment(self) -> None:
        env = dict(os.environ)
        env.pop("BOAT_TIMEZONE", None)
        env.pop("BOAT_UNITS", None)
        result = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, 'boat-chat'); import app; print(app.system_prompt())"], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
        self.assertIn("effective timezone UTC", result.stdout)
        self.assertIn("us_customary units", result.stdout)

    def test_footer_reads_unit_preference_each_call_but_timezone_is_startup_value(self) -> None:
        values = {"BOAT_UNITS": "metric"}
        with mock.patch.object(app, "setting_value", side_effect=lambda key, default=None: values.get(key, default)), \
             mock.patch.object(app, "LOCAL_TZ", ZoneInfo("Europe/Oslo")):
            first = app.system_prompt()
            values["BOAT_UNITS"] = "us_customary"
            with mock.patch.dict(os.environ, {"BOAT_TIMEZONE": "Pacific/Honolulu"}):
                second = app.system_prompt()
        self.assertIn("metric units", first)
        self.assertIn("us_customary units", second)
        self.assertIn("effective timezone Europe/Oslo", second)
        self.assertNotIn("Pacific/Honolulu", second)

    def test_effective_timezone_drives_query_window_without_mutating_context(self) -> None:
        oslo = ZoneInfo("Europe/Oslo")
        now = dt.datetime(2026, 7, 1, 23, 30, tzinfo=dt.timezone.utc)
        source = {"current_telemetry": {"temperature": {"value": 68, "unit": "°F"}}, "question_type": {}}
        with mock.patch.object(app, "LOCAL_TZ", oslo), mock.patch.object(query_planner, "LOCAL_TZ", oslo):
            window = app.resolve_fuel_usage_window("today", now.astimezone(oslo))
            plan = query_planner.build_query_plan("fuel today", now)
        self.assertEqual(window["start_utc"], "2026-07-01T22:00:00Z")
        self.assertEqual(plan["window"]["start_utc"], "2026-07-01T22:00:00Z")
        app.build_prompt("fuel today", source)
        self.assertEqual(source["current_telemetry"]["temperature"]["unit"], "°F")

    def test_request_time_line_follows_context_and_survives_trimming(self) -> None:
        context = {"now_utc": "2026-11-14T12:00:00Z", "question_type": {}, "answer_mode": "concise",
                   "current_telemetry": {"clock_status": "unverified", "values": {}},
                   "influx_history": {"blob": "x" * 5000}}
        with mock.patch.object(app, "LOCAL_TZ", ZoneInfo("Europe/Oslo")):
            prompt = app.build_prompt("battery?", context, max_chars=300)
            cli = app.build_cli_prompt("battery?", context)
        line = "Request time: 2026-11-14 13:00 CET (2026-11-14T12:00:00Z). Boat clock: unverified."
        self.assertIn(line, prompt)
        self.assertGreater(prompt.index(line), prompt.index("Boat context JSON:"))
        self.assertGreater(prompt.index(line), prompt.index('"context_trimming"'))
        self.assertLess(prompt.index(line), prompt.index("Question:\nbattery?"))
        self.assertNotIn("Request time", prompt.split("\n\nResponse mode:", 1)[0])
        self.assertIn(line, cli)
        self.assertIn("never report a stale RPM", cli)

    def test_data_age_rule_is_in_system_prompt(self) -> None:
        self.assertIn(app.DATA_AGE_RULE, app.system_prompt())
        self.assertIn(app.DATA_AGE_RULE, app.DEFAULT_SYSTEM_PROMPT)
        for path in (app.BOAT_CHAT_AGENT, app.KNOWLEDGE_ROOT / "answering.md"):
            self.assertIn(app.DATA_AGE_RULE, path.read_text(), str(path))

    def test_data_age_rule_appears_exactly_once_whichever_base_is_used(self) -> None:
        key = "Values in current_telemetry.stale_values are not current"
        cases = {
            "agent file": None,
            "fallback default": app.DEFAULT_SYSTEM_PROMPT,
            "custom without rule": "You are a custom boat assistant.",
            "rule wrapped across lines": app.DATA_AGE_RULE.replace(". ", ".\n").replace(", ", ",\n"),
        }
        for name, base in cases.items():
            with self.subTest(name):
                # setUp gave an empty cache, so system_prompt() loads its base through load_system_prompt().
                if base is None:
                    prompt = app.system_prompt()
                else:
                    with mock.patch.object(app, "load_system_prompt", return_value=base):
                        prompt = app.system_prompt()
                self.assertEqual(" ".join(prompt.split()).count(key), 1, name)

    def test_stale_canonical_key_keeps_authoritative_source_label(self) -> None:
        context = {"ha_telemetry": {"matched": [{"entity_id": "sensor.temp", "unit": "°C", "state": "20"}]},
                   "influx_history": {"units": {"sensor.temp": "F"}}, "question_type": {}}
        with mock.patch.object(app, "setting_value", side_effect=lambda key, default=None: "us_customary" if key == "BOAT_UNITS" else default):
            prompt = app.build_prompt("temperature", context)
        encoded = json.loads(prompt.split("Boat context JSON:\n", 1)[1].split("\n\nResponse mode:", 1)[0])
        self.assertEqual(encoded["ha_telemetry"]["matched"][0]["unit"], "°C")

    def test_nautical_units_remain_unchanged_in_normalized_context(self) -> None:
        context = {"current_telemetry": {"navigation.speedOverGround": {"value": 12, "unit": "kn"}}, "question_type": {}}
        with mock.patch.object(app, "setting_value", side_effect=lambda key, default=None: "us_customary" if key == "BOAT_UNITS" else default):
            prompt = app.build_prompt("speed", context)
        self.assertIn('"unit":"kn"', prompt)

    def test_actual_knowledge_rebuild_replaces_stale_guidance(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as directory:
            root = Path(directory)
            answering = root / "answering.md"; telemetry = root / "telemetry.md"
            answering.write_text("Use Pacific time and gallons used.\n")
            telemetry.write_text("All fuel answers use US units.\n")
            db = root / "memory.sqlite"
            with mock.patch.object(memory_index, "DEFAULT_DOC_PATHS", [answering, telemetry]), mock.patch.dict(os.environ, {"BOAT_CHAT_MEMORY_DB": str(db)}):
                memory_index.ensure()
                self.assertTrue(memory_index.search("Pacific gallons used"))
                answering.write_text((ROOT / "boat-chat" / "knowledge" / "answering.md").read_text())
                telemetry.write_text((ROOT / "boat-chat" / "knowledge" / "telemetry.md").read_text())
                memory_index.ensure()
                hits = memory_index.search("effective runtime timezone selected display units unknown source units")
            text = " ".join(str(hit) for hit in hits)
            self.assertTrue(hits)
            self.assertIn("effective runtime timezone", text)
            self.assertIn("selected Boat Chat display units", text)
            self.assertNotIn("local Pacific time", text)
            self.assertNotIn("All fuel answers use US units", text)


if __name__ == "__main__":
    unittest.main()
