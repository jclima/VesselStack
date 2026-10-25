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
case "$1" in
    -s)  # simulation: "Remv" lines a test chose
        if [ "$2" = remove ] && [ -e "$TEST_ROOT/apt-sim-remove" ]; then cat "$TEST_ROOT/apt-sim-remove"; fi ;;
    update)
        if [ -e "$TEST_ROOT/apt-update-fail" ]; then echo "E: Failed to fetch https://download.docker.com" >&2; exit 100; fi ;;
    install)
        case "$*" in
            *docker*)
                if [ -e "$TEST_ROOT/apt-install-fail" ]; then echo "E: Unable to locate package" >&2; exit 100; fi
                touch "$TEST_ROOT/docker-installed"
                # The "no docker" PATH (docker_absent) gains the docker stub once the engine is installed.
                case "$*" in
                    *docker-ce*)
                        fake=$(dirname "$(readlink -f "$0")")
                        if [ -d "$TEST_ROOT/bin-nodocker" ]; then ln -sf "$fake/docker" "$TEST_ROOT/bin-nodocker/docker"; fi ;;
                esac ;;
        esac ;;
esac
exit 0
EOF2
cat > "$FAKE_BIN/apt-cache" <<'EOF2'
#!/bin/bash
# "policy PKG": candidates come from $TEST_ROOT/apt-candidates ("PKG VERSION" lines) and, once
# Docker's repository entry has been written, also from $TEST_ROOT/apt-candidates-repo.
[ "$1" = policy ] || exit 1
files=("$TEST_ROOT/apt-candidates")
if [ -e "$TEST_ROOT/etc-apt/sources.list.d/docker.sources" ]; then files+=("$TEST_ROOT/apt-candidates-repo"); fi
version=$(cat "${files[@]}" 2>/dev/null | awk -v p="$2" '$1 == p {print $2; exit}')
printf '%s:\n  Installed: (none)\n  Candidate: %s\n' "$2" "${version:-(none)}"
EOF2
cat > "$FAKE_BIN/dpkg-query" <<'EOF2'
#!/bin/bash
pkg=${*: -1}
if grep -qxF "$pkg" "$TEST_ROOT/missing-pkgs" 2>/dev/null; then exit 1; fi
case "$pkg" in
    # Docker-related packages count as installed only when a test lists them.
    docker-ce|docker.io|docker-cli|docker-compose|docker-compose-v2|docker-doc|docker-buildx|podman-docker|containerd|runc)
        grep -qxF "$pkg" "$TEST_ROOT/installed-pkgs" 2>/dev/null || exit 1 ;;
esac
printf installed
EOF2
cat > "$FAKE_BIN/dpkg" <<'EOF2'
#!/bin/bash
# Querying the architecture is read-only and is not logged (the truncation test counts logged calls).
if [ "$1" = --print-architecture ]; then cat "$TEST_ROOT/dpkg-arch"; exit 0; fi
echo "dpkg $*" >> "$LOG"
exit 0
EOF2
cat > "$FAKE_BIN/gpg" <<'EOF2'
#!/bin/bash
# Prints the colon-format listing a test chose for "the downloaded key".
echo "gpg $*" >> "$LOG"
[ ! -e "$TEST_ROOT/gpg-unreadable" ] || exit 2
cat "$TEST_ROOT/gpg-colons"
EOF2
cat > "$FAKE_BIN/docker" <<'EOF2'
#!/bin/bash
echo "docker $*" >> "$LOG"
case "$1" in
    --version) echo "Docker version 29.9.2, build 0000000" ;;
    info) [ ! -e "$TEST_ROOT/docker-info-fail" ] || exit 1 ;;
    ps) if [ -e "$TEST_ROOT/docker-containers" ]; then echo abc123; fi ;;
    volume) if [ -e "$TEST_ROOT/docker-volumes" ]; then echo vol1; fi ;;
    compose) [ -e "$TEST_ROOT/docker-installed" ] || exit 1
             echo "Docker Compose version $(cat "$TEST_ROOT/compose-version")" ;;
    *) [ -e "$TEST_ROOT/docker-installed" ] ;;
