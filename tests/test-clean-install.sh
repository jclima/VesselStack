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
exit 0
EOF
cat > "$FAKE_BIN/systemctl" <<'EOF'
#!/bin/bash
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
exit 0
PIP
    chmod +x "$3/bin/pip"
    exit 0
fi
exec /usr/bin/python3 "$@"
EOF
chmod +x "$FAKE_BIN/docker" "$FAKE_BIN/systemctl" "$FAKE_BIN/python3" "$FAKE_BIN/curl"

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
test -s "$SYSTEM_ROOT/etc/systemd/system/vesselstack-chat.service"
test -s "$SYSTEM_ROOT/etc/systemd/system/vesselstack-control-panel.service"
test -s "$SYSTEM_ROOT/etc/systemd/system/vesselstack-chat-telegram.service"
test -s "$SYSTEM_ROOT/etc/systemd/system/vesselstack-telemetry-indexer.service"
test -s "$SYSTEM_ROOT/etc/systemd/system/vesselstack-telemetry-indexer.timer"
test -s "$APP_ROOT/control-panel/app.py"
test -s "$APP_ROOT/installer/install.sh"
test -s "$APP_ROOT/config/grafana/dashboards/system-health.json"
test -s "$DATA_ROOT/homeassistant/blueprints/automation/vesselstack/low-battery.yaml"
test -s "$DATA_ROOT/homeassistant/vesselstack-dashboard.example.yaml"
test "$(stat -c '%u:%g' "$DATA_ROOT/influxdb")" = "1000:1000"
test "$(stat -c '%u:%g' "$DATA_ROOT/grafana")" = "472:0"
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
grep -qx "Environment=BOAT_CHAT_ENV=$DATA_ROOT/boat-chat/boat-chat.env" "$SYSTEM_ROOT/etc/systemd/system/vesselstack-chat.service"
grep -qx '1.3.0' "$APP_ROOT/config/installed-version"

access_token_before=$(sed -n 's/^BOAT_CHAT_ACCESS_TOKEN=//p' "$APP_ROOT/config/boat-chat.env")
panel_token_before=$(sed -n 's/^CONTROL_PANEL_TOKEN=//p' "$APP_ROOT/config/control-panel.env")
sed -i '/^BOAT_CHAT_PROVIDER=/d' "$APP_ROOT/config/boat-chat.env"
printf '%s\n' 'BOAT_CHAT_PROVIDER=ollama' 'OPENAI_API_KEY=preserve-test-secret' \
    >> "$APP_ROOT/config/boat-chat.env"
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$APP_ROOT/installer/install.sh" \
    --config "$APP_ROOT/config/vesselstack.env" >/dev/null
grep -qx 'BOAT_CHAT_PROVIDER=ollama' "$APP_ROOT/config/boat-chat.env"
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
test "$(sed -n 's/^INFLUXDB_TOKEN=//p' "$APP_ROOT/config/boat-chat.env")" = "$influx_token_chat_before"
test "$(sed -n 's/^BOAT_CHAT_SETTINGS_TOKEN=//p' "$APP_ROOT/config/boat-chat.env")" = "$settings_token_before"
test "$(sed -n 's/^BOAT_CHAT_ACCESS_TOKEN=//p' "$APP_ROOT/config/boat-chat.env")" = "$access_token_before"
test "$(sed -n 's/^CONTROL_PANEL_TOKEN=//p' "$APP_ROOT/config/control-panel.env")" = "$panel_token_before"
if grep -R 'GENERATE' "$APP_ROOT/config"; then
    echo "Re-render produced an ungenerated secret" >&2
    exit 1
fi

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
quarantine=$(find "$DATA_ROOT" -maxdepth 1 -type d -name 'boat-chat-legacy-*' | head -n 1)
test -n "$quarantine"
test "$(stat -c '%u:%a' "$quarantine")" = "0:700"
grep -qx 'legacy-memory' "$quarantine/memory/legacy.txt"
grep -qx 'legacy-data' "$quarantine/data/legacy.txt"
grep -qx 'OPENAI_API_KEY=legacy-secret' "$quarantine/boat-chat.env"
grep -qx 'current' "$DATA_ROOT/boat-chat/memory/current.txt"
test -z "$(find "$APP_ROOT/boat-chat" -not -type l \( -perm -g=w -o -perm -o=w \) -print -quit)"

echo "clean installation checks passed"
