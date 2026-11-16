"""Real temp-root subprocess coverage for restore external transactions."""
from pathlib import Path
import json, os, re, shutil, signal, subprocess, sys, tempfile, threading, time, unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "scripts/vesselstackctl"
JOURNAL_HELPER = ROOT / "scripts/lib/restore_journal.py"
BOOT_SCRIPT = ROOT / "scripts/lib/restore-boot"

def replace_with_foreign_dir(path):
    """Swap `path` for a different directory that can never share its inode.

    Deleting and re-creating the same path can hand the freed inode number back (it does on Linux), which would make
    the foreign directory look like the recorded original. Build the replacement as a sibling while the original still
    exists, then remove the original and rename the sibling into place."""
    path = Path(path)
    old_inode = path.stat().st_ino
    sibling = path.with_name(path.name + ".foreign-swap")
    sibling.mkdir()
    (sibling / "marker").write_text("foreign")
    shutil.rmtree(path)
    sibling.rename(path)
    assert path.stat().st_ino != old_inode, "replacement directory reused the original inode; the test would prove nothing"

def helper_source():
    text = CONTROLLER.read_text()
    a = text.index("restore_known_units() {")
    b = text.index("restore_run_controller() {", a)
    return text[a:b]

def transaction_source():
    # The journal/reconcile functions, restore_config_identity and restore_commit_transaction.
    text = CONTROLLER.read_text()
    a = text.index("restore_state_get() {")
    b = text.index("restore_create_journal() {", a)
    return text[a:b]

def identity_source():
    text = CONTROLLER.read_text()
    a = text.index("restore_config_identity() {")
    b = text.index("restore_commit_transaction() {", a)
    return text[a:b]

def create_journal_source():
    # restore_create_journal (the extension of the extraction range past transaction_source(), which stops before it).
    text = CONTROLLER.read_text()
    a = text.index("restore_create_journal() {")
    return text[a:text.index("restore_stack() {", a)]

BOOT_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
_BOOT_FILE = None

def setUpModule():
    # A fixed boot id for every journal write, so the tests give the same result on macOS and Linux.
    global _BOOT_FILE
    handle = tempfile.NamedTemporaryFile("w", suffix=".boot_id", delete=False)
    handle.write(BOOT_ID + "\n"); handle.close()
    _BOOT_FILE = handle.name
    os.environ["VESSELSTACK_BOOT_ID_FILE"] = _BOOT_FILE

def tearDownModule():
    os.environ.pop("VESSELSTACK_BOOT_ID_FILE", None)
    if _BOOT_FILE:
        os.unlink(_BOOT_FILE)

STAMP = "20260102T030405Z-1-1"
STAGE_NAME = ".vesselstack-restore.AbC123"


def inode(path):
    st = os.lstat(path)
    return f"{st.st_dev}:{st.st_ino}"

def make_journal(base, root, data, stage, phase="prepared", stamp=STAMP):
    """A complete journal at <base>/restore/current for trees `root`/`data` and a staged copy in `stage`.
    The staged trees must exist; the state records all four inodes while nothing has moved."""
    current = base / "restore" / "current"
    current.mkdir(parents=True, mode=0o700)
    shutil.copy(JOURNAL_HELPER, current / "restore_journal.py")
    for name in ("controller", "new-controller"):
        (current / name).write_text("#!/bin/bash\n"); (current / name).chmod(0o755)
    (current / "manifest").write_text(""); (current / "quiesced-units").write_text("")
    new_root = stage / "vesselstack-backup" / str(root).lstrip("/")
    new_data = stage / "vesselstack-backup" / str(data).lstrip("/")
    subprocess.run([sys.executable, "-I", str(current / "restore_journal.py"), "set", str(current),
                    "format=1", "id=abc123", f"stamp={stamp}", f"phase={phase}", f"root={root}", f"data={data}",
                    f"system_root={base}", f"stage={stage}", "archive=/backups/x.tar.gz", "skip_installer=0",
                    f"root_live={inode(root)}", f"data_live={inode(data)}", f"root_new={inode(new_root)}",
                    f"data_new={inode(new_data)}", "started=2026-01-02T03:04:05Z", "last_error="], check=True)
    return current

def phase_of(directory):
    for line in (Path(directory) / "state").read_text().splitlines():
        if line.startswith("phase="): return line[6:]

def state_value(directory, key):
    for line in (Path(directory) / "state").read_text().splitlines():
        if line.startswith(key + "="): return line[len(key) + 1:]

def controller_function_source(name, end_marker):
    text = CONTROLLER.read_text()
    a = text.index(name)
    return text[a:text.index(end_marker, a)]

def pending_source():
    return controller_function_source("restore_state_peek() {", "ensure_lifecycle_lock() {")


def manual_procedure_block(path, function, marker, end):
    """From stamp_re= through the end of the heredoc of FUNCTION, with only the way a value is read and the
    indentation of the code normalised (the heredoc body itself is compared exactly by the callers' text)."""
    text = Path(path).read_text()
    a = text.index(f"{function}() {{")
    start = text.index("stamp_re=", a)
    stop = text.index(end, text.index(marker, start)) + len(end)
    block = text[start:stop]
    body_start = block.index(marker + "\n") + len(marker) + 1
    code, body = block[:body_start], block[body_start:]
    word = marker[2:]
    code = code.replace(marker, "<<HEREDOC")
    body = body.replace("\n" + word + "\n", "\nHEREDOC\n")
    code = re.sub(r'restore_state_peek "\$dir" (\w+)', r"READ \1", code)
    code = re.sub(r"journal_peek (\w+)", r"READ \1", code)
    code = re.sub(r'python3 -I "\$dir/restore_journal\.py" get "\$dir" (\w+) 2>/dev/null \|\| true', r"READ \1", code)
    code = "\n".join(line.lstrip() for line in code.splitlines())
    return code, body


class RestoreTransactionTests(unittest.TestCase):
    def run_harness(self, body, root, real_rename=False):
        script = self.build_harness(body, real_rename)
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(script); name = f.name
        os.chmod(name, 0o700)
        try:
            return subprocess.run(["bash", name, str(root)], capture_output=True, text=True)
        finally:
            os.unlink(name)

    def build_harness(self, body, real_rename=False):
        script = '''#!/bin/bash
set -eu
SYSTEM_ROOT=$1
SYSTEMD_DIR="$SYSTEM_ROOT/etc/systemd/system"
SBIN_DIR="$SYSTEM_ROOT/usr/local/sbin"
LIBEXEC_DIR="$SYSTEM_ROOT/usr/local/libexec/vesselstack"
ROOT="$SYSTEM_ROOT/opt/vesselstack"
ENV_FILE="$ROOT/config/vesselstack.env"
# A running systemd (restore_unit_idle treats every unit as idle without it); a test can remove the folder again.
mkdir -p "$SYSTEM_ROOT/run/systemd/system"
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
        if not real_rename:
            # The tests hook the shell mv to log and to fail renames; production renames through the state
            # helper (restore_rename_tree, tested on its own). Route the harness through the hooked mv.
            script = script.replace("restore_rename_tree() {", "restore_rename_tree_real() {")
            script = script.replace('# GNU "mv -T" is what', 'restore_rename_tree() { mv -T -- "$2" "$3"; }\n# GNU "mv -T" is what', 1)
        return script

    def run_transaction(self, mode, quiesce=False, mv_hook="", post="", pre=""):
        td = tempfile.TemporaryDirectory(); base = Path(td.name)
        root = base / "root"; data = base / "data"; (root / "old").mkdir(parents=True); (data / "old").mkdir(parents=True)
        stage = base / STAGE_NAME; backup = stage / "vesselstack-backup"
        (backup / str(root).lstrip("/")).mkdir(parents=True); (backup / str(data).lstrip("/")).mkdir(parents=True)
        (backup / str(root).lstrip("/") / "new").write_text("new"); (backup / str(data).lstrip("/") / "new").write_text("new")
        journal = make_journal(base, root, data, stage)
        old = journal / "controller"; new = journal / "new-controller"
        log = base / "calls.log"; active = base / "active"; log.write_text("")
        active.write_text("\n".join(["vesselstack-control-panel.service", "vesselstack-alerts.timer", "vesselstack-alerts.service"]) + "\n" if quiesce else "")
        systemd = base / "etc/systemd/system"; systemd.mkdir(parents=True)
        for unit in ("vesselstack-control-panel.service", "vesselstack-alerts.timer", "vesselstack-alerts.service"):
            (systemd / unit).write_text("unit")
        body = transaction_source() + """
SYSTEMD_DIR="@SYSTEMD@"
restore_run_controller() { action=$3; if [ "$action" = stop ]; then if [ "$2" = "@NEW@" ]; then [ "@MODE@" != new_stop_fail ]; else [ "@MODE@" != old_stop_fail ]; fi; elif [ "$action" = start ]; then if [ "$2" = "@NEW@" ]; then [ "@MODE@" != new_start_fail ] && [ "@MODE@" != new_stop_fail ]; else true; fi; fi; }
docker() { return 1; }
restore_docker_processes() { return 1; }
restore_publish_externals() { return 0; }
restore_sync_system_files() { return 0; }
restore_restore_externals() { return 0; }
restore_check_externals() { return 0; }
systemctl() {
    echo "systemctl $*" >> "@LOG@"
    local u
    case "$1" in
        is-active) u=${@: -1}; grep -qx "$u" "@ACTIVE@" ;;
        show) if [ "${3:-}" = ActiveState ]; then
                  u=${@: -1}
                  if [ -e "@ACTIVE@.act" ] && grep -qx "$u" "@ACTIVE@.act"; then echo activating
                  elif grep -qx "$u" "@ACTIVE@"; then echo active; else echo inactive; fi
              fi ;;
        stop) for u in "${@:2}"; do
                  case " ${STUCK_UNITS:-} " in *" $u "*) return 1 ;; esac
                  if [ -e "@ACTIVE@.act" ]; then { grep -vx "$u" "@ACTIVE@.act" || true; } > "@ACTIVE@.act.n"; command mv "@ACTIVE@.act.n" "@ACTIVE@.act"; fi
                  { grep -vx "$u" "@ACTIVE@" || true; } > "@ACTIVE@.n"; command mv "@ACTIVE@.n" "@ACTIVE@"
              done ;;
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
@PRE@
restore_commit_transaction "@STAGE@" "@JOURNAL@" "@ROOT@" "@DATA@" 20260102T030405Z-1-1 "@OLD@" "@NEW@" 1 && rc=0 || rc=$?
@POST@
exit "$rc"
"""
        for key, value in {"@SYSTEMD@": systemd, "@NEW@": new, "@OLD@": old, "@MODE@": mode, "@LOG@": log, "@ACTIVE@": active,
                           "@BACKUPROOT@": backup / str(root).lstrip("/"), "@ROOT@": root, "@DATA@": data, "@STAGE@": stage,
                           "@JOURNAL@": journal, "@HOOK@": mv_hook.replace("@ACTIVE@", str(active)).replace("@DATA@", str(data)).replace("@BACKUPDATA@", str(backup / str(data).lstrip("/"))),
                           "@PRE@": pre.replace("@ACTIVE@", str(active)).replace("@DATA@", str(data)).replace("@BASE@", str(base)),
                           "@POST@": post.replace("@STAGE@", str(stage)).replace("@JOURNAL@", str(journal))}.items():
            body = body.replace(key, str(value))
        result = self.run_harness(body, base)
        self.base = base; self.journal = journal; self.stage = stage; self.log = log
        self.archived = lambda kind: base / "restore" / f"{kind}-{STAMP}"
        return td, result, root, data

    def test_transaction_success(self):
        td, result, root, data = self.run_transaction("success"); self.addCleanup(td.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((root / "new").read_text(), "new"); self.assertEqual((data / "new").read_text(), "new")

    def test_transaction_start_failure_recovers_old_pair(self):
        td, result, root, data = self.run_transaction("new_start_fail"); self.addCleanup(td.cleanup)
        self.assertNotEqual(result.returncode, 0); self.assertTrue((root / "old").exists()); self.assertTrue((data / "old").exists())

    def test_transaction_new_stop_failure_is_only_a_warning(self):
        # The restored stack cannot be stopped through its controller: that is a WARN, the units are checked
        # instead, and the previous trees still come back.
        td, result, root, data = self.run_transaction("new_stop_fail"); self.addCleanup(td.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((root / "old").exists(), result.stderr); self.assertTrue((data / "old").exists(), result.stderr)
        self.assertIn("WARN the restored stack could not be stopped", result.stderr)

    def test_a_phase_that_cannot_be_recorded_aborts_from_the_last_recorded_phase(self):
        pre = 'restore_state_set() { local j=$1; shift; case "$*" in *phase=root-swap*) return 1 ;; esac; python3 -I "$j/restore_journal.py" set "$j" "$@"; }'
        td, result, root, data = self.run_transaction("success", pre=pre); self.addCleanup(td.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('could not record the restore phase "root-swap"', result.stderr)
        # data had been swapped; the journal's last recorded phase (data-swap) is enough to undo it by inode
        self.assertTrue((root / "old").exists(), result.stderr); self.assertTrue((data / "old").exists(), result.stderr)
        self.assertEqual(phase_of(self.archived("rolled-back")), "rolled-back")

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
        # every writer unit is stopped before the first rename, but only the ones that were running are recorded
        self.assertIn("systemctl stop vesselstack-homepage-refresh.service", calls[:first_mv])
        self.assertNotIn("systemctl start vesselstack-homepage-refresh.service", calls)
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

    # ---- the forward path stops and verifies like the rollback, before the first rename ----

    FAKE_DOCKER = '''
