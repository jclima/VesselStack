#!/usr/bin/env python3
"""Preview or deploy reviewed Git source to a separate live Boat Chat installation."""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path, PurePosixPath

# Applied to every git invocation: repository-local hooks and fsmonitor commands
# are attacker-controlled once an upstream commit has been pulled.
GIT_HARDENING = ['-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'core.pager=cat']


def owner_name(source):
    return pwd.getpwuid(source.stat().st_uid)


def unprivileged_command(source, command):
    """Wrap a command so it runs as the checkout owner, never as root.

    Pulled source is untrusted until reviewed: its tests and checker must not
    run with root privileges.  A root-owned checkout is refused when running as
    root because there is no unprivileged identity to drop to.
    """
    if os.geteuid() != 0:
        return list(command)
    entry = owner_name(source)
    if entry.pw_uid == 0:
        raise RuntimeError('Refusing to run source code as root: the source checkout is owned by root; use a checkout owned by an unprivileged user')
    return ['runuser', '-u', entry.pw_name, '--', 'env', 'HOME=' + entry.pw_dir, *command]


def git_command(source, *args):
    return unprivileged_command(source, ['git', *GIT_HARDENING, '-C', str(source), *args])


def git(source, *args):
    """Run git as the checkout owner (status, fetch, fast-forward)."""
    return subprocess.check_output(git_command(source, *args), text=True).strip()


def git_ok(source, *args):
    return subprocess.run(git_command(source, *args), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0


def run_unprivileged(source, command, extra_env=None, cwd=None):
    """Run a source-provided command as the checkout owner (default cwd=source)."""
    env_args = [f'{key}={value}' for key, value in (extra_env or {}).items()]
    full = unprivileged_command(source, ['env', 'PYTHONDONTWRITEBYTECODE=1', *env_args, *command])
    subprocess.run(full, cwd=cwd or source, check=True)


def require_root_controlled(path):
    """When running as root, the trust anchor and every parent must be root-owned
    and not group/world writable; otherwise the checkout owner could swap keys."""
    path = Path(path)
    if not path.exists():
        raise RuntimeError('Trust anchor does not exist: ' + str(path))
    if os.geteuid() != 0:
        return
    current = path.resolve()
    for candidate in [current, *current.parents]:
        info = candidate.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('Trust anchor must be root-owned and not group/world-writable along its whole path: ' + str(candidate))


def make_trust(gnupg_home=None, allowed_signers=None):
    """Fixed signature-verification configuration.

    Command-line `-c` values beat repository-local config, so a pulled commit
    cannot redirect gpg.program, the signer list or hooks; the system and global
    git configs are ignored too.  The keyring / allowed-signers file is meant to
    be root-controlled (see update.env.example).
    """
    config = [*GIT_HARDENING, '-c', 'gpg.program=gpg']
    env = {'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0'}
    if allowed_signers:
        require_root_controlled(allowed_signers)
        config += ['-c', 'gpg.format=ssh', '-c', 'gpg.ssh.program=ssh-keygen',
                   '-c', 'gpg.ssh.allowedSignersFile=' + str(Path(allowed_signers).resolve())]
    else:
        config += ['-c', 'gpg.format=openpgp']
    if gnupg_home:
        require_root_controlled(gnupg_home)
        env['GNUPGHOME'] = str(Path(gnupg_home).resolve())
    return {'config': config, 'env': env}


def trusted_command(source, trust, *args):
    # Runs in this process (root when invoked via sudo) against a checkout owned
    # by someone else, so the directory is marked safe on the command line only.
    return ['git', *trust['config'], '-c', 'safe.directory=' + str(source), '-C', str(source), *args]


def tgit(source, trust, *args):
    return subprocess.check_output(trusted_command(source, trust, *args), env={**os.environ, **trust['env']}, text=True).strip()


def tgit_ok(source, trust, *args):
    return subprocess.run(trusted_command(source, trust, *args), env={**os.environ, **trust['env']},
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0


def resolve_commit(source, rev, trust):
    return tgit(source, trust, 'rev-parse', '--verify', rev + '^{commit}')


def check_remote(source, expected):
    """Refuse to fetch unless origin is exactly the pinned update URL."""
    actual = git(source, 'remote', 'get-url', 'origin')
    if actual != expected:
        raise RuntimeError('Source origin does not match the pinned update remote; refusing to fetch (set EXPECTED_REMOTE_URL in the update configuration only after reviewing the change)')


def commit_is_verified(source, sha, trust):
    """True when the commit has a valid signature or carries a validly signed tag."""
    if tgit_ok(source, trust, 'verify-commit', sha):
        return True
    for tag in tgit(source, trust, 'tag', '--points-at', sha).splitlines():
        if tag and tgit_ok(source, trust, 'verify-tag', tag) and resolve_commit(source, tag, trust) == sha:
            return True
    return False


def require_verified(source, rev, allow_unsigned, trust):
    """Resolve `rev` to a commit id, verify that id, and return it."""
    sha = resolve_commit(source, rev, trust)
    if commit_is_verified(source, sha, trust):
        return sha
    if allow_unsigned:
        print('WARNING: '+rev+' is not signature-verified; continuing only because VESSELSTACK_UPDATE_ALLOW_UNSIGNED=true (home-lab only)')
        return sha
    raise RuntimeError('Refusing unverified source: '+rev+' has no valid commit or tag signature. Import the maintainer signing key into the root-controlled keyring (VERIFY_GNUPGHOME) or allowed-signers file (VERIFY_ALLOWED_SIGNERS), or (home lab only) set VESSELSTACK_UPDATE_ALLOW_UNSIGNED=true')


def pull_verified(source, expected_remote, allow_unsigned, trust):
    """Fetch, verify the upstream commit, and only then fast-forward the checkout."""
    check_remote(source, expected_remote)
    git(source, 'fetch', '--tags', 'origin')
    upstream = require_verified(source, '@{upstream}', allow_unsigned, trust)
    git(source, 'merge', '--ff-only', upstream)
    if resolve_commit(source, 'HEAD', trust) != upstream:
        raise RuntimeError('Checkout HEAD is not the verified upstream commit after fast-forward')
    return upstream


def extract_tar_stream(stream, destination):
    """Extract regular files/directories only; refuse links, devices and traversal."""
    destination = Path(destination)
    with tarfile.open(fileobj=stream, mode='r|') as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts:
                raise RuntimeError('Unsafe path in source archive: ' + member.name)
            target = destination.joinpath(*name.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755 if member.mode & 0o111 else 0o644)
                with os.fdopen(fd, 'wb') as handle:
                    shutil.copyfileobj(archive.extractfile(member), handle)
                os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
            else:
                raise RuntimeError('Source archive may contain only regular files and directories: ' + member.name)


@contextlib.contextmanager
def exported_tree(source, sha, trust):
    """Export the VERIFIED COMMIT OBJECT (never the working tree) to a temp tree.

    The tree is created by this process (root when run via sudo) and is not
    writable by the unprivileged user that runs the source's tests, so tests
    cannot alter the bytes that are later deployed.
    """
    base = tempfile.mkdtemp(prefix='vesselstack-deploy-')
    try:
        tree = Path(base) / 'tree'
        tree.mkdir()
        os.chmod(base, 0o755)
        with subprocess.Popen(trusted_command(source, trust, 'archive', '--format=tar', sha),
                              stdout=subprocess.PIPE, env={**os.environ, **trust['env']}) as proc:
            extract_tar_stream(proc.stdout, tree)
            proc.stdout.read()
            if proc.wait() != 0:
                raise RuntimeError('git archive failed for commit ' + sha)
        os.chmod(tree, 0o755)
        yield tree
    finally:
        shutil.rmtree(base, ignore_errors=True)


def tree_manifest(tree):
    result = hashlib.sha256()
    for directory, names, files in os.walk(tree):
        names.sort()
        for name in sorted(files):
            path = Path(directory) / name
            result.update(str(path.relative_to(tree)).encode() + b'\0')
            result.update(b'link' if path.is_symlink() else hashlib.sha256(path.read_bytes()).digest())
    return result.hexdigest()


def assert_unchanged(source, sha, trust, tree, manifest):
    """Abort if the checkout moved, became dirty, or the exported tree changed."""
    if resolve_commit(source, 'HEAD', trust) != sha:
        raise RuntimeError('Source HEAD changed during the update; aborting')
    if git(source, 'status', '--porcelain', '--untracked-files=normal'):
        raise RuntimeError('Source checkout became dirty during the update; aborting')
    if tree_manifest(tree) != manifest:
        raise RuntimeError('Exported source tree changed during the update (tests must not modify it); aborting')


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def deploy_files(tree):
    tree=Path(tree)
    # Explicit code/static allowlist: no facts, docs, env, MCP, DB, service or infrastructure files.
    candidates=[]
    for pattern,suffixes in (('boat-chat/*',{'.py'}),('boat-chat/static/*',{'.html','.js','.css','.svg'})):
        candidates+=[p for p in sorted(tree.glob(pattern)) if p.suffix in suffixes]
    allowed=[str(p.relative_to(tree)) for p in candidates]
    if not {'boat-chat/app.py','boat-chat/cruise_efficiency.py'}<=set(allowed):
        raise RuntimeError('Source checkout is missing required application files')
    for name in allowed:
        p=tree/name
        if p.is_symlink() or not p.is_file():raise RuntimeError('Source must contain regular tracked files: '+name)
    return sorted(allowed,key=lambda name:(name=='boat-chat/app.py',name))


def atomic_write(target,data,uid,gid,mode):
    if target.is_symlink():raise RuntimeError('Refusing symlink target: '+str(target))
    # Existing target directories are required; never redirect to an arbitrary tree.
    fd,name=tempfile.mkstemp(prefix='.'+target.name+'.',dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as handle:
            handle.write(data);handle.flush();os.fsync(handle.fileno())
            if os.geteuid()==0:os.fchown(handle.fileno(),uid,gid)
            os.fchmod(handle.fileno(),mode)
        os.replace(name,target)
    finally:
        if os.path.exists(name):os.unlink(name)


def apply(source,target,files,backup,unit_plan=None,commit='unknown'):
    target=target.resolve()  # canonical path (e.g. /var -> /private/var on macOS) so the symlink guard compares like with like
    backup.mkdir(parents=True,mode=0o700,exist_ok=False)
    records=[]
    for name in files:
        current=target/name
        if current.is_symlink() or current.parent.resolve()!=target.joinpath(name).parent:
            raise RuntimeError('Refusing symlink deployment directory: '+name)
        if not current.parent.is_dir():raise RuntimeError('Target is not an existing installation: '+name)
        before=current.stat() if current.exists() else current.parent.stat()
        record=dict(name=name,before=digest(current),after=digest(source/name),uid=before.st_uid,gid=before.st_gid,
                    mode=(before.st_mode & 0o777) if current.exists() else 0o644,existed=current.exists())
        if current.exists():
            saved=backup/'files'/name;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(current,saved)
        records.append(record)
    receipt=dict(target=str(target),source_commit=commit,files=records)
    if unit_plan is not None:
        receipt['restart_units'] = unit_plan
    (backup/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    try:
        for record in records:
            current=target/record['name']
            if digest(current)!=record['before']:raise RuntimeError('Target changed during deployment: '+record['name'])
            atomic_write(current,(source/record['name']).read_bytes(),record['uid'],record['gid'],record['mode'])
    except Exception:
        rollback(backup,partial=True)
        raise
    return backup


def rollback(backup,partial=False):
    receipt=json.loads((backup/'receipt.json').read_text());target=Path(receipt['target']).resolve()
    # Validate the complete receipt before restoring anything.
    for record in receipt['files']:
        name=Path(record['name'])
        if name.is_absolute() or '..' in name.parts or name.parts[0]!='boat-chat':raise RuntimeError('Invalid rollback receipt')
        current=target/name
        allowed={record['after'],record['before']} if partial else {record['after']}
        if digest(current) not in allowed:raise RuntimeError('Refusing to overwrite later edits: '+str(name))
        if record['existed'] and digest(backup/'files'/name)!=record['before']:raise RuntimeError('Backup checksum mismatch')
    for record in receipt['files']:
        current=target/record['name']
        if record['existed']:atomic_write(current,(backup/'files'/record['name']).read_bytes(),record['uid'],record['gid'],record['mode'])
        elif current.exists():current.unlink()


def ready(url):
    for attempt in range(20):
        try:
            with urllib.request.urlopen(url,timeout=2) as response:data=json.load(response)
            if data.get('ok') is True and data.get('ready') is True:return True
        except (OSError,ValueError):pass
        time.sleep(.5)
    return False


def restart_units(units):
    for unit in units:
        subprocess.run(['systemctl', 'restart', unit], check=True)


def update_units(args, changed):
    units = [args.service]
    if args.telegram_service and 'boat-chat/telegram_bot.py' in changed:
        state = subprocess.run(['systemctl', 'is-active', '--quiet', args.telegram_service], check=False)
        if state.returncode == 0:
            units.append(args.telegram_service)
    return units


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--target',type=Path,required=True)
    parser.add_argument('--pull',action='store_true',help='Fetch, verify and fast-forward clean source only; never Git-pull the live target')
    parser.add_argument('--expected-remote',default=os.environ.get('VESSELSTACK_UPDATE_REMOTE_URL'),help='Pinned origin URL; required with --pull')
    parser.add_argument('--allow-unsigned',action='store_true',default=os.environ.get('VESSELSTACK_UPDATE_ALLOW_UNSIGNED','false').strip().lower()=='true',help='Home-lab only: accept source without a verified commit/tag signature')
    parser.add_argument('--gnupg-home',type=Path,default=os.environ.get('VESSELSTACK_UPDATE_GNUPGHOME') or None,help='Root-controlled GNUPGHOME holding the maintainer public key(s) used for signature verification')
    parser.add_argument('--allowed-signers',type=Path,default=os.environ.get('VESSELSTACK_UPDATE_ALLOWED_SIGNERS') or None,help='Root-controlled SSH allowed-signers file (switches verification to SSH signatures)')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--restart-chat',action='store_true')
    parser.add_argument('--service',choices=['boat-chat.service','vesselstack-chat.service'],default='vesselstack-chat.service')
    parser.add_argument('--telegram-service', choices=['boat-chat-telegram.service', 'vesselstack-chat-telegram.service'])
    parser.add_argument('--health-url',default='http://127.0.0.1:8765/health')
    parser.add_argument('--rollback',type=Path)
    args=parser.parse_args();source=args.source.resolve();target=args.target.resolve()
    if source==target or target.is_relative_to(source):parser.error('Source and live target must be separate; target cannot be inside source')
    if args.apply and not args.restart_chat:parser.error('--apply requires --restart-chat for readiness verification')
    if args.pull and not args.expected_remote:parser.error('--pull requires --expected-remote (or VESSELSTACK_UPDATE_REMOTE_URL)')
    with (target/'.chat-update.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.rollback:
            if not args.apply:parser.error('Rollback requires --apply --restart-chat')
            receipt=json.loads((args.rollback/'receipt.json').read_text())
            if Path(receipt['target']).resolve()!=target:parser.error('Rollback target does not match receipt')
            units = receipt.get('restart_units', [args.service])
            allowed = {args.service, args.telegram_service} - {None}
            if not units or units[0] != args.service or not set(units) <= allowed:
                raise RuntimeError('Rollback service list does not match local update configuration')
            rollback(args.rollback)
            restart_units(units)
            if not ready(args.health_url):raise RuntimeError('Rollback restored files, but readiness failed; inspect service logs')
            print('Rollback complete');return
        trust=make_trust(args.gnupg_home,args.allowed_signers)
        if git(source,'status','--porcelain','--untracked-files=normal'):raise RuntimeError('Source checkout is dirty; commit or resolve changes first')
        if args.expected_remote:check_remote(source,args.expected_remote)
        if args.pull:sha=pull_verified(source,args.expected_remote,args.allow_unsigned,trust)
        else:sha=require_verified(source,'HEAD',args.allow_unsigned,trust)
        # Everything below uses the VERIFIED COMMIT OBJECT exported to a private
        # tree, never the working tree: checker, tests and deployed bytes are the same.
        with exported_tree(source,sha,trust) as tree:
            manifest=tree_manifest(tree)
            # Source code is untrusted until verified: checker and tests never run as root.
            run_unprivileged(source,['python3',str(tree/'scripts/check-public-tree.py'),'--directory',str(tree)],cwd=tree)
            if (tree/'boat-chat/requirements.txt').read_bytes()!=(target/'boat-chat/requirements.txt').read_bytes():
                raise RuntimeError('Dependencies changed; review and provision them separately before deployment')
            run_unprivileged(source,['python3','-m','unittest','discover','-s','tests','-p','test_*.py'],{'PYTHONPATH':str(tree/'boat-chat')},cwd=tree)
            assert_unchanged(source,sha,trust,tree,manifest)
            files=deploy_files(tree)
            changed=[name for name in files if digest(tree/name)!=digest(target/name)]
            for name in changed:print('UPDATE',name)
            print(f'{len(changed)} changed code/static files; local configuration/data/infrastructure excluded')
            if not args.apply or not changed:return
            backup=target/'pre-change-backups'/('chat-update-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
            units = update_units(args, changed)
            assert_unchanged(source,sha,trust,tree,manifest)
            apply(tree,target,changed,backup,unit_plan=units,commit=sha)
            print('Rollback receipt:',backup/'receipt.json')
            try:
                restart_units(units)
                if not ready(args.health_url):raise RuntimeError('Application readiness check failed')
            except Exception:
                rollback(backup)
                restart_units(units)
                if not ready(args.health_url):raise RuntimeError('Deployment failed; rollback also failed readiness; inspect service logs')
                raise RuntimeError('Deployment failed; previous files restored and readiness verified')
            print('Deployment ready at source commit',sha)


if __name__=='__main__':main()
