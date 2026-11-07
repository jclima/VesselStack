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
LIBEXEC_PARENT="$SYSTEM_ROOT/usr/local/libexec"
LIBEXEC_DIR="$LIBEXEC_PARENT/vesselstack"
RELEASES_DIR="$SYSTEM_ROOT/var/lib/vesselstack/releases"
START=0
DRY_RUN=0
OFFLINE=${VESSELSTACK_OFFLINE:-false}

usage() {
    echo "Usage: sudo ./install.sh [--config FILE] [--dry-run] [--start] [--offline]"
    echo "  --offline  No network: install Python packages from VESSELSTACK_WHEELHOUSE and"
    echo "             require container images to be loaded already (see README, Offline install)"
}

# The arguments as given, so a fix made at a prompt can restart this installer unchanged.
ORIGINAL_ARGS=("$@")
while [ $# -gt 0 ]; do
    case "$1" in
        --config) CONFIG_FILE=$2; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --start) START=1; shift ;;
        --offline) OFFLINE=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

[ -f "$CONFIG_FILE" ] || { echo "Missing config: $CONFIG_FILE" >&2; exit 1; }
# Prompt helpers for the fixes offered below (see scripts/lib/remediate.sh).
# shellcheck source=scripts/lib/remediate.sh
source "$SCRIPT_DIR/scripts/lib/remediate.sh"
# After a fix this installer restarts itself with the answers descriptor still open; this process
# owns it now, so vs_prompt_close closes it before anything is installed or started.
if [ "${VESSELSTACK_REMEDIATION_ROUND:-0}" != 0 ] && [ -n "$VS_PROMPT_FD" ]; then VS_PROMPT_OWNED=1; fi
# config_key_assigned KEY: true when CONFIG_FILE itself assigns KEY (so editing it can fix it).
config_key_assigned() { grep -Eq "^(export )?$1=" "$CONFIG_FILE"; }
# Set below when an interactive run may offer to fix the value instead of stopping.
FIX_BOAT_UNITS=0
FIX_TOKEN_KEY=
# shellcheck disable=SC1090
source "$CONFIG_FILE"

# Existing 0.x installations did not define configurable bucket names.
: "${INFLUXDB_RAW_BUCKET:=vesselstack_raw}"
: "${INFLUXDB_HISTORY_BUCKET:=vesselstack_1m}"
: "${GRAFANA_PORT:=43000}"
: "${HOMEPAGE_PORT:=80}"
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
: "${SIGNALK_VESSELTWIN_PRELOAD:=true}"
: "${SOCKETCAN_ENABLE:=false}"
: "${SOCKETCAN_INTERFACE:=can0}"
: "${SOCKETCAN_BITRATE:=250000}"
: "${VESSELSTACK_CAN_CREATE_VCAN:=false}"
: "${AIS_ENABLE:=false}"
: "${AIS_IMAGE:=ghcr.io/jvde-github/ais-catcher:v0.70}"
: "${AIS_DEVICE:=/dev/bus/usb}"
: "${AIS_CATCHER_ARGS:=-q -N 8100 -S 5011}"
: "${VESSELSTACK_FIREWALL_ENABLE:=false}"
: "${VESSELSTACK_FIREWALL_STRICT:=false}"
: "${VESSELSTACK_FIREWALL_ALLOW_TCP=22}"
: "${VESSELSTACK_FIREWALL_ALLOW_UDP=}"
: "${CONTROL_PANEL_HOST:=127.0.0.1}"
: "${CONTROL_PANEL_PORT:=8780}"
# Whether the INPUT configuration really sets BOAT_CHAT_HOST: the installed vesselstack.env
# never carries it (it lives in config/boat-chat.env), so a re-render from it must keep
# the installed value (for example one set by `vesselstackctl lan local`), not reset it.
BOAT_CHAT_HOST_FROM_CONFIG=${BOAT_CHAT_HOST+x}
: "${BOAT_CHAT_HOST:=0.0.0.0}"
: "${VESSELSTACK_DATA_BIND_ADDRESS:=127.0.0.1}"
: "${VESSELSTACK_WEB_BIND_ADDRESS:=0.0.0.0}"
: "${HOMEASSISTANT_BIND_ADDRESS:=0.0.0.0}"
: "${BOAT_CHAT_PORT:=8765}"
: "${VESSELSTACK_BACKUP:=/var/backups/vesselstack}"
: "${TELEGRAM_ENABLE:=false}"
: "${TELEMETRY_INDEXER_ENABLE:=true}"
: "${BOAT_UNITS:=us_customary}"
# Older setup wizards offered "nautical" and installers before 0.5.7 did not check it, so
# such configs exist. It is read as metric (knots and nautical miles are kept in both
# systems), and the rewritten vesselstack.env persists "metric". Surrounding whitespace and
# letter case are tolerated for the two supported values.
BOAT_UNITS_INPUT=$BOAT_UNITS
BOAT_UNITS=${BOAT_UNITS#"${BOAT_UNITS%%[![:space:]]*}"}
BOAT_UNITS=${BOAT_UNITS%"${BOAT_UNITS##*[![:space:]]}"}
BOAT_UNITS=${BOAT_UNITS,,}
if [ "$BOAT_UNITS" = nautical ]; then
    echo "Note: BOAT_UNITS=nautical is no longer a separate option; using metric (knots and nautical miles are kept)." >&2
    BOAT_UNITS=metric
fi
: "${NODE_EXPORTER_ENABLE:=false}"
: "${DISPLAYS_ENABLE:=false}"
: "${DISPLAYS_BRIDGE_MQTT_USERNAME:=vesselstack-bridge}"
: "${DISPLAYS_SIGNALK_TOKEN:=}"
: "${DISPLAYS_HA_TOKEN:=}"
# Empty = unset: fresh InfluxDB setups get 30d on the raw bucket; existing buckets
# are never changed unless a value is set explicitly (see README).
: "${INFLUXDB_RAW_RETENTION:=}"
# Prometheus TSDB retention (time only; never 0, which Prometheus would treat as the default).
: "${PROMETHEUS_RETENTION:=30d}"
: "${VESSELSTACK_DISABLED_COMPONENTS:=}"

CORE_COMPONENTS=(homeassistant influxdb grafana prometheus mosquitto homepage)
disabled_input=$VESSELSTACK_DISABLED_COMPONENTS
VESSELSTACK_DISABLED_COMPONENTS=""
component_disabled() { case ",${VESSELSTACK_DISABLED_COMPONENTS}," in *,"$1",*) return 0 ;; *) return 1 ;; esac; }
while [ -n "$disabled_input" ]; do
    case "$disabled_input" in *,*) disabled=${disabled_input%%,*}; disabled_input=${disabled_input#*,} ;; *) disabled=$disabled_input; disabled_input="" ;; esac
    disabled=${disabled//[[:space:]]/}; [ -z "$disabled" ] && continue
    valid=0; for core in "${CORE_COMPONENTS[@]}"; do [ "$disabled" = "$core" ] && valid=1; done
    [ "$valid" -eq 1 ] || { echo "Unknown disabled component: $disabled" >&2; exit 1; }
    component_disabled "$disabled" && { echo "Duplicate disabled component: $disabled" >&2; exit 1; }
    VESSELSTACK_DISABLED_COMPONENTS="${VESSELSTACK_DISABLED_COMPONENTS:+$VESSELSTACK_DISABLED_COMPONENTS,}$disabled"
done

: "${VESSELSTACK_WHEELHOUSE:=$SCRIPT_DIR/offline/wheelhouse}"
case "$OFFLINE" in
    true|false) ;;
    *) echo "VESSELSTACK_OFFLINE must be true or false" >&2; exit 1 ;;
esac

required=(BOAT_NAME BOAT_TIMEZONE VESSELSTACK_USER VESSELSTACK_ROOT VESSELSTACK_DATA
          SIGNALK_URL INFLUXDB_ORG INFLUXDB_USERNAME MQTT_USERNAME)
for key in "${required[@]}"; do
    [ -n "${!key:-}" ] || { echo "Required setting is empty: $key" >&2; exit 1; }
done

case "$VESSELSTACK_ROOT:$VESSELSTACK_DATA" in
    *" "*) echo "Install paths cannot contain spaces" >&2; exit 1 ;;
esac
case "$VESSELSTACK_ROOT:$VESSELSTACK_DATA" in
    /home/*|*:/home/*|/root/*|*:/root/*)
        echo "VESSELSTACK_ROOT and VESSELSTACK_DATA cannot be under /home or /root (services use ProtectHome=true)" >&2
        exit 1 ;;
esac
case "$BOAT_NAME" in
    *[!A-Za-z0-9_.\ -]*) echo "BOAT_NAME may contain letters, numbers, spaces, dot, underscore, and hyphen" >&2; exit 1 ;;
esac
case "$SIGNALK_MODE" in
    existing|docker|native) ;;
    *) echo "SIGNALK_MODE must be existing, docker, or native" >&2; exit 1 ;;
esac
if [ "$SIGNALK_MODE" = native ] && [ "$OFFLINE" = true ]; then
    echo "Offline install: native SignalK installation requires its npm packages to be installed online first; use managed docker or existing mode." >&2
    exit 1
fi
case "$INFLUXDB_CONTAINER_NAME" in
    ''|*[!A-Za-z0-9_.-]*)
        echo 'INFLUXDB_CONTAINER_NAME may contain letters, numbers, dot, underscore, and hyphen' >&2
        exit 1
        ;;
esac
if [ -n "$INFLUXDB_RAW_RETENTION" ] && \
    ! [[ "$INFLUXDB_RAW_RETENTION" =~ ^(0|[1-9][0-9]{0,4}[hdw])$ ]]; then
    echo "INFLUXDB_RAW_RETENTION must be empty, 0 (keep forever), or a duration such as 720h, 30d or 8w" >&2
    exit 1
fi
if ! [[ "$PROMETHEUS_RETENTION" =~ ^[1-9][0-9]{0,4}[hdw]$ ]]; then
    echo "PROMETHEUS_RETENTION must be a duration such as 720h, 30d or 8w" >&2
    exit 1
fi
for key in SOCKETCAN_ENABLE VESSELSTACK_CAN_CREATE_VCAN AIS_ENABLE VESSELSTACK_FIREWALL_ENABLE VESSELSTACK_FIREWALL_STRICT \
    TELEGRAM_ENABLE TELEMETRY_INDEXER_ENABLE DISPLAYS_ENABLE SIGNALK_VESSELTWIN_PRELOAD NODE_EXPORTER_ENABLE; do
    case "${!key}" in
        true|false) ;;
        *) echo "$key must be true or false" >&2; exit 1 ;;
    esac
done
case "$BOAT_UNITS" in
    us_customary|metric) ;;
    *)
        if config_key_assigned BOAT_UNITS && vs_prompt_open; then
            FIX_BOAT_UNITS=1
        else
            echo "Refusing to continue: BOAT_UNITS=$(vs_printable "$BOAT_UNITS_INPUT") in $CONFIG_FILE is not supported." >&2
            echo "To fix: set it to us_customary or metric, for example: sudo sed -i 's/^BOAT_UNITS=.*/BOAT_UNITS=metric/' $CONFIG_FILE" >&2
            exit 1
        fi
        ;;
