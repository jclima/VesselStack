"""Recovery at boot: scripts/lib/restore-boot driven with a temp SYSTEM_ROOT, a fake `recover` and a boot-id seam file."""
from pathlib import Path
import os, shutil, signal, stat, subprocess, tempfile, time, unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/lib/restore-boot"
UNIT = ROOT / "systemd/boot-vesselstack-restore.service.in"
CONTROLLER = ROOT / "scripts/vesselstackctl"

BOOT_NOW = "11111111-2222-4333-8444-555555555555"
BOOT_BEFORE = "99999999-8888-4777-8666-555555555555"

FAKE_RECOVER = """#!/bin/bash
# Fake journal recover: logs its arguments and behaves as the files next to it say.
dir=$(cd "$(dirname "$0")" && pwd)
echo "$*" >> "$CALLS_LOG"
if [ "$1" = status ]; then echo "FAKE-STATUS"; exit 0; fi
echo "FAKE-RECOVER-OUTPUT"
if [ -f "$dir/sleep" ]; then exec sleep "$(cat "$dir/sleep")"; fi
if [ -f "$dir/rcs" ]; then
    rc=$(sed -n 1p "$dir/rcs"); sed -n '2,$p' "$dir/rcs" > "$dir/rcs.n"; mv "$dir/rcs.n" "$dir/rcs"
    [ -n "$rc" ] || rc=0
else
    rc=$(cat "$dir/rc" 2>/dev/null || echo 0)
fi
if [ -f "$dir/touch" ]; then touch "$(cat "$dir/touch")"; fi
if [ "$rc" = 0 ] && [ -f "$dir/archive" ]; then mv "$dir/../current" "$dir/../archived"; fi
exit "$rc"
"""


def have_timeout():
    return shutil.which("timeout") is not None