esac
EOF2
cat > "$FAKE_BIN/systemctl" <<'EOF2'
#!/bin/bash
echo "systemctl $*" >> "$LOG"
if [ "$1" = is-active ] && [ -e "$TEST_ROOT/docker-inactive" ]; then exit 3; fi
if [ "$1" = enable ]; then rm -f "$TEST_ROOT/docker-inactive"; fi
exit 0
EOF2
for tool in usermod gpasswd adduser groupadd; do
    cat > "$FAKE_BIN/$tool" <<EOF2
#!/bin/bash
echo "$tool \$*" >> "\$LOG"
EOF2
done
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
echo "ctl $* WHEELHOUSE=${VESSELSTACK_WHEELHOUSE:-} OFFLINE=${VESSELSTACK_OFFLINE:-}" >> "$LOG"
# Behave like the released v1.2.0/v1.3.0 controllers: "update" reads ONLY $1, as the
# release directory, and fails on anything else (for example a leading --offline).
if [ "$1" = update ]; then
    [ -x "$2/install.sh" ] || { echo "Not a VesselStack release directory: $2" >&2; exit 1; }
    [ "$#" -eq 2 ] || { echo "old ctl: unexpected extra arguments" >&2; exit 1; }
fi
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

DOCKER_FPR=9DC858229FC7DD38854AE2D88D81803C0EBFCD88
# set_os ID CODENAME [EXTRA os-release lines...]: the os-release file the script reads.
set_os() {
    local id=$1 codename=$2
    shift 2
    { printf 'ID=%s\nVERSION_CODENAME=%s\nPRETTY_NAME="Fake %s %s"\n' "$id" "$codename" "$id" "$codename"; printf '%s\n' "$@"; } > "$TEST_ROOT/os-release"
}

