#!/usr/bin/env python3
import datetime as dt
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "boat-chat"))
import app
import query_planner

NOW = dt.datetime(2026, 8, 27, 12, 0, tzinfo=query_planner.LOCAL_TZ)

class QueryPlannerTests(unittest.TestCase):
    def test_question_corpus(self):
        cases = {
            "Where is the boat?": ("position", False), "How far away is the nearest vessel?": ("ais", False),
            "Did the bilge pump run last night?": ("bilge", True), "How much water is in the fresh tank?": ("tank", False),
            "How hot did the port engine get yesterday?": ("temperature", True), "What caused the battery dip?": ("battery", True),
            "How many hours did the generator run this month?": ("generator", True),
        }
        for question, (signal, historical) in cases.items():
            with self.subTest(question=question):
                plan = query_planner.build_query_plan(question, now=NOW)
                self.assertIn(signal, plan["signals"]); self.assertEqual(historical, plan["historical"])

    def test_engine_comparison_rpm_filter(self):
        plan = query_planner.build_query_plan("Compare engine temperatures at 2000 RPM", now=NOW)
        self.assertIn("temperature", plan["signals"]); self.assertEqual("port_starboard", plan["comparison"])
        self.assertEqual({"rpm_min": 2000, "rpm_max": 2000}, plan["filters"])

    def test_classifier_and_snapshot_share_synonyms(self):
        self.assertIn("position", app.telemetry_concepts("Where is the boat?"))
        self.assertIn("ais", app.telemetry_concepts("nearest vessel"))

    def test_last_night_is_exact(self):
        window = query_planner.resolve_time_window("last night", now=NOW)
        self.assertEqual("2026-08-26 18:00 UTC", window["start_local"]); self.assertEqual("2026-08-27 06:00 UTC", window["stop_local"])

    def test_followup_keeps_previous_question(self):
        history = [{"role": "user", "content": "How hot did the port engine get yesterday?"}]
        effective = app.effective_question("What about starboard?", history)
        self.assertIn("port engine", effective); self.assertIn("starboard", effective)

    def test_context_budget_is_valid_json(self):
        context = {"boat_facts":{"name":"x"}, "query_plan":{"signals":["battery"]}, "local_docs":[{"text":"x"*5000}]*5}
        decoded = json.loads(app.serialize_context_with_budget(context, 500))
        self.assertEqual(["battery"], decoded["query_plan"]["signals"])

    def test_per_client_rate_limit(self):
        app.RATE_LIMIT_REQUESTS.clear()
        handler = object.__new__(app.BoatChatHandler)
        handler.client_address = ("10.0.0.20", 1234)
        self.assertTrue(all(handler.rate_limit_allowed() for _ in range(app.RATE_LIMIT_MAX_REQUESTS)))
        self.assertFalse(handler.rate_limit_allowed())

    def test_event_history_answer(self):
        context = {"query_plan":{"operation":"event_count"}, "ha_event_history":{"label":"last night","entities":{"binary_sensor.bilge":{"friendly_name":"Bilge Pump","transitions":[{"state":"on","time_local":"01:00"},{"state":"off","time_local":"01:02"}]}}}}
        self.assertIn("activated 1 time", app.answer_from_event_history(context))

    def test_answer_experience_builds_evidence_followups_and_metrics(self):
        context = {
            "query_plan":{"signals":["battery"],"operation":"minimum","historical":True,"window":{"label":"last night","start_local":"start","stop_local":"stop"}},
            "context_profile":{"live_telemetry":True,"history":True,"elapsed_ms":25},
            "battery_voltage":{"voltage":{"min":12.4,"avg":12.8,"max":13.2,"latest":12.9}},
        }
        payload = app.answer_experience(context)
        self.assertGreaterEqual(len(payload["evidence"]), 2)
        self.assertIn("Graph battery voltage", payload["followups"])
        self.assertEqual("Battery voltage", payload["metrics"][0]["label"])

    def test_experience_status_uses_cached_shape(self):
        original = app.telemetry_cache.get_summary
        app.telemetry_cache.get_summary = lambda *args, **kwargs: {"current_telemetry":{"values":{"electrical.batteries.shunt.capacity.stateOfCharge":88,"electrical.batteries.shunt.voltage":12.7}},"ha_states":{},"cache":{"age_seconds":10}}
        try:
            payload = app.experience_status()
            self.assertEqual(6, len(payload["cards"]))
            self.assertIn("88%", next(card["value"] for card in payload["cards"] if card["id"] == "battery"))
        finally:
            app.telemetry_cache.get_summary = original

    def test_briefing_and_answer_modes(self):
        context={"current_telemetry":{"values":{"electrical.batteries.shunt.capacity.stateOfCharge":90,"electrical.batteries.shunt.voltage":12.8}},"ha_states":{"sensor.boat_health_summary":{"state":"OK"}}}
        self.assertIn("Boat briefing: OK", app.answer_from_briefing("Brief me",context))
        prompt=app.build_prompt("Why?",{"answer_mode":"diagnose","boat_facts":{}})
        self.assertIn("Response mode: diagnose",prompt)

    def test_insights_include_relative_ais_targets(self):
        old_summary, old_trip, old_tasks = app.telemetry_cache.get_summary, app.last_trip_window, app.session_store.list_maintenance
        app.telemetry_cache.get_summary = lambda *args, **kwargs: {"current_telemetry":{"values":{}},"ha_states":{},"ais":{"target_count":1,"targets":[{"name":"Ferry","distance_nm":2.2,"bearing_deg":45.0}]}}
        app.last_trip_window = lambda: None
        app.session_store.list_maintenance = lambda: []
        try:
            payload = app.experience_insights()
            self.assertEqual(45.0, payload["ais"]["targets"][0]["bearing_deg"])
            self.assertAlmostEqual(90.0, app.bearing_deg({"latitude":0,"longitude":0},{"latitude":0,"longitude":1}))
        finally:
            app.telemetry_cache.get_summary, app.last_trip_window, app.session_store.list_maintenance = old_summary, old_trip, old_tasks

    def test_insights_mark_overdue_maintenance(self):
        old_summary, old_trip, old_tasks = app.telemetry_cache.get_summary, app.last_trip_window, app.session_store.list_maintenance
        app.telemetry_cache.get_summary = lambda *args, **kwargs: {"current_telemetry":{"values":{}},"ha_states":{},"ais":{"targets":[],"target_count":0}}
        app.last_trip_window = lambda: None
        app.session_store.list_maintenance = lambda: [{"id":1,"title":"Old task","due_date":"2000-01-01","completed":False}]
        try:
            payload = app.experience_insights()
            self.assertEqual("overdue", payload["maintenance"][0]["due_status"])
            self.assertEqual({"open":1,"overdue":1}, payload["maintenance_counts"])
        finally:
            app.telemetry_cache.get_summary, app.last_trip_window, app.session_store.list_maintenance = old_summary, old_trip, old_tasks

if __name__ == "__main__": unittest.main()