esac
# MQTT account names are written into the Mosquitto password and ACL files.
for key in MQTT_USERNAME DISPLAYS_BRIDGE_MQTT_USERNAME; do
    if ! [[ "${!key}" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]; then
        echo "$key may contain 1-64 letters, numbers, dot, underscore, and hyphen" >&2
        exit 1
    fi
done
# The optional SignalK and Home Assistant tokens go into a systemd EnvironmentFile line.
for key in DISPLAYS_SIGNALK_TOKEN DISPLAYS_HA_TOKEN; do
    if [[ "${!key}" =~ [[:cntrl:]] ]]; then
        if config_key_assigned "$key" && vs_prompt_open; then
            [ -n "$FIX_TOKEN_KEY" ] || FIX_TOKEN_KEY=$key
            continue
        fi
        echo "$key must not contain line breaks or control characters" >&2
        exit 1
    fi
done
for key in VESSELSTACK_FIREWALL_ALLOW_TCP VESSELSTACK_FIREWALL_ALLOW_UDP; do
    [[ "${!key}" =~ ^([0-9]{1,5}(:[0-9]{1,5})?(,[0-9]{1,5}(:[0-9]{1,5})?)*)?$ ]] || {
        echo "$key must be comma-separated ports or LOW:HIGH ranges, e.g. 22,5000:5100" >&2
        exit 1
    }
done
for key in GRAFANA_PORT HOMEPAGE_PORT INFLUXDB_PORT \
    PROMETHEUS_PORT MQTT_PORT AIS_WEB_PORT AIS_TCP_PORT BOAT_CHAT_PORT \
    CONTROL_PANEL_PORT; do
    port_value=${!key}
    case "$port_value" in
        ''|*[!0-9]*) echo "$key must be a numeric TCP port" >&2; exit 1 ;;
    esac
    if [ "$port_value" -lt 1 ] || [ "$port_value" -gt 65535 ]; then
        echo "$key must be between 1 and 65535" >&2
        exit 1
    fi
done
[[ "${HOMEPAGE_ALLOWED_HOSTS_EXTRA:-}" =~ ^[A-Za-z0-9_.:,-]*$ ]] || {
    echo "HOMEPAGE_ALLOWED_HOSTS_EXTRA must be comma-separated host names or HOST:PORT entries" >&2
    exit 1
}
for key in BOAT_CHAT_HOST CONTROL_PANEL_HOST VESSELSTACK_DATA_BIND_ADDRESS \
    VESSELSTACK_WEB_BIND_ADDRESS HOMEASSISTANT_BIND_ADDRESS; do
    python3 - "${!key}" <<'PY' || { echo "$key must be an IPv4 address" >&2; exit 1; }
import ipaddress
import sys
address = ipaddress.ip_address(sys.argv[1])
if address.version != 4:
    raise SystemExit(1)
PY
done

# Fail before installation when two enabled listeners would contend for the
# same host port. Host-networked Home Assistant and managed SignalK count too.
declare -A port_owner=()
register_port() {
    local owner=$1 value=$2
    if [ -n "${port_owner[$value]:-}" ]; then
        echo "Port $value is assigned to both ${port_owner[$value]} and $owner" >&2
        exit 1
    fi
    port_owner[$value]=$owner
}
register_port HomeAssistant 8123
register_port InfluxDB "$INFLUXDB_PORT"
register_port Grafana "$GRAFANA_PORT"
register_port Prometheus "$PROMETHEUS_PORT"
register_port MQTT "$MQTT_PORT"
register_port Homepage "$HOMEPAGE_PORT"
register_port BoatChat "$BOAT_CHAT_PORT"
register_port ControlPanel "$CONTROL_PANEL_PORT"
if [ "$AIS_ENABLE" = true ]; then
    register_port AISWeb "$AIS_WEB_PORT"
    register_port AISTCP "$AIS_TCP_PORT"
fi
if [ "$SIGNALK_MODE" != existing ]; then
    register_port SignalK 3000
fi

COMPOSE_PROFILES=()
[ "$SIGNALK_MODE" = docker ] && COMPOSE_PROFILES+=(--profile signalk)
[ "$AIS_ENABLE" = true ] && COMPOSE_PROFILES+=(--profile ais)
[ "$NODE_EXPORTER_ENABLE" = true ] && COMPOSE_PROFILES+=(--profile metrics)

compose() {
    docker compose "${COMPOSE_PROFILES[@]}" --env-file "$VESSELSTACK_ROOT/config/vesselstack.env" \
        -f "$VESSELSTACK_ROOT/compose.yml" "$@"
}

for command in curl docker jq openssl python3 sed sha256sum install tar systemctl; do
    command -v "$command" >/dev/null || { echo "Missing command: $command" >&2; exit 1; }
done
docker compose version >/dev/null || { echo "Docker Compose v2 is required" >&2; exit 1; }
id "$VESSELSTACK_USER" >/dev/null 2>&1 || { echo "User does not exist: $VESSELSTACK_USER" >&2; exit 1; }
python3 - "$VESSELSTACK_ROOT" "$VESSELSTACK_DATA" "$VESSELSTACK_BACKUP" <<'PY'
import pathlib
import sys

root, data, backup = (pathlib.Path(value).resolve(strict=False) for value in sys.argv[1:])
for label, path in (("VESSELSTACK_ROOT", root), ("VESSELSTACK_DATA", data), ("VESSELSTACK_BACKUP", backup)):
    if not path.is_absolute() or path == pathlib.Path("/"):
        raise SystemExit(f"{label} must be a specific absolute path, not {path}")
if root == data or root in data.parents or data in root.parents:
    raise SystemExit("VESSELSTACK_ROOT and VESSELSTACK_DATA must be separate, non-nested paths")
if backup == root or root in backup.parents or backup == data or data in backup.parents:
    raise SystemExit("VESSELSTACK_BACKUP must be outside VESSELSTACK_ROOT and VESSELSTACK_DATA")
PY
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
    case "$SOCKETCAN_INTERFACE" in
        can[0-9]*|vcan[0-9]*) ;;
        *) echo "SOCKETCAN_INTERFACE must look like can0 or vcan0" >&2; exit 1 ;;
    esac
    # A virtual interface may be created on demand (home-lab replay).
    if ! { [ "$VESSELSTACK_CAN_CREATE_VCAN" = true ] && [[ "$SOCKETCAN_INTERFACE" == vcan* ]]; }; then
        [ -e "${VESSELSTACK_SYS_CLASS_NET:-/sys/class/net}/$SOCKETCAN_INTERFACE" ] || {
            echo "SocketCAN interface not detected: $SOCKETCAN_INTERFACE" >&2
            exit 1
        }
    fi
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

# Check the pinned plugin before any installed file is touched. Existing mode is
# intentionally a no-op; offline managed installs require a pre-existing plugin.
if [ "$SIGNALK_VESSELTWIN_PRELOAD" = true ] && [ "$SIGNALK_MODE" != existing ] \
    && { [ "$OFFLINE" = true ] || [ "$DRY_RUN" -eq 1 ]; }; then
    preload_check_args=(--mode "$SIGNALK_MODE" --data "$VESSELSTACK_DATA" --check)
    [ "$OFFLINE" = true ] && preload_check_args+=(--offline)
    python3 "$SCRIPT_DIR/scripts/preload-signalk-vesseltwin.py" "${preload_check_args[@]}"
fi

# Everything that can fail for lack of a network, an image, a wheel or a supported
# upgrade path is checked here, before any installed file is touched.
if ! "$SCRIPT_DIR/scripts/migrate.sh" --check "$CONFIG_FILE" "$(<"$SCRIPT_DIR/VERSION")"; then
    echo "Preflight failed: this release cannot be installed over the current installation (see above). Nothing was changed." >&2
    exit 1
fi
compose_template() {
    SIGNALK_VERSION=$SIGNALK_VERSION AIS_IMAGE=$AIS_IMAGE \
        docker compose "${COMPOSE_PROFILES[@]}" --env-file "$CONFIG_FILE" \
        -f "$SCRIPT_DIR/templates/compose.yml" "$@"
}
compose_template config --quiet 2>/dev/null || {
    echo "Compose template validation failed for this configuration:" >&2
    compose_template config --quiet >&2 || true
    exit 1
}
# Python packages are only ever installed from the hash-pinned lock shipped in the
# release (pip --require-hashes, wheels only), online and offline.
PIP_LOCK="$SOURCE_ROOT/boat-chat/requirements.lock"
[ -s "$PIP_LOCK" ] || {
    echo "Missing $PIP_LOCK: the hash-pinned requirements lock must ship with the release (maintainers: scripts/update-requirements-lock.sh). Nothing was changed." >&2
    exit 1
}
if [ "$OFFLINE" = true ] && [ "$DISPLAYS_ENABLE" = true ] \
    && [ ! -x "$VESSELSTACK_ROOT/displays/.venv/bin/python" ]; then
    echo "Offline install: the displays bridge dependencies (displays/requirements.txt) are not part of the offline bundle yet. Install them once while online (run the installer without --offline), or set DISPLAYS_ENABLE=false. Nothing was changed." >&2
    exit 1
fi
if [ "$OFFLINE" = true ]; then
    [ -n "$(find "$VESSELSTACK_WHEELHOUSE" -maxdepth 1 -name '*.whl' -print -quit 2>/dev/null)" ] || {
        echo "Offline install: no Python wheels found in VESSELSTACK_WHEELHOUSE=$VESSELSTACK_WHEELHOUSE" >&2
        echo "Build the bundle on a connected machine with scripts/prepare-offline-bundle.sh and copy it here." >&2
        exit 1
    }
    # The wheelhouse must be <bundle>/wheelhouse of a bundle that verifies against this
    # release (manifest, lock hash, wheel hashes, no unlisted files; root-owned when
    # running as root). pip then reads exactly the verified directory.
    VESSELSTACK_WHEELHOUSE=$(readlink -f -- "$VESSELSTACK_WHEELHOUSE")
    [ "$(basename -- "$VESSELSTACK_WHEELHOUSE")" = wheelhouse ] || {
        echo "Offline install: VESSELSTACK_WHEELHOUSE must be the wheelhouse/ directory of an offline bundle" >&2
        exit 1
    }
    "$SCRIPT_DIR/scripts/prepare-offline-bundle.sh" --verify "$(dirname -- "$VESSELSTACK_WHEELHOUSE")" >/dev/null || {
        echo "Offline install: the offline bundle did not verify; refusing to install from it. Nothing was changed." >&2
        exit 1
    }
    mapfile -t required_images < <(
        compose_template config --images | sort -u
    )
    missing_images=()
    for image in "${required_images[@]}"; do
        docker image inspect "$image" >/dev/null 2>&1 || missing_images+=("$image")
    done
    if [ "${#missing_images[@]}" -gt 0 ]; then
        {
            echo "Offline install: these container images are not present locally:"
            printf '  %s\n' "${missing_images[@]}"
            echo "Load them from the offline bundle first, for example:"
            echo "  sudo scripts/prepare-offline-bundle.sh --load /path/to/offline-bundle"
            echo "(or: docker load -i <bundle>/images/<name>.tar for each image)"
        } >&2
        # Rendering configuration does not need images; starting services does.
        if [ "$START" -eq 1 ]; then exit 1; fi
        echo "WARNING: load the images before running with --start or starting services." >&2
    fi
