#!/bin/bash
# Reconcile mutually exclusive host/Compose services before applying a new mode.
set -u
ENV_FILE=${1:?configuration file is required}
ACTION=${2:-apply}
[ "$ACTION" = apply ] || { echo "usage: reconcile-lifecycle.sh ENV_FILE apply" >&2; exit 2; }
desired_mode=${VESSELSTACK_DESIRED_SIGNALK_MODE:-}
desired_ais=${VESSELSTACK_DESIRED_AIS_ENABLE:-}
desired_socketcan=${VESSELSTACK_DESIRED_SOCKETCAN_ENABLE:-}
SYSTEMD_ROOT=${VESSELSTACK_SYSTEM_ROOT:-${SYSTEM_ROOT:-}}
SYSTEMD_DIR="$SYSTEMD_ROOT/etc/systemd/system"
if [ ! -f "$ENV_FILE" ]; then
    prospective_root=$(dirname -- "$(dirname -- "$ENV_FILE")")
    if [ ! -f "$prospective_root/compose.yml" ] \
        && [ ! -e "$SYSTEMD_DIR/vesselstack-signalk.service" ] \
        && [ ! -e "$SYSTEMD_DIR/vesselstack-socketcan.service" ]; then
        exit 0
    fi
    echo "Managed lifecycle artifacts exist but configuration is missing: $ENV_FILE" >&2
    exit 1
fi
# shellcheck disable=SC1090
source "$ENV_FILE" || { echo "Could not load configuration: $ENV_FILE" >&2; exit 1; }
ROOT=${VESSELSTACK_ROOT:?VESSELSTACK_ROOT is required}
[ -z "$desired_mode" ] || SIGNALK_MODE=$desired_mode
[ -z "$desired_ais" ] || AIS_ENABLE=$desired_ais
[ -z "$desired_socketcan" ] || SOCKETCAN_ENABLE=$desired_socketcan
# A first install has no prior managed Compose file or native units. There is
# nothing to reconcile, and probing the host would create a false failure.
if [ ! -f "$ROOT/compose.yml" ] \
    && [ ! -e "$SYSTEMD_DIR/vesselstack-signalk.service" ] \
    && [ ! -e "$SYSTEMD_DIR/vesselstack-socketcan.service" ]; then
    exit 0
fi

unit_present() {
    local unit=$1 state
    state=$(systemctl show -p LoadState --value "$unit" 2>/dev/null) || {
        echo "Could not inspect $unit" >&2
        return 2
    }
    [ "$state" != not-found ]
}
stop_unit() {
    local unit=$1
    unit_present "$unit"; case $? in 1) return 0 ;; 2) return 1 ;; esac
    systemctl stop "$unit" || { echo "Could not stop $unit" >&2; return 1; }
}
disable_unit() {
    local unit=$1
    unit_present "$unit"; case $? in 1) return 0 ;; 2) return 1 ;; esac
    systemctl disable "$unit" || { echo "Could not disable $unit" >&2; return 1; }
}
compose_has() {
    local service=$1 output rc
    [ -f "$ROOT/compose.yml" ] || return 1
    output=$(docker compose --profile signalk --profile ais --env-file "$ENV_FILE" \
        -f "$ROOT/compose.yml" ps -q "$service" 2>/dev/null); rc=$?
    [ "$rc" -eq 0 ] || { echo "Could not inspect Compose service $service" >&2; return 2; }
    [ -n "$output" ]
}
stop_compose_service() {
    local service=$1
    compose_has "$service"; case $? in 1) return 0 ;; 2) return 1 ;; esac
    docker compose --profile signalk --profile ais --env-file "$ENV_FILE" \
        -f "$ROOT/compose.yml" stop "$service" || {
        echo "Could not stop Compose service $service" >&2
        return 1
    }
}

mode=${SIGNALK_MODE:-existing}
case "$mode" in docker|native|existing) ;; *) echo "Invalid SIGNALK_MODE: $mode" >&2; exit 1 ;; esac
case "$mode" in
    docker) stop_unit vesselstack-signalk.service || exit 1; disable_unit vesselstack-signalk.service || exit 1 ;;
    native) stop_compose_service signalk || exit 1 ;;
    existing) stop_unit vesselstack-signalk.service || exit 1; disable_unit vesselstack-signalk.service || exit 1; stop_compose_service signalk || exit 1 ;;
esac
if [ "${AIS_ENABLE:-false}" != true ]; then
    stop_compose_service ais-catcher || exit 1
fi
if [ "${SOCKETCAN_ENABLE:-false}" != true ]; then
    stop_unit vesselstack-socketcan.service || exit 1
    disable_unit vesselstack-socketcan.service || exit 1
fi
exit 0
