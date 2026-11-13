#!/bin/bash

set -euo pipefail

# Unattended by default: nothing in this file may block on a prompt. The interactive
# cases below unset it for their own runs and feed answers from a file.
export VESSELSTACK_NONINTERACTIVE=true
REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# The installer edits a config file only inside root-controlled folders, so a root run keeps its
# scratch space under /usr/local/share rather than the sticky, world-writable /tmp.
if [ "$(id -u)" -eq 0 ]; then
    mkdir -p /usr/local/share/vs-test
    TEST_ROOT=$(mktemp -d /usr/local/share/vs-test/vs-ci.XXXXXX)
else
    TEST_ROOT=$(mktemp -d)
fi
trap 'rm -rf -- "$TEST_ROOT"' EXIT
FAKE_BIN="$TEST_ROOT/bin"
APP_ROOT="$TEST_ROOT/opt/vesselstack"
DATA_ROOT="$TEST_ROOT/opt/vesselstack-data"
SYSTEM_ROOT="$TEST_ROOT/system-root"
CONFIG="$TEST_ROOT/vesselstack.env"
install -d "$FAKE_BIN"

cat > "$FAKE_BIN/docker" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$(dirname "$0")/docker.log"
# Knobs for the preflight and failure tests.
if [ "${1:-}" = image ] && [ "${2:-}" = inspect ] && [ -n "${FAKE_DOCKER_NO_IMAGE:-}" ]; then exit 1; fi
if [ "${1:-}" = manifest ] && [ -z "${FAKE_DOCKER_REGISTRY_OK:-}" ]; then exit 1; fi
if [ "${1:-}" = pull ] && [ -n "${FAKE_DOCKER_NO_IMAGE:-}" ] && [ -z "${FAKE_DOCKER_REGISTRY_OK:-}" ]; then exit 1; fi
if [ "${1:-}" = run ] && [ -n "${FAKE_DOCKER_FAIL_RUN:-}" ]; then echo "simulated hashing failure" >&2; exit 1; fi
# A pre-existing broker container: FAKE_MOSQ_MOUNTS lists "destination=source" lines.
if [ "${1:-}" = inspect ] && [[ "$*" == *Mounts* ]]; then
    [ -n "${FAKE_MOSQ_MOUNTS:-}" ] && printf '%b' "$FAKE_MOSQ_MOUNTS"
    exit 0
fi
# Emulate the Mosquitto hashing container used by displayctl sync-mqtt.
if [ "${1:-}" = run ]; then
    for argument in "$@"; do
        case "$argument" in
            *:/auth)
                host_path=${argument%:/auth}
                # Emulate the hashing container: promote passwords.new as 1883:1883 0600.
                [ -f "$host_path/passwords.new" ] && mv -f "$host_path/passwords.new" "$host_path/passwords"
                chmod 0600 "$host_path/passwords"
                if [ "$(id -u)" -eq 0 ]; then chown 1883:1883 "$host_path/passwords"; fi
                ;;
        esac
    done
fi
exit 0
EOF
cat > "$FAKE_BIN/systemctl" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$(dirname "$0")/systemctl.log"
# Another watchdog daemon is only running when a test says so.
if { [ "${1:-}" = is-active ] || [ "${1:-}" = is-enabled ]; } && [[ " $* " == *" watchdog.service "* ]]; then
    case "${1:-}" in
        is-active) [ -n "${FAKE_WATCHDOG_SERVICE_ACTIVE:-}" ] || exit 3 ;;
        is-enabled) [ -n "${FAKE_WATCHDOG_SERVICE_ENABLED:-}" ] || exit 3 ;;
    esac
fi
exit 0
EOF
cat > "$FAKE_BIN/curl" <<'EOF'
#!/bin/bash
exit 0
EOF
cat > "$FAKE_BIN/python3" <<'EOF'
#!/bin/bash
if [ "${1:-}" = -m ] && [ "${2:-}" = venv ]; then
    install -d "$3/bin"
    cat > "$3/bin/pip" <<'PIP'
#!/bin/bash
printf '%s\n' "$*" >> "$(dirname "$0")/../pip.log"
# FAKE_PIP_FAIL=displays fails only the displays bridge requirements install.
if [ -n "${FAKE_PIP_FAIL:-}" ] && { [ "$FAKE_PIP_FAIL" != displays ] || [[ "$*" == *displays* ]]; }; then
    echo "simulated: no route to pypi.org" >&2; exit 1
fi
exit 0
PIP
    chmod +x "$3/bin/pip"
    # A usable interpreter in the venv (the memory index runs with it); omitted on
    # request to exercise the failure path.
    [ -n "${FAKE_VENV_NO_PYTHON:-}" ] || ln -s /usr/bin/python3 "$3/bin/python"
    exit 0
fi
exec /usr/bin/python3 "$@"
EOF
# Records how the installer drops privileges, then really does it.
cat > "$FAKE_BIN/runuser" <<EOF
#!/bin/bash
printf '%s\\n' "\$*" >> "$TEST_ROOT/runuser.log"
# Drop this stub's directory wherever it sits in PATH, or exec would find itself again.
PATH=\$(printf '%s' "\$PATH" | tr ':' '\\n' | grep -vxF "$FAKE_BIN" | paste -sd:)
exec runuser "\$@"
EOF
chmod +x "$FAKE_BIN/docker" "$FAKE_BIN/systemctl" "$FAKE_BIN/python3" "$FAKE_BIN/curl" "$FAKE_BIN/runuser"

cat > "$CONFIG" <<EOF
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
SIGNALK_VESSELTWIN_PRELOAD="false"
SIGNALK_URL="http://127.0.0.1:3000"
SOCKETCAN_ENABLE="false"
SOCKETCAN_INTERFACE="can0"
SOCKETCAN_BITRATE="250000"
AIS_ENABLE="false"
AIS_IMAGE="ghcr.io/jvde-github/ais-catcher:v0.70"
AIS_DEVICE="/dev/bus/usb"
AIS_CATCHER_ARGS="-q -N 8100 -S 5011"
HOME_ASSISTANT_URL="http://127.0.0.1:8123"
HOME_ASSISTANT_TOKEN=""
INFLUXDB_URL="http://127.0.0.1:8086"
INFLUXDB_ORG="vesselstack"
INFLUXDB_USERNAME="boatadmin"
INFLUXDB_RAW_BUCKET="signalk"
INFLUXDB_HISTORY_BUCKET="signalk_1m"
INFLUXDB_HOME_ASSISTANT_BUCKET="homeassistant"
INFLUXDB_AIS_BUCKET="ais"
INFLUXDB_PASSWORD="GENERATE"
INFLUXDB_TOKEN="GENERATE"
GRAFANA_ADMIN_PASSWORD="GENERATE"
MQTT_USERNAME="homeassistant"
MQTT_PASSWORD="GENERATE"
BOAT_CHAT_PROVIDER="local"
BOAT_CHAT_HOST="0.0.0.0"
BOAT_CHAT_PORT="8765"
BOAT_CHAT_SETTINGS_TOKEN="GENERATE"
BOAT_CHAT_ACCESS_TOKEN="GENERATE"
VESSELSTACK_UNTRUSTED_INTERFACE="wlan0"
VESSELSTACK_FIREWALL_ENABLE="false"
TELEGRAM_ENABLE="false"
TELEMETRY_INDEXER_ENABLE="true"
EOF

env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$CONFIG" --dry-run > "$TEST_ROOT/dry-run.out"
grep -q 'Preflight passed' "$TEST_ROOT/dry-run.out"  # not a pipe: grep -q would SIGPIPE the installer under pipefail
COLLISION_CONFIG="$TEST_ROOT/port-collision.env"
cp "$CONFIG" "$COLLISION_CONFIG"
printf '\nGRAFANA_PORT="8086"\nINFLUXDB_PORT="8086"\n' >> "$COLLISION_CONFIG"
if env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$COLLISION_CONFIG" --dry-run >/dev/null 2>&1; then
    echo "Preflight accepted conflicting published ports" >&2
    exit 1
fi
NESTED_BACKUP_CONFIG="$TEST_ROOT/nested-backup.env"
cp "$CONFIG" "$NESTED_BACKUP_CONFIG"
printf '\nVESSELSTACK_BACKUP="%s/backups"\n' "$DATA_ROOT" >> "$NESTED_BACKUP_CONFIG"
if env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$NESTED_BACKUP_CONFIG" --dry-run >/dev/null 2>&1; then
    echo "Preflight accepted a backup directory inside the data tree" >&2
    exit 1
fi
# An older `vesselstackctl update` is a bash script that bash reads from disk while it runs this
# installer as a child. The installer must replace the file by rename (new inode), never by
# rewriting it in place, or the old process continues in the new content at its old offset.
SBIN="$SYSTEM_ROOT/usr/local/sbin"
install -d -m 0755 "$SBIN"
printf '#!/bin/bash\n# old vesselstackctl: must stay readable through its open descriptor\necho old\n' > "$SBIN/vesselstackctl"
chmod 0755 "$SBIN/vesselstackctl"
cp "$SBIN/vesselstackctl" "$TEST_ROOT/old-vesselstackctl.expected"
: > "$SBIN/.vesselstackctl.new.99999"   # leftover of a killed run: removed
: > "$SBIN/.unrelated.new.99999"        # anything else: untouched
old_inode=$(stat -c %i "$SBIN/vesselstackctl")
exec 9< "$SBIN/vesselstackctl"
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$CONFIG" >/dev/null
cat <&9 > "$TEST_ROOT/old-vesselstackctl.read"
exec 9<&-
cmp "$TEST_ROOT/old-vesselstackctl.expected" "$TEST_ROOT/old-vesselstackctl.read"
test "$(stat -c %i "$SBIN/vesselstackctl")" != "$old_inode"
test "$(stat -c %a "$SBIN/vesselstackctl")" = 755
grep -qxF "ROOT=\${VESSELSTACK_ROOT:-$APP_ROOT}" "$SBIN/vesselstackctl"
if compgen -G "$SBIN/.vesselstackctl.new.*" >/dev/null; then
    echo "The installer left a temporary vesselstackctl behind" >&2
    exit 1
fi
test -e "$SBIN/.unrelated.new.99999"
rm -f -- "$SBIN/.unrelated.new.99999"

test -x "$SYSTEM_ROOT/usr/local/sbin/vesselstackctl"
# The installed command defaults to this installation's root (no exported VESSELSTACK_ROOT needed).
grep -qxF "ROOT=\${VESSELSTACK_ROOT:-$APP_ROOT}" "$SYSTEM_ROOT/usr/local/sbin/vesselstackctl"
# The update launcher and the helper root executes are installed root-owned (0755)
# in a root-owned directory chain, never run from a source checkout.
test "$(stat -c '%u:%a' "$SYSTEM_ROOT/usr/local/sbin/vesselstack-update")" = "0:755"
test "$(stat -c '%u:%a' "$SYSTEM_ROOT/usr/local/libexec/vesselstack/deploy-chat.py")" = "0:755"
test "$(stat -c '%u:%a' "$SYSTEM_ROOT/usr/local/libexec/vesselstack")" = "0:755"
cmp "$REPO_ROOT/scripts/deploy-chat.py" "$SYSTEM_ROOT/usr/local/libexec/vesselstack/deploy-chat.py"
test -s "$SYSTEM_ROOT/etc/systemd/system/vesselstack-chat.service"
test -s "$SYSTEM_ROOT/etc/systemd/system/vesselstack-control-panel.service"
test -s "$SYSTEM_ROOT/etc/systemd/system/vesselstack-chat-telegram.service"
test -s "$SYSTEM_ROOT/etc/systemd/system/vesselstack-telemetry-indexer.service"
test -s "$SYSTEM_ROOT/etc/systemd/system/vesselstack-telemetry-indexer.timer"
# The displays bridge unit is always rendered but stays disabled by default.
unit="$SYSTEM_ROOT/etc/systemd/system/vesselstack-displays-bridge.service"
test -s "$unit"
grep -q "^User=$(id -un)\$" "$unit"
grep -q "^EnvironmentFile=$APP_ROOT/config/displays-bridge.env\$" "$unit"
grep -q "^ExecStart=$APP_ROOT/displays/.venv/bin/python $APP_ROOT/displays/bridge.py\$" "$unit"
if grep -q '@' "$unit"; then echo "Unit contains an unrendered placeholder" >&2; exit 1; fi
grep -qx 'disable --now vesselstack-displays-bridge.service' "$FAKE_BIN/systemctl.log"
if grep -qx 'enable vesselstack-displays-bridge.service' "$FAKE_BIN/systemctl.log"; then
    echo "Displays bridge was enabled with DISPLAYS_ENABLE=false" >&2
    exit 1
