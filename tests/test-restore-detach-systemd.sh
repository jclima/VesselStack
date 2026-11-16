#!/usr/bin/env bash
set -euo pipefail

# Real-systemd acceptance for the launcher that `vesselstackctl restore` uses to hand the folder swap to a
# transient unit.  It runs the product's own argument list (read out of restore_launch_worker) against a
# harmless stand-in payload; it never runs a restore and never touches a VesselStack installation.
#
# Opt-in: it starts and stops a transient unit on the host's systemd manager.  CI runs it on a fresh Ubuntu VM.
if [[ "${VESSELSTACK_SYSTEMD_RESTORE_TEST:-false}" != "true" ]]; then
    echo "SKIP: set VESSELSTACK_SYSTEMD_RESTORE_TEST=true to run the real-systemd restore launch test"
    exit 0
fi

TEST_FAILED=0
fail() { TEST_FAILED=1; echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }
[[ "$(uname -s)" == "Linux" ]] || fail "this test requires Linux"
[[ "$(id -u)" == 0 ]] || fail "this test requires root"
command -v systemctl >/dev/null || fail "systemctl is required"
command -v systemd-run >/dev/null || fail "systemd-run is required"
command -v setsid >/dev/null || fail "setsid is required"
[[ "$(ps -p 1 -o comm= | tr -d ' ')" == "systemd" ]] || fail "PID 1 is not systemd"

UNIT_NAME=run-vesselstack-restore
UNIT=$UNIT_NAME.service
# Refuse before changing anything when the fixed unit name is in use.
load=$(systemctl show -p LoadState --value "$UNIT" 2>/dev/null || true)
active=$(systemctl show -p ActiveState --value "$UNIT" 2>/dev/null || true)
[[ "$load" == "not-found" ]] || fail "$UNIT is already loaded (LoadState=${load:-unknown}); not touching it"
[[ -z "$active" || "$active" == "inactive" ]] || fail "$UNIT is already active ($active); not touching it"

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CTL=$REPO_ROOT/scripts/vesselstackctl
[[ -f "$CTL" ]] || fail "scripts/vesselstackctl not found"