fi
# Environment-file values. One predicate decides what cannot be written safely so
# the preflight below and env_line can never disagree.
# env-line-begin
vesselstack_env_keys() {
    cat <<'EOF'
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
SIGNALK_VESSELTWIN_PRELOAD
SOCKETCAN_ENABLE
SOCKETCAN_INTERFACE
SOCKETCAN_BITRATE
VESSELSTACK_CAN_CREATE_VCAN
AIS_ENABLE
AIS_IMAGE
AIS_DEVICE
AIS_CATCHER_ARGS
AIS_WEB_PORT
AIS_TCP_PORT
VESSELSTACK_DISABLED_COMPONENTS
VESSELSTACK_UNTRUSTED_INTERFACE
VESSELSTACK_FIREWALL_ENABLE
VESSELSTACK_FIREWALL_STRICT
VESSELSTACK_FIREWALL_ALLOW_TCP
VESSELSTACK_FIREWALL_ALLOW_UDP
CONTROL_PANEL_HOST
CONTROL_PANEL_PORT
TELEGRAM_ENABLE
TELEMETRY_INDEXER_ENABLE
NODE_EXPORTER_ENABLE
DISPLAYS_ENABLE
DISPLAYS_BRIDGE_MQTT_USERNAME
DISPLAYS_SIGNALK_TOKEN
DISPLAYS_HA_TOKEN
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
INFLUXDB_RAW_RETENTION
INFLUXDB_PASSWORD
INFLUXDB_TOKEN
GRAFANA_ADMIN_PASSWORD
GRAFANA_PORT
HOMEPAGE_PORT
HOMEPAGE_ALLOWED_HOSTS
HOMEPAGE_ALLOWED_HOSTS_EXTRA
PROMETHEUS_PORT
PROMETHEUS_RETENTION
MQTT_USERNAME
MQTT_PASSWORD
MQTT_PORT
VESSELSTACK_DATA_BIND_ADDRESS
VESSELSTACK_WEB_BIND_ADDRESS
HOMEASSISTANT_BIND_ADDRESS
EOF
}
# Keys written to boat-chat.env and control-panel.env beyond the list above.
extra_env_keys() {
    printf '%s\n' BOAT_CHAT_PROVIDER BOAT_CHAT_HOST BOAT_CHAT_PORT \
        BOAT_CHAT_SETTINGS_TOKEN BOAT_CHAT_ACCESS_TOKEN CONTROL_PANEL_TOKEN
}
# env_value_problem VALUE: prints the reason and returns 0 when VALUE cannot be
# written. Compose cannot parse the '\'' idiom, so a value with a single quote is
# double-quoted, which is only portable when it has no \ " $ or backtick.
env_value_problem() {
    local value=$1
    case $value in
        *$'\n'*|*$'\r'*) echo "a multi-line value"; return 0 ;;
    esac
    if [[ $value == *\'* && $value == *[\\\"\$\`]* ]]; then
        echo "a single quote combined with a backslash, double quote, dollar sign or backtick"
        return 0
    fi
    return 1
}
# env_line KEY VALUE: one env-file line that bash (`source`), systemd
# EnvironmentFile, Python shlex and Docker Compose all read back identically.
# Plain values stay bare; others are POSIX single-quoted; a value with a single
# quote is double-quoted (see env_value_problem for what is refused).
env_line() {
    local key=$1 value=$2 reason
    if reason=$(env_value_problem "$value"); then
        echo "Refusing value for $key: $reason" >&2
        exit 1
    fi
    if [ -n "$value" ] && [[ $value =~ ^[A-Za-z0-9_@%+=:,./-]+$ ]]; then
        printf '%s=%s\n' "$key" "$value"
    elif [[ $value != *\'* ]]; then
        printf "%s='%s'\n" "$key" "$value"
    else
        printf '%s="%s"\n' "$key" "$value"
    fi
}
# env-line-end

# --- Interactive fixes -------------------------------------------------------
# These run in the validation block, before the root check, the plugin staging and any
# trap, so a Cancel (or a fix that cannot be applied safely) changes nothing else.

# config_key_is_secret KEY: generated or preserved secrets. Replacing one here would
# desynchronise a running service (see preserve_secret), so it is never offered.
config_key_is_secret() {
    case $1 in
        INFLUXDB_PASSWORD|INFLUXDB_TOKEN|GRAFANA_ADMIN_PASSWORD|MQTT_PASSWORD|\
        BOAT_CHAT_SETTINGS_TOKEN|BOAT_CHAT_ACCESS_TOKEN|CONTROL_PANEL_TOKEN) return 0 ;;
    esac
    return 1
}

# config_env_snapshot FILE KEY: in a clean subshell, sources FILE and prints the declared
# state of every known setting except KEY (so a fix is checked against all the others).
config_env_snapshot() {
    local file=$1 skip=$2 key
    (
        set +eu
        while IFS= read -r key; do unset "$key"; done < <(vesselstack_env_keys; extra_env_keys)
        # shellcheck disable=SC1090
        source "$file" >/dev/null 2>&1 || exit 3
        while IFS= read -r key; do
            [ "$key" = "$skip" ] && continue
            declare -p "$key" 2>/dev/null || echo "unset $key"
        done < <(vesselstack_env_keys; extra_env_keys)
    )
}

# root_controlled_chain PATH: every existing component from PATH up to / is root-owned and not
# group- or world-writable, so no other user can swap the file this installer is about to edit.
root_controlled_chain() {
    local resolved uid mode
    resolved=$(realpath -m -- "$1") || return 1
    while :; do
        if [ -e "$resolved" ]; then
            read -r uid mode < <(stat -c '%u %a' -- "$resolved") || return 1
            if [ "$uid" != 0 ] || [ $((0$mode & 0022)) -ne 0 ]; then return 1; fi
        fi
        [ "$resolved" != / ] || return 0
        resolved=$(dirname -- "$resolved")
    done
}

# config_fix_key FILE KEY VALUE: sets KEY=VALUE in FILE (an env file this installer reads),
# keeping a root-only copy, and restarts this installer with the original arguments.
# Refuses (returns 1, FILE untouched) unless the result provably reads back the same for
# every other setting and gives KEY exactly VALUE.
config_fix_key() {
    local file=$1 key=$2 value=$3 line stamp copy tmp n=1 got round restart_env
    round=${VESSELSTACK_REMEDIATION_ROUND:-0}
    [[ $round =~ ^[0-9]+$ ]] || round=0
    if [ "$round" -ge 3 ]; then
        echo "Too many fixes in one run. Nothing more was changed; run the installer again." >&2
        return 1
    fi
    if [ -L "$file" ] || [ ! -f "$file" ]; then
        echo "Not changing $file: it is not a regular file (or is a symbolic link)." >&2
        return 1
    fi
    if [ "$(id -u)" -eq 0 ] && [ "$(stat -c '%u' "$file")" != 0 ]; then
        echo "Not changing $file: it is not owned by root." >&2
        return 1
    fi
    if [ "$(id -u)" -eq 0 ] && ! root_controlled_chain "$(dirname -- "$file")"; then
        echo "Not changing $file: its folder (or a parent folder) is not root-owned, or can be changed by other users." >&2
        return 1
    fi
    if env_value_problem "$value" >/dev/null || [[ $value =~ [[:cntrl:]] ]]; then
        echo "Not changing $file: that value cannot be written safely." >&2
        return 1
    fi
    line=$(env_line "$key" "$value")
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    copy="$file.before-fix-$stamp"
    while [ -e "$copy" ]; do n=$((n + 1)); copy="$file.before-fix-$stamp.$n"; done
    tmp=$(mktemp "$(dirname "$file")/.$(basename "$file").fix.XXXXXX") || {
        echo "Not changing $file: its folder is not writable." >&2
        return 1
    }
    if ! FIX_KEY=$key FIX_LINE=$line awk '
        BEGIN { key = ENVIRON["FIX_KEY"]; line = ENVIRON["FIX_LINE"]; done = 0 }
        index($0, key "=") == 1 || index($0, "export " key "=") == 1 {
            if (!done) { print line; done = 1 }
            next
        }
        { print }
        END { if (!done) print line }' "$file" > "$tmp" \
        || ! bash -n "$tmp" 2>/dev/null \
        || [ "$(config_env_snapshot "$file" "$key"; echo x)" != "$(config_env_snapshot "$tmp" "$key"; echo x)" ]; then
        rm -f -- "$tmp"
        echo "Not changing $file: the edit could not be verified. Edit $key in it by hand." >&2
        return 1
    fi
    got=$(
        set +eu
        # shellcheck disable=SC1090
        source "$tmp" >/dev/null 2>&1 || exit 3
        printf '%s' "${!key}"
        echo x
    ) || got=
    if [ "$got" != "${value}x" ]; then
        rm -f -- "$tmp"
        echo "Not changing $file: the edit could not be verified. Edit $key in it by hand." >&2
        return 1
    fi
    if [ "$(id -u)" -eq 0 ]; then
        if ! install -m 0600 -o 0 -g 0 "$file" "$copy" || ! chown --reference="$file" "$tmp"; then
            rm -f -- "$tmp" "$copy"
            echo "Not changing $file: could not keep a copy." >&2
            return 1
        fi
    else
        install -m 0600 "$file" "$copy" \
            || { rm -f -- "$tmp"; echo "Not changing $file: could not keep a copy." >&2; return 1; }
    fi
    chmod --reference="$file" "$tmp"
    mv -f -- "$tmp" "$file"
    echo "Changed $key in $file (previous copy: $copy). The copy is safe to delete once the install works." >&2
    # The answers descriptor (if any) is handed on so scripted answers continue where they were.
    restart_env=(VESSELSTACK_REMEDIATION_ROUND=$((round + 1)))
    if [ -n "$VS_PROMPT_FD" ]; then restart_env+=(VS_PROMPT_FD="$VS_PROMPT_FD"); fi
    exec env "${restart_env[@]}" "${BASH:-bash}" "$SCRIPT_DIR/install.sh" ${ORIGINAL_ARGS[@]+"${ORIGINAL_ARGS[@]}"}
}

# BOAT_UNITS other than us_customary, metric or the nautical mapping (flagged above).
fix_boat_units() {
    local choice new
    {
        echo "BOAT_UNITS in $CONFIG_FILE is \"$(vs_printable "$BOAT_UNITS_INPUT")\", which is not supported."
        echo "  [1] US customary (feet, °F, psi, US gallons)"
        echo "  [2] Metric (metres, °C, kPa, litres)"
        echo "  [c] Cancel (nothing is changed)"
    } >&2
    vs_prompt_choice choice "1 2"
    case $choice in
        1) new=us_customary ;;
        2) new=metric ;;
        *) echo "Unexpected answer; nothing was changed." >&2; exit 1 ;;
    esac
    config_fix_key "$CONFIG_FILE" BOAT_UNITS "$new" || {
        echo "Edit it by hand instead: sudo sed -i 's/^BOAT_UNITS=.*/BOAT_UNITS=$new/' $CONFIG_FILE" >&2
        exit 1
    }
}

# fix_unsafe_value KEY REASON: asks for a replacement for a setting that cannot be written
# safely. Values typed for secrets are hidden and never echoed or logged.
fix_unsafe_value() {
    local key=$1 reason=$2 choice new mode=
    {
        echo "$key in $CONFIG_FILE cannot be written safely ($reason)."
        echo "  [1] Enter a new value for $key"
        echo "  [c] Cancel (nothing is changed)"
    } >&2
    vs_prompt_choice choice "1"
    if vs_is_secret_key "$key"; then mode=secret; fi
    while :; do
        vs_prompt_input new "$mode"
        if reason=$(env_value_problem "$new"); then
            echo "That value still cannot be written safely ($reason). Try another." >&2
        elif [[ $new =~ [[:cntrl:]] ]]; then
            echo "That value still cannot be written safely (control characters). Try another." >&2
        else
            break
        fi
    done
    config_fix_key "$CONFIG_FILE" "$key" "$new" || {
        echo "Edit $key in $CONFIG_FILE by hand, then run again. Nothing was changed." >&2
        exit 1
    }
}

