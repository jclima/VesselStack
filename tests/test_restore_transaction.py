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
# GNU "mv -T" is what production uses; BSD mv (macOS) lacks it, so emulate it there.
__mv() {
    if [ "$(uname)" = Darwin ] && [ "${1:-}" = -T ]; then
        shift; [ "${1:-}" = -- ] && shift
        if [ -d "$2" ] && [ ! -L "$2" ]; then rmdir "$2" || return 1; fi
        command mv -- "$1" "$2"
    else
        command mv "$@"
    fi
}
''' + helper_source() + "\n" + body
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(script); name = f.name
        os.chmod(name, 0o700)
        try:
            return subprocess.run(["bash", name, str(root)], capture_output=True, text=True)
        finally:
            os.unlink(name)

    def run_transaction(self, mode, quiesce=False, mv_hook="", post=""):
        td = tempfile.TemporaryDirectory(); base = Path(td.name)
        root = base / "root"; data = base / "data"; (root / "old").mkdir(parents=True); (data / "old").mkdir(parents=True)
        stage = base / "stage"; backup = stage / "vesselstack-backup"
        (backup / str(root).lstrip("/")).mkdir(parents=True); (backup / str(data).lstrip("/")).mkdir(parents=True)
        (backup / str(root).lstrip("/") / "new").write_text("new"); (backup / str(data).lstrip("/") / "new").write_text("new")
        journal = base / "journal"; journal.mkdir(); (journal / "manifest").write_text("")
        old = base / "old-controller"; new = base / "new-controller"; old.write_text("old"); new.write_text("new"); old.chmod(0o755); new.chmod(0o755)
        log = base / "calls.log"; active = base / "active"; log.write_text("")
        active.write_text("\n".join(["vesselstack-control-panel.service", "vesselstack-alerts.timer", "vesselstack-alerts.service"]) + "\n" if quiesce else "")
        systemd = base / "etc/systemd/system"; systemd.mkdir(parents=True)
        for unit in ("vesselstack-control-panel.service", "vesselstack-alerts.timer", "vesselstack-alerts.service"):
            (systemd / unit).write_text("unit")
        body = transaction_source() + """
SYSTEMD_DIR="@SYSTEMD@"
restore_run_controller() { action=$3; if [ "$action" = stop ]; then if [ "$2" = "@NEW@" ]; then [ "@MODE@" != new_stop_fail ]; else [ "@MODE@" != old_stop_fail ]; fi; elif [ "$action" = start ]; then if [ "$2" = "@NEW@" ]; then [ "@MODE@" != new_start_fail ] && [ "@MODE@" != new_stop_fail ]; else true; fi; fi; }
restore_publish_externals() { return 0; }
restore_restore_externals() { return 0; }
systemctl() {
    echo "systemctl $*" >> "@LOG@"
    local u
    case "$1" in
        is-active) u=${@: -1}; grep -qx "$u" "@ACTIVE@" ;;
        stop) for u in "${@:2}"; do { grep -vx "$u" "@ACTIVE@" || true; } > "@ACTIVE@.n"; command mv "@ACTIVE@.n" "@ACTIVE@"; done ;;
        start) for u in "${@:2}"; do echo "$u" >> "@ACTIVE@"; done ;;
        *) return 0 ;;
    esac
}
mv() {
    echo "MV $*" >> "@LOG@"
    if [ "$*" = "-T -- @BACKUPROOT@ @ROOT@" ] && [ "@MODE@" = second_mv_fail ]; then return 1; fi
    @HOOK@
    __mv "$@"
}
restore_commit_transaction "@STAGE@" "@JOURNAL@" "@ROOT@" "@DATA@" S "@OLD@" "@NEW@" 1 && rc=0 || rc=$?
@POST@
exit "$rc"
"""
        for key, value in {"@SYSTEMD@": systemd, "@NEW@": new, "@OLD@": old, "@MODE@": mode, "@LOG@": log, "@ACTIVE@": active,
                           "@BACKUPROOT@": backup / str(root).lstrip("/"), "@ROOT@": root, "@DATA@": data, "@STAGE@": stage,
                           "@JOURNAL@": journal, "@HOOK@": mv_hook.replace("@ACTIVE@", str(active)).replace("@DATA@", str(data)).replace("@BACKUPDATA@", str(backup / str(data).lstrip("/"))),
                           "@POST@": post.replace("@STAGE@", str(stage)).replace("@JOURNAL@", str(journal))}.items():
            body = body.replace(key, str(value))
        result = self.run_harness(body, base)
        self.base = base; self.journal = journal; self.stage = stage; self.log = log
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

    def test_empty_directory_at_destination_is_not_nested(self):
        hook = 'if [ "$*" = "-T -- @BACKUPDATA@ @DATA@" ]; then mkdir "@DATA@"; fi'
        td, result, root, data = self.run_transaction("success", mv_hook=hook); self.addCleanup(td.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((data / "new").read_text(), "new")
        self.assertFalse((data / "data").exists(), "published tree was nested inside the destination")

    def test_non_empty_directory_at_destination_fails_closed(self):
        hook = 'if [ "$*" = "-T -- @BACKUPDATA@ @DATA@" ]; then mkdir "@DATA@"; echo x > "@DATA@/blocker"; fi'
        td, result, root, data = self.run_transaction("success", mv_hook=hook, post='restore_finish_failed_commit "@STAGE@" "@JOURNAL@"')
        self.addCleanup(td.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((data / "data").exists(), "published tree was nested inside the destination")
        self.assertIn("RECOVERY ERROR", result.stderr)

    def test_failed_rollback_keeps_stage_and_journal_and_names_them(self):
        hook = 'if [ "$*" = "-T -- @BACKUPDATA@ @DATA@" ]; then mkdir "@DATA@"; echo x > "@DATA@/blocker"; fi'
        td, result, root, data = self.run_transaction("success", mv_hook=hook, post='restore_finish_failed_commit "@STAGE@" "@JOURNAL@"')
        self.addCleanup(td.cleanup)
        self.assertTrue(self.stage.exists()); self.assertTrue(self.journal.exists())
        self.assertIn(f"stage:   {self.stage}", result.stderr); self.assertIn(f"journal: {self.journal}", result.stderr)
        self.assertIn("including secrets", result.stderr)

    def test_successful_rollback_removes_stage(self):
        td, result, root, data = self.run_transaction("new_start_fail", post='restore_finish_failed_commit "@STAGE@" "@JOURNAL@"')
        self.addCleanup(td.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((root / "old").exists()); self.assertTrue((data / "old").exists())
        self.assertFalse(self.stage.exists(), result.stderr); self.assertIn("staged copy of the backup was removed", result.stderr)

    def test_old_controller_stop_failure_removes_stage_without_touching_trees(self):
        td, result, root, data = self.run_transaction("old_stop_fail", post='restore_finish_failed_commit "@STAGE@" "@JOURNAL@"')
        self.addCleanup(td.cleanup)
        self.assertNotEqual(result.returncode, 0); self.assertTrue((root / "old").exists()); self.assertFalse(self.stage.exists())

    def test_writers_are_stopped_before_the_first_rename_and_resumed_on_success(self):
        td, result, root, data = self.run_transaction("success", quiesce=True); self.addCleanup(td.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text().splitlines()
        first_mv = next(i for i, line in enumerate(calls) if line.startswith("MV "))
        for unit in ("vesselstack-alerts.timer", "vesselstack-alerts.service", "vesselstack-control-panel.service"):
            self.assertIn(f"systemctl stop {unit}", calls[:first_mv])
            self.assertIn(f"systemctl start {unit}", calls[first_mv:])
        self.assertNotIn("systemctl stop vesselstack-homepage-refresh.service", calls)
        self.assertEqual((self.journal / "quiesced-units").read_text().split(),
                         ["vesselstack-alerts.timer", "vesselstack-alerts.service", "vesselstack-control-panel.service"])

    def test_unit_already_active_after_the_installer_is_not_started_again(self):
        # The restored installer (stubbed here by the hook) already brought the panel back.
        hook = 'if [ "$*" = "-T -- @BACKUPDATA@ @DATA@" ]; then echo vesselstack-control-panel.service >> "@ACTIVE@"; fi'
        td, result, root, data = self.run_transaction("success", quiesce=True, mv_hook=hook); self.addCleanup(td.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text().splitlines()
        self.assertNotIn("systemctl start vesselstack-control-panel.service", calls)
        self.assertIn("systemctl start vesselstack-alerts.timer", calls)

    def test_rollback_restarts_exactly_the_stopped_units(self):
        td, result, root, data = self.run_transaction("new_start_fail", quiesce=True); self.addCleanup(td.cleanup)
        self.assertNotEqual(result.returncode, 0)
        calls = self.log.read_text().splitlines()
        started = [line for line in calls if line.startswith("systemctl start ")]
        self.assertEqual(sorted(started), sorted(f"systemctl start {u}" for u in
                         ("vesselstack-alerts.timer", "vesselstack-alerts.service", "vesselstack-control-panel.service")))
        self.assertTrue((root / "old").exists())

    def test_skip_installer_notice_prints_the_install_command(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); root = base / "app"; (root / "installer").mkdir(parents=True); (root / "installer/install.sh").write_text("#!/bin/sh\n")
            body = transaction_source() + f'\nrestore_skip_installer_notice "{root}"\n'
            result = self.run_harness(body, base)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"sudo {root}/installer/install.sh --config {root}/config/vesselstack.env --start\n", result.stderr)
            self.assertIn("Python environments for Boat Chat and the displays", result.stderr)
            (root / "installer/install.sh").unlink()
            result = self.run_harness(body, base)
            self.assertNotIn("sudo ", result.stderr); self.assertIn("rerun the VesselStack installer", result.stderr)

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
