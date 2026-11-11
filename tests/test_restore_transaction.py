"""Real temp-root subprocess coverage for restore external transactions."""
from pathlib import Path
import os, subprocess, tempfile, unittest

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "scripts/vesselstackctl"

def helper_source():
    text = CONTROLLER.read_text()
    a = text.index("restore_external_candidates() {")
    b = text.index("restore_run_controller() {", a)
    return text[a:b]

def transaction_source():
    text = CONTROLLER.read_text()
    a = text.index("restore_commit_transaction() {")
    b = text.index("restore_stack() {", a)
    return text[a:b]

def identity_source():
    text = CONTROLLER.read_text()
    a = text.index("restore_config_identity() {")
    b = text.index("restore_commit_transaction() {", a)
    return text[a:b]

class RestoreTransactionTests(unittest.TestCase):
    def run_harness(self, body, root):
        script = '''#!/bin/bash
set -eu
SYSTEM_ROOT=$1
SYSTEMD_DIR="$SYSTEM_ROOT/etc/systemd/system"
SBIN_DIR="$SYSTEM_ROOT/usr/local/sbin"
LIBEXEC_DIR="$SYSTEM_ROOT/usr/local/libexec/vesselstack"
ROOT="$SYSTEM_ROOT/opt/vesselstack"
ENV_FILE="$ROOT/config/vesselstack.env"
''' + helper_source() + "\n" + body
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(script); name = f.name
        os.chmod(name, 0o700)
        try:
            return subprocess.run(["bash", name, str(root)], capture_output=True, text=True)
        finally:
            os.unlink(name)

    def run_transaction(self, mode):
        td = tempfile.TemporaryDirectory(); base = Path(td.name)
        root = base / "root"; data = base / "data"; (root / "old").mkdir(parents=True); (data / "old").mkdir(parents=True)
        stage = base / "stage"; backup = stage / "vesselstack-backup"
        (backup / str(root).lstrip("/")).mkdir(parents=True); (backup / str(data).lstrip("/")).mkdir(parents=True)
        (backup / str(root).lstrip("/") / "new").write_text("new"); (backup / str(data).lstrip("/") / "new").write_text("new")
        journal = base / "journal"; journal.mkdir(); (journal / "manifest").write_text("")
        old = base / "old-controller"; new = base / "new-controller"; old.write_text("old"); new.write_text("new"); old.chmod(0o755); new.chmod(0o755)
        body = transaction_source() + f'''\nrestore_run_controller() {{ action=$3; if [ "$action" = stop ]; then if [ "$2" = "{new}" ]; then [ "{mode}" != new_stop_fail ]; else [ "{mode}" != old_stop_fail ]; fi; elif [ "$action" = start ]; then if [ "$2" = "{new}" ]; then [ "{mode}" != new_start_fail ] && [ "{mode}" != new_stop_fail ]; else true; fi; fi; }}
restore_publish_externals() {{ return 0; }}
restore_restore_externals() {{ return 0; }}
systemctl() {{ return 0; }}
{("mv() { if [ \"$*\" = \"-- " + str(backup / str(root).lstrip("/")) + " " + str(root) + "\" ]; then return 1; fi; command mv \"$@\"; }" if mode == "second_mv_fail" else "")}
restore_commit_transaction "{stage}" "{journal}" "{root}" "{data}" S "{old}" "{new}" 1
'''
        result = self.run_harness(body, base)
        return td, result, root, data

    def test_transaction_success(self):
        td, result, root, data = self.run_transaction("success"); self.addCleanup(td.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((root / "new").read_text(), "new"); self.assertEqual((data / "new").read_text(), "new")

    def test_transaction_start_failure_recovers_old_pair(self):
        td, result, root, data = self.run_transaction("new_start_fail"); self.addCleanup(td.cleanup)
        self.assertNotEqual(result.returncode, 0); self.assertTrue((root / "old").exists()); self.assertTrue((data / "old").exists())

    def test_transaction_new_stop_failure_retains_new_pair(self):
        td, result, root, data = self.run_transaction("new_stop_fail"); self.addCleanup(td.cleanup)
        self.assertNotEqual(result.returncode, 0); self.assertTrue((root / "new").exists(), result.stderr); self.assertTrue((data / "new").exists(), result.stderr)

    def test_transaction_second_rename_failure_recovers_old_pair(self):
        td, result, root, data = self.run_transaction("second_mv_fail"); self.addCleanup(td.cleanup)
        self.assertNotEqual(result.returncode, 0); self.assertTrue((root / "old").exists(), result.stderr); self.assertTrue((data / "old").exists(), result.stderr)

    def test_exact_state_restore_and_bounded_publication(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); system = base / "system"
            (system / "etc/systemd/system/multi-user.target.wants").mkdir(parents=True)
            (system / "usr/local/sbin").mkdir(parents=True)
            (system / "usr/local/libexec/vesselstack").mkdir(parents=True)
            (system / "etc/vesselstack").mkdir(parents=True)
            ctl = system / "usr/local/sbin/vesselstackctl"; ctl.write_text("old"); ctl.chmod(0o640)
            unit = system / "etc/systemd/system/vesselstack-chat.service"; unit.write_text("old-unit")
            legacy = system / "etc/systemd/system/vesselstack-legacy.service"; legacy.write_text("old-legacy")
            (system / "etc/systemd/system/multi-user.target.wants/vesselstack-chat.service").symlink_to(unit)
            (system / "etc/other.conf").write_text("untouched")
            stage = base / "stage/vesselstack-backup" / str(system).lstrip("/"); stage.mkdir(parents=True)
            (stage / "usr/local/sbin").mkdir(parents=True)
            (stage / "etc/systemd/system/multi-user.target.wants").mkdir(parents=True)
            (stage / "usr/local/sbin/vesselstackctl").write_text("new")
            (stage / "usr/local/sbin/vesselstack-firewall").write_text("new-firewall")
            (stage / "etc/systemd/system/vesselstack-new.service").write_text("new-unit")
            (stage / "etc/systemd/system/vesselstack-legacy.service").write_text("new-legacy")
            (stage / "etc/systemd/system/multi-user.target.wants/vesselstack-new.service").symlink_to("../vesselstack-new.service")
            journal = base / "journal"
            body = f'''stage="{base / "stage"}"; journal="{journal}"
restore_snapshot_externals "$journal" "$stage"
restore_publish_externals "$journal" "$stage"
test "$(cat "$SBIN_DIR/vesselstackctl")" = new
test "$(cat "$SBIN_DIR/vesselstack-firewall")" = new-firewall
            test -L "$SYSTEMD_DIR/multi-user.target.wants/vesselstack-new.service"
test ! -e "$SBIN_DIR/vesselstack-update"
test "$(cat "$SYSTEM_ROOT/etc/other.conf")" = untouched
restore_restore_externals "$journal"
test "$(cat "$SBIN_DIR/vesselstackctl")" = old
test "$(cat "$SYSTEMD_DIR/vesselstack-legacy.service")" = old-legacy
            test "$(python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])' "$SBIN_DIR/vesselstackctl")" = 640
            test -L "$SYSTEMD_DIR/multi-user.target.wants/vesselstack-chat.service"
test ! -e "$SBIN_DIR/vesselstack-firewall"
test ! -e "$SYSTEMD_DIR/vesselstack-new.service"
'''
            result = self.run_harness(body, system)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_restore_config_identity_accepts_empty_values_and_inline_comments(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); system = base / "system"; root = system / "opt/vesselstack"; data = system / "opt/vesselstack-data"
            root.mkdir(parents=True); data.mkdir(parents=True)
            current = base / "current.env"; archived = base / "archived.env"
            current.write_text(f"VESSELSTACK_ROOT={root} # current root\nVESSELSTACK_DATA='{data}'\nOPTIONAL=\n")
            archived.write_text(f"export VESSELSTACK_ROOT='{root}' # archived root\nVESSELSTACK_DATA={data} # archived data\nOPTIONAL=''\n")
            result = self.run_harness(f'''{identity_source()}
VESSELSTACK_ROOT="{root}" VESSELSTACK_DATA="{data}" restore_config_identity "{current}" "{archived}"''', system)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_rejects_symlink_ancestor_before_changes(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); system = base / "system"; (system / "etc/systemd/system").mkdir(parents=True)
            outside = base / "outside"; outside.mkdir(); (system / "usr").symlink_to(outside, target_is_directory=True)
            stage = base / "stage/vesselstack-backup" / str(system).lstrip("/"); stage.mkdir(parents=True)
            result = self.run_harness(f'''stage="{base / "stage"}"; restore_snapshot_externals "{base / "journal"}" "$stage"''', system)
            self.assertNotEqual(result.returncode, 0)

    def test_controller_parses(self):
        result = subprocess.run(["bash", "-n", str(CONTROLLER)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

if __name__ == "__main__":
    unittest.main()
