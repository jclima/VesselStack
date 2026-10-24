#!/bin/bash
# Transactional install ordering, offline mode, upgrade-path preflight and the
# offline bundle tooling, against fake docker/pip/systemctl. Run as root on Linux.

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT
FAKE_BIN="$TEST_ROOT/bin"
APP_ROOT="$TEST_ROOT/opt/vesselstack"
DATA_ROOT="$TEST_ROOT/opt/vesselstack-data"
SYSTEM_ROOT="$TEST_ROOT/system-root"
CONFIG="$TEST_ROOT/vesselstack.env"
WHEELHOUSE="$TEST_ROOT/bundle-in/wheelhouse"
IMAGES_PRESENT="$TEST_ROOT/images-present"
VERSION=$(<"$REPO_ROOT/VERSION")
install -d "$FAKE_BIN" "$WHEELHOUSE"
# Work on a copy so the InfluxDB bootstrap (needs a live database) can be stubbed.
RELEASE="$TEST_ROOT/release"
cp -a "$REPO_ROOT" "$RELEASE"
rm -rf "$RELEASE/.git" "$RELEASE/generated"
printf '#!/bin/bash\nexit 0\n' > "$RELEASE/scripts/bootstrap-influx.sh"
: > "$IMAGES_PRESENT"
export TEST_ROOT IMAGES_PRESENT

cat > "$FAKE_BIN/docker" <<'EOF2'
#!/bin/bash
echo "$*" >> "$TEST_ROOT/docker.log"
case "${1:-}" in
    image)
        [ "${2:-}" = inspect ] && grep -qxF "$3" "$IMAGES_PRESENT"
        exit ;;
    pull) exit 0 ;;
    save)
        out=$3; image=$4
        printf '%s\n' "$image" > "$out"; exit 0 ;;
    load)
        cat "$3" >> "$IMAGES_PRESENT"; exit 0 ;;
    compose)
        if [[ " $* " == *" config --images "* ]]; then
            envf=; composef=; args=("$@")
            for ((i = 0; i < ${#args[@]}; i++)); do
                case "${args[$i]}" in
                    --env-file) envf=${args[$((i + 1))]} ;;
                    -f) composef=${args[$((i + 1))]} ;;
                esac
            done
            set -a
            # shellcheck disable=SC1090
            . "$envf"
            set +a
            while IFS= read -r line; do
                case "$line" in
                    *signalk-server*) [[ " $* " == *" signalk "* ]] || continue ;;
                    *'${AIS_IMAGE}'*) [[ " $* " == *" ais "* ]] || continue ;;
                esac
                eval "echo $line"
            done < <(sed -n 's/^ *image: *//p' "$composef")
        fi
        exit 0 ;;
esac
exit 0
EOF2
for command in systemctl curl; do printf '#!/bin/bash\nexit 0\n' > "$FAKE_BIN/$command"; done
cat > "$FAKE_BIN/python3" <<'EOF2'
#!/bin/bash
if [ "${1:-}" = -m ] && [ "${2:-}" = venv ]; then
    echo "venv $3" >> "$TEST_ROOT/pip.log"
    install -d "$3/bin"
    cat > "$3/bin/pip" <<PIP
#!/bin/bash
echo "pip \$*" >> "$TEST_ROOT/pip.log"
[ ! -e "$TEST_ROOT/pip-fail" ] || { echo "simulated network failure" >&2; exit 1; }
exit 0
PIP
    printf '#!%s/bin/python\nVIRTUAL_ENV=%s\n' "$3" "$3" > "$3/bin/tool"
    chmod +x "$3/bin/pip" "$3/bin/tool"
    exit 0