if [ "$FIX_BOAT_UNITS" -eq 1 ]; then fix_boat_units; fi
if [ -n "$FIX_TOKEN_KEY" ]; then
    fix_unsafe_value "$FIX_TOKEN_KEY" "line breaks or control characters"
fi

# Env files are written to a 0600 temp file beside the target and renamed into
# place only when every line was written, so a failure never leaves a truncated file.
ENV_TEMPS=()
new_env_temp() {
    NEW_ENV_TEMP=$(mktemp "$VESSELSTACK_ROOT/config/.$1.XXXXXX")
    ENV_TEMPS+=("$NEW_ENV_TEMP")
}
cleanup_env_temps() {
    [ "${#ENV_TEMPS[@]}" -eq 0 ] || rm -f -- "${ENV_TEMPS[@]}"
    return 0
}

# Read-only check of every value that will reach vesselstack.env, boat-chat.env
# and control-panel.env, run before anything is changed and before the dry run
# exits. Reports KEY names and reasons only, never values.
env_values_preflight() {
    local key bad=0 hard=0 fixable='' fix_reason='' reason secrets=0 report=''
    while IFS= read -r key; do
        if reason=$(env_value_problem "${!key:-}"); then
            report+="  $key: $reason"$'\n'
            bad=1
            if config_key_is_secret "$key"; then
                secrets=1; hard=1
            elif config_key_assigned "$key"; then
                if [ -z "$fixable" ]; then fixable=$key; fix_reason=$reason; fi
            else
                hard=1
            fi
        fi
    done < <(vesselstack_env_keys; extra_env_keys)
    if reason=$(env_value_problem "$(<"$SCRIPT_DIR/VERSION")"); then
        report+="  VERSION: $reason"$'\n'
        bad=1; hard=1
    fi
    if [ "$bad" -ne 0 ]; then
        # Only settings this file itself assigns, and that are not generated or preserved
        # secrets, can be fixed here; one per round (the installer restarts after each fix).
        if [ "$hard" -eq 0 ] && [ -n "$fixable" ] && vs_prompt_open; then
            fix_unsafe_value "$fixable" "$fix_reason"
        fi
        printf '%s' "$report" >&2
        echo "Refusing to continue: the settings above cannot be written safely to the environment files." >&2
        if [ "$secrets" -eq 1 ]; then
            echo "A password or token that a running service already uses must be changed in that service first (replacing it here alone would lock the service out); then edit it in $CONFIG_FILE." >&2
        fi
        echo "To fix: edit them in $CONFIG_FILE (values cannot combine a single quote with a backslash, double quote, dollar sign or backtick, and cannot span lines), then run again. Nothing was changed. See README.md, \"Upgrading to 0.6.0\"." >&2
        exit 1
    fi
}
generate_secret() {
    local current=$1
    if [ -z "$current" ] || [ "$current" = GENERATE ]; then
        openssl rand -hex 32
    else
        printf '%s' "$current"
    fi
}

# Preserve EVERY generated secret across re-renders. When the input config says
# GENERATE (or leaves a secret empty, e.g. a fresh copy of the example file used
# to upgrade an installed system) the value already installed wins. InfluxDB and
# Grafana only read their initial credentials on first start and Mosquitto's
# password file is keyed to MQTT_PASSWORD, so regenerating any of them would
# silently desynchronise the config from the running services.
installed_env="$VESSELSTACK_ROOT/config/vesselstack.env"
installed_value() {
    # Prints the installed value of $1 from the root-owned, mode-600 config.
    [ -r "$installed_env" ] || return 0
    (
        set +u
        unset "$1"
        # shellcheck disable=SC1090
        source "$installed_env" >/dev/null 2>&1 || exit 0
        value=${!1:-}
        [ "$value" = GENERATE ] || printf '%s' "$value"
    )
}
preserve_secret() {
    # $2 optionally names a fallback app env file. Those files are writable by
    # the service user, so only plain hex-like values are accepted from them and
    # they are never evaluated.
    local key=$1 file=${2:-} current=${!1:-GENERATE} kept=
    if [ -n "$current" ] && [ "$current" != GENERATE ]; then
        return 0
    fi
    kept=$(installed_value "$key")
    if [ -z "$kept" ] && [ -n "$file" ] && [ -r "$file" ]; then
        kept=$(sed -n "s/^${key}=//p" "$file" | tail -n 1)
        [[ "$kept" =~ ^[A-Za-z0-9._-]+$ ]] || kept=
        [ "$kept" != GENERATE ] || kept=
    fi
    if [ -n "$kept" ]; then
        printf -v "$key" '%s' "$kept"
    fi
}
preserve_secret INFLUXDB_PASSWORD
preserve_secret INFLUXDB_TOKEN
preserve_secret GRAFANA_ADMIN_PASSWORD
preserve_secret MQTT_PASSWORD
preserve_secret BOAT_CHAT_SETTINGS_TOKEN "$VESSELSTACK_ROOT/config/boat-chat.env"
preserve_secret BOAT_CHAT_ACCESS_TOKEN "$VESSELSTACK_ROOT/config/boat-chat.env"
preserve_secret CONTROL_PANEL_TOKEN "$VESSELSTACK_ROOT/config/control-panel.env"

INFLUXDB_PASSWORD=$(generate_secret "${INFLUXDB_PASSWORD:-GENERATE}")
INFLUXDB_TOKEN=$(generate_secret "${INFLUXDB_TOKEN:-GENERATE}")
GRAFANA_ADMIN_PASSWORD=$(generate_secret "${GRAFANA_ADMIN_PASSWORD:-GENERATE}")
BOAT_CHAT_SETTINGS_TOKEN=$(generate_secret "${BOAT_CHAT_SETTINGS_TOKEN:-GENERATE}")
BOAT_CHAT_ACCESS_TOKEN=$(generate_secret "${BOAT_CHAT_ACCESS_TOKEN:-GENERATE}")
CONTROL_PANEL_TOKEN=$(generate_secret "${CONTROL_PANEL_TOKEN:-GENERATE}")
MQTT_PASSWORD=$(generate_secret "${MQTT_PASSWORD:-GENERATE}")

# Secrets are resolved (preserved or generated) before the preflight so the
# check covers the values that will actually be written; read-only, so it is
# also safe ahead of the dry run.
env_values_preflight
vs_prompt_close

PLUGIN_STAGE=
if [ "$SIGNALK_VESSELTWIN_PRELOAD" = true ] && [ "$SIGNALK_MODE" != existing ] && [ "$OFFLINE" = false ] && [ "$DRY_RUN" -eq 0 ]; then
    PLUGIN_STAGE=$(mktemp -d "${TMPDIR:-/tmp}/vesselstack-signalk-plugin-stage.XXXXXX")
    chmod 0700 "$PLUGIN_STAGE"
    trap 'rm -rf -- "$PLUGIN_STAGE"; cleanup_env_temps' EXIT
    python3 "$SCRIPT_DIR/scripts/preload-signalk-vesseltwin.py" --mode "$SIGNALK_MODE" --data "$VESSELSTACK_DATA" \
        --stage "$PLUGIN_STAGE" --prepare
fi
# Everything the MQTT access sync would reject is checked here, read-only and
# before anything is changed, so a dry run (and "vesselstackctl update", which
# runs one before taking a backup) fails early instead of partway through.
# Secrets travel in the child environment only. The release's own tool and
# compose template are used because the installed copies may be older.
MOSQUITTO_COMPOSE="$SCRIPT_DIR/templates/compose.yml"
displays_preflight() {
    PYTHONDONTWRITEBYTECODE=1 \
        MQTT_USERNAME="$MQTT_USERNAME" MQTT_PASSWORD="${MQTT_PASSWORD:-}" \
        DISPLAYS_ENABLE="$DISPLAYS_ENABLE" \
        DISPLAYS_BRIDGE_MQTT_USERNAME="$DISPLAYS_BRIDGE_MQTT_USERNAME" \
        DISPLAYS_HA_TOKEN="$DISPLAYS_HA_TOKEN" HOME_ASSISTANT_URL="$HOME_ASSISTANT_URL" \
        python3 "$SCRIPT_DIR/displays/displayctl.py" preflight \
        --root "$VESSELSTACK_ROOT" --compose "$MOSQUITTO_COMPOSE" "$@"
}


if [ "$DRY_RUN" -eq 1 ]; then
    displays_preflight --check-image
    echo "Preflight passed"
    [ "$OFFLINE" = true ] && echo "Offline mode: Python packages from $VESSELSTACK_WHEELHOUSE, no image pulls"
    echo "Would install to $VESSELSTACK_ROOT with data in $VESSELSTACK_DATA"
    echo "SignalK mode: $SIGNALK_MODE ($SIGNALK_URL)"
    exit 0
fi

[ "$(id -u)" -eq 0 ] || { echo "Installation requires root" >&2; exit 1; }

