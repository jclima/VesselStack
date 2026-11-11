import json
import os
import stat
import tempfile
import subprocess
import unittest
from pathlib import Path

from common.boat_profile import (ProfileError, canonical_json, legacy_default,
                                  load_profile, normalize_profile,
                                  profile_fingerprint, write_profile)


class BoatProfileTests(unittest.TestCase):
    def test_cli_mutations_honor_shared_lock_and_nested_handoff(self):
        cli = Path(__file__).resolve().parents[1] / "common" / "boat_profile_cli.py"
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); root = base / "root"; system = base / "system"
            root.mkdir(); system.mkdir()
            from common.lifecycle_lock import LifecycleLock
            with LifecycleLock(root, system) as lock:
                blocked = subprocess.run(["python3", str(cli), "init", "--root", str(root)],
                                         env={**os.environ, "VESSELSTACK_SYSTEM_ROOT": str(system)},
                                         capture_output=True, text=True)
                self.assertEqual(blocked.returncode, 2)
                self.assertIn("busy", blocked.stderr.lower())
                self.assertFalse((root / "config" / "boat-profile.json").exists())
                nested = subprocess.run(["python3", str(cli), "init", "--root", str(root)],
                                        env={**lock.child_env(), "VESSELSTACK_SYSTEM_ROOT": str(system)},
                                        pass_fds=lock.pass_fds(), capture_output=True, text=True)
                self.assertEqual(nested.returncode, 0, nested.stderr)
                self.assertTrue((root / "config" / "boat-profile.json").exists())
                readonly = subprocess.run(["python3", str(cli), "validate", str(root / "config" / "boat-profile.json")],
                                          capture_output=True, text=True)
                self.assertEqual(readonly.returncode, 0, readonly.stderr)

    def test_legacy_and_empty_profiles(self):
        default = legacy_default()
        self.assertEqual(len(default["engines"]), 2)
        self.assertEqual([t["id"] for t in default["fuel_tanks"]], ["0", "1"])
        empty = {"schema_version": 1, "engines": [], "battery_banks": [], "fuel_tanks": [], "primary_battery_bank": None}
        self.assertEqual(normalize_profile(empty), empty)

    def test_single_three_and_electric_profiles(self):
        p = {"schema_version": 1, "engines": [{"id": "e1", "label": "Electric", "path": "propulsion.e1", "kind": "electric", "fuel_rate_supported": False}], "battery_banks": [{"id": "house", "label": "House", "path": "electrical.batteries.house", "role": "house"}], "fuel_tanks": [{"id": "0", "label": "Tank", "path": "tanks.fuel.0"}], "primary_battery_bank": "house"}
        self.assertEqual(normalize_profile(p), p)
        p["engines"] += [{"id": f"e{i}", "label": f"E{i}", "path": f"propulsion.e{i}", "kind": "combustion", "fuel_rate_supported": True} for i in (2, 3)]
        self.assertEqual(len(normalize_profile(p)["engines"]), 3)
        omitted = dict(p); omitted["engines"] = [{k: v for k, v in p["engines"][0].items() if k != "fuel_rate_supported"}]
        self.assertFalse(normalize_profile(omitted)["engines"][0]["fuel_rate_supported"])

    def test_invalid_schema_paths_duplicates_and_size(self):
        p = legacy_default()
        for key, value in (("schema_version", 2), ("engines", [{}])):
            bad = dict(p); bad[key] = value
            with self.assertRaises(ProfileError): normalize_profile(bad)
        bad = json.loads(json.dumps(p)); bad["engines"][1]["path"] = "propulsion..bad"
        with self.assertRaises(ProfileError): normalize_profile(bad)
        bad = json.loads(json.dumps(p)); bad["battery_banks"][0]["role"] = "starter"
        with self.assertRaisesRegex(ProfileError, "role house"): normalize_profile(bad)
        bad = json.loads(json.dumps(p)); bad["engines"][1]["id"] = "port"
        with self.assertRaises(ProfileError): normalize_profile(bad)
        bad = json.loads(json.dumps(p)); bad["engines"] = bad["engines"] * 5
        with self.assertRaises(ProfileError): normalize_profile(bad)

    def test_fingerprint_tracks_bindings_and_is_stable(self):
        p = legacy_default(); same = json.loads(canonical_json(p))
        self.assertEqual(profile_fingerprint(p), profile_fingerprint(same))
        same["engines"][0]["path"] = "propulsion.other"
        self.assertNotEqual(profile_fingerprint(p), profile_fingerprint(same))

    def test_load_missing_diagnostic_and_rejects_malformed_or_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile, used_legacy = load_profile(root)
            self.assertTrue(used_legacy); self.assertEqual(profile, legacy_default())
            cfg = root / "config"; cfg.mkdir(); target = cfg / "boat-profile.json"
            target.write_text("not json")
            with self.assertRaises(ProfileError): load_profile(root)
            target.unlink(); target.symlink_to(root / "missing")
            with self.assertRaises(ProfileError): load_profile(root)

    def test_atomic_write_backup_mode_and_init_preservation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); first = legacy_default(); write_profile(root, first)
            target = root / "config/boat-profile.json"; original = target.read_bytes()
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
            changed = json.loads(json.dumps(first)); changed["engines"][0]["label"] = "Changed"
            write_profile(root, changed)
            self.assertTrue(list((root / "config/boat-profile-backups").glob("*.json")))
            self.assertNotEqual(target.read_bytes(), original)
            leftovers = list((root / "config").glob(".boat-profile.*")); self.assertEqual(leftovers, [])

    def test_backup_symlink_sentinel_is_not_followed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); write_profile(root, legacy_default())
            target = root / "config/boat-profile.json"
            stamp = __import__("hashlib").sha256(target.read_bytes()).hexdigest()[:16]
            backups = root / "config/boat-profile-backups"; backups.mkdir()
            sentinel = root / "sentinel"; sentinel.write_text("do not overwrite")
            (backups / f"boat-profile-{stamp}.json").symlink_to(sentinel)
            changed = json.loads(json.dumps(legacy_default())); changed["engines"][0]["label"] = "Changed"
            with self.assertRaises(ProfileError): write_profile(root, changed)
            self.assertEqual(sentinel.read_text(), "do not overwrite")


if __name__ == "__main__": unittest.main()