FIXTURE_BASE=/root
[[ -d "$FIXTURE_BASE" ]] || fail "trusted fixture base does not exist: $FIXTURE_BASE"
base_uid=$(stat -c '%u' -- "$FIXTURE_BASE")
base_mode_int=$((8#$(stat -c '%a' -- "$FIXTURE_BASE")))
[[ "$base_uid" == 0 ]] || fail "trusted fixture base is not root-owned"
(( (base_mode_int & 18) == 0 )) || fail "trusted fixture base is group/other-writable"
FIXTURE="$FIXTURE_BASE/vesselstack-restore-detach-test-$(cat /proc/sys/kernel/random/uuid)"

STARTED=0
LAUNCHER_PGID=""
cleanup() {
    set +e
    if [[ "$TEST_FAILED" == 1 && -d "${FIXTURE:-}" ]]; then
        echo "diagnostics: fixture files" >&2
        ls -l "$FIXTURE" >&2
        tail -n 5 "$FIXTURE/payload.log" "$FIXTURE/heartbeat" >&2
        journalctl -u "$UNIT" -n 30 --no-pager >&2
    fi
    [[ -n "$LAUNCHER_PGID" ]] && kill -KILL -- "-$LAUNCHER_PGID" >/dev/null 2>&1
    if [[ "$STARTED" == 1 ]]; then
        timeout 40 systemctl stop "$UNIT" >/dev/null 2>&1
        systemctl reset-failed "$UNIT" >/dev/null 2>&1
    fi
    rm -rf -- "$FIXTURE"
}
trap cleanup EXIT

install -d -m 0700 -o root -g root "$FIXTURE" "$FIXTURE/current"
[[ "$(stat -c '%u %a' -- "$FIXTURE")" == "0 700" ]] || fail "fixture root is not root-owned mode 0700"

echo "systemd: $(systemctl --version | head -n 1)"

# ---- Take the argument list from the product, strictly. ----------------------------------------------------
# The assignment `cmd=( ... )` inside restore_launch_worker is cut out, checked to contain no expansion other than
# the four variables the function provides, and evaluated with fixture values for those.
body=$(awk '/^restore_launch_worker\(\) \{/{f=1} f&&/cmd=\(/{c=1} c{print} c&&/\)[[:space:]]*$/{exit}' "$CTL")
[[ -n "$body" ]] || fail "could not find the cmd=(...) assignment in restore_launch_worker"
text=${body#*cmd=(}
text=$(printf '%s' "$text" | tr '\n' ' ')
text=${text%) }
text=${text%)}
leftover=$text
for v in launcher stamp RESTORE_CURRENT id; do leftover=${leftover//\"\$$v\"/}; leftover=${leftover//\$$v/}; done
# shellcheck disable=SC2016  # Literal characters to refuse, not expansions.
if [[ "$leftover" == *'$'* || "$leftover" == *'`'* || "$leftover" == *';'* || "$leftover" == *'|'* || "$leftover" == *'&'* \
      || "$leftover" == *'<'* || "$leftover" == *'>'* || "$leftover" == *'('* || "$leftover" == *')'* ]]; then
    fail "the launcher array contains an expansion or operator this test does not understand: $leftover"
fi
STAMP="restore-test-stamp"
ID="test-restore-id"
# shellcheck disable=SC2034  # Read by the evaluated array text.
launcher=systemd-run stamp=$STAMP RESTORE_CURRENT=$FIXTURE/current id=$ID
declare -a ARGS
eval "ARGS=($text)"
(( ${#ARGS[@]} >= 14 )) || fail "extracted launcher array is implausibly short (${#ARGS[@]} words)"
[[ "${ARGS[0]}" == systemd-run ]] || fail "first word of the launcher array is '${ARGS[0]}', expected systemd-run"

has_arg() { local a; for a in "${ARGS[@]}"; do [[ "$a" == "$1" ]] && return 0; done; return 1; }
prop_arg() { local i; for ((i = 0; i < ${#ARGS[@]} - 1; i++)); do
    [[ "${ARGS[i]}" == -p && "${ARGS[i+1]}" == "$1="* ]] && { printf '%s' "${ARGS[i+1]#*=}"; return 0; }; done; return 1; }
has_arg "--unit=$UNIT_NAME" || fail "extracted arguments lack --unit=$UNIT_NAME"
has_arg --collect || fail "extracted arguments lack --collect"
has_arg --quiet || fail "extracted arguments lack --quiet"
has_arg "--description=VesselStack restore $STAMP" || fail "extracted arguments lack the description with the stamp"
for k in Type KillMode TimeoutStopSec After IgnoreOnIsolate; do
    prop_arg "$k" >/dev/null || fail "extracted arguments lack -p $k=..."
done
[[ "$(prop_arg Type)" == exec ]] || fail "the product no longer asks for Type=exec"
[[ "$(prop_arg KillMode)" == mixed ]] || fail "the product no longer asks for KillMode=mixed"
[[ "$(prop_arg After)" == *docker.service* ]] || fail "the product no longer orders the unit after docker.service"
n=${#ARGS[@]}
[[ "${ARGS[n-4]}" == /bin/bash && "${ARGS[n-3]}" == "$FIXTURE/current/recover" && "${ARGS[n-2]}" == worker && "${ARGS[n-1]}" == "$ID" ]] \
    || fail "the payload part of the launcher array is not '/bin/bash \$RESTORE_CURRENT/recover worker <id>'"
pass "launcher arguments taken from scripts/vesselstackctl (${#ARGS[@]} words): ${ARGS[*]}"

# ---- Stand-in payload. -------------------------------------------------------------------------------------
# Behaviour is chosen by the file "mode" because the unit does not inherit this shell's environment.
cat > "$FIXTURE/current/recover" <<EOF
#!/bin/bash
D=$FIXTURE
echo \$\$ > "\$D/payload.pid"
env > "\$D/payload.env"
ls -l /proc/\$\$/fd > "\$D/payload.fd"
cat /proc/\$\$/cgroup > "\$D/payload.cgroup"
echo "args: \$*" > "\$D/payload.args"
termed=0
trap 'echo "TERM received" >> "\$D/payload.log"; termed=\$((termed + 1))' TERM
mode=\$(cat "\$D/mode" 2>/dev/null || echo normal)
if [ "\$mode" = exit1 ]; then
    echo hb >> "\$D/heartbeat"
    sleep 1
    echo "result exit=1" > "\$D/result"
    exit 1
fi
after_term=0
for _ in \$(seq 1 25); do
    echo "hb \$(date +%s)" >> "\$D/heartbeat"
    if [ "\$termed" -gt 0 ]; then
        after_term=\$((after_term + 1))
        if [ "\$after_term" -ge 2 ]; then
            echo "term-shutdown" > "\$D/result"
            exit 0
        fi
    fi
    sleep 1 &
    wait \$! || true
done
echo "timeout-finished" > "\$D/result"
exit 3
EOF
chmod 0755 "$FIXTURE/current/recover"
echo normal > "$FIXTURE/mode"
: > "$FIXTURE/heartbeat"

hb_count() { wc -l < "$FIXTURE/heartbeat" | tr -d ' '; }
wait_for() { # wait_for SECONDS CMD...
    local limit=$(($1 * 10)) i; shift
    for ((i = 0; i < limit; i++)); do "$@" && return 0; sleep 0.1; done
    return 1
}
hb_advanced_from() { (( $(hb_count) > $1 )); }
show() { systemctl show -p "$1" --value "$UNIT" 2>/dev/null; }
is_gone() { [[ "$(show LoadState)" == not-found ]]; }
pid_file_ready() { [[ -s "$FIXTURE/payload.pid" ]]; }
main_pid_set() { [[ "$(show MainPID)" =~ ^[1-9][0-9]*$ ]]; }

# ---- 1, 3, 4: launch from a session that is then killed; marker variable and extra descriptor set. ---------
cat > "$FIXTURE/launch-session.sh" <<'EOF'
#!/bin/bash
# $1 = fixture; the rest is the launcher command. Same redirections as restore_launch_worker.
D=$1; shift
echo $$ > "$D/launcher.pid"
exec 9> "$D/extra-fd"
export VESSELSTACK_RESTORE_TEST_MARKER=must-not-reach-the-payload
"$@" </dev/null >/dev/null 2>"$D/launch.err"
echo $? > "$D/launch.rc"
sleep 120
EOF
chmod 0755 "$FIXTURE/launch-session.sh"
STARTED=1
setsid "$FIXTURE/launch-session.sh" "$FIXTURE" "${ARGS[@]}" </dev/null >/dev/null 2>&1 &
disown $! 2>/dev/null || true
wait_for 20 test -s "$FIXTURE/launch.rc" || fail "the launcher did not return within 20 s"
LAUNCHER_PGID=$(cat "$FIXTURE/launcher.pid")
[[ "$(cat "$FIXTURE/launch.rc")" == 0 ]] || fail "launch failed: rc=$(cat "$FIXTURE/launch.rc") stderr=$(head -c 400 "$FIXTURE/launch.err")"
wait_for 10 pid_file_ready || fail "the payload did not start"
wait_for 5 main_pid_set || fail "the unit has no MainPID"
payload_pid=$(cat "$FIXTURE/payload.pid")
main_pid=$(show MainPID)
[[ "$main_pid" == "$payload_pid" ]] || fail "MainPID ($main_pid) is not the payload ($payload_pid)"
[[ "$(show ActiveState)" == active ]] || fail "ActiveState is '$(show ActiveState)', expected active"
[[ "$(show Type)" == exec ]] || fail "Type is '$(show Type)', expected exec"
[[ "$(cat "$FIXTURE/payload.args")" == "args: worker $ID" ]] || fail "payload arguments were: $(cat "$FIXTURE/payload.args")"
pass "1. launch exit 0; $UNIT active with the payload (pid $payload_pid) as MainPID, Type=exec"

# 2. properties follow the product's own values
norm_span() { systemd-analyze timespan "$1" 2>/dev/null | awk '/μs:/ {print $2; exit}'; }
for k in Type KillMode TimeoutStopSec IgnoreOnIsolate After; do
    want=$(prop_arg "$k")
    case "$k" in
        TimeoutStopSec)
            got=$(show TimeoutStopUSec)
            wn=$(norm_span "$want"); gn=$(norm_span "$got")
            if [[ -n "$wn" && "$wn" == "$gn" ]]; then :; elif [[ -z "$wn" && "$got" == "$want" ]]; then :; else
                fail "2. TimeoutStopUSec is '$got' ($gn), the product asked for '$want' ($wn)"
            fi ;;
        After)
            got=$(show After)
            for dep in $want; do [[ " $got " == *" $dep "* ]] || fail "2. After= is '$got', lacks $dep"; done ;;
        *)
            got=$(show "$k")
            [[ "$got" == "$want" ]] || fail "2. $k is '$got', the product asked for '$want'" ;;
    esac
done
[[ "$(show KillMode)" == mixed && "$(show IgnoreOnIsolate)" == yes ]] || fail "2. KillMode/IgnoreOnIsolate are not mixed/yes"
pass "2. unit properties match the product's request (Type=$(show Type) KillMode=$(show KillMode) TimeoutStopUSec=$(show TimeoutStopUSec) IgnoreOnIsolate=$(show IgnoreOnIsolate) After includes docker.service)"

# 3. no inherited environment or descriptors
if grep -q 'VESSELSTACK_RESTORE_TEST_MARKER' "$FIXTURE/payload.env"; then
    fail "3. the payload inherited the launcher's environment variable"
fi
if grep -q 'extra-fd' "$FIXTURE/payload.fd" || grep -Eq '[[:space:]]9 ->' "$FIXTURE/payload.fd"; then
    fail "3. the payload inherited the launcher's extra descriptor: $(cat "$FIXTURE/payload.fd")"
fi
grep -Eq '[[:space:]]0 -> /dev/null' "$FIXTURE/payload.fd" || fail "3. payload stdin is not /dev/null: $(grep -E ' 0 ->' "$FIXTURE/payload.fd")"
pass "3. payload has no inherited marker variable or extra descriptor (stdin is /dev/null)"

# 4. kill the launching session
cg=$(cat "$FIXTURE/payload.cgroup")
[[ "$cg" == *"$UNIT"* ]] || fail "4. payload cgroup is not the unit's: $cg"
kill -KILL -- "-$LAUNCHER_PGID" >/dev/null 2>&1 || true
sleep 0.5
if kill -0 "$(cat "$FIXTURE/launcher.pid")" 2>/dev/null; then fail "4. could not kill the launching session"; fi
LAUNCHER_PGID=""
before=$(hb_count)
wait_for 6 hb_advanced_from "$before" || fail "4. the heartbeat stopped after the launching session was killed"
kill -0 "$payload_pid" 2>/dev/null || fail "4. the payload died with its launching session"
pass "4. payload (cgroup $cg) kept running after its launching session was SIGKILLed with its process group"

# 5. second launch with the same unit name is refused
rc=0
err=$("${ARGS[@]}" </dev/null 2>&1 >/dev/null) || rc=$?
[[ "$rc" != 0 ]] || fail "5. a second launch with the same unit name was accepted"
[[ "$(show MainPID)" == "$payload_pid" ]] || fail "5. the refused second launch changed MainPID to $(show MainPID)"
before=$(hb_count)
wait_for 6 hb_advanced_from "$before" || fail "5. the first payload stopped after the refused second launch"
pass "5. second launch refused (exit $rc: ${err:0:160}); first unit undisturbed"

# 6. stop: TERM goes to the main process, which gets to finish its own shutdown path
start=$SECONDS
timeout 60 systemctl stop "$UNIT" || fail "6. systemctl stop failed or took over 60 s"
elapsed=$((SECONDS - start))
grep -q 'TERM received' "$FIXTURE/payload.log" 2>/dev/null || fail "6. the payload did not record TERM"
[[ "$(cat "$FIXTURE/result" 2>/dev/null)" == term-shutdown ]] || fail "6. no shutdown result from the payload (it was killed outright?): $(cat "$FIXTURE/result" 2>/dev/null)"
(( elapsed < 30 )) || fail "6. stop took ${elapsed}s"
wait_for 10 is_gone || fail "6. the unit is still loaded after stop (LoadState=$(show LoadState))"
pass "6. systemctl stop sent TERM to the payload, which finished its own shutdown ($elapsed s) before the unit ended"

# 7. --collect also when the payload exits non-zero
rm -f "$FIXTURE/result" "$FIXTURE/payload.pid" "$FIXTURE/launch.rc"
echo exit1 > "$FIXTURE/mode"
"${ARGS[@]}" </dev/null >/dev/null 2>"$FIXTURE/launch.err" || fail "7. second launch (after the first unit ended) failed: $(head -c 300 "$FIXTURE/launch.err")"
wait_for 15 test -s "$FIXTURE/result" || fail "7. the failing payload did not finish"
[[ "$(cat "$FIXTURE/result")" == "result exit=1" ]] || fail "7. unexpected payload result"
wait_for 10 is_gone || fail "7. unit left behind after a non-zero exit (LoadState=$(show LoadState) ActiveState=$(show ActiveState) Result=$(show Result))"
[[ "$(systemctl is-failed "$UNIT" 2>/dev/null || true)" != failed ]] || fail "7. unit is listed as failed"
STARTED=0
pass "7. after the payload exited 1 the unit was collected (LoadState=not-found), not left failed"

echo "real systemd restore launch test passed"
