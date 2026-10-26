#!/bin/bash

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TEST_ROOT=$(mktemp -d)
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
    "$REPO_ROOT/install.sh" --config "$CONFIG" --dry-run | grep -q 'Preflight passed'
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
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$CONFIG" >/dev/null

test -x "$SYSTEM_ROOT/usr/local/sbin/vesselstackctl"
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
test ! -e "$APP_ROOT/config/displays-bridge.env"
test ! -e "$APP_ROOT/config/displays.json"
test ! -e "$APP_ROOT/displays/.venv"
test -s "$APP_ROOT/control-panel/app.py"
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
declare -A secret_before=()
for key in "${secret_keys[@]}"; do
    secret_before[$key]=$(sed -n "s/^${key}=//p" "$APP_ROOT/config/vesselstack.env")
    [[ "${secret_before[$key]}" =~ ^[a-f0-9]{64}$ ]] || { echo "$key was not generated" >&2; exit 1; }
done
influx_token_chat_before=$(sed -n 's/^INFLUXDB_TOKEN=//p' "$APP_ROOT/config/boat-chat.env")
settings_token_before=$(sed -n 's/^BOAT_CHAT_SETTINGS_TOKEN=//p' "$APP_ROOT/config/boat-chat.env")
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$CONFIG" >/dev/null
for key in "${secret_keys[@]}"; do
    test "$(sed -n "s/^${key}=//p" "$APP_ROOT/config/vesselstack.env")" = "${secret_before[$key]}"
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

echo "clean installation checks passed"
