#!/bin/bash
# vesselstack-update as root: only a root-controlled update.env and the root-owned
# helper installed under libexec may be used, never anything in the source checkout.
# Needs root and Linux (stat -c); a fake python3 records what would have been executed.

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
TEST_ROOT=$(mktemp -d -p "$TEST_BASE" vs-update-launcher.XXXXXX)
trap 'rm -rf -- "$TEST_ROOT"' EXIT
chmod 0755 "$TEST_ROOT"
SYSTEM_ROOT="$TEST_ROOT/sys"
SOURCE="$TEST_ROOT/source"
TARGET="$TEST_ROOT/target"
FAKE_BIN="$TEST_ROOT/bin"
CONFIG="$SYSTEM_ROOT/etc/vesselstack/update.env"
HELPER_DIR="$SYSTEM_ROOT/usr/local/libexec/vesselstack"
install -d -m 0755 "$SYSTEM_ROOT/etc/vesselstack" "$HELPER_DIR" "$SOURCE/scripts" "$TARGET" "$FAKE_BIN"
chmod 0755 "$SYSTEM_ROOT" "$SYSTEM_ROOT/etc" "$SYSTEM_ROOT/usr" "$SYSTEM_ROOT/usr/local" \
    "$SYSTEM_ROOT/usr/local/libexec" "$SYSTEM_ROOT/etc/vesselstack"
printf 'print("installed")\n' > "$HELPER_DIR/deploy-chat.py"
chmod 0755 "$HELPER_DIR/deploy-chat.py"
printf 'print("checkout")\n' > "$SOURCE/scripts/deploy-chat.py"
cat > "$CONFIG" <<EOT
SOURCE_DIRECTORY="$SOURCE"
TARGET_DIRECTORY="$TARGET"
CHAT_SERVICE="vesselstack-chat.service"
EOT
chmod 0600 "$CONFIG"
# Records the script and arguments instead of running the helper.
printf '#!/bin/bash\necho "RAN $*"\n' > "$FAKE_BIN/python3"
chmod 0755 "$FAKE_BIN/python3"

launcher() {
    env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
        ${LAUNCHER_ENV:+"$LAUNCHER_ENV"} "$REPO_ROOT/scripts/vesselstack-update" "$@"
}
refuses() {
    local message=$1
    shift
    if launcher "$@" >"$TEST_ROOT/out" 2>"$TEST_ROOT/err"; then
        echo "launcher ran although it should refuse: $message" >&2
        exit 1
    fi
    grep -q -- "$message" "$TEST_ROOT/err" || { echo "missing message '$message':" >&2; cat "$TEST_ROOT/err" >&2; exit 1; }
    if grep -q '^RAN' "$TEST_ROOT/out"; then
        echo "helper executed despite refusal: $message" >&2
        exit 1
    fi
}

# Happy path: the INSTALLED helper runs, the checkout copy never does.
out=$(launcher)
grep -q "^RAN -I $HELPER_DIR/deploy-chat.py " <<<"$out"
if grep -q "$SOURCE/scripts/deploy-chat.py" <<<"$out"; then
    echo "launcher executed the checkout helper as root" >&2
    exit 1
fi

# Missing installed helper: refuse (even though the checkout has one) and say how to fix it.
mv "$HELPER_DIR/deploy-chat.py" "$TEST_ROOT/helper.saved"
refuses 'Run the VesselStack installer once'
mv "$TEST_ROOT/helper.saved" "$HELPER_DIR/deploy-chat.py"

# Helper integrity: ownership and group/world write on the file and on each parent.
chmod 0775 "$HELPER_DIR/deploy-chat.py"
refuses 'must be root-owned and not group/world-writable'
chmod 0755 "$HELPER_DIR/deploy-chat.py"
chown 1234:1234 "$HELPER_DIR/deploy-chat.py"
refuses 'must be root-owned and not group/world-writable'
chown 0:0 "$HELPER_DIR/deploy-chat.py"
chmod 0777 "$HELPER_DIR"
refuses 'must be root-owned and not group/world-writable'
chmod 0755 "$HELPER_DIR"
chown 1234:1234 "$SYSTEM_ROOT/usr/local"
refuses 'must be root-owned and not group/world-writable'
chown 0:0 "$SYSTEM_ROOT/usr/local"
# A symlinked helper is judged by its real location.
install -d -m 0777 "$TEST_ROOT/writable"
mv "$HELPER_DIR/deploy-chat.py" "$TEST_ROOT/writable/deploy-chat.py"
ln -s "$TEST_ROOT/writable/deploy-chat.py" "$HELPER_DIR/deploy-chat.py"
refuses 'must be root-owned and not group/world-writable'
rm -f -- "$HELPER_DIR/deploy-chat.py"
mv "$TEST_ROOT/writable/deploy-chat.py" "$HELPER_DIR/deploy-chat.py"
rmdir "$TEST_ROOT/writable"
launcher >/dev/null

# update.env integrity: sourced as root, so same rules, plus never inside the checkout.
chmod 0666 "$CONFIG"
refuses 'must be root-owned and not group/world-writable'
chmod 0600 "$CONFIG"
chown 1234:1234 "$CONFIG"
refuses 'must be root-owned and not group/world-writable'
chown 0:0 "$CONFIG"
chmod 0777 "$SYSTEM_ROOT/etc/vesselstack"
refuses 'must be root-owned and not group/world-writable'
chmod 0755 "$SYSTEM_ROOT/etc/vesselstack"
# A malicious config must not even be sourced.
marker="$TEST_ROOT/sourced"
printf 'touch "%s"\n' "$marker" >> "$CONFIG"
chown 1234:1234 "$CONFIG"
refuses 'must be root-owned'
test ! -e "$marker"
chown 0:0 "$CONFIG"
sed -i '$d' "$CONFIG"
# Root-owned but stored inside the source checkout (owned by the unprivileged user).
install -d -m 0755 "$SOURCE/conf"
cp "$CONFIG" "$SOURCE/conf/update.env"
chown 0:0 "$SOURCE/conf/update.env"
chown 1234:1234 "$SOURCE"
LAUNCHER_ENV="VESSELSTACK_UPDATE_CONFIG=$SOURCE/conf/update.env" refuses 'must be root-owned'
chown 0:0 "$SOURCE"
LAUNCHER_ENV="VESSELSTACK_UPDATE_CONFIG=$SOURCE/conf/update.env" refuses 'inside SOURCE_DIRECTORY'

# The helper also guards itself when executed directly as root (e.g. by hand).
mkdir -p "$TEST_ROOT/tmpcopy"
chmod 0777 "$TEST_ROOT/tmpcopy"
cp "$REPO_ROOT/scripts/deploy-chat.py" "$TEST_ROOT/tmpcopy/deploy-chat.py"
if python3_out=$(/usr/bin/python3 -I "$TEST_ROOT/tmpcopy/deploy-chat.py" --target "$TARGET" 2>&1); then
    echo "helper ran as root from a writable location" >&2
    exit 1
fi
grep -q 'Refusing to run as root' <<<"$python3_out"
installed_check=$(/usr/bin/python3 -I -c '
import importlib.util, sys
spec = importlib.util.spec_from_file_location("d", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.require_root_controlled(sys.argv[2])
print("ok")' "$REPO_ROOT/scripts/deploy-chat.py" "$HELPER_DIR/deploy-chat.py")
test "$installed_check" = ok

echo "update launcher checks passed"