fi
# Unattended operation defaults: no watchdog device in the test root, so no watchdog drop-in; the
# journal is capped; scheduled backups are off (service and helper exist, the timer does not).
WATCHDOG_DROPIN="$SYSTEM_ROOT/etc/systemd/system.conf.d/60-vesselstack-watchdog.conf"
JOURNAL_DROPIN="$SYSTEM_ROOT/etc/systemd/journald.conf.d/60-vesselstack.conf"
test ! -e "$WATCHDOG_DROPIN"
grep -qx 'SystemMaxUse=200M' "$JOURNAL_DROPIN"
grep -qx 'SystemKeepFree=1G' "$JOURNAL_DROPIN"
grep -qx 'MaxFileSec=1week' "$JOURNAL_DROPIN"
if grep -q '^Storage=' "$JOURNAL_DROPIN"; then echo "The journald drop-in must not set Storage=" >&2; exit 1; fi
grep -qx 'restart systemd-journald' "$FAKE_BIN/systemctl.log"
test ! -e "$SYSTEM_ROOT/etc/systemd/system/vesselstack-backup.timer"
test -s "$SYSTEM_ROOT/etc/systemd/system/vesselstack-backup.service"
grep -qx 'ExecStart=/usr/local/sbin/vesselstackctl backup --scheduled' "$SYSTEM_ROOT/etc/systemd/system/vesselstack-backup.service"
grep -qx 'Environment=VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=false' "$SYSTEM_ROOT/etc/systemd/system/vesselstack-backup.service"
grep -qx 'ExecStopPost=/usr/local/libexec/vesselstack/record-backup-result' "$SYSTEM_ROOT/etc/systemd/system/vesselstack-backup.service"
test "$(stat -c '%u:%a' "$SYSTEM_ROOT/usr/local/libexec/vesselstack/record-backup-result")" = "0:755"
grep -qx 'disable vesselstack-backup.timer' "$FAKE_BIN/systemctl.log"
if grep -qx 'enable vesselstack-backup.timer' "$FAKE_BIN/systemctl.log"; then
    echo "The backup timer was enabled with VESSELSTACK_BACKUP_SCHEDULE=off" >&2
    exit 1
fi
# Weekly schedule + a watchdog device on a Raspberry Pi: both drop-ins, the timer, and daemon-reexec.
UNATTENDED_CONFIG="$TEST_ROOT/unattended.env"
cp "$CONFIG" "$UNATTENDED_CONFIG"
printf '\nVESSELSTACK_BACKUP_SCHEDULE="weekly"\nVESSELSTACK_JOURNAL_MAX_USE="64M"\n' >> "$UNATTENDED_CONFIG"
install -d "$SYSTEM_ROOT/sys/class/watchdog/watchdog0" "$SYSTEM_ROOT/dev" "$SYSTEM_ROOT/proc/device-tree"
printf '15\n' > "$SYSTEM_ROOT/sys/class/watchdog/watchdog0/max_timeout"
: > "$SYSTEM_ROOT/dev/watchdog"
run_unattended() {  # [env assignments...]
    env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" "$@" "$REPO_ROOT/install.sh" --config "$UNATTENDED_CONFIG"
}
# auto without a Raspberry Pi model: a watchdog device alone does not arm anything.
printf 'Generic x86 board\0' > "$SYSTEM_ROOT/proc/device-tree/model"
run_unattended >/dev/null
test ! -e "$WATCHDOG_DROPIN"
# true arms on any host that has the device.
printf '\nVESSELSTACK_WATCHDOG="true"\n' >> "$UNATTENDED_CONFIG"
run_unattended >/dev/null
grep -qx 'RuntimeWatchdogSec=10s' "$WATCHDOG_DROPIN"
sed -i '/^VESSELSTACK_WATCHDOG=/d' "$UNATTENDED_CONFIG"
rm -f -- "$WATCHDOG_DROPIN"
printf 'Raspberry Pi 4 Model B Rev 1.5\0' > "$SYSTEM_ROOT/proc/device-tree/model"
: > "$FAKE_BIN/systemctl.log"
run_unattended >/dev/null
grep -qx '\[Manager\]' "$WATCHDOG_DROPIN"
grep -qx 'RuntimeWatchdogSec=10s' "$WATCHDOG_DROPIN"
grep -qx 'RebootWatchdogSec=2min' "$WATCHDOG_DROPIN"
grep -qx 'daemon-reexec' "$FAKE_BIN/systemctl.log"
grep -qx 'SystemMaxUse=64M' "$JOURNAL_DROPIN"
grep -qx 'OnCalendar=Sun \*-\*-\* 03:30' "$SYSTEM_ROOT/etc/systemd/system/vesselstack-backup.timer"
grep -qx 'RandomizedDelaySec=15min' "$SYSTEM_ROOT/etc/systemd/system/vesselstack-backup.timer"
grep -qx 'Persistent=false' "$SYSTEM_ROOT/etc/systemd/system/vesselstack-backup.timer"
grep -qx 'enable vesselstack-backup.timer' "$FAKE_BIN/systemctl.log"
grep -qx 'TimeoutStopSec=15min' "$SYSTEM_ROOT/etc/systemd/system/vesselstack-backup.service"
grep -qx "Documentation=file://$APP_ROOT/README.md" "$SYSTEM_ROOT/etc/systemd/system/vesselstack-backup.service"
# A second identical run changes nothing, so it neither re-execs systemd nor restarts journald.
: > "$FAKE_BIN/systemctl.log"
run_unattended >/dev/null
if grep -qx 'daemon-reexec' "$FAKE_BIN/systemctl.log" || grep -qx 'restart systemd-journald' "$FAKE_BIN/systemctl.log"; then
    echo "An unchanged rerun re-executed systemd or restarted journald" >&2
    exit 1
fi
# N = min(10, max_timeout - 1): 8 gives 7, 11 gives 10.
printf '8\n' > "$SYSTEM_ROOT/sys/class/watchdog/watchdog0/max_timeout"
run_unattended >/dev/null
grep -qx 'RuntimeWatchdogSec=7s' "$WATCHDOG_DROPIN"
printf '11\n' > "$SYSTEM_ROOT/sys/class/watchdog/watchdog0/max_timeout"
run_unattended >/dev/null
grep -qx 'RuntimeWatchdogSec=10s' "$WATCHDOG_DROPIN"
# A maximum that leaves fewer than 5 seconds skips the watchdog with a warning and removes the drop-in.
printf '5\n' > "$SYSTEM_ROOT/sys/class/watchdog/watchdog0/max_timeout"
: > "$FAKE_BIN/systemctl.log"
warn_output=$(run_unattended 2>&1 >/dev/null)
grep -q 'too short to use safely' <<<"$warn_output"
test ! -e "$WATCHDOG_DROPIN"
grep -qx 'daemon-reexec' "$FAKE_BIN/systemctl.log"
# An unreadable maximum assumes 10 seconds and warns.
rm -f -- "$SYSTEM_ROOT/sys/class/watchdog/watchdog0/max_timeout"
warn_output=$(run_unattended 2>&1 >/dev/null)
grep -q 'could not read the hardware watchdog' <<<"$warn_output"
grep -qx 'RuntimeWatchdogSec=10s' "$WATCHDOG_DROPIN"
# watchdog.service active, or enabled: our drop-in is removed (systemd re-executed) and the installer warns.
for owner_env in FAKE_WATCHDOG_SERVICE_ACTIVE=1 FAKE_WATCHDOG_SERVICE_ENABLED=1; do
    run_unattended >/dev/null
    test -e "$WATCHDOG_DROPIN"
    : > "$FAKE_BIN/systemctl.log"
    warn_output=$(run_unattended "$owner_env" 2>&1 >/dev/null)
    grep -q 'watchdog.service is installed and active or enabled' <<<"$warn_output"
    test ! -e "$WATCHDOG_DROPIN"
    grep -qx 'daemon-reexec' "$FAKE_BIN/systemctl.log"
done
# Device gone: the drop-in is removed and systemd re-executed. VESSELSTACK_WATCHDOG=false also removes it.
run_unattended >/dev/null
test -e "$WATCHDOG_DROPIN"
rm -f -- "$SYSTEM_ROOT/dev/watchdog"
: > "$FAKE_BIN/systemctl.log"
run_unattended >/dev/null
test ! -e "$WATCHDOG_DROPIN"
grep -qx 'daemon-reexec' "$FAKE_BIN/systemctl.log"
: > "$SYSTEM_ROOT/dev/watchdog"
run_unattended >/dev/null
test -e "$WATCHDOG_DROPIN"
printf 'VESSELSTACK_WATCHDOG="false"\n' >> "$UNATTENDED_CONFIG"
run_unattended >/dev/null
test ! -e "$WATCHDOG_DROPIN"
# VESSELSTACK_JOURNAL_MAX_USE=off removes VesselStack's journald drop-in and restarts journald once.
test -e "$JOURNAL_DROPIN"
printf 'VESSELSTACK_JOURNAL_MAX_USE="off"\n' >> "$UNATTENDED_CONFIG"
: > "$FAKE_BIN/systemctl.log"
run_unattended >/dev/null
test ! -e "$JOURNAL_DROPIN"
grep -qx 'restart systemd-journald' "$FAKE_BIN/systemctl.log"
# A drop-in that cannot be written is an error, not "unchanged".
printf 'VESSELSTACK_JOURNAL_MAX_USE="64M"\n' >> "$UNATTENDED_CONFIG"
rm -rf -- "$SYSTEM_ROOT/etc/systemd/journald.conf.d"
: > "$SYSTEM_ROOT/etc/systemd/journald.conf.d"   # a file where the directory belongs: nothing can be written
rc=0
run_unattended >/dev/null 2>&1 || rc=$?
test "$rc" -ne 0
rm -f -- "$SYSTEM_ROOT/etc/systemd/journald.conf.d"
run_unattended >/dev/null
test -e "$JOURNAL_DROPIN"
# Back to schedule off: the timer is removed again.
printf 'VESSELSTACK_BACKUP_SCHEDULE="off"\n' >> "$UNATTENDED_CONFIG"
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$UNATTENDED_CONFIG" >/dev/null
test ! -e "$SYSTEM_ROOT/etc/systemd/system/vesselstack-backup.timer"
rm -f -- "$SYSTEM_ROOT/dev/watchdog"
# Restore the default installed state for the checks below.
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$CONFIG" >/dev/null
test ! -e "$APP_ROOT/config/displays-bridge.env"
test ! -e "$APP_ROOT/config/displays.json"
test ! -e "$APP_ROOT/displays/.venv"
test -s "$APP_ROOT/control-panel/app.py"
test -s "$APP_ROOT/common/vesselstack_auth.py"
test -s "$APP_ROOT/common/qrcodegen.py"
test -s "$APP_ROOT/installer/common/vesselstack_authctl.py"
test "$(stat -c '%a' "$APP_ROOT/common/vesselstack_auth.py")" = 644
test -s "$APP_ROOT/installer/install.sh"
test -s "$APP_ROOT/displays/displayctl.py"
test -s "$APP_ROOT/displays/mqtt_auth.py"
test -s "$APP_ROOT/installer/displays/mqtt_auth.py"
test ! -e "$APP_ROOT/displays/__pycache__"
grep -q '^acl_file /mosquitto/auth/acl$' "$APP_ROOT/config/mosquitto/mosquitto.conf"
grep -q '^password_file /mosquitto/auth/passwords$' "$APP_ROOT/config/mosquitto/mosquitto.conf"
grep -q 'config/mosquitto-auth:/mosquitto/auth:ro' "$APP_ROOT/compose.yml"
grep -q 'config/mosquitto:/mosquitto/config:ro' "$APP_ROOT/compose.yml"
# The new configuration lives in a root-owned directory mount; the legacy
# single-file path is never written (old containers keep using it).
test "$(stat -c '%a:%u:%g' "$APP_ROOT/config/mosquitto")" = 755:0:0
test "$(stat -c '%a:%u:%g' "$APP_ROOT/config/mosquitto/mosquitto.conf")" = 644:0:0
test ! -e "$APP_ROOT/config/mosquitto.conf"
grep -q '^DISPLAYS_ENABLE=false$' "$APP_ROOT/config/vesselstack.env"
grep -q '^DISPLAYS_BRIDGE_MQTT_USERNAME=vesselstack-bridge$' "$APP_ROOT/config/vesselstack.env"
test -s "$APP_ROOT/config/grafana/dashboards/system-health.json"
test -s "$DATA_ROOT/homeassistant/blueprints/automation/vesselstack/low-battery.yaml"
test -s "$DATA_ROOT/homeassistant/vesselstack-dashboard.example.yaml"
# The data root is root-owned (service group, 0750); only leaf mount roots belong to
# the accounts that write them.
test "$(stat -c '%u:%a' "$DATA_ROOT")" = "0:750"
test "$(stat -c '%u:%g' "$DATA_ROOT/influxdb")" = "1000:1000"
test "$(stat -c '%u:%g' "$DATA_ROOT/grafana")" = "472:0"
test "$(stat -c '%u:%g:%a' "$DATA_ROOT/prometheus")" = "65534:65534:750"
grep -q '^PROMETHEUS_RETENTION=30d$' "$APP_ROOT/config/vesselstack.env" || grep -q '^PROMETHEUS_RETENTION="30d"$' "$APP_ROOT/config/vesselstack.env"
# Code, venv and generated facts are not writable by the service user; the
# service user may write only under the state directory in the data tree.
test "$(stat -c '%u' "$APP_ROOT/boat-chat")" = 0
test "$(stat -c '%u' "$APP_ROOT/boat-chat/.venv")" = 0
test "$(stat -c '%u:%a' "$APP_ROOT/boat-chat/boat_facts.json")" = "0:640"
test -z "$(find "$APP_ROOT/boat-chat" -not -type l \( -perm -g=w -o -perm -o=w \) -print -quit)"
test -d "$DATA_ROOT/boat-chat"
grep -qx 'ProtectSystem=strict' "$SYSTEM_ROOT/etc/systemd/system/vesselstack-chat.service"
grep -qx "ReadWritePaths=$DATA_ROOT/boat-chat" "$SYSTEM_ROOT/etc/systemd/system/vesselstack-chat.service"
grep -qx "Environment=BOAT_CHAT_STATE_DIR=$DATA_ROOT/boat-chat" "$SYSTEM_ROOT/etc/systemd/system/vesselstack-telemetry-indexer.service"
grep -q 'Test Vessel Boat Chat' "$APP_ROOT/boat-chat/static/index.html"
grep -q '^INFLUXDB_HISTORY_BUCKET=signalk_1m$' "$APP_ROOT/config/vesselstack.env"
grep -q '^TELEGRAM_ENABLE=false$' "$APP_ROOT/config/vesselstack.env"
grep -q '^TELEMETRY_INDEXER_ENABLE=true$' "$APP_ROOT/config/boat-chat.env"
if grep -R 'GENERATE' "$APP_ROOT/config"; then
    echo "Rendered configuration contains an ungenerated secret" >&2
    exit 1
