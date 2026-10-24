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


def git(source, *args):
    command=['git','-C',str(source),*args]
    owner=source.stat().st_uid
    if os.geteuid()==0 and owner!=0:
        command=['runuser','-u',pwd.getpwuid(owner).pw_name,'--',*command]
    return subprocess.check_output(command,text=True).strip()


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


def apply(source,target,files,backup):
    backup.mkdir(parents=True,mode=0o700,exist_ok=False)
    records=[]
    for name in files:
        current=target/name
        if current.is_symlink() or current.parent.resolve()!=target.joinpath(name).parent.absolute():
            raise RuntimeError('Refusing symlink deployment directory: '+name)
        if not current.parent.is_dir():raise RuntimeError('Target is not an existing installation: '+name)
        before=current.stat() if current.exists() else current.parent.stat()
        record=dict(name=name,before=digest(current),after=digest(source/name),uid=before.st_uid,gid=before.st_gid,
                    mode=(before.st_mode & 0o777) if current.exists() else 0o644,existed=current.exists())
        if current.exists():
            saved=backup/'files'/name;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(current,saved)
        records.append(record)
    receipt=dict(target=str(target),source_commit=git(source,'rev-parse','HEAD'),files=records)
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


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--target',type=Path,required=True)
    parser.add_argument('--pull',action='store_true',help='Fast-forward clean source only; never Git-pull the live target')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--restart-chat',action='store_true')
    parser.add_argument('--service',choices=['boat-chat.service','vesselstack-chat.service'],default='vesselstack-chat.service')
    parser.add_argument('--health-url',default='http://127.0.0.1:8765/health')
    parser.add_argument('--rollback',type=Path)
    args=parser.parse_args();source=args.source.resolve();target=args.target.resolve()
    if source==target or target.is_relative_to(source):parser.error('Source and live target must be separate; target cannot be inside source')
    if args.apply and not args.restart_chat:parser.error('--apply requires --restart-chat for readiness verification')
    with (target/'.chat-update.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.rollback:
            if not args.apply:parser.error('Rollback requires --apply --restart-chat')
            receipt=json.loads((args.rollback/'receipt.json').read_text())
            if Path(receipt['target']).resolve()!=target:parser.error('Rollback target does not match receipt')
            rollback(args.rollback)
            subprocess.run(['systemctl','restart',args.service],check=True)
            if not ready(args.health_url):raise RuntimeError('Rollback restored files, but readiness failed; inspect service logs')
            print('Rollback complete');return
        if git(source,'status','--porcelain','--untracked-files=normal'):raise RuntimeError('Source checkout is dirty; commit or resolve changes first')
        if args.pull:
            git(source,'pull','--ff-only')
        subprocess.run(['python3',str(source/'scripts/check-public-tree.py')],cwd=source,check=True)
        if (source/'boat-chat/requirements.txt').read_bytes()!=(target/'boat-chat/requirements.txt').read_bytes():
            raise RuntimeError('Dependencies changed; review and provision them separately before deployment')
        for directory in ['tests']:
            env=dict(os.environ,PYTHONPATH=str(source/'boat-chat'))
            subprocess.run(['python3','-m','unittest','discover','-s',directory,'-p','test_*.py'],cwd=source,env=env,check=True)
        files=deploy_files(source)
        changed=[name for name in files if digest(source/name)!=digest(target/name)]
        for name in changed:print('UPDATE',name)
        print(f'{len(changed)} changed code/static files; local configuration/data/infrastructure excluded')
        if not args.apply or not changed:return
        backup=target/'pre-change-backups'/('chat-update-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        apply(source,target,changed,backup)
        print('Rollback receipt:',backup/'receipt.json')
        try:
            subprocess.run(['systemctl','restart',args.service],check=True)
            if not ready(args.health_url):raise RuntimeError('Application readiness check failed')
        except Exception:
            rollback(backup)
            subprocess.run(['systemctl','restart',args.service],check=True)
            if not ready(args.health_url):raise RuntimeError('Deployment failed; rollback also failed readiness; inspect service logs')
            raise RuntimeError('Deployment failed; previous files restored and readiness verified')
        print('Deployment ready at source commit',git(source,'rev-parse','HEAD'))


if __name__=='__main__':main()