reset_state() {
    rm -rf -- "$RELEASE_BASE" "$APP_ROOT" "$LOG" "$TEST_ROOT/curl.log" "$TEST_ROOT/docker-installed" "$TEST_ROOT/docker-inactive" \
        "$TEST_ROOT/missing-pkgs" "$TEST_ROOT/answers" "$TEST_ROOT/wizard-mode" "$TEST_ROOT/pwned-wizard" "$SERVE"/*.fail \
        "$TEST_ROOT/etc-apt" "$TEST_ROOT/var-lib-docker" "$TEST_ROOT/installed-pkgs" "$TEST_ROOT/apt-update-fail" \
        "$TEST_ROOT/apt-install-fail" "$TEST_ROOT/docker-info-fail" "$TEST_ROOT/docker-containers" "$TEST_ROOT/docker-volumes" \
        "$TEST_ROOT/gpg-unreadable" "$TEST_ROOT/bin-nodocker" "$TEST_ROOT/apt-sim-remove"
    : > "$TEST_ROOT/apt-candidates"
    printf 'docker-ce 5:29.9.2-1~debian.12~bookworm\ndocker-compose-plugin 5.6.1-1~debian.12~bookworm\n' > "$TEST_ROOT/apt-candidates-repo"
    : > "$TEST_ROOT/installed-pkgs"
    printf 'arm64\n' > "$TEST_ROOT/dpkg-arch"
    printf 'v5.6.1\n' > "$TEST_ROOT/compose-version"
    set_os debian bookworm
    printf 'pub:-:4096:1:8D81803C0EBFCD88:1487788586:::-:::escaESCA::::::23::0:\nfpr:::::::::%s:\nuid:-::::1487792064::B50C6A3598EE2C27B34302761B93B277BF674C93::Docker Release (CE deb) <docker@docker.com>::::::::::0:\nsub:-:4096:1:7EA0A9C3F273FCD8:1487788586:::::s::::::23:\nfpr:::::::::D3306A018370199E527AE7997EA0A9C3F273FCD8:\n' \
        "$DOCKER_FPR" > "$TEST_ROOT/gpg-colons"
    : > "$LOG"
    touch "$TEST_ROOT/docker-installed"
    serve_tarball "$GOOD"
    printf 'FAKE-DOCKER-KEY\n' > "$SERVE/gpg"
    write_config
}

RC=0
run() {  # args... (stdin is never a terminal)
    RC=0
    # SUDO_USER is always unset here (CI runs the test under sudo, which sets it); cases set it via EXTRA_ENV.
    env -u SUDO_USER PATH="${RUN_PATH:-$FAKE_BIN:$PATH}" TEST_ROOT="$TEST_ROOT" LOG="$LOG" VESSELSTACK_ROOT="$APP_ROOT" \
        VESSELSTACK_VERSION="$VER" VESSELSTACK_BOOTSTRAP_DOCTOR_WAIT=0 NO_COLOR=1 \
        VESSELSTACK_OS_RELEASE="$TEST_ROOT/os-release" VESSELSTACK_APT_KEYRING_DIR="$TEST_ROOT/etc-apt/keyrings" \
        VESSELSTACK_APT_SOURCES_DIR="$TEST_ROOT/etc-apt/sources.list.d" VESSELSTACK_APT_SOURCES_FILE="$TEST_ROOT/etc-apt/sources.list" \
        VESSELSTACK_DOCKER_DATA_DIR="$TEST_ROOT/var-lib-docker" \
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
# The reused saved config goes through the same strict validation as new input.
reset_state
run --yes
expect_rc 0 "run that saves a config"
rm -rf "$APP_ROOT"; : > "$LOG"
printf 'SOME_UNKNOWN_KEY=1\n' >> "$RELEASE_BASE/vesselstack.env"
run --yes
expect_rc 1 "reused config with an unknown key"
refuse_log "^install "

# ---- 6. prerequisites ------------------------------------------------------------
reset_state
printf 'rsync\njq\n' > "$TEST_ROOT/missing-pkgs"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 0 "missing apt packages"
expect_log "^apt-get update"
expect_log "^apt-get install -y --no-install-recommends (.* )?jq rsync"
expect_out "installed Debian packages"

# Docker. The default fake docker binary counts as "Docker already installed". To simulate
# a machine without Docker the script runs under a PATH made only of the stubs (minus docker)
# and symlinks to every host tool except docker*, because CI runners ship a real docker in
# /usr/bin that hiding the stub would expose. (The apt-get stub puts the docker stub on that
# PATH when docker-ce is "installed".)
docker_absent() {
    local nodocker="$TEST_ROOT/bin-nodocker" dir tool name
    rm -rf -- "$nodocker"
    install -d "$nodocker"
    for tool in "$FAKE_BIN"/*; do
        name=${tool##*/}
        [ "$name" = docker ] || ln -s "$tool" "$nodocker/$name"
    done
    for dir in /usr/local/sbin /usr/local/bin /usr/sbin /usr/bin /sbin /bin; do
        [ -d "$dir" ] || continue
        for tool in "$dir"/*; do
            name=${tool##*/}
            case "$name" in docker|docker-*) continue ;; esac
            if ! { [ -x "$tool" ] && [ ! -d "$tool" ] && [ ! -e "$nodocker/$name" ]; }; then continue; fi
            ln -s "$tool" "$nodocker/$name"
        done
    done
    ! PATH="$nodocker" command -v docker >/dev/null 2>&1 || fail_test "docker is still on the isolated PATH"
    RUN_PATH=$nodocker
    rm -f "$TEST_ROOT/docker-installed"
}
docker_restore() { RUN_PATH=; }
KEYFILE="$TEST_ROOT/etc-apt/keyrings/docker.asc"
SRCFILE="$TEST_ROOT/etc-apt/sources.list.d/docker.sources"
# line_of REGEX: first line number in the calls log.
line_of() { grep -nE -- "$1" "$LOG" | head -n 1 | cut -d: -f1; }
expect_before() {  # REGEX1 REGEX2: the first call happens before the second
    local a b
    a=$(line_of "$1"); b=$(line_of "$2")
    { [ -n "$a" ] && [ -n "$b" ] && [ "$a" -lt "$b" ]; } || fail_test "/$1/ must come before /$2/ in the calls"
}
expect_sources() {  # URI SUITE ARCH
    [ -f "$SRCFILE" ] || fail_test "no $SRCFILE"
    grep -qxF "URIs: $1" "$SRCFILE" || fail_test "sources: wrong URIs ($(grep URIs "$SRCFILE"))"
    grep -qxF "Suites: $2" "$SRCFILE" || fail_test "sources: wrong Suites"
    grep -qxF "Architectures: $3" "$SRCFILE" || fail_test "sources: wrong Architectures"
    grep -qxF "Components: stable" "$SRCFILE" || fail_test "sources: wrong Components"
    grep -qxF "Signed-By: $KEYFILE" "$SRCFILE" || fail_test "sources: wrong Signed-By"
    cmp -s "$KEYFILE" "$SERVE/gpg" || fail_test "installed key differs from the verified download"
}
expect_untouched_apt() {
    [ ! -e "$KEYFILE" ] || fail_test "key must not be installed"
    [ ! -e "$SRCFILE" ] || fail_test "repository entry must not be written"
}
refuse_group_change() { refuse_log "^(usermod|gpasswd|adduser|groupadd) "; }

