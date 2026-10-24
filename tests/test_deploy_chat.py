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
        argv=['deploy-chat.py','--source',str(self.source),'--target',str(self.target),'--apply','--restart-chat','--allow-unsigned']
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


class SourceTrustTests(unittest.TestCase):
    """Pulled source must be verified and must never execute as root."""
    def source(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        return Path(self.temp.name)

    def test_source_code_runs_as_checkout_owner_not_root(self):
        source=self.source();entry=mock.Mock(pw_uid=1000,pw_dir='/home/dev');entry.pw_name='dev'
        with mock.patch.object(deploy.os,'geteuid',return_value=0),mock.patch.object(deploy,'owner_name',return_value=entry):
            command=deploy.unprivileged_command(source,['python3','-m','unittest'])
        self.assertEqual(command[:4],['runuser','-u','dev','--']);self.assertIn('HOME=/home/dev',command)
        self.assertEqual(command[-3:],['python3','-m','unittest'])

    def test_root_owned_checkout_is_refused_when_running_as_root(self):
        source=self.source();entry=mock.Mock(pw_uid=0,pw_dir='/root');entry.pw_name='root'
        with mock.patch.object(deploy.os,'geteuid',return_value=0),mock.patch.object(deploy,'owner_name',return_value=entry):
            with self.assertRaisesRegex(RuntimeError,'as root'):deploy.unprivileged_command(source,['true'])

    def test_unprivileged_invocation_is_not_wrapped(self):
        with mock.patch.object(deploy.os,'geteuid',return_value=1000):
            self.assertEqual(deploy.unprivileged_command(Path('/x'),['git','status']),['git','status'])

    def test_tests_and_checker_never_run_directly_as_root(self):
        source=self.source();entry=mock.Mock(pw_uid=1000,pw_dir='/home/dev');entry.pw_name='dev'
        with mock.patch.object(deploy.os,'geteuid',return_value=0),mock.patch.object(deploy,'owner_name',return_value=entry),mock.patch.object(deploy.subprocess,'run') as run:
            deploy.run_unprivileged(source,['python3','scripts/check-public-tree.py'],{'PYTHONPATH':'x'})
        self.assertEqual(run.call_args.args[0][:4],['runuser','-u','dev','--'])

    def test_origin_must_match_pinned_remote(self):
        with mock.patch.object(deploy,'git',return_value='https://evil.example/VesselStack.git'):
            with self.assertRaisesRegex(RuntimeError,'pinned'):deploy.check_remote(Path('/x'),'https://github.com/jclima/VesselStack.git')
        with mock.patch.object(deploy,'git',return_value='https://github.com/jclima/VesselStack.git'):
            deploy.check_remote(Path('/x'),'https://github.com/jclima/VesselStack.git')

    def test_unverified_source_is_refused_unless_home_lab_opt_in(self):
        with mock.patch.object(deploy,'commit_is_verified',return_value=False):
            with self.assertRaisesRegex(RuntimeError,'unverified'):deploy.require_verified(Path('/x'),'HEAD',False)
            deploy.require_verified(Path('/x'),'HEAD',True)
        with mock.patch.object(deploy,'commit_is_verified',return_value=True):
            deploy.require_verified(Path('/x'),'HEAD',False)

    def test_signed_tag_is_accepted_when_commit_unsigned(self):
        def git_ok(source,*args):return args==('verify-tag','v1.3.1')
        with mock.patch.object(deploy,'git',side_effect=lambda source,*a:'abc123' if a[0]=='rev-parse' else 'v1.3.0\nv1.3.1'),mock.patch.object(deploy,'git_ok',side_effect=git_ok):
            self.assertTrue(deploy.commit_is_verified(Path('/x'),'HEAD'))
        with mock.patch.object(deploy,'git',side_effect=lambda source,*a:'abc123' if a[0]=='rev-parse' else ''),mock.patch.object(deploy,'git_ok',return_value=False):
            self.assertFalse(deploy.commit_is_verified(Path('/x'),'HEAD'))

    def test_pull_verifies_before_merging_and_never_merges_unverified(self):
        calls=[]
        def git(source,*args):
            calls.append(args[0]);return 'abc123' if args[0]=='rev-parse' else ''
        with mock.patch.object(deploy,'git',side_effect=git),mock.patch.object(deploy,'commit_is_verified',side_effect=lambda *a:calls.append('verify') or False):
            with self.assertRaisesRegex(RuntimeError,'unverified'):deploy.pull_verified(Path('/x'),'',False)
        self.assertNotIn('merge',calls)
        calls.clear()
        with mock.patch.object(deploy,'git',side_effect=git),mock.patch.object(deploy,'commit_is_verified',side_effect=lambda *a:calls.append('verify') or True):
            deploy.pull_verified(Path('/x'),'',False)
        self.assertLess(calls.index('verify'),calls.index('merge'));self.assertNotIn('pull',calls)

    def test_pull_requires_pinned_remote_argument(self):
        argv=['deploy-chat.py','--target','/tmp/x','--pull']
        with mock.patch('sys.argv',argv),self.assertRaises(SystemExit):deploy.main()


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
