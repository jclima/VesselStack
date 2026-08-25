#!/bin/bash
# VesselStack installer. Safe default: render only; --start launches services.

set -euo pipefail
umask 077

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SOURCE_ROOT=$SCRIPT_DIR
CONFIG_FILE="$SCRIPT_DIR/vesselstack.env"
SYSTEM_ROOT=${VESSELSTACK_SYSTEM_ROOT:-}
SYSTEMD_DIR="$SYSTEM_ROOT/etc/systemd/system"
SBIN_DIR="$SYSTEM_ROOT/usr/local/sbin"
START=0
DRY_RUN=0

usage() {
    echo "Usage: sudo ./install.sh [--config FILE] [--dry-run] [--start]"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --config) CONFIG_FILE=$2; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --start) START=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

[ -f "$CONFIG_FILE" ] || { echo "Missing config: $CONFIG_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
source "$CONFIG_FILE"

# Existing 0.x installations did not define configurable bucket names.
: "${INFLUXDB_RAW_BUCKET:=vesselstack_raw}"
: "${INFLUXDB_HISTORY_BUCKET:=vesselstack_1m}"
: "${GRAFANA_PORT:=43000}"
: "${HEIMDALL_PORT:=80}"
: "${HEIMDALL_HTTPS_PORT:=443}"
: "${INFLUXDB_PORT:=8086}"
: "${INFLUXDB_CONTAINER_NAME:=vesselstack-influxdb}"
: "${PROMETHEUS_PORT:=9090}"
: "${MQTT_PORT:=1883}"
: "${AIS_WEB_PORT:=8100}"
: "${AIS_TCP_PORT:=5011}"
: "${INFLUXDB_HOME_ASSISTANT_BUCKET:=homeassistant}"
: "${INFLUXDB_AIS_BUCKET:=ais}"
: "${SIGNALK_MODE:=existing}"
: "${SIGNALK_VERSION:=v2.27.0}"
: "${SOCKETCAN_ENABLE:=false}"
: "${SOCKETCAN_INTERFACE:=can0}"
: "${SOCKETCAN_BITRATE:=250000}"
: "${AIS_ENABLE:=false}"
: "${AIS_IMAGE:=ghcr.io/jvde-github/ais-catcher:v0.70}"
: "${AIS_DEVICE:=/dev/bus/usb}"
: "${AIS_CATCHER_ARGS:=-q -N 8100 -S 5011}"
: "${VESSELSTACK_FIREWALL_ENABLE:=false}"
: "${CONTROL_PANEL_HOST:=127.0.0.1}"
: "${CONTROL_PANEL_PORT:=8780}"

required=(BOAT_NAME BOAT_TIMEZONE VESSELSTACK_USER VESSELSTACK_ROOT VESSELSTACK_DATA
          SIGNALK_URL INFLUXDB_ORG INFLUXDB_USERNAME MQTT_USERNAME)
for key in "${required[@]}"; do
    [ -n "${!key:-}" ] || { echo "Required setting is empty: $key" >&2; exit 1; }
done

case "$VESSELSTACK_ROOT:$VESSELSTACK_DATA" in
    *" "*) echo "Install paths cannot contain spaces" >&2; exit 1 ;;
esac
case "$BOAT_NAME" in
    *[!A-Za-z0-9_.\ -]*) echo "BOAT_NAME may contain letters, numbers, spaces, dot, underscore, and hyphen" >&2; exit 1 ;;
esac
case "$SIGNALK_MODE" in
    existing|docker|native) ;;
    *) echo "SIGNALK_MODE must be existing, docker, or native" >&2; exit 1 ;;
esac
case "$INFLUXDB_CONTAINER_NAME" in
    ''|*[!A-Za-z0-9_.-]*)
        echo 'INFLUXDB_CONTAINER_NAME may contain letters, numbers, dot, underscore, and hyphen' >&2
        exit 1
        ;;
esac
case "$SOCKETCAN_ENABLE:$AIS_ENABLE:$VESSELSTACK_FIREWALL_ENABLE" in
    true:true:true|true:true:false|true:false:true|true:false:false|\
    false:true:true|false:true:false|false:false:true|false:false:false) ;;
    *) echo "Hardware and firewall enable settings must be true or false" >&2; exit 1 ;;