# Without --yes and without a terminal the Docker question cannot be answered.
reset_state
docker_absent
printf 'y\n' > "$TEST_ROOT/answers"   # "Continue?" only
EXTRA_ENV=VESSELSTACK_BOOTSTRAP_ANSWERS="$TEST_ROOT/answers" run --no-wizard --config "$TEST_ROOT/in.env"
EXTRA_ENV=
docker_restore
expect_rc 1 "no terminal and no --yes"
expect_out "no terminal|No answer"
refuse_log "^apt-get (install|remove)"
expect_untouched_apt

# Declining the Docker installation changes nothing.
reset_state
docker_absent
printf 'y\nn\n' > "$TEST_ROOT/answers"   # Continue? yes, install Docker? no
EXTRA_ENV=VESSELSTACK_BOOTSTRAP_ANSWERS="$TEST_ROOT/answers" run --no-wizard --config "$TEST_ROOT/in.env"
EXTRA_ENV=
docker_restore
expect_rc 1 "declining the Docker install"
expect_out "Docker is required"
refuse_log "^apt-get (install|remove)"
expect_untouched_apt
[ ! -e "$TEST_ROOT/curl.log" ] || ! grep -q 'download.docker.com' "$TEST_ROOT/curl.log" || fail_test "nothing may be downloaded before the answer"
expect_no_release

# Fresh Raspberry Pi OS bookworm (arm64): official repository, verified key, full package set.
reset_state
docker_absent
touch "$TEST_ROOT/docker-inactive"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
docker_restore
expect_rc 0 "fresh bookworm arm64"
expect_sources "https://download.docker.com/linux/debian" bookworm arm64
expect_log "^apt-get update"
expect_log "^apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin$"
expect_before "^apt-get update" "^apt-get install -y docker-ce"
expect_log "^gpg .*--show-keys --with-colons"
expect_log "^systemctl enable --now docker"
expect_before "^apt-get install -y docker-ce" "^systemctl enable --now docker"
expect_log "^docker --version"
expect_log "^docker compose version"
expect_log "^docker info"
expect_before "^systemctl enable --now docker" "^docker info"
expect_out "signing key verified"
expect_out "Docker Compose version v5.6.1"
refuse_log "docker\.io|docker-compose-v2|apt-get remove"
refuse_log "^apt-get install .*--no-install-recommends .*docker"
refuse_group_change
grep -qE -- "--proto '?=https'? .*https://download.docker.com/linux/debian/gpg" "$TEST_ROOT/curl.log" || fail_test "key must be fetched over https only"
grep -qE -- "--tlsv1.2" "$TEST_ROOT/curl.log" || fail_test "key download must require TLS 1.2"
[ ! -e "$TEST_ROOT/etc-apt/keyrings/.docker-key-verify" ] || fail_test "temporary key directory was not removed"
expect_out "No docker group change"
# Re-running when everything is in place is a no-op.
: > "$LOG"
rm -rf -- "$APP_ROOT" "$RELEASE_BASE"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 0 "re-run after the install"
expect_out "Docker and Docker Compose v2 are installed"
refuse_log "^apt-get"

# Raspberry Pi OS / Debian 13 (trixie).
reset_state
docker_absent
set_os debian trixie
printf 'docker-ce 5:29.9.2-1~debian.13~trixie\ndocker-compose-plugin 2.40.3-1~debian.13~trixie\n' > "$TEST_ROOT/apt-candidates-repo"
printf 'v2.40.3\n' > "$TEST_ROOT/compose-version"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
docker_restore
expect_rc 0 "trixie"
expect_sources "https://download.docker.com/linux/debian" trixie arm64
expect_out "Docker Compose version v2.40.3"

# Debian amd64.
reset_state
docker_absent
printf 'amd64\n' > "$TEST_ROOT/dpkg-arch"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
docker_restore
expect_rc 0 "amd64"
expect_sources "https://download.docker.com/linux/debian" bookworm amd64

