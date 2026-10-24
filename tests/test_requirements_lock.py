import hashlib
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / 'boat-chat' / 'requirements.txt'
LOCK = ROOT / 'boat-chat' / 'requirements.lock'


def lock_entries():
    text = LOCK.read_text().replace('\\\n', ' ')
    entries = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)((?: --hash=sha256:[0-9a-f]{64})+)', ' '.join(line.split()))
        assert match, 'malformed lock line: ' + line
        entries[match.group(1).lower()] = (match.group(2), re.findall(r'[0-9a-f]{64}', match.group(3)))
    return entries


class RequirementsLockTests(unittest.TestCase):
    def test_lock_matches_requirements_file(self):
        header = re.search(r'^# requirements-sha256: ([0-9a-f]{64})$', LOCK.read_text(), re.M)
        self.assertIsNotNone(header, 'lock lacks the requirements-sha256 header')
        self.assertEqual(header.group(1), hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest(),
                         'requirements.txt changed: run scripts/update-requirements-lock.sh')

    def test_every_requirement_is_pinned_with_hashes(self):
        entries = lock_entries()
        for line in REQUIREMENTS.read_text().splitlines():
            line = line.split('#')[0].strip()
            if not line:
                continue
            name, version = line.split('==')
            self.assertIn(name.lower().replace('_', '-'), entries)
            self.assertEqual(entries[name.lower().replace('_', '-')][0], version)
        for name, (_version, hashes) in entries.items():
            self.assertTrue(hashes, name)

    def test_lock_has_no_unhashed_or_unpinned_lines(self):
        for line in LOCK.read_text().replace('\\\n', ' ').splitlines():
            if line.strip() and not line.startswith('#'):
                self.assertIn('==', line)
                self.assertNotIn('git+', line)
                self.assertNotIn('http', line)


if __name__ == '__main__':
    unittest.main()