esac
for key in GRAFANA_PORT HEIMDALL_PORT HEIMDALL_HTTPS_PORT INFLUXDB_PORT \
    PROMETHEUS_PORT MQTT_PORT AIS_WEB_PORT AIS_TCP_PORT; do
    port_value=${!key}
    case "$port_value" in
        ''|*[!0-9]*) echo "$key must be a numeric TCP port" >&2; exit 1 ;;
    esac
    if [ "$port_value" -lt 1 ] || [ "$port_value" -gt 65535 ]; then
        echo "$key must be between 1 and 65535" >&2
        exit 1
    fi
done

COMPOSE_PROFILES=()
[ "$SIGNALK_MODE" = docker ] && COMPOSE_PROFILES+=(--profile signalk)
[ "$AIS_ENABLE" = true ] && COMPOSE_PROFILES+=(--profile ais)

compose() {
    docker compose "${COMPOSE_PROFILES[@]}" --env-file "$VESSELSTACK_ROOT/config/vesselstack.env" \
        -f "$VESSELSTACK_ROOT/compose.yml" "$@"
}

for command in curl docker jq openssl python3 sed sha256sum install tar systemctl; do
    command -v "$command" >/dev/null || { echo "Missing command: $command" >&2; exit 1; }
done
docker compose version >/dev/null || { echo "Docker Compose v2 is required" >&2; exit 1; }
id "$VESSELSTACK_USER" >/dev/null 2>&1 || { echo "User does not exist: $VESSELSTACK_USER" >&2; exit 1; }
# Written through the indirect configuration loop below.
# shellcheck disable=SC2034
VESSELSTACK_UID=$(id -u "$VESSELSTACK_USER")
# shellcheck disable=SC2034
VESSELSTACK_GID=$(id -g "$VESSELSTACK_USER")

if [ "$SIGNALK_MODE" = native ]; then
    command -v node >/dev/null || { echo "Native SignalK requires Node.js 22 or newer" >&2; exit 1; }
    command -v npm >/dev/null || { echo "Native SignalK requires npm" >&2; exit 1; }
    [ "$(node -p 'process.versions.node.split(".")[0]')" -ge 22 ] || {
        echo "Native SignalK requires Node.js 22 or newer" >&2
        exit 1
    }
fi
if [ "$SOCKETCAN_ENABLE" = true ]; then
    [ -e "/sys/class/net/$SOCKETCAN_INTERFACE" ] || {
        echo "SocketCAN interface not detected: $SOCKETCAN_INTERFACE" >&2
        exit 1
    }
fi
if [ "$AIS_ENABLE" = true ]; then
    [ -e "$AIS_DEVICE" ] || { echo "AIS device not detected: $AIS_DEVICE" >&2; exit 1; }
fi
if [ "$VESSELSTACK_FIREWALL_ENABLE" = true ]; then
    for firewall_command in /usr/sbin/iptables /usr/sbin/ip6tables; do
        [ -x "$firewall_command" ] || { echo "Missing firewall command: $firewall_command" >&2; exit 1; }
    done
    [ -e "/sys/class/net/$VESSELSTACK_UNTRUSTED_INTERFACE" ] || {
        echo "Untrusted interface not detected: $VESSELSTACK_UNTRUSTED_INTERFACE" >&2
        exit 1
    }
fi
if [ "$SIGNALK_MODE" = existing ]; then
    curl --fail --silent --show-error --max-time 5 "$SIGNALK_URL/signalk" >/dev/null || {
        echo "Existing SignalK did not respond at $SIGNALK_URL" >&2
        exit 1
    }
fi

if [ "$DRY_RUN" -eq 1 ]; then
    echo "Preflight passed"
    echo "Would install to $VESSELSTACK_ROOT with data in $VESSELSTACK_DATA"
    echo "SignalK mode: $SIGNALK_MODE ($SIGNALK_URL)"
    exit 0
fi

[ "$(id -u)" -eq 0 ] || { echo "Installation requires root" >&2; exit 1; }
install -d -m 0755 "$SYSTEMD_DIR" "$SBIN_DIR"

generate_secret() {
    local current=$1
    if [ -z "$current" ] || [ "$current" = GENERATE ]; then
        openssl rand -hex 32
    else
        printf '%s' "$current"
    fi
}