# Ubuntu uses Docker's ubuntu repository (and UBUNTU_CODENAME for derivatives).
reset_state
docker_absent
set_os ubuntu noble
run --yes --no-wizard --config "$TEST_ROOT/in.env"
docker_restore
expect_rc 0 "ubuntu"
expect_sources "https://download.docker.com/linux/ubuntu" noble arm64
reset_state
docker_absent
set_os linuxmint wilma 'ID_LIKE="ubuntu debian"' 'UBUNTU_CODENAME=noble'
run --yes --no-wizard --config "$TEST_ROOT/in.env"
docker_restore
expect_rc 0 "ubuntu derivative"
expect_sources "https://download.docker.com/linux/ubuntu" noble arm64

# A key that does not match the pinned fingerprint is refused before anything is written.
reset_state
docker_absent
sed -i 's/9DC858229FC7DD38854AE2D88D81803C0EBFCD88/0000000000000000000000000000000000000000/' "$TEST_ROOT/gpg-colons"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
docker_restore
expect_rc 1 "fingerprint mismatch"
expect_out "Refusing to trust the downloaded Docker key"
expect_out "expected: $DOCKER_FPR"
expect_out "Nothing was changed"
refuse_log "^apt-get (install|remove|update)"
expect_untouched_apt
expect_no_release
# A file holding Docker's key plus a second key is refused too (apt trusts every key in Signed-By).
reset_state
docker_absent
printf 'pub:-:4096:1:AAAAAAAAAAAAAAAA:1487788586:::-:::escaESCA::::::23::0:\nfpr:::::::::AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA:\n' >> "$TEST_ROOT/gpg-colons"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
docker_restore
expect_rc 1 "two keys in the file"
expect_out "2 key\\(s\\)"
refuse_log "^apt-get (install|remove|update)"
expect_untouched_apt
# Docker's key listed second (an attacker's key first) is refused as well.
reset_state
docker_absent
printf 'pub:-:4096:1:AAAAAAAAAAAAAAAA:1487788586:::-:::escaESCA::::::23::0:\nfpr:::::::::AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA:\n%s\n' "$(cat "$TEST_ROOT/gpg-colons")" > "$TEST_ROOT/gpg-colons.new"
mv "$TEST_ROOT/gpg-colons.new" "$TEST_ROOT/gpg-colons"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
docker_restore
expect_rc 1 "attacker key first"
expect_untouched_apt
# Download and gpg failures stop cleanly.
reset_state
docker_absent
touch "$SERVE/gpg.fail"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
docker_restore
expect_rc 1 "key download fails"
expect_out "Could not download Docker's package signing key"
expect_out "Failing command: curl"
expect_untouched_apt
reset_state
docker_absent
touch "$TEST_ROOT/gpg-unreadable"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
docker_restore
expect_rc 1 "gpg cannot read the key"
expect_out "gpg could not read"
expect_untouched_apt

# 32-bit Raspberry Pi OS (armhf, even on a 64-bit kernel) is refused with reflash advice.
reset_state
docker_absent
printf 'armhf\n' > "$TEST_ROOT/dpkg-arch"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
docker_restore
expect_rc 1 "armhf"
expect_out "32-bit Raspberry Pi OS"
expect_out "Raspberry Pi OS \\(64-bit\\)"
expect_out "Raspberry Pi Imager"
expect_out "Nothing was changed"
refuse_log "^apt-get (install|remove|update)"
expect_untouched_apt
[ ! -e "$TEST_ROOT/curl.log" ] || ! grep -q 'download.docker.com' "$TEST_ROOT/curl.log" || fail_test "no download for unsupported systems"
expect_no_release
# Other architectures and other distributions get a clear message too.
reset_state
docker_absent
printf 'riscv64\n' > "$TEST_ROOT/dpkg-arch"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
docker_restore
expect_rc 1 "unsupported architecture"
expect_out "supports 64-bit ARM \\(arm64\\) and x86-64 \\(amd64\\)"
reset_state
docker_absent
set_os kali kali-rolling 'ID_LIKE=debian'
run --yes --no-wizard --config "$TEST_ROOT/in.env"
docker_restore
expect_rc 1 "unsupported Debian derivative"
expect_out "supports Debian, Raspberry Pi OS \\(64-bit\\) and Ubuntu"
refuse_log "^apt-get (install|remove|update)"

