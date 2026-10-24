import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "boat-chat"))
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
import app

class UpdateCompatibilityTests(unittest.TestCase):
    def test_default_sensitive_access_remains_protected(self):
        with mock.patch.object(app, 'setting_value', side_effect=lambda key,default=None:default):
            self.assertTrue(app.access_token_required())

    def test_integration_values_survive_settings_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'chat.env'
            path.write_text('INFLUXDB_ORG=fixture-org\nINFLUXDB_TOKEN=fixture-only-token\nSIGNALK_URL=http://signalk.test:3000\n')
            with mock.patch.object(app,'ENV_CONFIG',path):
                app.write_env_config({'BOAT_CHAT_PROVIDER':'local'})
            text=path.read_text()
            self.assertIn('INFLUXDB_ORG=fixture-org',text)
            self.assertIn('INFLUXDB_TOKEN=fixture-only-token',text)
            self.assertIn('SIGNALK_URL=http://signalk.test:3000',text)

    def test_economy_query_uses_configured_bucket(self):
        with mock.patch.object(app,'INFLUX_HISTORY_BUCKET','fixture_history'),mock.patch.object(app,'query_influx',return_value=[]) as query,mock.patch.object(app.telemetry_cache,'put_summary'):
            app.fuel_economy_summary(use_cache=False)
        self.assertIn('from(bucket: "fixture_history")',query.call_args.args[0])

    def test_outing_note_uses_local_facts(self):
        with mock.patch.object(app,'load_boat_facts',return_value={'navigation':{'location_note':'Synthetic non-tidal test area'}}):
            result=app.outing_brief_context('tomorrow',{})
        self.assertEqual(result['location_note'],'Synthetic non-tidal test area')

    def test_ready_flag_is_required_in_lifecycle_health(self):
        text=(Path(__file__).resolve().parents[1]/'scripts/vesselstackctl').read_text()
        block=text[text.index('check_chat_endpoint() {'):text.index('status_stack() {')]
        for body,expected in [('{"ok":true,"ready":true}',0),('{"ok":true,"ready":false}',1),('invalid',1)]:
            harness='curl() { cat; };\n'+block+'\ncheck_chat_endpoint http://127.0.0.1/health'
            result=subprocess.run(['bash','-c',harness],input=body,text=True,capture_output=True)
            self.assertEqual(result.returncode,expected)
