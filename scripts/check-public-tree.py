#!/usr/bin/env python3
"""Reject runtime artifacts and recognizable secrets; never print matched values."""
import argparse
import ipaddress
import re
import subprocess
from pathlib import Path

BLOCKED_PARTS = {'.git-local', '.local', '.venv', 'memory', 'data', 'backups',
                 'pre-change-backups', 'reviews', '.claude', '.codex', '.agents', '.tools'}
BLOCKED_NAMES = {'.mcp.json', 'boat_facts.json', 'boat-chat.env', 'boatstack.env', 'token.pass'}
SECRET_PATTERNS = [
    re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b'),
    re.compile(rb'\bAKIA[A-Z0-9]{16}\b'),
    re.compile(rb'\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{30,}\b'),
    re.compile(rb'https?://[^\s/:]+:[^\s/@]+@'),
]


def findings(name, data):
    path = Path(name)
    if set(path.parts) & BLOCKED_PARTS or path.name in BLOCKED_NAMES:
        return ['runtime/private artifact']
    if path.suffix in {'.sqlite', '.sqlite3', '.db', '.jsonl', '.log', '.secret', '.tar', '.gz', '.zip'} or (path.name.endswith('.env') and not path.name.endswith('example.env')):
        return ['runtime/private artifact']
    problems = []
    if any(pattern.search(data) for pattern in SECRET_PATTERNS):problems.append('credential pattern')
    for value in re.findall(rb'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])', data):
        try:
            ip = ipaddress.ip_address(value.decode())
            if any(ip in network for network in PRIVATE_NETWORKS):problems.append('private network literal');break
        except ValueError:pass
    if re.search(rb'(?i)\b(?:[a-f0-9]{2}:){5}[a-f0-9]{2}\b',data):
        if any(value.lower()!=b'00:00:00:00:00:00' for value in re.findall(rb'(?i)\b(?:[a-f0-9]{2}:){5}[a-f0-9]{2}\b',data)):
            problems.append('hardware address')
    return problems


PRIVATE_NETWORKS = [ipaddress.ip_network((address, prefix)) for address, prefix in (
    (0x0A000000, 8), (0xAC100000, 12), (0xC0A80000, 16))]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path)
    parser.add_argument('--staged',action='store_true')
    args=parser.parse_args()
    failures=[];count=0
    if args.directory:
        entries=((str(p.relative_to(args.directory)),p.read_bytes()) for p in args.directory.rglob('*')
                 if p.is_file() and '.git' not in p.relative_to(args.directory).parts and '__pycache__' not in p.parts)
    else:
        names=subprocess.check_output(['git','-c',f'safe.directory={Path.cwd()}','ls-files','-z']).decode().split('\0')
        def read(name):
            return subprocess.check_output(['git','-c',f'safe.directory={Path.cwd()}','show',':'+name]) if args.staged else Path(name).read_bytes()
        entries=((name,read(name)) for name in names if name)
    for name,data in entries:
        count+=1
        for reason in findings(name,data):failures.append((name,reason))
    for name,reason in failures:print(f'{name}: {reason}')
    if failures:raise SystemExit(f'Public-tree check failed: {len(failures)} finding(s), values withheld')
    print(f'Public-tree check passed: {count} files (heuristic scan; manual review still required)')


if __name__=='__main__':main()