docker() {
    echo "docker $*" >> "@BASE@/calls.log"
    local id fmt line name project mounts a m
    case "$1" in
        info) return 0 ;;
        ps) [ -e "@BASE@/containers" ] && cut -d'|' -f1 "@BASE@/containers" || true ;;
        inspect) fmt=$3; id=${@: -1}
            line=$(grep "^$id|" "@BASE@/containers") || return 1
            IFS='|' read -r _ name project mounts <<< "$line"
            case "$fmt" in *Mounts*) tr ';' '\\n' <<< "$mounts" | while IFS= read -r m; do case "$m" in ro:*) echo "false|${m#ro:}" ;; *) echo "true|$m" ;; esac; done ;;
                *) printf '/%s|%s\\n' "$name" "$project" ;; esac ;;
        start) case " ${DOCKER_START_FAIL:-} " in *" ${@: -1} "*) return 1 ;; esac ;;
        stop) for a in "${@:2}"; do
                  case "$a" in -t|30) continue ;; esac
                  case " ${DOCKER_STUCK:-} " in *" $a "*) continue ;; esac
                  { grep -v "^$a|" "@BASE@/containers" || true; } > "@BASE@/containers.n"; command mv "@BASE@/containers.n" "@BASE@/containers"
              done ;;
        *) return 0 ;;
    esac
}
'''

    def test_forward_path_stops_a_unit_that_is_activating_and_starts_it_again(self):
        pre = 'echo vesselstack-alerts.service > "@ACTIVE@.act"'
        td, result, root, data = self.run_transaction("success", pre=pre); self.addCleanup(td.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text().splitlines()
        first_mv = next(i for i, line in enumerate(calls) if line.startswith("MV "))
        self.assertIn("systemctl stop vesselstack-alerts.service", calls[:first_mv])
        self.assertIn("systemctl start vesselstack-alerts.service", calls[first_mv:])
        self.assertEqual((self.journal / "quiesced-units").read_text().split(), ["vesselstack-alerts.service"])

    def test_forward_path_aborts_before_any_move_when_a_unit_cannot_be_stopped(self):
        pre = 'echo vesselstack-alerts.service > "@ACTIVE@.act"; STUCK_UNITS=vesselstack-alerts.service'
        td, result, root, data = self.run_transaction("success", pre=pre); self.addCleanup(td.cleanup)
        self.assertNotEqual(result.returncode, 0)
        calls = self.log.read_text().splitlines()
        self.assertFalse([c for c in calls if c.startswith("MV ") and "restore/current" not in c], calls)  # only the journal is archived
        self.assertIn("these units are still running", result.stderr)
        self.assertTrue((root / "old").exists()); self.assertTrue((data / "old").exists())
        self.assertEqual(phase_of(self.archived("rolled-back")), "rolled-back")
        self.assertIn("systemctl start vesselstack-alerts.service", calls, "what was stopped is started again")

    def test_forward_path_stops_a_container_that_mounts_the_data_folder(self):
        pre = self.FAKE_DOCKER + 'printf "c1|plain|none|@DATA@/sub\\n" > "@BASE@/containers"'
        td, result, root, data = self.run_transaction("success", pre=pre); self.addCleanup(td.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text().splitlines()
        first_mv = next(i for i, line in enumerate(calls) if line.startswith("MV "))
        self.assertTrue(any(c.startswith("docker stop") and "c1" in c for c in calls[:first_mv]), calls)

    def test_forward_path_aborts_before_any_move_when_a_container_cannot_be_stopped(self):
        pre = self.FAKE_DOCKER + 'DOCKER_STUCK=c1; printf "c1|plain|none|@DATA@/sub\\n" > "@BASE@/containers"'
        td, result, root, data = self.run_transaction("success", pre=pre); self.addCleanup(td.cleanup)
        self.assertNotEqual(result.returncode, 0)
        calls = self.log.read_text().splitlines()
        self.assertFalse([c for c in calls if c.startswith("MV ") and "restore/current" not in c], calls)
        self.assertIn("Containers are still running", result.stderr)
        self.assertTrue((root / "old").exists()); self.assertTrue((data / "old").exists())

    def test_a_unit_whose_state_cannot_be_read_is_not_idle(self):
        for name, fake in (("systemctl fails", "return 1"), ("empty answer", "return 0"), ("inactive", "echo inactive"),
                           ("failed", "echo failed"), ("activating", "echo activating"), ("active", "echo active")):
            with self.subTest(name):
                body = transaction_source() + f"systemctl() {{ {fake}; }}\nrestore_unit_idle some.service && echo IDLE || echo BUSY\n"
                with tempfile.TemporaryDirectory() as td:
                    result = self.run_harness(body, Path(td))
                self.assertEqual(result.stdout.strip(), "IDLE" if name in ("inactive", "failed") else "BUSY", result.stderr)

    # ---- the firewall: restarted after the system files are back, never stopped ----

    def test_the_firewall_is_restarted_after_the_system_files_are_restored_and_never_stopped(self):
        lay = self.layout("externals", "swapped", "swapped", True)
        self.reconcile(lay)
        self.assert_rolled_back(lay)
        calls = lay["calls"]
        self.assertIn("systemctl restart vesselstack-firewall.service", calls)
        self.assertLess(calls.index("systemctl daemon-reload"), calls.index("systemctl restart vesselstack-firewall.service"))
        self.assertNotIn("systemctl stop vesselstack-firewall.service", calls)

    def test_the_firewall_is_left_alone_when_no_system_file_was_touched(self):
        lay = self.layout("data-swap", "swapped", "none", False)
        self.reconcile(lay)
        self.assert_rolled_back(lay)
        self.assertNotIn("systemctl restart vesselstack-firewall.service", lay["calls"])

    def test_a_firewall_that_cannot_be_restarted_is_said_plainly_and_does_not_fail_the_rollback(self):
        lay = self.layout("externals", "swapped", "swapped", True)
        self.reconcile(lay, extra="FAIL_UNIT_START=vesselstack-firewall.service")
        self.assert_rolled_back(lay)
        self.assertIn("the firewall (vesselstack-firewall.service) could not be restarted", lay["result"].stderr)

    def test_status_3_says_when_the_firewall_is_not_running(self):
        lay = self.layout("externals", "swapped", "swapped", True)
        self.reconcile(lay, extra="FAIL_UNIT_START=vesselstack-firewall.service; FAIL_START_ALWAYS=1")
        self.assertEqual(lay["result"].returncode, 3, lay["result"].stderr)
        self.assertIn("The firewall (vesselstack-firewall.service) is NOT running", lay["result"].stderr)

    # ---- a journal left in phase "abandoned" is only archived ----

    def test_an_abandoned_journal_is_only_archived_and_nothing_is_touched(self):
        lay = self.layout("abandoned")
        self.reconcile(lay)
        self.assertEqual(lay["result"].returncode, 0, lay["result"].stderr)
        self.assertIn("outcome=abandoned", lay["result"].stdout)
        self.assertFalse(lay["journal"].exists()); self.assertFalse(lay["stage"].exists())
        archived = self.archive(lay, "abandoned")
        self.assertEqual(phase_of(archived), "abandoned")
        self.assertNotIn("rollback-failed", (archived / "state").read_text())
        self.assertEqual((lay["root"] / "marker").read_text(), "old-root"); self.assertEqual((lay["data"] / "marker").read_text(), "old-data")
        # nothing is moved or rolled back: the follow-up only starts what the restore had stopped
        self.assertFalse([c for c in lay["calls"] if c.startswith(("controller ", "docker stop", "systemctl stop", "systemctl daemon-reload"))], lay["calls"])
        self.assertIn("systemctl start vesselstack-control-panel.service", lay["calls"])

    def test_the_journal_controller_is_a_dereferenced_copy_and_restore_locks_with_the_normalised_root(self):
        text = CONTROLLER.read_text()
        self.assertIn('cp -L -p -- "$0" "$building/controller"', text)
        lock = text[text.index("\nensure_lifecycle_lock() {"):text.index("\nusage() {")]
        self.assertIn('lock_root=$(restore_normalize_path "$ROOT")', lock)
        self.assertNotIn('--root "$ROOT"', lock)

    def test_a_rollback_failure_lists_the_units_it_stopped(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        lay["active"].write_text("vesselstack-chat.service\n")
        self.reconcile(lay, extra='STUCK_UNITS=vesselstack-control-panel.service; echo vesselstack-control-panel.service >> "@ACTIVE@"'.replace("@ACTIVE@", str(lay["active"])))
        self.assertNotEqual(lay["result"].returncode, 0)
        self.assertIn("Units stopped during the recovery", lay["result"].stderr)
        self.assertIn("vesselstack-control-panel.service", lay["result"].stderr.split("Units stopped during the recovery")[1])

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


    # ---- system-level files after restoring an older (or the same) release ----
    NEWER_ONLY = ("vesselstack-backup.service", "vesselstack-backup.timer",
                  "vesselstack-alerts.service", "vesselstack-alerts.timer")

    @staticmethod
    def fs_state(*dirs):
        state = {}
        for top in dirs:
            for path in sorted(Path(top).rglob("*")):
                if path.is_symlink():
                    state[str(path)] = ("link", os.readlink(path))
                elif path.is_file():
                    state[str(path)] = ("file", path.read_bytes(), path.stat().st_mode & 0o777)
        return state

    def run_sync(self, archive="older", scripts="older", mode="success"):
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup); base = Path(td.name)
        root = base / "root"; data = base / "data"
        (root / "old").mkdir(parents=True); (data / "old").mkdir(parents=True)
        systemd = base / "etc/systemd/system"; sbin = base / "usr/local/sbin"; libexec = base / "usr/local/libexec/vesselstack"
        for d in (systemd / "multi-user.target.wants", systemd / "timers.target.wants", sbin, libexec):
            d.mkdir(parents=True)
        def put(path, text, mode=0o644):
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text); path.chmod(mode)
        # the running (newer) system
        for unit in ("vesselstack-chat.service", "vesselstack-control-panel.service", "vesselstack-custom.service") + self.NEWER_ONLY:
            put(systemd / unit, f"current {unit}")
        (systemd / "multi-user.target.wants/vesselstack-chat.service").symlink_to("../vesselstack-chat.service")
        (systemd / "multi-user.target.wants/vesselstack-custom.service").symlink_to("../vesselstack-custom.service")
        for unit in ("vesselstack-backup.timer", "vesselstack-alerts.timer"):
            (systemd / "timers.target.wants" / unit).symlink_to(f"../{unit}")
        put(sbin / "vesselstackctl", "ctl-new", 0o755); put(sbin / "vesselstack-update", "update-new", 0o755)
        put(libexec / "deploy-chat.py", "chat-new", 0o755); put(libexec / "remediate.sh", "rem-new")
        put(libexec / "record-backup-result", "rec-new", 0o755); put(libexec / "vesselstack-alerts.py", "alerts-new", 0o755)
        put(base / "etc/vesselstack/update.env", "KEY=1", 0o600)
        # the staged archive
        stage = base / STAGE_NAME; backup = stage / "vesselstack-backup"
        def staged(path): return backup / str(path).lstrip("/")
        (staged(root)).mkdir(parents=True); (staged(data) / "x").parent.mkdir(parents=True); (staged(data) / "new").write_text("new")
        (staged(root) / "new").write_text("new")
        archived = ["vesselstack-chat.service", "vesselstack-control-panel.service"]
        if archive == "same": archived += list(self.NEWER_ONLY)
        if archive == "none": archived = []
        for unit in archived: put(staged(systemd / unit), f"archived {unit}")
        put(staged(sbin / "vesselstackctl"), "ctl-old", 0o755)
        if scripts != "none":
            sc = staged(root) / "installer/scripts"; (sc / "lib").mkdir(parents=True)
            put(sc / "vesselstack-update", "update-old", 0o755); put(sc / "deploy-chat.py", "chat-old", 0o755)
            put(sc / "lib/remediate.sh", "rem-old")
            if scripts == "same":
                put(sc / "lib/record-backup-result", "rec-old", 0o755); put(sc / "vesselstack-alerts.py", "alerts-old", 0o755)
        journal = make_journal(base, root, data, stage)
        old = journal / "controller"; new = journal / "new-controller"
        log = base / "calls.log"; log.write_text("")
        active = base / "active"
        active.write_text("vesselstack-alerts.timer\nvesselstack-alerts.service\nvesselstack-control-panel.service\n")
        before = self.fs_state(base / "etc", base / "usr")
        body = transaction_source() + """
SYSTEMD_DIR="@SYSTEMD@"
restore_run_controller() {
    action=$3
    if [ "$action" = start ] && [ "$2" = "@NEW@" ]; then
        [ -e "$SYSTEMD_DIR/vesselstack-backup.timer" ] || : > "@JOURNAL@.removed-before-start"
        [ "@MODE@" != new_start_fail ]
    else true; fi
}
mv() { __mv "$@"; }
docker() { return 1; }
restore_docker_processes() { return 1; }
systemctl() {
    local u state=missing
    case "$1" in
        is-active) u=${@: -1}; grep -qx "$u" "@ACTIVE@" ;;
        show) if [ "${3:-}" = ActiveState ]; then echo inactive; fi ;;
        stop) echo "systemctl $*" >> "@LOG@"; for u in "${@:2}"; do { grep -vx "$u" "@ACTIVE@" || true; } > "@ACTIVE@.n"; command mv "@ACTIVE@.n" "@ACTIVE@"; done ;;
        start) u=$2; [ -e "$SYSTEMD_DIR/$u" ] && state=present
               echo "systemctl start $u unit-file=$state" >> "@LOG@"; echo "$u" >> "@ACTIVE@" ;;
        *) echo "systemctl $*" >> "@LOG@"; return 0 ;;
    esac
}
restore_snapshot_externals "@JOURNAL@" "@STAGE@"
restore_commit_transaction "@STAGE@" "@JOURNAL@" "@ROOT@" "@DATA@" 20260102T030405Z-1-1 "@OLD@" "@NEW@" 1 && rc=0 || rc=$?
exit "$rc"
"""
        for key, value in {"@SYSTEMD@": systemd, "@NEW@": new, "@OLD@": old, "@MODE@": mode, "@LOG@": log, "@ACTIVE@": active,
                           "@ROOT@": root, "@DATA@": data, "@STAGE@": stage, "@JOURNAL@": journal}.items():
            body = body.replace(key, str(value))
        result = self.run_harness(body, base)
        return {"base": base, "result": result, "before": before, "journal": journal, "log": log.read_text().splitlines(),
                "systemd": systemd, "sbin": sbin, "libexec": libexec, "root": root}

    def test_older_release_removes_units_it_does_not_have_and_rebuilds_helpers(self):
        r = self.run_sync(); s = r["systemd"]
        self.assertEqual(r["result"].returncode, 0, r["result"].stderr)
        for unit in self.NEWER_ONLY:
            self.assertFalse((s / unit).exists(), unit)
        for wants in ("vesselstack-backup.timer", "vesselstack-alerts.timer"):
            self.assertFalse((s / "timers.target.wants" / wants).is_symlink(), wants)
        self.assertFalse((r["libexec"] / "vesselstack-alerts.py").exists())
        self.assertEqual((s / "vesselstack-chat.service").read_text(), "archived vesselstack-chat.service")
        self.assertEqual((r["libexec"] / "deploy-chat.py").read_text(), "chat-old")
        self.assertEqual((r["libexec"] / "remediate.sh").read_text(), "rem-old")
        self.assertEqual((r["sbin"] / "vesselstack-update").read_text(), "update-old")
        self.assertEqual(oct((r["libexec"] / "deploy-chat.py").stat().st_mode & 0o777), "0o755")
        self.assertEqual(oct((r["libexec"] / "remediate.sh").stat().st_mode & 0o777), "0o644")
        # the helper the older tree does not ship is gone; vesselstackctl comes from the archive
        self.assertFalse((r["libexec"] / "record-backup-result").exists())
        self.assertEqual((r["sbin"] / "vesselstackctl").read_text(), "ctl-old")
        # the owner's own unit and its enable link are untouched
        self.assertEqual((s / "vesselstack-custom.service").read_text(), "current vesselstack-custom.service")
        self.assertEqual(os.readlink(s / "multi-user.target.wants/vesselstack-custom.service"), "../vesselstack-custom.service")
        self.assertEqual(sorted((r["journal"] / "removed-units").read_text().split()), sorted(self.NEWER_ONLY))
        # removed units are not started again; the Control Panel (still present) is
        calls = r["log"]
        self.assertFalse([c for c in calls if c.startswith("systemctl start vesselstack-alerts")], calls)
        self.assertIn("systemctl start vesselstack-control-panel.service unit-file=present", calls)
        self.assertEqual([c for c in calls if c == "systemctl daemon-reload"], ["systemctl daemon-reload"])

    def test_failure_after_the_externals_step_restores_everything(self):
        r = self.run_sync(mode="new_start_fail")
        self.assertNotEqual(r["result"].returncode, 0)
        self.assertTrue(Path(str(r["journal"]) + ".removed-before-start").exists(), "units were never removed before the failure")
        after = self.fs_state(r["base"] / "etc", r["base"] / "usr")
        self.assertEqual(after, r["before"])
        self.assertTrue(os.path.islink(r["systemd"] / "timers.target.wants/vesselstack-alerts.timer"))
        self.assertEqual(os.readlink(r["systemd"] / "timers.target.wants/vesselstack-alerts.timer"), "../vesselstack-alerts.timer")
        # the rollback archived the journal; the record of removed units is deliberately not kept
        archived = r["base"] / "restore" / f"rolled-back-{STAMP}"
        self.assertTrue((archived / "state").is_file()); self.assertFalse((archived / "removed-units").exists())
        self.assertFalse(r["journal"].exists())
        # the quiesced timer comes back only after its unit file was restored
        starts = [c for c in r["log"] if c.startswith("systemctl start vesselstack-alerts.timer")]
        self.assertEqual(starts, ["systemctl start vesselstack-alerts.timer unit-file=present"])
        self.assertTrue((r["root"] / "old").exists())

    def test_tree_without_installer_scripts_leaves_helpers_alone(self):
        r = self.run_sync(scripts="none")
        self.assertEqual(r["result"].returncode, 0, r["result"].stderr)
        self.assertEqual((r["libexec"] / "deploy-chat.py").read_text(), "chat-new")
        self.assertEqual((r["libexec"] / "vesselstack-alerts.py").read_text(), "alerts-new")
        self.assertEqual((r["sbin"] / "vesselstack-update").read_text(), "update-new")
        self.assertFalse((r["systemd"] / "vesselstack-alerts.timer").exists())

    def test_same_release_removes_nothing(self):
        r = self.run_sync(archive="same", scripts="same")
        self.assertEqual(r["result"].returncode, 0, r["result"].stderr)
        for unit in self.NEWER_ONLY:
            self.assertEqual((r["systemd"] / unit).read_text(), f"archived {unit}")
        self.assertTrue((r["systemd"] / "timers.target.wants/vesselstack-alerts.timer").is_symlink())
        self.assertEqual((r["journal"] / "removed-units").read_text(), "")
        self.assertEqual((r["libexec"] / "vesselstack-alerts.py").read_text(), "alerts-old")
        self.assertEqual((r["libexec"] / "record-backup-result").read_text(), "rec-old")

    def test_archive_without_unit_files_removes_no_units(self):
        r = self.run_sync(archive="none")
        self.assertEqual(r["result"].returncode, 0, r["result"].stderr)
        self.assertIn("WARN the backup contains no VesselStack unit files; leaving the installed units in place", r["result"].stderr)
        s = r["systemd"]
        for unit in ("vesselstack-chat.service", "vesselstack-control-panel.service", "vesselstack-custom.service") + self.NEWER_ONLY:
            self.assertEqual((s / unit).read_text(), f"current {unit}", unit)
        self.assertEqual(os.readlink(s / "multi-user.target.wants/vesselstack-chat.service"), "../vesselstack-chat.service")
        for unit in ("vesselstack-backup.timer", "vesselstack-alerts.timer"):
            self.assertEqual(os.readlink(s / "timers.target.wants" / unit), f"../{unit}")
        removed = r["journal"] / "removed-units"
        self.assertEqual(removed.read_text() if removed.exists() else "", "")

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

    # ---- crash and recovery: restore_reconcile (what `restore-recover rollback` runs) from every journal phase ----

    # (name, phase recorded, data step, root step, system files published)
    CRASH_POINTS = [
        ("before the data swap", "data-swap", "none", "none", False),
        ("data renamed away only", "data-swap", "away", "none", False),
        ("data swapped", "data-swap", "swapped", "none", False),
        ("before the root swap", "root-swap", "swapped", "none", False),
        ("root renamed away only", "root-swap", "swapped", "away", False),
        ("both swapped", "root-swap", "swapped", "swapped", False),
        ("externals published", "externals", "swapped", "swapped", True),
        ("reloaded", "reloaded", "swapped", "swapped", True),
        ("started", "starting", "swapped", "swapped", True),
    ]

    def layout(self, phase, data_step="none", root_step="none", published=False, quiesced=("vesselstack-control-panel.service",)):
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup); base = Path(td.name)
        root = base / "opt/vesselstack"; data = base / "opt/vesselstack-data"
        for tree, label in ((root, "root"), (data, "data")):
            tree.mkdir(parents=True); (tree / "marker").write_text(f"old-{label}")
        sbin = base / "usr/local/sbin"; systemd = base / "etc/systemd/system"
        sbin.mkdir(parents=True); systemd.mkdir(parents=True)
        (sbin / "vesselstackctl").write_text("old-ctl"); (sbin / "vesselstackctl").chmod(0o755)
        (systemd / "vesselstack-chat.service").write_text("old-unit")
        stage = base / "opt" / STAGE_NAME; backup = stage / "vesselstack-backup"
        for tree, label in ((root, "root"), (data, "data")):
            staged = backup / str(tree).lstrip("/"); staged.mkdir(parents=True); (staged / "marker").write_text(f"new-{label}")
        for path, text in ((sbin / "vesselstackctl", "new-ctl"), (systemd / "vesselstack-chat.service", "new-unit"),
                           (systemd / "vesselstack-extra.service", "new-extra")):
            staged = backup / str(path).lstrip("/"); staged.parent.mkdir(parents=True, exist_ok=True); staged.write_text(text)
        journal = make_journal(base, root, data, stage, phase=phase)
        (journal / "quiesced-units").write_text("".join(f"{unit}\n" for unit in quiesced))
        (base / "active").write_text("")
        original = {"root": inode(root), "data": inode(data)}
        new = {"root": inode(backup / str(root).lstrip("/")), "data": inode(backup / str(data).lstrip("/"))}
        for tree, step in ((data, data_step), (root, root_step)):
            if step in ("away", "swapped"):
                os.rename(tree, f"{tree}.pre-restore-{STAMP}")
            if step == "swapped":
                os.rename(backup / str(tree).lstrip("/"), tree)
        return {"base": base, "root": root, "data": data, "stage": stage, "journal": journal, "sbin": sbin, "systemd": systemd,
                "original": original, "new": new, "published": published, "log": base / "calls.log",
                "active": base / "active", "root_step": root_step, "data_step": data_step, "phase": phase}

    def reconcile(self, lay, extra="", via_recover=False):
        body = pending_source() + transaction_source() + """
SYSTEMD_DIR="@SYSTEMD@"
mv() { if [ "${LOG_RM:-0}" = 1 ]; then echo "MV $*" >> "@LOG@"; fi; __mv "$@"; }
rm() { if [ "${LOG_RM:-0}" = 1 ]; then echo "RM $*" >> "@LOG@"; fi; command rm "$@"; }
restore_run_controller() {
    echo "controller $2 $3" >> "@LOG@"
    if [ -n "${FAIL_START_FLAG:-}" ] && [ "$3" = start ] && [ -e "$FAIL_START_FLAG" ]; then rm -f "$FAIL_START_FLAG"; return 1; fi
    if [ "${FAIL_START_ALWAYS:-0}" = 1 ] && [ "$3" = start ]; then return 1; fi
    if [ "${FAIL_NEW_STOP:-0}" = 1 ] && [ "$3" = stop ] && [ "$2" = "@J@/new-controller" ]; then return 1; fi
    return 0
}
active_remove() { local line out=''; while IFS= read -r line; do [ "$line" = "$1" ] || out="$out$line"$'\n'; done < "@ACTIVE@"; printf '%s' "$out" > "@ACTIVE@"; }
systemctl() {
    echo "systemctl $*" >> "@LOG@"
    local u
    case "$1" in
        is-active) u=${@: -1}; case $'\n'"$(<"@ACTIVE@")"$'\n' in *$'\n'"$u"$'\n'*) true ;; *) false ;; esac ;;
        show) if [ "${3:-}" = ActiveState ]; then
                  u=${@: -1}
                  case " ${ACTIVATING_UNITS:-} " in *" $u "*) echo activating; return 0 ;; esac
                  if [ -e "@ACTIVE@.act" ] && grep -qx "$u" "@ACTIVE@.act"; then echo activating; return 0; fi
                  case $'\n'"$(<"@ACTIVE@")"$'\n' in *$'\n'"$u"$'\n'*) echo active ;; *) echo inactive ;; esac
              fi ;;
        stop) for u in "${@:2}"; do
                  case " ${STUCK_UNITS:-} " in *" $u "*) return 1 ;; esac
                  if [ -e "@ACTIVE@.act" ]; then { grep -vx "$u" "@ACTIVE@.act" || true; } > "@ACTIVE@.act.n"; command mv "@ACTIVE@.act.n" "@ACTIVE@.act"; fi
                  active_remove "$u"
              done ;;
        start|restart) for u in "${@:2}"; do
                  case " ${FAIL_UNIT_START:-} " in *" $u "*) return 1 ;; esac
                  echo "$u" >> "@ACTIVE@"
              done ;;
        daemon-reload) [ "${FAIL_RELOAD:-0}" != 1 ] ;;
        *) return 0 ;;
    esac
}
# A fake docker: containers are lines "id|name|project|mount;mount" in @CONT@. DOCKER_DOWN=1: the daemon does not answer;
# DOCKER_STUCK: ids that "docker stop" cannot stop; DOCKER_PROCS=1: a dockerd process exists.
docker() {
    echo "docker $*" >> "@LOG@"
    local id fmt line name project mounts a m
    case "$1" in
        info) [ "${DOCKER_DOWN:-0}" != 1 ] ;;
        ps) [ -e "@CONT@" ] && cut -d'|' -f1 "@CONT@" || true ;;
        inspect) fmt=$3; id=${@: -1}
            line=$(grep "^$id|" "@CONT@") || return 1
            IFS='|' read -r _ name project mounts <<< "$line"
            case "$fmt" in *Mounts*) tr ';' '\n' <<< "$mounts" | while IFS= read -r m; do case "$m" in ro:*) echo "false|${m#ro:}" ;; *) echo "true|$m" ;; esac; done ;;
                *) printf '/%s|%s\n' "$name" "$project" ;; esac ;;
        start) case " ${DOCKER_START_FAIL:-} " in *" ${@: -1} "*) return 1 ;; esac ;;
        stop) for a in "${@:2}"; do
                  case "$a" in -t|30) continue ;; esac
                  case " ${DOCKER_STUCK:-} " in *" $a "*) continue ;; esac
                  { grep -v "^$a|" "@CONT@" || true; } > "@CONT@.n"; command mv "@CONT@.n" "@CONT@"
              done ;;
        *) return 0 ;;
    esac
}
restore_docker_processes() { [ "${DOCKER_PROCS:-0}" = 1 ]; }
restore_snapshot_externals "@J@" "@STAGE@"
if [ "@PUBLISHED@" = 1 ]; then restore_publish_externals "@J@" "@STAGE@"; fi
@EXTRA@
restore_reconcile "@J@" && rc=0 || rc=$?
echo "outcome=${restore_outcome:-}"
exit "$rc"
"""
        for key, value in {"@SYSTEMD@": lay["systemd"], "@LOG@": lay["log"], "@J@": lay["journal"], "@STAGE@": lay["stage"],
                           "@ACTIVE@": lay["active"], "@CONT@": lay["base"] / "containers", "@PUBLISHED@": "1" if lay["published"] else "0", "@EXTRA@": extra}.items():
            body = body.replace(key, str(value))
        if via_recover:
            # Not run here: the journal's own `recover` (extracted from the controller) runs this body as its controller,
            # which is how the boot script reaches the same code in these tests.
            tail = 'restore_reconcile "%s" && rc=0 || rc=$?\necho "outcome=${restore_outcome:-}"\nexit "$rc"\n' % lay["journal"]
            assert tail in body
            body = body.replace(tail, f'SYSTEM_ROOT="{lay["base"]}"\nRESTORE_CURRENT="{lay["journal"]}"\nROOT="/nonexistent/vesselstack"\n'
                                       f'usage() {{ echo usage; }}\nrestore_take_lock() {{ :; }}\nrestore_recover_command rollback\n')
            harness = lay["base"] / "controller-harness.sh"
            harness.write_text(self.build_harness(body, lay.get("real_rename", False))); harness.chmod(0o700)
            controller = lay["journal"] / "controller"
            controller.write_text(f'#!/bin/bash\nexec bash "{harness}" "{lay["base"]}"\n'); controller.chmod(0o700)
            return None
        result = self.run_harness(body, lay["base"], real_rename=lay.get("real_rename", False))
        lay["result"] = result
        lay["calls"] = lay["log"].read_text().splitlines() if lay["log"].exists() else []
        return result

    def archive(self, lay, kind):
        return lay["base"] / "restore" / f"{kind}-{STAMP}"

    def set_state(self, journal, *assignments):
        subprocess.run([sys.executable, "-I", str(journal / "restore_journal.py"), "set", str(journal), *assignments], check=True)

    def assert_rolled_back(self, lay, started_new_stack=None):
        result = lay["result"]
        if started_new_stack is None:
            # the restored stack is stopped through its own controller exactly when its tree was at ROOT
            started_new_stack = lay["root_step"] == "swapped"
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("outcome=rolled-back", result.stdout)
        # the original trees are at the live paths (by inode); a restored tree that was live is set aside
        self.assertEqual(inode(lay["root"]), lay["original"]["root"], result.stderr)
        self.assertEqual(inode(lay["data"]), lay["original"]["data"], result.stderr)
        self.assertEqual((lay["root"] / "marker").read_text(), "old-root")
        self.assertEqual((lay["data"] / "marker").read_text(), "old-data")
        for tree in ("root", "data"):
            path = lay[tree]
            self.assertFalse(Path(f"{path}.pre-restore-{STAMP}").exists(), f"{tree}: the pre-restore tree was left behind")
            failed = Path(f"{path}.failed-restore-{STAMP}")
            if failed.exists():
                self.assertEqual(inode(failed), lay["new"][tree], f"{tree}: the failed tree is not the staged tree")
        # system files are exactly the snapshot
        self.assertEqual((lay["sbin"] / "vesselstackctl").read_text(), "old-ctl")
        self.assertEqual((lay["systemd"] / "vesselstack-chat.service").read_text(), "old-unit")
        self.assertFalse((lay["systemd"] / "vesselstack-extra.service").exists())
        # the old stack and the stopped units were started again, the stage is gone, the journal is archived
        calls = lay["calls"]
        old_start = f"controller {lay['journal']}/controller start"
        new_stop = f"controller {lay['journal']}/new-controller stop"
        self.assertIn(old_start, calls)
        self.assertIn("systemctl start vesselstack-control-panel.service", calls)
        if started_new_stack:
            self.assertIn(new_stop, calls)
            self.assertLess(calls.index(new_stop), calls.index(old_start))
        else:
            self.assertNotIn(new_stop, calls)
        self.assertFalse(lay["stage"].exists())
        self.assertFalse(lay["journal"].exists(), "restore/current must be gone once rolled back")
        archived = self.archive(lay, "rolled-back")
        self.assertEqual(phase_of(archived), "rolled-back")
        for name in ("files", "controller", "new-controller", "lifecycle_lock.py", "restore_journal.py", "recover", "manifest"):
            self.assertFalse((archived / name).exists(), name)
        self.assertTrue((archived / "quiesced-units").is_file())

    def test_every_crash_point_of_the_swap_rolls_back_by_inode(self):
        for name, phase, data_step, root_step, published in self.CRASH_POINTS:
            with self.subTest(name):
                lay = self.layout(phase, data_step, root_step, published)
                self.reconcile(lay)
                self.assert_rolled_back(lay)
        # the swapped trees end up in *.failed-restore-{STAMP}
        lay = self.layout("root-swap", "swapped", "swapped", True)
        self.reconcile(lay)
        self.assertEqual((Path(f"{lay['root']}.failed-restore-{STAMP}") / "marker").read_text(), "new-root")
        self.assertEqual((Path(f"{lay['data']}.failed-restore-{STAMP}") / "marker").read_text(), "new-data")

    def test_prepared_and_stopping_change_no_tree(self):
        lay = self.layout("prepared", quiesced=())
        self.reconcile(lay)
        self.assertEqual(lay["result"].returncode, 0, lay["result"].stderr)
        self.assertEqual(inode(lay["root"]), lay["original"]["root"]); self.assertEqual(inode(lay["data"]), lay["original"]["data"])
        self.assertFalse(lay["stage"].exists())
        # only the read-only firewall questions are asked
        self.assertEqual([c for c in lay["calls"] if not c.startswith("systemctl is-")], [], "nothing was stopped, so nothing is started")
        self.assertEqual(phase_of(self.archive(lay, "rolled-back")), "rolled-back")
        self.assertEqual((lay["sbin"] / "vesselstackctl").read_text(), "old-ctl")
        for phase in ("stopping", "stopped"):
            lay = self.layout(phase)
            self.reconcile(lay)
            self.assertEqual(lay["result"].returncode, 0, lay["result"].stderr)
            self.assertIn(f"controller {lay['journal']}/controller start", lay["calls"], phase)
            self.assertIn("systemctl start vesselstack-control-panel.service", lay["calls"], phase)
            self.assertFalse(lay["stage"].exists())
            self.assertEqual(inode(lay["root"]), lay["original"]["root"])

    def test_running_reconcile_twice_is_harmless(self):
        # The first run fails after the trees are back (systemd cannot reload): rollback-failed.
        lay = self.layout("externals", "swapped", "swapped", True)
        self.reconcile(lay, extra="export FAIL_RELOAD=1")
        self.assertNotEqual(lay["result"].returncode, 0)
        self.assertIn("outcome=rollback-failed", lay["result"].stdout)
        self.assertEqual(phase_of(lay["journal"]), "rollback-failed")
        self.assertEqual(inode(lay["root"]), lay["original"]["root"])
        self.assertTrue(lay["journal"].exists() and lay["stage"].exists())
        # The second run retries the same decision procedure and finishes. The trees are back already, so the
        # restored stack is not stopped again (its controller is not even consulted).
        lay["log"].write_text("")
        self.reconcile(lay)
        self.assert_rolled_back(lay, started_new_stack=False)

    def test_retry_with_the_trees_back_never_calls_the_new_controller(self):
        lay = self.layout("starting", "swapped", "swapped", True)
        self.reconcile(lay, extra="export FAIL_RELOAD=1 FAIL_NEW_STOP=1")
        self.assertEqual(phase_of(lay["journal"]), "rollback-failed")
        self.assertEqual(inode(lay["root"]), lay["original"]["root"])
        self.assertIn(f"controller {lay['journal']}/new-controller stop", lay["calls"])   # a failing stop is only a warning
        self.assertIn("WARN the restored stack could not be stopped", lay["result"].stderr)
        lay["log"].write_text("")
        self.reconcile(lay, extra="export FAIL_NEW_STOP=1")
        self.assert_rolled_back(lay, started_new_stack=False)
        self.assertNotIn(f"controller {lay['journal']}/new-controller stop", lay["calls"])

    def test_a_failed_start_after_the_trees_are_back_ends_rolled_back_with_status_3(self):
        for phase, data_step, root_step in (("starting", "swapped", "swapped"), ("reloaded", "swapped", "swapped"),
                                            ("stopped", "none", "none"), ("stopping", "none", "none"), ("prepared", "none", "none")):
            with self.subTest(phase):
                lay = self.layout(phase, data_step, root_step, phase in ("starting", "reloaded"), quiesced=("vesselstack-control-panel.service",))
                self.reconcile(lay, extra="export FAIL_START_ALWAYS=1")
                result = lay["result"]
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn("outcome=rolled-back", result.stdout)
                self.assertIn("The previous system is back in place, but it could not be started:", result.stderr)
                self.assertIn("Run: sudo vesselstackctl start, then sudo vesselstackctl doctor", result.stderr)
                self.assertEqual(inode(lay["root"]), lay["original"]["root"]); self.assertEqual(inode(lay["data"]), lay["original"]["data"])
                self.assertFalse(lay["journal"].exists(), "a start failure must not leave the journal pending")
                archived = self.archive(lay, "rolled-back")
                self.assertEqual(phase_of(archived), "rolled-back")
                self.assertIn("could not be started", (archived / "state").read_text())
                if phase != "prepared":
                    self.assertIn("systemctl start vesselstack-control-panel.service", lay["calls"], "the quiesced units are still tried")
                self.assertFalse(lay["stage"].exists())

    def test_lock_taking_commands_work_again_after_a_start_failure(self):
        lay = self.layout("starting", "swapped", "swapped", True)
        self.reconcile(lay, extra="export FAIL_START_ALWAYS=1")
        self.assertEqual(lay["result"].returncode, 3)
        body = pending_source() + f'SYSTEM_ROOT="{lay["base"]}"; RESTORE_CURRENT="{lay["base"]}/restore/current"\nrestore_pending_check; echo passed\n'
        result = self.run_harness(body, lay["base"])
        self.assertEqual(result.returncode, 0, result.stderr); self.assertIn("passed", result.stdout)

    def test_a_failed_unit_restart_alone_also_ends_rolled_back_with_status_3(self):
        lay = self.layout("starting", "swapped", "swapped", True)
        self.reconcile(lay, extra="export FAIL_UNIT_START=vesselstack-control-panel.service")
        self.assertEqual(lay["result"].returncode, 3, lay["result"].stderr)
        self.assertIn("could not start: vesselstack-control-panel.service", lay["result"].stderr)
        self.assertFalse(lay["journal"].exists())

    def test_a_unit_that_is_already_running_is_restarted_not_started(self):
        lay = self.layout("stopping", quiesced=("vesselstack-control-panel.service",))
        lay["active"].write_text("vesselstack-control-panel.service\n")
        self.reconcile(lay)
        self.assertEqual(lay["result"].returncode, 0, lay["result"].stderr)
        self.assertIn("systemctl restart vesselstack-control-panel.service", lay["calls"])
        self.assertNotIn("systemctl start vesselstack-control-panel.service", lay["calls"])

    def test_unknown_non_empty_directory_at_a_live_path_stops_and_deletes_nothing(self):
        lay = self.layout("root-swap", "swapped", "away", False)
        lay["root"].mkdir(); (lay["root"] / "precious").write_text("owner data")
        self.reconcile(lay)
        result = lay["result"]
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outcome=rollback-failed", result.stdout)
        self.assertEqual(phase_of(lay["journal"]), "rollback-failed")
        error = next(line[len("last_error="):] for line in (lay["journal"] / "state").read_text().splitlines() if line.startswith("last_error="))
        self.assertIn(str(lay["root"]), error); self.assertIn(str(lay["root"]), result.stderr)
        self.assertEqual((lay["root"] / "precious").read_text(), "owner data")
        self.assertTrue(Path(f"{lay['root']}.pre-restore-{STAMP}").exists())
        # ROOT is decided first and the procedure stops there: the data tree is untouched.
        self.assertEqual((lay["data"] / "marker").read_text(), "new-data")
        self.assertTrue(Path(f"{lay['data']}.pre-restore-{STAMP}").exists())
        self.assertFalse(Path(f"{lay['data']}.failed-restore-{STAMP}").exists())
        self.assertTrue(lay["stage"].exists() and lay["journal"].exists())
        self.assertNotIn(f"controller {lay['journal']}/controller start", lay["calls"])
        # An empty directory of unknown identity is only removed, never one with content.
        lay = self.layout("root-swap", "swapped", "away", False)
        lay["root"].mkdir()
        self.reconcile(lay)
        self.assert_rolled_back(lay)

    def test_committed_never_rolls_back_and_cleans_up(self):
        lay = self.layout("committed", "swapped", "swapped", True)
        (lay["journal"] / "removed-units").write_text("vesselstack-alerts.timer\n")
        records = lay["base"] / "restore"
        for n in range(1, 5):   # four older records: only the newest three may remain
            (records / f"done-2020010{n}T000000Z-1-1").mkdir()
        self.reconcile(lay)
        result = lay["result"]
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("outcome=committed", result.stdout)
        self.assertEqual((lay["root"] / "marker").read_text(), "new-root"); self.assertEqual((lay["data"] / "marker").read_text(), "new-data")
        self.assertTrue(Path(f"{lay['root']}.pre-restore-{STAMP}").exists())
        self.assertEqual(lay["calls"], [])
        self.assertEqual((lay["sbin"] / "vesselstackctl").read_text(), "new-ctl")
        self.assertFalse(lay["stage"].exists()); self.assertFalse(lay["journal"].exists())
        done = self.archive(lay, "done")
        self.assertEqual(phase_of(done), "committed")
        self.assertEqual((done / "removed-units").read_text(), "vesselstack-alerts.timer\n")
        self.assertTrue((done / "quiesced-units").is_file())
        for name in ("files", "controller", "new-controller", "lifecycle_lock.py", "restore_journal.py", "recover"):
            self.assertFalse((done / name).exists(), name)
        remaining = sorted(path.name for path in records.glob("done-*"))
        self.assertEqual(len(remaining), 3, remaining)
        self.assertIn(f"done-{STAMP}", remaining); self.assertNotIn("done-20200101T000000Z-1-1", remaining)

    def test_a_rolled_back_journal_that_was_not_archived_is_finished(self):
        lay = self.layout("rolled-back")
        self.reconcile(lay)
        self.assertEqual(lay["result"].returncode, 0, lay["result"].stderr)
        self.assertEqual(lay["calls"], [])
        self.assertEqual(phase_of(self.archive(lay, "rolled-back")), "rolled-back")
        self.assertFalse(lay["stage"].exists())

    def test_unknown_phase_and_missing_inodes_fail_closed(self):
        lay = self.layout("sideways", "swapped", "swapped", False)
        self.reconcile(lay)
        self.assertNotEqual(lay["result"].returncode, 0)
        self.assertEqual(phase_of(lay["journal"]), "rollback-failed")
        self.assertEqual((lay["root"] / "marker").read_text(), "new-root")
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.set_state(lay["journal"], "root_live=")
        self.reconcile(lay)
        self.assertNotEqual(lay["result"].returncode, 0)
        self.assertEqual((lay["root"] / "marker").read_text(), "new-root")
        self.assertEqual(phase_of(lay["journal"]), "rollback-failed")

    def test_in_process_failure_goes_through_the_same_function_and_names_the_journal(self):
        td, result, root, data = self.run_transaction("new_start_fail", quiesce=True); self.addCleanup(td.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(phase_of(self.archived("rolled-back")), "rolled-back")
        self.assertFalse(self.journal.exists()); self.assertTrue((root / "old").exists()); self.assertTrue((data / "old").exists())
        self.assertIn(f"Recovery journal: {self.journal}", result.stderr)
        self.assertIn(f"Staged copy of the backup: {self.stage}", result.stderr)
        self.assertIn(f"See: sudo {self.journal}/recover status", result.stderr)

    def test_a_successful_transaction_ends_committed_with_the_journal_in_place(self):
        td, result, root, data = self.run_transaction("success"); self.addCleanup(td.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(phase_of(self.journal), "committed")
        self.assertTrue(self.stage.exists(), "cleanup is the caller's job, after the phase is durable")

    def test_each_phase_is_on_disk_before_the_rename_it_names(self):
        hook = 'echo "$(sed -n s/^phase=//p "@JOURNAL@/state")" >> "@JOURNAL@.phases"'
        # run_transaction substitutes @JOURNAL@ only in `post`, so build the hook path from @ACTIVE@ (a sibling of restore/)
        hook = hook.replace('@JOURNAL@', '$(dirname @ACTIVE@)/restore/current')
        td, result, root, data = self.run_transaction("success", mv_hook=hook); self.addCleanup(td.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = (self.base / "restore/current.phases").read_text().split()
        self.assertEqual(seen, ["data-swap", "data-swap", "root-swap", "root-swap"])

    def test_every_phase_is_written_with_the_boot_id(self):
        hook = 'echo "$(sed -n s/^boot_id=//p "@JOURNAL@/state")" >> "@JOURNAL@.boots"'
        hook = hook.replace('@JOURNAL@', '$(dirname @ACTIVE@)/restore/current')
        td, result, root, data = self.run_transaction("success", mv_hook=hook); self.addCleanup(td.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = (self.base / "restore/current.boots").read_text().split()
        self.assertEqual(seen, [BOOT_ID] * 4)
        self.assertEqual(phase_of(self.journal), "committed")
        self.assertEqual(state_value(self.journal, "boot_id"), BOOT_ID)

    def test_a_rolled_back_journal_keeps_the_boot_id_and_no_failure_kind(self):
        td, result, root, data = self.run_transaction("new_start_fail", quiesce=True); self.addCleanup(td.cleanup)
        text = (self.archived("rolled-back") / "state").read_text()
        self.assertIn(f"boot_id={BOOT_ID}\n", text)
        self.assertNotRegex(text, r"last_error_kind=(retry|manual|previous)")

    def test_a_failed_rollback_stores_its_kind_and_the_boot_id(self):
        # retry: the trees are back but systemd cannot reload
        lay = self.layout("externals", "swapped", "swapped", True)
        self.reconcile(lay, extra="export FAIL_RELOAD=1")
        self.assertEqual(state_value(lay["journal"], "last_error_kind"), "retry")
        self.assertEqual(state_value(lay["journal"], "boot_id"), BOOT_ID)
        self.assertTrue(state_value(lay["journal"], "last_error"))
        # manual: the journal itself cannot be used
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.set_state(lay["journal"], "stamp=S")
        self.reconcile(lay)
        self.assertEqual(state_value(lay["journal"], "last_error_kind"), "manual")
        self.assertEqual(state_value(lay["journal"], "boot_id"), BOOT_ID)
        # previous: the previous folder is a symbolic link
        lay = self.layout("root-swap", "swapped", "swapped", False)
        pre = Path(f"{lay['root']}.pre-restore-{STAMP}"); moved = Path(f"{pre}.real")
        os.rename(pre, moved); pre.symlink_to(moved, target_is_directory=True)
        self.reconcile(lay)
        self.assertEqual(state_value(lay["journal"], "last_error_kind"), "previous")
        self.assertEqual(state_value(lay["journal"], "boot_id"), BOOT_ID)

    def test_a_retried_rollback_that_succeeds_leaves_no_failure_kind(self):
        lay = self.layout("externals", "swapped", "swapped", True)
        self.reconcile(lay, extra="export FAIL_RELOAD=1")
        self.assertEqual(state_value(lay["journal"], "last_error_kind"), "retry")
        self.reconcile(lay)
        self.assertEqual(lay["result"].returncode, 0, lay["result"].stderr)
        text = (self.archive(lay, "rolled-back") / "state").read_text()
        self.assertIn(f"boot_id={BOOT_ID}\n", text)
        self.assertNotRegex(text, r"last_error_kind=(retry|manual|previous)")

    def test_the_validator_accepts_a_journal_without_boot_id_and_rejects_bad_ones(self):
        def write(lay, **changes):
            path = lay["journal"] / "state"
            lines = [l for l in path.read_text().splitlines() if not l.startswith(("boot_id=", "last_error_kind="))]
            lines += [f"{k}={v}" for k, v in changes.items()]
            path.write_text("\n".join(lines) + "\n")
        for name, changes, outcome in (
            ("no boot id (0.9.0 journal)", {}, "rolled-back"),
            ("valid boot id", {"boot_id": BOOT_ID}, "rolled-back"),
            ("empty failure kind", {"last_error_kind": ""}, "rolled-back"),
            ("malformed boot id", {"boot_id": "not-a-boot-id"}, "rollback-failed"),
            ("upper-case boot id", {"boot_id": BOOT_ID.upper()}, "rollback-failed"),
            ("bad failure kind", {"last_error_kind": "sometimes"}, "rollback-failed"),
        ):
            with self.subTest(name):
                lay = self.layout("root-swap", "swapped", "swapped", False)
                write(lay, **changes)
                self.reconcile(lay)
                self.assertIn(f"outcome={outcome}", lay["result"].stdout, lay["result"].stderr)

    def test_recover_status_says_what_the_failure_kind_means(self):
        lay = self.layout("root-swap", "swapped", "away", False)
        self.set_state(lay["journal"], "last_error=boom")
        self.assertIn("Last error: boom\n", self.status_harness(lay).stdout)
        for kind, words in (("retry", "(this can be retried once the cause is fixed)"),
                            ("manual", "(a retry will not help; do the steps by hand that were printed with the error)"),
                            ("previous", "(a retry will not help; use one of the two ways out that were printed with the error)")):
            with self.subTest(kind):
                self.set_state(lay["journal"], f"last_error_kind={kind}")
                self.assertIn(f"Last error: boom {words}\n", self.status_harness(lay).stdout)

    # ---- recover status, with the application folder gone ----
    def status_harness(self, lay, action="status"):
        body = pending_source() + transaction_source() + f"""
usage() {{ echo usage; }}
SYSTEM_ROOT="{lay['base']}"
RESTORE_CURRENT="{lay['base']}/restore/current"
ROOT="/nonexistent/vesselstack"
restore_recover_command {action}
"""
        return self.run_harness(body, lay["base"])

    def test_recover_status_works_with_root_absent(self):
        lay = self.layout("root-swap", "swapped", "away", False)
        self.assertFalse(lay["root"].exists())
        result = self.status_harness(lay)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("A restore did not finish.", out)
        self.assertIn("Phase:     root-swap (swapping in the restored application folder)", out)
        self.assertIn("Started:   2026-01-02T03:04:05Z", out)
        self.assertIn("Archive:   /backups/x.tar.gz", out)
        self.assertIn(f"Staged copy of the backup: {lay['stage']}", out)
        self.assertIn(f"the previous one is kept as {lay['root']}.pre-restore-{STAMP}", out)
        self.assertIn(f"sudo {lay['base']}/restore/current/recover rollback", out)
        self.set_state(lay["journal"], "last_error=boom")
        self.assertIn("Last error: boom", self.status_harness(lay).stdout)

    def test_recover_status_for_a_committed_journal_says_it_only_cleans_up(self):
        lay = self.layout("committed", "swapped", "swapped", True)
        out = self.status_harness(lay).stdout
        self.assertIn("completed; only cleanup was left", out); self.assertIn("does not undo anything", out)

    def test_recover_with_no_journal_says_nothing_is_pending(self):
        lay = self.layout("prepared")
        shutil.rmtree(lay["journal"])
        for action in ("status", "rollback"):
            result = self.status_harness(lay, action)
            self.assertEqual(result.returncode, 0, result.stderr); self.assertEqual(result.stdout.strip(), "No restore is pending.")

    def test_recover_refuses_a_journal_for_another_system_root(self):
        lay = self.layout("prepared")
        self.set_state(lay["journal"], "system_root=/elsewhere")
        result = self.status_harness(lay)
        self.assertNotEqual(result.returncode, 0); self.assertIn("belongs to the system root", result.stderr)

    def test_recover_wrapper_runs_the_journals_controller_with_the_journals_paths(self):
        text = CONTROLLER.read_text()
        a = text.index("<<'RECOVER'\n") + len("<<'RECOVER'\n"); wrapper = text[a:text.index("\nRECOVER\n", a) + 1]
        lay = self.layout("root-swap", "swapped", "away", False)
        (lay["journal"] / "recover").write_text(wrapper); (lay["journal"] / "recover").chmod(0o700)
        (lay["journal"] / "controller").write_text('#!/bin/bash\necho "args=$*"; echo "root=$VESSELSTACK_ROOT"; echo "data=$VESSELSTACK_DATA"; echo "sys=$VESSELSTACK_SYSTEM_ROOT"; echo "env=${VESSELSTACK_ENV:-unset}"\n')
        env = dict(os.environ, VESSELSTACK_ENV="/should/be/ignored", VESSELSTACK_ROOT="/wrong")
        result = subprocess.run(["bash", str(lay["journal"] / "recover"), "status"], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["args=restore-recover status", f"root={lay['root']}", f"data={lay['data']}",
                                                      f"sys={lay['base']}", "env=unset"])

    # ---- the pending refusal and the doctor/status lines ----
    def test_pending_check_refuses_with_the_documented_message(self):
        lay = self.layout("data-swap")
        body = pending_source() + f'SYSTEM_ROOT="{lay["base"]}"; RESTORE_CURRENT="{lay["base"]}/restore/current"\nrestore_pending_check; echo passed\n'
        result = self.run_harness(body, lay["base"])
        self.assertEqual(result.returncode, 1); self.assertNotIn("passed", result.stdout)
        self.assertIn("A restore did not finish (phase: data-swap). Nothing else can change this system until it is resolved.", result.stderr)
        self.assertIn(f"See: sudo {lay['base']}/restore/current/recover status", result.stderr)
        # the restore's own children carry its id
        result = self.run_harness("VESSELSTACK_RESTORE_ID=abc123\n" + body, lay["base"])
        self.assertEqual(result.returncode, 0, result.stderr); self.assertIn("passed", result.stdout)
        result = self.run_harness("VESSELSTACK_RESTORE_ID=other\n" + body, lay["base"])
        self.assertEqual(result.returncode, 1)
        shutil.rmtree(lay["journal"])
        self.assertEqual(self.run_harness(body, lay["base"]).returncode, 0)

    def test_doctor_lines_and_leftover_warnings(self):
        lay = self.layout("starting", "swapped", "swapped", True)
        legacy = lay["base"] / "var/lib/vesselstack/.restore-journal.AbCdEf"; legacy.mkdir(parents=True)
        os.utime(legacy, (1, 1))
        body = pending_source() + controller_function_source("check_restore_state() {", "status_unit() {") + f"""
SYSTEM_ROOT="{lay['base']}"; RESTORE_CURRENT="{lay['base']}/restore/current"; RESTORE_DIR="{lay['base']}/restore"
VESSELSTACK_DATA="{lay['data']}"
if check_restore_state; then echo state-ok; else echo state-failed; fi
check_restore_leftovers
"""
        result = self.run_harness(body, lay["base"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("state-failed", result.stdout)
        self.assertIn("FAIL restore did not finish (phase starting)", result.stderr)
        self.assertIn(f"see: sudo {lay['base']}/restore/current/recover status", result.stderr)
        self.assertIn(f"WARN leftover restore data {legacy}", result.stderr)
        self.assertIn("days old", result.stderr); self.assertIn("including secrets", result.stderr)
        self.assertNotIn(f"data {lay['stage']}", result.stderr, "the stage the pending journal names is already reported")
        stray = lay["base"] / "opt/.vesselstack-restore.OLD"; stray.mkdir()
        shutil.rmtree(lay["journal"])
        result = self.run_harness(body, lay["base"])
        self.assertIn("state-ok", result.stdout)
        self.assertIn(str(stray), result.stderr)

    def test_dispatch_order_puts_recovery_before_the_lock_and_the_configuration(self):
        text = CONTROLLER.read_text()
        tail = text[text.index("\ncommand=$1\n"):]
        order = [tail.index(marker) for marker in ('restore_recover_command "$@"', "restore_pending_check; fi",
                                                   'ensure_lifecycle_lock "$command"', "\nload_config\n")]
        self.assertEqual(order, sorted(order))
        self.assertLess(text.index('Restore "INCOMPLETE (phase'), text.index("read_install_marker\n        if [ \"$MARKER_PRESENT\""))

    def run_real_controller(self, base, *args):
        """The real script, 'root' by a fake id, against a temp system root that has no application folder."""
        fakebin = base / "fakebin"; fakebin.mkdir(exist_ok=True)
        (fakebin / "id").write_text("#!/bin/sh\necho 0\n"); (fakebin / "id").chmod(0o755)
        env = dict(os.environ, PATH=f"{fakebin}:{os.environ['PATH']}", VESSELSTACK_SYSTEM_ROOT=str(base),
                   VESSELSTACK_ROOT=str(base / "opt/vesselstack-missing"))
        for name in ("VESSELSTACK_ENV", "VESSELSTACK_LIFECYCLE_LOCK_FD", "VESSELSTACK_RESTORE_ID"):
            env.pop(name, None)
        return subprocess.run(["bash", str(CONTROLLER), *args], capture_output=True, text=True, env=env)

    def test_real_controller_while_a_restore_is_pending_and_root_is_missing(self):
        lay = self.layout("root-swap", "swapped", "away", False)
        base = lay["base"]
        # in production the journal lives under <system root>/var/lib/vesselstack/restore
        (base / "var/lib/vesselstack").mkdir(parents=True)
        os.rename(base / "restore", base / "var/lib/vesselstack/restore")
        self.set_state(base / "var/lib/vesselstack/restore/current", f"system_root={base}")
        result = self.run_real_controller(base, "restore-recover", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Phase:     root-swap", result.stdout)
        self.assertEqual(self.run_real_controller(base, "restore", "--status").stdout, result.stdout)
        for command in (("start",), ("backup",), ("restore", "/x.tar.gz", "--yes"), ("component", "add", "grafana"), ("update", "/x")):
            refused = self.run_real_controller(base, *command)
            self.assertEqual(refused.returncode, 1, command)
            self.assertIn("A restore did not finish (phase: root-swap). Nothing else can change this system until it is resolved.", refused.stderr, command)
            self.assertIn(f"See: sudo {base}/var/lib/vesselstack/restore/current/recover status", refused.stderr, command)
        status = self.run_real_controller(base, "status")
        self.assertEqual(status.returncode, 1)
        self.assertIn("Restore              INCOMPLETE (phase root-swap); see: sudo", status.stdout)
        doctor = self.run_real_controller(base, "doctor")
        self.assertEqual(doctor.returncode, 1); self.assertIn("FAIL restore did not finish (phase root-swap)", doctor.stderr)
        shutil.rmtree(base / "var/lib/vesselstack/restore")
        self.assertEqual(self.run_real_controller(base, "restore", "--status").stdout, "No restore is pending.\n")
        self.assertEqual(self.run_real_controller(base, "restore-recover", "rollback").stdout, "No restore is pending.\n")

    def test_restore_refuses_when_a_journal_directory_already_exists(self):
        text = CONTROLLER.read_text()
        a = text.index("restore_stack() {"); body = text[a:text.index('    verify_backup "$archive"', a)]
        self.assertIn('if [ -e "$RESTORE_CURRENT" ] || [ -L "$RESTORE_CURRENT" ]; then', body)
        self.assertIn('echo "See: sudo $RESTORE_CURRENT/recover status" >&2', body)

    # ---- recovery that must never lock the owner out, and must not trust a damaged journal ----
    @staticmethod
    def state_text(journal):
        return (journal / "state").read_text()

    def test_units_that_are_running_are_stopped_before_any_tree_moves(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        lay["active"].write_text("vesselstack-chat.service\nvesselstack-alerts.timer\nvesselstack-control-panel.service\n")
        self.reconcile(lay, extra="export LOG_RM=1")
        self.assert_rolled_back(lay)
        calls = lay["calls"]
        first_move = next(i for i, c in enumerate(calls) if c.startswith("MV "))
        for unit in ("vesselstack-chat.service", "vesselstack-alerts.timer", "vesselstack-control-panel.service"):
            self.assertIn(f"systemctl stop {unit}", calls[:first_move], unit)
        self.assertLess(calls.index("systemctl stop vesselstack-alerts.timer"), calls.index("systemctl stop vesselstack-chat.service"),
                        "timers are stopped before services so a timer cannot start its service again")
        self.assertLess(calls.index(f"controller {lay['journal']}/new-controller stop"), first_move)
        self.assertNotIn("systemctl stop vesselstack-firewall.service", calls, "only units that were active are stopped")

    def test_the_live_tree_at_root_is_stopped_with_the_journals_controller_and_an_absent_root_with_none(self):
        lay = self.layout("stopped")
        self.reconcile(lay)
        self.assertIn(f"controller {lay['journal']}/controller stop", lay["calls"])
        self.assertNotIn(f"controller {lay['journal']}/new-controller stop", lay["calls"])
        lay = self.layout("root-swap", "swapped", "away", False)
        self.reconcile(lay)
        self.assertEqual(lay["result"].returncode, 0, lay["result"].stderr)
        self.assertFalse([c for c in lay["calls"] if c.startswith("controller ") and c.endswith(" stop")], lay["calls"])

    def test_a_unit_that_cannot_be_stopped_fails_the_rollback_with_nothing_moved(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        lay["active"].write_text("vesselstack-chat.service\nvesselstack-alerts.timer\n")
        self.reconcile(lay, extra="export LOG_RM=1 STUCK_UNITS=vesselstack-chat.service")
        result = lay["result"]
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(phase_of(lay["journal"]), "rollback-failed")
        self.assertIn("vesselstack-chat.service", self.state_text(lay["journal"]))
        self.assertIn("still running", result.stderr); self.assertIn("nothing was moved", result.stderr)
        self.assertEqual((lay["root"] / "marker").read_text(), "new-root"); self.assertEqual((lay["data"] / "marker").read_text(), "new-data")
        self.assertFalse([c for c in lay["calls"] if c.startswith(("MV ", "RM "))], lay["calls"])
        self.assertTrue(lay["stage"].exists())

    # ---- fixes 2, 3 and 5: what must not be running before a tree moves ----

    def containers(self, lay, *lines):
        (lay["base"] / "containers").write_text("".join(line + "\n" for line in lines))

    def moves(self, lay):
        return [c for c in lay["calls"] if c.startswith(("MV ", "RM "))]

    def test_stack_containers_that_can_be_stopped_are_stopped_and_the_rollback_goes_on(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.containers(lay, "c1|vesselstack-grafana|vesselstack|/elsewhere", "c2|other-app|other|/x;%s/influx" % lay["data"],
                        "c3|unrelated|other|/srv/data;%s-extra/x" % lay["data"])
        self.reconcile(lay, extra="export LOG_RM=1")
        self.assert_rolled_back(lay)
        stops = [c for c in lay["calls"] if c.startswith("docker stop")]
        self.assertEqual(len(stops), 1, lay["calls"])
        self.assertIn("c1", stops[0]); self.assertIn("c2", stops[0]); self.assertNotIn("c3", stops[0])
        first_move = next(i for i, c in enumerate(lay["calls"]) if c.startswith("MV "))
        self.assertLess(lay["calls"].index(stops[0]), first_move)

    def test_a_container_that_cannot_be_stopped_fails_the_rollback_with_nothing_moved(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.containers(lay, "c1|vesselstack-grafana|vesselstack|/elsewhere", "c2|mine|other|%s/x y" % lay["root"])
        self.reconcile(lay, extra="export LOG_RM=1 DOCKER_STUCK=c1")
        result = lay["result"]
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(phase_of(lay["journal"]), "rollback-failed")
        self.assertIn("Containers are still running and using the application or data folder: vesselstack-grafana", self.state_text(lay["journal"]))
        self.assertIn("sudo docker stop vesselstack-grafana", self.state_text(lay["journal"]))
        self.assertNotIn("mine", self.state_text(lay["journal"]).split("last_error=")[1], "a stopped container is not named")
        self.assertFalse(self.moves(lay), lay["calls"])
        self.assertEqual((lay["root"] / "marker").read_text(), "new-root")

    def test_an_unrelated_container_is_ignored_and_no_docker_proceeds(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.containers(lay, "c9|unrelated|other|/srv/data;%s.pre-restore-x/y" % lay["data"])
        self.reconcile(lay)
        self.assert_rolled_back(lay)
        self.assertFalse([c for c in lay["calls"] if c.startswith("docker stop")], lay["calls"])
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.reconcile(lay, extra="export DOCKER_DOWN=1")
        self.assert_rolled_back(lay)

    def test_a_docker_that_does_not_answer_while_its_processes_run_fails_closed(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.reconcile(lay, extra="export LOG_RM=1 DOCKER_DOWN=1 DOCKER_PROCS=1")
        self.assertNotEqual(lay["result"].returncode, 0)
        self.assertEqual(phase_of(lay["journal"]), "rollback-failed")
        self.assertIn("could not check for running containers", lay["result"].stderr)
        self.assertFalse(self.moves(lay), lay["calls"])

    def test_the_firewall_and_socketcan_units_are_neither_stopped_nor_counted_as_running(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        lay["active"].write_text("vesselstack-firewall.service\nvesselstack-socketcan.service\nvesselstack-chat.service\n")
        self.reconcile(lay)
        self.assert_rolled_back(lay)
        self.assertIn("systemctl stop vesselstack-chat.service", lay["calls"])
        for unit in ("vesselstack-firewall.service", "vesselstack-socketcan.service"):
            self.assertNotIn(f"systemctl stop {unit}", lay["calls"])
        # they are still active: nothing stopped them
        self.assertIn("vesselstack-firewall.service", lay["active"].read_text())

    def test_a_unit_that_is_restarting_is_stopped_and_blocks_when_it_stays_so(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        (lay["base"] / "active.act").write_text("vesselstack-chat.service\n")
        self.reconcile(lay)
        self.assert_rolled_back(lay)
        self.assertIn("systemctl stop vesselstack-chat.service", lay["calls"])
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.reconcile(lay, extra="export LOG_RM=1 ACTIVATING_UNITS=vesselstack-chat.service")
        self.assertNotEqual(lay["result"].returncode, 0)
        self.assertEqual(phase_of(lay["journal"]), "rollback-failed")
        self.assertIn("systemctl stop vesselstack-chat.service", lay["calls"])
        self.assertIn("still running", lay["result"].stderr); self.assertIn("vesselstack-chat.service", lay["result"].stderr)
        self.assertFalse(self.moves(lay), lay["calls"])

    # ---- fix 4: only the recorded tree is put back ----

    def test_a_pre_restore_directory_with_another_identity_is_refused_and_nothing_is_moved(self):
        for which in ("root", "data"):
            with self.subTest(which):
                lay = self.layout("root-swap", "swapped", "swapped", False)
                pre = Path(f"{lay[which]}.pre-restore-{STAMP}")
                replace_with_foreign_dir(pre)
                self.reconcile(lay, extra="export LOG_RM=1")
                self.assertNotEqual(lay["result"].returncode, 0)
                self.assertEqual(phase_of(lay["journal"]), "rollback-failed")
                self.assertIn("not the previous", lay["result"].stderr)
                self.assertFalse(self.moves(lay), lay["calls"])
                self.assertEqual((lay["root"] / "marker").read_text(), "new-root")
                self.assertEqual((pre / "marker").read_text(), "foreign")

    def test_rolled_back_is_never_recorded_with_a_foreign_tree_in_place(self):
        lay = self.layout("stopped")
        # the live data path holds a different (but ordinary) directory than the recorded original
        replace_with_foreign_dir(lay["data"])
        self.reconcile(lay)
        self.assertNotEqual(lay["result"].returncode, 0)
        self.assertEqual(phase_of(lay["journal"]), "rollback-failed")
        self.assertNotEqual(phase_of(lay["journal"]), "rolled-back")
        self.assertFalse(self.archive(lay, "rolled-back").exists())

    # ---- fix 6: the failed rollback remembers where it started ----

    def test_a_failure_from_data_swap_keeps_the_origin_and_the_retry_does_not_rewrite_system_files(self):
        lay = self.layout("data-swap", "swapped", "none", False)
        lay["active"].write_text("vesselstack-chat.service\n")
        self.reconcile(lay, extra="export STUCK_UNITS=vesselstack-chat.service")
        self.assertNotEqual(lay["result"].returncode, 0)
        self.assertEqual(phase_of(lay["journal"]), "rollback-failed")
        self.assertEqual(state_value(lay["journal"], "rollback_from"), "data-swap")
        # a failure before the phase is rewritten (here: validation) also records it, and never overwrites it
        lay2 = self.layout("data-swap", "swapped", "none", False)
        self.set_state(lay2["journal"], "root=relative/path")
        self.reconcile(lay2)
        self.assertEqual(state_value(lay2["journal"], "rollback_from"), "data-swap")
        # the cause is fixed: the retry rolls back without restoring the system files from the snapshot
        lay["calls"].clear()
        lay["log"].write_text("")
        self.reconcile(lay, extra="restore_restore_externals() { echo EXTERNALS >> '%s'; }" % lay["log"])
        self.assertEqual(lay["result"].returncode, 0, lay["result"].stderr)
        self.assertNotIn("EXTERNALS", lay["calls"])
        self.assertIn("outcome=rolled-back", lay["result"].stdout)

    def test_a_snapshot_is_not_used_when_the_externals_were_never_touched(self):
        for phase, data_step, root_step in (("stopped", "none", "none"), ("data-swap", "swapped", "none"), ("root-swap", "swapped", "swapped")):
            with self.subTest(phase):
                lay = self.layout(phase, data_step, root_step, False)
                empty = r'find "@J@/files" -type f -exec sh -c ": > \"\$1\"" _ {} \;'.replace("@J@", str(lay["journal"]))
                self.reconcile(lay, extra=empty)
                self.assert_rolled_back(lay)      # the live system files are still the old ones: nothing overwrote them
                self.assertEqual((lay["sbin"] / "vesselstackctl").read_text(), "old-ctl")

    def test_a_damaged_snapshot_in_phase_externals_fails_closed_and_changes_nothing(self):
        for damage in ("empty", "missing", "unlisted"):
            with self.subTest(damage):
                lay = self.layout("externals", "swapped", "swapped", True)
                snap = lay["journal"] / "files" / str(lay["sbin"]).lstrip("/") / "vesselstackctl"
                unit = lay["journal"] / "files" / str(lay["systemd"]).lstrip("/") / "vesselstack-chat.service"
                extra = {"empty": f': > "{snap}"', "missing": f'rm -f "{unit}"',
                         "unlisted": f'printf "present\\t/etc/passwd\\n" >> "{lay["journal"]}/manifest"'}[damage]
                self.reconcile(lay, extra="export LOG_RM=1\n" + extra)
                result = lay["result"]
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(phase_of(lay["journal"]), "rollback-failed")
                named = {"empty": str(lay["sbin"] / "vesselstackctl"), "missing": str(lay["systemd"] / "vesselstack-chat.service"), "unlisted": "/etc/passwd"}[damage]
                self.assertIn(named, result.stderr)
                self.assertIn(named, self.state_text(lay["journal"]))
                self.assertEqual((lay["sbin"] / "vesselstackctl").read_text(), "new-ctl", "a system file was changed")
                self.assertEqual((lay["root"] / "marker").read_text(), "new-root", "a tree was moved")
                self.assertEqual((lay["data"] / "marker").read_text(), "new-data")
                self.assertFalse([c for c in lay["calls"] if c.startswith(("MV ", "controller ", "systemctl stop"))], lay["calls"])

    def test_an_empty_update_env_snapshot_is_not_damage(self):
        lay = self.layout("externals", "swapped", "swapped", True)
        env = lay["base"] / "etc/vesselstack/update.env"; env.parent.mkdir(parents=True); env.write_text("")
        self.reconcile(lay, extra=f'echo "KEY=1" > "{env}"; : > "@J@/files/{str(env).lstrip("/")}"'.replace("@J@", str(lay["journal"])))
        self.assertEqual(lay["result"].returncode, 0, lay["result"].stderr)

    def test_each_system_file_is_replaced_by_a_rename(self):
        lay = self.layout("externals", "swapped", "swapped", True)
        self.reconcile(lay, extra="export LOG_RM=1")
        self.assert_rolled_back(lay)
        renames = [c for c in lay["calls"] if c.startswith("MV -f -- ")]
        self.assertTrue(any(c.endswith("/vesselstackctl") for c in renames), renames)
        self.assertFalse([c for c in lay["calls"] if c.startswith("RM ") and c.endswith("/vesselstackctl")], "no remove-then-copy")

    # ---- fix 1: a journal that is written is a journal the recovery accepts ----

    def journal_for_layout(self, root_raw, data_raw, mkdirs=True, extra=""):
        """Normalise like restore_stack, refuse like restore_stack, else build the real journal and validate it."""
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup); base = Path(td.name); self.last_base = base
        (base / "usr/local/libexec/vesselstack").mkdir(parents=True)
        shutil.copy(JOURNAL_HELPER, base / "usr/local/libexec/vesselstack/restore_journal.py")
        (base / "opt/vesselstack/common").mkdir(parents=True); (base / "opt/vesselstack/common/lifecycle_lock.py").write_text("")
        root_raw = root_raw.replace("@B@", str(base)); data_raw = data_raw.replace("@B@", str(base))
        body = create_journal_source() + "\n" + transaction_source() + """
RESTORE_DIR="$SYSTEM_ROOT/var/lib/vesselstack/restore"; RESTORE_CURRENT="$RESTORE_DIR/current"
mv() { __mv "$@"; }
root=$(restore_normalize_path "@RAWROOT@"); data=$(restore_normalize_path "@RAWDATA@")
if ! restore_layout_ok "$root" "$data"; then echo "REFUSED $restore_layout_error"; exit 0; fi
stage_parent=${data%/*}; [ -n "$stage_parent" ] || stage_parent=/
stage=$(mktemp -d "${stage_parent%/}/.vesselstack-restore.XXXXXX")
mkdir -p "$stage/vesselstack-backup/${root#/}/scripts" "$stage/vesselstack-backup/${data#/}"
echo '#!/bin/bash' > "$stage/vesselstack-backup/${root#/}/scripts/vesselstackctl"
@EXTRA@
restore_create_journal "$stage" "$root" "$data" "$STAMPV" /backups/x.tar.gz 0 || { echo "CREATE-FAILED pending=$restore_journal_pending"; exit 0; }
restore_state_load "$RESTORE_CURRENT" || { echo "LOAD-FAILED"; exit 0; }
if restore_validate_state; then echo "VALID"; else echo "INVALID $restore_validate_error"; fi
""".replace("@RAWROOT@", root_raw).replace("@RAWDATA@", data_raw).replace("$STAMPV", STAMP).replace("@EXTRA@", extra)
        if mkdirs:
            for raw in (root_raw, data_raw):
                Path(raw.rstrip("/").replace("//", "/")).mkdir(parents=True, exist_ok=True)
        # the helper and the lock module are looked up under the harness's ROOT and LIBEXEC_DIR
        result = self.run_harness(body, base)
        return result

    def test_the_journal_written_for_each_layout_passes_the_recovery_validator(self):
        layouts = [
            ("normal", "@B@/opt/vesselstack", "@B@/opt/vesselstack-data"),
            ("trailing slash on DATA", "@B@/opt/vesselstack", "@B@/opt/vesselstack-data/"),
            ("trailing slash on ROOT", "@B@/opt/vesselstack/", "@B@/opt/vesselstack-data"),
            ("double slash", "@B@//opt/vesselstack", "@B@/opt//vesselstack-data//"),
            ("DATA directly under the system root", "@B@/opt/vesselstack", "@B@/vsdata"),
            ("spaces", "@B@/opt/vessel stack", "@B@/opt/vessel data"),
        ]
        for name, root, data in layouts:
            with self.subTest(name):
                result = self.journal_for_layout(root, data)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip().splitlines()[-1], "VALID", result.stdout + result.stderr)

    def test_a_new_journal_removes_the_record_of_an_earlier_automatic_recovery(self):
        extra = ('mkdir -p "$RESTORE_DIR"; printf "journal_id=old\\nresult=rolled-back\\n" > "$RESTORE_DIR/boot-recovery"; '
                 'echo OLD-LOG > "$RESTORE_DIR/boot-recovery.log"')
        result = self.journal_for_layout("@B@/opt/vesselstack", "@B@/opt/vesselstack-data", extra=extra)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "VALID", result.stdout + result.stderr)
        restore_dir = self.last_base / "var/lib/vesselstack/restore"
        self.assertEqual(phase_of(restore_dir / "current"), "prepared")
        self.assertFalse((restore_dir / "boot-recovery").exists(), "the record of the earlier restore is gone")
        self.assertEqual((restore_dir / "boot-recovery.log").read_text(), "OLD-LOG\n", "the log is kept")

    def test_unusual_root_data_layouts_are_refused_up_front(self):
        for name, root, data in (
            ("data inside root", "@B@/opt/vs", "@B@/opt/vs/data"),
            ("root inside data", "@B@/opt/vs/app", "@B@/opt/vs"),
            ("same folder", "@B@/opt/vs", "@B@/opt/vs/"),
            ("root is /", "/", "@B@/opt/vs"),
            ("relative", "opt/vs", "@B@/opt/vs-data"),
        ):
            with self.subTest(name):
                result = self.journal_for_layout(root, data, mkdirs=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.startswith("REFUSED "), result.stdout + result.stderr)

    def test_the_validator_accepts_data_directly_under_the_real_root_directory(self):
        body = transaction_source() + """
SYSTEM_ROOT=""
st_format=1 st_keys=$'\nsystem_root\n' st_system_root="" st_root=/opt/vs st_data=/vsdata st_stamp=@STAMP@
st_stage=/.vesselstack-restore.AbC123 st_root_live=1:2 st_data_live=1:3 st_root_new=1:4 st_data_new=1:5
restore_validate_state && echo VALID || echo "INVALID $restore_validate_error"
""".replace("@STAMP@", STAMP)
        result = self.run_harness(body, Path(tempfile.gettempdir()))
        self.assertEqual(result.stdout.strip(), "VALID", result.stdout + result.stderr)

    def test_every_validator_rule_fails_closed_before_any_move_or_removal(self):
        base_ns = {}
        cases = [
            ("bad format", lambda l: ("format=2",)),
            ("relative root", lambda l: ("root=opt/vesselstack",)),
            ("root is /", lambda l: ("root=/",)),
            ("dot-dot in root", lambda l: (f"root={l['base']}/opt/../opt/vesselstack",)),
            ("double slash in data", lambda l: (f"data={l['base']}//opt/vesselstack-data",)),
            ("trailing slash", lambda l: (f"data={l['data']}/",)),
            ("root and data equal", lambda l: (f"data={l['root']}",)),
            ("data inside root", lambda l: (f"data={l['root']}/data",)),
            ("system root differs", lambda l: ("system_root=/elsewhere",)),
            ("bad stamp", lambda l: ("stamp=S",)),
            ("stamp with a path", lambda l: ("stamp=20260102T030405Z-1-1/../x",)),
            ("dot-dot in stage", lambda l: (f"stage={l['base']}/opt/../opt/{STAGE_NAME}",)),
            ("stage in another directory", lambda l: (f"stage={l['base']}/{STAGE_NAME}",)),
            ("stage with a wrong name", lambda l: (f"stage={l['base']}/opt/.vesselstack-restore.x",)),
            ("stage that is not a staging name", lambda l: (f"stage={l['base']}/opt/vesselstack",)),
            ("bad inode value", lambda l: ("root_new=abc",)),
            ("empty inode value", lambda l: ("data_live=",)),
        ]
        for name, change in cases:
            with self.subTest(name):
                lay = self.layout("root-swap", "swapped", "swapped", False)
                self.set_state(lay["journal"], *change(lay))
                self.reconcile(lay, extra="export LOG_RM=1")
                result = lay["result"]
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn("outcome=rollback-failed", result.stdout)
                self.assertEqual(phase_of(lay["journal"]), "rollback-failed")
                self.assertEqual((lay["root"] / "marker").read_text(), "new-root"); self.assertEqual((lay["data"] / "marker").read_text(), "new-data")
                self.assertTrue(lay["stage"].exists())
                self.assertFalse([c for c in lay["calls"] if c.startswith(("RM ", "MV ", "controller ", "systemctl")) and not c.startswith("systemctl is-")], lay["calls"])

    def test_a_stage_that_is_a_symlink_is_not_removed(self):
        lay = self.layout("committed", "swapped", "swapped", False)
        target = lay["base"] / "precious"; target.mkdir(); (target / "keep").write_text("x")
        shutil.rmtree(lay["stage"]); lay["stage"].symlink_to(target, target_is_directory=True)
        self.reconcile(lay, extra="export LOG_RM=1")
        self.assertNotEqual(lay["result"].returncode, 0)
        self.assertTrue((target / "keep").exists())
        self.assertFalse([c for c in lay["calls"] if c.startswith("RM ")], lay["calls"])
        self.assertEqual(phase_of(lay["journal"]), "committed", "a finished restore is never turned into a failed rollback")

    def test_a_repeated_key_in_the_state_fails_closed(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        with open(lay["journal"] / "state", "a") as handle: handle.write("phase=prepared\n")
        self.reconcile(lay, extra="export LOG_RM=1")
        self.assertNotEqual(lay["result"].returncode, 0)
        self.assertIn("more than once", lay["result"].stderr)
        self.assertEqual((lay["root"] / "marker").read_text(), "new-root")
        self.assertFalse([c for c in lay["calls"] if c.startswith(("RM ", "MV ", "controller ", "systemctl")) and not c.startswith("systemctl is-")], lay["calls"])
        result = self.status_harness(lay)
        self.assertNotEqual(result.returncode, 0); self.assertIn("cannot be read", result.stderr)

    def test_the_shell_peek_and_the_helper_agree_on_a_repeated_key(self):
        lay = self.layout("committed", "swapped", "swapped", True)
        with open(lay["journal"] / "state", "a") as handle: handle.write("phase=prepared\n")
        body = pending_source() + f'SYSTEM_ROOT="{lay["base"]}"; RESTORE_CURRENT="{lay["base"]}/restore/current"\nrestore_pending_check; echo passed\n'
        result = self.run_harness(body, lay["base"])
        self.assertEqual(result.returncode, 1); self.assertNotIn("passed", result.stdout)
        self.assertIn("cannot be read", result.stderr)

    def test_a_different_recorded_device_number_still_recognises_the_trees(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        changes = []
        for key in ("root_live", "data_live", "root_new", "data_new"):
            changes.append(f"{key}=4242:{state_value(lay['journal'], key).split(':')[1]}")
        self.set_state(lay["journal"], *changes)
        self.reconcile(lay)
        self.assert_rolled_back(lay)

    @staticmethod
    def state_value(journal, key):
        for line in (journal / "state").read_text().splitlines():
            if line.startswith(key + "="): return line[len(key) + 1:]

    def test_a_pre_restore_tree_that_is_a_symlink_is_refused_before_any_move(self):
        for which in ("root", "data"):
            with self.subTest(which):
                lay = self.layout("root-swap", "swapped", "swapped", False)
                pre = Path(f"{lay[which]}.pre-restore-{STAMP}")
                moved = Path(f"{pre}.real"); os.rename(pre, moved); pre.symlink_to(moved, target_is_directory=True)
                lay["active"].write_text("vesselstack-chat.service\n")
                self.reconcile(lay, extra="export LOG_RM=1")
                result = lay["result"]
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("is a symbolic link", result.stderr)
                self.assertEqual((lay["root"] / "marker").read_text(), "new-root"); self.assertEqual((lay["data"] / "marker").read_text(), "new-data")
                self.assertFalse([c for c in lay["calls"] if c.startswith(("MV ", "RM ", "controller ", "systemctl stop"))], lay["calls"])
                self.assertEqual(phase_of(lay["journal"]), "rollback-failed")

    def assert_two_ways_out(self, err, pre):
        self.assertIn("Two ways out", err)
        self.assertIn("MOVE", err); self.assertIn(f"{pre}", err)
        self.assertIn("recover rollback", err)
        self.assertIn(".manual-", err)
        self.assertIn("sudo vesselstackctl restore /path/to/vesselstack-<time>.tar.gz --yes", err)
        self.assertNotIn("restore ARCHIVE", err)
        self.assertIn("xargs -r sudo systemctl stop", err); self.assertIn("xargs -r sudo docker stop", err)
        self.assertIn("--all --plain --no-legend", err); self.assertNotIn("--state=active", err)
        self.assertIn("shows inactive or failed", err); self.assertIn("repeat the stop", err)
        self.assertIn("sudo docker ps --no-trunc --format '{{.Names}} {{.Mounts}}'", err)
        self.assertIn("/usr/local/sbin/vesselstackctl may belong to the restored release", err)
        self.assertIn("a folder that contains them (such as / or /opt). List the containers", err)
        self.assertNotIn("read-write", err); self.assertNotIn("read-only", err)
        self.assertIn("go on only when no such container is left", err)
        self.assertIn("is expected and harmless", err)
        self.assertIn("sudo mv -T ", err); self.assertIn("(if it says the target exists, use another name)", err)
        self.assertIn(f".failed-restore-{STAMP} folder does not exist either, stop and get help.\n      Otherwise, restore again from a backup, which replaces both folders together:", err)
        self.assertNotIn("To resolve this by hand", err)

    def assert_way_b_folders(self, err, lay):
        for which in ("root", "data"):
            folder = lay[which]
            self.assertIn(f"sudo mv -T {folder}.failed-restore-{STAMP} {folder}   (only if {folder} is missing)", err)
        self.assertIn("If the application folder or the data folder is missing at its", err)

    def test_a_missing_pre_restore_tree_is_refused_before_anything_moves_and_every_retry_says_the_same(self):
        for which in ("root", "data"):
            with self.subTest(which):
                lay = self.layout("root-swap", "swapped", "swapped", False)
                pre = Path(f"{lay[which]}.pre-restore-{STAMP}")
                shutil.rmtree(pre)
                for attempt in (1, 2):
                    self.reconcile(lay, extra="export LOG_RM=1")
                    result = lay["result"]
                    self.assertNotEqual(result.returncode, 0, attempt)
                    self.assertEqual(phase_of(lay["journal"]), "rollback-failed")
                    self.assert_two_ways_out(result.stderr, pre)
                    self.assert_way_b_folders(result.stderr, lay)
                    self.assertIn("does not exist", result.stderr); self.assertIn("Nothing was moved".lower(), result.stderr.lower())
                    self.assertFalse(self.moves(lay), lay["calls"])
                    for tree in ("root", "data"):
                        self.assertEqual(inode(lay[tree]), lay["new"][tree], tree)
                        self.assertEqual((lay[tree] / "marker").read_text(), f"new-{tree}")
                    self.assertEqual(sorted(p.name for p in lay["base"].glob("opt/*failed-restore*")), [])

    def test_a_missing_pre_restore_tree_with_the_folder_itself_absent_is_refused_with_the_two_ways_out(self):
        for which in ("root", "data"):
            with self.subTest(which):
                lay = self.layout("root-swap", "swapped", "swapped", False)
                pre = Path(f"{lay[which]}.pre-restore-{STAMP}")
                shutil.rmtree(pre); shutil.rmtree(lay[which])
                self.reconcile(lay, extra="export LOG_RM=1")
                result = lay["result"]
                self.assertNotEqual(result.returncode, 0)
                self.assert_two_ways_out(result.stderr, pre)
                self.assert_way_b_folders(result.stderr, lay)
                self.assertFalse(self.moves(lay), lay["calls"])
                self.assertFalse(lay[which].exists())
                self.assertEqual(sorted(p.name for p in lay["base"].glob("opt/*failed-restore*")), [])

    def test_a_foreign_or_symlinked_pre_restore_tree_gets_the_two_ways_out_and_nothing_moves(self):
        for kind in ("foreign", "symlink"):
            for which in ("root", "data"):
                with self.subTest(kind=kind, which=which):
                    lay = self.layout("root-swap", "swapped", "swapped", False)
                    pre = Path(f"{lay[which]}.pre-restore-{STAMP}")
                    if kind == "foreign":
                        replace_with_foreign_dir(pre)
                    else:
                        moved = Path(f"{pre}.real"); os.rename(pre, moved); pre.symlink_to(moved, target_is_directory=True)
                    self.reconcile(lay, extra="export LOG_RM=1")
                    result = lay["result"]
                    self.assertNotEqual(result.returncode, 0)
                    self.assert_two_ways_out(result.stderr, pre)
                    if kind == "symlink":
                        self.assertIn("Replace the link", result.stderr); self.assertIn("sudo mv", result.stderr)
                    self.assertFalse(self.moves(lay), lay["calls"])
                    for tree in ("root", "data"):
                        self.assertEqual(inode(lay[tree]), lay["new"][tree], tree)
                    self.assertEqual(sorted(p.name for p in lay["base"].glob("opt/*failed-restore*")), [])

    def test_a_crash_with_the_folder_absent_and_the_previous_one_set_aside_still_rolls_back(self):
        lay = self.layout("root-swap", "away", "away", False)
        self.reconcile(lay)
        self.assert_rolled_back(lay)

    def test_a_tree_on_another_filesystem_than_its_parent_is_refused(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        # a stub of the helper's same-fs check stands in for a mount point
        self.reconcile(lay, extra='python3() { if [ "${3:-}" = inspect ] && [ "${4:-}" = "@ROOT@" ]; then echo "1:1 different"; else command python3 "$@"; fi; }'.replace("@ROOT@", str(lay["root"])))
        self.assertNotEqual(lay["result"].returncode, 0)
        self.assertIn("not on the same filesystem", lay["result"].stderr)
        self.assertEqual((lay["root"] / "marker").read_text(), "new-root")

    def test_a_symlinked_ancestor_on_the_same_filesystem_is_restored_and_rolled_back(self):
        # /opt -> a real directory elsewhere on the same disk: forward checks and the recovery must agree.
        lay = self.layout("root-swap", "swapped", "swapped", False)
        base = lay["base"]
        os.rename(base / "opt", base / "opt-real"); os.symlink(base / "opt-real", base / "opt", target_is_directory=True)
        lay["real_rename"] = True
        for label in ("root", "data"):
            result = self.run_harness(transaction_source() + f'\nrestore_fs_check "{lay["journal"]}/restore_journal.py" x "{lay[label]}" && echo OK\n', base)
            self.assertEqual(result.stdout.strip(), "OK", result.stderr)
        self.reconcile(lay)
        self.assert_rolled_back(lay)

    def test_a_layout_the_recovery_would_refuse_is_refused_with_a_plain_message(self):
        helper = self.layout("stopped")["journal"] / "restore_journal.py"
        fake = helper.parent / "fake_helper.py"
        fake.write_text("import sys\nprint('1:2 different' if sys.argv[2] == '/mnt/data' else '1:2 same')\n")
        body = transaction_source() + f'''
