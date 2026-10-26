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
# Empty = unset: fresh InfluxDB setups get 30d on the raw bucket; existing buckets
# are never changed unless a value is set explicitly (see README).
: "${INFLUXDB_RAW_RETENTION:=}"
# Prometheus TSDB retention (time only; never 0, which Prometheus would treat as the default).
: "${PROMETHEUS_RETENTION:=30d}"

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
    TELEGRAM_ENABLE TELEMETRY_INDEXER_ENABLE; do
    case "${!key}" in
        true|false) ;;
        *) echo "$key must be true or false" >&2; exit 1 ;;
    esac
done
for key in VESSELSTACK_FIREWALL_ALLOW_TCP VESSELSTACK_FIREWALL_ALLOW_UDP; do
    [[ "${!key}" =~ ^([0-9]{1,5}(:[0-9]{1,5})?(,[0-9]{1,5}(:[0-9]{1,5})?)*)?$ ]] || {
        echo "$key must be comma-separated ports or LOW:HIGH ranges, e.g. 22,5000:5100" >&2
        exit 1
    }
done
for key in GRAFANA_PORT HEIMDALL_PORT HEIMDALL_HTTPS_PORT INFLUXDB_PORT \
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
register_port HeimdallHTTP "$HEIMDALL_PORT"
register_port HeimdallHTTPS "$HEIMDALL_HTTPS_PORT"
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
        { compose_template config --images; echo "${MOSQUITTO_IMAGE:-eclipse-mosquitto:2.1.2-alpine}"; } | sort -u
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

if [ "$DRY_RUN" -eq 1 ]; then
    echo "Preflight passed"
    [ "$OFFLINE" = true ] && echo "Offline mode: Python packages from $VESSELSTACK_WHEELHOUSE, no image pulls"
    echo "Would install to $VESSELSTACK_ROOT with data in $VESSELSTACK_DATA"
    echo "SignalK mode: $SIGNALK_MODE ($SIGNALK_URL)"
    exit 0
fi

[ "$(id -u)" -eq 0 ] || { echo "Installation requires root" >&2; exit 1; }
install -d -m 0755 "$SYSTEMD_DIR" "$SBIN_DIR"

# Transactional ordering: the network/package-dependent step runs FIRST, into a
# staging virtualenv. Only when it succeeds is anything else rewritten, and the
# staged venv is swapped in later. A failed (or offline) run therefore changes
# nothing: no env, units, code or installed-version are touched.
install -d -m 0755 "$VESSELSTACK_ROOT"
STAGING_DIR=$(mktemp -d "$VESSELSTACK_ROOT/.venv-staging.XXXXXX")
trap 'rm -rf -- "$STAGING_DIR"' EXIT
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
    "$VESSELSTACK_ROOT/installer"
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
if [ -e "$VESSELSTACK_ROOT/boat-chat/memory" ] || [ -e "$VESSELSTACK_ROOT/boat-chat/data" ] \
    || [ -e "$VESSELSTACK_ROOT/boat-chat/boat-chat.env" ] \
    || [ -L "$VESSELSTACK_ROOT/boat-chat/memory" ] || [ -L "$VESSELSTACK_ROOT/boat-chat/data" ]; then
    # Quiesce writers so SQLite files are not copied mid-write.
    systemctl stop vesselstack-chat.service vesselstack-chat-telegram.service \
        vesselstack-telemetry-indexer.service 2>/dev/null || true
    for runtime_name in memory data boat-chat.env; do
        migrate_runtime_path "$runtime_name"
    done