# Remove temp files left in config/ by an earlier run that was killed between
# creating and renaming an env file. Only the exact names new_env_temp creates
# (.vesselstack.env.XXXXXX, .boat-chat.env.XXXXXX, .control-panel.env.XXXXXX with
# six alphanumeric characters); nothing else is touched.
remove_stale_env_temps() {
    local dir="$VESSELSTACK_ROOT/config" name file
    [ -d "$dir" ] || return 0
    for name in vesselstack.env boat-chat.env control-panel.env; do
        for file in "$dir/.$name."??????; do
            if [ ! -f "$file" ] || [ -L "$file" ]; then continue; fi
            [[ ${file##*/."$name".} =~ ^[A-Za-z0-9]{6}$ ]] || continue
            rm -f -- "$file"
        done
    done
}
remove_stale_env_temps
# Same checks as the dry run; a missing Mosquitto image is pulled now, before
# any file is changed, so an offline boat fails here with nothing touched.
displays_preflight --pull-image
install -d -m 0755 "$SYSTEMD_DIR" "$SBIN_DIR"

# Transactional ordering: the network/package-dependent step runs FIRST, into a
# staging virtualenv. Only when it succeeds is anything else rewritten, and the
# staged venv is swapped in later. A failed (or offline) run therefore changes
# nothing: no env, units, code or installed-version are touched.
install -d -m 0755 "$VESSELSTACK_ROOT"
STAGING_DIR=$(mktemp -d "$VESSELSTACK_ROOT/.venv-staging.XXXXXX")
trap 'rm -rf -- "$STAGING_DIR" ${PLUGIN_STAGE:+"$PLUGIN_STAGE"}; cleanup_env_temps' EXIT
STAGING_VENV="$STAGING_DIR/venv"
pip_args=(install --disable-pip-version-check --only-binary=:all: --require-hashes)
if [ "$OFFLINE" = true ]; then
    pip_args+=(--no-index --find-links "$VESSELSTACK_WHEELHOUSE")
fi
if ! python3 -m venv "$STAGING_VENV" \
    || ! "$STAGING_VENV/bin/pip" "${pip_args[@]}" -r "$PIP_LOCK"; then
    echo "ERROR: could not build the Python environment$([ "$OFFLINE" = true ] && echo " from $VESSELSTACK_WHEELHOUSE" || echo " (is the network reachable, and is there a wheel in boat-chat/requirements.lock for this platform? use --offline with a prepared bundle)")." >&2
    echo "Nothing was changed on this system." >&2
    exit 1
fi

# Only boats that opt in need the extra network download. It runs before
# anything is rendered, so an offline boat fails here with the installation
# untouched. The environment is root-owned but readable by the service user
# (see the helper for why this cannot rely on this script's umask 077).
if [ "$DISPLAYS_ENABLE" = true ]; then
    install -d -m 0755 "$VESSELSTACK_ROOT" "$VESSELSTACK_ROOT/displays"
    "$SCRIPT_DIR/scripts/install-displays-venv.sh" \
        "$VESSELSTACK_ROOT/displays/.venv" "$SCRIPT_DIR/displays/requirements.txt"
fi

# Ownership model for VESSELSTACK_DATA: the directory itself and every managed
# intermediate component is ROOT-owned and not writable by the service user, so the
# service user can never rename, replace or symlink an entry that root later
# creates, chowns or writes through (no check-then-use race). Only the leaf mount
# roots are owned by the account that must write them (InfluxDB uid 1000, Grafana
# uid 472, the service user for boat-chat and native SignalK state).
fail_data_path() {
    echo "ERROR: $1" >&2
    exit 1
}
safe_data_path() {
    # $1: path under VESSELSTACK_DATA. Every existing component must be a real
    # directory/file (never a symlink) and, except an optional leaf, root-owned and
    # not group/world-writable. Mode "leaf" lets the final component exist with any
    # owner (a service-owned mount root) but still never as a symlink.
    local path=$1 mode=${2:-strict} current=$VESSELSTACK_DATA part owner perms index=0 count
    local rest=${path#"$VESSELSTACK_DATA"}
    local -a parts
    IFS=/ read -ra parts <<<"${rest#/}"
    count=${#parts[@]}
    for part in "${parts[@]}"; do
        index=$((index + 1))
        [ -n "$part" ] || continue
        current=$current/$part
        [ -e "$current" ] || [ -L "$current" ] || return 0
        [ ! -L "$current" ] || fail_data_path "$current is a symlink inside the data directory; remove it and rerun"
        if [ "$index" -eq "$count" ] && [ "$mode" = leaf ]; then
            continue
        fi
        owner=$(stat -c %u "$current")
        perms=$(stat -c %a "$current")
        if [ "$owner" != 0 ] || [ $((8#$perms & 8#022)) -ne 0 ]; then
            fail_data_path "$current must be owned by root and not group/world-writable (found uid $owner mode $perms); fix with: chown root:root '$current' && chmod go-w '$current'"
        fi
    done
}
install -d -m 0755 "$VESSELSTACK_ROOT" "$VESSELSTACK_ROOT/config" \
    "$VESSELSTACK_ROOT/boat-chat" "$VESSELSTACK_ROOT/control-panel" \
    "$VESSELSTACK_ROOT/displays" "$VESSELSTACK_ROOT/installer"
install -d -m 0755 "$RELEASES_DIR"
install -d -m 0755 "$VESSELSTACK_ROOT/systemd"
[ ! -L "$VESSELSTACK_DATA" ] || fail_data_path "$VESSELSTACK_DATA is a symlink"
# Take the data root away from the service user FIRST (upgrades from releases where
# it was service-owned): it stays traversable by the service group but is not
# writable. Entries planted before this point are rejected by the checks below.
install -d -m 0750 -o root -g "$VESSELSTACK_GID" "$VESSELSTACK_DATA"
for managed in influxdb grafana prometheus boat-chat signalk mosquitto; do
    safe_data_path "$VESSELSTACK_DATA/$managed" leaf
done
# Official images run their data processes as these fixed container users.
# Change only the mount root itself; never recursively rewrite existing data.
install -d -m 0750 -o 1000 -g 1000 "$VESSELSTACK_DATA/influxdb"
install -d -m 0750 -o 472 -g 0 "$VESSELSTACK_DATA/grafana"
# The signalk-server image runs SignalK as its "node" user (uid/gid 1000, config
# dir /home/node/.signalk). Without a pre-created, correctly owned source directory
# Docker creates the bind mount as root:root and SignalK exits with
# "EACCES: permission denied, open '/home/node/.signalk/package.json'".
# Native mode keeps its own service-user-owned directory (install-signalk-native.sh).
SIGNALK_CONTAINER_UID=1000
SIGNALK_CONTAINER_GID=1000
ensure_container_data_dir() {
    # $1 dir, $2 uid, $3 gid, $4 label used in messages. Creates the directory, or
    # repairs an existing wrongly-owned one only under the conservative rules below.
    local dir=$1 uid=$2 gid=$3 label=$4 want="$2:$3" have mode
    safe_data_path "$dir" leaf
    if [ ! -e "$dir" ]; then
        install -d -m 0750 -o "$uid" -g "$gid" "$dir"
        return 0
    fi
    [ -d "$dir" ] || fail_data_path "$dir exists but is not a directory"
    have=$(stat -c %u:%g "$dir")
    [ "$have" != "$want" ] || return 0
    if [ -z "$(find "$dir" -mindepth 1 -print -quit)" ]; then
        chown -h "$want" "$dir"
        echo "Repaired $label data directory ownership: $dir $have -> $want (empty directory)"
        return 0
    fi
    # Populated. The only state repaired automatically is the known one: Docker created
    # the bind-mount source as root:root. A directory owned by anyone else may still be
    # written by that owner while a root-run chown walks it (symlink and hard-link
    # swaps), so it is left untouched.
    if [ "$have" != 0:0 ]; then
        fail_data_path "$dir is not empty and is owned by $have (neither root:root nor $want); making no change. Stop whatever uses it, review the contents, then run: chown -R -h $want '$dir' and rerun"
    fi
    # Lock the top directory first (root-only, no traversal) so no other user can swap
    # entries during the walk, then refuse anything a chown could be tricked through.
    mode=$(stat -c %a "$dir")
    chmod 0700 "$dir"
    local problem=''
    if [ -n "$(find -P "$dir" -xdev -mindepth 1 -type l -print -quit)" ]; then
        problem="contains symlinks (they may be legitimate, e.g. node_modules/.bin)"
    elif [ -n "$(find -P "$dir" -xdev -mindepth 1 -type f -links +1 -print -quit)" ]; then
        problem="contains files with more than one hard link"
    elif [ -n "$(find -P "$dir" -xdev -mindepth 1 -not -type f -not -type d -print -quit)" ]; then
        problem="contains special files (devices, sockets or pipes)"
    fi
    if [ -n "$problem" ]; then
        chmod "$mode" "$dir"
        fail_data_path "$dir $problem; refusing to change ownership. Review it, then run: chown -R -h $want '$dir' and rerun"
    fi
    find -P "$dir" -xdev -mindepth 1 -exec chown -h "$want" {} +
    chown -h "$want" "$dir"
    chmod "$mode" "$dir"
    echo "Repaired $label data directory ownership: $dir $have -> $want (recursive, locked during the change; no symlinks, hard links or special files found)"
}
ensure_signalk_docker_dir() {
    ensure_container_data_dir "$VESSELSTACK_DATA/signalk" "$SIGNALK_CONTAINER_UID" "$SIGNALK_CONTAINER_GID" SignalK
}
if [ "$SIGNALK_MODE" = docker ]; then
    ensure_signalk_docker_dir
fi
# The prom/prometheus image runs as nobody (65534:65534) and keeps its TSDB in
# /prometheus. Bind-mounting it here keeps metrics across container re-creation and
# puts them inside the backed-up data directory. Created before compose up.
PROMETHEUS_CONTAINER_UID=65534
PROMETHEUS_CONTAINER_GID=65534
ensure_container_data_dir "$VESSELSTACK_DATA/prometheus" "$PROMETHEUS_CONTAINER_UID" "$PROMETHEUS_CONTAINER_GID" Prometheus
# Code and the Python virtualenv are root-owned; the service user may only write
# under the state directory inside VESSELSTACK_DATA (see systemd ReadWritePaths).
BOAT_CHAT_STATE="$VESSELSTACK_DATA/boat-chat"
install -d -m 0750 -o "$VESSELSTACK_USER" -g "$VESSELSTACK_USER" "$BOAT_CHAT_STATE"
# Quarantined legacy state lives OUTSIDE every service-writable tree.
QUARANTINE_ROOT="$SYSTEM_ROOT/var/lib/vesselstack/quarantine"
ensure_quarantine_root() {
    [ ! -L "$QUARANTINE_ROOT" ] || fail_data_path "$QUARANTINE_ROOT is a symlink"
    install -d -m 0700 -o root -g root "$QUARANTINE_ROOT"
    python3 - "$QUARANTINE_ROOT" <<'PY' || fail_data_path "$QUARANTINE_ROOT must be a root-owned directory with mode 0700 (not a symlink)"
import os
import stat
import sys

info = os.lstat(sys.argv[1])
if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
    raise SystemExit(1)
PY
}
LEGACY_QUARANTINE=
quarantine_legacy() {
    # Legacy runtime state that cannot be migrated is moved (never deleted) into a
    # root-only directory outside the code tree and outside the service-writable
    # data tree, so nothing sensitive stays readable or replaceable by the service user.
    local old=$1
    if [ -z "$LEGACY_QUARANTINE" ]; then
        ensure_quarantine_root
        LEGACY_QUARANTINE=$(mktemp -d "$QUARANTINE_ROOT/boat-chat-legacy-$(date -u +%Y%m%dT%H%M%SZ).XXXXXX")
        chmod 0700 "$LEGACY_QUARANTINE"
    fi
    mv -- "$old" "$LEGACY_QUARANTINE/"
    echo "WARNING: moved legacy $old to $LEGACY_QUARANTINE (root-only); review and merge manually, then remove it" >&2
}
# Earlier 0.4.0 builds created quarantine directories inside the data tree.
for earlier_quarantine in "$VESSELSTACK_DATA"/boat-chat-legacy-*; do
    if [ -d "$earlier_quarantine" ] && [ ! -L "$earlier_quarantine" ]; then
        ensure_quarantine_root
        mv -- "$earlier_quarantine" "$QUARANTINE_ROOT/"
        echo "WARNING: moved earlier quarantine $earlier_quarantine to $QUARANTINE_ROOT/ (it sat in a service-writable tree; treat its contents as untrusted)" >&2
    fi
done
migrate_runtime_path() {
    local name=$1 old="$VESSELSTACK_ROOT/boat-chat/$1" new="$BOAT_CHAT_STATE/$1" stage
    if [ -L "$old" ]; then
        rm -f -- "$old"
        return 0
    fi
    [ -e "$old" ] || return 0
    if [ -e "$new" ] || [ -L "$new" ]; then
        quarantine_legacy "$old"
        return 0
    fi
    # The state directory is writable by the service user, so copy and chown in a
    # root-only staging directory and publish with rename(2), which never follows a
    # symlink planted at the destination.
    stage=$(mktemp -d "$VESSELSTACK_DATA/.migrate.XXXXXX")
    cp -a -- "$old" "$stage/$name"
    chown -hR "$VESSELSTACK_USER:$VESSELSTACK_USER" "$stage/$name"
    if mv -T -- "$stage/$name" "$new" 2>/dev/null; then
        rm -rf -- "$stage" "$old"
    else
        rm -rf -- "$stage"
        quarantine_legacy "$old"
    fi
}
stop_telemetry_for_migration() {
    local unit
    for unit in vesselstack-telemetry-sample.timer vesselstack-telemetry-indexer.timer \
        vesselstack-telemetry-sample.service vesselstack-telemetry-indexer.service; do
        if systemctl show -p LoadState --value "$unit" 2>/dev/null | grep -qx not-found; then
            continue
        fi
        systemctl stop "$unit"
    done
}
telemetry_sample_was_active=0
telemetry_indexer_was_active=0
if systemctl is-active --quiet vesselstack-telemetry-sample.timer; then telemetry_sample_was_active=1; fi
if systemctl is-active --quiet vesselstack-telemetry-indexer.timer; then telemetry_indexer_was_active=1; fi
restore_telemetry_on_exit() {
    local rc=$?
    if ! rm -rf -- "$STAGING_DIR" ${PLUGIN_STAGE:+"$PLUGIN_STAGE"}; then
        echo "Installer staging cleanup failed" >&2
        if [ "$rc" -eq 0 ]; then rc=1; fi
    fi
    cleanup_env_temps
    if [ "$rc" -ne 0 ] || [ "$START" -eq 0 ]; then
        if [ "$telemetry_sample_was_active" -eq 1 ]; then systemctl start vesselstack-telemetry-sample.timer 2>/dev/null || true; fi
        if [ "$telemetry_indexer_was_active" -eq 1 ]; then systemctl start vesselstack-telemetry-indexer.timer 2>/dev/null || true; fi
    fi
    return "$rc"
}
trap restore_telemetry_on_exit EXIT
if [ -e "$VESSELSTACK_ROOT/boat-chat/memory" ] || [ -e "$VESSELSTACK_ROOT/boat-chat/data" ] \
    || [ -e "$VESSELSTACK_ROOT/boat-chat/boat-chat.env" ] \
    || [ -L "$VESSELSTACK_ROOT/boat-chat/memory" ] || [ -L "$VESSELSTACK_ROOT/boat-chat/data" ]; then
    # Quiesce writers so SQLite files are not copied mid-write.
    systemctl stop vesselstack-chat.service vesselstack-chat-telegram.service
    stop_telemetry_for_migration
    for runtime_name in memory data boat-chat.env; do
        migrate_runtime_path "$runtime_name"
    done
fi
# Quiesce telemetry writers before replacing application files, even when no
# legacy runtime migration is needed.
stop_telemetry_for_migration
tar -C "$SOURCE_ROOT/boat-chat" \
    --exclude=boat-chat.env --exclude=.venv --exclude=memory --exclude=data \
    --exclude='__pycache__' --exclude='*.pyc' -cf - . \
    | tar --no-same-owner -C "$VESSELSTACK_ROOT/boat-chat" -xf -
# Discard any previously service-writable virtualenv, swap in the staged one
# (built above, root-owned) and take ownership of all application code before
# root ever executes anything from it. Same filesystem, so the move is a rename;
# scripts that embedded the staging path are rewritten to the final path.
rm -rf -- "$VESSELSTACK_ROOT/boat-chat/.venv"
mv -- "$STAGING_VENV" "$VESSELSTACK_ROOT/boat-chat/.venv"
grep -rlIF -- "$STAGING_VENV" "$VESSELSTACK_ROOT/boat-chat/.venv" 2>/dev/null \
    | while IFS= read -r relocated; do
        sed -i "s|$STAGING_VENV|$VESSELSTACK_ROOT/boat-chat/.venv|g" "$relocated"
    done || true
chown -hR root:root "$VESSELSTACK_ROOT/boat-chat"
# The service user must be able to read and execute the venv but never write it.
chmod -R go-w,go+rX "$VESSELSTACK_ROOT/boat-chat"
tar -C "$SOURCE_ROOT/control-panel" --exclude='__pycache__' --exclude='*.pyc' -cf - . \
    | tar --no-same-owner -C "$VESSELSTACK_ROOT/control-panel" -xf -
tar -C "$SOURCE_ROOT/displays" --exclude=.venv --exclude='__pycache__' --exclude='*.pyc' -cf - . \
    | tar --no-same-owner -C "$VESSELSTACK_ROOT/displays" -xf -
# GNU tar run as root keeps the archive's modes, which are only as readable as the
# source checkout (often 0600/0700 under umask 077). The bridge runs as the
# service user and needs read access; the tree holds no secrets and stays root-owned.
chmod -R u=rwX,go=rX "$VESSELSTACK_ROOT/displays"
# Keep a bundled, reviewed installer so the control panel can run preflight and
# apply operations without depending on a source checkout that may be removed.
if [ "$SOURCE_ROOT" != "$VESSELSTACK_ROOT/installer" ]; then
    tar -C "$SOURCE_ROOT" --exclude=.venv --exclude='__pycache__' --exclude='*.pyc' \
        -cf - install.sh VERSION scripts systemd templates boat-chat control-panel displays \
        | tar --no-same-owner -C "$VESSELSTACK_ROOT/installer" -xf -
fi
# The source application keeps legacy defaults for compatibility. Installed
# branding is vessel-specific; the generated facts file remains authoritative.
sed -i "s/VesselStack/$BOAT_NAME/g" \
    "$VESSELSTACK_ROOT/boat-chat/static/index.html" \
    "$VESSELSTACK_ROOT/boat-chat/BOAT_CHAT_AGENT.md" \
    "$VESSELSTACK_ROOT/boat-chat/telegram_bot.py"
# Render the always-on Mosquitto ACL and hashed passwords. Secrets travel in the
# child environment only, never on a command line. The release's compose template
# names the image because the installed compose.yml is still the previous one.
sync_mqtt_access() {
    PYTHONDONTWRITEBYTECODE=1 \
        MQTT_USERNAME="$MQTT_USERNAME" MQTT_PASSWORD="$MQTT_PASSWORD" \
        DISPLAYS_ENABLE="$DISPLAYS_ENABLE" \
        DISPLAYS_BRIDGE_MQTT_USERNAME="$DISPLAYS_BRIDGE_MQTT_USERNAME" \
        python3 "$VESSELSTACK_ROOT/displays/displayctl.py" sync-mqtt \
        --root "$VESSELSTACK_ROOT" --data "$VESSELSTACK_DATA" \
        --compose "$MOSQUITTO_COMPOSE"
}
# Displays are opt-in. Seed an instruments-only displays.json once (no display
# entries, so no display account is created implicitly) and never overwrite an
# existing one. It holds no secrets and must be readable by the service user.
DISPLAYS_JSON="$VESSELSTACK_ROOT/config/displays.json"
if [ "$DISPLAYS_ENABLE" = true ]; then
    if [ ! -e "$DISPLAYS_JSON" ] && [ ! -L "$DISPLAYS_JSON" ]; then
        displays_seed=$(mktemp)
        jq '.displays = {}' "$VESSELSTACK_ROOT/displays/displays.example.json" > "$displays_seed"
        install -m 0644 "$displays_seed" "$DISPLAYS_JSON"
        rm -f -- "$displays_seed"
        echo "Seeded $DISPLAYS_JSON with the example instruments and no displays"
    elif [ -f "$DISPLAYS_JSON" ] && [ ! -L "$DISPLAYS_JSON" ]; then
        chmod 0644 "$DISPLAYS_JSON"
    fi
fi
# Mosquitto reads its configuration from the DIRECTORY config/mosquitto (mounted
# as a directory, so a replaced file reaches the broker). Refuse a planted
# symlink before anything is written.
MOSQUITTO_CONF_DIR="$VESSELSTACK_ROOT/config/mosquitto"
MOSQUITTO_CONF="$MOSQUITTO_CONF_DIR/mosquitto.conf"
MOSQUITTO_CONF_STAMP="$VESSELSTACK_ROOT/config/.mosquitto-conf.sha256"
if [ -L "$MOSQUITTO_CONF_DIR" ] || [ -L "$MOSQUITTO_CONF" ]; then
    echo "Refusing to use $MOSQUITTO_CONF_DIR: it contains a symbolic link" >&2
    exit 1
fi
if [ -e "$MOSQUITTO_CONF_DIR" ] && [ ! -d "$MOSQUITTO_CONF_DIR" ]; then
    echo "Refusing to use $MOSQUITTO_CONF_DIR: it is not a directory" >&2
    exit 1
fi
# The auth files live in the root-owned config/mosquitto-auth directory (mounted
# read-only into the broker), so syncing is safe on every run, including
# render-only runs such as Control Panel apply. It runs BEFORE the new compose
# definition and Mosquitto configuration are installed, so a failed sync aborts
# with the previous broker configuration still in place.
sync_mqtt_access
install -m 0644 "$SCRIPT_DIR/templates/compose.yml" "$VESSELSTACK_ROOT/compose.yml"
if [ "$NODE_EXPORTER_ENABLE" = true ]; then
    install -m 0644 "$SCRIPT_DIR/templates/prometheus-node-exporter.yml" "$VESSELSTACK_ROOT/config/prometheus.yml"
else
    install -m 0644 "$SCRIPT_DIR/templates/prometheus.yml" "$VESSELSTACK_ROOT/config/prometheus.yml"
fi
PROMETHEUS_CONF="$VESSELSTACK_ROOT/config/prometheus.yml"
PROMETHEUS_CONF_STAMP="$VESSELSTACK_ROOT/config/.prometheus-conf.sha256"
PROMETHEUS_CONF_HASH=$(sha256sum "$PROMETHEUS_CONF" | cut -d' ' -f1)
write_prometheus_stamp() {
    local tmp
    tmp=$(mktemp "${PROMETHEUS_CONF_STAMP}.tmp.XXXXXX") || return 1
    if ! printf '%s\n' "$PROMETHEUS_CONF_HASH" > "$tmp" || ! chmod 0644 "$tmp" || ! mv -f -- "$tmp" "$PROMETHEUS_CONF_STAMP"; then
        rm -f -- "$tmp"
        return 1
    fi
}
# The legacy single-file config/mosquitto.conf is deliberately left alone: a
# container created by an older release keeps bind-mounting it, also across
# reboots, until it is recreated; a downgrade finds it unchanged.
install -d -m 0755 -o 0 -g 0 "$MOSQUITTO_CONF_DIR"
install -m 0644 -o 0 -g 0 "$SCRIPT_DIR/templates/mosquitto.conf" "$MOSQUITTO_CONF"
MOSQUITTO_CONF_HASH=$(sha256sum "$MOSQUITTO_CONF" | cut -d' ' -f1)
# The bridge environment file holds the bridge MQTT password generated by the
# sync above, so it is written by displayctl (never via argv) after that sync.
if [ "$DISPLAYS_ENABLE" = true ]; then
    PYTHONDONTWRITEBYTECODE=1 \
        SIGNALK_URL="$SIGNALK_URL" MQTT_PORT="$MQTT_PORT" \
        DISPLAYS_BRIDGE_MQTT_USERNAME="$DISPLAYS_BRIDGE_MQTT_USERNAME" \
        DISPLAYS_SIGNALK_TOKEN="$DISPLAYS_SIGNALK_TOKEN" BOAT_TIMEZONE="$BOAT_TIMEZONE" \
        DISPLAYS_HA_TOKEN="$DISPLAYS_HA_TOKEN" HOME_ASSISTANT_URL="$HOME_ASSISTANT_URL" \
        python3 "$VESSELSTACK_ROOT/displays/displayctl.py" bridge-env \
        --root "$VESSELSTACK_ROOT" \
        --output "$VESSELSTACK_ROOT/config/displays-bridge.env" \
        >/dev/null
else
    rm -f -- "$VESSELSTACK_ROOT/config/displays-bridge.env"
fi
install -d -m 0755 "$VESSELSTACK_ROOT/config/grafana/provisioning/datasources" \
    "$VESSELSTACK_ROOT/config/grafana/provisioning/dashboards" \
    "$VESSELSTACK_ROOT/config/grafana/dashboards"
install -m 0644 "$SCRIPT_DIR/templates/grafana/provisioning/datasources/vesselstack.yml" \
    "$VESSELSTACK_ROOT/config/grafana/provisioning/datasources/vesselstack.yml"
install -m 0644 "$SCRIPT_DIR/templates/grafana/provisioning/dashboards/vesselstack.yml" \
    "$VESSELSTACK_ROOT/config/grafana/provisioning/dashboards/vesselstack.yml"
install -m 0644 "$SCRIPT_DIR/templates/grafana/dashboards/system-health.json" \
    "$VESSELSTACK_ROOT/config/grafana/dashboards/system-health.json"
safe_data_path "$VESSELSTACK_DATA/homeassistant/blueprints/automation/vesselstack"
safe_data_path "$VESSELSTACK_DATA/homeassistant/vesselstack-dashboard.example.yaml" leaf
install -d -m 0755 "$VESSELSTACK_DATA/homeassistant/blueprints/automation/vesselstack"
for blueprint in "$SCRIPT_DIR"/templates/homeassistant/blueprints/automation/vesselstack/*.yaml; do
    install -m 0644 "$blueprint" \
        "$VESSELSTACK_DATA/homeassistant/blueprints/automation/vesselstack/$(basename "$blueprint")"
done
install -m 0644 "$SCRIPT_DIR/templates/homeassistant/lovelace-vesselstack.example.yaml" \
    "$VESSELSTACK_DATA/homeassistant/vesselstack-dashboard.example.yaml"
# The installed copy defaults to this installation's root, so a custom
# VESSELSTACK_ROOT works without exporting it (the environment still overrides).
if [[ $VESSELSTACK_ROOT =~ ^[A-Za-z0-9_./+-]+$ ]]; then
    sed "s|^ROOT=\${VESSELSTACK_ROOT:-/opt/vesselstack}\$|ROOT=\${VESSELSTACK_ROOT:-$VESSELSTACK_ROOT}|" \
        "$SCRIPT_DIR/scripts/vesselstackctl" > "$SBIN_DIR/vesselstackctl"
else
    echo "WARN: VESSELSTACK_ROOT contains unusual characters; export VESSELSTACK_ROOT when running vesselstackctl" >&2
    cp "$SCRIPT_DIR/scripts/vesselstackctl" "$SBIN_DIR/vesselstackctl"
fi
chmod 0755 "$SBIN_DIR/vesselstackctl"
install -m 0755 "$SCRIPT_DIR/scripts/vesselstack-firewall" "$SBIN_DIR/vesselstack-firewall"
# The update launcher and the helper it runs as root are installed root-owned and
# never executed from a source checkout. A new helper version only arrives through
# this installer (i.e. from a reviewed/verified release tree).
install -d -m 0755 -o root -g root "$LIBEXEC_PARENT" "$LIBEXEC_DIR"
install -m 0755 -o root -g root "$SCRIPT_DIR/scripts/vesselstack-update" "$SBIN_DIR/vesselstack-update"
install -m 0755 -o root -g root "$SCRIPT_DIR/scripts/deploy-chat.py" "$LIBEXEC_DIR/deploy-chat.py"
install -m 0644 -o root -g root "$SCRIPT_DIR/scripts/lib/remediate.sh" "$LIBEXEC_DIR/remediate.sh"
sed "s|@VESSELSTACK_ROOT@|$VESSELSTACK_ROOT|g" \
    "$SCRIPT_DIR/systemd/vesselstack-firewall.service.in" \
    > "$SYSTEMD_DIR/vesselstack-firewall.service"
chmod 0644 "$SYSTEMD_DIR/vesselstack-firewall.service"
install -m 0644 "$SCRIPT_DIR/systemd/vesselstack-signalk.service.in" \
    "$VESSELSTACK_ROOT/systemd/vesselstack-signalk.service.in"
install -m 0644 "$SCRIPT_DIR/systemd/vesselstack-socketcan.service.in" \
    "$VESSELSTACK_ROOT/systemd/vesselstack-socketcan.service.in"

new_env_temp vesselstack.env; vs_env_tmp=$NEW_ENV_TEMP
while IFS= read -r key; do
    env_line "$key" "${!key:-}" >> "$vs_env_tmp"
done < <(vesselstack_env_keys)
mv -f -- "$vs_env_tmp" "$VESSELSTACK_ROOT/config/vesselstack.env"

# Retain provider credentials and optional Boat Chat settings across installer
# re-renders while replacing the integration values owned by VesselStack.
new_env_temp boat-chat.env; boat_chat_temp=$NEW_ENV_TEMP
if [ -r "$VESSELSTACK_ROOT/config/boat-chat.env" ]; then
    cp "$VESSELSTACK_ROOT/config/boat-chat.env" "$boat_chat_temp"
fi
for key in BOAT_NAME BOAT_TIMEZONE VESSELSTACK_VERSION BOAT_CHAT_SETTINGS_TOKEN BOAT_CHAT_ACCESS_TOKEN \
    TELEGRAM_ENABLE TELEMETRY_INDEXER_ENABLE BOAT_UNITS \
    SIGNALK_URL HOME_ASSISTANT_URL \
    HOME_ASSISTANT_TOKEN INFLUXDB_URL INFLUXDB_ORG INFLUXDB_TOKEN \
    INFLUXDB_RAW_BUCKET INFLUXDB_HISTORY_BUCKET \
    INFLUXDB_HOME_ASSISTANT_BUCKET INFLUXDB_AIS_BUCKET; do
    sed -i "/^${key}=/d" "$boat_chat_temp"
done
for key in BOAT_CHAT_PROVIDER BOAT_CHAT_HOST BOAT_CHAT_PORT; do
    key_given=${!key+x}
    [ "$key" != BOAT_CHAT_HOST ] || key_given=$BOAT_CHAT_HOST_FROM_CONFIG
    if [ -n "$key_given" ]; then
        sed -i "/^${key}=/d" "$boat_chat_temp"
        env_line "$key" "${!key}" >> "$boat_chat_temp"
    elif ! grep -q "^${key}=" "$boat_chat_temp"; then
        case "$key" in
            BOAT_CHAT_PROVIDER) env_line "$key" local >> "$boat_chat_temp" ;;
            BOAT_CHAT_HOST) env_line "$key" 0.0.0.0 >> "$boat_chat_temp" ;;
            BOAT_CHAT_PORT) env_line "$key" 8765 >> "$boat_chat_temp" ;;
        esac
    fi
done
{
    env_line BOAT_NAME "$BOAT_NAME"
    env_line BOAT_TIMEZONE "$BOAT_TIMEZONE"
    env_line VESSELSTACK_VERSION "$(<"$SCRIPT_DIR/VERSION")"
    env_line BOAT_CHAT_SETTINGS_TOKEN "$BOAT_CHAT_SETTINGS_TOKEN"
    env_line BOAT_CHAT_ACCESS_TOKEN "$BOAT_CHAT_ACCESS_TOKEN"
    env_line TELEGRAM_ENABLE "$TELEGRAM_ENABLE"
    env_line TELEMETRY_INDEXER_ENABLE "$TELEMETRY_INDEXER_ENABLE"
    env_line BOAT_UNITS "$BOAT_UNITS"
    env_line SIGNALK_URL "$SIGNALK_URL"
    env_line HOME_ASSISTANT_URL "$HOME_ASSISTANT_URL"
    env_line HOME_ASSISTANT_TOKEN "${HOME_ASSISTANT_TOKEN:-}"
    env_line INFLUXDB_URL "$INFLUXDB_URL"
    # shellcheck disable=SC2153  # Required and validated through the key list.
    env_line INFLUXDB_ORG "$INFLUXDB_ORG"
    env_line INFLUXDB_TOKEN "$INFLUXDB_TOKEN"
    env_line INFLUXDB_RAW_BUCKET "$INFLUXDB_RAW_BUCKET"
    env_line INFLUXDB_HISTORY_BUCKET "$INFLUXDB_HISTORY_BUCKET"
    env_line INFLUXDB_HOME_ASSISTANT_BUCKET "$INFLUXDB_HOME_ASSISTANT_BUCKET"
    env_line INFLUXDB_AIS_BUCKET "$INFLUXDB_AIS_BUCKET"
} >> "$boat_chat_temp"
chown "$VESSELSTACK_USER:$VESSELSTACK_USER" "$boat_chat_temp"
mv -f -- "$boat_chat_temp" "$VESSELSTACK_ROOT/config/boat-chat.env"

new_env_temp control-panel.env; control_panel_temp=$NEW_ENV_TEMP
{
    env_line CONTROL_PANEL_HOST "$CONTROL_PANEL_HOST"
    env_line CONTROL_PANEL_PORT "$CONTROL_PANEL_PORT"
    env_line CONTROL_PANEL_TOKEN "$CONTROL_PANEL_TOKEN"
} >> "$control_panel_temp"
mv -f -- "$control_panel_temp" "$VESSELSTACK_ROOT/config/control-panel.env"

# Vessel identity is generated into a root-owned temporary file in the (root-owned)
# application directory and renamed into place; the service user can only read it.
facts_temp=$(mktemp "$VESSELSTACK_ROOT/boat-chat/.boat_facts.XXXXXX")
jq -n \
    --arg name "$BOAT_NAME" --arg type "${BOAT_TYPE:-}" \
    --arg mmsi "${BOAT_MMSI:-}" --arg callsign "${BOAT_CALLSIGN:-}" \
    --arg loa "${BOAT_LOA_M:-}" --arg beam "${BOAT_BEAM_M:-}" \
    '{vessel:{name:$name,type:$type,mmsi:$mmsi,callsign:$callsign,loa_m:$loa,beam_m:$beam}}' \
    > "$facts_temp"
chown "root:$VESSELSTACK_USER" "$facts_temp"
chmod 0640 "$facts_temp"
mv -f -- "$facts_temp" "$VESSELSTACK_ROOT/boat-chat/boat_facts.json"

sed -e "s|@VESSELSTACK_USER@|$VESSELSTACK_USER|g" \
    -e "s|@VESSELSTACK_ROOT@|$VESSELSTACK_ROOT|g" \
    -e "s|@VESSELSTACK_DATA@|$VESSELSTACK_DATA|g" \
    "$SCRIPT_DIR/systemd/vesselstack-chat.service.in" > "$SYSTEMD_DIR/vesselstack-chat.service"
systemctl daemon-reload
systemctl enable vesselstack-chat.service
for unit in vesselstack-chat-telegram.service vesselstack-telemetry-indexer.service vesselstack-telemetry-sample.service \
    vesselstack-displays-bridge.service; do
    sed -e "s|@VESSELSTACK_USER@|$VESSELSTACK_USER|g" \
        -e "s|@VESSELSTACK_ROOT@|$VESSELSTACK_ROOT|g" \
        -e "s|@VESSELSTACK_DATA@|$VESSELSTACK_DATA|g" \
        "$SCRIPT_DIR/systemd/$unit.in" > "$SYSTEMD_DIR/$unit"
    chmod 0644 "$SYSTEMD_DIR/$unit"
done
sed -e "s|@VESSELSTACK_USER@|$VESSELSTACK_USER|g" \
    -e "s|@VESSELSTACK_ROOT@|$VESSELSTACK_ROOT|g" \
    -e "s|@VESSELSTACK_DATA@|$VESSELSTACK_DATA|g" \
    "$SCRIPT_DIR/systemd/vesselstack-telemetry-sample.timer.in" > "$SYSTEMD_DIR/vesselstack-telemetry-sample.timer"
chmod 0644 "$SYSTEMD_DIR/vesselstack-telemetry-sample.timer"
install -m 0644 "$SCRIPT_DIR/systemd/vesselstack-telemetry-indexer.timer" \
    "$SYSTEMD_DIR/vesselstack-telemetry-indexer.timer"
sed "s|@VESSELSTACK_ROOT@|$VESSELSTACK_ROOT|g" \
    "$SCRIPT_DIR/systemd/vesselstack-control-panel.service.in" \
    > "$SYSTEMD_DIR/vesselstack-control-panel.service"
chmod 0644 "$SYSTEMD_DIR/vesselstack-control-panel.service"
systemctl daemon-reload
systemctl enable vesselstack-control-panel.service
if [ "$TELEGRAM_ENABLE" = true ]; then
    systemctl enable vesselstack-chat-telegram.service
else
    systemctl disable vesselstack-chat-telegram.service 2>/dev/null || true
fi
if [ "$TELEMETRY_INDEXER_ENABLE" = true ]; then
    systemctl enable vesselstack-telemetry-indexer.timer
    systemctl enable vesselstack-telemetry-sample.timer
else
    systemctl disable vesselstack-telemetry-indexer.timer 2>/dev/null || true
    systemctl disable vesselstack-telemetry-sample.timer 2>/dev/null || true
fi
if [ "$DISPLAYS_ENABLE" = true ]; then
    systemctl enable vesselstack-displays-bridge.service
    # The environment file was just rewritten; restart a running bridge so it
    # takes the new settings (a stopped one is started with the stack). Harmless
    # on a render-only run.
    [ "$START" -eq 1 ] || systemctl try-restart vesselstack-displays-bridge.service
else
    systemctl disable --now vesselstack-displays-bridge.service 2>/dev/null || true
fi

compose config --quiet

# Everything rendered and validated: record the new schema version last.
"$SCRIPT_DIR/scripts/migrate.sh" "$VESSELSTACK_ROOT/config/vesselstack.env" \
    "$(<"$SCRIPT_DIR/VERSION")"

if [ "$SIGNALK_MODE" = native ]; then
    "$SCRIPT_DIR/scripts/install-signalk-native.sh" "$VESSELSTACK_ROOT/config/vesselstack.env"
    SIGNALK_CONTAINER_UID=$VESSELSTACK_UID
    SIGNALK_CONTAINER_GID=$VESSELSTACK_GID
fi
SIGNALK_PLUGIN_INSTALLED=0
if [ "$SIGNALK_VESSELTWIN_PRELOAD" = true ] && [ "$SIGNALK_MODE" != existing ]; then
    preload_args=(--mode "$SIGNALK_MODE" --data "$VESSELSTACK_DATA" --uid "$SIGNALK_CONTAINER_UID" --gid "$SIGNALK_CONTAINER_GID")
    if [ "$OFFLINE" = true ]; then
        preload_args+=(--offline --check)
    else
        preload_args+=(--stage "$PLUGIN_STAGE" --publish)
    fi
    # Exit status 10 means this run newly installed the plugin; a running Signal K
    # container only sees it after a restart (native mode restarts below anyway).
    preload_rc=0
    python3 "$SCRIPT_DIR/scripts/preload-signalk-vesseltwin.py" "${preload_args[@]}" || preload_rc=$?
    case "$preload_rc" in
        0) ;;
        10) SIGNALK_PLUGIN_INSTALLED=1 ;;
        *) exit "$preload_rc" ;;
    esac
fi

# Home Assistant bind address: add, update or remove the managed http block in its
# configuration.yaml (only that block; never the owner's own http: section). A refusal is
# printed with manual instructions and never aborts the installation. With START=0 the
# container is not touched; it picks the file up at its next start.
env VESSELSTACK_ROOT="$VESSELSTACK_ROOT" VESSELSTACK_ENV="$VESSELSTACK_ROOT/config/vesselstack.env" \
    VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" "$SBIN_DIR/vesselstackctl" ha-http sync || \
    echo "WARN: could not apply HOMEASSISTANT_BIND_ADDRESS to Home Assistant; run: sudo vesselstackctl ha-http sync" >&2

# Home page (Homepage): render its configuration from the installed settings and refresh
# HOMEPAGE_ALLOWED_HOSTS before any container starts. Your own tiles in
# config/homepage-custom/ are created once and never overwritten.
env VESSELSTACK_ROOT="$VESSELSTACK_ROOT" VESSELSTACK_ENV="$VESSELSTACK_ROOT/config/vesselstack.env" \
    VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" "$SBIN_DIR/vesselstackctl" homepage sync || \
    echo "WARN: could not render the home page configuration; run: sudo vesselstackctl homepage sync" >&2

# Boat Chat reports "not ready" until its memory index has content, and nothing else
# builds it. Do it here, after the code, facts file and virtualenv are in place and the
# state directory exists, as the service user (see scripts/memory-index.sh). `ensure` is
# idempotent, so a re-run or an upgrade only rebuilds when the bundled knowledge changed.
# A failure is loud but never aborts the installation (doctor reports it too).
if ! "$SCRIPT_DIR/scripts/memory-index.sh" "$VESSELSTACK_ROOT/config/vesselstack.env" ensure; then
    echo "WARN: the Boat Chat memory index could not be built, so Boat Chat will report 'not ready' (and 'vesselstackctl doctor' will fail)." >&2
    echo "WARN: build it manually with: sudo vesselstackctl reindex" >&2
fi

if [ "$START" -eq 1 ]; then
    "$SCRIPT_DIR/scripts/configure-hardware.sh" "$VESSELSTACK_ROOT/config/vesselstack.env"
    if [ "$VESSELSTACK_FIREWALL_ENABLE" = true ]; then
        systemctl enable --now vesselstack-firewall.service
    else
        systemctl disable --now vesselstack-firewall.service 2>/dev/null || true
    fi
    install -d -m 0750 "$VESSELSTACK_DATA/mosquitto"
    # Heimdall (the previous start page) held port 80; remove its container and image
    # before Homepage is started. Its data directory is never touched.
    env VESSELSTACK_ROOT="$VESSELSTACK_ROOT" VESSELSTACK_ENV="$VESSELSTACK_ROOT/config/vesselstack.env" \
        VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" "$SBIN_DIR/vesselstackctl" homepage remove-heimdall || \
        echo "WARN: could not remove the old Heimdall container; run: sudo vesselstackctl homepage remove-heimdall" >&2
    # Broker access files were already rendered by sync_mqtt_access above.
    selected_services=()
    for core in "${CORE_COMPONENTS[@]}"; do
        component_disabled "$core" || selected_services+=("$core")
    done
    [ "$SIGNALK_MODE" = docker ] && selected_services+=(signalk)
    [ "$AIS_ENABLE" = true ] && selected_services+=(ais-catcher)
    [ "$NODE_EXPORTER_ENABLE" = true ] && selected_services+=(node-exporter)
    compose_up=(compose up -d)
    [ "$OFFLINE" != true ] || compose_up=(compose up -d --pull never)
    if [ "${#selected_services[@]}" -gt 0 ]; then
        "${compose_up[@]}" "${selected_services[@]}"
    fi
    if ! component_disabled prometheus && {
        [ ! -r "$PROMETHEUS_CONF_STAMP" ] ||
        [ "$(<"$PROMETHEUS_CONF_STAMP")" != "$PROMETHEUS_CONF_HASH" ];
    }; then
        "${compose_up[@]}" --force-recreate prometheus || exit 1
        write_prometheus_stamp || exit 1
    fi
    # "compose up -d" leaves a running Signal K container alone, so a plugin that this
    # run newly preloaded would stay unloaded until the next restart.
    if [ "$SIGNALK_MODE" = docker ] && [ "$SIGNALK_PLUGIN_INSTALLED" -eq 1 ]; then
        compose restart signalk
    fi
    [ "$NODE_EXPORTER_ENABLE" = true ] || compose rm --stop --force node-exporter 2>/dev/null || true
    # A changed compose definition is recreated by "compose up -d" itself; a
    # changed configuration file alone is not, and may have been rendered by an
    # earlier render-only run, so compare with the stamp of the last applied one.
    if ! component_disabled mosquitto && [ -r "$MOSQUITTO_CONF_STAMP" ] && [ "$(<"$MOSQUITTO_CONF_STAMP")" != "$MOSQUITTO_CONF_HASH" ]; then
        "${compose_up[@]}" --force-recreate mosquitto
    fi
    if ! component_disabled mosquitto; then
        printf '%s\n' "$MOSQUITTO_CONF_HASH" > "$MOSQUITTO_CONF_STAMP"
    fi
    if ! component_disabled influxdb; then
        "$SCRIPT_DIR/scripts/bootstrap-influx.sh" "$VESSELSTACK_ROOT/config/vesselstack.env"
    fi
    systemctl restart vesselstack-chat.service
    if [ "$TELEGRAM_ENABLE" = true ]; then
        systemctl restart vesselstack-chat-telegram.service
    else
        systemctl stop vesselstack-chat-telegram.service 2>/dev/null || true
    fi
    if [ "$TELEMETRY_INDEXER_ENABLE" = true ]; then
        systemctl start vesselstack-telemetry-sample.timer vesselstack-telemetry-indexer.timer
    else
        systemctl stop vesselstack-telemetry-sample.timer vesselstack-telemetry-indexer.timer 2>/dev/null || true
    fi
    if [ "$DISPLAYS_ENABLE" = true ]; then
        systemctl restart vesselstack-displays-bridge.service
    else
        systemctl stop vesselstack-displays-bridge.service 2>/dev/null || true
    fi
    if [ "${VESSELSTACK_SKIP_PANEL_RESTART:-false}" != true ]; then
        systemctl restart vesselstack-control-panel.service
    fi
    if [ "$SIGNALK_MODE" = native ]; then
        systemctl restart vesselstack-signalk.service
    fi
fi

if [ "$START" -eq 0 ]; then
    # A render-only run never recreates containers. A broker created by an older
    # release (or before displays were enabled) keeps its old configuration and
    # mounts, across reboots too, until the stack is started.
    mosquitto_mounts=$(docker inspect -f \
        '{{range .Mounts}}{{.Destination}}={{.Source}}{{"\n"}}{{end}}' vesselstack-mosquitto \
        2>/dev/null) || mosquitto_mounts=
    if [ -n "$mosquitto_mounts" ] && {
        ! grep -qx "/mosquitto/config=$MOSQUITTO_CONF_DIR" <<<"$mosquitto_mounts" ||
            ! grep -q '^/mosquitto/auth=' <<<"$mosquitto_mounts"
    }; then
        echo "NOTICE: the running Mosquitto container was created by an older configuration and"
        echo "        keeps its old configuration and mounts, also after a reboot, until it is"
        echo "        recreated. New MQTT accounts (displays bridge, displays) and the ACL take"
        echo "        effect only after: sudo vesselstackctl start"
    fi
    if [ "$DISPLAYS_ENABLE" = true ] && ! systemctl is-active --quiet vesselstack-displays-bridge.service; then
        echo "NOTICE: the displays bridge is enabled but not running; start it with: sudo vesselstackctl start"
    fi
fi

echo "VesselStack rendered successfully at $VESSELSTACK_ROOT"
echo "Secrets are stored in mode-600 config files and were not printed."
echo "Control panel: http://$CONTROL_PANEL_HOST:$CONTROL_PANEL_PORT"