restore_fs_check "{fake}" data /mnt/data && echo "BAD data accepted"
echo "error=$restore_fs_error"
restore_fs_check "{fake}" application /opt/app && echo "application accepted"
'''
        result = self.run_harness(body, Path(tempfile.gettempdir()))
        self.assertIn("error=the data folder /mnt/data is not on the same filesystem as its parent directory", result.stdout)
        self.assertNotIn("BAD", result.stdout)
        self.assertIn("application accepted", result.stdout)

    def test_the_filesystem_rules_run_before_the_journal_exists(self):
        text = CONTROLLER.read_text()
        stack = text[text.index("\nrestore_stack() {"):text.index("# ---------------------------------------------------------------- backup drive")]
        create = stack.index('restore_create_journal "$stage"')
        for needle in ('restore_fs_check "$LIBEXEC_DIR/restore_journal.py"', 'restore_device "$stage"', 'restore_device "${path%/*}/"'):
            self.assertIn(needle, stack)
            self.assertLess(stack.index(needle), create, needle)
        self.assertNotIn("mv -T", text[text.index("\nrestore_mv_tree() {"):text.index("\nrestore_enter() {")], "no copy fallback")
        self.assertIn("restore_journal.py\" rename", text[text.index("\nrestore_rename_tree() {"):text.index("\nrestore_mv_tree() {")])

    def test_the_restore_renames_through_the_helper_and_a_cross_device_rename_fails(self):
        lay = self.layout("stopped")
        src = lay["base"] / "src"; src.mkdir(); dst = lay["base"] / "dst"
        body = transaction_source() + f'''
