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

    def test_executable_paths_are_not_http_or_settings_file_settable(self):
        for key in ('BOAT_CHAT_CODEX_BIN','BOAT_CHAT_CLAUDE_BIN'):
            self.assertNotIn(key,app.SETTING_KEYS)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'chat.env'
            with mock.patch.object(app,'ENV_CONFIG',path):
                app.update_settings({'BOAT_CHAT_CODEX_BIN':'/tmp/evil','BOAT_CHAT_CLAUDE_BIN':'/tmp/evil','BOAT_CHAT_PROVIDER':'local'})
                text=path.read_text()
                path.write_text(text+'BOAT_CHAT_CODEX_BIN=/tmp/evil\n')
                self.assertNotIn('BOAT_CHAT_CODEX_BIN',app.parse_env_config())
            self.assertNotIn('/tmp/evil',text)

    def test_runtime_paths_follow_state_directory_override(self):
        code=('import sys;sys.path.insert(0,"boat-chat");import memory_index,telemetry_cache,app,telegram_bot;'
              'print(memory_index.db_path());print(telemetry_cache.power_db_path());print(app.ENV_CONFIG);print(telegram_bot.STATE_DIR);print(telegram_bot.ENV_CONFIG)')
        root=Path(__file__).resolve().parents[1]
        env={k:v for k,v in os.environ.items() if k not in ('BOAT_CHAT_MEMORY_DB','BOAT_CHAT_POWER_DB')}
        env['BOAT_CHAT_STATE_DIR']='/var/state/boat'
        out=subprocess.run(['python3','-c',code],cwd=root,env=env,text=True,capture_output=True,check=True).stdout.split()
        self.assertEqual(out,['/var/state/boat/memory/boat_memory.sqlite','/var/state/boat/data/power_tracking.sqlite','/var/state/boat/boat-chat.env','/var/state/boat/memory','/var/state/boat/boat-chat.env'])

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
