import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tarfile
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
        # Files that exist in the exported tree but must never be deployed.
        (self.source/'boat-chat/boat_facts.json').write_text('facts\n')
        (self.source/'.mcp.json').write_text('credentials\n')
        (self.source/'BOAT_AGENT.md').write_text('agent\n')
        self.backup=self.root/'backup'
        self.names=['boat-chat/cruise_efficiency.py','boat-chat/app.py']

    def tearDown(self):self.temp.cleanup()

    def git(self,source,*args):
        return ''

    def apply(self):
        return deploy.apply(self.source,self.target,self.names,self.backup,commit='fixture-commit')

    def test_allowlist_excludes_configuration_and_infrastructure(self):
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
        with self.main_mocks(argv,ready=[False,True]):
            with self.assertRaisesRegex(RuntimeError,'previous files restored'):deploy.main()
        for name in self.names:self.assertEqual((self.target/name).read_text(),'old\n')

    def main_mocks(self,argv,ready=(True,),unchanged=None):
        @contextlib.contextmanager
        def tree(source,sha,trust):yield self.source
        stack=contextlib.ExitStack()
        stack.enter_context(mock.patch('sys.argv',argv))
        stack.enter_context(mock.patch.object(deploy,'git',side_effect=self.git))
        stack.enter_context(mock.patch.object(deploy,'require_verified',return_value='fixture-commit'))
        stack.enter_context(mock.patch.object(deploy,'exported_tree',tree))
        stack.enter_context(mock.patch.object(deploy,'run_unprivileged'))
        stack.enter_context(mock.patch.object(deploy.subprocess,'run'))
        stack.enter_context(mock.patch.object(deploy,'ready',side_effect=list(ready)))
        self.unchanged=stack.enter_context(mock.patch.object(deploy,'assert_unchanged',side_effect=unchanged))
        return stack

    def test_state_is_rechecked_after_tests_and_again_before_apply(self):
        argv=['deploy-chat.py','--source',str(self.source.parent/'checkout'),'--target',str(self.target),'--apply','--restart-chat','--allow-unsigned']
        (self.source.parent/'checkout').mkdir()
        with self.main_mocks(argv):deploy.main()
        self.assertEqual(self.unchanged.call_count,2)
        self.assertEqual((self.target/'boat-chat/app.py').read_text(),'new\n')

    def test_changed_head_or_tree_aborts_before_any_file_is_written(self):
        argv=['deploy-chat.py','--source',str(self.source.parent/'checkout'),'--target',str(self.target),'--apply','--restart-chat','--allow-unsigned']
        (self.source.parent/'checkout').mkdir()
        with self.main_mocks(argv,unchanged=[None,RuntimeError('Source HEAD changed')]):
            with self.assertRaisesRegex(RuntimeError,'HEAD changed'):deploy.main()
        for name in self.names:self.assertEqual((self.target/name).read_text(),'old\n')
        self.assertFalse((self.target/'pre-change-backups').exists())

    def test_source_symlink_is_rejected(self):
        p=self.source/'boat-chat/app.py';p.unlink();p.symlink_to(self.source/'boat-chat/cruise_efficiency.py')
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
        trust=deploy.make_trust()
        with mock.patch.object(deploy,'resolve_commit',return_value='abc123'),mock.patch.object(deploy,'commit_is_verified',return_value=False):
            with self.assertRaisesRegex(RuntimeError,'unverified'):deploy.require_verified(Path('/x'),'HEAD',False,trust)
            self.assertEqual(deploy.require_verified(Path('/x'),'HEAD',True,trust),'abc123')
        with mock.patch.object(deploy,'resolve_commit',return_value='abc123'),mock.patch.object(deploy,'commit_is_verified',return_value=True):
            self.assertEqual(deploy.require_verified(Path('/x'),'HEAD',False,trust),'abc123')

    def test_signed_tag_is_accepted_when_commit_unsigned(self):
        trust=deploy.make_trust()
        def tgit_ok(source,trust,*args):return args==('verify-tag','v1.3.1')
        with mock.patch.object(deploy,'tgit',side_effect=lambda source,trust,*a:'abc123' if a[0]=='rev-parse' else 'v1.3.0\nv1.3.1'),mock.patch.object(deploy,'tgit_ok',side_effect=tgit_ok):
            self.assertTrue(deploy.commit_is_verified(Path('/x'),'abc123',trust))
        with mock.patch.object(deploy,'tgit',side_effect=lambda source,trust,*a:'abc123' if a[0]=='rev-parse' else ''),mock.patch.object(deploy,'tgit_ok',return_value=False):
            self.assertFalse(deploy.commit_is_verified(Path('/x'),'abc123',trust))

    def test_tag_that_no_longer_points_at_the_commit_is_rejected(self):
        trust=deploy.make_trust()
        def tgit(source,trust,*a):return 'othersha' if a[0]=='rev-parse' else 'v1'
        with mock.patch.object(deploy,'tgit',side_effect=tgit),mock.patch.object(deploy,'tgit_ok',side_effect=lambda s,t,*a:a[0]=='verify-tag'):
            self.assertFalse(deploy.commit_is_verified(Path('/x'),'abc123',trust))

    def test_verification_is_pinned_to_the_commit_id(self):
        calls=[]
        with mock.patch.object(deploy,'tgit_ok',side_effect=lambda s,t,*a:calls.append(a) or True):
            deploy.commit_is_verified(Path('/x'),'abc123',deploy.make_trust())
        self.assertEqual(calls,[('verify-commit','abc123')])

    def test_pull_verifies_before_merging_and_never_merges_unverified(self):
        calls=[];trust=deploy.make_trust()
        def git(source,*args):
            calls.append(args[0]);return ''
        with mock.patch.object(deploy,'git',side_effect=git),mock.patch.object(deploy,'resolve_commit',return_value='abc123'),mock.patch.object(deploy,'commit_is_verified',side_effect=lambda *a:calls.append('verify') or False):
            with self.assertRaisesRegex(RuntimeError,'unverified'):deploy.pull_verified(Path('/x'),'',False,trust)
        self.assertNotIn('merge',calls)
        calls.clear()
        with mock.patch.object(deploy,'git',side_effect=git),mock.patch.object(deploy,'resolve_commit',return_value='abc123'),mock.patch.object(deploy,'commit_is_verified',side_effect=lambda *a:calls.append('verify') or True):
            self.assertEqual(deploy.pull_verified(Path('/x'),'',False,trust),'abc123')
        self.assertLess(calls.index('verify'),calls.index('merge'));self.assertNotIn('pull',calls)

    def test_pull_fails_if_head_is_not_the_verified_commit_after_merge(self):
        shas=iter(['upstream','other'])
        with mock.patch.object(deploy,'git',return_value=''),mock.patch.object(deploy,'resolve_commit',side_effect=lambda *a:next(shas)),mock.patch.object(deploy,'commit_is_verified',return_value=True):
            with self.assertRaisesRegex(RuntimeError,'not the verified upstream'):deploy.pull_verified(Path('/x'),'',False,deploy.make_trust())

    def test_pull_requires_pinned_remote_argument(self):
        argv=['deploy-chat.py','--target','/tmp/x','--pull']
        with mock.patch('sys.argv',argv),self.assertRaises(SystemExit):deploy.main()