python3() {{ if [ "${{3:-}}" = rename ]; then echo "restore_journal: [Errno 18] Invalid cross-device link: '$4'" >&2; return 1; fi; command python3 "$@"; }}
restore_mv_tree "{lay["journal"]}" "{src}" "{dst}" && echo MOVED || echo FAILED
'''
        result = self.run_harness(body, lay["base"], real_rename=True)
        self.assertEqual(result.stdout.strip(), "FAILED", result.stderr)
        self.assertTrue(src.is_dir()); self.assertFalse(dst.exists())
        body = transaction_source() + f'\nrestore_mv_tree "{lay["journal"]}" "{src}" "{dst}" && echo MOVED || echo FAILED\n'
        result = self.run_harness(body, lay["base"], real_rename=True)
        self.assertEqual(result.stdout.strip(), "MOVED", result.stderr)
        self.assertTrue(dst.is_dir()); self.assertFalse(src.exists())

    def test_a_journal_that_fails_after_the_rename_is_removed_or_reported_as_pending(self):
        fail_dir_fsync = 'python3() { if [ "${3:-}" = fsync-dir ]; then return 1; fi; command python3 "$@"; }'
        result = self.journal_for_layout("@B@/opt/vesselstack", "@B@/opt/vesselstack-data", extra=fail_dir_fsync)
        self.assertIn("CREATE-FAILED pending=0", result.stdout, result.stdout + result.stderr)
        self.assertFalse(list(self.last_base.glob("var/lib/vesselstack/restore/current")), "the journal was removed")
        result = self.journal_for_layout("@B@/opt/vesselstack", "@B@/opt/vesselstack-data", extra=fail_dir_fsync + '\nrm() { return 1; }')
        self.assertIn("CREATE-FAILED pending=1", result.stdout, result.stdout + result.stderr)
        self.assertTrue((self.last_base / "var/lib/vesselstack/restore/current").exists(), "a journal that is pending stays")

    def test_the_journal_is_validated_while_it_is_still_being_built(self):
        text = CONTROLLER.read_text()
        create = text[text.index("\nrestore_create_journal() {"):text.index("\nrestore_stack() {")]
        self.assertLess(create.index('restore_state_load "$building"'), create.index('mv -T -- "$building" "$RESTORE_CURRENT"'))
        self.assertLess(create.index("restore_validate_state"), create.index('mv -T -- "$building" "$RESTORE_CURRENT"'))

    def test_the_rollback_goes_on_when_the_journal_cannot_be_written(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.reconcile(lay, extra='restore_state_set() { echo "restore_journal: [Errno 28] No space left on device" >&2; return 1; }')
        result = lay["result"]
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("outcome=rolled-back", result.stdout)
        self.assertEqual(inode(lay["root"]), lay["original"]["root"]); self.assertEqual(inode(lay["data"]), lay["original"]["data"])
        self.assertIn("could not be updated", result.stderr); self.assertIn(str(self.archive(lay, "rolled-back")), result.stderr)
        self.assertEqual(phase_of(self.archive(lay, "rolled-back")), "root-swap", "the journal itself could not be updated")
        self.assertIn("systemctl daemon-reload", lay["calls"])

    def test_a_committed_or_rolled_back_journal_does_not_block_other_commands(self):
        for phase in ("committed", "rolled-back"):
            with self.subTest(phase):
                lay = self.layout(phase, "swapped", "swapped", True)
                body = pending_source() + f'SYSTEM_ROOT="{lay["base"]}"; RESTORE_CURRENT="{lay["base"]}/restore/current"\nrestore_pending_check; echo passed\n'
                result = self.run_harness(body, lay["base"])
                self.assertEqual(result.returncode, 0, result.stderr); self.assertIn("passed", result.stdout)
                doctor = pending_source() + controller_function_source("check_restore_state() {", "status_unit() {") + f"""
SYSTEM_ROOT="{lay['base']}"; RESTORE_CURRENT="{lay['base']}/restore/current"; RESTORE_DIR="{lay['base']}/restore"
if check_restore_state; then echo state-ok; else echo state-failed; fi
restore_status_line && echo status-ok || echo status-blocked
"""
                result = self.run_harness(doctor, lay["base"])
                self.assertIn("state-ok", result.stdout); self.assertIn("status-ok", result.stdout)
                self.assertIn("restore cleanup pending", result.stderr)
                self.assertIn("cleanup pending", result.stdout)

    # ---- recovery at boot (scripts/lib/restore-boot) ----
    OTHER_BOOT_ID = "f0e1d2c3-b4a5-4968-8776-655443322110"

    def real_recover_wrapper(self):
        # The journal's own `recover`, exactly as the controller writes it.
        text = CONTROLLER.read_text()
        start = "cat > \"$building/recover\" <<'RECOVER'\n"
        a = text.index(start) + len(start)
        return text[a:text.index("\nRECOVER\n", a) + 1]

    def boot_ready(self, **layout_args):
        """A journal from the harness whose `recover` is the real wrapper and whose controller is the harness."""
        lay = self.layout(*layout_args.pop("args"), **layout_args)
        self.reconcile(lay, via_recover=True)
        recover = lay["journal"] / "recover"
        recover.write_text(self.real_recover_wrapper()); recover.chmod(0o700)
        return lay

    def run_restore_boot(self, lay, boot_id):
        seam = lay["base"] / "boot_id.now"; seam.write_text(boot_id + "\n")
        # The harness keeps its journals in <base>/restore; the boot script looks under var/lib/vesselstack/restore.
        link = lay["base"] / "var/lib/vesselstack/restore"
        if not link.exists():
            link.parent.mkdir(parents=True, exist_ok=True); link.symlink_to(lay["base"] / "restore")
        env = dict(os.environ, SYSTEM_ROOT=str(lay["base"]), VESSELSTACK_BOOT_ID_FILE=str(seam), RESTORE_BOOT_BUSY_WAIT="0", RESTORE_BOOT_TIMEOUT="120")
        return subprocess.run(["/bin/bash", str(BOOT_SCRIPT)], env=env, capture_output=True, text=True, timeout=300)

    def test_the_boot_script_drives_the_real_recover_and_the_folders_come_back(self):
        for name, phase, data_step, root_step, published in self.CRASH_POINTS:
            with self.subTest(name):
                lay = self.boot_ready(args=(phase, data_step, root_step, published))
                self.assertEqual(state_value(lay["journal"], "boot_id"), BOOT_ID)
                result = self.run_restore_boot(lay, self.OTHER_BOOT_ID)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                record = (lay["base"] / "restore" / "current").exists()
                self.assertFalse(record, "the journal is archived")
                self.assertEqual(phase_of(self.archive(lay, "rolled-back")), "rolled-back")
                self.assertEqual(inode(lay["root"]), lay["original"]["root"]); self.assertEqual(inode(lay["data"]), lay["original"]["data"])
                self.assertEqual((lay["root"] / "marker").read_text(), "old-root"); self.assertEqual((lay["data"] / "marker").read_text(), "old-data")
                values = dict(line.partition("=")[::2] for line in (lay["base"] / "restore" / "boot-recovery").read_text().splitlines())
                self.assertEqual((values["result"], values["phase_after"], values["phase_before"], values["attempts"], values["rc"]),
                                 ("rolled-back", "archived", phase, "1", "0"))
                self.assertIn("Rollback completed", (lay["base"] / "restore" / "boot-recovery.log").read_text())

    def test_the_boot_script_moves_nothing_when_the_journal_is_from_this_boot(self):
        lay = self.boot_ready(args=("root-swap", "swapped", "swapped", False))
        result = self.run_restore_boot(lay, BOOT_ID)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(lay["journal"].exists()); self.assertEqual(phase_of(lay["journal"]), "root-swap")
        self.assertEqual(inode(lay["root"]), lay["new"]["root"]); self.assertEqual(inode(lay["data"]), lay["new"]["data"])
        self.assertFalse((lay["base"] / "restore" / "boot-recovery").exists())

    def test_a_rollback_leaves_the_boot_recovery_unit_script_and_setting_alone(self):
        lay = self.layout("externals", "swapped", "swapped", True)
        planted = {lay["systemd"] / "boot-vesselstack-restore.service": "unit",
                   lay["base"] / "usr/local/lib/vesselstack-recovery/restore-boot": "script",
                   lay["base"] / "etc/vesselstack-recovery.conf": "MODE=hold\n",
                   lay["systemd"] / "multi-user.target.wants/boot-vesselstack-restore.service": None}
        for path, text in planted.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            if text is None: path.symlink_to("../boot-vesselstack-restore.service")
            else: path.write_text(text)
        self.reconcile(lay)
        self.assert_rolled_back(lay)
        for path, text in planted.items():
            if text is None: self.assertTrue(path.is_symlink(), path)
            else: self.assertEqual(path.read_text(), text, path)
        self.assertFalse((lay["systemd"] / "vesselstack-extra.service").exists(), "the rollback did remove what the restore published")

    # ---- recover abandon ----
    def recover_harness(self, lay, arguments, env=""):
        body = (f"export {env}\n" if env else "") + pending_source() + transaction_source() + f"""
usage() {{ echo usage; }}
SYSTEM_ROOT="{lay['base']}"
RESTORE_CURRENT="{lay['base']}/restore/current"
ROOT="/nonexistent/vesselstack"
restore_take_lock() {{ :; }}
mv() {{ __mv "$@"; }}
systemctl() {{
    echo "systemctl $*" >> "{lay['base']}/calls.log"
    case "$1" in is-active) return 1 ;; start|restart) for u in "${{@:2}}"; do case " ${{FAIL_UNIT_START:-}} " in *" $u "*) return 1 ;; esac; done ;; esac
    return 0
}}
restore_recover_command {arguments}
"""
        return self.run_harness(body, lay["base"])

    def test_abandon_is_allowed_only_when_both_original_trees_are_in_place(self):
        # allowed: nothing was moved (the stack was merely stopped)
        lay = self.layout("stopped")
        status = self.recover_harness(lay, "status")
        self.assertIn(f"{lay['journal']}/recover abandon --yes", status.stdout)
        dry = self.recover_harness(lay, "abandon")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("would archive the journal", dry.stdout); self.assertIn("would NOT start the stack", dry.stdout)
        self.assertTrue(lay["journal"].exists() and lay["stage"].exists(), "the dry run changes nothing")
        done = self.recover_harness(lay, "abandon --yes")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Not done: the system files", done.stdout); self.assertIn("the stack was not started", done.stdout)
        self.assertIn("sudo vesselstackctl doctor", done.stdout); self.assertIn("sudo vesselstackctl start", done.stdout)
        self.assertFalse(lay["journal"].exists()); self.assertFalse(lay["stage"].exists())
        archived = self.archive(lay, "abandoned")
        self.assertEqual(phase_of(archived), "abandoned"); self.assertFalse((archived / "controller").exists())
        self.assertEqual(inode(lay["root"]), lay["original"]["root"]); self.assertEqual(inode(lay["data"]), lay["original"]["data"])
        self.assertEqual(self.recover_harness(lay, "status").stdout.strip(), "No restore is pending.")

    def test_abandon_refuses_when_a_tree_is_not_the_original(self):
        for name, data_step, root_step, expected in (("both restored", "swapped", "swapped", "application folder"),
                                                     ("data restored only", "swapped", "none", "data folder"),
                                                     ("root missing", "none", "away", "is missing")):
            with self.subTest(name):
                lay = self.layout("root-swap", data_step, root_step, False)
                status = self.recover_harness(lay, "status")
                self.assertNotIn("abandon", status.stdout)
                for arguments in ("abandon", "abandon --yes"):
                    refused = self.recover_harness(lay, arguments)
                    self.assertEqual(refused.returncode, 1, arguments)
                    self.assertIn("Refusing to abandon the restore", refused.stderr); self.assertIn(expected, refused.stderr)
                self.assertTrue(lay["journal"].exists() and lay["stage"].exists())

    def test_abandon_rejects_unknown_arguments_and_keeps_the_newest_three_records(self):
        lay = self.layout("stopped")
        self.assertEqual(self.recover_harness(lay, "abandon --force").returncode, 2)
        for n in range(1, 5): (lay["base"] / "restore" / f"abandoned-2020010{n}T000000Z-1-1").mkdir()
        self.assertEqual(self.recover_harness(lay, "abandon --yes").returncode, 0)
        self.assertEqual(len(list((lay["base"] / "restore").glob("abandoned-*"))), 3)

    def test_abandon_restarts_quiesced_timers_and_lists_what_it_leaves_behind(self):
        lay = self.layout("stopped", quiesced=("vesselstack-backup.timer", "vesselstack-alerts.timer"))
        left = Path(f"{lay['root']}.pre-restore-{STAMP}"); left.mkdir()
        dry = self.recover_harness(lay, "abandon")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("Directories left behind", dry.stdout); self.assertIn(str(left), dry.stdout); self.assertIn(str(lay["stage"]), dry.stdout)
        self.assertIn("including secrets", dry.stdout); self.assertNotIn("belong to the restored release", dry.stdout)
        done = self.recover_harness(lay, "abandon --yes")
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = lay["log"].read_text()
        self.assertIn("systemctl start vesselstack-backup.timer", calls); self.assertIn("systemctl start vesselstack-alerts.timer", calls)
        self.assertIn("Units started again: vesselstack-backup.timer vesselstack-alerts.timer", done.stdout)
        self.assertIn("Directories left behind", done.stdout); self.assertIn(str(left), done.stdout)
        self.assertNotIn(str(lay["stage"]), done.stdout, "the staged copy was removed")
        self.assertNotIn("RESTORED release", done.stdout)
        self.assertFalse((self.archive(lay, "abandoned") / "files").exists())

    def test_abandon_names_a_timer_that_could_not_be_started(self):
        lay = self.layout("stopped", quiesced=("vesselstack-backup.timer", "vesselstack-alerts.timer"))
        done = self.recover_harness(lay, "abandon --yes", env="FAIL_UNIT_START=vesselstack-alerts.timer")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Units started again: vesselstack-backup.timer", done.stdout)
        self.assertIn("could not be started: vesselstack-alerts.timer", done.stdout)

    def test_abandon_after_system_files_were_published_keeps_the_snapshot_and_warns(self):
        for phase, origin in (("externals", None), ("reloaded", None), ("starting", None), ("rollback-failed", "starting")):
            with self.subTest(phase=phase):
                lay = self.layout(phase)
                (lay["journal"] / "files").mkdir(); (lay["journal"] / "files" / "kept").write_text("snapshot")
                (lay["journal"] / "manifest").write_text("one\n")
                if origin:
                    subprocess.run([sys.executable, "-I", str(lay["journal"] / "restore_journal.py"), "set", str(lay["journal"]),
                                    f"rollback_from={origin}"], check=True)
                (lay["root"] / "installer").mkdir(); (lay["root"] / "installer" / "install.sh").write_text("#!/bin/sh\n")
                dry = self.recover_harness(lay, "abandon")
                self.assertIn("may belong to the restored release", dry.stdout)
                done = self.recover_harness(lay, "abandon --yes")
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn("RESTORED release", done.stdout)
                self.assertIn(f"sudo {lay['root']}/installer/install.sh --config {lay['root']}/config/vesselstack.env --start", done.stdout)
                archived = self.archive(lay, "abandoned")
                self.assertEqual((archived / "files" / "kept").read_text(), "snapshot")
                self.assertEqual((archived / "manifest").read_text(), "one\n")

    def test_abandon_from_a_failed_rollback_with_no_or_an_unknown_origin_counts_the_system_files_as_touched(self):
        for origin in (None, "", "somewhere"):
            with self.subTest(origin=origin):
                lay = self.layout("rollback-failed")
                (lay["journal"] / "files").mkdir(); (lay["journal"] / "files" / "kept").write_text("snapshot")
                (lay["journal"] / "manifest").write_text("one\n")
                if origin is not None:
                    self.set_state(lay["journal"], f"rollback_from={origin}")
                dry = self.recover_harness(lay, "abandon")
                self.assertEqual(dry.returncode, 0, dry.stderr)
                done = self.recover_harness(lay, "abandon --yes")
                self.assertEqual(done.returncode, 0, done.stderr)
                archived = self.archive(lay, "abandoned")
                self.assertEqual((archived / "files" / "kept").read_text(), "snapshot")
                self.assertEqual((archived / "manifest").read_text(), "one\n")
                if origin == "somewhere":
                    self.assertIn("may have begun publishing system files", dry.stdout)
                    self.assertIn("may have begun publishing system files", done.stdout)
                else:
                    self.assertIn("publishing system files", dry.stdout)
                    self.assertIn("publishing system files", done.stdout)

    def test_abandon_without_an_installer_says_to_rerun_it(self):
        lay = self.layout("starting")
        done = self.recover_harness(lay, "abandon --yes")
        self.assertIn("rerun the installer for the installed version", done.stdout)

    def test_a_damaged_journal_gets_the_same_manual_procedure_everywhere(self):
        lay = self.layout("stopped")
        state = lay["journal"] / "state"
        state.write_text(state.read_text() + "phase=stopped\n")  # a repeated key
        for command in ("status", "rollback", "abandon --yes"):
            with self.subTest(command=command):
                result = self.recover_harness(lay, command)
                self.assertEqual(result.returncode, 1)
                self.assertIn("To resolve this by hand:", result.stderr)
                self.assertIn("(not readable from the journal)", result.stderr)
                self.assertIn(f"sudo mv -T {lay['journal']} {lay['journal']}.manual-", result.stderr)
                self.assertIn("sudo vesselstackctl doctor", result.stderr)
        pending = self.run_harness(pending_source() + f'SYSTEM_ROOT="{lay["base"]}"; RESTORE_CURRENT="{lay["base"]}/restore/current"\nrestore_pending_check\n', lay["base"])
        self.assertEqual(pending.returncode, 1); self.assertIn("To resolve this by hand:", pending.stderr)

    def test_install_sh_prints_the_same_manual_procedure_as_vesselstackctl(self):
        self.assertEqual(manual_procedure_block(ROOT / "install.sh", "restore_manual_procedure", "<<EOF", "\nEOF\n"),
                         manual_procedure_block(CONTROLLER, "restore_manual_procedure", "<<EOF", "\nEOF\n"))

    def test_a_journal_the_validator_rejects_gets_the_manual_procedure_with_the_folders(self):
        lay = self.layout("stopped")
        subprocess.run([sys.executable, "-I", str(lay["journal"] / "restore_journal.py"), "set", str(lay["journal"]), "stamp=bogus"], check=True)
        result = self.recover_harness(lay, "status")
        self.assertEqual(result.returncode, 1)
        self.assertIn("To resolve this by hand:", result.stderr)
        self.assertIn(f"application folder {lay['root']} and the data folder {lay['data']}", result.stderr)
        self.assertNotIn("(not readable", result.stderr)

    def test_everything_is_flushed_to_disk_before_the_rename_and_before_the_first_stop(self):
        text = CONTROLLER.read_text()
        def body(name, following):
            a = text.index(f"\n{name}() {{"); return text[a:text.index(f"\n{following}() {{", a)]
        create = body("restore_create_journal", "restore_stack")
        self.assertLess(create.index('restore_journal.py" fsync-tree "$building"'), create.index('mv -T -- "$building" "$RESTORE_CURRENT"'),
                        "the building journal is flushed before it is renamed to current")
        stack = text[text.index("\nrestore_stack() {"):text.index("# ---------------------------------------------------------------- backup drive")]
        flush = stack.index('restore_journal.py" fsync-tree "$stage"')
        self.assertLess(flush, stack.index('restore_create_journal "$stage"'))
        self.assertLess(flush, stack.index('restore_forward "$journal"'), "the staged copy is flushed before anything is stopped")
        self.assertIn("restore_commit_transaction", body("restore_forward", "restore_stack"), "restore_forward is what runs the transaction")
        self.assertIn("this can take several minutes", stack[:flush + 400] + stack[flush - 400:flush])

    # ---- the recover wrapper refuses a damaged journal ----
    def test_recover_wrapper_fails_loudly_for_a_missing_or_empty_part(self):
        text = CONTROLLER.read_text()
        a = text.index("<<'RECOVER'\n") + len("<<'RECOVER'\n"); wrapper = text[a:text.index("\nRECOVER\n", a) + 1]
        for victim in ("controller", "restore_journal.py", "state"):
            for how in ("empty", "missing"):
                with self.subTest(victim=victim, how=how):
                    lay = self.layout("root-swap", "swapped", "away", False)
                    (lay["journal"] / "recover").write_text(wrapper); (lay["journal"] / "recover").chmod(0o700)
                    (lay["journal"] / "controller").write_text('#!/bin/bash\necho ran\n')
                    if how == "empty": (lay["journal"] / victim).write_text("")
                    else: (lay["journal"] / victim).unlink()
                    result = subprocess.run(["bash", str(lay["journal"] / "recover"), "status"], capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(str(lay["journal"]), result.stderr); self.assertIn(victim, result.stderr)

    # ---- fourth review: containers that mount a parent, containers started again, the firewall, no systemd ----

    def test_a_read_write_mount_of_a_parent_is_stopped_and_a_read_only_one_is_ignored(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        base = lay["base"]
        os.symlink(lay["data"], base / "datalink", target_is_directory=True)
        self.containers(lay,
                        "c1|rw-slash|other|/",
                        "c2|ro-slash|other|ro:/",
                        "c3|rw-parent|other|%s/opt" % base,
                        "c4|ro-parent|other|ro:%s/opt" % base,
                        "c5|via-link|other|%s/datalink/sub" % base,
                        "c6|ro-inside|other|ro:%s/x" % lay["data"],
                        "c7|sibling|other|%s/opt/vesselstack-other" % base)
        self.reconcile(lay, extra="export LOG_RM=1")
        self.assert_rolled_back(lay)
        stops = [c for c in lay["calls"] if c.startswith("docker stop")]
        self.assertEqual(len(stops), 1, lay["calls"])
        for cid in ("c1", "c3", "c5", "c6"):
            self.assertIn(cid, stops[0].split(), cid)
        for cid in ("c2", "c4", "c7"):
            self.assertNotIn(cid, stops[0].split(), cid)

    def test_a_symlinked_parent_does_not_hide_a_mount_of_it(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        base = lay["base"]
        os.symlink(base / "opt", base / "optlink", target_is_directory=True)
        self.containers(lay, "c1|parent-by-link|other|%s/optlink" % base)
        self.reconcile(lay)
        self.assert_rolled_back(lay)
        self.assertTrue([c for c in lay["calls"] if c.startswith("docker stop") and "c1" in c.split()], lay["calls"])

    def test_a_stopped_container_outside_the_stack_is_started_again_after_a_rollback_and_the_stack_one_is_not(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.containers(lay, "c1|vesselstack-grafana|vesselstack|/elsewhere", "c2|other-app|other|%s/influx" % lay["data"])
        self.reconcile(lay)
        self.assert_rolled_back(lay)
        calls = lay["calls"]
        self.assertIn("docker start c2", calls)
        self.assertNotIn("docker start c1", calls, "the stack's own containers come back with the stack's start")
        self.assertLess(calls.index("docker stop -t 30 c1 c2"), calls.index("docker start c2"))
        self.assertIn("Containers started again: other-app", lay["result"].stdout)
        record = (self.archive(lay, "rolled-back") / "quiesced-containers").read_text().splitlines()
        self.assertEqual(record, ["c1 vesselstack-grafana vesselstack", "c2 other-app other"])

    def test_a_container_that_does_not_start_again_is_named_and_does_not_fail_the_rollback(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.containers(lay, "c2|other-app|other|%s/influx" % lay["data"], "c3|second|other|%s/x" % lay["data"])
        self.reconcile(lay, extra="export DOCKER_START_FAIL=c2")
        self.assert_rolled_back(lay)
        self.assertIn("could not be started again: other-app", lay["result"].stderr)
        self.assertIn("sudo docker start other-app", lay["result"].stderr)
        self.assertIn("Containers started again: second", lay["result"].stdout)

    def test_the_containers_a_failed_rollback_stopped_are_listed_with_the_units(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        lay["active"].write_text("vesselstack-chat.service\n")
        self.containers(lay, "c2|other-app|other|%s/influx" % lay["data"])
        self.reconcile(lay, extra="export STUCK_UNITS=vesselstack-chat.service")
        self.assertNotEqual(lay["result"].returncode, 0)
        # the units fail first, nothing else was stopped: no containers are listed
        self.assertNotIn("Containers stopped during the recovery", lay["result"].stderr)
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.containers(lay, "c2|other-app|other|%s/influx" % lay["data"], "c3|stuck|other|%s/y" % lay["data"])
        self.reconcile(lay, extra="export DOCKER_STUCK=c3")
        self.assertNotEqual(lay["result"].returncode, 0)
        # the one that stayed running is not claimed as stopped
        self.assertIn("Containers stopped during the recovery (not started again): other-app\n", lay["result"].stderr)

    def test_a_container_is_started_again_after_a_commit_and_after_an_abort(self):
        pre = self.FAKE_DOCKER + 'printf "c1|plain|none|@DATA@/sub\\n" > "@BASE@/containers"'
        td, result, root, data = self.run_transaction("success", pre=pre); self.addCleanup(td.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text().splitlines()
        self.assertIn("docker start c1", calls)
        self.assertLess(calls.index("docker stop -t 30 c1"), calls.index("docker start c1"))
        self.assertIn("Containers started again: plain", result.stdout)
        td, result, root, data = self.run_transaction("new_start_fail", pre=pre); self.addCleanup(td.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("docker start c1", self.log.read_text().splitlines())
        self.assertTrue((root / "old").exists())

    def test_a_container_that_cannot_be_recorded_is_not_stopped(self):
        pre = self.FAKE_DOCKER + 'printf "c1|plain|none|@DATA@/sub\\n" > "@BASE@/containers"; restore_sync_file() { case "$2" in *quiesced-containers) return 1 ;; *) return 0 ;; esac; }'
        td, result, root, data = self.run_transaction("success", pre=pre); self.addCleanup(td.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not record in the journal which containers were stopped", result.stderr)
        self.assertFalse([c for c in self.log.read_text().splitlines() if c.startswith("docker stop")])

    def test_the_firewall_notice_uses_the_record_made_before_the_restore(self):
        # was running before, not running now: said at the end of a successful rollback, with how to bring it back
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.set_state(lay["journal"], "firewall_enabled=yes", "firewall_active=yes")
        self.reconcile(lay)
        self.assert_rolled_back(lay)
        self.assertIn("The firewall (vesselstack-firewall.service) is NOT running", lay["result"].stderr)
        self.assertIn("sudo systemctl start vesselstack-firewall.service", lay["result"].stderr)
        # it was not running before: nothing to say, even though it is enabled
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.set_state(lay["journal"], "firewall_enabled=yes", "firewall_active=no")
        self.reconcile(lay)
        self.assert_rolled_back(lay)
        self.assertIn("NOT running", lay["result"].stderr)
        self.assertIn("already not running before the restore", lay["result"].stderr)
        # neither enabled nor running before: nothing to say
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.set_state(lay["journal"], "firewall_enabled=no", "firewall_active=no")
        self.reconcile(lay)
        self.assert_rolled_back(lay)
        self.assertNotIn("NOT running", lay["result"].stderr)
        # it is running now: nothing to say
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.set_state(lay["journal"], "firewall_enabled=yes", "firewall_active=yes")
        lay["active"].write_text("vesselstack-firewall.service\n")
        self.reconcile(lay)
        self.assert_rolled_back(lay)
        self.assertNotIn("NOT running", lay["result"].stderr)
        # a journal without the record (an earlier release) falls back to the live state
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.reconcile(lay)
        self.assert_rolled_back(lay)
        self.assertIn("NOT running", lay["result"].stderr)

    def test_a_refused_rollback_says_the_firewall_is_down_and_gives_the_retry_command(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.set_state(lay["journal"], "firewall_enabled=yes", "firewall_active=yes")
        lay["active"].write_text("vesselstack-chat.service\n")
        self.reconcile(lay, extra="export STUCK_UNITS=vesselstack-chat.service")
        self.assertNotEqual(lay["result"].returncode, 0)
        self.assertIn("is NOT running", lay["result"].stderr)
        self.assertNotIn("move the journal aside", lay["result"].stderr)
        self.assertNotIn("To resolve this by hand", lay["result"].stderr)
        self.assertIn(f"run the recovery again: sudo {lay['journal']}/recover rollback", lay["result"].stderr)

    def test_recover_status_says_when_the_firewall_is_not_running(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.set_state(lay["journal"], "firewall_enabled=yes", "firewall_active=yes")
        status = self.recover_harness(lay, "status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("The firewall (vesselstack-firewall.service) is NOT running", status.stdout)
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.set_state(lay["journal"], "firewall_enabled=no", "firewall_active=no")
        self.assertNotIn("NOT running", self.recover_harness(lay, "status").stdout)

    def test_a_firewall_that_cannot_be_restarted_is_said_to_be_down(self):
        lay = self.layout("externals", "swapped", "swapped", True)
        self.reconcile(lay, extra="FAIL_UNIT_START=vesselstack-firewall.service")
        self.assert_rolled_back(lay)
        self.assertIn("so the firewall is DOWN now", lay["result"].stderr)
        self.assertNotIn("may not be applied", lay["result"].stderr)

    def test_the_journal_the_restore_writes_records_the_firewall_and_is_accepted_by_the_recovery(self):
        result = self.journal_for_layout("@B@/opt/vesselstack", "@B@/opt/vesselstack-data")
        self.assertEqual(result.stdout.strip().splitlines()[-1], "VALID", result.stdout + result.stderr)
        state = (self.last_base / "var/lib/vesselstack/restore/current/state").read_text().splitlines()
        self.assertIn("firewall_enabled=no", state); self.assertIn("firewall_active=no", state)
        fake = 'systemctl() { case "$1" in is-enabled|is-active) return 0 ;; *) return 0 ;; esac; }'
        result = self.journal_for_layout("@B@/opt/vesselstack", "@B@/opt/vesselstack-data", extra=fake)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "VALID", result.stdout + result.stderr)
        state = (self.last_base / "var/lib/vesselstack/restore/current/state").read_text().splitlines()
        self.assertIn("firewall_enabled=yes", state); self.assertIn("firewall_active=yes", state)

    def test_a_bad_firewall_record_is_rejected_and_a_missing_one_is_accepted(self):
        lay = self.layout("stopped")
        self.set_state(lay["journal"], "firewall_active=maybe")
        result = self.recover_harness(lay, "status")
        self.assertEqual(result.returncode, 1)
        self.assertIn("record of the firewall state", result.stderr)
        self.set_state(lay["journal"], "firewall_active=yes", "firewall_enabled=yes")
        self.assertEqual(self.recover_harness(lay, "status").returncode, 0)

    def test_an_abandoned_journal_that_is_reconciled_follows_up_like_the_abandon_command(self):
        lay = self.layout("abandoned", quiesced=("vesselstack-backup.timer",))
        self.set_state(lay["journal"], "firewall_enabled=yes", "firewall_active=yes")
        (lay["journal"] / "quiesced-containers").write_text("c2 other-app other\nc1 vesselstack-grafana vesselstack\n")
        self.containers(lay)
        self.reconcile(lay)
        result = lay["result"]
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("outcome=abandoned", result.stdout)
        self.assertIn("systemctl start vesselstack-backup.timer", lay["calls"])
        self.assertIn("Units started again: vesselstack-backup.timer", result.stdout)
        self.assertIn("docker start c2", lay["calls"]); self.assertNotIn("docker start c1", lay["calls"])
        self.assertIn("Containers started again: other-app", result.stdout)
        self.assertIn("NOT running", result.stdout)
        self.assertIn("RESTORED release", result.stdout)
        self.assertFalse([c for c in lay["calls"] if c.startswith("controller ")], lay["calls"])
        self.assertEqual((lay["root"] / "marker").read_text(), "old-root")
        self.assertFalse(lay["journal"].exists())

    def test_an_abandoned_journal_that_no_longer_validates_ends_with_the_manual_procedure_and_no_rollback(self):
        lay = self.layout("abandoned")
        self.set_state(lay["journal"], "stamp=bogus")
        for _ in range(2):
            self.reconcile(lay)
            self.assertNotEqual(lay["result"].returncode, 0)
            self.assertIn("To resolve this by hand:", lay["result"].stderr)
            self.assertEqual(phase_of(lay["journal"]), "abandoned")
            self.assertFalse([c for c in lay["calls"] if c.startswith(("controller ", "systemctl", "docker"))], lay["calls"])
            self.assertEqual((lay["root"] / "marker").read_text(), "old-root")
            self.assertEqual((lay["data"] / "marker").read_text(), "old-data")
            self.assertIsNone(state_value(lay["journal"], "rollback_from"))

    def test_without_a_running_systemd_every_unit_is_idle_and_with_one_a_failed_answer_is_not(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            body = transaction_source() + 'systemctl() { return 1; }\nrestore_unit_idle some.service && echo IDLE || echo BUSY\n'
            self.assertEqual(self.run_harness(body, base).stdout.strip(), "BUSY")
            body = transaction_source() + 'rmdir "$SYSTEM_ROOT/run/systemd/system" "$SYSTEM_ROOT/run/systemd" "$SYSTEM_ROOT/run"\nsystemctl() { return 1; }\nrestore_unit_idle some.service && echo IDLE || echo BUSY\n'
            result = self.run_harness(body, base)
            self.assertEqual(result.stdout.strip(), "IDLE", result.stderr)

    def test_the_swap_renames_are_proved_with_a_probe_folder_that_is_always_removed(self):
        lay = self.layout("stopped")
        base = lay["base"]; stage = lay["stage"]; helper_dir = lay["journal"]
        harness = transaction_source() + f'''