# Conflicting unofficial packages: declined, accepted interactively, accepted with --yes.
reset_state
docker_absent
printf 'docker.io\ncontainerd\nrunc\n' > "$TEST_ROOT/installed-pkgs"
printf 'y\ny\nn\n' > "$TEST_ROOT/answers"   # Continue, install Docker, then the last question: no
EXTRA_ENV=VESSELSTACK_BOOTSTRAP_ANSWERS="$TEST_ROOT/answers" run --no-wizard --config "$TEST_ROOT/in.env"
EXTRA_ENV=
docker_restore
expect_rc 1 "declining the removal of conflicting packages"
expect_out "must be removed first: docker.io containerd runc"
expect_out "Nothing was changed"
refuse_log "^apt-get (install|remove)"
expect_untouched_apt
reset_state
docker_absent
printf 'docker.io\ncontainerd\n' > "$TEST_ROOT/installed-pkgs"
printf 'y\ny\ny\ny\ny\ny\n' > "$TEST_ROOT/answers"   # Continue, install Docker, remove packages, then yes to everything after
EXTRA_ENV=VESSELSTACK_BOOTSTRAP_ANSWERS="$TEST_ROOT/answers" run --no-wizard --config "$TEST_ROOT/in.env"
EXTRA_ENV=
docker_restore
expect_rc 0 "accepting the removal"
expect_log "^apt-get remove -y docker.io containerd$"
refuse_log "^apt-get purge|autoremove"
expect_log "^apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin$"
expect_before "^apt-get update" "^apt-get remove"
expect_before "^apt-get remove" "^apt-get install -y docker-ce"
expect_sources "https://download.docker.com/linux/debian" bookworm arm64
reset_state
docker_absent
printf 'docker.io\ndocker-compose\npodman-docker\n' > "$TEST_ROOT/installed-pkgs"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
docker_restore
expect_rc 0 "--yes removes conflicting packages"
expect_log "^apt-get remove -y docker.io docker-compose podman-docker$"
expect_log "^apt-get install -y docker-ce"
# Packages that apt would remove as a side effect are named in the question and the output.
reset_state
docker_absent
printf 'runc\n' > "$TEST_ROOT/installed-pkgs"
printf 'Remv runc [1.1.5]\nRemv podman [4.3.1]\n' > "$TEST_ROOT/apt-sim-remove"
printf 'y\ny\nn\n' > "$TEST_ROOT/answers"
EXTRA_ENV=VESSELSTACK_BOOTSTRAP_ANSWERS="$TEST_ROOT/answers" run --no-wizard --config "$TEST_ROOT/in.env"
EXTRA_ENV=
expect_rc 1 "declining the removal of dependents"
expect_out "apt would also remove packages that depend on them: podman"
expect_out "Remove runc \\(and, because they depend on them: podman\\)"
refuse_log "^apt-get (install|remove) "
expect_log "^apt-get -s remove runc$"
printf 'y\ny\ny\ny\ny\ny\n' > "$TEST_ROOT/answers"
EXTRA_ENV=VESSELSTACK_BOOTSTRAP_ANSWERS="$TEST_ROOT/answers" run --no-wizard --config "$TEST_ROOT/in.env"
EXTRA_ENV=
docker_restore
expect_rc 0 "accepting the removal with dependents"
expect_log "^apt-get remove -y runc$"
expect_out "removed conflicting packages: runc \\(and dependents: podman\\)"
# The repository does not offer docker-ce: nothing is removed.
reset_state
docker_absent
printf 'docker.io\n' > "$TEST_ROOT/installed-pkgs"
: > "$TEST_ROOT/apt-candidates-repo"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
docker_restore
expect_rc 1 "repository without docker-ce"
expect_out "does not offer docker-ce"
refuse_log "^apt-get (remove|install -y docker)"
# Conflicts while a daemon holds containers: --yes is not enough ...
reset_state
printf 'docker.io\n' > "$TEST_ROOT/installed-pkgs"
rm -f "$TEST_ROOT/docker-installed"   # Compose is missing
touch "$TEST_ROOT/docker-containers"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 1 "containers present with --yes"
expect_out "already holds 1 container\\(s\\)"
expect_out "--yes is not enough"
refuse_log "^apt-get (install|remove|update)"
expect_untouched_apt
# ... and the same for volumes only.
reset_state
printf 'docker.io\n' > "$TEST_ROOT/installed-pkgs"
rm -f "$TEST_ROOT/docker-installed"   # Compose is missing
touch "$TEST_ROOT/docker-volumes"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 1 "volumes present with --yes"
refuse_log "^apt-get (install|remove|update)"
# ... but an explicit interactive confirmation proceeds (default answer is no).
reset_state
printf 'docker.io\n' > "$TEST_ROOT/installed-pkgs"
rm -f "$TEST_ROOT/docker-installed"   # Compose is missing
touch "$TEST_ROOT/docker-containers"
printf 'y\ny\nn\n' > "$TEST_ROOT/answers"   # Continue, install Docker, then the last question: no
EXTRA_ENV=VESSELSTACK_BOOTSTRAP_ANSWERS="$TEST_ROOT/answers" run --no-wizard --config "$TEST_ROOT/in.env"
EXTRA_ENV=
expect_rc 1 "declining with containers present"
refuse_log "^apt-get (install|remove)"
reset_state
printf 'docker.io\n' > "$TEST_ROOT/installed-pkgs"
rm -f "$TEST_ROOT/docker-installed"   # Compose is missing
touch "$TEST_ROOT/docker-containers"
printf 'y\ny\ny\ny\ny\ny\n' > "$TEST_ROOT/answers"   # Continue, install Docker, remove packages, then yes to everything after
EXTRA_ENV=VESSELSTACK_BOOTSTRAP_ANSWERS="$TEST_ROOT/answers" run --no-wizard --config "$TEST_ROOT/in.env"
EXTRA_ENV=
expect_rc 0 "confirming with containers present"
expect_log "^apt-get remove -y docker.io$"
# A stray Compose v1 package alone is not an engine conflict, so --yes may remove it.
reset_state
printf 'docker-compose\n' > "$TEST_ROOT/installed-pkgs"
rm -f "$TEST_ROOT/docker-installed"
touch "$TEST_ROOT/docker-containers"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 0 "docker-compose v1 removed, plugin installed"
expect_log "^apt-get remove -y docker-compose$"
expect_log "^apt-get install -y docker-compose-plugin$"
refuse_log "^apt-get install -y docker-ce"