# Preserve generated administration tokens across re-renders when the installed
# configuration, rather than the original example file, is used as input.
if [ "${BOAT_CHAT_SETTINGS_TOKEN:-GENERATE}" = GENERATE ] && \
    [ -r "$VESSELSTACK_ROOT/config/boat-chat.env" ]; then
    BOAT_CHAT_SETTINGS_TOKEN=$(sed -n 's/^BOAT_CHAT_SETTINGS_TOKEN=//p' \
        "$VESSELSTACK_ROOT/config/boat-chat.env" | tail -n 1)
fi
if [ "${CONTROL_PANEL_TOKEN:-GENERATE}" = GENERATE ] && \
    [ -r "$VESSELSTACK_ROOT/config/control-panel.env" ]; then
    CONTROL_PANEL_TOKEN=$(sed -n 's/^CONTROL_PANEL_TOKEN=//p' \
        "$VESSELSTACK_ROOT/config/control-panel.env" | tail -n 1)
fi

INFLUXDB_PASSWORD=$(generate_secret "${INFLUXDB_PASSWORD:-GENERATE}")
INFLUXDB_TOKEN=$(generate_secret "${INFLUXDB_TOKEN:-GENERATE}")
GRAFANA_ADMIN_PASSWORD=$(generate_secret "${GRAFANA_ADMIN_PASSWORD:-GENERATE}")
BOAT_CHAT_SETTINGS_TOKEN=$(generate_secret "${BOAT_CHAT_SETTINGS_TOKEN:-GENERATE}")
CONTROL_PANEL_TOKEN=$(generate_secret "${CONTROL_PANEL_TOKEN:-GENERATE}")
MQTT_PASSWORD=$(generate_secret "${MQTT_PASSWORD:-GENERATE}")

install -d -m 0755 "$VESSELSTACK_ROOT" "$VESSELSTACK_ROOT/config" \
    "$VESSELSTACK_ROOT/boat-chat" "$VESSELSTACK_ROOT/control-panel" \
    "$VESSELSTACK_ROOT/installer"
install -d -m 0755 "$VESSELSTACK_ROOT/systemd"
install -d -m 0750 -o "$VESSELSTACK_USER" -g "$VESSELSTACK_USER" "$VESSELSTACK_DATA"
# Official images run their data processes as these fixed container users.
# Change only the mount root itself; never recursively rewrite existing data.
install -d -m 0750 -o 1000 -g 1000 "$VESSELSTACK_DATA/influxdb"
install -d -m 0750 -o 472 -g 0 "$VESSELSTACK_DATA/grafana"
tar -C "$SOURCE_ROOT/boat-chat" \
    --exclude=boat-chat.env --exclude=.venv --exclude=memory --exclude=data \
    --exclude='__pycache__' --exclude='*.pyc' -cf - . \
    | tar --no-same-owner -C "$VESSELSTACK_ROOT/boat-chat" -xf -
tar -C "$SOURCE_ROOT/control-panel" --exclude='__pycache__' --exclude='*.pyc' -cf - . \
    | tar --no-same-owner -C "$VESSELSTACK_ROOT/control-panel" -xf -
# Keep a bundled, reviewed installer so the control panel can run preflight and
# apply operations without depending on a source checkout that may be removed.
if [ "$SOURCE_ROOT" != "$VESSELSTACK_ROOT/installer" ]; then
    tar -C "$SOURCE_ROOT" --exclude='__pycache__' --exclude='*.pyc' \
        -cf - install.sh VERSION scripts systemd templates boat-chat control-panel \
        | tar --no-same-owner -C "$VESSELSTACK_ROOT/installer" -xf -
fi
# The source application keeps legacy defaults for compatibility. Installed
# branding is vessel-specific; the generated facts file remains authoritative.
sed -i "s/VesselStack/$BOAT_NAME/g" \
    "$VESSELSTACK_ROOT/boat-chat/static/index.html" \
    "$VESSELSTACK_ROOT/boat-chat/BOAT_CHAT_AGENT.md" \
    "$VESSELSTACK_ROOT/boat-chat/telegram_bot.py"