LIBEXEC_DIR="{helper_dir}"
restore_probe_renames "{stage}" "{lay['root']}" "{lay['data']}" && echo PROBED || echo "REFUSED $restore_probe_error"
'''
        # empty probe folders an interrupted run left in the parents are removed; one with content is not touched
        (base / "opt/.vesselstack-restore-probe.111").mkdir(); (base / "opt/.vesselstack-restore-probe.222").mkdir()
        (base / "opt/.vesselstack-restore-probe.222/keep").write_text("x")
        result = self.run_harness(harness, base, real_rename=True)
        self.assertEqual(result.stdout.strip(), "PROBED", result.stderr)
        self.assertFalse((base / "opt/.vesselstack-restore-probe.111").exists())
        self.assertTrue((base / "opt/.vesselstack-restore-probe.222/keep").exists())
        shutil.rmtree(base / "opt/.vesselstack-restore-probe.222")
        self.assertEqual(sorted(p.name for p in (base / "opt").glob(".vesselstack-restore-probe.*")), [])
        self.assertEqual(sorted(p.name for p in stage.glob(".vesselstack-restore-probe.*")), [])
        # a parent that refuses the rename (a bind mount of the same disk gives EXDEV): refused, nothing left behind
        failing = harness.replace("restore_probe_renames \"", '''python3() {{ if [ "${{3:-}}" = rename ] && [[ "${{5:-}}" == "{0}"/.vesselstack-restore-probe.* ]]; then echo "[Errno 18] Invalid cross-device link" >&2; return 1; fi; command python3 "$@"; }}