fi
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
safe_data_path "$VESSELSTACK_DATA/homeassistant/blueprints/automation/vesselstack"
safe_data_path "$VESSELSTACK_DATA/homeassistant/vesselstack-dashboard.example.yaml" leaf
install -d -m 0755 "$VESSELSTACK_DATA/homeassistant/blueprints/automation/vesselstack"
for blueprint in "$SCRIPT_DIR"/templates/homeassistant/blueprints/automation/vesselstack/*.yaml; do
    install -m 0644 "$blueprint" \
        "$VESSELSTACK_DATA/homeassistant/blueprints/automation/vesselstack/$(basename "$blueprint")"
done
install -m 0644 "$SCRIPT_DIR/templates/homeassistant/lovelace-vesselstack.example.yaml" \
    "$VESSELSTACK_DATA/homeassistant/vesselstack-dashboard.example.yaml"
install -m 0755 "$SCRIPT_DIR/scripts/vesselstackctl" "$SBIN_DIR/vesselstackctl"
install -m 0755 "$SCRIPT_DIR/scripts/vesselstack-firewall" "$SBIN_DIR/vesselstack-firewall"
# The update launcher and the helper it runs as root are installed root-owned and
# never executed from a source checkout. A new helper version only arrives through
# this installer (i.e. from a reviewed/verified release tree).
install -d -m 0755 -o root -g root "$LIBEXEC_PARENT" "$LIBEXEC_DIR"
install -m 0755 -o root -g root "$SCRIPT_DIR/scripts/vesselstack-update" "$SBIN_DIR/vesselstack-update"
install -m 0755 -o root -g root "$SCRIPT_DIR/scripts/deploy-chat.py" "$LIBEXEC_DIR/deploy-chat.py"
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
VESSELSTACK_CAN_CREATE_VCAN
AIS_ENABLE
AIS_IMAGE
AIS_DEVICE
AIS_CATCHER_ARGS
AIS_WEB_PORT
AIS_TCP_PORT
VESSELSTACK_UNTRUSTED_INTERFACE
VESSELSTACK_FIREWALL_ENABLE
VESSELSTACK_FIREWALL_STRICT
VESSELSTACK_FIREWALL_ALLOW_TCP
VESSELSTACK_FIREWALL_ALLOW_UDP
CONTROL_PANEL_HOST
CONTROL_PANEL_PORT
TELEGRAM_ENABLE
TELEMETRY_INDEXER_ENABLE
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
HEIMDALL_PORT
HEIMDALL_HTTPS_PORT
PROMETHEUS_PORT
PROMETHEUS_RETENTION
MQTT_USERNAME
MQTT_PASSWORD
MQTT_PORT
VESSELSTACK_DATA_BIND_ADDRESS
VESSELSTACK_WEB_BIND_ADDRESS
HOMEASSISTANT_BIND_ADDRESS
EOF

# Retain provider credentials and optional Boat Chat settings across installer
# re-renders while replacing the integration values owned by VesselStack.
boat_chat_temp=$(mktemp)
if [ -r "$VESSELSTACK_ROOT/config/boat-chat.env" ]; then
    cp "$VESSELSTACK_ROOT/config/boat-chat.env" "$boat_chat_temp"
fi
for key in BOAT_NAME BOAT_TIMEZONE VESSELSTACK_VERSION BOAT_CHAT_SETTINGS_TOKEN BOAT_CHAT_ACCESS_TOKEN \
    TELEGRAM_ENABLE TELEMETRY_INDEXER_ENABLE \
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
    printf 'BOAT_TIMEZONE=%q\n' "$BOAT_TIMEZONE"
    printf 'VESSELSTACK_VERSION=%q\n' "$(<"$SCRIPT_DIR/VERSION")"
    printf 'BOAT_CHAT_SETTINGS_TOKEN=%q\n' "$BOAT_CHAT_SETTINGS_TOKEN"
    printf 'BOAT_CHAT_ACCESS_TOKEN=%q\n' "$BOAT_CHAT_ACCESS_TOKEN"
    printf 'TELEGRAM_ENABLE=%q\n' "$TELEGRAM_ENABLE"
    printf 'TELEMETRY_INDEXER_ENABLE=%q\n' "$TELEMETRY_INDEXER_ENABLE"
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
for unit in vesselstack-chat-telegram.service vesselstack-telemetry-indexer.service; do
    sed -e "s|@VESSELSTACK_USER@|$VESSELSTACK_USER|g" \
        -e "s|@VESSELSTACK_ROOT@|$VESSELSTACK_ROOT|g" \
        -e "s|@VESSELSTACK_DATA@|$VESSELSTACK_DATA|g" \
        "$SCRIPT_DIR/systemd/$unit.in" > "$SYSTEMD_DIR/$unit"
    chmod 0644 "$SYSTEMD_DIR/$unit"
done
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
else
    systemctl disable vesselstack-telemetry-indexer.timer 2>/dev/null || true
fi

compose config --quiet

# Everything rendered and validated: record the new schema version last.
"$SCRIPT_DIR/scripts/migrate.sh" "$VESSELSTACK_ROOT/config/vesselstack.env" \
    "$(<"$SCRIPT_DIR/VERSION")"

if [ "$SIGNALK_MODE" = native ]; then
    "$SCRIPT_DIR/scripts/install-signalk-native.sh" "$VESSELSTACK_ROOT/config/vesselstack.env"
fi

# Home Assistant bind address: add, update or remove the managed http block in its
# configuration.yaml (only that block; never the owner's own http: section). A refusal is
# printed with manual instructions and never aborts the installation. With START=0 the
# container is not touched; it picks the file up at its next start.
env VESSELSTACK_ROOT="$VESSELSTACK_ROOT" VESSELSTACK_ENV="$VESSELSTACK_ROOT/config/vesselstack.env" \
    VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" "$SBIN_DIR/vesselstackctl" ha-http sync || \
    echo "WARN: could not apply HOMEASSISTANT_BIND_ADDRESS to Home Assistant; run: sudo vesselstackctl ha-http sync" >&2

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
    "$SCRIPT_DIR/scripts/mosquitto-credentials.sh" "$VESSELSTACK_ROOT/config/vesselstack.env"
    if [ "$OFFLINE" = true ]; then
        compose up -d --pull never
    else
        compose up -d
    fi
    "$SCRIPT_DIR/scripts/bootstrap-influx.sh" "$VESSELSTACK_ROOT/config/vesselstack.env"
    systemctl restart vesselstack-chat.service
    if [ "$TELEGRAM_ENABLE" = true ]; then
        systemctl restart vesselstack-chat-telegram.service
    else
        systemctl stop vesselstack-chat-telegram.service 2>/dev/null || true
    fi
    if [ "$TELEMETRY_INDEXER_ENABLE" = true ]; then
        systemctl start vesselstack-telemetry-indexer.timer
    else
        systemctl stop vesselstack-telemetry-indexer.timer 2>/dev/null || true
    fi
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
