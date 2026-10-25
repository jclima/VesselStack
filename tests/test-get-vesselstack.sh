#!/bin/bash
# Guided bootstrap (scripts/get-vesselstack.sh) against fake curl, apt-get, docker,
# systemctl and vesselstackctl stubs and a fake release tarball. Nothing is
# installed on the host. Needs root and Linux (stat -c, setpriv).

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
# /tmp and (on GitHub runners) /opt are world-writable, so root-controlled
# fixtures need a base whose whole chain is root-owned and not group/world-writable.
TEST_BASE=/usr/local/share/vs-test
install -d -m 0755 -o root -g root "$TEST_BASE"
_chain=$(realpath "$TEST_BASE")
while :; do
    read -r _uid _mode < <(stat -c '%u %a' "$_chain")
    if [ "$_uid" != 0 ] || [ $(( 0$_mode & 0022 )) -ne 0 ]; then
        echo "test base $_chain is not root-controlled (uid $_uid, mode $_mode)" >&2
        exit 1
    fi
    [ "$_chain" = / ] && break
    _chain=$(dirname "$_chain")
done
TEST_ROOT=$(mktemp -d -p "$TEST_BASE" vs-get.XXXXXX)
chmod 0755 "$TEST_ROOT"
trap 'rm -rf -- "$TEST_ROOT"; userdel vesselstack-wizard >/dev/null 2>&1 || true' EXIT

GET="$TEST_ROOT/get-vesselstack.sh"
FAKE_BIN="$TEST_ROOT/bin"
SERVE="$TEST_ROOT/serve"
RELEASE_BASE="$TEST_ROOT/releases"
APP_ROOT="$TEST_ROOT/opt/vesselstack"
LOG="$TEST_ROOT/calls.log"
OUT="$TEST_ROOT/out"
VER=9.9.9
install -d "$FAKE_BIN" "$SERVE"
install -m 0755 "$REPO_ROOT/scripts/get-vesselstack.sh" "$GET"

# ---- stubs --------------------------------------------------------------
cat > "$FAKE_BIN/curl" <<'EOF2'
#!/bin/bash
echo "curl $*" >> "$TEST_ROOT/curl.log"
out=; url=; head=0
while [ $# -gt 0 ]; do
    case "$1" in
        -o) out=$2; shift 2 ;;
        -I|-fsSI) head=1; shift ;;
        -*) case "$1" in *I*) head=1 ;; esac; shift ;;
        *) url=$1; shift ;;
    esac
done
[ "$head" -eq 1 ] && exit 0
name=${url##*/}
[ ! -e "$TEST_ROOT/serve/$name.fail" ] || exit 22
cp "$TEST_ROOT/serve/$name" "$out" || exit 22
EOF2
cat > "$FAKE_BIN/apt-get" <<'EOF2'
#!/bin/bash
echo "apt-get $*" >> "$LOG"
case "$*" in *docker*) touch "$TEST_ROOT/docker-installed" ;; esac
exit 0
EOF2
cat > "$FAKE_BIN/apt-cache" <<'EOF2'
#!/bin/bash
# "policy PKG": the candidate comes from $TEST_ROOT/apt-candidates lines "PKG VERSION".
[ "$1" = policy ] || exit 1
version=$(awk -v p="$2" '$1 == p {print $2}' "$TEST_ROOT/apt-candidates" 2>/dev/null)
printf '%s:\n  Installed: (none)\n  Candidate: %s\n' "$2" "${version:-(none)}"
EOF2
cat > "$FAKE_BIN/dpkg-query" <<'EOF2'
#!/bin/bash
pkg=${*: -1}
if grep -qxF "$pkg" "$TEST_ROOT/missing-pkgs" 2>/dev/null; then exit 1; fi
printf installed
EOF2
cat > "$FAKE_BIN/docker" <<'EOF2'
#!/bin/bash
echo "docker $*" >> "$LOG"
[ -e "$TEST_ROOT/docker-installed" ]
EOF2
cat > "$FAKE_BIN/systemctl" <<'EOF2'
#!/bin/bash
echo "systemctl $*" >> "$LOG"
if [ "$1" = is-active ] && [ -e "$TEST_ROOT/docker-inactive" ]; then exit 3; fi
if [ "$1" = enable ]; then rm -f "$TEST_ROOT/docker-inactive"; fi
exit 0
EOF2
cat > "$FAKE_BIN/runuser" <<'EOF2'
#!/bin/bash
echo "runuser $*" >> "$LOG"
exec "$(PATH=/usr/sbin:/sbin:/usr/bin:/bin command -v runuser)" "$@"
EOF2
# Real account management only for the temporary wizard account; everything else is logged only.
cat > "$FAKE_BIN/useradd" <<'EOF2'
#!/bin/bash
echo "useradd $*" >> "$LOG"
if [ "${*: -1}" = vesselstack-wizard ]; then
    exec "$(PATH=/usr/sbin:/sbin:/usr/bin:/bin command -v useradd)" "$@"
