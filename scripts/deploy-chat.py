#!/usr/bin/env python3
"""Preview or deploy reviewed Git source to a separate live Boat Chat installation."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


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
    return unprivileged_command(source, ['git', '-C', str(source), *args])


def git(source, *args):
    return subprocess.check_output(git_command(source, *args), text=True).strip()


def git_ok(source, *args):
    return subprocess.run(git_command(source, *args), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0


def run_unprivileged(source, command, extra_env=None):
    """Run a source-provided command as the checkout owner (cwd=source)."""
    env_args = [f'{key}={value}' for key, value in (extra_env or {}).items()]
    full = unprivileged_command(source, ['env', 'PYTHONDONTWRITEBYTECODE=1', *env_args, *command])
    subprocess.run(full, cwd=source, check=True)


def check_remote(source, expected):
    """Refuse to fetch unless origin is exactly the pinned update URL."""
    actual = git(source, 'remote', 'get-url', 'origin')
    if actual != expected:
        raise RuntimeError('Source origin does not match the pinned update remote; refusing to fetch (set EXPECTED_REMOTE_URL in the update configuration only after reviewing the change)')


def commit_is_verified(source, rev):
    """True when the commit has a valid signature or carries a validly signed tag."""
    sha = git(source, 'rev-parse', '--verify', rev + '^{commit}')
    if git_ok(source, 'verify-commit', sha):
        return True
    for tag in git(source, 'tag', '--points-at', sha).splitlines():
        if tag and git_ok(source, 'verify-tag', tag):
            return True
    return False


def require_verified(source, rev, allow_unsigned):
    if commit_is_verified(source, rev):
        return
    if allow_unsigned:
        print('WARNING: '+rev+' is not signature-verified; continuing only because VESSELSTACK_UPDATE_ALLOW_UNSIGNED=true (home-lab only)')
        return
    raise RuntimeError('Refusing unverified source: '+rev+' has no valid commit or tag signature. Import the maintainer signing key for the checkout owner, or (home lab only) set VESSELSTACK_UPDATE_ALLOW_UNSIGNED=true')


def pull_verified(source, expected_remote, allow_unsigned):
    """Fetch, verify the upstream commit, and only then fast-forward the checkout."""
    check_remote(source, expected_remote)
    git(source, 'fetch', '--tags', 'origin')
    upstream = git(source, 'rev-parse', '--verify', '@{upstream}^{commit}')
    require_verified(source, upstream, allow_unsigned)
    git(source, 'merge', '--ff-only', upstream)


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def deploy_files(source):
    tracked=git(source,'ls-files').splitlines()
    # Explicit code/static allowlist: no facts, docs, env, MCP, DB, service or infrastructure files.
    allowed=[]
    for name in tracked:
        p=Path(name)
        if len(p.parts)==2 and p.parts[0]=='boat-chat' and p.suffix=='.py':allowed.append(name)
        elif len(p.parts)==3 and p.parts[:2]==('boat-chat','static') and p.suffix in {'.html','.js','.css','.svg'}:allowed.append(name)
    if not {'boat-chat/app.py','boat-chat/cruise_efficiency.py'}<=set(allowed):
        raise RuntimeError('Source checkout is missing required application files')
    for name in allowed:
        p=source/name
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


def apply(source,target,files,backup,unit_plan=None):
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
    receipt=dict(target=str(target),source_commit=git(source,'rev-parse','HEAD'),files=records)
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
        if git(source,'status','--porcelain','--untracked-files=normal'):raise RuntimeError('Source checkout is dirty; commit or resolve changes first')
        if args.expected_remote:check_remote(source,args.expected_remote)
        if args.pull:pull_verified(source,args.expected_remote,args.allow_unsigned)
        else:require_verified(source,'HEAD',args.allow_unsigned)
        # Source code is untrusted until verified: checker and tests never run as root.
        run_unprivileged(source,['python3',str(source/'scripts/check-public-tree.py')])
        if (source/'boat-chat/requirements.txt').read_bytes()!=(target/'boat-chat/requirements.txt').read_bytes():
            raise RuntimeError('Dependencies changed; review and provision them separately before deployment')
        run_unprivileged(source,['python3','-m','unittest','discover','-s','tests','-p','test_*.py'],{'PYTHONPATH':str(source/'boat-chat')})
        files=deploy_files(source)
        changed=[name for name in files if digest(source/name)!=digest(target/name)]
        for name in changed:print('UPDATE',name)
        print(f'{len(changed)} changed code/static files; local configuration/data/infrastructure excluded')
        if not args.apply or not changed:return
        backup=target/'pre-change-backups'/('chat-update-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        units = update_units(args, changed)
        apply(source,target,changed,backup,unit_plan=units)
        print('Rollback receipt:',backup/'receipt.json')
        try:
            restart_units(units)
            if not ready(args.health_url):raise RuntimeError('Application readiness check failed')
        except Exception:
            rollback(backup)
            restart_units(units)
            if not ready(args.health_url):raise RuntimeError('Deployment failed; rollback also failed readiness; inspect service logs')
            raise RuntimeError('Deployment failed; previous files restored and readiness verified')
        print('Deployment ready at source commit',git(source,'rev-parse','HEAD'))


if __name__=='__main__':main()
