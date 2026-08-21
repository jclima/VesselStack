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
EOF

env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$CONFIG" --dry-run | grep -q 'Preflight passed'
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$CONFIG" >/dev/null

test -x "$SYSTEM_ROOT/usr/local/sbin/vesselstackctl"
test -s "$SYSTEM_ROOT/etc/systemd/system/vesselstack-chat.service"
test -s "$APP_ROOT/config/grafana/dashboards/system-health.json"
test -s "$DATA_ROOT/homeassistant/blueprints/automation/vesselstack/low-battery.yaml"
test -s "$DATA_ROOT/homeassistant/vesselstack-dashboard.example.yaml"
test "$(stat -c '%u:%g' "$DATA_ROOT/influxdb")" = "1000:1000"
test "$(stat -c '%u:%g' "$DATA_ROOT/grafana")" = "472:0"
grep -q 'Test Vessel Boat Chat' "$APP_ROOT/boat-chat/static/index.html"
grep -q '^INFLUXDB_HISTORY_BUCKET=signalk_1m$' "$APP_ROOT/config/vesselstack.env"
if grep -R 'GENERATE' "$APP_ROOT/config"; then
    echo "Rendered configuration contains an ungenerated secret" >&2
    exit 1
fi
test "$(stat -c '%a' "$APP_ROOT/config/vesselstack.env")" = 600
grep -qx '1.0.1' "$APP_ROOT/config/installed-version"

echo "clean installation checks passed"