install -m 0644 "$SCRIPT_DIR/templates/compose.yml" "$VESSELSTACK_ROOT/compose.yml"
install -m 0644 "$SCRIPT_DIR/templates/prometheus.yml" "$VESSELSTACK_ROOT/config/prometheus.yml"
install -m 0644 "$SCRIPT_DIR/templates/mosquitto.conf" "$VESSELSTACK_ROOT/config/mosquitto.conf"
install -d -m 0755 "$VESSELSTACK_ROOT/config/grafana/provisioning/datasources" \
    "$VESSELSTACK_ROOT/config/grafana/provisioning/dashboards" \
    "$VESSELSTACK_ROOT/config/grafana/dashboards"
install -m 0644 "$SCRIPT_DIR/templates/grafana/provisioning/datasources/vesselstack.yml" \
    "$VESSELSTACK_ROOT/config/grafana/provisioning/datasources/vesselstack.yml"
install -m 0644 "$SCRIPT_DIR/templates/grafana/provisioning/dashboards/vesselstack.yml" \
    "$VESSELSTACK_ROOT/config/grafana/provisioning/dashboards/vesselstack.yml"
install -m 0644 "$SCRIPT_DIR/templates/grafana/dashboards/system-health.json" \
    "$VESSELSTACK_ROOT/config/grafana/dashboards/system-health.json"