# Docker present, Compose plugin missing: only the plugin is installed (candidate already known).
reset_state
rm -f "$TEST_ROOT/docker-installed"
printf 'docker-compose-plugin 5.6.1-1~debian.12~bookworm\n' > "$TEST_ROOT/apt-candidates"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 0 "plugin only"
expect_log "^apt-get install -y docker-compose-plugin$"
refuse_log "docker-ce|docker\.io|apt-get remove"
expect_untouched_apt
refuse_group_change
# ... and the repository is set up first when apt has no candidate for the plugin.
reset_state
rm -f "$TEST_ROOT/docker-installed"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 0 "plugin only after repository setup"
expect_sources "https://download.docker.com/linux/debian" bookworm arm64
expect_before "^apt-get update" "^apt-get install -y docker-compose-plugin"
refuse_log "^apt-get install -y docker-ce|docker\.io"
# Plugin missing and Docker's repository lacks it too: stop with instructions.
reset_state
rm -f "$TEST_ROOT/docker-installed"
: > "$TEST_ROOT/apt-candidates-repo"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 1 "no plugin candidate"
expect_out "does not offer docker-compose-plugin"
refuse_log "^apt-get install -y docker"

# Already satisfied: nothing is installed, downloaded or removed.
reset_state
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 0 "docker already satisfied"
expect_out "Docker and Docker Compose v2 are installed"
refuse_log "^apt-get (install|remove)"
refuse_log "^gpg"
expect_untouched_apt
[ ! -e "$TEST_ROOT/curl.log" ] || ! grep -q 'download.docker.com' "$TEST_ROOT/curl.log" || fail_test "no download when Docker is satisfied"
# ... and a stopped service is only started.
reset_state
touch "$TEST_ROOT/docker-inactive"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 0 "docker installed but stopped"
expect_log "^systemctl enable --now docker"
refuse_log "^apt-get (install|remove)"