restore_probe_renames "'''.format(base / "opt"), 1)
        result = self.run_harness(failing, base, real_rename=True)
        self.assertTrue(result.stdout.startswith("REFUSED a folder could not be renamed into"), result.stdout + result.stderr)
        self.assertIn("Invalid cross-device link", result.stdout)
        self.assertIn("not writable", result.stdout); self.assertIn("separate mount", result.stdout)
        self.assertEqual(sorted(p.name for p in (base / "opt").glob(".vesselstack-restore-probe.*")), [])
        self.assertEqual(sorted(p.name for p in stage.glob(".vesselstack-restore-probe.*")), [])
        text = CONTROLLER.read_text()
        stack = text[text.index("\nrestore_stack() {"):text.index("# ---------------------------------------------------------------- backup drive")]
        self.assertLess(stack.index("restore_probe_renames"), stack.index('restore_create_journal "$stage"'), "before the journal is written")

    def test_a_file_in_the_way_of_a_folder_is_said_plainly(self):
        lay = self.layout("stopped")
        afile = lay["base"] / "afile"; afile.write_text("x")
        body = transaction_source() + f'restore_fs_check "{lay["journal"]}/restore_journal.py" data "{afile}" || echo "error=$restore_fs_error"\n'
        result = self.run_harness(body, lay["base"])
        self.assertIn(f"error=something is in the way at {afile}", result.stdout)
        self.assertIn("Move it away", result.stdout)
        self.assertNotIn("could not check", result.stdout)

    def test_the_recover_wrapper_prints_the_manual_procedure_when_python_is_missing(self):
        text = CONTROLLER.read_text()
        a = text.index("<<'RECOVER'\n") + len("<<'RECOVER'\n"); wrapper = text[a:text.index("\nRECOVER\n", a) + 1]
        lay = self.layout("root-swap", "swapped", "away", False)
        (lay["journal"] / "recover").write_text(wrapper); (lay["journal"] / "recover").chmod(0o700)
        bindir = lay["base"] / "bin"; bindir.mkdir()
        for tool in ("dirname", "date", "cat"):
            os.symlink(shutil.which(tool), bindir / tool)
        env = dict(os.environ, PATH=str(bindir))
        result = subprocess.run(["/bin/bash", str(lay["journal"] / "recover"), "status"], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Cannot read the restore journal", result.stderr)
        self.assertIn("To resolve this by hand:", result.stderr)
        self.assertIn("(not readable from the journal)", result.stderr)
        self.assertIn(f"sudo mv -T {lay['journal']} {lay['journal']}.manual-", result.stderr)
        self.assertIn("sudo vesselstackctl doctor", result.stderr)
        # a helper that does not run is the same case, and the folders are printed when the state can be read anyway
        (lay["journal"] / "restore_journal.py").write_text("raise SystemExit(1)\n")
        result = subprocess.run(["/bin/bash", str(lay["journal"] / "recover"), "status"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("To resolve this by hand:", result.stderr)

    # ---- fifth review ----

    def test_a_journal_that_cannot_be_used_gets_the_safe_manual_procedure_and_a_retryable_refusal_does_not(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.set_state(lay["journal"], "stamp=bogus")
        self.reconcile(lay)
        err = lay["result"].stderr
        self.assertNotEqual(lay["result"].returncode, 0)
        self.assertIn("To resolve this by hand", err)
        self.assertIn("is the RESTORED copy", err)
        self.assertIn("<folder>.failed-restore-<stamp>", err)
        self.assertIn("both folders must come from the same side", err)
        self.assertIn("move the journal aside", err)
        self.assertNotIn("run the recovery again:", err)

    def test_the_manual_procedure_names_this_restores_stamp_when_it_can_be_read(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.set_state(lay["journal"], "format=2")  # rejected by the validator; the stamp stays valid
        self.reconcile(lay)
        err = lay["result"].stderr
        self.assertIn("To resolve this by hand", err)
        self.assertIn(f"<folder>.pre-restore-{STAMP} exists next to it", err)
        self.assertIn(f"move it to <folder>.failed-restore-{STAMP}, then move the .pre-restore-{STAMP} one back", err)
        self.assertIn(f"Only folders carrying the stamp {STAMP} belong to this restore", err)
        self.assertIn("with a different stamp was left by an earlier restore: leave it alone", err)
        self.assertNotIn("<stamp>", err)
        self.assertNotIn("modification times", err)
        # the recover wrapper reads the stamp the same way (here the state lacks a key but still has its stamp)
        lay = self.layout("stopped")
        state = lay["journal"] / "state"
        state.write_text("".join(l for l in state.read_text().splitlines(True) if not l.startswith("system_root=")))
        result = self.recover_harness(lay, "status")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"<folder>.pre-restore-{STAMP} exists next to it", result.stderr)
        self.assertIn(f"Only folders carrying the stamp {STAMP} belong to this restore", result.stderr)
        self.assertNotIn("<stamp>", result.stderr)

    def test_the_manual_procedure_says_how_to_choose_the_folder_when_the_stamp_cannot_be_read(self):
        for bad in ("bogus", "20260102T030405Z-1-1\x01"):
            with self.subTest(stamp=repr(bad)):
                lay = self.layout("root-swap", "swapped", "swapped", False)
                self.set_state(lay["journal"], f"stamp={bad}")
                self.reconcile(lay)
                err = lay["result"].stderr
                self.assertIn("<folder>.pre-restore-<stamp> exists next to it", err)
                self.assertIn("<folder>.failed-restore-<stamp>", err)
                self.assertIn("The stamp could not be read from the journal", err)
                self.assertIn("cannot tell this restore's folders from", err)
                self.assertIn("/done-<stamp>, rolled-back-<stamp> or abandoned-<stamp>)", err)
                self.assertIn("belongs to that earlier restore: ignore it", err)
                self.assertIn("move nothing, and get help", err)
                self.assertIn("this restore may have moved a folder and these steps cannot tell which: leave the journal in", err)
                self.assertIn("do step 1, skip this folder step, and go on with step 4", err)
                self.assertNotIn("only if both folders are present", err)
                self.assertNotIn("modification", err)
                self.assertNotIn("ls -ld", err)
                self.assertNotIn("newest stamp", err)
                # the validator's own error line may quote the value; the procedure itself never does
                procedure = err[err.index("To resolve this by hand"):]
                self.assertNotIn("bogus", procedure)
                self.assertNotIn("\x01", procedure)
                self.assertNotIn("was left by an earlier restore", procedure)
                self.assertNotIn("modification", procedure)

    def test_the_manual_procedure_text_is_the_same_in_the_recover_wrapper(self):
        self.assertEqual(manual_procedure_block(CONTROLLER, "restore_manual_procedure", "<<EOF", "\nEOF\n"),
                         manual_procedure_block(CONTROLLER, "cannot_read", "<<MANUAL", "\nMANUAL\n"))

    def test_the_manual_procedure_still_names_the_stamp_when_a_repeated_key_breaks_the_quiet_read(self):
        lay = self.layout("stopped")
        state = lay["journal"] / "state"
        state.write_text(state.read_text() + "phase=stopped\n")  # a repeated non-stamp key: get fails for every key
        for command in ("status", "rollback"):
            with self.subTest(command=command):
                result = self.recover_harness(lay, command)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"<folder>.pre-restore-{STAMP} exists next to it", result.stderr)
                self.assertIn(f"Only folders carrying the stamp {STAMP} belong to this restore", result.stderr)
                self.assertNotIn("<stamp>", result.stderr)
        # two different stamp= lines are no stamp at all
        state.write_text(state.read_text() + "stamp=20260102T030405Z-2-2\n")
        result = self.recover_harness(lay, "status")
        self.assertIn("The stamp could not be read from the journal", result.stderr)
        self.assertNotIn(STAMP, result.stderr.split("To resolve this by hand")[1])

    def test_the_manual_procedure_stops_everything_first_and_starts_with_the_installer_when_system_files_were_published(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.set_state(lay["journal"], "format=2")
        self.reconcile(lay)
        err = lay["result"].stderr
        procedure = err[err.index("To resolve this by hand"):]
        stop, first_move = procedure.index("1) stop everything that uses the two folders"), procedure.index("move it to <folder>.failed-restore")
        self.assertLess(stop, first_move)
        first = procedure[stop:first_move]
        self.assertIn("systemctl list-units 'vesselstack-*' --all --plain --no-legend | awk '{print $1}' | grep -vx", first)
        self.assertNotIn("--state=active", first)
        self.assertIn("shows inactive or failed (not", first); self.assertIn("activating, deactivating or reloading", first)
        self.assertIn("sudo docker ps --no-trunc --format '{{.Names}} {{.Mounts}}'", first)
        self.assertIn("sudo docker stop NAME", first)
        self.assertIn("vesselstack-firewall", first); self.assertIn("vesselstack-socketcan", first)
        self.assertIn("sudo docker ps -q --filter label=com.docker.compose.project=vesselstack | xargs -r sudo docker stop", first)
        self.assertIn("a folder that contains them (such as / or /opt). List the containers", first)
        self.assertNotIn("read-write", first); self.assertNotIn("read-only", first)
        self.assertIn("make each move with sudo mv -T, so that a move onto an existing folder fails instead of nesting it", procedure)
        self.assertIn("again and go on only when no such container is left", first)
        self.assertIn("Unit ... not loaded", first)
        self.assertIn("sudo mv -T ", procedure); self.assertIn("(if it says the target exists, use another name)", procedure)
        start = procedure[procedure.index("5) start the stack"):]
        self.assertNotIn("grep -E", procedure); self.assertNotIn("rollback_from", procedure); self.assertNotIn("abandoned_from", procedure)
        self.assertIn(f"sudo {lay['root']}/installer/install.sh --config {lay['root']}/config/vesselstack.env --start", start)
        self.assertIn("If the installer stops, it says why: fix what it names and run it again.", start)
        self.assertIn("Only if it\n     cannot be run at all", start)
        self.assertIn("sudo vesselstackctl start", start)
        self.assertLess(start.index("install.sh --config"), start.index("sudo vesselstackctl start"))
        self.assertIn("the timers\n     (backups, alerts, telemetry) stay stopped until the installer has been run", start)
        self.assertIn("the moved-aside journal", start)
        self.assertIn("sudo vesselstackctl doctor", start)
        # a root that cannot be read still gives an instruction that makes sense
        state = lay["journal"] / "state"
        state.write_text("".join(l for l in state.read_text().splitlines(True) if not l.startswith("root=")) + "phase=stopped\n")
        result = self.recover_harness(lay, "status")
        self.assertIn("sudo <application folder>/installer/install.sh --config <application folder>/config/vesselstack.env --start", result.stderr)

    def test_a_root_with_a_control_character_is_printed_with_a_question_mark(self):
        lay = self.layout("stopped")
        self.set_state(lay["journal"], "format=2")
        subprocess.run([sys.executable, "-I", str(lay["journal"] / "restore_journal.py"), "set", str(lay["journal"]),
                        "root=/opt/vessel\x01stack"], check=True)
        result = self.recover_harness(lay, "status")
        self.assertEqual(result.returncode, 1)
        self.assertIn("application folder /opt/vessel?stack", result.stderr)
        self.assertNotIn("\x01", result.stderr.split("To resolve this by hand")[1])

    def test_a_container_stopped_by_the_recovery_is_named_only_when_it_really_stopped(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.containers(lay, "c1|vesselstack-grafana|vesselstack|/elsewhere", "c2|mine|other|%s/x" % lay["root"])
        self.reconcile(lay, extra="export DOCKER_STUCK=c1")
        err = lay["result"].stderr
        self.assertNotEqual(lay["result"].returncode, 0)
        self.assertIn("Containers stopped during the recovery (not started again): mine", err)
        self.assertNotIn("not started again): vesselstack-grafana", err); self.assertNotIn("mine vesselstack-grafana", err)
        self.assertNotIn("again): mine vesselstack-grafana", err)

    def test_a_rollback_goes_on_when_the_containers_cannot_be_recorded_and_says_so(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.containers(lay, "c1|mine|other|%s/x" % lay["root"], "c2|vesselstack-grafana|vesselstack|%s/y" % lay["data"])
        self.reconcile(lay, extra='restore_sync_file() { case "$2" in *quiesced-containers) return 1 ;; *) return 0 ;; esac; }')
        self.assert_rolled_back(lay)
        err = lay["result"].stderr
        self.assertIn("could not be recorded", err); self.assertIn("may not be started again automatically: mine vesselstack-grafana", err)
        self.assertNotIn("will NOT", err)
        self.assertIn("sudo docker start mine\n", err); self.assertNotIn("docker start mine vesselstack-grafana", err)
        self.assertTrue([c for c in lay["calls"] if c.startswith("docker stop")], lay["calls"])

    def test_a_rollback_from_a_rescue_system_without_systemd_ends_rolled_back_with_status_3(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        self.reconcile(lay, extra='rmdir "$SYSTEM_ROOT/run/systemd/system"')
        result = lay["result"]
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("outcome=rolled-back", result.stdout)
        self.assertEqual((lay["root"] / "marker").read_text(), "old-root"); self.assertEqual((lay["data"] / "marker").read_text(), "old-data")
        self.assertIn("systemd is not running here", result.stderr)
        self.assertIn("sudo vesselstackctl start", result.stderr)
        self.assertNotIn("systemctl daemon-reload", lay["calls"])
        self.assertFalse([c for c in lay["calls"] if c.startswith(("systemctl start", "systemctl restart")) or c.endswith(" start") and c.startswith("controller")], lay["calls"])
        self.assertFalse(lay["journal"].exists())
        self.assertEqual(phase_of(self.archive(lay, "rolled-back")), "rolled-back")

    def test_an_abandon_records_where_it_came_from_and_the_warning_follows_it(self):
        lay = self.layout("stopped")
        done = self.recover_harness(lay, "abandon --yes")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("publishing system files", done.stdout)
        self.assertIn("abandoned_from=stopped", (self.archive(lay, "abandoned") / "state").read_text())
        for source, expected, unexpected in (("stopping", None, "publishing system files"), ("externals", "had begun publishing system files", "may have"),
                                             (None, "may have begun publishing system files", "had begun")):
            with self.subTest(source=source):
                lay = self.layout("abandoned")
                if source: self.set_state(lay["journal"], f"abandoned_from={source}")
                self.reconcile(lay)
                self.assertEqual(lay["result"].returncode, 0, lay["result"].stderr)
                if expected: self.assertIn(expected, lay["result"].stdout)
                self.assertNotIn(unexpected, lay["result"].stdout)
        lay = self.layout("rollback-failed")
        self.set_state(lay["journal"], "rollback_from=externals")
        self.assertEqual(self.recover_harness(lay, "abandon --yes").returncode, 0)
        self.assertIn("abandoned_from=rollback-failed", (self.archive(lay, "abandoned") / "state").read_text())

    def test_an_abandoned_journal_with_an_unknown_origin_keeps_the_system_files_warning(self):
        for source, extra in (("unknown", None), ("somethingelse", None)):
            with self.subTest(source=source, rollback_from=extra):
                lay = self.layout("abandoned")
                self.set_state(lay["journal"], f"abandoned_from={source}")
                if extra: self.set_state(lay["journal"], f"rollback_from={extra}")
                self.reconcile(lay)
                self.assertEqual(lay["result"].returncode, 0, lay["result"].stderr)
                self.assertIn("may have begun publishing system files", lay["result"].stdout)
        lay = self.layout("abandoned")
        self.set_state(lay["journal"], "abandoned_from=rollback-failed")
        self.reconcile(lay)
        self.assertEqual(lay["result"].returncode, 0, lay["result"].stderr)
        self.assertIn("had begun publishing system files", lay["result"].stdout)
        self.assertTrue((self.archive(lay, "abandoned") / "manifest").exists(), "the snapshot is kept")
        lay = self.layout("abandoned")
        self.set_state(lay["journal"], "abandoned_from=rollback-failed", "rollback_from=prepared")
        self.reconcile(lay)
        self.assertNotIn("publishing system files", lay["result"].stdout)
        lay = self.layout("abandoned")
        self.set_state(lay["journal"], "abandoned_from=rollback-failed", "rollback_from=bogus")
        self.reconcile(lay)
        self.assertEqual(lay["result"].returncode, 0, lay["result"].stderr)
        self.assertIn("may have begun publishing system files", lay["result"].stdout)
        self.assertTrue((self.archive(lay, "abandoned") / "manifest").exists(), "the snapshot is kept")

    def test_the_recover_wrapper_explains_each_way_the_journal_can_be_unreadable(self):
        text = CONTROLLER.read_text()
        a = text.index("<<'RECOVER'\n") + len("<<'RECOVER'\n"); wrapper = text[a:text.index("\nRECOVER\n", a) + 1]
        def run(lay):
            (lay["journal"] / "recover").write_text(wrapper); (lay["journal"] / "recover").chmod(0o700)
            return subprocess.run(["/bin/bash", str(lay["journal"] / "recover"), "status"], capture_output=True, text=True)
        lay = self.layout("root-swap", "swapped", "away", False)
        (lay["journal"] / "state").write_text("")
        result = run(lay)
        self.assertEqual(result.returncode, 1)
        self.assertIn("is missing or empty", result.stderr); self.assertIn("To resolve this by hand", result.stderr)
        lay = self.layout("root-swap", "swapped", "away", False)
        state = lay["journal"] / "state"
        state.write_text("".join(l for l in state.read_text().splitlines(True) if not l.startswith("system_root=")))
        result = run(lay)
        self.assertEqual(result.returncode, 1)
        self.assertIn('the state does not record "system_root"', result.stderr)
        self.assertNotIn("python3 is missing", result.stderr); self.assertIn("To resolve this by hand", result.stderr)
        self.assertIn(f"application folder {lay['root']} and the data folder {lay['data']}", result.stderr)

    def test_the_firewall_notice_after_a_commit_when_the_restored_release_stopped_it(self):
        set_fw = 'python3 -I "@BASE@/restore/current/restore_journal.py" set "@BASE@/restore/current" %s'
        td, result, root, data = self.run_transaction("success", pre=set_fw % "firewall_enabled=yes firewall_active=yes"); self.addCleanup(td.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("The firewall (vesselstack-firewall.service) is NOT running", result.stderr)
        td, result, root, data = self.run_transaction("success", pre=set_fw % "firewall_enabled=yes firewall_active=no"); self.addCleanup(td.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("NOT running", result.stderr)

    def test_a_compose_label_with_a_separator_does_not_corrupt_the_recorded_name(self):
        lay = self.layout("root-swap", "swapped", "swapped", False)
        body = transaction_source() + f'''
docker() {{ case "$1" in info) return 0 ;; ps) echo c1 ;;
    inspect) case "$3" in *Mounts*) echo "true|{lay['root']}/x" ;; *) echo "/mine|we|ird proj" ;; esac ;; esac; }}
restore_docker_processes() {{ return 1; }}
restore_find_containers "{lay['root']}" "{lay['data']}" "{lay['journal']}/restore_journal.py" || true
restore_record_containers "{lay['journal']}"
echo "name=${{restore_container_name_list[0]}} label=${{restore_container_label_list[0]}}"
cat "{lay['journal']}/quiesced-containers"
'''
        result = self.run_harness(body, lay["base"])
        self.assertIn("name=mine label=we|ird_proj", result.stdout, result.stderr)
        self.assertIn("c1 mine we|ird_proj", result.stdout)
        # a file written by the previous format is still read
        (lay["journal"] / "quiesced-containers").write_text("c2 other-app other\nc1 vesselstack-grafana vesselstack\n")
        started = self.run_harness(transaction_source() + f'docker() {{ echo "docker $*"; }}\nrestore_start_containers "{lay["journal"]}/quiesced-containers"\n', lay["base"])
        self.assertIn("Containers started again: other-app", started.stdout, started.stderr)

    def test_the_unused_swap_in_function_is_gone(self):
        self.assertNotIn("\nswap_in() {", CONTROLLER.read_text())

    def test_the_sources_say_what_the_owner_needs_to_see(self):
        text = CONTROLLER.read_text()
        self.assertIn("WARN the restore completed, but its recovery journal could not be archived", text)
        self.assertIn("recover rollback   (in this phase it only removes leftovers", text)
        self.assertIn("restore-recover status|rollback|abandon [--yes]", text)
        self.assertIn('install -d -m 0700 "$RESTORE_DIR"', text)
        self.assertNotIn('install -d -m 0700 "$SYSTEM_ROOT/var/lib/vesselstack"', text)

# ---------------------------------------------------------------- lock hand-over and the hidden worker action
HEX_ID = "0123456789abcdef0123456789abcdef"
LOCK_HELPER = ROOT / "common/lifecycle_lock.py"


class RestoreLockTests(unittest.TestCase):
    """restore_take_lock with the journal's own copy of the lock helper, while the restore is pending."""

    def pending_system(self, phase="starting", helper_source=None):
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup); base = Path(td.name)
        (base / "opt/vesselstack").mkdir(parents=True)
        journal = base / "var/lib/vesselstack/restore/current"; journal.mkdir(parents=True, mode=0o700)
        shutil.copy(JOURNAL_HELPER, journal / "restore_journal.py")
        (journal / "lifecycle_lock.py").write_text(helper_source if helper_source is not None else LOCK_HELPER.read_text())
        subprocess.run([sys.executable, "-I", str(journal / "restore_journal.py"), "set", str(journal),
                        "format=1", f"id={HEX_ID}", f"phase={phase}"], check=True)
        return base, journal

    def run_take_lock(self, base, journal, *take_lock_args, preset_id=False, env=None):
        text = CONTROLLER.read_text()
        functions = text[text.index("restore_state_get() {"):text.index("restore_state_set() {")] + \
            text[text.index("restore_take_lock() {"):text.index("restore_recover_rollback() {")]
        preset = "st_id=" + HEX_ID if preset_id else ""
        script = base / "take-lock.sh"
        script.write_text(f"""#!/bin/bash
set -euo pipefail
if [ "${{1:-}}" = restore-recover ]; then
    echo "reentered $*"
    echo "RID=${{VESSELSTACK_RESTORE_ID:-}}"
    echo "FD=${{VESSELSTACK_LIFECYCLE_LOCK_FD:+set}}"
    exit 0
fi
ROOT="{base}/opt/vesselstack"; SYSTEM_ROOT="{base}"
{functions}
{preset}
restore_take_lock "{journal}" {" ".join(take_lock_args)}
echo "returned"
""")
        run_env = {k: v for k, v in os.environ.items() if k not in ("VESSELSTACK_LIFECYCLE_LOCK_FD", "VESSELSTACK_RESTORE_ID")}
        run_env.update(env or {})
        return subprocess.run(["bash", str(script), "first"], capture_output=True, text=True, env=run_env, timeout=60)

    def test_the_recovery_takes_a_fresh_lock_while_its_restore_is_pending(self):
        for phase in ("prepared", "starting", "rollback-failed", "abandoned"):
            for preset in (False, True):
                with self.subTest(phase=phase, preset_id=preset):
                    base, journal = self.pending_system(phase)
                    result = self.run_take_lock(base, journal, "rollback", preset_id=preset)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("reentered restore-recover rollback", result.stdout)
                    self.assertIn(f"RID={HEX_ID}", result.stdout)
                    self.assertIn("FD=set", result.stdout)

    def test_a_journal_from_0_9_0_with_a_helper_that_has_no_check_still_works(self):
        old = LOCK_HELPER.read_text().replace("journal = _unfinished_restore(self.system_root)", "journal = None")
        self.assertNotEqual(old, LOCK_HELPER.read_text())
        base, journal = self.pending_system("starting", helper_source=old)
        result = self.run_take_lock(base, journal, "rollback")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"RID={HEX_ID}", result.stdout)

    def test_without_the_restore_id_the_same_helper_refuses(self):
        base, journal = self.pending_system("starting")
        result = subprocess.run([sys.executable, "-I", str(journal / "lifecycle_lock.py"), "run", "--root", str(base / "opt/vesselstack"),
                                 "--system-root", str(base), "--", "true"],
                                capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != "VESSELSTACK_RESTORE_ID"})
        self.assertEqual(result.returncode, 75)
        self.assertIn(f"a restore is unfinished; see: sudo {journal}/recover status", result.stderr)

    def test_an_id_exported_by_the_caller_is_replaced_by_the_journals(self):
        base, journal = self.pending_system("starting")
        result = self.run_take_lock(base, journal, "rollback", env={"VESSELSTACK_RESTORE_ID": "f" * 32})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"RID={HEX_ID}", result.stdout)

    def hold(self, base):
        """Hold the lifecycle lock of the test system (as the restore itself does) and return the handle."""
        from common.lifecycle_lock import LifecycleLock
        with mock.patch.dict(os.environ, {"VESSELSTACK_RESTORE_ID": HEX_ID}):
            os.environ.pop("VESSELSTACK_LIFECYCLE_LOCK_FD", None)
            return LifecycleLock(base / "opt/vesselstack", base).acquire()

    def test_a_wait_is_passed_to_the_helper_and_waits_for_the_lock(self):
        import threading
        base, journal = self.pending_system("prepared")
        holder = self.hold(base)
        timer = threading.Timer(1.0, holder.release); timer.start()
        try:
            result = self.run_take_lock(base, journal, "--wait", "10", "worker", HEX_ID)
        finally:
            timer.join()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"reentered restore-recover worker {HEX_ID}", result.stdout)

    def test_a_wait_that_runs_out_gives_up_with_75(self):
        base, journal = self.pending_system("prepared")
        holder = self.hold(base)
        try:
            busy = self.run_take_lock(base, journal, "--wait", "1", "worker", HEX_ID)
        finally:
            holder.release()
        self.assertEqual(busy.returncode, 75)
        self.assertNotIn("reentered", busy.stdout)

    def test_recover_rollback_through_the_real_controller_is_not_locked_out(self):
        # The journal's own wrapper and controller copy, the real lock helper and a pending journal: the rollback
        # starts (it is not refused by the lock), while another command is refused.
        helper = RestoreTransactionTests(); self.addCleanup(helper.doCleanups)
        lay = helper.layout("prepared")
        base = lay["base"]
        (base / "var/lib/vesselstack").mkdir(parents=True)
        os.rename(base / "restore", base / "var/lib/vesselstack/restore")
        journal = base / "var/lib/vesselstack/restore/current"
        helper.set_state(journal, f"system_root={base}")
        shutil.copy(CONTROLLER, journal / "controller"); (journal / "controller").chmod(0o700)
        shutil.copy(LOCK_HELPER, journal / "lifecycle_lock.py")
        recover = journal / "recover"; recover.write_text(helper.real_recover_wrapper()); recover.chmod(0o700)
        (base / "opt/vesselstack/common").mkdir(parents=True); shutil.copy(LOCK_HELPER, base / "opt/vesselstack/common/lifecycle_lock.py")
        fakebin = base / "fakebin"; fakebin.mkdir()
        (fakebin / "id").write_text("#!/bin/sh\necho 0\n")
        (fakebin / "systemctl").write_text("#!/bin/sh\ncase \"$1\" in is-active|is-enabled) exit 1 ;; esac\nexit 0\n")
        for name in ("id", "systemctl"): (fakebin / name).chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k not in ("VESSELSTACK_LIFECYCLE_LOCK_FD", "VESSELSTACK_RESTORE_ID", "VESSELSTACK_ENV")}
        env.update(PATH=f"{fakebin}:{os.environ['PATH']}", VESSELSTACK_SYSTEM_ROOT=str(base))
        refused = subprocess.run(["bash", str(CONTROLLER), "start"], capture_output=True, text=True, env=env)
        self.assertEqual(refused.returncode, 1); self.assertIn("A restore did not finish", refused.stderr)
        result = subprocess.run([str(recover), "rollback"], capture_output=True, text=True, env=env, timeout=120)
        self.assertNotIn("lifecycle lock", result.stderr, result.stdout + result.stderr)
        self.assertNotIn("a restore is unfinished", result.stderr)
        self.assertIn("Recovering an unfinished restore (phase: prepared)", result.stdout, result.stdout + result.stderr)


class RestoreWorkerTests(unittest.TestCase):
    """`recover worker ID`: the forward part of a restore, run from the journal alone."""
    run_harness = RestoreTransactionTests.run_harness
    build_harness = RestoreTransactionTests.build_harness

    STUBS = r"""
SYSTEMD_DIR="@SYSTEMD@"
RESTORE_DIR="@BASE@/restore"
RESTORE_CURRENT="$RESTORE_DIR/current"
usage() { echo usage; }
restore_take_lock() { echo "TAKE_LOCK $*" >> "@LOG@"; }
restore_state_set() { local j=$1; shift; case "$*" in *phase=*) echo "PHASE $*" >> "@LOG@" ;; esac; python3 -I "$j/restore_journal.py" set "$j" "$@"; }
restore_run_controller() { action=$3; if [ "$action" = stop ]; then if [ "$2" = "@NEW@" ]; then [ "@MODE@" != new_stop_fail ]; else [ "@MODE@" != old_stop_fail ]; fi; elif [ "$action" = start ]; then if [ "$2" = "@NEW@" ]; then [ "@MODE@" != new_start_fail ] && [ "@MODE@" != new_stop_fail ]; else true; fi; fi; }
docker() { return 1; }
restore_docker_processes() { return 1; }
restore_publish_externals() { return 0; }
restore_sync_system_files() { return 0; }
restore_restore_externals() { return 0; }
restore_check_externals() { return 0; }
systemctl() {
    echo "systemctl $*" >> "@LOG@"
    local u
    case "$1" in
        is-active) u=${@: -1}; grep -qx "$u" "@ACTIVE@" ;;
        show) if [ "${3:-}" = ActiveState ]; then u=${@: -1}; if grep -qx "$u" "@ACTIVE@"; then echo active; else echo inactive; fi; fi ;;
        stop) for u in "${@:2}"; do { grep -vx "$u" "@ACTIVE@" || true; } > "@ACTIVE@.n"; command mv "@ACTIVE@.n" "@ACTIVE@"; done ;;
        start) for u in "${@:2}"; do echo "$u" >> "@ACTIVE@"; done ;;
        daemon-reload) if [ "@SIGNAL@" = 1 ] && [ ! -e "@BASE@/signalled" ]; then : > "@BASE@/signalled"; kill -HUP $$; kill -INT $$; fi ;;
        *) return 0 ;;
    esac
}
mv() { echo "MV $*" >> "@LOG@"; __mv "$@"; }
"""

    def prepare(self, mode="success", signal=False):
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup); base = Path(td.name)
        root = base / "root"; data = base / "data"; (root / "old").mkdir(parents=True); (data / "old").mkdir(parents=True)
        stage = base / STAGE_NAME; backup = stage / "vesselstack-backup"
        for tree in (root, data):
            (backup / str(tree).lstrip("/")).mkdir(parents=True); (backup / str(tree).lstrip("/") / "new").write_text("new")
        journal = make_journal(base, root, data, stage)
        subprocess.run([sys.executable, "-I", str(journal / "restore_journal.py"), "set", str(journal), f"id={HEX_ID}", "skip_installer=1"], check=True)
        systemd = base / "etc/systemd/system"; systemd.mkdir(parents=True)
        (base / "calls.log").write_text(""); (base / "active").write_text("")
        return {"base": base, "root": root, "data": data, "stage": stage, "journal": journal, "systemd": systemd,
                "log": base / "calls.log", "mode": mode, "signal": signal}

    def body(self, lay, tail):
        text = pending_source() + transaction_source() + controller_function_source("restore_forward() {", "restore_stack() {") + self.STUBS + tail
        for key, value in {"@SYSTEMD@": lay["systemd"], "@BASE@": lay["base"], "@LOG@": lay["log"], "@ACTIVE@": lay["base"] / "active",
                           "@NEW@": lay["journal"] / "new-controller", "@MODE@": lay["mode"], "@SIGNAL@": "1" if lay["signal"] else "0"}.items():
            text = text.replace(key, str(value))
        return text

    def run_inline(self, lay):
        return self.run_harness(self.body(lay, f'\nrestore_forward "{lay["journal"]}"\nexit 0\n'), lay["base"])

    def run_worker(self, lay, worker_id=HEX_ID):
        return self.run_harness(self.body(lay, f'\nrestore_recover_command worker "{worker_id}"\nexit 0\n'), lay["base"])

    def calls(self, lay):
        return lay["log"].read_text().replace(str(lay["base"]), "BASE").splitlines()

    def files(self, lay):
        return sorted(p.name for p in (lay["base"] / "restore").glob("worker-*"))

    def assert_nothing_happened(self, lay, result, before):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        lines = result.stderr.strip().splitlines()
        self.assertEqual(len(lines), 1, result.stderr); self.assertTrue(lines[0].startswith("worker: "), lines[0])
        self.assertEqual(self.files(lay), [])
        self.assertEqual(self.calls(lay), [])
        self.assertEqual((lay["journal"] / "state").read_text() if (lay["journal"] / "state").exists() else None, before)
        self.assertTrue((lay["root"] / "old").exists()); self.assertTrue((lay["data"] / "old").exists())

    def test_a_worker_that_is_not_admitted_does_nothing_and_exits_0(self):
        cases = {"another id": HEX_ID[::-1], "uppercase": HEX_ID.upper(), "too short": "abc123", "too long": HEX_ID + "0",
                 "path": "../../etc/passwd", "empty": ""}
        for name, worker_id in cases.items():
            with self.subTest(name):
                lay = self.prepare(); before = (lay["journal"] / "state").read_text()
                self.assert_nothing_happened(lay, self.run_worker(lay, worker_id), before)

    def test_a_phase_other_than_prepared_is_not_admitted(self):
        for phase in ("stopping", "stopped", "data-swap", "root-swap", "externals", "reloaded", "starting", "committed",
                      "rolling-back", "rolled-back", "rollback-failed", "abandoned"):
            with self.subTest(phase):
                lay = self.prepare()
                subprocess.run([sys.executable, "-I", str(lay["journal"] / "restore_journal.py"), "set", str(lay["journal"]), f"phase={phase}"], check=True)
                before = (lay["journal"] / "state").read_text()
                result = self.run_worker(lay)
                self.assert_nothing_happened(lay, result, before)
                self.assertIn(f"phase {phase}", result.stderr)

    def test_a_missing_journal_is_not_admitted(self):
        lay = self.prepare(); shutil.rmtree(lay["journal"])
        self.assert_nothing_happened(lay, self.run_worker(lay), None)

    def test_a_journal_without_state_is_not_admitted(self):
        lay = self.prepare(); (lay["journal"] / "state").unlink()
        result = self.run_worker(lay)
        self.assertEqual(result.returncode, 0, result.stderr); self.assertEqual(self.files(lay), []); self.assertEqual(self.calls(lay), [])

    def test_a_second_start_after_the_phase_moved_on_changes_nothing(self):
        lay = self.prepare()
        first = self.run_worker(lay); self.assertEqual(first.returncode, 0, first.stderr)
        files = self.files(lay); rc_text = (lay["base"] / f"restore/worker-{HEX_ID}.rc").read_text()
        calls = self.calls(lay)
        second = self.run_worker(lay)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(second.stderr.strip().splitlines()), 1)
        self.assertEqual(self.files(lay), files); self.assertEqual(self.calls(lay), calls)
        self.assertEqual((lay["base"] / f"restore/worker-{HEX_ID}.rc").read_text(), rc_text)

    def test_the_worker_runs_the_same_phases_and_calls_as_the_inline_restore(self):
        inline = self.prepare(); worker = self.prepare()
        result_inline = self.run_inline(inline)
        result_worker = self.run_worker(worker)
        self.assertEqual(result_inline.returncode, 0, result_inline.stderr)
        self.assertEqual(result_worker.returncode, 0, result_worker.stderr)
        calls = [line for line in self.calls(worker) if not line.startswith("TAKE_LOCK")]
        # The message carries both complete logs: a difference is then diagnosed from the evidence, not by a rerun.
        self.assertEqual(calls, self.calls(inline),
                         "worker calls (%d lines):\n%s\n\ninline calls (%d lines):\n%s" % (
                             len(calls), "\n".join(calls), len(self.calls(inline)), "\n".join(self.calls(inline))))
        phases = [line.split()[1] for line in calls if line.startswith("PHASE ")]
        self.assertEqual(phases, ["phase=stopping", "phase=stopped", "phase=data-swap", "phase=root-swap", "phase=externals",
                                  "phase=reloaded", "phase=starting", "phase=committed"])
        self.assertTrue(any(line.startswith("TAKE_LOCK ") and line.endswith(f" --wait 120 worker {HEX_ID}") for line in self.calls(worker)))
        for lay in (inline, worker):
            self.assertEqual((lay["root"] / "new").read_text(), "new"); self.assertEqual((lay["data"] / "new").read_text(), "new")
            self.assertEqual(phase_of(lay["base"] / "restore" / f"done-{STAMP}"), "committed")
            self.assertFalse((lay["base"] / "restore/current").exists())
        out = (worker["base"] / f"restore/worker-{HEX_ID}.out").read_text()
        err = (worker["base"] / f"restore/worker-{HEX_ID}.err").read_text()
        self.assertEqual(out.replace(str(worker["base"]), "BASE"), result_inline.stdout.replace(str(inline["base"]), "BASE"))
        self.assertEqual(err.replace(str(worker["base"]), "BASE"), result_inline.stderr.replace(str(inline["base"]), "BASE"))
        self.assertIn("Restore completed", out)
        self.assertEqual(result_worker.stdout, ""); self.assertEqual(result_worker.stderr, "")

    def test_the_worker_writes_its_files_with_mode_0600_and_the_status_last(self):
        lay = self.prepare()
        result = self.run_worker(lay); self.assertEqual(result.returncode, 0, result.stderr)
        restore = lay["base"] / "restore"
        self.assertEqual(self.files(lay), [f"worker-{HEX_ID}.{ext}" for ext in ("err", "out", "pid", "rc")])
        for ext in ("err", "out", "pid", "rc"):
            self.assertEqual((restore / f"worker-{HEX_ID}.{ext}").stat().st_mode & 0o777, 0o600, ext)
        self.assertEqual((restore / f"worker-{HEX_ID}.rc").read_text(), "0\n")
        self.assertRegex((restore / f"worker-{HEX_ID}.pid").read_text(), r"^[0-9]+ (-|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\n$")

    def test_a_forward_part_that_fails_leaves_the_failing_status_and_a_rolled_back_journal(self):
        inline = self.prepare("new_start_fail"); worker = self.prepare("new_start_fail")
        result_inline = self.run_inline(inline); result_worker = self.run_worker(worker)
        self.assertNotEqual(result_inline.returncode, 0)
        self.assertEqual(result_worker.returncode, result_inline.returncode)
        restore = worker["base"] / "restore"
        self.assertEqual((restore / f"worker-{HEX_ID}.rc").read_text(), f"{result_inline.returncode}\n")
        self.assertEqual(phase_of(restore / f"rolled-back-{STAMP}"), "rolled-back")
        self.assertTrue((worker["root"] / "old").exists()); self.assertTrue((worker["data"] / "old").exists())
        self.assertEqual((restore / f"worker-{HEX_ID}.err").read_text().replace(str(worker["base"]), "BASE"),
                         result_inline.stderr.replace(str(inline["base"]), "BASE"))
        self.assertEqual([l for l in self.calls(worker) if not l.startswith("TAKE_LOCK")], self.calls(inline))

    def test_the_worker_ignores_sigint_and_sighup(self):
        lay = self.prepare(signal=True)
        result = self.run_worker(lay)
        self.assertTrue((lay["base"] / "signalled").exists(), "the test did send the signals")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((lay["base"] / f"restore/worker-{HEX_ID}.rc").read_text(), "0\n")
        self.assertEqual(phase_of(lay["base"] / "restore" / f"done-{STAMP}"), "committed")

    def test_only_the_newest_three_sets_of_worker_files_are_kept(self):
        lay = self.prepare(); restore = lay["base"] / "restore"
        old_ids = [f"{n:032x}" for n in range(1, 6)]
        for index, old_id in enumerate(old_ids):
            for ext in ("pid", "out", "err", "rc"):
                path = restore / f"worker-{old_id}.{ext}"; path.write_text("x")
                os.utime(path, (1_000_000 + index * 100, 1_000_000 + index * 100))
        (restore / "worker-notes.txt").write_text("keep")
        result = self.run_worker(lay); self.assertEqual(result.returncode, 0, result.stderr)
        kept = sorted({name.split(".")[0][len("worker-"):] for name in self.files(lay) if name.endswith(".pid")})
        self.assertEqual(kept, sorted([HEX_ID, old_ids[4], old_ids[3]]))
        self.assertEqual(list(restore.glob(f"worker-{old_ids[0]}.*")), [])
        self.assertEqual(len(list(restore.glob(f"worker-{old_ids[4]}.*"))), 4)
        self.assertEqual((restore / "worker-notes.txt").read_text(), "keep")
        self.assertTrue((restore / f"worker-{HEX_ID}.rc").exists())

    def test_the_worker_action_is_not_advertised(self):
        text = CONTROLLER.read_text()
        usage = text[text.index("usage() {"):text.index("EOF", text.index("usage() {"))]
        self.assertNotIn("worker", usage)
        wrapper = text[text.index("cat > \"$building/recover\""):text.index("\nRECOVER\n")]
        self.assertNotIn("worker", wrapper)

    def test_the_pid_file_records_the_pid_and_the_boot_id(self):
        lay = self.prepare()
        self.assertEqual(self.run_worker(lay).returncode, 0)
        self.assertRegex((lay["base"] / "restore" / f"worker-{HEX_ID}.pid").read_text(), rf"^[0-9]+ {BOOT_ID}\n$")
        lay = self.prepare()
        text = self.body(lay, f'\nexport VESSELSTACK_BOOT_ID_FILE="@BASE@/no-boot-id"\nrestore_recover_command worker "{HEX_ID}"\nexit 0\n'.replace("@BASE@", str(lay["base"])))
        self.assertEqual(self.run_harness(text, lay["base"]).returncode, 0)
        self.assertRegex((lay["base"] / "restore" / f"worker-{HEX_ID}.pid").read_text(), r"^[0-9]+ -\n$")

    def test_a_failure_before_the_pid_file_is_written_still_leaves_a_result_file(self):
        lay = self.prepare()
        (lay["base"] / "restore" / f"worker-{HEX_ID}.pid").mkdir()  # the pid file cannot be written
        result = self.run_harness(self.body(lay, f'\nrestore_recover_command worker "{HEX_ID}"\nexit $?\n'), lay["base"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((lay["base"] / "restore" / f"worker-{HEX_ID}.rc").read_text(), f"{result.returncode}\n")
        self.assertTrue((lay["root"] / "old").exists()); self.assertTrue((lay["data"] / "old").exists())
        self.assertEqual(phase_of(lay["journal"]), "prepared")

    def run_worker_with_term(self, lay, phase):
        """The worker gets SIGTERM right after the journal records PHASE."""
        hook = (r'restore_state_set() { local j=$1; shift; case "$*" in *phase=*) echo "PHASE $*" >> "@LOG@" ;; esac; '
                r'python3 -I "$j/restore_journal.py" set "$j" "$@" || return; '
                'case "$*" in *"phase=' + phase + '"*) kill -TERM $$ ;; esac; }\n')
        hook = hook.replace("@LOG@", str(lay["log"]))
        return self.run_harness(self.body(lay, "\n" + hook + f'restore_recover_command worker "{HEX_ID}"\nexit 0\n'), lay["base"])

    def test_a_stop_request_before_any_folder_moved_rolls_the_restore_back(self):
        for phase in ("stopping", "stopped"):
            with self.subTest(phase):
                lay = self.prepare()
                result = self.run_worker_with_term(lay, phase)
                restore = lay["base"] / "restore"
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertTrue((lay["root"] / "old").exists()); self.assertTrue((lay["data"] / "old").exists())
                self.assertFalse((lay["root"] / "new").exists()); self.assertFalse((lay["data"] / "new").exists())
                self.assertEqual([l for l in self.calls(lay) if l.startswith("MV ") and "rolled-back-" not in l], [], "only the journal was archived")
                self.assertFalse((restore / "current").exists())
                self.assertEqual(phase_of(restore / f"rolled-back-{STAMP}"), "rolled-back")
                self.assertRegex(state_value(restore / f"rolled-back-{STAMP}", "stop_requested"), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
                self.assertNotIn("stop requested", state_value(restore / f"rolled-back-{STAMP}", "last_error").replace("stop requested before anything was moved", ""))
                self.assertEqual((restore / f"worker-{HEX_ID}.rc").read_text(), "1\n")
                self.assertIn("stop requested before anything was moved", (restore / f"worker-{HEX_ID}.err").read_text())

    def test_a_stop_request_while_still_prepared_ends_the_restore_without_stopping_anything(self):
        lay = self.prepare()
        # TERM arrives after the worker took over and before the commit transaction does its first step.
        hook = ('eval "$(declare -f restore_commit_transaction | sed 1s/restore_commit_transaction/restore_commit_original/)"\n'
                'restore_commit_transaction() { kill -TERM $$; restore_commit_original "$@"; }\n')
        result = self.run_harness(self.body(lay, "\n" + hook + f'restore_recover_command worker "{HEX_ID}"\nexit 0\n'), lay["base"])
        restore = lay["base"] / "restore"
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual([l for l in self.calls(lay) if l.startswith("systemctl stop") or l.startswith("systemctl start")], [])
        self.assertNotIn("PHASE phase=stopping", "\n".join(self.calls(lay)))
        self.assertEqual([l for l in self.calls(lay) if l.startswith("MV ") and "rolled-back-" not in l], [], "only the journal was archived")
        self.assertTrue((lay["root"] / "old").exists()); self.assertTrue((lay["data"] / "old").exists())
        self.assertFalse(lay["stage"].exists())
        self.assertFalse((restore / "current").exists())
        self.assertEqual(phase_of(restore / f"rolled-back-{STAMP}"), "rolled-back")
        self.assertRegex(state_value(restore / f"rolled-back-{STAMP}", "stop_requested"), r"^\d{4}-")
        self.assertNotEqual((restore / f"worker-{HEX_ID}.rc").read_text(), "0\n")
        self.assertIn("stop requested before anything was stopped", (restore / f"worker-{HEX_ID}.err").read_text())

    def test_a_stop_request_once_the_first_folder_moves_does_not_interrupt_the_restore(self):
        for phase in ("data-swap", "root-swap", "starting"):
            with self.subTest(phase):
                lay = self.prepare()
                result = self.run_worker_with_term(lay, phase)
                restore = lay["base"] / "restore"
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((lay["root"] / "new").read_text(), "new"); self.assertEqual((lay["data"] / "new").read_text(), "new")
                self.assertEqual(phase_of(restore / f"done-{STAMP}"), "committed")
                self.assertEqual((restore / f"worker-{HEX_ID}.rc").read_text(), "0\n")
                self.assertRegex(state_value(restore / f"done-{STAMP}", "stop_requested"), r"^\d{4}-")

    def test_without_a_stop_request_no_stop_is_recorded(self):
        lay = self.prepare()
        self.assertEqual(self.run_worker(lay).returncode, 0)
        self.assertIsNone(state_value(lay["base"] / "restore" / f"done-{STAMP}", "stop_requested"))


class RestoreDetachTests(unittest.TestCase):
    """The real controller: a restore that hands its forward part to a background worker and follows it.

    The pieces around the forward part are replaced in a copy of the controller (the journal is copied into place
    instead of being built from an archive, and restore_forward only walks the phases), so the hand-off, the
    lock release, the worker and the follower are the production code. The launcher is a stub that starts the
    worker like a transient unit does: a new session and an empty environment but PATH."""

    HOOK = r'''
restore_stack() {
    local arg
    restore_foreground=0
    for arg in "$@"; do [ "$arg" != --foreground ] || restore_foreground=1; done
    restore_early_stage=$SYSTEM_ROOT/opt/@STAGE@
    trap 'restore_early_signal 130' INT
    trap 'restore_early_signal 129' HUP
    trap 'restore_early_signal 143' TERM
    while [ -e "$SYSTEM_ROOT/early-hold" ]; do sleep 0.1; done
    install -d -m 0700 "$RESTORE_DIR"
    cp -a "$SYSTEM_ROOT/prebuilt" "$RESTORE_CURRENT"
    restore_run_journal "$RESTORE_CURRENT" "$restore_early_stage"
}
restore_forward() {
    local journal=$1 mode step
    restore_exit_status=1
    restore_state_load "$journal" && restore_validate_state || { echo "journal rejected: $restore_validate_error" >&2; return 1; }
    mode=$(cat "$SYSTEM_ROOT/ctl-mode" 2>/dev/null || echo ok)
    env > "$SYSTEM_ROOT/worker-env.$$"
    for step in stopping data-swap root-swap starting; do
        restore_state_set "$journal" "phase=$step"
        if [ "$step" = stopping ] && [ "$mode" = hang ]; then
            : > "$SYSTEM_ROOT/hanging"
            while [ ! -e "$SYSTEM_ROOT/release" ]; do sleep 0.1; done
        fi
        if [ "$step" = stopping ] && [ "${mode#fail}" != "$mode" ]; then
            echo "Restore failed: simulated failure" >&2
            restore_state_set "$journal" phase=rolled-back
            restore_reconcile "$journal" || true
            restore_exit_status=${mode#fail}
            return "$restore_exit_status"
        fi
        sleep "$(cat "$SYSTEM_ROOT/ctl-step" 2>/dev/null || echo 0)"
    done
    restore_state_set "$journal" phase=committed
    restore_reconcile "$journal" || true
    echo "Restore completed"
    echo "Previous trees kept as $st_root.pre-restore-$st_stamp and $st_data.pre-restore-$st_stamp."
    return 0
}
restore_run_controller() { return 0; }
docker() { return 1; }
mv() {
    if [ "$(uname)" = Darwin ] && [ "${1:-}" = -T ]; then
        shift; [ "${1:-}" = -- ] && shift
        if [ -d "$2" ] && [ ! -L "$2" ]; then rmdir "$2" || return 1; fi
        command mv -- "$1" "$2"
    else
        command mv "$@"
    fi
}
'''

    def setUp(self):
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        self.base = base = Path(td.name).resolve()
        self.procs = []
        self.addCleanup(self.stop_everything)
        self.root = base / "opt/vesselstack"; self.data = base / "opt/vesselstack-data"
        for tree, label in ((self.root, "root"), (self.data, "data")):
            tree.mkdir(parents=True); (tree / "marker").write_text(f"old-{label}")
        (self.root / "common").mkdir()
        shutil.copy(LOCK_HELPER, self.root / "common/lifecycle_lock.py")
        self.stage = base / "opt" / STAGE_NAME
        for tree in (self.root, self.data):
            staged = self.stage / "vesselstack-backup" / str(tree).lstrip("/"); staged.mkdir(parents=True); (staged / "marker").write_text("new")
        (base / "run/systemd/system").mkdir(parents=True)
        fakebin = base / "fakebin"; fakebin.mkdir()
        (fakebin / "id").write_text("#!/bin/sh\necho 0\n")
        (fakebin / "systemctl").write_text("#!/bin/sh\ncase \"$1\" in is-active|is-enabled) exit 1 ;; esac\nexit 0\n")
        for name in ("id", "systemctl"): (fakebin / name).chmod(0o755)
        text = CONTROLLER.read_text()
        marker = "\n[ $# -gt 0 ] || { usage; exit 2; }\n"
        self.assertEqual(text.count(marker), 1)
        text = text.replace(marker, self.HOOK.replace("@STAGE@", STAGE_NAME) + marker)
        call = '\nensure_lifecycle_lock "$command" "$@"\n'
        self.assertEqual(text.count(call), 1)
        text = text.replace(call, call + 'if [ "$command" = restore ]; then restore_stack "$@"; exit $?; fi\n')
        self.ctl = base / "test-controller"; self.ctl.write_text(text); self.ctl.chmod(0o700)
        helper = RestoreTransactionTests()
        journal = make_journal(base, self.root, self.data, self.stage)
        subprocess.run([sys.executable, "-I", str(journal / "restore_journal.py"), "set", str(journal), f"id={HEX_ID}"], check=True)
        shutil.copy(self.ctl, journal / "controller"); (journal / "controller").chmod(0o700)
        shutil.copy(LOCK_HELPER, journal / "lifecycle_lock.py")
        (journal / "recover").write_text(helper.real_recover_wrapper()); (journal / "recover").chmod(0o700)
        os.rename(journal, base / "prebuilt"); shutil.rmtree(base / "restore")
        (base / "var/lib/vesselstack").mkdir(parents=True)
        self.restore_dir = base / "var/lib/vesselstack/restore"; self.journal = self.restore_dir / "current"
        self.launcher = base / "launcher.py"
        self.launcher.write_text(self.STUB.replace("@PY@", sys.executable).replace("@BASE@", str(base)))
        self.launcher.chmod(0o755)

    STUB = r'''#!@PY@
import json, os, shutil, signal, subprocess, sys
base = "@BASE@"
args = sys.argv[1:]
with open(os.path.join(base, "launcher.log"), "a") as log:
    log.write(json.dumps(args) + "\n")
with open(os.path.join(base, "launcher-fds"), "a") as out:
    idents = []
    for n in sorted(int(n) for n in os.listdir("/dev/fd")):
        try:
            st = os.fstat(n)
            idents.append([st.st_dev, st.st_ino])
        except OSError:
            pass
    out.write(json.dumps(idents) + "\n")
missing = [need for need in ("--unit=run-vesselstack-restore", "--collect", "--quiet", "Type=exec", "KillMode=mixed", "TimeoutStopSec=10min",
                             "After=docker.service", "IgnoreOnIsolate=yes") if need not in args]
if missing or "--wait" in args or "--pipe" in args:
    open(os.path.join(base, "launcher-bad-args"), "w").write(repr(missing))
    sys.exit(98)
try:
    mode = open(os.path.join(base, "launcher-mode")).read().strip()
except OSError:
    mode = "start"
if mode == "fail":
    print("Failed to start transient service: Unit run-vesselstack-restore.service already exists.", file=sys.stderr)
    sys.exit(1)
if mode == "never":
    sys.exit(0)
cmd = args[args.index("/bin/bash"):]
cmd[0] = shutil.which("bash")
def reset():
    for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, signal.SIG_DFL)
worker = subprocess.Popen(["/usr/bin/env", "-i", "PATH=" + os.environ["PATH"]] + ["VESSELSTACK_BOOT_ID_FILE=" + os.environ[name] for name in ("VESSELSTACK_BOOT_ID_FILE",) if name in os.environ] + cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True, preexec_fn=reset)
# The only record of the workers this launcher really started, written by nobody else: the test's clean-up signals
# these and never a pid it reads from a worker-*.pid file (a file under test, which tests fill with unrelated pids).
with open(os.path.join(base, "started-workers.log"), "a") as started:
    started.write("%d %d\n" % (worker.pid, os.getpgid(worker.pid)))
if mode == "start-then-fail":
    print("Failed to start transient service: simulated", file=sys.stderr)
    sys.exit(1)
'''

    def started_workers(self):
        """(pid, pgid) of every worker the stub launcher started, from the file only the launcher writes."""
        found = []
        try:
            lines = (self.base / "started-workers.log").read_text().splitlines()
        except OSError:
            return found
        for line in lines:
            try:
                pid, pgid = (int(field) for field in line.split())
            except ValueError:
                continue
            found.append((pid, pgid))
        return found

    def own_process(self, pid):
        """True when `pid` is a live process that mentions this test's temp directory on its command line."""
        try:
            if sys.platform.startswith("linux"):
                command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            else:
                command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return str(self.base) in command

    @staticmethod
    def signal_ours(pid, group, sig=signal.SIGKILL):
        """Last line of defence for every kill of this class. Never pid or group 0 or 1 (on Linux killpg(1, ...) is
        kill(-1, ...), which kills every process of the user, the runner agent of a CI job included), never the test
        runner, its parent or its own group. The callers pass only processes this test started itself."""
        refused = {0, 1, os.getpid(), os.getppid(), os.getpgrp()}
        if pid in refused or pid < 0:
            return
        try:
            if group not in refused and group > 1:
                os.killpg(group, sig)
        except OSError:
            pass
        try:
            os.kill(pid, sig)
        except OSError:
            pass

    def stop_everything(self):
        """Nothing started by a test outlives it: every foreground and every worker, with its process group.

        Only processes this test started are signalled: the Popen objects it holds (all started in a session of their
        own, so the group is theirs) and the workers its stub launcher recorded. A worker-*.pid file is never read for
        this: tests write unrelated pids into those on purpose."""
        try:
            (self.base / "release").write_text("")
        except OSError:
            pass
        for proc in self.procs:
            self.signal_ours(proc.pid, proc.pid)  # its group is its own: it was started with start_new_session=True
            proc.wait()
        for pid, pgid in self.started_workers():
            if self.own_process(pid):
                self.signal_ours(pid, pgid)

    def env(self, **extra):
        # The launch wait is the production default: a worker that is slow to start (a busy CI runner) must not be
        # taken for one that never appears. The tests of that case set a short wait themselves.
        env = {k: v for k, v in os.environ.items() if not k.startswith("VESSELSTACK_")}
        env.update(PATH=f"{self.base / 'fakebin'}:{os.environ['PATH']}", VESSELSTACK_SYSTEM_ROOT=str(self.base),
                   VESSELSTACK_ROOT=str(self.root), VESSELSTACK_TEST_SYSTEMD_RUN=str(self.launcher),
                   VESSELSTACK_BOOT_ID_FILE=_BOOT_FILE, VESSELSTACK_RESTORE_FOLLOW_INTERVAL="0.1",
                   VESSELSTACK_RESTORE_LAUNCH_WAIT="30", VESSELSTACK_RESTORE_DEAD_GRACE="1", VESSELSTACK_RESTORE_LOCK_WAIT="10")
        env.update(extra)
        return env

    def start(self, *args, command=("restore", "x.tar.gz", "--yes"), **extra_env):
        proc = subprocess.Popen(["bash", str(self.ctl), *command, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=self.env(**extra_env), start_new_session=True)
        self.procs.append(proc)
        return proc

    def finish(self, proc, timeout=60):
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err

    def run_ctl(self, *args, timeout=60, **extra_env):
        result = subprocess.run(["bash", str(self.ctl), *args], capture_output=True, text=True, env=self.env(**extra_env), timeout=timeout)
        return result.returncode, result.stdout, result.stderr

    def wait_for(self, path, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if Path(path).exists():
                return
            time.sleep(0.05)
        self.fail(f"{path} did not appear\n{self.diagnostics()}")

    def diagnostics(self):
        """What a failed wait needs to explain itself: the foregrounds' status and output so far, the launcher's log, the
        worker files, the journal phase and the processes that mention this test's directory."""
        lines = []
        for number, proc in enumerate(self.procs):
            lines.append(f"foreground {number}: pid {proc.pid} returncode {proc.poll()}")
            for name in ("stdout", "stderr"):
                stream = getattr(proc, name)
                text = ""
                if stream is not None:
                    try:
                        os.set_blocking(stream.fileno(), False)
                        text = os.read(stream.fileno(), 65536).decode(errors="replace")
                    except (OSError, ValueError):
                        pass
                lines.append(f"  {name} so far: {self.normalise(text)!r}")
        for name in ("launcher.log", "started-workers.log"):
            try:
                lines.append(f"{name}: {self.normalise((self.base / name).read_text())!r}")
            except OSError:
                lines.append(f"{name}: (none)")
        try:
            lines.append("journal phase: %r runner: %r" % (state_value(self.journal, "phase"), state_value(self.journal, "runner")))
        except (OSError, ValueError) as exc:
            lines.append(f"journal: unreadable ({exc})")
        try:
            for path in sorted(self.restore_dir.iterdir()):
                text = ""
                if path.is_file() and path.name.startswith("worker-"):
                    text = path.read_text(errors="replace")[-1500:]
                lines.append(f"  {path.name}: {self.normalise(text)!r}")
        except OSError:
            lines.append("restore dir: (none)")
        lines.append("files here: " + " ".join(sorted(path.name for path in self.base.iterdir())))
        ps = subprocess.run(["ps", "-eo", "pid,pgid,stat,command"], capture_output=True, text=True).stdout
        lines.append("processes:\n" + "\n".join(self.normalise(line) for line in ps.splitlines() if str(self.base) in line))
        return "\n".join(lines)

    def mode(self, name, value):
        (self.base / name).write_text(value)

    def worker_pid(self):
        self.wait_for(self.restore_dir / f"worker-{HEX_ID}.pid")
        return int((self.restore_dir / f"worker-{HEX_ID}.pid").read_text().split()[0])

    def archived(self, kind="done"):
        return self.restore_dir / f"{kind}-{STAMP}"

    def normalise(self, text):
        return text.replace(str(self.base), "BASE")

    def launcher_calls(self):
        log = self.base / "launcher.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def assert_untouched(self):
        self.assertEqual((self.root / "marker").read_text(), "old-root"); self.assertEqual((self.data / "marker").read_text(), "old-data")
        self.assertFalse(self.stage.exists(), "the staged copy was removed")
        self.assertFalse(self.journal.exists())

    # ---- the normal course ----
    def test_a_detached_restore_commits_and_prints_the_same_lines_as_an_inline_one(self):
        self.mode("ctl-step", "0.1")
        rc, out, err = self.finish(self.start("--foreground"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.launcher_calls(), [])
        self.assertEqual(state_value(self.archived(), "runner"), "inline")
        self.assertIsNone(state_value(self.archived(), "launched"))
        inline_out, inline_err = self.normalise(out), self.normalise(err)
        self.assertIn("Restore completed", inline_out)
        self.tearDown_fixture_and_rebuild()
        rc, out, err = self.finish(self.start())
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.normalise(out), inline_out)
        self.assertEqual(phase_of(self.archived()), "committed")
        self.assertEqual(state_value(self.archived(), "runner"), "worker")
        self.assertRegex(state_value(self.archived(), "launched"), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIn("runs in the background", err)
        self.assertRegex(err, r"(?m)^Restore: ")
        self.assertEqual((self.root / "marker").read_text(), "old-root")  # the forward part was only simulated
        calls = self.launcher_calls(); self.assertEqual(len(calls), 1)
        args = calls[0]
        self.assertEqual(args[-4:], ["/bin/bash", f"{self.journal}/recover", "worker", HEX_ID])
        self.assertIn(f"--description=VesselStack restore {STAMP}", args)
        for needed in ("--unit=run-vesselstack-restore", "--collect", "--quiet"):
            self.assertIn(needed, args)
        for prop in ("Type=exec", "KillMode=mixed", "TimeoutStopSec=10min", "After=docker.service", "IgnoreOnIsolate=yes"):
            self.assertEqual(args[args.index(prop) - 1], "-p", prop)
        self.assertNotIn("--wait", args); self.assertNotIn("--pipe", args)
        self.assertFalse((self.base / "launcher-bad-args").exists())
        from common.lifecycle_lock import lock_path
        lock = os.stat(lock_path(self.root, self.base))
        for idents in (json.loads(line) for line in (self.base / "launcher-fds").read_text().splitlines()):
            self.assertNotIn([lock.st_dev, lock.st_ino], idents, "the lifecycle lock descriptor was passed to the launcher")

    def tearDown_fixture_and_rebuild(self):
        """Put the fixture back as it was before the restore (the archived record of the first run is dropped)."""
        shutil.rmtree(self.restore_dir, ignore_errors=True)
        for tree in (self.root, self.data):
            staged = self.stage / "vesselstack-backup" / str(tree).lstrip("/")
            staged.mkdir(parents=True, exist_ok=True); (staged / "marker").write_text("new")

    def test_the_worker_has_only_what_a_transient_unit_gets_and_the_foreground_gave_up_the_lock(self):
        from common.lifecycle_lock import LifecycleLock, LifecycleLockBusy
        self.mode("ctl-mode", "hang")
        proc = self.start()
        self.wait_for(self.base / "hanging")
        self.assertIsNone(proc.poll(), "the foreground is still following while the worker holds the lock")
        worker = self.worker_pid(); self.assertNotEqual(worker, proc.pid)
        envs = list(self.base.glob("worker-env.*")); self.assertEqual(len(envs), 1)
        names = {line.split("=", 1)[0] for line in envs[0].read_text().splitlines()}
        self.assertNotIn("VESSELSTACK_TEST_SYSTEMD_RUN", names); self.assertNotIn("VESSELSTACK_ENV", names)
        self.assertNotIn("VESSELSTACK_RESTORE_FOLLOW_INTERVAL", names)
        self.assertIn("VESSELSTACK_SYSTEM_ROOT", names)  # from the journal, through the recover wrapper
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VESSELSTACK_LIFECYCLE_LOCK_FD", None)
            with self.assertRaises(LifecycleLockBusy):
                LifecycleLock(self.root, self.base).acquire()
        self.mode("release", "")
        rc, out, err = self.finish(proc)
        self.assertEqual(rc, 0, err)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VESSELSTACK_LIFECYCLE_LOCK_FD", None)
            # The result file is written just before the worker (and the lock helper around it) exits.
            deadline = time.time() + 10
            while True:
                try:
                    LifecycleLock(self.root, self.base).acquire().release()
                    break
                except LifecycleLockBusy:
                    if time.time() > deadline:
                        raise
                    time.sleep(0.05)

    def test_a_failing_restore_passes_its_status_and_messages_through(self):
        results = {}
        for mode, status in (("fail1", 1), ("fail3", 3)):
            for foreground in (True, False):
                if results:
                    self.tearDown_fixture_and_rebuild()
                self.mode("ctl-mode", mode)
                rc, out, err = self.finish(self.start(*(["--foreground"] if foreground else [])))
                self.assertEqual(rc, status, err)
                self.assertIn("Restore failed: simulated failure", err)
                self.assertEqual(phase_of(self.archived("rolled-back")), "rolled-back")
                results[(mode, foreground)] = (rc, self.normalise(out))
            self.assertEqual(results[(mode, True)], results[(mode, False)])

    # ---- signals and re-attaching ----
    def test_a_signal_in_the_following_foreground_only_stops_following(self):
        for sig, code in ((signal.SIGINT, 130), (signal.SIGHUP, 129), (signal.SIGTERM, 143)):
            with self.subTest(sig=sig.name):
                if (self.base / "release").exists():
                    (self.base / "release").unlink(); (self.base / "hanging").unlink()
                    self.wait_for_lock_free()  # the previous worker (and the lock helper around it) has really ended
                    self.tearDown_fixture_and_rebuild()
                    for old in self.base.glob("worker-env.*"): old.unlink()
                self.mode("ctl-mode", "hang")
                proc = self.start()
                self.wait_for(self.base / "hanging")
                worker = self.worker_pid()
                os.kill(proc.pid, sig)
                rc, out, err = self.finish(proc)
                self.assertEqual(rc, code, err)
                self.assertIn("The restore continues in the background. Follow it: sudo vesselstackctl restore --status --follow", err)
                os.kill(worker, 0)  # the worker was not signalled
                self.mode("release", "")
                self.wait_for(self.restore_dir / f"worker-{HEX_ID}.rc")
                self.assertEqual((self.restore_dir / f"worker-{HEX_ID}.rc").read_text(), "0\n")
                self.assertEqual(phase_of(self.archived()), "committed")

    def wait_for_lock_free(self, timeout=60):
        """The lifecycle lock can be taken: the worker of an earlier run in this test has ended. Bounded."""
        from common.lifecycle_lock import LifecycleLock, LifecycleLockBusy
        deadline = time.time() + timeout
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VESSELSTACK_LIFECYCLE_LOCK_FD", None)
            while True:
                try:
                    LifecycleLock(self.root, self.base).acquire().release()
                    return
                except LifecycleLockBusy:
                    if time.time() > deadline:
                        self.fail("the lifecycle lock is still held by an earlier run\n" + self.diagnostics())
                    time.sleep(0.05)

    def wait_for_runner(self, timeout=30):
        """The journal exists and has been handed to a worker (runner=worker)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if state_value(self.journal, "runner") == "worker":
                    return
            except OSError:
                pass
            time.sleep(0.05)
        self.fail("the journal was not handed to a worker")

    def test_a_signal_in_the_launch_window_says_the_restore_may_still_start(self):
        self.mode("launcher-mode", "never")
        proc = self.start(VESSELSTACK_RESTORE_LAUNCH_WAIT="30")
        self.wait_for_runner()
        time.sleep(1)  # the follower has installed its traps
        os.kill(proc.pid, signal.SIGINT)
        rc, out, err = self.finish(proc)
        self.assertEqual(rc, 130, err)
        self.assertIn("The background restore has not started yet and may still start. Follow it: sudo vesselstackctl restore --status --follow", err)
        self.assertNotIn("No restore worker is running now", err)
        self.assertEqual(phase_of(self.journal), "prepared")  # nothing was cancelled

    def test_status_follow_waits_for_a_worker_in_the_launch_window(self):
        self.mode("launcher-mode", "never")
        proc = self.start(VESSELSTACK_RESTORE_LAUNCH_WAIT="30")
        self.wait_for_runner()
        proc.kill(); proc.wait()
        # no worker appears: the normal status and the way to roll back, after the launch wait
        start = time.time()
        rc, out, err = self.run_ctl("restore", "--status", "--follow", VESSELSTACK_RESTORE_LAUNCH_WAIT="2")
        self.assertGreaterEqual(time.time() - start, 1.0)  # whole seconds: it can fire up to one second early
        self.assertEqual(rc, 0, err)
        self.assertIn("A restore did not finish.", out)
        self.assertIn(f"sudo {self.journal}/recover rollback", out)
        # a worker that appears during the wait is followed to its end
        self.mode("ctl-mode", "hang")
        def late_worker():
            time.sleep(1)
            env = {"PATH": self.env()["PATH"], "VESSELSTACK_BOOT_ID_FILE": _BOOT_FILE}
            self.procs.append(subprocess.Popen(["/usr/bin/env", "-i", *[f"{k}={v}" for k, v in env.items()], "bash", str(self.journal / "recover"), "worker", HEX_ID],
                                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True))
            self.wait_for(self.base / "hanging")
            self.mode("release", "")
        thread = threading.Thread(target=late_worker); thread.start()
        try:
            rc, out, err = self.run_ctl("restore", "--status", "--follow", VESSELSTACK_RESTORE_LAUNCH_WAIT="20")
        finally:
            thread.join()
        self.assertEqual(rc, 0, err)
        self.assertIn("Restore completed", out)

    def test_a_missing_boot_id_file_prints_nothing_to_stderr(self):
        self.mode("ctl-mode", "hang")
        proc = self.start()
        self.wait_for(self.base / "hanging"); self.worker_pid()
        proc.kill(); proc.wait()
        rc, out, err = self.run_ctl("status", VESSELSTACK_BOOT_ID_FILE=str(self.base / "no-such-boot-id"))
        self.assertIn("Restore              RUNNING (phase stopping)", out)
        self.assertNotIn("No such file", err)
        self.assertEqual(err, "")

    def test_status_follow_attaches_to_a_running_worker_and_then_reports_the_last_result(self):
        self.mode("ctl-mode", "hang")
        proc = self.start()
        self.wait_for(self.base / "hanging"); self.worker_pid()
        proc.kill(); proc.wait()  # the connection dropped
        rc, out, err = self.run_ctl("restore", "--status")
        first = out.splitlines()[0]
        self.assertEqual(first, "A restore is running in the background (phase: stopping). Follow it: sudo vesselstackctl restore --status --follow")
        rc, out, err = self.run_ctl("status")
        self.assertEqual(rc, 1)
        self.assertIn("Restore              RUNNING (phase stopping); follow: sudo vesselstackctl restore --status --follow", out)
        rc, out, err = self.run_ctl("doctor")
        self.assertIn("WARN restore RUNNING (phase stopping); follow: sudo vesselstackctl restore --status --follow", err)
        self.assertNotIn("FAIL restore", err)
        rc, out, err = self.run_ctl("restore-recover", "status")
        self.assertEqual(out.splitlines()[0], "A restore is running in the background (phase: stopping). Follow it: sudo vesselstackctl restore --status --follow")
        timer = threading.Timer(1.0, lambda: (self.base / "release").write_text("")); timer.start()
        try:
            rc, out, err = self.run_ctl("restore", "--status", "--follow")
        finally:
            timer.join()
        self.assertEqual(rc, 0, err)
        self.assertIn("Restore completed", out)
        rc, out, err = self.run_ctl("restore", "--status", "--follow")
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines()[0], "No restore is pending.")
        self.assertRegex(out.splitlines()[1], r"^The last restore that ran in the background completed at \d{4}-")
        self.assertEqual(self.run_ctl("restore", "--status")[1], "No restore is pending.\n")
        # a failed one is reported with its status, and a follow of it ends with that status
        self.tearDown_fixture_and_rebuild()
        for path in self.restore_dir.glob("worker-*"): path.unlink()
        self.mode("ctl-mode", "fail3")
        self.assertEqual(self.finish(self.start())[0], 3)
        rc, out, err = self.run_ctl("restore", "--status", "--follow")
        self.assertRegex(out.splitlines()[1], r"^The last restore that ran in the background failed \(exit status 3\) at ")

    def test_status_follow_is_not_refused_by_the_lock_and_needs_no_pending_restore(self):
        text = CONTROLLER.read_text()
        a = text.index("command_needs_lifecycle_lock() {")
        body = text[a:text.index("\n}\n", a)]
        self.assertIn('[ "${2:-}" = --status ] && [ "${3:-}" = --follow ] && [ "$#" -eq 3 ] && return 1', body)
        self.assertEqual(self.run_ctl("restore", "--status", "--follow")[1], "No restore is pending.\n")
        self.assertEqual(self.run_ctl("restore", "--status", "--bogus")[0], 2)

    # ---- the hand-off goes wrong ----
    def test_a_launcher_that_fails_cancels_the_restore_and_changes_nothing(self):
        self.mode("launcher-mode", "fail")
        rc, out, err = self.finish(self.start())
        self.assertEqual(rc, 1, err)
        self.assertIn("The restore could not be started in the background: Failed to start transient service", err)
        self.assertIn("--foreground", err)
        self.assertEqual(phase_of(self.archived("rolled-back")), "rolled-back")
        self.assert_untouched()
        self.assertEqual(list(self.restore_dir.glob("worker-*")), [])

    def test_a_launcher_that_fails_after_the_worker_started_leaves_a_late_worker_harmless(self):
        self.mode("launcher-mode", "start-then-fail")
        rc, out, err = self.finish(self.start())
        self.assertEqual(rc, 1, err)
        self.assert_untouched()
        time.sleep(3)  # the worker has been waiting for the lock; it now gets it, finds no restore and ends
        self.assertEqual(list(self.restore_dir.glob("worker-*")), [])
        self.assertFalse(self.journal.exists())
        self.assertEqual(phase_of(self.archived("rolled-back")), "rolled-back")

    def test_a_worker_that_never_appears_is_cancelled_after_the_wait(self):
        self.mode("launcher-mode", "never")
        start = time.time()
        rc, out, err = self.finish(self.start(VESSELSTACK_RESTORE_LAUNCH_WAIT="2"))
        self.assertEqual(rc, 1, err)
        # the follower counts whole seconds with $SECONDS, so it can fire up to one second early
        self.assertGreaterEqual(time.time() - start, 2 - 1)
        self.assertIn("The restore did not start in the background. Nothing was changed", err)
        self.assert_untouched()
        self.assertEqual(phase_of(self.archived("rolled-back")), "rolled-back")

    def test_the_cancel_never_touches_a_restore_that_is_no_longer_prepared(self):
        install = lambda: (self.restore_dir.mkdir(mode=0o700, exist_ok=True), shutil.copytree(self.base / "prebuilt", self.journal, symlinks=True))
        install()
        subprocess.run([sys.executable, "-I", str(self.journal / "restore_journal.py"), "set", str(self.journal), "phase=data-swap"], check=True)
        rc, out, err = self.run_ctl("restore-recover", "unlaunched", HEX_ID)
        self.assertEqual(rc, 4, err)
        self.assertEqual(phase_of(self.journal), "data-swap"); self.assertTrue(self.stage.exists())
        for other in ("0" * 32, "bad"):
            self.assertEqual(self.run_ctl("restore-recover", "unlaunched", other)[0], 4)
        self.assertEqual(phase_of(self.journal), "data-swap")
        # prepared, but a worker already wrote its pid: also left alone
        subprocess.run([sys.executable, "-I", str(self.journal / "restore_journal.py"), "set", str(self.journal), "phase=prepared"], check=True)
        (self.restore_dir / f"worker-{HEX_ID}.pid").write_text("1\n")
        self.assertEqual(self.run_ctl("restore-recover", "unlaunched", HEX_ID)[0], 4)
        self.assertEqual(phase_of(self.journal), "prepared")
        (self.restore_dir / f"worker-{HEX_ID}.pid").unlink()
        rc, out, err = self.run_ctl("restore-recover", "unlaunched", HEX_ID)
        self.assertEqual(rc, 0, err)
        self.assertFalse(self.journal.exists()); self.assertFalse(self.stage.exists())

    def test_a_worker_that_is_killed_is_reported_and_the_restore_can_be_rolled_back(self):
        self.mode("ctl-mode", "hang")
        proc = self.start()
        self.wait_for(self.base / "hanging")
        os.kill(self.worker_pid(), signal.SIGKILL)
        rc, out, err = self.finish(proc)
        self.assertEqual(rc, 1, err)
        self.assertIn(f"The restore worker stopped in phase stopping the running stack; no folder had been moved. Run: sudo {self.journal}/recover status", err)
        self.assertEqual(phase_of(self.journal), "stopping")
        result = subprocess.run([str(self.journal / "recover"), "rollback"], capture_output=True, text=True, timeout=60,
                                env=self.env(VESSELSTACK_ROOT="/wrong"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Rollback completed", result.stdout)
        self.assertFalse(self.journal.exists())
        self.assertEqual((self.root / "marker").read_text(), "old-root")

    def test_a_second_restore_while_one_runs_is_refused_before_anything_happens(self):
        self.mode("ctl-mode", "hang")
        proc = self.start()
        self.wait_for(self.base / "hanging"); self.worker_pid()
        rc, out, err = self.run_ctl("restore", "y.tar.gz", "--yes")
        self.assertEqual(rc, 1)
        self.assertIn("A restore did not finish (phase: stopping). Nothing else can change this system until it is resolved.", err)
        self.assertEqual(len(self.launcher_calls()), 1)
        self.assertEqual(phase_of(self.journal), "stopping")
        self.mode("release", "")
        self.assertEqual(self.finish(proc)[0], 0)

    # ---- when it still runs in the terminal ----
    def test_without_systemd_or_a_launcher_the_restore_runs_inline(self):
        shutil.rmtree(self.base / "run/systemd/system")
        rc, out, err = self.finish(self.start())
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.launcher_calls(), [])
        self.assertEqual(state_value(self.archived(), "runner"), "inline")
        self.tearDown_fixture_and_rebuild()
        (self.base / "run/systemd/system").mkdir(parents=True)
        rc, out, err = self.finish(self.start(VESSELSTACK_TEST_SYSTEMD_RUN=str(self.base / "no-such-launcher")))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.launcher_calls(), [])
        self.assertEqual(state_value(self.archived(), "runner"), "inline")
        self.tearDown_fixture_and_rebuild()
        env = self.env(); env.pop("VESSELSTACK_TEST_SYSTEMD_RUN")
        proc = subprocess.run(["bash", str(self.ctl), "restore", "x.tar.gz", "--yes"], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.launcher_calls(), [])

    def test_the_decision_to_detach_is_one_function(self):
        text = CONTROLLER.read_text()
        a = text.index("restore_can_detach() {"); body = text[a:text.index("\n}\n", a)]
        for needle in ('restore_foreground', "restore_systemd_running", "command -v systemd-run", "VESSELSTACK_TEST_SYSTEMD_RUN"):
            self.assertIn(needle, body)

    def test_a_signal_before_the_journal_exists_removes_the_stage_and_leaves_no_journal(self):
        for sig, code in ((signal.SIGINT, 130), (signal.SIGHUP, 129), (signal.SIGTERM, 143)):
            with self.subTest(sig=sig.name):
                self.mode("early-hold", "")
                proc = self.start()
                time.sleep(1.0)
                self.assertIsNone(proc.poll())
                os.kill(proc.pid, sig)
                rc, out, err = self.finish(proc)
                self.assertEqual(rc, code, err)
                self.assertIn("interrupted before anything was changed", err)
                self.assertFalse(self.stage.exists()); self.assertFalse(self.journal.exists())
                self.assertEqual((self.root / "marker").read_text(), "old-root")
                (self.base / "early-hold").unlink()
                self.tearDown_fixture_and_rebuild()

    # ---- a restore is "running" only while its worker process is ----
    def install_pending(self, phase="stopping"):
        self.restore_dir.mkdir(mode=0o700, exist_ok=True)
        shutil.copytree(self.base / "prebuilt", self.journal, symlinks=True)
        subprocess.run([sys.executable, "-I", str(self.journal / "restore_journal.py"), "set", str(self.journal), f"phase={phase}"], check=True)

    def unrelated_process(self):
        proc = subprocess.Popen(["sleep", "120"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.procs.append(proc)  # its own session: the clean-up may signal its group
        return proc

    def assert_not_running(self, label):
        rc, out, err = self.run_ctl("doctor")
        self.assertIn("FAIL restore did not finish (phase stopping)", err, label)
        self.assertNotIn("RUNNING", err, label)
        rc, out, err = self.run_ctl("status")
        self.assertIn("Restore              INCOMPLETE (phase stopping)", out, label)
        rc, out, err = self.run_ctl("restore-recover", "status")
        self.assertEqual(out.splitlines()[0], "A restore did not finish.", label)
        self.assertIn(f"sudo {self.journal}/recover rollback", out, label)
        rc, out, err = self.run_ctl("restore", "--status", "--follow", timeout=30)
        self.assertEqual(out.splitlines()[0], "A restore did not finish.", label)
        self.assertNotIn("Follow it", out, label)

    def test_a_pid_file_that_does_not_belong_to_a_live_worker_is_not_a_running_restore(self):
        self.install_pending(); other = self.unrelated_process()
        dead = subprocess.Popen(["true"]); dead.wait()
        pidfile = self.restore_dir / f"worker-{HEX_ID}.pid"
        cases = {"another boot, a live unrelated pid": f"{other.pid} 11111111-2222-4333-8444-555555555555\n",
                 "this boot, a live unrelated pid": f"{other.pid} {BOOT_ID}\n",
                 "an old file with only a pid, unrelated": f"{other.pid}\n",
                 "this boot, a dead pid": f"{dead.pid} {BOOT_ID}\n",
                 "not a pid": f"abc {BOOT_ID}\n",
                 "too many fields": f"{other.pid} {BOOT_ID} x\n"}
        for label, text in cases.items():
            with self.subTest(label):
                pidfile.write_text(text)
                self.assert_not_running(label)

    def test_the_genuine_worker_is_running_with_either_form_of_pid_file(self):
        self.mode("ctl-mode", "hang")
        proc = self.start()
        self.wait_for(self.base / "hanging")
        pid = self.worker_pid()
        pidfile = self.restore_dir / f"worker-{HEX_ID}.pid"
        self.assertEqual(pidfile.read_text(), f"{pid} {BOOT_ID}\n")
        for text in (f"{pid} {BOOT_ID}\n", f"{pid}\n", f"{pid} -\n"):
            with self.subTest(text=text):
                pidfile.write_text(text)
                rc, out, err = self.run_ctl("restore-recover", "status")
                self.assertEqual(out.splitlines()[0], "A restore is running in the background (phase: stopping). Follow it: sudo vesselstackctl restore --status --follow")
        pidfile.write_text(f"{pid} 11111111-2222-4333-8444-555555555555\n")
        self.assertEqual(self.run_ctl("restore-recover", "status")[1].splitlines()[0], "A restore did not finish.")
        pidfile.write_text(f"{pid} {BOOT_ID}\n")
        self.mode("release", "")
        self.assertEqual(self.finish(proc)[0], 0)

    def test_the_pid_file_holds_a_dash_when_the_boot_id_cannot_be_read(self):
        self.mode("ctl-mode", "hang")
        proc = self.start(VESSELSTACK_BOOT_ID_FILE=str(self.base / "no-boot-id"))
        self.wait_for(self.base / "hanging")
        pid = self.worker_pid()
        self.assertEqual((self.restore_dir / f"worker-{HEX_ID}.pid").read_text(), f"{pid} -\n")
        self.assertIn("A restore is running in the background", self.run_ctl("restore-recover", "status")[1])
        self.mode("release", "")
        self.assertEqual(self.finish(proc)[0], 0)

    # ---- the follower never waits for ever ----
    def test_a_follower_ends_when_the_restore_was_rolled_back_from_another_terminal(self):
        self.mode("launcher-mode", "never")
        proc = self.start(VESSELSTACK_RESTORE_LAUNCH_WAIT="4")
        self.wait_for(self.base / "launcher.log")
        for _ in range(50):
            rc, out, err = self.run_ctl("restore-recover", "rollback")
            if rc == 0:
                break
            time.sleep(0.1)
        self.assertEqual(rc, 0, err)
        rc, out, err = self.finish(proc, timeout=30)
        self.assertEqual(rc, 1, err)
        self.assertIn("The restore is no longer pending: it was rolled back or cancelled by another command. See: sudo vesselstackctl restore --status", err)
        self.assertNotIn("did not start in the background", err)

    def test_a_follower_ends_when_the_restore_was_abandoned_from_another_terminal(self):
        self.mode("launcher-mode", "never")
        proc = self.start(VESSELSTACK_RESTORE_LAUNCH_WAIT="4")
        self.wait_for(self.base / "launcher.log")
        for _ in range(50):
            rc, out, err = self.run_ctl("restore-recover", "abandon", "--yes")
            if rc == 0:
                break
            time.sleep(0.1)
        self.assertEqual(rc, 0, err)
        rc, out, err = self.finish(proc, timeout=30)
        self.assertEqual(rc, 1, err)
        self.assertIn("The restore is no longer pending", err)

    def test_the_cancel_is_tried_three_times_through_the_journals_own_controller(self):
        self.mode("launcher-mode", "never")
        calls = self.base / "controller-calls"
        stub = self.base / "prebuilt" / "controller"
        stub.write_text(f'#!/bin/bash\necho "$@" >> "{calls}"\nexit 75\n')
        rc, out, err = self.finish(self.start(VESSELSTACK_RESTORE_LAUNCH_WAIT="1"), timeout=60)
        self.assertEqual(rc, 1, err)
        self.assertIn(f"The restore did not start in the background and could not be cancelled; run: sudo {self.journal}/recover status", err)
        self.assertEqual(calls.read_text().splitlines(), [f"restore-recover unlaunched {HEX_ID}"] * 3)
        self.assertEqual(phase_of(self.journal), "prepared")

    def test_the_cancel_runs_the_journals_controller_not_the_one_that_follows(self):
        self.mode("launcher-mode", "never")
        calls = self.base / "controller-calls"
        (self.base / "prebuilt" / "controller").write_text(f'#!/bin/bash\necho "$@" >> "{calls}"\nexit 0\n')
        rc, out, err = self.finish(self.start(VESSELSTACK_RESTORE_LAUNCH_WAIT="1"), timeout=60)
        self.assertEqual(rc, 1, err)
        self.assertIn("The restore did not start in the background. Nothing was changed", err)
        self.assertEqual(calls.read_text().splitlines(), [f"restore-recover unlaunched {HEX_ID}"])

    def test_an_attached_follower_ends_when_the_worker_is_killed(self):
        self.mode("ctl-mode", "hang")
        first = self.start()
        self.wait_for(self.base / "hanging")
        worker = self.worker_pid()
        attached = self.start(command=("restore", "--status", "--follow"))
        time.sleep(1.0)
        self.assertIsNone(attached.poll())
        os.kill(worker, signal.SIGKILL)
        rc, out, err = self.finish(attached, timeout=30)
        self.assertEqual(rc, 1, err)
        self.assertIn("The restore worker stopped in phase", err)
        self.assertEqual(self.finish(first, timeout=30)[0], 1)

    def test_a_signal_while_no_worker_runs_does_not_claim_one_continues(self):
        # a worker that started (pid file written) and was killed without writing its result, signalled
        # while the follower is still inside its dead-worker grace period
        self.mode("ctl-mode", "hang")
        proc = self.start(VESSELSTACK_RESTORE_DEAD_GRACE="60")
        self.wait_for(self.base / "hanging")
        os.kill(self.worker_pid(), signal.SIGKILL)
        time.sleep(1.5)
        self.assertIsNone(proc.poll())
        self.assertFalse((self.restore_dir / f"worker-{HEX_ID}.rc").exists())
        os.kill(proc.pid, signal.SIGTERM)
        rc, out, err = self.finish(proc, timeout=30)
        self.assertEqual(rc, 143, err)
        self.assertNotIn("continues in the background", err)
        self.assertNotIn("may still start", err)
        self.assertIn("Stopped following. No restore worker is running now. See: sudo vesselstackctl restore --status", err)

    def test_the_follower_reads_the_journal_again_only_when_its_state_file_changed(self):
        text = CONTROLLER.read_text()
        a = text.index("restore_follow() {"); body = text[a:text.index("\n}\n", a)]
        self.assertIn("restore_state_stamp", body)
        self.assertEqual(body.count("restore_state_peek"), 2)
        self.assertIn('stat -c', text[text.index("restore_state_stamp() {"):text.index("restore_follow_signal() {")])
        self.assertIn('stat -f', text[text.index("restore_state_stamp() {"):text.index("restore_follow_signal() {")])

    # ---- the result of the last background restore ----
    def test_status_follow_reports_a_result_only_for_the_newest_set_of_worker_files(self):
        self.restore_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        old_id, new_id = "a" * 32, "b" * 32
        for ext in ("pid", "out", "err", "rc"):
            path = self.restore_dir / f"worker-{old_id}.{ext}"; path.write_text("0\n" if ext == "rc" else "x")
            os.utime(path, (1_000_000, 1_000_000))
        pid = self.restore_dir / f"worker-{new_id}.pid"; pid.write_text("1 -\n"); os.utime(pid, (2_000_000, 2_000_000))
        rc, out, err = self.run_ctl("restore", "--status", "--follow", timeout=30)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.splitlines()[0], "No restore is pending.")
        self.assertEqual(out.splitlines()[1], "The last background restore did not run to the end or was cancelled; no result was recorded for it.")
        self.assertNotIn("completed", out)
        result = self.restore_dir / f"worker-{new_id}.rc"; result.write_text("3\n"); os.utime(result, (2_000_100, 2_000_100))
        rc, out, err = self.run_ctl("restore", "--status", "--follow", timeout=30)
        self.assertRegex(out.splitlines()[1], r"^The last restore that ran in the background failed \(exit status 3\) at ")

    def test_cleanup_never_signals_a_pid_read_from_a_worker_pid_file(self):
        """The clean-up once read worker-*.pid and killpg(1, SIGKILL) took down the whole CI runner. It must not signal
        any pid a test planted there, nor 0/1, the test runner, its parent or any group of theirs, whatever else is written."""
        self.restore_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        planted = {"1 -": "a" * 32, f"{os.getpid()} -": "b" * 32, f"{os.getppid()} {BOOT_ID}": "c" * 32, f"{os.getpgrp()}": "d" * 32}
        for text, ident in planted.items():
            (self.restore_dir / f"worker-{ident}.pid").write_text(text + "\n")
        forbidden = {0, 1, os.getpid(), os.getppid(), os.getpgrp()}
        calls = []
        def record(kind):
            return lambda target, sig: calls.append((kind, target, sig))
        with mock.patch.object(os, "kill", record("kill")), mock.patch.object(os, "killpg", record("killpg")):
            self.stop_everything()
            self.assertEqual(calls, [], "the pid files were used to signal something")
            # even a launcher record that names such pids (it never does) cannot make the clean-up signal them
            (self.base / "started-workers.log").write_text("".join(f"{pid} {pgid}\n" for pid in forbidden for pgid in forbidden) + "junk\n")
            with mock.patch.object(type(self), "own_process", lambda self, pid: True):
                self.stop_everything()
        self.assertEqual([call for call in calls if call[1] in forbidden or call[1] < 0], [], calls)
        (self.base / "started-workers.log").unlink()

if __name__ == "__main__":
    unittest.main()
