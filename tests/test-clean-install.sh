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
grep -qx '1.0.1' "$APP_ROOT/config/installed-version"

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

echo "clean installation checks passed"