fi
exit 0
EOF2
cat > "$FAKE_BIN/userdel" <<'EOF2'
#!/bin/bash
echo "userdel $*" >> "$LOG"
if [ "${*: -1}" = vesselstack-wizard ]; then
    exec "$(PATH=/usr/sbin:/sbin:/usr/bin:/bin command -v userdel)" "$@"
fi
exit 0
EOF2
cat > "$FAKE_BIN/vesselstackctl" <<'EOF2'
#!/bin/bash
echo "ctl $* WHEELHOUSE=${VESSELSTACK_WHEELHOUSE:-}" >> "$LOG"
case "$1" in urls) echo "Boat Chat  http://0.0.0.0:8765" ;; esac
[ -z "${FAKE_CTL_FAIL:-}" ] || [ "$1" != "${FAKE_CTL_FAIL}" ]
EOF2
chmod 0755 "$FAKE_BIN"/*

# ---- fixture release tarball -----------------------------------------------
SRC="$TEST_ROOT/src/vesselstack-$VER"
install -d "$SRC/scripts" "$SRC/wizard"
printf '%s\n' "$VER" > "$SRC/VERSION"
printf 'deadbeef\n' > "$SRC/COMMIT"
cat > "$SRC/install.sh" <<'EOF2'
#!/bin/bash
echo "install $* WHEELHOUSE=${VESSELSTACK_WHEELHOUSE:-}" >> "$LOG"
case " $* " in
    *" --dry-run "*) [ -z "${FAKE_FAIL_DRY:-}" ] || exit 1; exit 0 ;;
esac
mkdir -p "$VESSELSTACK_ROOT/config"
printf '%s\n' "$(cat "$(dirname "$0")/VERSION")" > "$VESSELSTACK_ROOT/config/installed-version"
EOF2
cat > "$SRC/scripts/prepare-offline-bundle.sh" <<'EOF2'
#!/bin/bash
echo "bundle $*" >> "$LOG"
exit 0
EOF2
cat > "$SRC/vesselstack-wizard.py" <<'EOF2'
# Runs as an unprivileged user with a minimal environment (no LOG, no TEST_ROOT).
import os, runpy, sys
args = sys.argv[1:]
if "--validate" in args:
    # Root-side validation: run the REAL wizard code from the repository.
    sys.argv = [sys.argv[0]] + args
    runpy.run_path("@REPO_ROOT@/vesselstack-wizard.py", run_name="__main__")
    sys.exit(0)
out = args[args.index("--output") + 1]
port = args[args.index("--port") + 1]
print("VesselStack wizard: http://127.0.0.1:" + port)
print("Open this address (valid for this run only): http://127.0.0.1:" + port + "/#token=FAKETOKEN123")
print("uid=" + str(os.getuid()) + " env=" + ",".join(sorted(os.environ)))
mode = ""
try:
    mode = open("@TEST_ROOT@/wizard-mode").read().strip()
except OSError:
    pass
if mode == "nosave":
    sys.exit(0)
if mode == "symlink":
    os.symlink("/etc/passwd", out); sys.exit(0)
if mode == "fifo":
    os.mkfifo(out); sys.exit(0)
with open(out, "w") as handle:
    handle.write("BOAT_NAME='Test Boat'\nVESSELSTACK_USER=daemon\nBOAT_MMSI=''\n")
    if mode == "big":
        handle.write("# " + "x" * 70000 + "\n")
    if mode == "unknown":
        handle.write("NOT_A_SETTING=x\n")
    if mode == "badchars":
        handle.write("BOAT_CALLSIGN='a;b'\n")
    if mode == "enum":
        handle.write("SIGNALK_MODE=surprise\n")
    if mode == "boolean":
        handle.write("AIS_ENABLE=yes\n")
    if mode == "inject":
        handle.write("BOAT_CALLSIGN=$(touch @TEST_ROOT@/pwned-wizard)\n")
EOF2
sed -i "s|@TEST_ROOT@|$TEST_ROOT|g; s|@REPO_ROOT@|$REPO_ROOT|g" "$SRC/vesselstack-wizard.py"
chmod 0755 "$SRC/install.sh" "$SRC/scripts/prepare-offline-bundle.sh"
GOOD="$TEST_ROOT/good.tar.gz"
tar -C "$TEST_ROOT/src" -czf "$GOOD" "vesselstack-$VER"
GOOD_SHA=$(sha256sum "$GOOD" | cut -d' ' -f1)

serve_tarball() {  # file [sha256]
    cp "$1" "$SERVE/vesselstack-$VER.tar.gz"
    printf '%s  vesselstack-%s.tar.gz\n' "${2:-$(sha256sum "$1" | cut -d' ' -f1)}" "$VER" > "$SERVE/vesselstack-$VER.tar.gz.sha256"
}

make_evil() {  # kind -> path
    python3 - "$1" "$TEST_ROOT/evil-$1.tar.gz" "$VER" <<'PY'
import io, sys, tarfile
kind, out, ver = sys.argv[1:]
top = f"vesselstack-{ver}"
def add(archive, name, data=b"x", **kw):
    info = tarfile.TarInfo(name); info.size = len(data); info.mode = kw.get("mode", 0o644)
    archive.addfile(info, io.BytesIO(data))
with tarfile.open(out, "w:gz") as archive:
    add(archive, f"{top}/VERSION", ver.encode() + b"\n")
    if kind == "traversal": add(archive, f"{top}/../../escaped.txt")
    elif kind == "absolute": add(archive, "/etc/vs-escaped.txt")
    elif kind == "prefix": add(archive, "elsewhere/file")
    elif kind == "setuid": add(archive, f"{top}/suid", mode=0o4755)
    elif kind in ("symlink", "hardlink"):
        info = tarfile.TarInfo(f"{top}/link"); info.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
        info.linkname = "/etc/passwd" if kind == "symlink" else f"{top}/VERSION"
        archive.addfile(info)
    elif kind == "device":
        info = tarfile.TarInfo(f"{top}/null"); info.type = tarfile.CHRTYPE; archive.addfile(info)
PY
    echo "$TEST_ROOT/evil-$1.tar.gz"
}

# ---- helpers -------------------------------------------------------------
write_config() {  # [user]
    cat > "$TEST_ROOT/in.env" <<EOF2
BOAT_NAME='Test Boat'
VESSELSTACK_USER=${1:-daemon}
VESSELSTACK_ROOT=$APP_ROOT
HOME_ASSISTANT_TOKEN=sekrit-token-value
BOAT_MMSI=''
EOF2
    chmod 0644 "$TEST_ROOT/in.env"
}

reset_state() {
    rm -rf -- "$RELEASE_BASE" "$APP_ROOT" "$LOG" "$TEST_ROOT/curl.log" "$TEST_ROOT/docker-installed" "$TEST_ROOT/docker-inactive" \
        "$TEST_ROOT/missing-pkgs" "$TEST_ROOT/answers" "$TEST_ROOT/wizard-mode" "$TEST_ROOT/pwned-wizard" "$SERVE"/*.fail
    printf 'docker-compose-v2 2.24.0\n' > "$TEST_ROOT/apt-candidates"
    : > "$LOG"
    touch "$TEST_ROOT/docker-installed"
    serve_tarball "$GOOD"
    write_config
}

RC=0
run() {  # args... (stdin is never a terminal)
    RC=0
    # SUDO_USER is always unset here (CI runs the test under sudo, which sets it); cases set it via EXTRA_ENV.
    env -u SUDO_USER PATH="$FAKE_BIN:$PATH" TEST_ROOT="$TEST_ROOT" LOG="$LOG" VESSELSTACK_ROOT="$APP_ROOT" \
        VESSELSTACK_VERSION="$VER" VESSELSTACK_BOOTSTRAP_DOCTOR_WAIT=0 NO_COLOR=1 \
        ${EXTRA_ENV:+"$EXTRA_ENV"} \
        bash "$GET" --dir "$RELEASE_BASE" --base-url "https://example.invalid/releases/v$VER" "$@" \
        </dev/null >"$OUT" 2>&1 || RC=$?
}
fail_test() { echo "FAIL: $1" >&2; echo "--- output ---" >&2; cat "$OUT" >&2; echo "--- calls ---" >&2; cat "$LOG" >&2; exit 1; }
expect_rc() { [ "$RC" -eq "$1" ] || fail_test "$2 (exit $RC, wanted $1)"; }
expect_out() { grep -qE -- "$1" "$OUT" || fail_test "output lacks /$1/"; }
refuse_out() { if grep -qE -- "$1" "$OUT"; then fail_test "output must not contain /$1/"; fi; }
expect_log() { grep -qE -- "$1" "$LOG" || fail_test "calls lack /$1/"; }
refuse_log() { if grep -qE -- "$1" "$LOG"; then fail_test "calls must not contain /$1/"; fi; }
expect_no_wizard_user() { if getent passwd vesselstack-wizard >/dev/null; then fail_test "temporary account vesselstack-wizard was not removed"; fi; }
expect_no_release() { [ ! -e "$RELEASE_BASE/$VER" ] || fail_test "release directory must not exist"; }

# ---- 1. help and refusals --------------------------------------------------
reset_state
run --help
expect_rc 0 "--help"
for option in --version --yes --no-wizard --config --offline --bundle --dir; do expect_out -- "$option"; done
[ ! -e "$RELEASE_BASE" ] || fail_test "--help must not create anything"

# Not root: refuse before touching anything (and --help still works unprivileged).
RC=0
setpriv --reuid=65534 --regid=65534 --clear-groups env PATH="$FAKE_BIN:$PATH" TEST_ROOT="$TEST_ROOT" LOG="$LOG" \
    bash "$GET" --dir "$RELEASE_BASE" --yes </dev/null >"$OUT" 2>&1 || RC=$?
expect_rc 1 "non-root run must be refused"
expect_out "must run as root"
expect_out "sudo bash get-vesselstack.sh"
[ ! -e "$RELEASE_BASE" ] || fail_test "non-root run created files"
RC=0
setpriv --reuid=65534 --regid=65534 --clear-groups bash "$GET" --help </dev/null >"$OUT" 2>&1 || RC=$?
expect_rc 0 "--help as non-root"

run --version not-a-version --yes
expect_rc 1 "bad version"
run --offline --yes
expect_rc 1 "--offline without --bundle"
expect_out "--bundle"
run --no-wizard --yes
expect_rc 1 "--no-wizard without --config"
run --base-url http://insecure.invalid --yes
expect_rc 1 "non-https base url"

# ---- 2. truncated script does nothing -----------------------------------------
reset_state
size=$(stat -c %s "$GET")
for pct in 5 15 25 35 45 55 65 75 85 95; do
    head -c $(( size * pct / 100 )) "$GET" > "$TEST_ROOT/trunc.sh"
    RC=0
    env PATH="$FAKE_BIN:$PATH" TEST_ROOT="$TEST_ROOT" LOG="$LOG" VESSELSTACK_ROOT="$APP_ROOT" \
        bash -s -- --yes --dir "$RELEASE_BASE" --no-wizard --config "$TEST_ROOT/in.env" \
        < "$TEST_ROOT/trunc.sh" >"$OUT" 2>&1 || RC=$?
    [ ! -s "$LOG" ] || fail_test "truncated script (${pct}%) ran something"
    [ ! -e "$RELEASE_BASE" ] || fail_test "truncated script (${pct}%) created files"
done
head -c $(( size - 6 )) "$GET" > "$TEST_ROOT/trunc.sh"
RC=0
env PATH="$FAKE_BIN:$PATH" TEST_ROOT="$TEST_ROOT" LOG="$LOG" bash -s -- --yes --dir "$RELEASE_BASE" \
    < "$TEST_ROOT/trunc.sh" >"$OUT" 2>&1 || RC=$?
[ "$RC" -ne 0 ] || fail_test "script cut inside the final main call must fail"
if ! { [ ! -s "$LOG" ] && [ ! -e "$RELEASE_BASE" ]; }; then fail_test "script cut inside the final main call did something"; fi
[ "$(tail -n1 "$GET")" = 'main "$@"' ] || fail_test "main must be invoked on the last line"

# ---- 3. checksum and archive safety ----------------------------------------
reset_state
serve_tarball "$GOOD" "$(printf '%064d' 0)"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 1 "sha256 mismatch must be refused"
expect_out "CHECKSUM MISMATCH"
expect_no_release
refuse_log "^install "

reset_state
head -c $(( $(stat -c %s "$GOOD") / 2 )) "$GOOD" > "$TEST_ROOT/partial.tar.gz"
serve_tarball "$TEST_ROOT/partial.tar.gz" "$GOOD_SHA"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 1 "truncated tarball download must be refused"
expect_out "CHECKSUM MISMATCH"
expect_no_release
refuse_log "^install "

reset_state
: > "$SERVE/vesselstack-$VER.tar.gz.fail"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 1 "failed download"
expect_out "Download failed"
expect_no_release

reset_state
printf 'not a checksum\n' > "$SERVE/vesselstack-$VER.tar.gz.sha256"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 1 "garbage .sha256"
expect_no_release

reset_state
run --yes --no-wizard --config "$TEST_ROOT/in.env" --sha256 "$(printf '%064d' 1)"
expect_rc 1 "--sha256 pin that does not match"
expect_out "--sha256"
expect_no_release

for kind in traversal absolute prefix symlink hardlink setuid device; do
    reset_state
    evil=$(make_evil "$kind")
    serve_tarball "$evil"
    run --yes --no-wizard --config "$TEST_ROOT/in.env"
    expect_rc 1 "unsafe tarball ($kind) must be refused"
    expect_out "archive refused"
    expect_no_release
    refuse_log "^install "
    if ! { [ ! -e "$TEST_ROOT/releases/escaped.txt" ] && [ ! -e /etc/vs-escaped.txt ]; }; then fail_test "$kind: archive wrote outside the target"; fi
done

# ---- 4. fresh install with an existing config -----------------------------------
reset_state
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 0 "fresh install with --no-wizard --config"
expect_out "checksum verified"
expect_out "sha256 $GOOD_SHA"
refuse_out "sekrit-token-value"
expect_out "BOAT_MMSI +\(not set\)"
# Order: dry run first, then the real install; then doctor and urls.
dry=$(grep -n -- "^install .*--dry-run" "$LOG" | head -n1 | cut -d: -f1)
real=$(grep -n -- "^install .*--start" "$LOG" | head -n1 | cut -d: -f1)
if ! { [ -n "$dry" ] && [ -n "$real" ] && [ "$dry" -lt "$real" ]; }; then fail_test "dry run must precede install"; fi
expect_log "^install --config $RELEASE_BASE/vesselstack.env --dry-run"
expect_log "^install --config $RELEASE_BASE/vesselstack.env --start"
expect_log "^ctl doctor"
expect_log "^ctl urls"
refuse_log "^wizard "
refuse_log "^useradd"
read -r uid mode < <(stat -c '%u %a' "$RELEASE_BASE/vesselstack.env")
if ! { [ "$uid" = 0 ] && [ "$mode" = 600 ]; }; then fail_test "env copy must be root-only 600 (got $uid $mode)"; fi
[ "$(stat -c %u "$RELEASE_BASE/$VER")" = 0 ] || fail_test "release dir not root-owned"
[ -z "$(find "$RELEASE_BASE/$VER" \( ! -uid 0 -o -perm /022 \) -print -quit)" ] || fail_test "release tree is not root-owned and non-writable by others"
[ -x "$RELEASE_BASE/$VER/install.sh" ] || fail_test "install.sh lost its exec bit"
for leftover in "$RELEASE_BASE"/.stage.*; do
    [ ! -e "$leftover" ] || fail_test "staging directory left behind"
done
expect_out "This run changed"
expect_out "unpacked release to"
expect_out "Show the Boat Chat access token"

# Re-running is safe: installed-version now exists, so it reports and stops.
before=$(wc -l < "$LOG")
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 0 "re-run after install"
expect_out "already installed"
[ "$(wc -l < "$LOG")" -eq "$before" ] || fail_test "re-run must not call anything"

# Same tarball again (no installed-version): the verified release is reused.
rm -rf "$APP_ROOT"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 0 "re-run after the marker was removed"
expect_out "reusing"

# A dry-run failure stops before the real install.
reset_state
EXTRA_ENV=FAKE_FAIL_DRY=1 run --yes --no-wizard --config "$TEST_ROOT/in.env"
EXTRA_ENV=
expect_rc 1 "failed dry run"
expect_out "preflight failed"
refuse_log "^install .*--start"

# doctor failing is reported, with a non-zero exit but the install kept.
reset_state
EXTRA_ENV=FAKE_CTL_FAIL=doctor run --yes --no-wizard --config "$TEST_ROOT/in.env"
EXTRA_ENV=
expect_rc 3 "unhealthy doctor"
expect_out "doctor reported problems"
expect_log "^install .*--start"

# Unsafe config contents are refused before anything is installed.
reset_state
printf 'BOAT_NAME=%s\n' "\$(touch $TEST_ROOT/pwned)" > "$TEST_ROOT/bad.env"
run --yes --no-wizard --config "$TEST_ROOT/bad.env"
expect_rc 1 "config with command substitution"
[ ! -e "$TEST_ROOT/pwned" ] || fail_test "config content was executed"
refuse_log "^install "

# The shipped example env passes the config linter.
reset_state
cp "$REPO_ROOT/vesselstack.example.env" "$TEST_ROOT/example.env"
run --yes --no-wizard --config "$TEST_ROOT/example.env"
expect_rc 0 "the shipped example env must pass the config linter"

# ---- 5. wizard flow -----------------------------------------------------------
reset_state
run --yes --wizard-port 8123
expect_rc 0 "wizard flow"
expect_out "ssh -L 8123:127.0.0.1:8123"
expect_out "http://127.0.0.1:8123/#token="
# No SUDO_USER: runs as a temporary dedicated account (never the shared nobody) with a minimal
# environment, never as root; the account is created before and removed after the run.
expect_log "^useradd --system --no-create-home --shell /usr/sbin/nologin vesselstack-wizard$"
expect_log "^userdel vesselstack-wizard$"
expect_no_wizard_user
refuse_log "runuser -u nobody"
refuse_out "uid=65534 "
expect_log "^runuser -u vesselstack-wizard -- env -i PATH=[^ ]+ HOME=/[^ ]+ LANG=C.UTF-8 python3 -I $RELEASE_BASE/$VER/vesselstack-wizard.py --port 8123 --output /[^ ]+/vesselstack.env --exit-after-save"
expect_out "uid=[1-9][0-9]* env=HOME,LANG,PATH$"
refuse_out "uid=0 "
wiz=$(grep -n "^runuser" "$LOG" | head -n1 | cut -d: -f1)
dry=$(grep -n -- "^install .*--dry-run" "$LOG" | head -n1 | cut -d: -f1)
[ "$wiz" -lt "$dry" ] || fail_test "wizard must run before install"
[ "$(stat -c '%a %U' "$RELEASE_BASE/vesselstack.env")" = "600 root" ] || fail_test "wizard output must be root-owned mode 600"
if ls -d "$RELEASE_BASE"/.wizard.* >/dev/null 2>&1; then fail_test "wizard directory was not removed"; fi

# SUDO_USER is used when it is a real unprivileged account; a random free port otherwise.
reset_state
EXTRA_ENV=SUDO_USER=daemon run --yes
EXTRA_ENV=
expect_rc 0 "wizard as SUDO_USER"
refuse_log "^useradd .*vesselstack-wizard"
expect_no_wizard_user
expect_log "^runuser -u daemon -- env -i .*--port [0-9]{4,5} "
expect_out "ssh -L ([0-9]{4,5}):127.0.0.1:\\1 "
reset_state
EXTRA_ENV=SUDO_USER=root run --yes
EXTRA_ENV=
expect_log "^runuser -u vesselstack-wizard "
expect_no_wizard_user
# An unknown SUDO_USER also falls back to the dedicated account.
reset_state
EXTRA_ENV=SUDO_USER=no-such-account-xyz run --yes
EXTRA_ENV=
expect_rc 0 "wizard with unknown SUDO_USER"
expect_log "^runuser -u vesselstack-wizard "
expect_no_wizard_user
# A stale account from an interrupted run is replaced by a fresh one and removed again.
reset_state
useradd --system --no-create-home --shell /usr/sbin/nologin vesselstack-wizard
run --yes
expect_rc 0 "wizard with a stale vesselstack-wizard account"
expect_log "^userdel vesselstack-wizard$"
expect_log "^useradd --system --no-create-home .* vesselstack-wizard$"
expect_no_wizard_user

# The wizard closed without saving: nothing is installed.
reset_state
echo nosave > "$TEST_ROOT/wizard-mode"
run --yes
expect_rc 1 "wizard closed without saving"
expect_no_wizard_user
expect_out "before a configuration was saved"
refuse_log "^install "

# Wizard output is copied once without following links, then linted: a symlink, a FIFO,
# an oversized file and injection content are all refused and nothing is installed.
# Output that passes the syntax lint but breaks the wizard's own rules (unknown key, characters,
# enum, boolean) is refused too. The temporary account is removed after every failure.
for mode in symlink fifo big inject unknown badchars enum boolean; do
    reset_state
    echo "$mode" > "$TEST_ROOT/wizard-mode"
    run --yes
    expect_rc 1 "wizard output ($mode) must be refused"
    expect_out "wizard output was refused|saved configuration is invalid"
    expect_log "^userdel vesselstack-wizard$"
    expect_no_wizard_user
    refuse_log "^install "
    [ ! -e "$TEST_ROOT/pwned-wizard" ] || fail_test "wizard output content was executed"
    [ ! -e "$RELEASE_BASE/vesselstack.env" ] || fail_test "refused wizard output must not be installed"
done
rm -f "$TEST_ROOT/wizard-mode"

# ---- 5b. --config is staged once (no re-read of the user-controlled file) -------
reset_state
mkdir -p "$TEST_ROOT/cfg"
cp "$TEST_ROOT/in.env" "$TEST_ROOT/cfg/real.env"
ln -s real.env "$TEST_ROOT/cfg/link.env"
run --yes --no-wizard --config "$TEST_ROOT/cfg/link.env"
expect_rc 1 "--config symlink must be refused"
expect_out "refused"
refuse_log "^install "
mkfifo "$TEST_ROOT/cfg/fifo.env"
run --yes --no-wizard --config "$TEST_ROOT/cfg/fifo.env"
expect_rc 1 "--config FIFO must be refused (and must not hang)"
run --yes --no-wizard --config "$TEST_ROOT/cfg"
expect_rc 1 "--config directory must be refused"
run --yes --no-wizard --config "$TEST_ROOT/cfg/missing.env"
expect_rc 1 "--config missing file must be refused"
{ cat "$TEST_ROOT/in.env"; printf '# %070000d\n' 0; } > "$TEST_ROOT/cfg/big.env"
run --yes --no-wizard --config "$TEST_ROOT/cfg/big.env"
expect_rc 1 "oversized --config must be refused"
expect_out "larger than"
printf 'BOAT_NAME=%s\n' "\$(touch $TEST_ROOT/pwned-config)" > "$TEST_ROOT/cfg/inject.env"
run --yes --no-wizard --config "$TEST_ROOT/cfg/inject.env"
expect_rc 1 "--config injection must be refused"
[ ! -e "$TEST_ROOT/pwned-config" ] || fail_test "config content was executed"
refuse_log "^install "
# --config gets the same strict validation as the wizard output.
for bad in 'NOT_A_SETTING=x' "BOAT_CALLSIGN='a;b'" 'SIGNALK_MODE=surprise' 'AIS_ENABLE=yes' 'BOAT_NAME=two words'; do
    reset_state
    { cat "$TEST_ROOT/in.env"; printf '%s\n' "$bad"; } > "$TEST_ROOT/cfg/strict.env"
    run --yes --no-wizard --config "$TEST_ROOT/cfg/strict.env"
    expect_rc 1 "--config with '$bad' must be refused"
    expect_out "configuration file was refused"
    refuse_log "^install "
    [ ! -e "$RELEASE_BASE/vesselstack.env" ] || fail_test "refused --config must not be installed"
done
# A file swapped for a symlink after the check cannot matter: only the staged copy is used.
# The installed file is a root-owned copy, identical to the original content.
reset_state
run --yes --no-wizard --config "$TEST_ROOT/cfg/real.env"
expect_rc 0 "regular --config"
[ "$(stat -c '%a %U' "$RELEASE_BASE/vesselstack.env")" = "600 root" ] || fail_test "installed config must be root-owned 600"
cmp -s "$TEST_ROOT/cfg/real.env" "$RELEASE_BASE/vesselstack.env" || fail_test "installed config differs from staged input"
# shellcheck disable=SC2016  # literal text search, no expansion wanted
if grep -n 'install -m 0600 -o root -g root "\$CONFIG_ARG"\|lint "\$CONFIG_ARG"' "$GET"; then fail_test "script must not lint or install the original --config path"; fi
if ls -d "$RELEASE_BASE"/.stage.* >/dev/null 2>&1; then fail_test "staging directory was not removed"; fi

# A saved configuration from an earlier run is reused (default answer r).
reset_state
run --yes
expect_rc 0 "first wizard run"
rm -rf "$APP_ROOT"; : > "$LOG"
run --yes
expect_rc 0 "second run reuses the saved config"
expect_out "reusing the saved configuration"
refuse_log "^wizard "

# ---- 6. prerequisites ------------------------------------------------------------
reset_state
printf 'rsync\njq\n' > "$TEST_ROOT/missing-pkgs"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 0 "missing apt packages"
expect_log "^apt-get update"
expect_log "^apt-get install -y --no-install-recommends (.* )?jq rsync"
expect_out "installed Debian packages"

reset_state
rm -f "$TEST_ROOT/docker-installed"
printf '' > "$TEST_ROOT/answers"
EXTRA_ENV=VESSELSTACK_BOOTSTRAP_ANSWERS="$TEST_ROOT/answers" run --no-wizard --config "$TEST_ROOT/in.env"
EXTRA_ENV=
expect_rc 1 "no terminal and no --yes"
expect_out "no terminal|No answer"
refuse_log "docker.io"

reset_state
rm -f "$TEST_ROOT/docker-installed"
printf 'y\nn\n' > "$TEST_ROOT/answers"
EXTRA_ENV=VESSELSTACK_BOOTSTRAP_ANSWERS="$TEST_ROOT/answers" run --no-wizard --config "$TEST_ROOT/in.env"
EXTRA_ENV=
expect_rc 1 "declining the Docker install"
expect_out "Docker is required"
refuse_log "^apt-get install .*docker"
expect_no_release

reset_state
rm -f "$TEST_ROOT/docker-installed"
touch "$TEST_ROOT/docker-inactive"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 0 "--yes installs Docker"
expect_log "^apt-get install -y --no-install-recommends docker-compose-v2"
expect_log "^systemctl enable --now docker"

# Debian 13 style: only docker-compose, at 2.x, is offered.
reset_state
rm -f "$TEST_ROOT/docker-installed"
printf 'docker-compose 2.26.1-4\n' > "$TEST_ROOT/apt-candidates"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 0 "docker-compose 2.x package"
expect_log "^apt-get install -y --no-install-recommends docker-compose$"
# Debian 12 style: only the v1 docker-compose exists, so point at Docker's repository.
reset_state
rm -f "$TEST_ROOT/docker-installed"
printf 'docker-compose 1.29.2-3\n' > "$TEST_ROOT/apt-candidates"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 1 "only Compose v1 available"
expect_out "docker-compose-plugin"
expect_out "docs.docker.com/engine/install/debian"
refuse_log "^apt-get install .*docker"
expect_no_release
# Package lists are refreshed even when no base package is missing.
reset_state
rm -f "$TEST_ROOT/docker-installed"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_log "^apt-get update"

# ---- 7. service account -----------------------------------------------------------
reset_state
write_config vstestboat
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 0 "missing service account"
expect_log "^useradd --system --create-home --shell /usr/sbin/nologin vstestboat"
useradd_line=$(grep -n "^useradd" "$LOG" | cut -d: -f1)
dry=$(grep -n -- "--dry-run" "$LOG" | head -n1 | cut -d: -f1)
[ "$useradd_line" -lt "$dry" ] || fail_test "the account must exist before the installer preflight"
reset_state
write_config root
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 1 "root as service account"
refuse_log "^install "

# ---- 8. existing installation / upgrade -------------------------------------------------
reset_state
install -d "$APP_ROOT/config"
echo 1.0.0 > "$APP_ROOT/config/installed-version"
run --yes
expect_rc 0 "upgrade with --yes"
expect_out "UPGRADE"
expect_log "^ctl update $RELEASE_BASE/$VER WHEELHOUSE=$"
refuse_log "^wizard "
refuse_log "^install "
expect_out "backup made first"

reset_state
install -d "$APP_ROOT/config"
echo 1.0.0 > "$APP_ROOT/config/installed-version"
printf 'y\nn\n' > "$TEST_ROOT/answers"
EXTRA_ENV=VESSELSTACK_BOOTSTRAP_ANSWERS="$TEST_ROOT/answers" run
EXTRA_ENV=
expect_rc 0 "declined upgrade"
expect_out "Nothing was changed on the installed system"
expect_out "vesselstackctl update"
refuse_log "^ctl update"

reset_state
install -d "$APP_ROOT/config"
echo 99.0.0 > "$APP_ROOT/config/installed-version"
run --yes
expect_rc 1 "downgrade"
expect_out "Downgrades are not supported"
expect_no_release

# ---- 9. offline -----------------------------------------------------------------------------
reset_state
install -d -m 0755 "$TEST_ROOT/bundle/wheelhouse"
cp "$GOOD" "$TEST_ROOT/carry-vesselstack-$VER.tar.gz"
install -d "$TEST_ROOT/carry"
cp "$GOOD" "$TEST_ROOT/carry/vesselstack-$VER.tar.gz"
printf '%s  vesselstack-%s.tar.gz\n' "$GOOD_SHA" "$VER" > "$TEST_ROOT/carry/vesselstack-$VER.tar.gz.sha256"
printf 'rsync\n' > "$TEST_ROOT/missing-pkgs"
run --yes --offline --bundle "$TEST_ROOT/bundle" --tarball "$TEST_ROOT/carry/vesselstack-$VER.tar.gz" --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 1 "offline mode cannot install packages"
expect_out "Offline mode cannot install packages"
rm -f "$TEST_ROOT/missing-pkgs"
run --yes --offline --bundle "$TEST_ROOT/bundle" --tarball "$TEST_ROOT/carry/vesselstack-$VER.tar.gz" --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 0 "offline install"
[ ! -e "$TEST_ROOT/curl.log" ] || fail_test "offline mode must not use curl"
refuse_log "^apt-get"
expect_log "^bundle --verify $TEST_ROOT/bundle"
expect_log "^bundle --load $TEST_ROOT/bundle"
expect_log "^install --config $RELEASE_BASE/vesselstack.env --offline --dry-run WHEELHOUSE=$TEST_ROOT/bundle/wheelhouse"
expect_log "^install --config $RELEASE_BASE/vesselstack.env --offline --start WHEELHOUSE=$TEST_ROOT/bundle/wheelhouse"

reset_state
run --yes --offline --bundle "$TEST_ROOT/no-such-bundle" --tarball "$TEST_ROOT/carry/vesselstack-$VER.tar.gz" --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 1 "missing bundle"
refuse_log "^install "
# A local tarball without its checksum file is refused.
reset_state
cp "$GOOD" "$TEST_ROOT/lonely.tar.gz"
run --yes --offline --bundle "$TEST_ROOT/bundle" --tarball "$TEST_ROOT/lonely.tar.gz" --no-wizard --config "$TEST_ROOT/in.env" --version "$VER"
expect_rc 1 "tarball without .sha256"
expect_out "Checksum file not found"

# Offline upgrade passes the wheelhouse through to vesselstackctl update.
reset_state
install -d "$APP_ROOT/config"
echo 1.0.0 > "$APP_ROOT/config/installed-version"
run --yes --offline --bundle "$TEST_ROOT/bundle" --tarball "$TEST_ROOT/carry/vesselstack-$VER.tar.gz"
expect_rc 0 "offline upgrade"
expect_log "^ctl update --offline $RELEASE_BASE/$VER WHEELHOUSE=$TEST_ROOT/bundle/wheelhouse"

# ---- 10. root-controlled release directory ------------------------------------------------
reset_state
install -d -m 0777 "$RELEASE_BASE"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 1 "world-writable release base"
expect_out "root-owned"
refuse_log "^install "
rm -rf "$RELEASE_BASE"

echo "get-vesselstack tests passed"