class FixedVerificationConfigTests(unittest.TestCase):
    def test_fixed_config_overrides_repo_local_and_global_settings(self):
        trust=deploy.make_trust()
        command=deploy.trusted_command(Path('/src'),trust,'verify-commit','abc')
        for setting in ['core.hooksPath=/dev/null','gpg.program=gpg','gpg.format=openpgp','core.fsmonitor=false']:
            self.assertIn(setting,command)
        self.assertLess(command.index('core.hooksPath=/dev/null'),command.index('-C'))
        self.assertIn('safe.directory=/src',command)
        self.assertEqual(trust['env']['GIT_CONFIG_GLOBAL'],os.devnull);self.assertEqual(trust['env']['GIT_CONFIG_NOSYSTEM'],'1')

    def test_gnupg_home_and_allowed_signers_are_honored(self):
        with tempfile.TemporaryDirectory() as directory:
            signers=Path(directory)/'allowed_signers';signers.write_text('x\n')
            with mock.patch.object(deploy.os,'geteuid',return_value=1000):  # ownership policy is tested separately
                trust=deploy.make_trust(gnupg_home=directory,allowed_signers=signers)
            self.assertEqual(trust['env']['GNUPGHOME'],str(Path(directory).resolve()))
            self.assertIn('gpg.format=ssh',trust['config'])
            self.assertIn('gpg.ssh.allowedSignersFile='+str(signers.resolve()),trust['config'])
        with self.assertRaisesRegex(RuntimeError,'does not exist'):deploy.make_trust(gnupg_home='/nonexistent/vesselstack-gnupg')

    def test_root_requires_root_controlled_trust_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(deploy.os,'geteuid',return_value=0):
                with self.assertRaisesRegex(RuntimeError,'root-owned'):deploy.require_root_controlled(directory)

    def test_non_root_does_not_enforce_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(deploy.os,'geteuid',return_value=1000):deploy.require_root_controlled(directory)


