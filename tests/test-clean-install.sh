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
grep -q '^acl_file /mosquitto/auth/acl$' "$APP_ROOT/config/mosquitto.conf"
grep -q '^password_file /mosquitto/auth/passwords$' "$APP_ROOT/config/mosquitto.conf"
grep -q 'config/mosquitto-auth:/mosquitto/auth:ro' "$APP_ROOT/compose.yml"
grep -q '^DISPLAYS_ENABLE=false$' "$APP_ROOT/config/vesselstack.env"
grep -q '^DISPLAYS_BRIDGE_MQTT_USERNAME=vesselstack-bridge$' "$APP_ROOT/config/vesselstack.env"
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
grep -qx '1.3.0' "$APP_ROOT/config/installed-version"

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
if tail -n +"$((log_before + 1))" "$FAKE_BIN/docker.log" | grep -Eq '^(run|pull|kill|image|compose (up|pull))'; then
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
printf 'DISPLAYS_ENABLE="true"\nDISPLAYS_SIGNALK_TOKEN="to\\"k\\\\en"\n' >> "$ON_CONFIG"
: > "$FAKE_BIN/systemctl.log"
env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
    "$REPO_ROOT/install.sh" --config "$ON_CONFIG" >/dev/null
grep -qx 'enable vesselstack-displays-bridge.service' "$FAKE_BIN/systemctl.log"
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
test "$(stat -c '%a:%u:%g' "$BENV")" = "600:$(id -u):$(id -g)"
grep -qx "DISPLAYS_CONFIG=\"$ON_APP/config/displays.json\"" "$BENV"
grep -qx 'SIGNALK_URL="http://127.0.0.1:3000"' "$BENV"
grep -qx 'MQTT_HOST="127.0.0.1"' "$BENV"
grep -qx 'MQTT_PORT="1883"' "$BENV"
grep -qx 'DISPLAYS_BRIDGE_MQTT_USERNAME="vesselstack-bridge"' "$BENV"
grep -qx 'DISPLAYS_SIGNALK_TOKEN="to\\"k\\\\en"' "$BENV"
bridge_secret=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["bridge"]["mqtt_password"])' \
    "$ON_APP/config/displays.secrets.json")
grep -qx "DISPLAYS_BRIDGE_MQTT_PASSWORD=\"$bridge_secret\"" "$BENV"
grep -q 'displays/requirements.txt' "$ON_APP/displays/.venv/pip.log"
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

echo "clean installation checks passed"