install -d -m 0755 "$VESSELSTACK_DATA/homeassistant/blueprints/automation/vesselstack"
for blueprint in "$SCRIPT_DIR"/templates/homeassistant/blueprints/automation/vesselstack/*.yaml; do
    install -m 0644 "$blueprint" \
        "$VESSELSTACK_DATA/homeassistant/blueprints/automation/vesselstack/$(basename "$blueprint")"
done
install -m 0644 "$SCRIPT_DIR/templates/homeassistant/lovelace-vesselstack.example.yaml" \
    "$VESSELSTACK_DATA/homeassistant/vesselstack-dashboard.example.yaml"
install -m 0755 "$SCRIPT_DIR/scripts/vesselstackctl" "$SBIN_DIR/vesselstackctl"
install -m 0755 "$SCRIPT_DIR/scripts/vesselstack-firewall" "$SBIN_DIR/vesselstack-firewall"
sed "s|@VESSELSTACK_ROOT@|$VESSELSTACK_ROOT|g" \
    "$SCRIPT_DIR/systemd/vesselstack-firewall.service.in" \
    > "$SYSTEMD_DIR/vesselstack-firewall.service"
chmod 0644 "$SYSTEMD_DIR/vesselstack-firewall.service"
install -m 0644 "$SCRIPT_DIR/systemd/vesselstack-signalk.service.in" \
    "$VESSELSTACK_ROOT/systemd/vesselstack-signalk.service.in"
install -m 0644 "$SCRIPT_DIR/systemd/vesselstack-socketcan.service.in" \
    "$VESSELSTACK_ROOT/systemd/vesselstack-socketcan.service.in"

install -m 0600 /dev/null "$VESSELSTACK_ROOT/config/vesselstack.env"
while IFS= read -r key; do
    printf '%s=%q\n' "$key" "${!key:-}" >> "$VESSELSTACK_ROOT/config/vesselstack.env"
done <<'EOF'
BOAT_NAME
BOAT_TYPE
BOAT_MMSI
BOAT_CALLSIGN
BOAT_LOA_M
BOAT_BEAM_M
BOAT_TIMEZONE
BOAT_UNITS
VESSELSTACK_USER
VESSELSTACK_UID
VESSELSTACK_GID
VESSELSTACK_ROOT
VESSELSTACK_DATA
VESSELSTACK_BACKUP
SIGNALK_MODE
SIGNALK_VERSION
SIGNALK_URL
SOCKETCAN_ENABLE
SOCKETCAN_INTERFACE
SOCKETCAN_BITRATE
AIS_ENABLE
AIS_IMAGE
AIS_DEVICE
AIS_CATCHER_ARGS
AIS_WEB_PORT
AIS_TCP_PORT
VESSELSTACK_UNTRUSTED_INTERFACE
VESSELSTACK_FIREWALL_ENABLE
CONTROL_PANEL_HOST
CONTROL_PANEL_PORT
HOME_ASSISTANT_URL
HOME_ASSISTANT_TOKEN
INFLUXDB_URL
INFLUXDB_PORT
INFLUXDB_CONTAINER_NAME
INFLUXDB_ORG
INFLUXDB_USERNAME
INFLUXDB_RAW_BUCKET
INFLUXDB_HISTORY_BUCKET
INFLUXDB_HOME_ASSISTANT_BUCKET
INFLUXDB_AIS_BUCKET
INFLUXDB_PASSWORD
INFLUXDB_TOKEN
GRAFANA_ADMIN_PASSWORD
GRAFANA_PORT
HEIMDALL_PORT
HEIMDALL_HTTPS_PORT
PROMETHEUS_PORT
MQTT_USERNAME
MQTT_PASSWORD
MQTT_PORT
EOF

# Retain provider credentials and optional Boat Chat settings across installer
# re-renders while replacing the integration values owned by VesselStack.
boat_chat_temp=$(mktemp)
if [ -r "$VESSELSTACK_ROOT/config/boat-chat.env" ]; then
    cp "$VESSELSTACK_ROOT/config/boat-chat.env" "$boat_chat_temp"
fi
for key in BOAT_NAME VESSELSTACK_VERSION BOAT_CHAT_SETTINGS_TOKEN \
    SIGNALK_URL HOME_ASSISTANT_URL \
    HOME_ASSISTANT_TOKEN INFLUXDB_URL INFLUXDB_ORG INFLUXDB_TOKEN \
    INFLUXDB_RAW_BUCKET INFLUXDB_HISTORY_BUCKET \
    INFLUXDB_HOME_ASSISTANT_BUCKET INFLUXDB_AIS_BUCKET; do
    sed -i "/^${key}=/d" "$boat_chat_temp"
done
for key in BOAT_CHAT_PROVIDER BOAT_CHAT_HOST BOAT_CHAT_PORT; do
    if [ -n "${!key+x}" ]; then
        sed -i "/^${key}=/d" "$boat_chat_temp"
        printf '%s=%q\n' "$key" "${!key}" >> "$boat_chat_temp"
    elif ! grep -q "^${key}=" "$boat_chat_temp"; then
        case "$key" in
            BOAT_CHAT_PROVIDER) printf '%s=%q\n' "$key" local >> "$boat_chat_temp" ;;
            BOAT_CHAT_HOST) printf '%s=%q\n' "$key" 0.0.0.0 >> "$boat_chat_temp" ;;
            BOAT_CHAT_PORT) printf '%s=%q\n' "$key" 8765 >> "$boat_chat_temp" ;;
        esac
    fi
done
{
    printf 'BOAT_NAME=%q\n' "$BOAT_NAME"
    printf 'VESSELSTACK_VERSION=%q\n' "$(<"$SCRIPT_DIR/VERSION")"
    printf 'BOAT_CHAT_SETTINGS_TOKEN=%q\n' "$BOAT_CHAT_SETTINGS_TOKEN"
    printf 'SIGNALK_URL=%q\n' "$SIGNALK_URL"
    printf 'HOME_ASSISTANT_URL=%q\n' "$HOME_ASSISTANT_URL"
    printf 'HOME_ASSISTANT_TOKEN=%q\n' "${HOME_ASSISTANT_TOKEN:-}"
    printf 'INFLUXDB_URL=%q\n' "$INFLUXDB_URL"
    # shellcheck disable=SC2153  # Required and validated through the key list.
    printf 'INFLUXDB_ORG=%q\n' "$INFLUXDB_ORG"
    printf 'INFLUXDB_TOKEN=%q\n' "$INFLUXDB_TOKEN"
    printf 'INFLUXDB_RAW_BUCKET=%q\n' "$INFLUXDB_RAW_BUCKET"
    printf 'INFLUXDB_HISTORY_BUCKET=%q\n' "$INFLUXDB_HISTORY_BUCKET"
    printf 'INFLUXDB_HOME_ASSISTANT_BUCKET=%q\n' "$INFLUXDB_HOME_ASSISTANT_BUCKET"
    printf 'INFLUXDB_AIS_BUCKET=%q\n' "$INFLUXDB_AIS_BUCKET"
} >> "$boat_chat_temp"
install -m 0600 "$boat_chat_temp" "$VESSELSTACK_ROOT/config/boat-chat.env"
rm -f -- "$boat_chat_temp"
chown "$VESSELSTACK_USER:$VESSELSTACK_USER" "$VESSELSTACK_ROOT/config/boat-chat.env"

install -m 0600 /dev/null "$VESSELSTACK_ROOT/config/control-panel.env"
{
    printf 'CONTROL_PANEL_HOST=%q\n' "$CONTROL_PANEL_HOST"
    printf 'CONTROL_PANEL_PORT=%q\n' "$CONTROL_PANEL_PORT"
    printf 'CONTROL_PANEL_TOKEN=%q\n' "$CONTROL_PANEL_TOKEN"
} >> "$VESSELSTACK_ROOT/config/control-panel.env"

"$SCRIPT_DIR/scripts/migrate.sh" "$VESSELSTACK_ROOT/config/vesselstack.env" \
    "$(<"$SCRIPT_DIR/VERSION")"

jq -n \
    --arg name "$BOAT_NAME" --arg type "${BOAT_TYPE:-}" \
    --arg mmsi "${BOAT_MMSI:-}" --arg callsign "${BOAT_CALLSIGN:-}" \
    --arg loa "${BOAT_LOA_M:-}" --arg beam "${BOAT_BEAM_M:-}" \
    '{vessel:{name:$name,type:$type,mmsi:$mmsi,callsign:$callsign,loa_m:$loa,beam_m:$beam}}' \
    > "$VESSELSTACK_ROOT/boat-chat/boat_facts.json"

python3 -m venv "$VESSELSTACK_ROOT/boat-chat/.venv"
"$VESSELSTACK_ROOT/boat-chat/.venv/bin/pip" install -r "$VESSELSTACK_ROOT/boat-chat/requirements.txt"
chown -R "$VESSELSTACK_USER:$VESSELSTACK_USER" "$VESSELSTACK_ROOT/boat-chat"

sed -e "s|@VESSELSTACK_USER@|$VESSELSTACK_USER|g" \
    -e "s|@VESSELSTACK_ROOT@|$VESSELSTACK_ROOT|g" \
    "$SCRIPT_DIR/systemd/vesselstack-chat.service.in" > "$SYSTEMD_DIR/vesselstack-chat.service"
systemctl daemon-reload
systemctl enable vesselstack-chat.service
sed "s|@VESSELSTACK_ROOT@|$VESSELSTACK_ROOT|g" \
    "$SCRIPT_DIR/systemd/vesselstack-control-panel.service.in" \
    > "$SYSTEMD_DIR/vesselstack-control-panel.service"
chmod 0644 "$SYSTEMD_DIR/vesselstack-control-panel.service"
systemctl daemon-reload
systemctl enable vesselstack-control-panel.service

compose config --quiet

if [ "$SIGNALK_MODE" = native ]; then
    "$SCRIPT_DIR/scripts/install-signalk-native.sh" "$VESSELSTACK_ROOT/config/vesselstack.env"
fi

if [ "$START" -eq 1 ]; then
    "$SCRIPT_DIR/scripts/configure-hardware.sh" "$VESSELSTACK_ROOT/config/vesselstack.env"
    if [ "$VESSELSTACK_FIREWALL_ENABLE" = true ]; then
        systemctl enable --now vesselstack-firewall.service
    else
        systemctl disable --now vesselstack-firewall.service 2>/dev/null || true
    fi
    install -d -m 0750 "$VESSELSTACK_DATA/mosquitto"
    docker run --rm -v "$VESSELSTACK_DATA/mosquitto:/mosquitto/data" \
        eclipse-mosquitto:2.1.2-alpine mosquitto_passwd -b -c \
        /mosquitto/data/passwords "$MQTT_USERNAME" "$MQTT_PASSWORD"
    compose up -d
    "$SCRIPT_DIR/scripts/bootstrap-influx.sh" "$VESSELSTACK_ROOT/config/vesselstack.env"
    systemctl restart vesselstack-chat.service
    if [ "${VESSELSTACK_SKIP_PANEL_RESTART:-false}" != true ]; then
        systemctl restart vesselstack-control-panel.service
    fi
    if [ "$SIGNALK_MODE" = native ]; then
        systemctl restart vesselstack-signalk.service
    fi
fi

echo "VesselStack rendered successfully at $VESSELSTACK_ROOT"
echo "Secrets are stored in mode-600 config files and were not printed."
echo "Control panel: http://$CONTROL_PANEL_HOST:$CONTROL_PANEL_PORT"
