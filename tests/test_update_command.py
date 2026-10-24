import importlib.util
import json
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('update_deploy',ROOT/'scripts/deploy-chat.py')
deploy=importlib.util.module_from_spec(spec);spec.loader.exec_module(deploy)


class UpdateCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'source space';self.target=self.root/'live space'
        (self.source/'scripts').mkdir(parents=True);self.target.mkdir()
        (self.source/'scripts/deploy-chat.py').touch()
        self.config=self.root/'update.env'
        self.config.write_text(f'SOURCE_DIRECTORY="{self.source}"\nTARGET_DIRECTORY="{self.target}"\nCHAT_SERVICE="vesselstack-chat.service"\nTELEGRAM_SERVICE="vesselstack-chat-telegram.service"\n')
        self.bin=self.root/'bin';self.bin.mkdir()
        python=self.bin/'python3'
        python.write_text('#!/usr/bin/python3\nimport sys,json\nprint(json.dumps(sys.argv[1:]))\n');python.chmod(0o755)
        self.env=dict(os.environ,PATH=str(self.bin)+':'+os.environ['PATH'],VESSELSTACK_UPDATE_CONFIG=str(self.config))

    def tearDown(self):self.temp.cleanup()

    def run_command(self,*args):
        return subprocess.run(['bash',str(ROOT/'scripts/vesselstack-update'),*args],env=self.env,text=True,capture_output=True)

    def test_default_pulls_and_previews_without_apply(self):
        result=self.run_command();self.assertEqual(result.returncode,0,result.stderr)
        argv=json.loads(result.stdout);self.assertIn('--pull',argv);self.assertNotIn('--apply',argv)
        self.assertEqual(argv[argv.index('--source')+1],str(self.source))
        self.assertEqual(argv[argv.index('--target')+1],str(self.target))

    def test_apply_pulls_tests_and_restarts_via_helper(self):
        result=self.run_command('--apply');self.assertEqual(result.returncode,0,result.stderr)
        argv=json.loads(result.stdout)
        for flag in ['--pull','--apply','--restart-chat','--telegram-service']:self.assertIn(flag,argv)

    def test_rollback_does_not_pull(self):
        result=self.run_command('--rollback','/example/backup with spaces')
        self.assertEqual(result.returncode,0,result.stderr);argv=json.loads(result.stdout)
        self.assertNotIn('--pull',argv)
        self.assertEqual(argv[argv.index('--rollback')+1],'/example/backup with spaces')

    def test_unknown_or_extra_arguments_fail(self):
        for args in [('--unknown',),('--apply','extra'),('--rollback',)]:
            self.assertEqual(self.run_command(*args).returncode,2)

    def test_missing_configuration_fails_before_helper(self):
        self.config.unlink();result=self.run_command();self.assertNotEqual(result.returncode,0)
        self.assertIn('Missing readable update configuration',result.stderr)


class WorkerRestartTests(unittest.TestCase):
    def args(self):return types.SimpleNamespace(service='vesselstack-chat.service',telegram_service='vesselstack-chat-telegram.service')

    def test_active_changed_worker_restarts(self):
        with mock.patch.object(deploy.subprocess,'run',return_value=types.SimpleNamespace(returncode=0)):
            self.assertEqual(deploy.update_units(self.args(),['boat-chat/telegram_bot.py']),['vesselstack-chat.service','vesselstack-chat-telegram.service'])

    def test_inactive_worker_stays_stopped(self):
        with mock.patch.object(deploy.subprocess,'run',return_value=types.SimpleNamespace(returncode=3)):
            self.assertEqual(deploy.update_units(self.args(),['boat-chat/telegram_bot.py']),['vesselstack-chat.service'])

    def test_unchanged_worker_is_not_probed_or_restarted(self):
        with mock.patch.object(deploy.subprocess,'run') as run:
            self.assertEqual(deploy.update_units(self.args(),['boat-chat/app.py']),['vesselstack-chat.service'])
        run.assert_not_called()