fi
if [ "${1:-}" = -m ] && [ "${2:-}" = pip ] && [ "${3:-}" = download ]; then
    echo "pip $*" >> "$TEST_ROOT/pip.log"
    while [ $# -gt 0 ]; do
        [ "$1" = --dest ] && touch "$2/sqlite_vec-0.0.0-py3-none-any.whl"
        shift
    done
    exit 0
fi
exec /usr/bin/python3 "$@"
EOF2
chmod +x "$FAKE_BIN"/*

cat > "$CONFIG" <<EOF2
BOAT_NAME="Test Vessel"
BOAT_TYPE="Test boat"
BOAT_MMSI=""
BOAT_CALLSIGN=""
BOAT_LOA_M=""
BOAT_BEAM_M=""
BOAT_TIMEZONE="UTC"
BOAT_UNITS="metric"
VESSELSTACK_USER="$(id -un)"
VESSELSTACK_UID="$(id -u)"
VESSELSTACK_GID="$(id -g)"
VESSELSTACK_ROOT="$APP_ROOT"
VESSELSTACK_DATA="$DATA_ROOT"
VESSELSTACK_BACKUP="$TEST_ROOT/backups"
SIGNALK_MODE="existing"
SIGNALK_VERSION="v2.27.0"
SIGNALK_URL="http://127.0.0.1:3000"
SOCKETCAN_ENABLE="false"
AIS_ENABLE="false"
HOME_ASSISTANT_URL="http://127.0.0.1:8123"
INFLUXDB_URL="http://127.0.0.1:8086"
INFLUXDB_ORG="vesselstack"
INFLUXDB_USERNAME="boatadmin"
INFLUXDB_PASSWORD="GENERATE"
INFLUXDB_TOKEN="GENERATE"
GRAFANA_ADMIN_PASSWORD="GENERATE"
MQTT_USERNAME="homeassistant"
MQTT_PASSWORD="GENERATE"
BOAT_CHAT_SETTINGS_TOKEN="GENERATE"
BOAT_CHAT_ACCESS_TOKEN="GENERATE"
VESSELSTACK_UNTRUSTED_INTERFACE="wlan0"
VESSELSTACK_FIREWALL_ENABLE="false"
EOF2

installer() {
    env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" "$RELEASE/install.sh" --config "$CONFIG" "$@"
}
fail() { echo "FAIL: $*" >&2; exit 1; }
no_staging() { [ -z "$(find "$APP_ROOT" -maxdepth 1 -name '.venv-staging.*' -print -quit)" ] || fail "staging directory left behind"; }
snapshot() { (cd "$APP_ROOT" && find . -path ./.venv-staging.\* -prune -o -type f -print0 | sort -z | xargs -0 sha256sum; sha256sum "$SYSTEM_ROOT"/etc/systemd/system/*.service) | sha256sum; }

# --- Offline preflight: no wheelhouse, then missing images ------------------
if installer --offline --dry-run >/dev/null 2>"$TEST_ROOT/err"; then fail "offline preflight accepted a missing wheelhouse"; fi
grep -q 'no Python packages found' "$TEST_ROOT/err"
test ! -e "$APP_ROOT" || fail "preflight created files"

touch "$WHEELHOUSE/sqlite_vec-0.0.0-py3-none-any.whl"
export VESSELSTACK_WHEELHOUSE="$WHEELHOUSE"
installer --offline --dry-run >"$TEST_ROOT/out" 2>"$TEST_ROOT/err" || { cat "$TEST_ROOT/err" >&2; exit 1; }
grep -q 'Preflight passed' "$TEST_ROOT/out"
grep -q 'Offline mode' "$TEST_ROOT/out"
grep -q 'eclipse-mosquitto:2.1.2-alpine' "$TEST_ROOT/err"
grep -q 'prepare-offline-bundle.sh --load' "$TEST_ROOT/err"
grep -q 'docker load' "$TEST_ROOT/err"
# Services cannot start without images: fail before touching anything.
if installer --offline --start >/dev/null 2>"$TEST_ROOT/err"; then fail "--offline --start accepted missing images"; fi
grep -q 'influxdb:2.9.1' "$TEST_ROOT/err"
test ! -e "$APP_ROOT" || fail "failed offline preflight created files"
test ! -s "$TEST_ROOT/pip.log" 2>/dev/null || fail "pip ran although preflight failed"

# --- Offline install with every image present --------------------------------
for image in homeassistant/home-assistant:2026.6.4 influxdb:2.9.1 grafana/grafana:13.0.1 \
    prom/prometheus:v2.55.0 eclipse-mosquitto:2.1.2-alpine lscr.io/linuxserver/heimdall:v2.8.1-ls357; do
    echo "$image" >> "$IMAGES_PRESENT"
done
: > "$TEST_ROOT/docker.log"
installer --offline --start >/dev/null 2>"$TEST_ROOT/err" || { cat "$TEST_ROOT/err" >&2; fail "offline install failed"; }
grep -q -- "--no-index --find-links $WHEELHOUSE -r .*requirements.txt" "$TEST_ROOT/pip.log" || fail "offline pip flags missing"
grep -q -- 'compose .* up -d --pull never' "$TEST_ROOT/docker.log" || fail "offline start must not pull"
if grep -E '^(pull|compose .* pull)' "$TEST_ROOT/docker.log"; then fail "offline install pulled an image"; fi
grep -qx "$VERSION" "$APP_ROOT/config/installed-version"
test "$(head -n 1 "$APP_ROOT/boat-chat/.venv/bin/tool")" = "#!$APP_ROOT/boat-chat/.venv/bin/python" || fail "venv scripts not relocated"
grep -qx "VIRTUAL_ENV=$APP_ROOT/boat-chat/.venv" "$APP_ROOT/boat-chat/.venv/bin/tool" || fail "venv path not relocated"
no_staging
test "$(stat -c '%u' "$APP_ROOT/boat-chat/.venv")" = 0
# The service user can read and execute the venv but never write it.
test "$(( 0$(stat -c '%a' "$APP_ROOT/boat-chat/.venv/bin/tool") & 0044 ))" -ne 0 || fail "venv not readable by the service user"
test -z "$(find "$APP_ROOT/boat-chat/.venv" -not -type l \( -perm -g=w -o -perm -o=w \) -print -quit)"

# --- A failed package install changes nothing ---------------------------------
printf '1.3.0\n' > "$APP_ROOT/config/installed-version"
before=$(snapshot)
touch "$TEST_ROOT/pip-fail"
if installer >/dev/null 2>"$TEST_ROOT/err"; then fail "install succeeded although pip failed"; fi
grep -q 'Nothing was changed' "$TEST_ROOT/err"
test "$(snapshot)" = "$before" || fail "failed install modified the installation"
grep -qx '1.3.0' "$APP_ROOT/config/installed-version"
no_staging
# Offline failure message names the wheelhouse.
if installer --offline >/dev/null 2>"$TEST_ROOT/err"; then fail "offline install succeeded although pip failed"; fi
grep -q "from $WHEELHOUSE" "$TEST_ROOT/err"
test "$(snapshot)" = "$before"
rm -f "$TEST_ROOT/pip-fail"

# --- Upgrade path is a preflight check; downgrades are refused ----------------
printf '9.0.0\n' > "$APP_ROOT/config/installed-version"
before=$(snapshot)
for flags in "--dry-run" ""; do
    # shellcheck disable=SC2086
    if installer $flags >/dev/null 2>"$TEST_ROOT/err"; then fail "downgrade accepted ($flags)"; fi
    grep -q 'Downgrade from 9.0.0 to .* is not supported' "$TEST_ROOT/err"
    test "$(snapshot)" = "$before" || fail "refused downgrade modified the installation"
done
printf '1.3.0\n' > "$APP_ROOT/config/installed-version"
installer --dry-run >/dev/null 2>&1
installer >/dev/null 2>&1
grep -qx "$VERSION" "$APP_ROOT/config/installed-version"

# --- Offline bundle tooling -------------------------------------------------------
: > "$TEST_ROOT/pip.log"
BUNDLE="$TEST_ROOT/bundle"
env PATH="$FAKE_BIN:$PATH" "$RELEASE/scripts/prepare-offline-bundle.sh" --output "$BUNDLE" >/dev/null
ls "$BUNDLE"/wheelhouse/*.whl >/dev/null
test "$(find "$BUNDLE/images" -type f | wc -l)" -eq 8 || fail "expected one tarball per image (8)"
grep -qx "release=$VERSION" "$BUNDLE/BUNDLE_INFO"
grep -qx 'image=eclipse-mosquitto:2.1.2-alpine' "$BUNDLE/BUNDLE_INFO"
grep -qx 'image=cr.signalk.io/signalk/signalk-server:v2.27.0' "$BUNDLE/BUNDLE_INFO"
grep -q 'sqlite_vec' "$BUNDLE/MANIFEST.sha256"
grep -q 'pip -m pip download .*--only-binary=:all: --python-version 3.11 --implementation cp --platform manylinux2014_aarch64' "$TEST_ROOT/pip.log" || fail "pip download platform args missing"
grep -q -- 'pull --platform linux/arm64 influxdb:2.9.1' "$TEST_ROOT/docker.log"
env PATH="$FAKE_BIN:$PATH" "$RELEASE/scripts/prepare-offline-bundle.sh" --verify "$BUNDLE" >/dev/null
env PATH="$FAKE_BIN:$PATH" "$RELEASE/scripts/prepare-offline-bundle.sh" --python-version 3.13 --platform manylinux_2_39_aarch64 --output "$TEST_ROOT/bundle2" >/dev/null
grep -q -- '--python-version 3.13 --implementation cp --platform manylinux_2_39_aarch64' "$TEST_ROOT/pip.log"
env PATH="$FAKE_BIN:$PATH" "$RELEASE/scripts/prepare-offline-bundle.sh" --native --output "$TEST_ROOT/bundle3" >/dev/null
tail -n 1 "$TEST_ROOT/pip.log" | grep -q -- '--platform' && fail "--native must not pin a platform"
# Tampering, truncation and version mismatch are detected before anything is loaded.
printf 'x' >> "$BUNDLE/wheelhouse/sqlite_vec-0.0.0-py3-none-any.whl"
if env PATH="$FAKE_BIN:$PATH" "$RELEASE/scripts/prepare-offline-bundle.sh" --load "$BUNDLE" >/dev/null 2>&1; then fail "tampered bundle loaded"; fi
: > "$TEST_ROOT/bundle2/wheelhouse/sqlite_vec-0.0.0-py3-none-any.whl"
sed -i 's/^release=.*/release=0.0.1/' "$TEST_ROOT/bundle3/BUNDLE_INFO"
if env PATH="$FAKE_BIN:$PATH" "$RELEASE/scripts/prepare-offline-bundle.sh" --verify "$TEST_ROOT/bundle3" >/dev/null 2>&1; then fail "mismatched bundle verified"; fi
# --load restores the images that offline preflight asks for.
: > "$IMAGES_PRESENT"
env PATH="$FAKE_BIN:$PATH" "$RELEASE/scripts/prepare-offline-bundle.sh" --output "$TEST_ROOT/bundle4" >/dev/null
env PATH="$FAKE_BIN:$PATH" "$RELEASE/scripts/prepare-offline-bundle.sh" --load "$TEST_ROOT/bundle4" >/dev/null
grep -qxF 'influxdb:2.9.1' "$IMAGES_PRESENT"
installer --offline --dry-run >/dev/null 2>"$TEST_ROOT/err"
grep -q 'not present locally' "$TEST_ROOT/err" && fail "images still missing after load" || true

echo "offline install checks passed"
