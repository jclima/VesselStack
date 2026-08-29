#!/usr/bin/env python3
import tempfile
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "boat-chat"))
import session_store

class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "sessions.sqlite"

    def tearDown(self): self.tempdir.cleanup()

    def test_session_round_trip_and_clear(self):
        session_store.append_exchange("web:abc", "battery?", "13.2 V", {"signals":["battery"]}, self.path)
        saved = session_store.get_session("web:abc", self.path)
        self.assertEqual(2, len(saved["turns"])); self.assertEqual(["battery"], saved["query_plan"]["signals"])
        session_store.clear_session("web:abc", self.path)
        self.assertEqual([], session_store.get_session("web:abc", self.path)["turns"])

    def test_feedback_and_audit(self):
        session_store.record_request("req1", "web:abc", "hash", {"signals":[]}, {"history":False}, 10, "answered", self.path)
        session_store.record_feedback("req1", "web:abc", "incomplete", "missing tank", self.path)
        status = session_store.status(self.path)
        self.assertEqual(1, status["chat_requests"]); self.assertEqual(1, status["chat_feedback"])

    def test_rejects_invalid_rating_and_session(self):
        self.assertEqual("", session_store.normalize_session_id("bad session/id"))
        with self.assertRaises(ValueError): session_store.record_feedback("r", "s", "maybe", path=self.path)
        with self.assertRaises(ValueError): session_store.record_feedback("missing", "s", "wrong", path=self.path)

    def test_maintenance_round_trip(self):
        task = session_store.save_maintenance({"title":"Change impeller","due_date":"2026-09-15","notes":"Port engine"}, self.path)
        self.assertFalse(task["completed"])
        task = session_store.save_maintenance({**task,"completed":True}, self.path)
        self.assertTrue(task["completed"])
        self.assertEqual("Change impeller", session_store.list_maintenance(self.path)[0]["title"])

    def test_maintenance_round_trip(self):
        task = session_store.save_maintenance({"title":"Change impeller","due_date":"2026-09-15","notes":"Port engine"}, self.path)
        self.assertFalse(task["completed"])
        task = session_store.save_maintenance({**task,"completed":True}, self.path)
        self.assertTrue(task["completed"])
        self.assertEqual("Change impeller", session_store.list_maintenance(self.path)[0]["title"])

if __name__ == "__main__": unittest.main()