# apt-get update fails after the repository was added: say what was left behind.
reset_state
docker_absent
touch "$TEST_ROOT/apt-update-fail"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
docker_restore
expect_rc 1 "apt-get update failure"
expect_out "apt-get update failed after adding Docker's repository"
expect_out "Failing command: apt-get update"
expect_out "To undo: sudo rm "
expect_out "$SRCFILE"
refuse_log "^apt-get (install|remove)"
expect_no_release
# apt-get install fails: command and state are reported.
reset_state
docker_absent
touch "$TEST_ROOT/apt-install-fail"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
docker_restore
expect_rc 1 "apt-get install failure"
expect_out "Failing command: apt-get install -y docker-ce"
expect_out "Re-run this installer"
# Compose older than v2 is rejected after the install.
reset_state
docker_absent
printf 'v1.29.2\n' > "$TEST_ROOT/compose-version"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
docker_restore
expect_rc 1 "Compose v1 after install"
expect_out "needs Compose v2 or newer"

# A leftover docker.list (the old manual instructions) is set aside, with consent.
reset_state
docker_absent
install -d "$TEST_ROOT/etc-apt/sources.list.d"
printf 'deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable\n' > "$TEST_ROOT/etc-apt/sources.list.d/docker.list"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
docker_restore
expect_rc 0 "leftover docker.list"
[ ! -e "$TEST_ROOT/etc-apt/sources.list.d/docker.list" ] || fail_test "docker.list must be set aside"
[ -f "$TEST_ROOT/etc-apt/sources.list.d/docker.list.vesselstack-saved" ] || fail_test "docker.list must be kept under the new name"
expect_sources "https://download.docker.com/linux/debian" bookworm arm64
reset_state
docker_absent
install -d "$TEST_ROOT/etc-apt/sources.list.d"
printf 'deb https://download.docker.com/linux/debian bookworm stable\n' > "$TEST_ROOT/etc-apt/sources.list.d/docker.list"
printf 'y\ny\nn\n' > "$TEST_ROOT/answers"   # Continue, install Docker, then the last question: no
EXTRA_ENV=VESSELSTACK_BOOTSTRAP_ANSWERS="$TEST_ROOT/answers" run --no-wizard --config "$TEST_ROOT/in.env"
EXTRA_ENV=
docker_restore
expect_rc 1 "declining to set aside docker.list"
[ -f "$TEST_ROOT/etc-apt/sources.list.d/docker.list" ] || fail_test "docker.list must stay"
refuse_log "^apt-get (install|remove|update)"
expect_untouched_apt

# Offline mode cannot install Docker.
reset_state
docker_absent
install -d -m 0755 "$TEST_ROOT/bundle/wheelhouse"
install -d "$TEST_ROOT/carry"
cp "$GOOD" "$TEST_ROOT/carry/vesselstack-$VER.tar.gz"
printf '%s  vesselstack-%s.tar.gz\n' "$GOOD_SHA" "$VER" > "$TEST_ROOT/carry/vesselstack-$VER.tar.gz.sha256"
run --yes --offline --bundle "$TEST_ROOT/bundle" --tarball "$TEST_ROOT/carry/vesselstack-$VER.tar.gz" --no-wizard --config "$TEST_ROOT/in.env"
docker_restore
expect_rc 1 "offline without Docker"
expect_out "cannot be installed automatically"
refuse_log "^apt-get"
[ ! -e "$TEST_ROOT/curl.log" ] || fail_test "offline mode must not use curl"
rm -rf "$TEST_ROOT/bundle" "$TEST_ROOT/carry"

# Package lists are refreshed when a base package is missing.
reset_state
printf 'rsync\n' > "$TEST_ROOT/missing-pkgs"
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
expect_log "^ctl update $RELEASE_BASE/$VER WHEELHOUSE= OFFLINE=$"
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
expect_log "^ctl update $RELEASE_BASE/$VER WHEELHOUSE=$TEST_ROOT/bundle/wheelhouse OFFLINE=true$"
refuse_log "^ctl update --offline"
expect_out "VESSELSTACK_OFFLINE=true VESSELSTACK_WHEELHOUSE=$TEST_ROOT/bundle/wheelhouse vesselstackctl update"

# ---- 10. root-controlled release directory ------------------------------------------------
reset_state
install -d -m 0777 "$RELEASE_BASE"
run --yes --no-wizard --config "$TEST_ROOT/in.env"
expect_rc 1 "world-writable release base"
expect_out "root-owned"
refuse_log "^install "
rm -rf "$RELEASE_BASE"

echo "get-vesselstack tests passed"
