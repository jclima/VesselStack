import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('deploy_chat',ROOT/'scripts/deploy-chat.py')
deploy=importlib.util.module_from_spec(spec);spec.loader.exec_module(deploy)
privacy_spec=importlib.util.spec_from_file_location('public_tree',ROOT/'scripts/check-public-tree.py')
privacy=importlib.util.module_from_spec(privacy_spec);privacy_spec.loader.exec_module(privacy)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'source';self.target=self.root/'live'
        for root in [self.source,self.target]:
            (root/'boat-chat/static').mkdir(parents=True)
            (root/'boat-chat/requirements.txt').write_text('same\n')
        for name in ['app.py','cruise_efficiency.py']:
            (self.source/'boat-chat'/name).write_text('new\n')
            (self.target/'boat-chat'/name).write_text('old\n')
        (self.target/'boat-chat/app.py').chmod(0o640)
        (self.target/'boat-chat/boat_facts.json').write_text('private facts\n')
        (self.target/'.mcp.json').write_text('private credentials\n')
        self.backup=self.root/'backup'
        self.names=['boat-chat/cruise_efficiency.py','boat-chat/app.py']

    def tearDown(self):self.temp.cleanup()

    def git(self,source,*args):
        if args[0]=='status':return ''
        if args[0]=='ls-files':return '\n'.join(self.names+['boat-chat/boat_facts.json','.mcp.json','BOAT_AGENT.md','systemd/chat.service'])
        return 'fixture-commit'

    def apply(self):
        with mock.patch.object(deploy,'git',side_effect=self.git):
            return deploy.apply(self.source,self.target,self.names,self.backup)

    def test_allowlist_excludes_configuration_and_infrastructure(self):
        with mock.patch.object(deploy,'git',side_effect=self.git):
            self.assertEqual(deploy.deploy_files(self.source),self.names)

    def test_deploy_and_rollback_preserve_private_files_and_metadata(self):
        self.apply()
        self.assertEqual((self.target/'boat-chat/app.py').read_text(),'new\n')
        self.assertEqual((self.target/'boat-chat/app.py').stat().st_mode & 0o777,0o640)
        self.assertEqual((self.target/'boat-chat/boat_facts.json').read_text(),'private facts\n')
        self.assertEqual((self.target/'.mcp.json').read_text(),'private credentials\n')
        deploy.rollback(self.backup)
        self.assertEqual((self.target/'boat-chat/app.py').read_text(),'old\n')

    def test_later_edits_prevent_rollback(self):
        self.apply();(self.target/'boat-chat/app.py').write_text('later work\n')
        with self.assertRaisesRegex(RuntimeError,'later edits'):deploy.rollback(self.backup)
        self.assertEqual((self.target/'boat-chat/app.py').read_text(),'later work\n')

    def test_corrupt_backup_prevents_rollback(self):
        self.apply();(self.backup/'files/boat-chat/app.py').write_text('corrupt')
        with self.assertRaisesRegex(RuntimeError,'checksum'):deploy.rollback(self.backup)

    def test_new_files_are_removed_only_during_rollback(self):
        (self.source/'boat-chat/new.py').write_text('new module')
        self.names.insert(0,'boat-chat/new.py');self.apply()
        self.assertTrue((self.target/'boat-chat/new.py').exists())
        deploy.rollback(self.backup)
        self.assertFalse((self.target/'boat-chat/new.py').exists())

    def test_copy_failure_restores_prior_files(self):
        original=deploy.atomic_write;calls=[]
        def fail_once(*args):
            calls.append(args[0])
            if len(calls)==2:raise OSError('simulated failure')
            return original(*args)
        with mock.patch.object(deploy,'atomic_write',side_effect=fail_once):
            with self.assertRaises(OSError):self.apply()
        for name in self.names:self.assertEqual((self.target/name).read_text(),'old\n')

    def test_failed_readiness_automatically_restores_old_files(self):
        argv=['deploy-chat.py','--source',str(self.source),'--target',str(self.target),'--apply','--restart-chat']
        with mock.patch('sys.argv',argv),mock.patch.object(deploy,'git',side_effect=self.git),mock.patch.object(deploy.subprocess,'run'),mock.patch.object(deploy,'ready',side_effect=[False,True]):
            with self.assertRaisesRegex(RuntimeError,'previous files restored'):deploy.main()
        for name in self.names:self.assertEqual((self.target/name).read_text(),'old\n')

    def test_source_symlink_is_rejected(self):
        p=self.source/'boat-chat/app.py';p.unlink();p.symlink_to(self.source/'boat-chat/cruise_efficiency.py')
        with mock.patch.object(deploy,'git',side_effect=self.git):
            with self.assertRaisesRegex(RuntimeError,'regular tracked'):deploy.deploy_files(self.source)

    def test_target_symlink_is_rejected(self):
        p=self.target/'boat-chat/app.py';p.unlink();p.symlink_to(self.target/'boat-chat/cruise_efficiency.py')
        with self.assertRaisesRegex(RuntimeError,'symlink'):self.apply()


class PrivacyTests(unittest.TestCase):
    def test_rejects_private_artifacts_without_printing_content(self):
        for name in ['boat-chat/boat_facts.json','.mcp.json','pre-change-backups/config.txt','control-panel/data/audit.jsonl']:
            self.assertIn('runtime/private artifact',privacy.findings(name,b'private content'))

    def test_generic_examples_and_loopback_are_allowed(self):
        self.assertEqual(privacy.findings('boat-chat/boat_facts.example.json',b'{"name":"My Boat"}'),[])
        self.assertEqual(privacy.findings('example.txt',b'http://127.0.0.1:8765'),[])

    def test_credential_pattern_is_reported_without_value(self):
        value=b'ghp_'+b'x'*40
        result=privacy.findings('fixture.txt',value)
        self.assertEqual(result,['credential pattern'])
        self.assertNotIn(value.decode(),str(result))