class BootCase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        self.base = Path(td.name)
        self.dir = self.base / "var/lib/vesselstack/restore"
        self.journal = self.dir / "current"
        self.dir.mkdir(parents=True)
        self.calls = self.base / "calls.log"
        self.boot_file = self.base / "boot_id"; self.boot_file.write_text(BOOT_NOW + "\n")
        (self.base / "etc").mkdir()
        self.shim = None
        if not have_timeout():
            # macOS without coreutils: a minimal timeout(1) so the script can run here. It is only a stand-in.
            self.shim = self.base / "shim"; self.shim.mkdir()
            (self.shim / "timeout").write_text(
                '#!/bin/bash\nif [ "$1" = -k ]; then shift 2; fi\nlimit=$1; shift\n"$@" & pid=$!\n'
                '( sleep "${limit%[smh]}"; kill -TERM $pid 2>/dev/null ) & killer=$!\nrc=0; wait $pid || rc=$?\n'
                'kill $killer 2>/dev/null; [ $rc -eq 143 ] && rc=124; exit $rc\n')
            (self.shim / "timeout").chmod(0o755)

    # ---- fixtures ----
    def make_journal(self, phase="starting", boot=BOOT_BEFORE, jid="abc123", rollback_from=None, kind=None, extra="", with_recover=True, state=True):
        self.journal.mkdir(parents=True, exist_ok=True)
        if state:
            lines = [f"format=1", f"id={jid}", f"phase={phase}"]
            if boot is not None: lines.append(f"boot_id={boot}")
            if rollback_from is not None: lines.append(f"rollback_from={rollback_from}")
            if kind is not None: lines.append(f"last_error_kind={kind}")
            (self.journal / "state").write_text("\n".join(lines) + "\n" + extra)
        if with_recover:
            (self.journal / "recover").write_text(FAKE_RECOVER)
            (self.journal / "recover").chmod(0o700)

    def conf(self, text):
        (self.base / "etc/vesselstack-recovery.conf").write_text(text)

    def env(self, **more):
        env = dict(os.environ)
        env.update(SYSTEM_ROOT=str(self.base), VESSELSTACK_BOOT_ID_FILE=str(self.boot_file),
                   RESTORE_BOOT_TIMEOUT="20", RESTORE_BOOT_BUSY_WAIT="0", RESTORE_BOOT_BUSY_TRIES="3", CALLS_LOG=str(self.calls))
        if self.shim: env["PATH"] = f"{self.shim}:{env['PATH']}"
        env.update(more)
        return env

    def run_boot(self, boot=None, **more):
        if boot is not None: self.boot_file.write_text(boot + "\n")
        result = subprocess.run(["/bin/bash", str(SCRIPT)], env=self.env(**more), capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def record(self):
        path = self.dir / "boot-recovery"
        if not path.exists(): return None
        values = {}
        for line in path.read_text().splitlines():
            key, _, value = line.partition("=")
            self.assertNotIn(key, values, "a key is written once")
            values[key] = value
        return values

    def calls_made(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def log(self):
        return (self.dir / "boot-recovery.log").read_text()

    def mode_of(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)


class DecisionTable(BootCase):
    ROLLBACK_PHASES = ("prepared", "stopping", "stopped", "data-swap", "root-swap", "externals", "reloaded", "starting")

    def test_every_rollback_phase_rolls_back_in_rollback_mode(self):
        for phase in self.ROLLBACK_PHASES:
            with self.subTest(phase=phase):
                self.calls.write_text(""); (self.dir / "boot-recovery").unlink(missing_ok=True)
                self.make_journal(phase)
                self.run_boot()
                self.assertEqual(self.calls_made(), ["rollback"])
                rec = self.record()
                self.assertEqual((rec["action"], rec["result"], rec["phase_before"], rec["rc"]), ("rollback", "rolled-back", phase, "0"))

    def test_every_rollback_phase_is_held_in_hold_mode(self):
        self.conf("MODE=hold\n")
        for phase in self.ROLLBACK_PHASES + ("rolling-back", "rollback-failed"):
            with self.subTest(phase=phase):
                self.calls.write_text("")
                self.make_journal(phase, rollback_from="starting", kind="retry")
                out = self.run_boot()
                self.assertNotIn("rollback", self.calls_made())
                rec = self.record()
                self.assertEqual((rec["action"], rec["result"]), ("hold", "held"))
                self.assertIn("automatic rollback is turned off", rec["reason"])
                self.assertIn("HELD", out.stdout)

    def test_committed_and_rolled_back_are_cleaned_up_in_both_modes(self):
        for mode in ("rollback", "hold"):
            for phase in ("committed", "rolled-back"):
                with self.subTest(mode=mode, phase=phase):
                    self.calls.write_text(""); (self.dir / "boot-recovery").unlink(missing_ok=True)
                    self.conf(f"MODE={mode}\n")
                    self.make_journal(phase)
                    self.run_boot()
                    self.assertEqual(self.calls_made(), ["rollback"])
                    rec = self.record()
                    self.assertEqual((rec["action"], rec["result"]), ("cleanup", "cleaned-up"))

    def test_an_abandoned_journal_is_cleaned_up_in_rollback_mode_and_held_in_hold_mode(self):
        self.conf("MODE=rollback\n"); self.make_journal("abandoned"); self.run_boot()
        self.assertEqual(self.calls_made(), ["rollback"])
        self.assertEqual((self.record()["action"], self.record()["result"]), ("cleanup", "cleaned-up"))
        shutil.rmtree(self.journal); self.calls.write_text(""); (self.dir / "boot-recovery").unlink()
        self.conf("MODE=hold\n"); self.make_journal("abandoned"); out = self.run_boot()
        self.assertEqual(self.calls_made(), ["status"], "recover rollback would start the stopped services")
        rec = self.record()
        self.assertEqual((rec["action"], rec["result"], rec["attempts"]), ("hold", "held", "0"))
        self.assertIn("automatic recovery is turned off", rec["reason"]); self.assertIn("not started", rec["reason"])
        self.assertIn("HELD", out.stdout)

    def test_rolling_back_needs_rollback_from(self):
        self.make_journal("rolling-back", rollback_from="root-swap")
        self.run_boot()
        self.assertEqual(self.calls_made(), ["rollback"])
        self.calls.write_text(""); shutil.rmtree(self.journal)
        self.make_journal("rolling-back")
        self.run_boot()
        self.assertEqual(self.calls_made(), ["status"])
        self.assertIn("does not say where the rollback started", self.record()["reason"])
        shutil.rmtree(self.journal); self.calls.write_text("")
        self.make_journal("rolling-back", rollback_from="")
        self.run_boot()
        self.assertEqual(self.calls_made(), ["status"])

    def test_rollback_failed_retries_only_for_kind_retry_and_below_two_attempts(self):
        for kind, attempts, expect_run, reason in (
                ("retry", 0, True, None), ("retry", 1, True, None), ("retry", 2, False, "tried 2 times"),
                ("manual", 0, False, "a retry will not help"), ("previous", 0, False, "a retry will not help"),
                ("", 0, False, "a retry will not help")):
            with self.subTest(kind=kind, attempts=attempts):
                if self.journal.exists(): shutil.rmtree(self.journal)
                self.calls.write_text("")
                (self.dir / "boot-recovery").unlink(missing_ok=True)
                if attempts:
                    (self.dir / "boot-recovery").write_text(f"journal_id=abc123\nattempts={attempts}\n")
                self.make_journal("rollback-failed", rollback_from="starting", kind=kind)
                self.run_boot()
                rec = self.record()
                if expect_run:
                    self.assertEqual(self.calls_made(), ["rollback"]); self.assertEqual(rec["attempts"], str(attempts + 1))
                else:
                    self.assertEqual(self.calls_made(), ["status"]); self.assertIn(reason, rec["reason"]); self.assertEqual(rec["action"], "hold")
        shutil.rmtree(self.journal); self.calls.write_text("")
        self.make_journal("rollback-failed", kind="retry")
        self.run_boot()
        self.assertEqual(self.calls_made(), ["status"])
        self.assertIn("does not say where the rollback started", self.record()["reason"])

    def test_starting_adds_a_note(self):
        self.make_journal("starting"); self.run_boot()
        self.assertIn("restored folders are kept as *.failed-restore-", self.record()["note"])
        shutil.rmtree(self.journal); self.make_journal("stopped"); self.run_boot()
        self.assertEqual(self.record()["note"], "")


class SameBootAndUnreadable(BootCase):
    def test_a_journal_written_in_this_boot_is_left_alone(self):
        self.make_journal("root-swap", boot=BOOT_NOW)
        out = self.run_boot()
        self.assertEqual(self.calls_made(), [])
        self.assertIsNone(self.record())
        self.assertIn("may be running", out.stdout)
        self.assertIn("no action", self.log())
        self.assertEqual(self.mode_of(self.dir / "boot-recovery.log"), 0o600)

    def test_a_journal_without_a_boot_id_is_held(self):
        self.make_journal("root-swap", boot=None)
        self.run_boot()
        self.assertEqual(self.calls_made(), ["status"])
        self.assertIn("does not say which boot wrote it", self.record()["reason"])

    def test_this_boots_id_must_be_readable(self):
        for text in (None, "", "not-a-uuid\n"):
            with self.subTest(text=text):
                if self.journal.exists(): shutil.rmtree(self.journal)
                self.calls.write_text("")
                if text is None: self.boot_file.unlink(missing_ok=True)
                else: self.boot_file.write_text(text)
                self.make_journal("root-swap")
                self.run_boot()
                self.assertEqual(self.calls_made(), ["status"])
                self.assertIn("cannot read this boot's id", self.record()["reason"])

    def test_an_unreadable_journal_is_held_and_rollback_is_never_called(self):
        cases = {
            "no state": dict(state=False),
            "empty state": dict(state=False, pre=("state", "")),
            "no recover": dict(with_recover=False),
            "empty recover": dict(with_recover=False, pre=("recover", "")),
            "repeated phase": dict(extra="phase=stopped\n"),
            "repeated id": dict(extra="id=other\n"),
            "repeated boot_id": dict(extra=f"boot_id={BOOT_BEFORE}\n"),
            "repeated rollback_from": dict(rollback_from="x", extra="rollback_from=y\n"),
            "unknown phase": dict(phase="exploding"),
            "malformed boot_id": dict(boot="nonsense"),
            "no id": dict(jid=""),
        }
        for name, spec in cases.items():
            with self.subTest(name):
                if self.journal.exists(): shutil.rmtree(self.journal)
                self.calls.write_text("")
                spec = dict(spec); pre = spec.pop("pre", None)
                self.make_journal(**spec)
                if pre: (self.journal / pre[0]).write_text(pre[1])
                out = self.run_boot()
                self.assertNotIn("rollback", self.calls_made())
                rec = self.record()
                self.assertEqual((rec["action"], rec["result"]), ("hold", "held"))
                self.assertIn("the journal cannot be read", rec["reason"])
                self.assertIn("HELD", out.stdout)

    def test_no_journal_is_not_a_recovery(self):
        out = self.run_boot()
        self.assertIsNone(self.record()); self.assertEqual(self.calls_made(), [])
        self.assertIn("nothing to do", out.stdout)

    def test_recover_status_output_lands_in_the_log_on_a_hold(self):
        self.make_journal("root-swap", boot=None); self.run_boot()
        self.assertIn("FAKE-STATUS", self.log())


class RunsAndRecord(BootCase):
    def test_results_follow_the_exit_status(self):
        for rc, result, extra in ((0, "rolled-back", {}), (3, "rolled-back-not-started", {}), (1, "failed", {}), (5, "failed", {}), (124, "timed-out", {}), (137, "timed-out", {})):
            with self.subTest(rc=rc):
                if self.journal.exists(): shutil.rmtree(self.journal)
                (self.dir / "boot-recovery").unlink(missing_ok=True)
                self.make_journal("starting"); (self.journal / "rc").write_text(str(rc))
                out = self.run_boot()
                rec = self.record()
                self.assertEqual((rec["rc"], rec["result"]), (str(rc), result))
                self.assertEqual(rec["phase_after"], "starting"); self.assertTrue(rec["finished"].endswith("Z")); self.assertTrue(rec["finished_epoch"].isdigit())
                self.assertIn("FAKE-RECOVER-OUTPUT", self.log())
                self.assertTrue(out.stdout.strip())

    def test_a_timeout_is_recorded(self):
        self.make_journal("starting"); (self.journal / "sleep").write_text("30")
        started = time.time()
        self.run_boot(RESTORE_BOOT_TIMEOUT="1")
        self.assertLess(time.time() - started, 25)
        rec = self.record()
        self.assertEqual((rec["rc"], rec["result"]), ("124", "timed-out"))

    def test_archived_journal_is_phase_after_archived(self):
        self.make_journal("starting"); (self.journal / "archive").write_text("")
        self.run_boot()
        self.assertEqual(self.record()["phase_after"], "archived"); self.assertFalse(self.journal.exists())

    def test_the_attempt_is_counted_before_recover_runs(self):
        self.make_journal("starting"); (self.journal / "sleep").write_text("30")
        proc = subprocess.Popen(["/bin/bash", str(SCRIPT)], env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            deadline = time.time() + 20
            while time.time() < deadline and not (self.dir / "boot-recovery").exists():
                time.sleep(0.05)
            deadline = time.time() + 20
            while time.time() < deadline and "rollback" not in self.calls_made():
                time.sleep(0.05)
            rec = self.record()
            self.assertEqual(rec["attempts"], "1"); self.assertEqual(rec["rc"], ""); self.assertEqual(rec["finished"], "")
            self.assertEqual((rec["action"], rec["phase_before"], rec["journal_id"], rec["boot_id"]), ("rollback", "starting", "abc123", BOOT_NOW))
            self.assertRegex(rec["started"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            os.killpg(proc.pid, signal.SIGKILL)  # a power loss in the middle: everything is gone, the record stays
            proc.wait()
        finally:
            if proc.poll() is None: os.killpg(proc.pid, signal.SIGKILL); proc.wait()
            proc.stdout.close(); proc.stderr.close()
        rec = self.record()
        self.assertEqual((rec["attempts"], rec["rc"], rec["result"]), ("1", "", ""))
        # the next boot sees one attempt already made
        shutil.rmtree(self.journal); self.make_journal("rollback-failed", rollback_from="starting", kind="retry")
        self.run_boot()
        self.assertEqual(self.record()["attempts"], "2")

    def test_a_different_journal_id_resets_the_count(self):
        (self.dir / "boot-recovery").write_text("journal_id=old\nattempts=2\n")
        self.make_journal("rollback-failed", jid="new", rollback_from="starting", kind="retry")
        self.run_boot()
        self.assertEqual(self.record()["attempts"], "1"); self.assertEqual(self.calls_made(), ["rollback"])

    def test_lock_busy_three_times_is_a_hold_and_not_an_attempt(self):
        self.make_journal("starting"); (self.journal / "rcs").write_text("75\n75\n75\n")
        (self.dir / "boot-recovery").write_text("journal_id=abc123\nattempts=1\n")
        out = self.run_boot()
        self.assertEqual(self.calls_made(), ["rollback"] * 3 + ["status"])
        rec = self.record()
        self.assertEqual((rec["action"], rec["result"], rec["attempts"]), ("hold", "held", "1"))
        self.assertIn("another VesselStack operation holds the lock", rec["reason"]); self.assertIn("HELD", out.stdout)
        self.assertEqual(self.log().count("FAKE-RECOVER-OUTPUT"), 3, "the log keeps the output of the busy tries")

    def test_lock_busy_then_free_succeeds(self):
        self.make_journal("starting"); (self.journal / "rcs").write_text("75\n0\n")
        self.run_boot()
        rec = self.record()
        self.assertEqual((rec["result"], rec["attempts"], rec["rc"]), ("rolled-back", "1", "0"))
        self.assertEqual(self.calls_made(), ["rollback", "rollback"])

    def test_record_and_log_are_private_and_the_log_is_truncated_each_run(self):
        self.make_journal("starting"); (self.dir / "boot-recovery.log").write_text("OLD LOG\n")
        os.chmod(self.dir / "boot-recovery.log", 0o644)
        self.run_boot()
        self.assertNotIn("OLD LOG", self.log())
        self.assertEqual(self.mode_of(self.dir / "boot-recovery"), 0o600); self.assertEqual(self.mode_of(self.dir / "boot-recovery.log"), 0o600)
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith(".boot-recovery.")], [], "no temp file is left behind")

    def stub_dir(self, name, body):
        stubs = self.base / "stubs"; stubs.mkdir(exist_ok=True)
        (stubs / name).write_text("#!/bin/bash\n" + body); (stubs / name).chmod(0o755)
        return stubs

    def run_with_stubs(self, stubs):
        env = self.env(); env["PATH"] = f"{stubs}:{env['PATH']}"
        return subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)

    def test_an_internal_error_is_recorded_and_fails_the_unit(self):
        self.make_journal("starting")
        result = self.run_with_stubs(self.stub_dir("date", "exit 1\n"))
        self.assertNotEqual(result.returncode, 0)
        rec = self.record()
        self.assertEqual(rec["result"], "internal-error"); self.assertIn("stopped with status", rec["reason"]); self.assertEqual(self.calls_made(), [])

    def test_a_record_that_cannot_be_written_after_the_run_does_not_turn_it_into_an_error(self):
        self.make_journal("starting")
        flag = self.base / "fail-next-mktemp"
        (self.journal / "touch").write_text(str(flag))  # the fake recover arms the stub while it runs
        stubs = self.stub_dir("mktemp", f'if [ -e "{flag}" ]; then rm -f "{flag}"; exit 1; fi\nexec "{shutil.which("mktemp")}" "$@"\n')
        result = self.run_with_stubs(stubs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls_made(), ["rollback"])
        self.assertIn("cannot be written after the run", self.log()); self.assertIn("exited with status 0 (rolled-back)", self.log())
        self.assertIn("could not be written", result.stderr)
        rec = self.record()
        self.assertEqual((rec["attempts"], rec["rc"], rec["result"]), ("1", "", ""), "the record written before the run stays")

    def test_a_record_that_cannot_be_written_before_the_run_means_no_rollback_but_a_clean_unit(self):
        self.make_journal("starting")
        result = self.run_with_stubs(self.stub_dir("mktemp", "exit 1\n"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls_made(), [], "no rollback without a recorded attempt")
        self.assertIn("NOT run", result.stderr); self.assertIn("is the disk full", result.stderr)
        self.assertIn("nothing was run", self.log()); self.assertIsNone(self.record())


class AttemptLimit(BootCase):
    def boots(self, n):
        return [f"{i:08x}-0000-4000-8000-000000000000" for i in range(1, n + 1)]

    def test_a_rollback_that_never_ends_is_tried_three_times_and_then_held(self):
        for phase in ("rolling-back", "data-swap", "starting", "prepared", "externals"):
            with self.subTest(phase=phase):
                if self.journal.exists(): shutil.rmtree(self.journal)
                (self.dir / "boot-recovery").unlink(missing_ok=True); self.calls.write_text("")
                self.make_journal(phase, rollback_from="data-swap" if phase == "rolling-back" else None)
                (self.journal / "rc").write_text("1")  # recover fails, or is killed (137): the journal stays as it was
                for n, boot in enumerate(self.boots(3), 1):
                    self.run_boot(boot=boot)
                    rec = self.record()
                    self.assertEqual((rec["attempts"], rec["action"], rec["result"]), (str(n), "rollback", "failed"))
                self.assertEqual(self.calls_made(), ["rollback"] * 3)
                out = self.run_boot(boot=self.boots(4)[3])
                self.assertEqual(self.calls_made(), ["rollback"] * 3 + ["status"], "recover rollback is not called a fourth time")
                rec = self.record()
                self.assertEqual((rec["action"], rec["result"], rec["attempts"]), ("hold", "held", "3"))
                self.assertIn("tried 3 times", rec["reason"]); self.assertIn("limit is 3", rec["reason"])
                self.assertIn("HELD", out.stdout)
                self.run_boot(boot=self.boots(5)[4])
                self.assertEqual(self.calls_made().count("rollback"), 3)

    def test_a_killed_rollback_is_counted_too(self):
        self.make_journal("rolling-back", rollback_from="data-swap"); (self.journal / "rc").write_text("137")
        for n, boot in enumerate(self.boots(3), 1):
            self.run_boot(boot=boot)
            rec = self.record(); self.assertEqual((rec["attempts"], rec["rc"], rec["result"]), (str(n), "137", "timed-out"))
        self.run_boot(boot=self.boots(4)[3])
        self.assertEqual(self.calls_made().count("rollback"), 3); self.assertEqual(self.record()["action"], "hold")

    def test_a_rollback_that_hangs_is_counted_and_limited(self):
        self.make_journal("rolling-back", rollback_from="starting"); (self.journal / "sleep").write_text("30")
        for n, boot in enumerate(self.boots(3), 1):
            self.run_boot(boot=boot, RESTORE_BOOT_TIMEOUT="1")
            self.assertEqual((self.record()["attempts"], self.record()["result"]), (str(n), "timed-out"))
        self.run_boot(boot=self.boots(4)[3], RESTORE_BOOT_TIMEOUT="1")
        self.assertEqual(self.calls_made().count("rollback"), 3); self.assertEqual(self.record()["result"], "held")

    def test_rollback_failed_keeps_its_limit_of_two(self):
        self.make_journal("rollback-failed", rollback_from="starting", kind="retry"); (self.journal / "rc").write_text("1")
        for n, boot in enumerate(self.boots(2), 1):
            self.run_boot(boot=boot); self.assertEqual(self.record()["attempts"], str(n))
        self.run_boot(boot=self.boots(3)[2])
        self.assertEqual(self.calls_made().count("rollback"), 2)
        rec = self.record(); self.assertEqual((rec["action"], rec["attempts"]), ("hold", "2")); self.assertIn("limit after a failed rollback is 2", rec["reason"])

    def test_a_different_journal_id_resets_the_limit(self):
        self.make_journal("rolling-back", rollback_from="starting"); (self.journal / "rc").write_text("1")
        (self.dir / "boot-recovery").write_text("journal_id=abc123\nattempts=3\n")
        self.run_boot(); self.assertEqual(self.calls_made(), ["status"])
        shutil.rmtree(self.journal); self.calls.write_text("")
        self.make_journal("rolling-back", jid="another", rollback_from="starting"); (self.journal / "rc").write_text("1")
        self.run_boot()
        self.assertEqual(self.calls_made(), ["rollback"]); self.assertEqual(self.record()["attempts"], "1")

    def test_a_cleanup_that_keeps_failing_is_capped_as_well(self):
        for phase in ("committed", "rolled-back", "abandoned"):
            with self.subTest(phase=phase):
                if self.journal.exists(): shutil.rmtree(self.journal)
                (self.dir / "boot-recovery").unlink(missing_ok=True); self.calls.write_text("")
                self.make_journal(phase); (self.journal / "rc").write_text("1")
                for n, boot in enumerate(self.boots(3), 1):
                    self.run_boot(boot=boot)
                    rec = self.record(); self.assertEqual((rec["action"], rec["result"], rec["attempts"]), ("cleanup", "failed", str(n)))
                self.run_boot(boot=self.boots(4)[3])
                self.assertEqual(self.calls_made().count("rollback"), 3)
                rec = self.record(); self.assertEqual((rec["action"], rec["result"]), ("hold", "held"))
                self.assertIn("cleanup was already tried 3 times", rec["reason"])

    def test_a_busy_lock_is_not_an_attempt_and_does_not_use_up_the_limit(self):
        self.make_journal("rolling-back", rollback_from="starting")
        for boot in self.boots(5):
            (self.journal / "rcs").write_text("75\n75\n75\n")
            self.run_boot(boot=boot)
            self.assertEqual(self.record()["attempts"], "0"); self.assertEqual(self.record()["result"], "held")

    def test_the_attempts_value_must_be_a_plain_number(self):
        for value, expect_run in (("0", True), ("2", True), ("002", False), ("+1", False), ("1x", False), ("", False), ("1000", False), ("-1", False), (" 1", False), ("3", False)):
            with self.subTest(value=value):
                if self.journal.exists(): shutil.rmtree(self.journal)
                self.calls.write_text("")
                (self.dir / "boot-recovery").write_text(f"journal_id=abc123\nattempts={value}\n")
                self.make_journal("rolling-back", rollback_from="starting"); self.run_boot()
                self.assertEqual("rollback" in self.calls_made(), expect_run)
        # a base-10 reading: "08" and "09" are not octal accidents, they are refused as unreadable
        shutil.rmtree(self.journal); self.calls.write_text("")
        (self.dir / "boot-recovery").write_text("journal_id=abc123\nattempts=08\n")
        self.make_journal("rolling-back", rollback_from="starting"); self.run_boot()
        self.assertEqual(self.calls_made(), ["status"])

    def test_the_lock_is_waited_for_with_the_defaults_of_ten_tries(self):
        self.make_journal("starting"); (self.journal / "rcs").write_text("75\n" * 10)
        self.run_boot(RESTORE_BOOT_BUSY_TRIES="10")
        self.assertEqual(self.calls_made(), ["rollback"] * 10 + ["status"])
        text = SCRIPT.read_text()
        self.assertIn("busy_wait=${RESTORE_BOOT_BUSY_WAIT:-30}", text); self.assertIn("busy_tries=${RESTORE_BOOT_BUSY_TRIES:-10}", text)


class ModeFile(BootCase):
    def mode_runs(self, text):
        if self.journal.exists(): shutil.rmtree(self.journal)
        self.calls.write_text(""); (self.dir / "boot-recovery").unlink(missing_ok=True)
        self.conf(text)
        self.make_journal("starting"); self.run_boot()
        return "rollback" in self.calls_made()

    def test_mode_parsing(self):
        bom = "\ufeff"
        for text, expect_run in (("MODE=rollback\n", True), ("MODE=hold\n", False), ("MODE=hold", False), ("", False), ("hold\n", False),
                                 ("# comment\nMODE=hold\n", False), ("# comment\nMODE=rollback\n\n", True), ("MODE = hold\n", False), ("MODE = rollback\n", False),
                                 ("# only a comment\n", False), ("\n\n", False), ("mode=hold\n", False), ("mode=rollback\n", False),
                                 (" MODE=hold\n", False), (" MODE=rollback\n", True), ("export MODE=hold\n", False), ("export MODE=rollback\n", False),
                                 (bom + "MODE=hold\n", False), (bom + "MODE=rollback\n", True), ("MODE=rollback\r\n", True), ("MODE=hold\r\n", False),
                                 ("MODE=hold # c\n", False), ("MODE=rollback # c\n", True), ("MODE=rollback #c\n", False), ("OTHER=1\nMODE=rollback\n", False),
                                 ("MODE=x\n", False), ("MODE=rollback\nMODE=rollback\n", False),
                                 ('MODE="hold"\n', False), ("MODE='hold'\n", False), ('MODE="rollback"\n', True), ("MODE='rollback'\n", True),
                                 ("MODE=hold \n", False), ("MODE=  hold\n", False), ('MODE= "hold" \n', False), ('MODE= "rollback" \n', False)):
            with self.subTest(text=text):
                self.assertEqual(self.mode_runs(text), expect_run)

    def test_every_file_that_is_not_one_valid_line_says_why_in_the_log(self):
        for text, why in (("", "no MODE= line"), ("# c\n", "no MODE= line"), ("MODE = hold\n", "not MODE=rollback or MODE=hold"), ("mode=hold\n", "not MODE=rollback or MODE=hold"),
                          ("export MODE=hold\n", "export MODE=hold"), ("MODE=x\n", "neither MODE=rollback nor MODE=hold")):
            with self.subTest(text=text):
                self.assertFalse(self.mode_runs(text))
                self.assertIn(why, self.log()); self.assertIn("treated as MODE=hold", self.log())
                self.assertEqual(self.record()["mode"], "hold")

    def test_a_mode_file_that_cannot_be_read_holds(self):
        if os.geteuid() == 0: self.skipTest("root reads everything")
        self.conf("MODE=rollback\n"); os.chmod(self.base / "etc/vesselstack-recovery.conf", 0)
        self.make_journal("starting"); self.run_boot()
        self.assertEqual(self.calls_made(), ["status"]); self.assertIn("cannot be read", self.log())

    def test_a_missing_file_means_rollback(self):
        self.make_journal("starting"); self.run_boot()
        self.assertEqual(self.calls_made(), ["rollback"]); self.assertEqual(self.record()["mode"], "rollback")

    def test_a_file_with_a_bad_value_or_two_mode_lines_fails_closed_and_names_the_value(self):
        for text, shown in (("MODE=garbage\n", "MODE=garbage"), ("MODE=HOLD\n", "MODE=HOLD"), ("MODE=Rollback\n", "MODE=Rollback"), ("MODE=\n", "MODE="),
                            ('MODE="hold\n', 'MODE="hold'), ("MODE='rollback\"\n", "MODE='rollback\""), ("MODE=\"\"\n", 'MODE=""'),
                            ('MODE=" hold"\n', 'MODE=" hold"'), ("MODE=hold hold\n", "MODE=hold hold"),
                            ("MODE=rollback\nMODE=rollback\n", "more than one MODE= line"), ("MODE=hold\nMODE=rollback\n", "more than one MODE= line"),
                            ("MODE=rollback\nMODE=hold\n", "more than one MODE= line")):
            with self.subTest(text=text):
                self.assertFalse(self.mode_runs(text), "a bad mode file holds")
                rec = self.record()
                self.assertEqual((rec["mode"], rec["action"]), ("hold", "hold"))
                self.assertIn("treated as MODE=hold", self.log()); self.assertIn(shown, self.log())

    def test_a_bad_value_is_sanitised_in_the_log(self):
        self.assertFalse(self.mode_runs("MODE=ab\x01cd\x1b[31m\n"))
        self.assertNotIn("\x01", self.log()); self.assertNotIn("\x1b", self.log()); self.assertIn("MODE=ab?cd?[31m", self.log())

    def test_a_mode_file_that_is_a_directory_holds(self):
        self.make_journal("starting"); (self.base / "etc/vesselstack-recovery.conf").mkdir()
        self.run_boot()
        self.assertEqual(self.calls_made(), ["status"]); self.assertIn("not a regular file", self.log())

    def test_a_mode_file_that_is_not_a_regular_file_holds(self):
        self.make_journal("starting")
        target = self.base / "elsewhere"; target.write_text("MODE=rollback\n")
        (self.base / "etc/vesselstack-recovery.conf").symlink_to(target)
        self.run_boot()
        self.assertEqual(self.calls_made(), ["status"]); self.assertIn("not a regular file", self.log())

    def test_the_file_is_never_executed(self):
        planted = self.base / "planted"
        self.conf(f"MODE=hold\ntouch {planted}\n$(touch {planted})\nMODE=`touch {planted}`\n")
        self.make_journal("starting"); self.run_boot()
        self.assertFalse(planted.exists()); self.assertEqual(self.record()["action"], "hold")


class HardeningFollowUps(BootCase):
    def stub_dir(self, name, body):
        stubs = self.base / "stubs"; stubs.mkdir(exist_ok=True)
        (stubs / name).write_text("#!/bin/bash\n" + body); (stubs / name).chmod(0o755)
        return stubs

    def test_a_record_that_is_a_directory_holds_and_is_not_written_into(self):
        (self.dir / "boot-recovery").mkdir(); self.make_journal("starting")
        out = self.run_boot()
        self.assertEqual(self.calls_made(), ["status"]); self.assertIn("HELD", out.stdout)
        self.assertTrue((self.dir / "boot-recovery").is_dir()); self.assertEqual(list((self.dir / "boot-recovery").iterdir()), [])
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith(".boot-recovery.")], [])
        self.assertIn("not a regular file", self.log())

    def test_a_record_that_is_a_symlink_holds_and_the_target_is_untouched(self):
        target = self.base / "elsewhere"; target.write_text("journal_id=abc123\nattempts=0\n")
        (self.dir / "boot-recovery").symlink_to(target); self.make_journal("starting")
        out = self.run_boot()
        self.assertEqual(self.calls_made(), ["status"]); self.assertIn("HELD", out.stdout)
        self.assertEqual(target.read_text(), "journal_id=abc123\nattempts=0\n")
        self.assertIn("not a regular file", self.log())

    def test_status_75_with_an_unchanged_state_is_still_a_busy_lock(self):
        self.make_journal("starting"); (self.journal / "rcs").write_text("75\n75\n75\n")
        self.run_boot()
        rec = self.record(); self.assertEqual((rec["result"], rec["attempts"]), ("held", "0"))

    def test_status_75_from_a_rollback_that_changed_the_state_is_an_attempt_that_failed(self):
        self.make_journal("starting"); (self.journal / "rcs").write_text("75\n0\n")
        (self.journal / "touch").write_text(str(self.journal / "state-marker"))
        # the fake recover rewrites the state file as a running rollback would
        (self.journal / "recover").write_text(FAKE_RECOVER.replace('exit "$rc"', 'echo "x=1" >> "$dir/state"\nexit "$rc"'))
        out = self.run_boot()
        self.assertEqual(self.calls_made(), ["rollback"], "no retry in this boot")
        rec = self.record()
        self.assertEqual((rec["result"], rec["rc"], rec["attempts"]), ("failed", "75", "1"))
        self.assertIn("changed the journal state", rec["reason"]); self.assertNotIn("HELD", out.stdout)

    def test_the_attempt_is_flushed_to_disk_before_the_run(self):
        self.make_journal("starting")
        synclog = self.base / "sync.log"
        stubs = self.stub_dir("sync", f'echo "$*" >> "{synclog}"\necho "attempts=$(sed -n s/^attempts=//p "{self.dir}/boot-recovery")" >> "{synclog}"\nexit 0\n')
        env = self.env(); env["PATH"] = f"{stubs}:{env['PATH']}"
        result = subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = synclog.read_text().splitlines()
        self.assertEqual(lines[0], f"-- {self.dir}/boot-recovery {self.dir}"); self.assertEqual(lines[1], "attempts=1", "the record was written before the sync")

    def test_the_timed_out_reasons(self):
        self.make_journal("starting"); (self.journal / "rc").write_text("137"); self.run_boot()
        self.assertIn("was killed", self.record()["reason"])
        shutil.rmtree(self.journal); (self.dir / "boot-recovery").unlink(); self.make_journal("starting"); (self.journal / "rc").write_text("124"); self.run_boot()
        self.assertIn("did not finish within", self.record()["reason"])


class UnitAndInventory(unittest.TestCase):
    def test_the_unit_has_the_designed_directives(self):
        text = UNIT.read_text()
        for line in ("ConditionPathExists=/var/lib/vesselstack/restore/current", "RefuseManualStart=yes", "After=local-fs.target docker.service",
                     "Wants=docker.service", "Type=oneshot", "RemainAfterExit=yes", "ExecStart=/usr/local/lib/vesselstack-recovery/restore-boot",
                     "TimeoutStartSec=40min", "WantedBy=multi-user.target"):
            self.assertIn(line + "\n", text)
        self.assertNotIn("network-online", text); self.assertNotIn("@", text)

    def test_neither_name_is_known_or_inventoried_by_the_restore(self):
        text = CONTROLLER.read_text()
        a = text.index("restore_known_units() {"); b = text.index("restore_external_validate_path() {", a)
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            systemd = base / "etc/systemd/system"; (systemd / "multi-user.target.wants").mkdir(parents=True)
            (systemd / "vesselstack-chat.service").write_text("x")
            (systemd / "boot-vesselstack-restore.service").write_text("planted")
            (systemd / "multi-user.target.wants/boot-vesselstack-restore.service").symlink_to("../boot-vesselstack-restore.service")
            (base / "usr/local/lib/vesselstack-recovery").mkdir(parents=True)
            (base / "usr/local/lib/vesselstack-recovery/restore-boot").write_text("planted")
            script = f'''SYSTEM_ROOT="{base}"
SYSTEMD_DIR="$SYSTEM_ROOT/etc/systemd/system"
SBIN_DIR="$SYSTEM_ROOT/usr/local/sbin"
LIBEXEC_DIR="$SYSTEM_ROOT/usr/local/libexec/vesselstack"
{text[a:b]}
echo KNOWN; restore_known_units; echo CANDIDATES; restore_external_candidates; echo HELPERS; restore_helper_map
'''
            out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
        self.assertIn("vesselstack-chat.service", out)
        self.assertNotIn("boot-vesselstack-restore", out); self.assertNotIn("vesselstack-recovery", out); self.assertNotIn("restore-boot", out)


class StatusAndDoctorLines(unittest.TestCase):
    """restore_status_line and check_restore_state, run from the controller's own source."""
    FINISHED = "2026-05-01T10:20:30Z"

    def setUp(self):
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        self.base = Path(td.name)
        self.dir = self.base / "var/lib/vesselstack/restore"; self.dir.mkdir(parents=True)
        self.boot_file = self.base / "boot_id"; self.boot_file.write_text(BOOT_NOW + "\n")

    def record(self, **values):
        data = dict(journal_id="abc123", attempts="1", started=self.FINISHED, finished=self.FINISHED, finished_epoch=str(int(time.time()) - 60),
                    phase_before="starting", phase_after="archived", boot_id=BOOT_BEFORE, mode="rollback", action="rollback", rc="0",
                    result="rolled-back", reason="", note="")
        data.update(values)
        (self.dir / "boot-recovery").write_text("".join(f"{k}={v}\n" for k, v in data.items()))

    def pending(self, phase="starting", jid="abc123"):
        import sys
        current = self.dir / "current"; current.mkdir()
        shutil.copy(ROOT / "scripts/lib/restore_journal.py", current / "restore_journal.py")
        subprocess.run([sys.executable, "-I", str(current / "restore_journal.py"), "set", str(current), "format=1", f"id={jid}", f"phase={phase}"],
                       check=True, env=dict(os.environ, VESSELSTACK_BOOT_ID_FILE=str(self.boot_file)))

    def run_function(self, name):
        text = CONTROLLER.read_text()
        a = text.index("restore_state_peek() {"); b = text.index("ensure_lifecycle_lock() {", a)
        c = text.index("# FAIL (returns 1) when a restore did not finish"); d = text.index("# WARN about restore leftovers", c)
        script = f'''RESTORE_DIR="{self.dir}"
RESTORE_CURRENT="$RESTORE_DIR/current"
export VESSELSTACK_BOOT_ID_FILE="{self.boot_file}"
{text[a:b]}
{text[c:d]}
{name} && echo "RC=0" || echo "RC=$?"
'''
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        return result.stdout, result.stderr

    def test_status_line_for_each_result(self):
        cases = (
            ({"result": "rolled-back"}, "%-20s rolled back automatically at boot (%s)" % ("Restore", self.FINISHED)),
            ({"result": "rolled-back", "note": "the restore may have finished except for its last step"}, "the restore may have finished except for its last step)"),
            ({"result": "rolled-back-not-started", "rc": "3"}, "rolled back at boot (%s) but the stack was not started; run: sudo vesselstackctl start" % self.FINISHED),
            ({"result": "cleaned-up"}, "the leftovers of a finished restore were cleaned up at boot (%s)" % self.FINISHED),
        )
        for values, expected in cases:
            with self.subTest(values=values):
                self.record(**values)
                out, _ = self.run_function("restore_status_line")
                self.assertIn(expected, out); self.assertIn("RC=0", out)

    def test_status_line_for_held_failed_and_timed_out_needs_the_pending_journal(self):
        self.pending("starting")
        for result, reason, expected in (
                ("held", "automatic rollback is turned off", f"HELD at boot: automatic rollback is turned off; see: sudo {self.dir}/current/recover status"),
                ("failed", "x", f"automatic rollback failed at boot (see {self.dir}/boot-recovery.log); see: sudo {self.dir}/current/recover status"),
                ("timed-out", "x", f"automatic rollback at boot timed out (see {self.dir}/boot-recovery.log)"),
                ("internal-error", "x", "automatic rollback failed at boot")):
            with self.subTest(result=result):
                self.record(result=result, reason=reason, phase_after="starting")
                out, _ = self.run_function("restore_status_line")
                self.assertIn(expected, out)
                self.assertIn("INCOMPLETE (phase starting)", out); self.assertIn("RC=1", out, "the pending restore still fails the status")

    def test_the_record_is_shown_by_these_rules_only(self):
        # a hold with no pending journal (resolved by hand): nothing
        self.record(result="held", reason="x"); out, _ = self.run_function("restore_status_line"); self.assertNotIn("HELD", out)
        # a record about another journal than the pending one: nothing
        self.pending("starting", jid="other"); self.record(result="held", reason="x")
        out, _ = self.run_function("restore_status_line"); self.assertNotIn("HELD", out)
        shutil.rmtree(self.dir / "current")
        # rolled back: this boot, or less than two days ago; older and from another boot: nothing
        self.record(finished_epoch="1000", boot_id=BOOT_BEFORE); out, _ = self.run_function("restore_status_line"); self.assertNotIn("rolled back", out)
        self.record(finished_epoch="1000", boot_id=BOOT_NOW); out, _ = self.run_function("restore_status_line"); self.assertIn("rolled back automatically", out)
        self.record(finished_epoch=str(int(time.time()) - 3 * 86400)); out, _ = self.run_function("restore_status_line"); self.assertNotIn("rolled back", out)
        # garbage: nothing, and no crash
        for junk in ("result=nonsense\n", "\x00\x01 garbage", "result=held\nresult=held\n", ""):
            (self.dir / "boot-recovery").write_text(junk)
            out, err = self.run_function("restore_status_line"); self.assertEqual(out.strip(), "RC=0", (junk, out, err))
        # a symbolic link is not read
        (self.dir / "boot-recovery").unlink(); (self.dir / "elsewhere").write_text("result=rolled-back\nfinished=x\n")
        (self.dir / "boot-recovery").symlink_to(self.dir / "elsewhere")
        out, _ = self.run_function("restore_status_line"); self.assertNotIn("rolled back", out)

    def test_unprintable_values_are_neutralised(self):
        self.pending("starting")
        self.record(result="held", reason="bad\x1b[31m\x07thing")
        out, _ = self.run_function("restore_status_line")
        self.assertNotIn("\x1b", out); self.assertNotIn("\x07", out); self.assertIn("HELD at boot: bad?[31m?thing", out)

    def test_doctor_lines(self):
        self.record(result="rolled-back", note="the restore may have finished except for its last step")
        out, err = self.run_function("check_restore_state")
        self.assertIn("INFO an interrupted restore was rolled back automatically at boot (%s); the previous folders are back and the stack was started" % self.FINISHED, out)
        self.assertIn("the restore may have finished except for its last step", out); self.assertIn("RC=0", out)
        self.record(result="rolled-back-not-started", rc="3")
        out, err = self.run_function("check_restore_state")
        self.assertIn("WARN an interrupted restore was rolled back at boot (%s) but the stack was not started" % self.FINISHED, err)
        self.assertIn("run: sudo vesselstackctl start", err); self.assertIn("RC=0", out)
        self.record(result="cleaned-up")
        out, err = self.run_function("check_restore_state"); self.assertIn("INFO the leftovers of a finished restore were cleaned up at boot", out)
        self.pending("starting")
        for result, expected in (("held", "INFO the automatic recovery at boot held and changed nothing: why"),
                                 ("failed", f"INFO the automatic rollback at boot failed; see {self.dir}/boot-recovery.log"),
                                 ("timed-out", f"INFO the automatic rollback at boot timed out; see {self.dir}/boot-recovery.log")):
            self.record(result=result, reason="why", phase_after="starting")
            out, err = self.run_function("check_restore_state")
            self.assertIn(expected, out)
            self.assertIn("FAIL restore did not finish (phase starting)", err); self.assertIn("RC=1", out, "a pending restore stays a FAIL")


if __name__ == "__main__":
    unittest.main()