fi
test "$(stat -c '%a' "$APP_ROOT/config/vesselstack.env")" = 600
test "$(stat -c '%a' "$APP_ROOT/config/control-panel.env")" = 600
grep -Eq '^CONTROL_PANEL_TOKEN=[a-f0-9]{64}$' "$APP_ROOT/config/control-panel.env"
grep -Eq '^BOAT_CHAT_ACCESS_TOKEN=[a-f0-9]{64}$' "$APP_ROOT/config/boat-chat.env"
grep -q '^VESSELSTACK_DATA_BIND_ADDRESS=127.0.0.1$' "$APP_ROOT/config/vesselstack.env"
grep -q '^HOMEASSISTANT_BIND_ADDRESS=0.0.0.0$' "$APP_ROOT/config/vesselstack.env"
test ! -e "$DATA_ROOT/homeassistant/configuration.yaml"
grep -qx "Environment=BOAT_CHAT_ENV=$DATA_ROOT/boat-chat/boat-chat.env" "$SYSTEM_ROOT/etc/systemd/system/vesselstack-chat.service"
grep -qx "$(<"$REPO_ROOT/VERSION")" "$APP_ROOT/config/installed-version"

access_token_before=$(sed -n 's/^BOAT_CHAT_ACCESS_TOKEN=//p' "$APP_ROOT/config/boat-chat.env")
panel_token_before=$(sed -n 's/^CONTROL_PANEL_TOKEN=//p' "$APP_ROOT/config/control-panel.env")
sed -i '/^BOAT_CHAT_PROVIDER=/d' "$APP_ROOT/config/boat-chat.env"
printf '%s\n' 'BOAT_CHAT_PROVIDER=ollama' 'OPENAI_API_KEY=preserve-test-secret' \
    >> "$APP_ROOT/config/boat-chat.env"
# A loopback BOAT_CHAT_HOST (`vesselstackctl lan local`) survives a re-render from the
# installed configuration, which never carries that key.
sed -i 's/^BOAT_CHAT_HOST=.*/BOAT_CHAT_HOST=127.0.0.1/' "$APP_ROOT/config/boat-chat.env"
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$APP_ROOT/installer/install.sh" \
    --config "$APP_ROOT/config/vesselstack.env" >/dev/null
grep -qx 'BOAT_CHAT_PROVIDER=ollama' "$APP_ROOT/config/boat-chat.env"
grep -qx 'BOAT_CHAT_HOST=127.0.0.1' "$APP_ROOT/config/boat-chat.env"
grep -qx 'OPENAI_API_KEY=preserve-test-secret' "$APP_ROOT/config/boat-chat.env"
test "$(sed -n 's/^CONTROL_PANEL_TOKEN=//p' "$APP_ROOT/config/control-panel.env")" = \
    "$panel_token_before"
test "$(sed -n 's/^BOAT_CHAT_ACCESS_TOKEN=//p' "$APP_ROOT/config/boat-chat.env")" = \
    "$access_token_before"

# Every generated secret survives a re-render from the ORIGINAL example-style
# config (which still says GENERATE): InfluxDB/Grafana/MQTT ignore changed
# credentials after first start, so regenerating would desynchronise them.
secret_keys=(INFLUXDB_PASSWORD INFLUXDB_TOKEN GRAFANA_ADMIN_PASSWORD MQTT_PASSWORD)
declare -A pw_before=()
for key in "${secret_keys[@]}"; do
    pw_before[$key]=$(sed -n "s/^${key}=//p" "$APP_ROOT/config/vesselstack.env")
    [[ "${pw_before[$key]}" =~ ^[a-f0-9]{64}$ ]] || { echo "$key was not generated" >&2; exit 1; }
done
influx_token_chat_before=$(sed -n 's/^INFLUXDB_TOKEN=//p' "$APP_ROOT/config/boat-chat.env")
settings_token_before=$(sed -n 's/^BOAT_CHAT_SETTINGS_TOKEN=//p' "$APP_ROOT/config/boat-chat.env")
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$CONFIG" >/dev/null
for key in "${secret_keys[@]}"; do
    test "$(sed -n "s/^${key}=//p" "$APP_ROOT/config/vesselstack.env")" = "${pw_before[$key]}"
done
# A configuration that really sets BOAT_CHAT_HOST (here the wizard-style original) does apply it.
grep -qx 'BOAT_CHAT_HOST=0.0.0.0' "$APP_ROOT/config/boat-chat.env"
test "$(sed -n 's/^INFLUXDB_TOKEN=//p' "$APP_ROOT/config/boat-chat.env")" = "$influx_token_chat_before"
test "$(sed -n 's/^BOAT_CHAT_SETTINGS_TOKEN=//p' "$APP_ROOT/config/boat-chat.env")" = "$settings_token_before"
test "$(sed -n 's/^BOAT_CHAT_ACCESS_TOKEN=//p' "$APP_ROOT/config/boat-chat.env")" = "$access_token_before"
test "$(sed -n 's/^CONTROL_PANEL_TOKEN=//p' "$APP_ROOT/config/control-panel.env")" = "$panel_token_before"
if grep -R 'GENERATE' "$APP_ROOT/config"; then
    echo "Re-render produced an ungenerated secret" >&2
    exit 1
fi

