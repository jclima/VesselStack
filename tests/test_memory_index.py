#!/usr/bin/env python3
"""Memory index: idempotent ensure(), hash-driven refresh, and the readiness it feeds."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "boat-chat"))

import app  # noqa: E402
import memory_index  # noqa: E402


class EnsureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.db = base / "state" / "memory" / "boat_memory.sqlite"
        self.docs = [base / "answering.md", base / "telemetry.md"]
        self.docs[0].write_text("# Answering\n\nAnswer from live data first.\n")
        self.docs[1].write_text("# Telemetry\n\nBattery voltage comes from the shunt.\n")
        for patcher in (
            mock.patch.object(memory_index, "DEFAULT_DOC_PATHS", self.docs),
            mock.patch.dict(os.environ, {"BOAT_CHAT_MEMORY_DB": str(self.db)}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tempdir.cleanup)

    def test_fresh_state_is_built_once_and_then_left_alone(self) -> None:
        self.assertFalse(memory_index.status()["exists"])
        first = memory_index.ensure()
        self.assertEqual("rebuilt", first["action"])
        self.assertEqual("missing", first["reason"])
        self.assertGreater(first["chunks"], 0)
        stamp = memory_index.status()["last_rebuild"]["time"]
        second = memory_index.ensure()
        self.assertEqual("unchanged", second["action"])
        self.assertEqual(first["chunks"], second["chunks"])
        self.assertEqual(stamp, memory_index.status()["last_rebuild"]["time"])

    def test_changed_documents_trigger_a_rebuild(self) -> None:
        memory_index.ensure()
        self.docs[1].write_text("# Telemetry\n\nBattery voltage comes from the shunt.\n\n## Tanks\n\nFuel is tank one.\n")
        again = memory_index.ensure()
        self.assertEqual(("rebuilt", "documents changed"), (again["action"], again["reason"]))
        self.assertTrue(any("Fuel is tank one" in str(hit) for hit in memory_index.search("fuel tank one")))
        self.assertEqual("unchanged", memory_index.ensure()["action"])

    def test_empty_index_is_rebuilt_and_missing_documents_fail_loudly(self) -> None:
        memory_index.ensure()
        conn = memory_index.connect()
        with conn:
            conn.execute("delete from chunks")
        conn.close()
        self.assertEqual("empty", memory_index.ensure()["reason"])
        for doc in self.docs:
            doc.unlink()
        with self.assertRaises(RuntimeError):
            memory_index.ensure()

    def test_readiness_memory_layer_follows_the_index(self) -> None:
        with mock.patch.object(app.telemetry_cache, "status", return_value={"items": []}):
            before = app.readiness_status()["layers"]["memory"]
            self.assertEqual({"ready": False, "chunks": 0}, before)
            memory_index.ensure()
            after = app.readiness_status()["layers"]["memory"]
        self.assertTrue(after["ready"])
        self.assertGreater(after["chunks"], 0)

    def test_bundled_default_documents_produce_chunks(self) -> None:
        # The real default knowledge files shipped in the repository must index.
        with mock.patch.object(memory_index, "DEFAULT_DOC_PATHS", [
            ROOT / "boat-chat" / "knowledge" / "answering.md",
            ROOT / "boat-chat" / "knowledge" / "telemetry.md",
        ]):
            self.assertGreater(memory_index.ensure()["chunks"], 0)


class TelemetryIndexerTests(unittest.TestCase):
    def test_indexer_refreshes_memory_first_and_survives_a_failure(self) -> None:
        import telemetry_indexer

        with mock.patch.object(telemetry_indexer.memory_index, "ensure", side_effect=RuntimeError("boom")), \
                mock.patch.object(telemetry_indexer.telemetry_cache, "connect"), \
                mock.patch.object(telemetry_indexer.telemetry_cache, "prune_category", return_value=0), \
                mock.patch.object(telemetry_indexer.telemetry_cache, "put_summary"), \
                mock.patch.object(telemetry_indexer, "refresh_latest_context", return_value={}), \
                mock.patch.object(telemetry_indexer.app, "engine_run_history_summary", return_value={}), \
                mock.patch.object(telemetry_indexer.app, "fuel_usage_summary", return_value={}), \
                mock.patch.object(telemetry_indexer.app, "fuel_economy_summary", return_value={}), \
                mock.patch.object(telemetry_indexer.app, "shore_power_history_summary", return_value={}), \
                mock.patch.object(telemetry_indexer.app, "battery_voltage_summary", return_value={}):
            result = telemetry_indexer.refresh_all()
        first = result["items"][0]
        self.assertEqual(("memory_index", "documents", False), (first["category"], first["key"], first["ok"]))
        self.assertIn("boom", first["error"])
        self.assertGreater(len(result["items"]), 1)  # the telemetry refresh still ran


if __name__ == "__main__":
    unittest.main()
