#!/usr/bin/env python3
import json
import datetime as dt
import os
import tempfile
import threading
import unittest
import urllib.request
import sys
from pathlib import Path
from http.server import ThreadingHTTPServer
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "boat-chat"))
import app
import session_store

class ApiIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_env_config = app.ENV_CONFIG
        app.ENV_CONFIG = Path(self.tempdir.name) / "missing.env"
        self.original_memory_db = os.environ.get("BOAT_CHAT_MEMORY_DB")
        os.environ["BOAT_CHAT_MEMORY_DB"] = str(Path(self.tempdir.name) / "test.sqlite")
        os.environ["BOAT_CHAT_PROVIDER"] = "local"
        app.RATE_LIMIT_REQUESTS.clear()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.BoatChatHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        app.ENV_CONFIG = self.original_env_config
        if self.original_memory_db is None: os.environ.pop("BOAT_CHAT_MEMORY_DB", None)
        else: os.environ["BOAT_CHAT_MEMORY_DB"] = self.original_memory_db
        os.environ.pop("BOAT_CHAT_PROVIDER", None)
        self.tempdir.cleanup()

    def post(self, path, payload):
        request = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}{path}", data=json.dumps(payload).encode(), headers={"Content-Type":"application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=5) as response: return json.loads(response.read())

    def test_chat_feedback_and_clear_flow(self):
        result = self.post("/api/chat", {"message":"What is the boat name?", "session_id":"web:test"})
        self.assertIn("VesselStack", result["answer"]); self.assertIn("request_id", result)
        self.assertEqual({"context_profile", "query_plan"}, set(result["context"]))
        self.assertTrue(self.post("/api/feedback", {"request_id":result["request_id"],"session_id":"web:test","rating":"helpful"})["ok"])
        self.assertEqual(2, len(session_store.get_session("web:test")["turns"]))
        self.post("/api/session/clear", {"session_id":"web:test"})
        self.assertEqual([], session_store.get_session("web:test")["turns"])

    def test_api_json_serializes_resolved_time_windows(self):
        encoded = json.dumps({"window":{"start":dt.datetime(2026,8,28,8,0),"day":dt.date(2026,8,28)}}, default=app.json_default)
        decoded = json.loads(encoded)
        self.assertEqual("2026-08-28T08:00:00", decoded["window"]["start"])
        self.assertEqual("2026-08-28", decoded["window"]["day"])

    def test_capabilities_are_public_but_live_status_requires_token(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.server.server_port}/api/capabilities",timeout=5) as response:
            self.assertTrue(json.loads(response.read())["groups"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{self.server.server_port}/api/status",timeout=5)
        self.assertEqual(401,caught.exception.code)

    def test_insights_and_maintenance_require_dedicated_token(self):
        os.environ["BOAT_CHAT_ACCESS_TOKEN"] = "test-token"
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(f"http://127.0.0.1:{self.server.server_port}/api/insights",timeout=5)
            self.assertEqual(401,caught.exception.code)
            request=urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}/api/maintenance",data=json.dumps({"title":"Inspect belts"}).encode(),headers={"Content-Type":"application/json","X-Boat-Chat-Token":"test-token"},method="POST")
            with urllib.request.urlopen(request,timeout=5) as response:
                self.assertEqual("Inspect belts",json.loads(response.read())["task"]["title"])
        finally:
            os.environ.pop("BOAT_CHAT_ACCESS_TOKEN",None)

if __name__ == "__main__": unittest.main()