# Boat Chat's memory index: built by the installer (nothing else builds it), as the
# service user with a minimal environment, idempotent, and a failure is loud but never
# aborts the installation.
memory_db="$DATA_ROOT/boat-chat/memory/boat_memory.sqlite"
index_chunks() {
    /usr/bin/python3 -c 'import sqlite3, sys; print(sqlite3.connect(sys.argv[1]).execute("select count(*) from chunks").fetchone()[0])' "$1"
}
test -s "$memory_db"
[ "$(index_chunks "$memory_db")" -gt 0 ] || { echo "Fresh install did not build the memory index" >&2; exit 1; }
grep -q -- "^-u $(id -un) -- env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/nonexistent PYTHONDONTWRITEBYTECODE=1 BOAT_CHAT_STATE_DIR=$DATA_ROOT/boat-chat BOAT_CHAT_ENV=$DATA_ROOT/boat-chat/boat-chat.env $APP_ROOT/boat-chat/.venv/bin/python $APP_ROOT/boat-chat/memory_index.py ensure\$" "$TEST_ROOT/runuser.log"
# Idempotent: nothing changed, nothing rebuilt (and nothing printed as a warning).
rerun_output=$(env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$CONFIG" 2>&1)
grep -q 'memory index: unchanged' <<<"$rerun_output"
if grep -q 'WARN: the Boat Chat memory index' <<<"$rerun_output"; then
    echo "Idempotent re-run warned about the memory index" >&2
    exit 1
fi
# An existing install without an index (the upgrade case) gets one.
rm -f -- "$memory_db" "$memory_db-wal" "$memory_db-shm"
rerun_output=$(env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$CONFIG" 2>&1)
grep -q 'memory index: rebuilt' <<<"$rerun_output"
[ "$(index_chunks "$memory_db")" -gt 0 ]
# Failure path: no interpreter in the venv. The install still succeeds, prints the
# WARN with the exact manual command, and leaves the index absent for doctor to report.
rm -f -- "$memory_db" "$memory_db-wal" "$memory_db-shm"
env PATH="$FAKE_BIN:$PATH" FAKE_VENV_NO_PYTHON=1 VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$CONFIG" >/dev/null 2>"$TEST_ROOT/index-fail.stderr"
grep -q 'WARN: the Boat Chat memory index could not be built' "$TEST_ROOT/index-fail.stderr"
grep -q 'WARN: build it manually with: sudo vesselstackctl reindex' "$TEST_ROOT/index-fail.stderr"
test ! -e "$memory_db"
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$CONFIG" >/dev/null
[ "$(index_chunks "$memory_db")" -gt 0 ]

# Retention: empty by default (no implicit change to existing buckets), validated.
grep -q "^INFLUXDB_RAW_RETENTION=''\?$" "$APP_ROOT/config/vesselstack.env"
BAD_RETENTION_CONFIG="$TEST_ROOT/bad-retention.env"
cp "$CONFIG" "$BAD_RETENTION_CONFIG"
printf '\nINFLUXDB_RAW_RETENTION="1y"\n' >> "$BAD_RETENTION_CONFIG"
if env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$BAD_RETENTION_CONFIG" --dry-run >/dev/null 2>&1; then
    echo "Preflight accepted an invalid INFLUXDB_RAW_RETENTION" >&2
    exit 1
fi

# Home Assistant bind address: validated, and a loopback value makes the installer manage one
# delimited http block (created with the stock configuration on a fresh data directory);
# the default value removes it again without touching anything else in the file.
BAD_HA_CONFIG="$TEST_ROOT/bad-ha.env"
cp "$CONFIG" "$BAD_HA_CONFIG"
printf '\nHOMEASSISTANT_BIND_ADDRESS="lan"\n' >> "$BAD_HA_CONFIG"
if env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$BAD_HA_CONFIG" --dry-run >/dev/null 2>&1; then
    echo "Preflight accepted an invalid HOMEASSISTANT_BIND_ADDRESS" >&2
    exit 1
fi
HA_LOCAL_CONFIG="$TEST_ROOT/ha-local.env"
cp "$CONFIG" "$HA_LOCAL_CONFIG"
printf '\nHOMEASSISTANT_BIND_ADDRESS="127.0.0.1"\n' >> "$HA_LOCAL_CONFIG"
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$HA_LOCAL_CONFIG" >/dev/null
grep -q '^HOMEASSISTANT_BIND_ADDRESS=127.0.0.1$' "$APP_ROOT/config/vesselstack.env"
HA_YAML="$DATA_ROOT/homeassistant/configuration.yaml"
grep -qx '  server_host: 127.0.0.1' "$HA_YAML"
test "$(grep -c '^http:' "$HA_YAML")" -eq 1
printf 'logger:\n  default: warning\n' >> "$HA_YAML"
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$HA_LOCAL_CONFIG" >/dev/null
test "$(grep -c '^http:' "$HA_YAML")" -eq 1
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$CONFIG" >/dev/null
grep -q '^HOMEASSISTANT_BIND_ADDRESS=0.0.0.0$' "$APP_ROOT/config/vesselstack.env"
if grep -q 'VESSELSTACK\|server_host' "$HA_YAML"; then echo "unexpected content in $HA_YAML" >&2; exit 1; fi
grep -qx 'logger:' "$HA_YAML"
grep -qx 'default_config:' "$HA_YAML"

# Legacy runtime state that cannot be migrated (new path already exists) is moved
# into a root-only quarantine outside the code tree, never left in place and
# never deleted.
install -d "$DATA_ROOT/boat-chat/memory" "$DATA_ROOT/boat-chat/data" \
    "$APP_ROOT/boat-chat/memory" "$APP_ROOT/boat-chat/data"
printf 'current\n' > "$DATA_ROOT/boat-chat/memory/current.txt"
printf 'current\n' > "$DATA_ROOT/boat-chat/data/current.txt"
printf 'OPENAI_API_KEY=current\n' > "$DATA_ROOT/boat-chat/boat-chat.env"
printf 'legacy-memory\n' > "$APP_ROOT/boat-chat/memory/legacy.txt"
printf 'legacy-data\n' > "$APP_ROOT/boat-chat/data/legacy.txt"
printf 'OPENAI_API_KEY=legacy-secret\n' > "$APP_ROOT/boat-chat/boat-chat.env"
chmod 0666 "$APP_ROOT/boat-chat/boat-chat.env"
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$CONFIG" >/dev/null 2>"$TEST_ROOT/quarantine.stderr"
grep -q 'moved legacy' "$TEST_ROOT/quarantine.stderr"
test ! -e "$APP_ROOT/boat-chat/memory"
test ! -e "$APP_ROOT/boat-chat/data"
test ! -e "$APP_ROOT/boat-chat/boat-chat.env"
QUARANTINE_ROOT="$SYSTEM_ROOT/var/lib/vesselstack/quarantine"
quarantine=$(find "$QUARANTINE_ROOT" -maxdepth 1 -type d -name 'boat-chat-legacy-*' | head -n 1)
test -n "$quarantine"
test "$(stat -c '%u:%a' "$QUARANTINE_ROOT")" = "0:700"
test "$(stat -c '%u:%a' "$quarantine")" = "0:700"
# Never inside the service-writable data tree.
test -z "$(find "$DATA_ROOT" -maxdepth 1 -name 'boat-chat-legacy-*' -print -quit)"
grep -qx 'legacy-memory' "$quarantine/memory/legacy.txt"
grep -qx 'legacy-data' "$quarantine/data/legacy.txt"
grep -qx 'OPENAI_API_KEY=legacy-secret' "$quarantine/boat-chat.env"
grep -qx 'current' "$DATA_ROOT/boat-chat/memory/current.txt"
test -z "$(find "$APP_ROOT/boat-chat" -not -type l \( -perm -g=w -o -perm -o=w \) -print -quit)"

# Non-root service user: ownership model, symlink refusal and quarantine relocation.
# Needs a real second account, so it only runs as root where useradd exists.
if [ "$(id -u)" -eq 0 ] && command -v useradd >/dev/null; then
    svc="vsstest$$"
    useradd --system --no-create-home --user-group --shell /usr/sbin/nologin "$svc"
    trap 'rm -rf -- "$TEST_ROOT"; userdel "$svc" 2>/dev/null || true' EXIT
    chmod 0755 "$TEST_ROOT"
    svc_gid=$(id -g "$svc")
    APP2="$TEST_ROOT/opt2/vesselstack"
    DATA2="$TEST_ROOT/opt2/vesselstack-data"
    CONFIG2="$TEST_ROOT/service-user.env"
    VICTIM="$TEST_ROOT/victim"
    install -d -m 0755 "$TEST_ROOT/opt2"
    sed -e "s|^VESSELSTACK_USER=.*|VESSELSTACK_USER=\"$svc\"|" \
        -e "s|^VESSELSTACK_ROOT=.*|VESSELSTACK_ROOT=\"$APP2\"|" \
        -e "s|^VESSELSTACK_DATA=.*|VESSELSTACK_DATA=\"$DATA2\"|" "$CONFIG" > "$CONFIG2"
    # shellcheck disable=SC2120  # Optional extra installer arguments.
    install_two() {
        env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
            "$REPO_ROOT/install.sh" --config "$CONFIG2" "$@"
    }
    # An upgrade from a release where the service user owned the data root and had
    # planted a symlink: root must refuse before chowning through it.
    install -d -m 0750 -o "$svc" -g "$svc" "$DATA2" "$VICTIM"
    chown root:root "$VICTIM"
    ln -s "$VICTIM" "$DATA2/grafana"
    chown -h "$svc:$svc" "$DATA2/grafana"
    if install_two >/dev/null 2>"$TEST_ROOT/symlink.stderr"; then
        echo "Installer followed a service-planted symlink" >&2
        exit 1
    fi
    grep -q 'is a symlink' "$TEST_ROOT/symlink.stderr"
    test "$(stat -c '%u:%g' "$VICTIM")" = "0:0"
    rm -f -- "$DATA2/grafana"
    install_two >/dev/null
    test "$(stat -c '%u:%g:%a' "$DATA2")" = "0:$svc_gid:750"
    test "$(stat -c '%u:%g' "$DATA2/influxdb")" = "1000:1000"
    test "$(stat -c '%u:%g' "$DATA2/grafana")" = "472:0"
    test "$(stat -c '%U' "$DATA2/boat-chat")" = "$svc"
    # The memory index is created by the service user, never by root, and root leaves no
    # root-owned SQLite side files behind in the service-writable state directory.
    test -s "$DATA2/boat-chat/memory/boat_memory.sqlite"
    test "$(stat -c '%U' "$DATA2/boat-chat/memory/boat_memory.sqlite")" = "$svc"
    test -z "$(find "$DATA2/boat-chat/memory" -not -user "$svc" -print -quit)"
    grep -q -- "^-u $svc -- env -i " "$TEST_ROOT/runuser.log"
    test "$(stat -c '%u' "$DATA2/homeassistant")" = 0
    # Native SignalK keeps its service-user-owned state directory (not 1000:1000).
    NATIVE_BIN="$TEST_ROOT/native-bin"
    install -d "$NATIVE_BIN"
    printf '#!/bin/bash\necho 22\n' > "$NATIVE_BIN/node"
    printf '#!/bin/bash\nexit 0\n' > "$NATIVE_BIN/npm"
    chmod +x "$NATIVE_BIN/node" "$NATIVE_BIN/npm"
    sed -e 's|^SIGNALK_MODE=.*|SIGNALK_MODE="native"|' "$CONFIG2" > "$TEST_ROOT/native.env"
    env PATH="$NATIVE_BIN:$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
        "$REPO_ROOT/install.sh" --config "$TEST_ROOT/native.env" >/dev/null
    test "$(stat -c '%U:%a' "$DATA2/signalk")" = "$svc:750"
    # The service user can write its state directory but cannot create, rename or
    # replace anything directly under the data root.
    runuser -u "$svc" -- touch "$DATA2/boat-chat/state-write-test"
    if runuser -u "$svc" -- touch "$DATA2/planted" 2>/dev/null; then
        echo "Service user can create entries in the data root" >&2
        exit 1
    fi
    if runuser -u "$svc" -- mv "$DATA2/boat-chat" "$DATA2/boat-chat.moved" 2>/dev/null; then
        echo "Service user can rename a managed data entry" >&2
        exit 1
    fi
    # A managed intermediate directory that is not root-owned is refused, not followed.
    chown "$svc:$svc" "$DATA2/homeassistant"
    if install_two >/dev/null 2>"$TEST_ROOT/owner.stderr"; then
        echo "Installer wrote through a service-owned intermediate directory" >&2
        exit 1
    fi
    grep -q 'must be owned by root' "$TEST_ROOT/owner.stderr"
    chown root:root "$DATA2/homeassistant"
    # Quarantine directories created in the data tree by earlier builds move out of it.
    install -d -m 0700 "$DATA2/boat-chat-legacy-earlier.ABC123"
    printf 'old\n' > "$DATA2/boat-chat-legacy-earlier.ABC123/file"
    # A dangling symlink planted at a migration destination is never followed.
    install -d "$APP2/boat-chat/memory"
    printf 'legacy\n' > "$APP2/boat-chat/memory/legacy.txt"
    ln -s "$TEST_ROOT/not-created" "$DATA2/boat-chat/memory"
    install_two >/dev/null 2>"$TEST_ROOT/relocate.stderr"
    test ! -e "$DATA2/boat-chat-legacy-earlier.ABC123"
    test -f "$QUARANTINE_ROOT/boat-chat-legacy-earlier.ABC123/file"
    test ! -e "$TEST_ROOT/not-created"
    test ! -e "$APP2/boat-chat/memory"
    grep -q 'moved legacy' "$TEST_ROOT/relocate.stderr"
    # A symlinked quarantine root is refused.
    rm -rf -- "$QUARANTINE_ROOT"
    ln -s "$VICTIM" "$QUARANTINE_ROOT"
    install -d "$APP2/boat-chat/memory"
    if install_two >/dev/null 2>&1; then
        echo "Installer accepted a symlinked quarantine root" >&2
        exit 1
    fi
    rm -f -- "$QUARANTINE_ROOT"
fi

# A render-only re-run keeps the broker's ACL and passwords in sync without
# touching the broker-writable data directory.
AUTH_DIR="$APP_ROOT/config/mosquitto-auth"
rm -rf "$AUTH_DIR"
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$APP_ROOT/installer/install.sh" \
    --config "$APP_ROOT/config/vesselstack.env" >/dev/null
test -s "$AUTH_DIR/acl"
test -s "$AUTH_DIR/passwords"
grep -qx 'user homeassistant' "$AUTH_DIR/acl"
test "$(stat -c '%a:%u:%g' "$AUTH_DIR")" = 750:0:1883
test "$(stat -c '%a:%u:%g' "$AUTH_DIR/acl")" = 600:1883:1883
test "$(stat -c '%a:%u:%g' "$AUTH_DIR/passwords")" = 600:1883:1883
test ! -e "$DATA_ROOT/mosquitto"
# Preflight (--dry-run) must not create or change any MQTT access files.
acl_before=$(sha256sum "$AUTH_DIR/acl")
log_before=$(wc -l < "$FAKE_BIN/docker.log")
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$APP_ROOT/installer/install.sh" --dry-run \
    --config "$APP_ROOT/config/vesselstack.env" >/dev/null
test "$(sha256sum "$AUTH_DIR/acl")" = "$acl_before"
if tail -n +"$((log_before + 1))" "$FAKE_BIN/docker.log" | grep -Eq '^(run|pull|kill|image (pull|load)|compose (up|pull))'; then
    echo 'Preflight ran or pulled a container or signalled the broker' >&2
    exit 1
fi
test ! -e "$APP_ROOT/config/displays.secrets.json"

# Opt in: seeded instruments-only displays.json, bridge environment file, venv and
# an enabled unit; an existing displays.json is never overwritten.
ON_APP="$TEST_ROOT/on/opt/vesselstack"
ON_DATA="$TEST_ROOT/on/opt/vesselstack-data"
ON_CONFIG="$TEST_ROOT/displays-on.env"
sed -e "s|^VESSELSTACK_ROOT=.*|VESSELSTACK_ROOT=\"$ON_APP\"|" \
    -e "s|^VESSELSTACK_DATA=.*|VESSELSTACK_DATA=\"$ON_DATA\"|" \
    -e "s|^VESSELSTACK_BACKUP=.*|VESSELSTACK_BACKUP=\"$TEST_ROOT/on-backups\"|" "$CONFIG" > "$ON_CONFIG"
printf 'DISPLAYS_ENABLE="true"\nDISPLAYS_SIGNALK_TOKEN="to\\"k\\\\en"\nDISPLAYS_HA_TOKEN="ha\\"t\\\\ok"\n' >> "$ON_CONFIG"
: > "$FAKE_BIN/systemctl.log"
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$ON_CONFIG" >/dev/null
grep -qx 'enable vesselstack-displays-bridge.service' "$FAKE_BIN/systemctl.log"
# A render-only run restarts an already running bridge so it reads the new environment.
grep -qx 'try-restart vesselstack-displays-bridge.service' "$FAKE_BIN/systemctl.log"
if grep -q 'disable --now vesselstack-displays-bridge.service' "$FAKE_BIN/systemctl.log"; then
    echo "Displays bridge was disabled with DISPLAYS_ENABLE=true" >&2
    exit 1
fi
DJSON="$ON_APP/config/displays.json"
test -s "$DJSON"
test "$(stat -c '%a' "$DJSON")" = 644
test "$(jq -c '.displays' "$DJSON")" = '{}'
test "$(jq '.instruments | length' "$DJSON")" -eq 4
python3 "$REPO_ROOT/displays/displayctl.py" validate "$DJSON" >/dev/null
test ! -e "$ON_APP/config/mosquitto-auth/acl" || ! grep -q 'vsdisp-' "$ON_APP/config/mosquitto-auth/acl"
BENV="$ON_APP/config/displays-bridge.env"
test "$(stat -c '%a:%u:%g' "$BENV")" = 600:0:0
grep -qx "DISPLAYS_CONFIG=\"$ON_APP/config/displays.json\"" "$BENV"
grep -qx 'SIGNALK_URL="http://127.0.0.1:3000"' "$BENV"
grep -qx 'MQTT_HOST="127.0.0.1"' "$BENV"
grep -qx 'MQTT_PORT="1883"' "$BENV"
grep -qx 'DISPLAYS_BRIDGE_MQTT_USERNAME="vesselstack-bridge"' "$BENV"
grep -qx 'DISPLAYS_SIGNALK_TOKEN="to\\"k\\\\en"' "$BENV"
grep -qx 'DISPLAYS_HA_TOKEN="ha\\"t\\\\ok"' "$BENV"
grep -qx 'HOME_ASSISTANT_URL="http://127.0.0.1:8123"' "$BENV"
bridge_secret=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["bridge"]["mqtt_password"])' \
    "$ON_APP/config/displays.secrets.json")
grep -qx "DISPLAYS_BRIDGE_MQTT_PASSWORD=\"$bridge_secret\"" "$BENV"
grep -q 'displays/requirements.txt' "$ON_APP/displays/.venv/pip.log"
grep -q -- '--require-hashes' "$ON_APP/displays/.venv/pip.log"
test -s "$ON_APP/displays/bridge.py"
test ! -e "$ON_APP/installer/displays/.venv"
if grep -q 'DISPLAYS_BRIDGE_MQTT_PASSWORD\|'"$bridge_secret" "$ON_APP/config/vesselstack.env"; then
    echo "Bridge MQTT password leaked into vesselstack.env" >&2
    exit 1
fi
# Re-run: the operator's displays.json (and its mode fix-up) survives; the
# password is stable; turning displays off removes the secret-bearing env file.
jq '.mqtt_host = "boat.local"' "$DJSON" > "$TEST_ROOT/edited.json"
cp "$TEST_ROOT/edited.json" "$DJSON"
chmod 0600 "$DJSON"
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$ON_APP/installer/install.sh" --config "$ON_APP/config/vesselstack.env" >/dev/null
test "$(jq -r '.mqtt_host' "$DJSON")" = boat.local
test "$(stat -c '%a' "$DJSON")" = 644
grep -qx "DISPLAYS_BRIDGE_MQTT_PASSWORD=\"$bridge_secret\"" "$BENV"
sed -i 's/^DISPLAYS_ENABLE=.*/DISPLAYS_ENABLE=false/' "$ON_APP/config/vesselstack.env"
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$ON_APP/installer/install.sh" --config "$ON_APP/config/vesselstack.env" >/dev/null
test ! -e "$BENV"
test "$(jq -r '.mqtt_host' "$DJSON")" = boat.local
# A control character in the token is refused before anything is installed.
printf 'DISPLAYS_SIGNALK_TOKEN=$'"'"'bad\\ntoken'"'"'\n' >> "$ON_CONFIG"
if env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$ON_CONFIG" --dry-run >/dev/null 2>&1; then
    echo "Preflight accepted a SignalK token containing a line break" >&2
    exit 1
fi
# The same for the Home Assistant token.
sed -i '/^DISPLAYS_SIGNALK_TOKEN=/d' "$ON_CONFIG"
printf 'DISPLAYS_HA_TOKEN=$'"'"'bad\\ntoken'"'"'\n' >> "$ON_CONFIG"
if env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$ON_CONFIG" --dry-run >/dev/null 2>&1; then
    echo "Preflight accepted a Home Assistant token containing a line break" >&2
    exit 1
fi

# --- The service user must be able to run the bridge ------------------------------
# install.sh runs under umask 077 and as root; the bridge runs unprivileged. The
# environment and the copied sources must stay root-owned yet world-readable, even
# when the source checkout itself is private and when an earlier release left a
# 0700 environment behind.
assert_bridge_tree_readable() {
    local app=$1 venv=$1/displays/.venv
    test "$(stat -c '%a:%u:%g' "$venv")" = 755:0:0
    test -z "$(find "$venv" ! -perm -o=r)"
    test -z "$(find "$venv" -type d ! -perm -o=x)"
    test -z "$(find "$venv/bin" -type f ! -perm -o=x)"
    test -z "$(find "$app/displays" ! -perm -o=r)"
    test -z "$(find "$app/displays" -type d ! -perm -o=x)"
    test -r "$app/displays/boards/registry.json"
    test "$(find "$app/displays" ! -user root | wc -l)" -eq 0
}
assert_bridge_tree_readable "$ON_APP"
PRIVATE_SRC="$TEST_ROOT/private-src"
install -d -m 0755 "$PRIVATE_SRC"
tar -C "$REPO_ROOT" --exclude=.git --exclude=generated --exclude='__pycache__' -cf - . \
    | tar --no-same-owner -C "$PRIVATE_SRC" -xf -
chmod -R go-rwx "$PRIVATE_SRC"
P_APP="$TEST_ROOT/p/opt/vesselstack"
P_CONFIG="$TEST_ROOT/p.env"
sed -e "s|^VESSELSTACK_ROOT=.*|VESSELSTACK_ROOT=\"$P_APP\"|" \
    -e "s|^VESSELSTACK_DATA=.*|VESSELSTACK_DATA=\"$TEST_ROOT/p/opt/vesselstack-data\"|" \
    -e "s|^VESSELSTACK_BACKUP=.*|VESSELSTACK_BACKUP=\"$TEST_ROOT/p-backups\"|" "$CONFIG" > "$P_CONFIG"
printf 'DISPLAYS_ENABLE="true"\n' >> "$P_CONFIG"
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$PRIVATE_SRC/install.sh" --config "$P_CONFIG" >/dev/null
assert_bridge_tree_readable "$P_APP"
# A venv an earlier release left 0700/0600 is repaired by the next run.
chmod -R go-rwx "$P_APP/displays/.venv" "$P_APP/displays"
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$PRIVATE_SRC/install.sh" --config "$P_CONFIG" >/dev/null
assert_bridge_tree_readable "$P_APP"
# The Boat Chat environment is owned by the service user as before.
test "$(stat -c '%U' "$P_APP/boat-chat/.venv")" = "$(id -un)"
# Unreachable PyPI aborts before anything is rendered, with a clear message.
OFF_APP="$TEST_ROOT/o/opt/vesselstack"
OFF_CONFIG="$TEST_ROOT/o.env"
sed -e "s|^VESSELSTACK_ROOT=.*|VESSELSTACK_ROOT=\"$OFF_APP\"|" \
    -e "s|^VESSELSTACK_DATA=.*|VESSELSTACK_DATA=\"$TEST_ROOT/o/opt/vesselstack-data\"|" \
    -e "s|^VESSELSTACK_BACKUP=.*|VESSELSTACK_BACKUP=\"$TEST_ROOT/o-backups\"|" "$CONFIG" > "$OFF_CONFIG"
printf 'DISPLAYS_ENABLE="true"\n' >> "$OFF_CONFIG"
if env FAKE_PIP_FAIL=displays PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$OFF_CONFIG" >"$TEST_ROOT/offline.out" 2>&1; then
    echo "install.sh succeeded although pip failed" >&2
    exit 1
fi
grep -q 'Could not install the displays bridge dependencies' "$TEST_ROOT/offline.out"
test ! -e "$OFF_APP/compose.yml"
test ! -e "$OFF_APP/config/vesselstack.env"
test ! -e "$OFF_APP/config/mosquitto-auth"
test ! -e "$OFF_APP/boat-chat"

# --- Failure atomicity, preflight and the Mosquitto configuration path ---------
tree_digest() {
    (cd "$1" && { find . -print0 | sort -z | xargs -0 stat -c '%n %a %u:%g'; \
        find . -type f -print0 | sort -z | xargs -0 sha256sum; } | sha256sum)
}
X_APP="$TEST_ROOT/x/opt/vesselstack"
X_DATA="$TEST_ROOT/x/opt/vesselstack-data"
X_CONFIG="$TEST_ROOT/x.env"
sed -e "s|^VESSELSTACK_ROOT=.*|VESSELSTACK_ROOT=\"$X_APP\"|" \
    -e "s|^VESSELSTACK_DATA=.*|VESSELSTACK_DATA=\"$X_DATA\"|" \
    -e "s|^VESSELSTACK_BACKUP=.*|VESSELSTACK_BACKUP=\"$TEST_ROOT/x-backups\"|" "$CONFIG" > "$X_CONFIG"
printf 'DISPLAYS_ENABLE="true"\n' >> "$X_CONFIG"
# A release-0.3.0 style install left a single-file mosquitto.conf behind.
install -d -m 0755 "$X_APP/config"
printf 'legacy-marker\n' > "$X_APP/config/mosquitto.conf"
chmod 0640 "$X_APP/config/mosquitto.conf"
xinstall() { env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" "$@"; }
xinstall "$REPO_ROOT/install.sh" --config "$X_CONFIG" >/dev/null
grep -qx legacy-marker "$X_APP/config/mosquitto.conf"
test "$(stat -c '%a' "$X_APP/config/mosquitto.conf")" = 640
grep -q '^acl_file ' "$X_APP/config/mosquitto/mosquitto.conf"
XENV="$X_APP/config/vesselstack.env"

expect_unchanged_failure() { # LABEL COMMAND... : must fail and leave the install tree untouched
    local label=$1 before after
    shift
    before=$(tree_digest "$X_APP")
    if "$@" >"$TEST_ROOT/failure.out" 2>&1; then
        echo "$label: expected a failure" >&2
        cat "$TEST_ROOT/failure.out" >&2
        exit 1
    fi
    after=$(tree_digest "$X_APP")
    [ "$before" = "$after" ] || { echo "$label: the failed run changed the installation" >&2; exit 1; }
}

# 1. An administrator password that mosquitto_passwd would store differently is
# refused up front, by a dry run and by a real run, with nothing changed.
BADPW="$TEST_ROOT/badpw.env"
cp "$XENV" "$BADPW"
printf 'MQTT_PASSWORD="bad:password"\n' >> "$BADPW"
expect_unchanged_failure "dry-run with ':' password" xinstall "$X_APP/installer/install.sh" --config "$BADPW" --dry-run
grep -q "MQTT_PASSWORD" "$TEST_ROOT/failure.out"
expect_unchanged_failure "real run with ':' password" xinstall "$X_APP/installer/install.sh" --config "$BADPW"
printf '\nMQTT_PASSWORD=" padded"\n' >> "$BADPW"
expect_unchanged_failure "dry-run with padded password" xinstall "$X_APP/installer/install.sh" --config "$BADPW" --dry-run

# 1b. A value the env files cannot hold safely (a single quote with a $) is refused
# by a dry run and by a real run BEFORE anything changes: the installed env files
# stay byte-identical, no temp file is left behind, and the message names only the
# key, never the value.
BADVAL="$TEST_ROOT/badval.env"
cp "$XENV" "$BADVAL"
cat >> "$BADVAL" <<'CFG'
HOME_ASSISTANT_TOKEN="it's \$SECRET"
CFG
env_before=$(sha256sum "$XENV" "$X_APP/config/boat-chat.env" "$X_APP/config/control-panel.env")
expect_unchanged_failure "dry-run with unsafe env value" xinstall "$X_APP/installer/install.sh" --config "$BADVAL" --dry-run
grep -q "HOME_ASSISTANT_TOKEN" "$TEST_ROOT/failure.out"
grep -q 'Upgrading to 0.6.0' "$TEST_ROOT/failure.out"
if grep -q 'SECRET' "$TEST_ROOT/failure.out"; then echo "the refusal printed the value" >&2; exit 1; fi
expect_unchanged_failure "real run with unsafe env value" xinstall "$X_APP/installer/install.sh" --config "$BADVAL"
grep -q "HOME_ASSISTANT_TOKEN" "$TEST_ROOT/failure.out"
test "$env_before" = "$(sha256sum "$XENV" "$X_APP/config/boat-chat.env" "$X_APP/config/control-panel.env")"
test -z "$(find "$X_APP/config" -maxdepth 1 -name '.*.env.*' -print -quit)"
# A safe value with an apostrophe still passes the dry run.
GOODVAL="$TEST_ROOT/goodval.env"
cp "$XENV" "$GOODVAL"
cat >> "$GOODVAL" <<'CFG'
HOME_ASSISTANT_TOKEN="it's fine"
CFG
xinstall "$X_APP/installer/install.sh" --config "$GOODVAL" --dry-run > "$TEST_ROOT/goodval.out"
grep -q 'Preflight passed' "$TEST_ROOT/goodval.out"

# 1b-bis. Legacy BOAT_UNITS=nautical (offered by older wizards, unchecked before 0.5.7) is read as
# metric: the dry run prints one note and changes nothing; a real run persists metric. Any other
# unsupported value is refused before anything changes, with a message naming the file and fix.
NAUTICAL="$TEST_ROOT/nautical.env"
grep -v '^BOAT_UNITS=' "$XENV" > "$NAUTICAL"
printf 'BOAT_UNITS=nautical\n' >> "$NAUTICAL"
env_before=$(sha256sum "$XENV")
xinstall "$X_APP/installer/install.sh" --config "$NAUTICAL" --dry-run >"$TEST_ROOT/nautical.out" 2>&1
grep -q 'Preflight passed' "$TEST_ROOT/nautical.out"
test "$(grep -c 'BOAT_UNITS=nautical is no longer a separate option; using metric (knots and nautical miles are kept)' "$TEST_ROOT/nautical.out")" -eq 1
test "$env_before" = "$(sha256sum "$XENV")"
IMPERIAL="$TEST_ROOT/imperial.env"
grep -v '^BOAT_UNITS=' "$XENV" > "$IMPERIAL"
printf 'BOAT_UNITS=imperial\n' >> "$IMPERIAL"
expect_unchanged_failure "dry-run with unsupported units" xinstall "$X_APP/installer/install.sh" --config "$IMPERIAL" --dry-run
grep -qF "Refusing to continue: BOAT_UNITS=imperial in $IMPERIAL is not supported." "$TEST_ROOT/failure.out"
grep -qF "To fix: set it to us_customary or metric, for example: sudo sed -i 's/^BOAT_UNITS=.*/BOAT_UNITS=metric/' $IMPERIAL" "$TEST_ROOT/failure.out"
expect_unchanged_failure "real run with unsupported units" xinstall "$X_APP/installer/install.sh" --config "$IMPERIAL"
grep -q 'BOAT_UNITS=imperial' "$TEST_ROOT/failure.out"
xinstall "$X_APP/installer/install.sh" --config "$NAUTICAL" >/dev/null 2>&1
grep -q "^BOAT_UNITS=metric$" "$XENV"
if grep -q nautical "$XENV"; then echo "the installed config still says nautical" >&2; exit 1; fi

# 1b-ter. Interactive fixes (VESSELSTACK_PROMPT_ANSWERS is the test hook for the terminal):
# an unsupported units value or an unsafe saved value is fixed at the prompt, a copy of the
# previous file is kept (root-only), and the installer restarts with the same arguments.
# Cancel, end of input and unattended runs change nothing; generated secrets are never offered.
ANS="$TEST_ROOT/answers"
xask() { # ANSWERS_FILE COMMAND... : run with the terminal replaced by the answers file
    local answers=$1
    shift
    env -u VESSELSTACK_NONINTERACTIVE VESSELSTACK_PROMPT_ANSWERS="$answers" PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" "$@"
}
fresh_config() { # NAME : a copy of the installed config; sets FRESH
    FRESH="$TEST_ROOT/$1.env"
    cp "$XENV" "$FRESH"
}
copies_of() { compgen -G "$1.before-fix-*" || true; }
inst="$X_APP/installer/install.sh"

# Units: [2] metric.
fresh_config fix-units
grep -v '^BOAT_UNITS=' "$XENV" > "$FRESH"
printf 'BOAT_UNITS=imperial\n' >> "$FRESH"
printf '2\n' > "$ANS"
xask "$ANS" "$inst" --config "$FRESH" --dry-run >"$TEST_ROOT/fix.out" 2>&1
grep -q 'Preflight passed' "$TEST_ROOT/fix.out"
grep -qF "BOAT_UNITS in $FRESH is \"imperial\", which is not supported." "$TEST_ROOT/fix.out"
grep -qF '[1] US customary (feet, °F, psi, US gallons)' "$TEST_ROOT/fix.out"
grep -qF '[2] Metric (metres, °C, kPa, litres)' "$TEST_ROOT/fix.out"
grep -qF '[c] Cancel (nothing is changed)' "$TEST_ROOT/fix.out"
grep -q "^Changed BOAT_UNITS in $FRESH (previous copy: $FRESH.before-fix-" "$TEST_ROOT/fix.out"
grep -qx 'BOAT_UNITS=metric' "$FRESH"
test "$(grep -c '^BOAT_UNITS=' "$FRESH")" -eq 1
test "$(copies_of "$FRESH" | wc -l)" -eq 1
fix_copy=$(copies_of "$FRESH")
test "$(stat -c '%a %u:%g' "$fix_copy")" = "600 0:0"
grep -qx 'BOAT_UNITS=imperial' "$fix_copy"
test "$(stat -c '%a' "$FRESH")" = "$(stat -c '%a' "$XENV")"
test -z "$(find "$TEST_ROOT" -maxdepth 1 -name '.*.fix.*' -print -quit)"
# A bad choice is asked again; [1] is US customary; the dry run changed nothing installed.
fresh_config fix-units1
grep -v '^BOAT_UNITS=' "$XENV" > "$FRESH"
printf 'BOAT_UNITS=imperial\n' >> "$FRESH"
printf 'x\n1\n' > "$ANS"
xask "$ANS" "$inst" --config "$FRESH" --dry-run >"$TEST_ROOT/fix.out" 2>&1
grep -q 'Please enter one of: 1, 2, or c to cancel.' "$TEST_ROOT/fix.out"
grep -qx 'BOAT_UNITS=us_customary' "$FRESH"
# Anything but exactly one listed token is a bad answer: "1 2" and "1 b" are asked again and
# never guess (a bad answer must not fall through to metric).
fresh_config fix-units-multi
grep -v '^BOAT_UNITS=' "$XENV" > "$FRESH"
printf 'BOAT_UNITS=imperial\n' >> "$FRESH"
printf '1 2\n1 b\n 2 1 \n1\n' > "$ANS"
xask "$ANS" "$inst" --config "$FRESH" --dry-run >"$TEST_ROOT/fix.out" 2>&1
test "$(grep -c 'Please enter one of: 1, 2, or c to cancel.' "$TEST_ROOT/fix.out")" -eq 3
grep -qx 'BOAT_UNITS=us_customary' "$FRESH"
fresh_config fix-units-multi-cancel
grep -v '^BOAT_UNITS=' "$XENV" > "$FRESH"
printf 'BOAT_UNITS=imperial\n' >> "$FRESH"
multi_before=$(sha256sum "$FRESH")
printf '1 2\nc\n' > "$ANS"
if xask "$ANS" "$inst" --config "$FRESH" --dry-run >"$TEST_ROOT/fix.out" 2>&1; then
    echo "a multi-token answer was accepted" >&2
    exit 1
fi
test "$multi_before" = "$(sha256sum "$FRESH")"
test -z "$(copies_of "$FRESH")"

# A config in a folder other users can change is never edited, even when the answer is valid.
if [ "$(id -u)" -eq 0 ]; then
    open_dir="$TEST_ROOT/open-dir"
    mkdir -p "$open_dir"
    chmod 0777 "$open_dir"
    grep -v '^BOAT_UNITS=' "$XENV" > "$open_dir/vs.env"
    printf 'BOAT_UNITS=imperial\n' >> "$open_dir/vs.env"
    open_before=$(sha256sum "$open_dir/vs.env")
    printf '2\n' > "$ANS"
    if xask "$ANS" "$inst" --config "$open_dir/vs.env" --dry-run >"$TEST_ROOT/fix.out" 2>&1; then
        echo "a config in a world-writable folder was edited" >&2
        exit 1
    fi
    grep -qF "its folder (or a parent folder) is not root-owned, or can be changed by other users" "$TEST_ROOT/fix.out"
    grep -qF "Edit it by hand instead:" "$TEST_ROOT/fix.out"
    test "$open_before" = "$(sha256sum "$open_dir/vs.env")"
    test -z "$(copies_of "$open_dir/vs.env")"
fi

# Cancel and end of input leave the file byte-identical and print one clear line.
fresh_config fix-cancel
grep -v '^BOAT_UNITS=' "$XENV" > "$FRESH"
printf 'BOAT_UNITS=imperial\n' >> "$FRESH"
cancel_before=$(sha256sum "$FRESH")
for answers in 'c\n' 'C\n' ''; do
    # shellcheck disable=SC2059
    printf "$answers" > "$ANS"
    if xask "$ANS" "$inst" --config "$FRESH" --dry-run >"$TEST_ROOT/fix.out" 2>&1; then
        echo "a cancelled prompt succeeded" >&2
        exit 1
    fi
    grep -qx 'Cancelled. Nothing was changed.' "$TEST_ROOT/fix.out"
    test "$cancel_before" = "$(sha256sum "$FRESH")"
    test -z "$(copies_of "$FRESH")"
done

# Unsafe saved value: hidden re-entry, nothing printed, an unsafe replacement is refused.
fresh_config fix-token
# shellcheck disable=SC2016  # literal $ in the fixture
printf 'HOME_ASSISTANT_TOKEN="it'"'"'s \\$SECRET"\n' >> "$FRESH"
# shellcheck disable=SC2016  # literal $ in the fixture
printf '1\nzqbad'"'"'\\$x\nnew-token-value\n' > "$ANS"
xask "$ANS" "$inst" --config "$FRESH" --dry-run >"$TEST_ROOT/fix.out" 2>&1
grep -q 'Preflight passed' "$TEST_ROOT/fix.out"
grep -qF 'HOME_ASSISTANT_TOKEN in' "$TEST_ROOT/fix.out"
grep -qF '[1] Enter a new value for HOME_ASSISTANT_TOKEN' "$TEST_ROOT/fix.out"
grep -q 'That value still cannot be written safely' "$TEST_ROOT/fix.out"
for secret in SECRET zqbad 'new-token-value'; do
    if grep -q "$secret" "$TEST_ROOT/fix.out"; then echo "the prompt printed a typed or saved value" >&2; exit 1; fi
done
grep -qx 'HOME_ASSISTANT_TOKEN=new-token-value' "$FRESH"
test "$(grep -c '^HOME_ASSISTANT_TOKEN=' "$FRESH")" -eq 1
test "$(copies_of "$FRESH" | wc -l)" -eq 1
grep -q 'SECRET' "$(copies_of "$FRESH")"
# A control character in a display token is fixed the same way, after a units fix in the same
# run (two restarts; the answers carry over).
fresh_config fix-two
grep -v '^BOAT_UNITS=' "$XENV" > "$FRESH"
printf 'BOAT_UNITS=imperial\nDISPLAYS_SIGNALK_TOKEN="a\tb"\n' >> "$FRESH"
printf '2\n1\nclean-token\n' > "$ANS"
xask "$ANS" "$inst" --config "$FRESH" --dry-run >"$TEST_ROOT/fix.out" 2>&1
grep -q 'Preflight passed' "$TEST_ROOT/fix.out"
grep -qx 'BOAT_UNITS=metric' "$FRESH"
grep -qx 'DISPLAYS_SIGNALK_TOKEN=clean-token' "$FRESH"
test "$(copies_of "$FRESH" | wc -l)" -eq 2

# Generated or preserved secrets are never offered for replacement (the service still
# holds the old one); the hard stop explains. Unattended runs behave exactly as before.
fresh_config fix-secret
# shellcheck disable=SC2016  # literal $ in the fixture
printf 'INFLUXDB_PASSWORD="it'"'"'s \\$x"\n' >> "$FRESH"
hardstop_before=$(sha256sum "$FRESH")
printf '1\nreplacement\n' > "$ANS"
if xask "$ANS" "$inst" --config "$FRESH" --dry-run >"$TEST_ROOT/fix.out" 2>&1; then
    echo "a bad secret was accepted" >&2
    exit 1
fi
grep -q 'INFLUXDB_PASSWORD' "$TEST_ROOT/fix.out"
grep -q 'must be changed in that service first' "$TEST_ROOT/fix.out"
grep -q 'Nothing was changed' "$TEST_ROOT/fix.out"
if grep -q 'Enter a new value' "$TEST_ROOT/fix.out"; then echo "a secret was offered for replacement" >&2; exit 1; fi
test "$hardstop_before" = "$(sha256sum "$FRESH")"
test -z "$(copies_of "$FRESH")"

# A value that does not come from the file cannot be fixed by editing it: hard stop.
fresh_config fix-env
grep -v '^BOAT_UNITS=' "$XENV" > "$FRESH"
printf '2\n' > "$ANS"
if BOAT_UNITS=imperial xask "$ANS" "$inst" --config "$FRESH" --dry-run >"$TEST_ROOT/fix.out" 2>&1; then
    echo "an unsupported environment value was accepted" >&2
    exit 1
fi
grep -qF "BOAT_UNITS=imperial in $FRESH is not supported." "$TEST_ROOT/fix.out"
test -z "$(copies_of "$FRESH")"

# A symbolic link is never rewritten; the round cap stops a restart loop.
fresh_config fix-link-target
grep -v '^BOAT_UNITS=' "$XENV" > "$FRESH"
printf 'BOAT_UNITS=imperial\n' >> "$FRESH"
ln -s "$FRESH" "$TEST_ROOT/fix-link.env"
link_before=$(sha256sum "$FRESH")
printf '2\n' > "$ANS"
if xask "$ANS" "$inst" --config "$TEST_ROOT/fix-link.env" --dry-run >"$TEST_ROOT/fix.out" 2>&1; then
    echo "a symbolic-link config was rewritten" >&2
    exit 1
fi
grep -q 'it is not a regular file' "$TEST_ROOT/fix.out"
grep -q 'Edit it by hand instead' "$TEST_ROOT/fix.out"
test "$link_before" = "$(sha256sum "$FRESH")"
printf '2\n' > "$ANS"
if VESSELSTACK_REMEDIATION_ROUND=3 xask "$ANS" "$inst" --config "$FRESH" --dry-run >"$TEST_ROOT/fix.out" 2>&1; then
    echo "the restart cap did not stop the run" >&2
    exit 1
fi
grep -q 'Too many fixes in one run' "$TEST_ROOT/fix.out"
test "$link_before" = "$(sha256sum "$FRESH")"
test -z "$(copies_of "$FRESH")"

# Unattended, with and without a terminal that is not available: the old message, nothing asked.
printf '2\n' > "$ANS"
# The answers hook is ignored unless the file is private to this user: a group- or world-writable
# file or a symbolic link is never read.
chmod 0666 "$ANS"
if env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" VESSELSTACK_PROMPT_ANSWERS="$ANS" \
    "$inst" --config "$IMPERIAL" --dry-run >"$TEST_ROOT/fix.out" 2>&1 </dev/null; then
    echo "a world-writable answers file was honoured" >&2
    exit 1
fi
if grep -q 'Choice:' "$TEST_ROOT/fix.out"; then echo "a world-writable answers file was read" >&2; exit 1; fi
chmod 0600 "$ANS"
if env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" VESSELSTACK_PROMPT_ANSWERS="$ANS" \
    "$inst" --config "$IMPERIAL" --dry-run >"$TEST_ROOT/fix.out" 2>&1; then
    echo "an unattended run accepted an unsupported value" >&2
    exit 1
fi
grep -qF "BOAT_UNITS=imperial in $IMPERIAL is not supported." "$TEST_ROOT/fix.out"
grep -qF "To fix: set it to us_customary or metric" "$TEST_ROOT/fix.out"
if grep -q 'Choice:' "$TEST_ROOT/fix.out"; then echo "an unattended run asked a question" >&2; exit 1; fi
if env -u VESSELSTACK_NONINTERACTIVE PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    setsid "$inst" --config "$IMPERIAL" --dry-run </dev/null >"$TEST_ROOT/fix.out" 2>&1; then
    echo "a run without a terminal accepted an unsupported value" >&2
    exit 1
fi
grep -qF "BOAT_UNITS=imperial in $IMPERIAL is not supported." "$TEST_ROOT/fix.out"
test -z "$(copies_of "$IMPERIAL")"

# The real thing: a pseudo-terminal, answers typed on stdin of `script`.
if command -v script >/dev/null 2>&1 && command -v setsid >/dev/null 2>&1; then
    fresh_config fix-pty
    grep -v '^BOAT_UNITS=' "$XENV" > "$FRESH"
    printf 'BOAT_UNITS=imperial\n' >> "$FRESH"
    pty_cmd="env -u VESSELSTACK_NONINTERACTIVE PATH=$FAKE_BIN:$PATH VESSELSTACK_SYSTEM_ROOT=$SYSTEM_ROOT $inst --config $FRESH --dry-run"
    # Give the installer a moment to reach the prompt; the pty keeps early input queued anyway.
    { sleep 2; printf '2\n'; sleep 3; } | timeout 120 setsid script -qec "$pty_cmd" /dev/null >"$TEST_ROOT/pty.out" 2>&1 || true
    grep -q 'Preflight passed' "$TEST_ROOT/pty.out"
    grep -qx 'BOAT_UNITS=metric' "$FRESH"
    test "$(copies_of "$FRESH" | wc -l)" -eq 1
else
    echo "SKIP: pty smoke test (script or setsid not installed)"
fi
# Nothing above touched the installed tree or left temp files beside the configs.
test -z "$(find "$TEST_ROOT" -maxdepth 1 -name '.*.fix.*' -print -quit)"

# 1c. Stale env temp files (exact mktemp names) are removed by a real run; a dry run
# leaves them, and similarly named files are never touched.
for stale in .vesselstack.env.AbC123 .boat-chat.env.xYz789 .control-panel.env.000000; do : > "$X_APP/config/$stale"; done
for keep in .vesselstack.env.short .vesselstack.env.AbC1234 .other.env.AbC123 vesselstack.env.AbC123; do : > "$X_APP/config/$keep"; done
xinstall "$X_APP/installer/install.sh" --config "$XENV" --dry-run >/dev/null
test -e "$X_APP/config/.vesselstack.env.AbC123"
xinstall "$X_APP/installer/install.sh" --config "$XENV" >/dev/null
for stale in .vesselstack.env.AbC123 .boat-chat.env.xYz789 .control-panel.env.000000; do
    test ! -e "$X_APP/config/$stale" || { echo "stale temp $stale was not removed" >&2; exit 1; }
done
for keep in .vesselstack.env.short .vesselstack.env.AbC1234 .other.env.AbC123 vesselstack.env.AbC123; do
    test -e "$X_APP/config/$keep" || { echo "$keep should not have been removed" >&2; exit 1; }
    rm -f "$X_APP/config/$keep"
done

# 2. Bridge username colliding with the administrator account.
COLLIDE="$TEST_ROOT/collide.env"
cp "$XENV" "$COLLIDE"
printf 'DISPLAYS_BRIDGE_MQTT_USERNAME="homeassistant"\n' >> "$COLLIDE"
expect_unchanged_failure "dry-run with colliding bridge name" xinstall "$X_APP/installer/install.sh" --config "$COLLIDE" --dry-run
grep -q "more than one account" "$TEST_ROOT/failure.out"

# 3. An invalid displays.json (checked only when displays are enabled).
cp "$X_APP/config/displays.json" "$TEST_ROOT/displays.good"
printf '{not json\n' > "$X_APP/config/displays.json"
expect_unchanged_failure "dry-run with invalid displays.json" xinstall "$X_APP/installer/install.sh" --config "$XENV" --dry-run
grep -q "displays.json" "$TEST_ROOT/failure.out"
cp "$TEST_ROOT/displays.good" "$X_APP/config/displays.json"

# 4. Missing Mosquitto image: offline is a clear failure that never pulls; with
# a reachable registry the dry run only warns ("will be pulled") and still pulls nothing.
: > "$FAKE_BIN/docker.log"
expect_unchanged_failure "dry-run without the image, offline" \
    env FAKE_DOCKER_NO_IMAGE=1 PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$X_APP/installer/install.sh" --config "$XENV" --dry-run
grep -q 'docker pull eclipse-mosquitto:' "$TEST_ROOT/failure.out"
env FAKE_DOCKER_NO_IMAGE=1 FAKE_DOCKER_REGISTRY_OK=1 PATH="$FAKE_BIN:$PATH" \
    VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" "$X_APP/installer/install.sh" --config "$XENV" --dry-run \
    > "$TEST_ROOT/dry.out" 2> "$TEST_ROOT/dry.err"
grep -q 'Preflight passed' "$TEST_ROOT/dry.out"
grep -q 'will be pulled' "$TEST_ROOT/dry.err"
if grep -Eq '^(pull|run) ' "$FAKE_BIN/docker.log"; then
    echo "A dry run pulled or ran a container" >&2
    exit 1
fi
# A real run offline without the image fails before any file is touched.
expect_unchanged_failure "real run without the image, offline" \
    env FAKE_DOCKER_NO_IMAGE=1 PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$X_APP/installer/install.sh" --config "$XENV"

# 5. A hashing failure during the real run aborts before the new compose
# definition and Mosquitto configuration are installed.
printf '# previous-compose-marker\n' >> "$X_APP/compose.yml"
printf '# previous-conf-marker\n' >> "$X_APP/config/mosquitto/mosquitto.conf"
conf_before=$(sha256sum "$X_APP/config/mosquitto/mosquitto.conf" "$X_APP/compose.yml")
if env FAKE_DOCKER_FAIL_RUN=1 PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$X_APP/installer/install.sh" --config "$XENV" >"$TEST_ROOT/failure.out" 2>&1; then
    echo "A hashing failure did not abort the installer" >&2
    exit 1
fi
grep -q 'hashing MQTT passwords' "$TEST_ROOT/failure.out"
test "$conf_before" = "$(sha256sum "$X_APP/config/mosquitto/mosquitto.conf" "$X_APP/compose.yml")"
grep -q previous-compose-marker "$X_APP/compose.yml"
grep -q previous-conf-marker "$X_APP/config/mosquitto/mosquitto.conf"
# ...and the next healthy run repairs both.
xinstall "$X_APP/installer/install.sh" --config "$XENV" >/dev/null
if grep -q previous- "$X_APP/compose.yml" "$X_APP/config/mosquitto/mosquitto.conf"; then
    echo "A healthy run did not restore compose.yml and mosquitto.conf" >&2
    exit 1
fi

# 6. A symlinked config/mosquitto is refused before anything is written.
S_APP="$TEST_ROOT/s/opt/vesselstack"
S_CONFIG="$TEST_ROOT/s.env"
sed -e "s|^VESSELSTACK_ROOT=.*|VESSELSTACK_ROOT=\"$S_APP\"|" \
    -e "s|^VESSELSTACK_DATA=.*|VESSELSTACK_DATA=\"$TEST_ROOT/s/opt/vesselstack-data\"|" \
    -e "s|^VESSELSTACK_BACKUP=.*|VESSELSTACK_BACKUP=\"$TEST_ROOT/s-backups\"|" "$CONFIG" > "$S_CONFIG"
install -d -m 0755 "$S_APP/config" "$TEST_ROOT/elsewhere"
ln -s "$TEST_ROOT/elsewhere" "$S_APP/config/mosquitto"
if xinstall "$REPO_ROOT/install.sh" --config "$S_CONFIG" >"$TEST_ROOT/symlink.out" 2>&1; then
    echo "install.sh accepted a symlinked config/mosquitto" >&2
    exit 1
fi
grep -q 'symbolic link' "$TEST_ROOT/symlink.out"
test -z "$(ls -A "$TEST_ROOT/elsewhere")"
test ! -e "$S_APP/compose.yml"

# 7. Render-only runs never recreate containers, but tell the operator when the
# running broker predates the new configuration; a current broker needs no notice.
OLD_MOUNTS="/mosquitto/config/mosquitto.conf=$X_APP/config/mosquitto.conf\n/mosquitto/data=$X_DATA/mosquitto\n"
NEW_MOUNTS="/mosquitto/config=$X_APP/config/mosquitto\n/mosquitto/data=$X_DATA/mosquitto\n/mosquitto/auth=$X_APP/config/mosquitto-auth\n"
: > "$FAKE_BIN/docker.log"
env FAKE_MOSQ_MOUNTS="$OLD_MOUNTS" PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$X_APP/installer/install.sh" --config "$XENV" > "$TEST_ROOT/render-old.out"
grep -q 'NOTICE: the running Mosquitto container' "$TEST_ROOT/render-old.out"
grep -q 'vesselstackctl start' "$TEST_ROOT/render-old.out"
env FAKE_MOSQ_MOUNTS="$NEW_MOUNTS" PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$X_APP/installer/install.sh" --config "$XENV" > "$TEST_ROOT/render-new.out"
if grep -q 'NOTICE: the running Mosquitto container' "$TEST_ROOT/render-new.out"; then
    echo "A current broker produced the upgrade notice" >&2
    exit 1
fi
if grep -Eq '^(compose (up|down|stop|restart|rm)|stop|rm|restart|kill -s KILL)' "$FAKE_BIN/docker.log"; then
    echo "A render-only run recreated or stopped a container" >&2
    exit 1
fi

# Docker-mode SignalK: the bind-mount source is created for the container user
# (uid/gid 1000 of the signalk-server image; the ordering before compose up is
# checked statically in test-distribution.sh), a root:root directory
# left by Docker is repaired, symlinks are refused and existing mode creates nothing.
# Ownership assertions need root.
APP3="$TEST_ROOT/opt3/vesselstack"
DATA3="$TEST_ROOT/opt3/vesselstack-data"
CONFIG3="$TEST_ROOT/signalk-docker.env"
sed -e "s|^VESSELSTACK_ROOT=.*|VESSELSTACK_ROOT=\"$APP3\"|" \
    -e "s|^VESSELSTACK_DATA=.*|VESSELSTACK_DATA=\"$DATA3\"|" \
    -e 's|^SIGNALK_MODE=.*|SIGNALK_MODE="docker"|' "$CONFIG" > "$CONFIG3"
install_three() {
    env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
        "$REPO_ROOT/install.sh" --config "$CONFIG3"
}
install_three >/dev/null
test -d "$DATA3/signalk"
test ! -e "$DATA_ROOT/signalk"
if [ "$(id -u)" -eq 0 ]; then
    test "$(stat -c '%u:%g:%a' "$DATA3/signalk")" = "1000:1000:750"
    # Docker created the source as root:root on an earlier run: empty, repaired.
    chown 0:0 "$DATA3/signalk"
    chmod 0755 "$DATA3/signalk"
    repair_output=$(install_three)
    grep -q 'Repaired SignalK data directory ownership' <<<"$repair_output"
    test "$(stat -c '%u:%g' "$DATA3/signalk")" = "1000:1000"
    # A non-empty root-owned tree is repaired recursively, locked while it happens, and
    # keeps its original mode.
    install -d "$DATA3/signalk/plugin-config-data"
    printf '{}\n' > "$DATA3/signalk/plugin-config-data/a.json"
    chown -R 0:0 "$DATA3/signalk"
    chmod 0755 "$DATA3/signalk"
    repair_output=$(install_three)
    grep -q 'recursive' <<<"$repair_output"
    test "$(stat -c '%u:%g:%a' "$DATA3/signalk")" = "1000:1000:755"
    test "$(stat -c '%u:%g' "$DATA3/signalk/plugin-config-data/a.json")" = "1000:1000"
    # Prometheus (nobody, 65534): same helper; an empty root-owned directory is repaired.
    test "$(stat -c '%u:%g:%a' "$DATA3/prometheus")" = "65534:65534:750"
    chown 0:0 "$DATA3/prometheus"
    repair_output=$(install_three)
    grep -q 'Repaired Prometheus data directory ownership' <<<"$repair_output"
    test "$(stat -c '%u:%g' "$DATA3/prometheus")" = "65534:65534"
    { cat "$CONFIG3"; printf 'PROMETHEUS_RETENTION="0"\n'; } > "$CONFIG3.bad"
    if env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" "$REPO_ROOT/install.sh" --config "$CONFIG3.bad" >/dev/null 2>&1; then
        echo 'PROMETHEUS_RETENTION=0 must be rejected' >&2; exit 1
    fi
    # Already correct: nothing is printed or changed.
    test -z "$(install_three | grep 'Repaired SignalK' || true)"
    sk_refused() {
        # $1 = expected stderr text. Nothing may change: owner, mode and contents.
        if install_three >/dev/null 2>"$TEST_ROOT/sk-inner.stderr"; then
            echo "Installer repaired a SignalK directory it must refuse ($1)" >&2
            exit 1
        fi
        grep -q "$1" "$TEST_ROOT/sk-inner.stderr"
    }
    sk_reset_root() { chown -R -h 0:0 "$DATA3/signalk"; chmod 0755 "$DATA3/signalk"; }
    # A symlink inside the tree is refused and nothing is rewritten.
    VICTIM3="$TEST_ROOT/victim3"
    install -d -m 0755 "$VICTIM3"
    ln -s "$VICTIM3" "$DATA3/signalk/escape"
    sk_reset_root
    sk_refused 'contains symlinks'
    test "$(stat -c '%u:%g:%a' "$DATA3/signalk")" = "0:0:755"
    test "$(stat -c '%u:%g' "$VICTIM3")" = "0:0"
    test "$(stat -c '%u:%g' "$DATA3/signalk/plugin-config-data/a.json")" = "0:0"
    rm -f -- "$DATA3/signalk/escape"
    # A file with a second hard link (possibly to a file outside) is refused.
    VICTIM_FILE="$TEST_ROOT/victim3-file"
    printf 'x\n' > "$VICTIM_FILE"
    chown 0:0 "$VICTIM_FILE"
    ln "$VICTIM_FILE" "$DATA3/signalk/plugin-config-data/linked.json"
    sk_reset_root
    sk_refused 'more than one hard link'
    test "$(stat -c '%u:%g:%a' "$DATA3/signalk")" = "0:0:755"
    test "$(stat -c '%u:%g' "$VICTIM_FILE")" = "0:0"
    rm -f -- "$DATA3/signalk/plugin-config-data/linked.json"
    # A special file (a FIFO) is refused.
    mkfifo "$DATA3/signalk/pipe"
    sk_reset_root
    sk_refused 'special files'
    test "$(stat -c '%u:%g:%a' "$DATA3/signalk")" = "0:0:755"
    rm -f -- "$DATA3/signalk/pipe"
    # A populated directory owned by somebody else is not the known Docker state:
    # no change at all, with manual instructions.
    sk_reset_root
    chown -R 4242:4242 "$DATA3/signalk"
    sk_refused 'owned by 4242:4242'
    grep -q 'making no change' "$TEST_ROOT/sk-inner.stderr"
    grep -q "chown -R -h 1000:1000" "$TEST_ROOT/sk-inner.stderr"
    test "$(stat -c '%u:%g:%a' "$DATA3/signalk")" = "4242:4242:755"
    test "$(stat -c '%u:%g' "$DATA3/signalk/plugin-config-data/a.json")" = "4242:4242"
    chown -R 1000:1000 "$DATA3/signalk"
fi
# A symlink in place of the signalk directory itself is refused.
rm -rf -- "$DATA3/signalk"
ln -s "$TEST_ROOT" "$DATA3/signalk"
if install_three >/dev/null 2>"$TEST_ROOT/sk-link.stderr"; then
    echo "Installer followed a symlinked SignalK data directory" >&2
    exit 1
fi
grep -q 'is a symlink' "$TEST_ROOT/sk-link.stderr"
rm -f -- "$DATA3/signalk"

# Installer --start honors persisted component exclusions. Influx bootstrap is
# skipped, and an excluded broker is neither force-recreated nor stamped.
printf '\nVESSELSTACK_DISABLED_COMPONENTS="influxdb,mosquitto"\n' >> "$CONFIG"
rm -f -- "$APP_ROOT/config/.mosquitto-conf.sha256"
: > "$FAKE_BIN/docker.log"
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$CONFIG" --start >/dev/null
if grep -Eq 'compose .* up -d .*influxdb|compose .* up -d influxdb|bootstrap-influx' "$FAKE_BIN/docker.log"; then
    echo 'excluded InfluxDB was started or bootstrapped' >&2
    exit 1
fi
if grep -Eq 'force-recreate mosquitto|compose .* up -d .*mosquitto' "$FAKE_BIN/docker.log"; then
    echo 'excluded Mosquitto was started or recreated' >&2
    exit 1
fi
test ! -e "$APP_ROOT/config/.mosquitto-conf.sha256"

# Settings from the Heimdall era: a retired "heimdall" entry in the disabled list is dropped (not
# translated to homepage) instead of aborting, HEIMDALL_PORT becomes HOMEPAGE_PORT and no
# HEIMDALL_* key is written back.
LEGACY_CONFIG="$TEST_ROOT/legacy-heimdall.env"
cp "$CONFIG" "$LEGACY_CONFIG"
printf '\nVESSELSTACK_DISABLED_COMPONENTS="heimdall, grafana"\nHEIMDALL_PORT="8081"\nHEIMDALL_HTTPS_PORT="8443"\n' >> "$LEGACY_CONFIG"
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$LEGACY_CONFIG" >/dev/null 2>"$TEST_ROOT/legacy.stderr"
grep -q "retired 'heimdall'" "$TEST_ROOT/legacy.stderr"
grep -q '^VESSELSTACK_DISABLED_COMPONENTS=grafana$' "$APP_ROOT/config/vesselstack.env"
grep -q '^HOMEPAGE_PORT=8081$' "$APP_ROOT/config/vesselstack.env"
if grep -qi 'heimdall' "$APP_ROOT/config/vesselstack.env"; then
    echo "Installed settings still mention Heimdall" >&2
    exit 1
fi
# An explicit HOMEPAGE_PORT wins over a leftover HEIMDALL_PORT.
printf 'HOMEPAGE_PORT="8090"\n' >> "$LEGACY_CONFIG"
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$LEGACY_CONFIG" >/dev/null 2>&1
grep -q '^HOMEPAGE_PORT=8090$' "$APP_ROOT/config/vesselstack.env"

echo "clean installation checks passed"