def commit_repo(path):
    env=dict(os.environ,GIT_AUTHOR_NAME='t',GIT_AUTHOR_EMAIL='t@example.invalid',GIT_COMMITTER_NAME='t',GIT_COMMITTER_EMAIL='t@example.invalid',GIT_CONFIG_GLOBAL=os.devnull,GIT_CONFIG_NOSYSTEM='1')
    def run(*args):subprocess.run(['git','-C',str(path),*args],check=True,env=env,stdout=subprocess.DEVNULL)
    run('init','-q');(path/'boat-chat').mkdir();(path/'boat-chat/app.py').write_text('committed\n')
    run('add','-A');run('commit','-q','-m','c')
    return subprocess.check_output(['git','-C',str(path),'rev-parse','HEAD'],text=True,env=env).strip()


class ExportedTreeTests(unittest.TestCase):
    def test_tree_comes_from_the_commit_object_not_the_working_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            repo=Path(directory)/'repo';repo.mkdir();sha=commit_repo(repo)
            (repo/'boat-chat/app.py').write_text('tampered working tree\n');(repo/'untracked.py').write_text('x')
            with deploy.exported_tree(repo,sha,deploy.make_trust()) as tree:
                self.assertEqual((tree/'boat-chat/app.py').read_text(),'committed\n')
                self.assertFalse((tree/'untracked.py').exists())

    def test_archive_extraction_refuses_links_and_traversal(self):
        def archive(member,data=b''):
            buffer=io.BytesIO()
            with tarfile.open(fileobj=buffer,mode='w') as bundle:bundle.addfile(member,io.BytesIO(data) if data else None)
            return io.BytesIO(buffer.getvalue())
        link=tarfile.TarInfo('boat-chat/app.py');link.type=tarfile.SYMTYPE;link.linkname='/etc/passwd'
        escape=tarfile.TarInfo('../escape');escape.size=1
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError,'regular files'):deploy.extract_tar_stream(archive(link),directory)
            with self.assertRaisesRegex(RuntimeError,'Unsafe path'):deploy.extract_tar_stream(archive(escape,b'x'),directory)

    def test_manifest_detects_modification_during_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            tree=Path(directory);(tree/'a.py').write_text('one')
            before=deploy.tree_manifest(tree);(tree/'a.py').write_text('two')
            with mock.patch.object(deploy,'resolve_commit',return_value='sha'),mock.patch.object(deploy,'git',return_value=''):
                with self.assertRaisesRegex(RuntimeError,'tree changed'):deploy.assert_unchanged(Path('/x'),'sha',{},tree,before)
                deploy.assert_unchanged(Path('/x'),'sha',{},tree,deploy.tree_manifest(tree))

    def test_head_move_or_dirty_checkout_aborts(self):
        with tempfile.TemporaryDirectory() as directory:
            tree=Path(directory);manifest=deploy.tree_manifest(tree)
            with mock.patch.object(deploy,'resolve_commit',return_value='moved'),mock.patch.object(deploy,'git',return_value=''):
                with self.assertRaisesRegex(RuntimeError,'HEAD changed'):deploy.assert_unchanged(Path('/x'),'sha',{},tree,manifest)
            with mock.patch.object(deploy,'resolve_commit',return_value='sha'),mock.patch.object(deploy,'git',return_value=' M boat-chat/app.py'):
                with self.assertRaisesRegex(RuntimeError,'dirty'):deploy.assert_unchanged(Path('/x'),'sha',{},tree,manifest)


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
