#!/bin/bash
# Configure explicitly enabled marine hardware after probing it.

set -euo pipefail

ENV_FILE=${1:-/opt/vesselstack/config/vesselstack.env}
# shellcheck disable=SC1090
source "$ENV_FILE"
SYSTEMD_DIR="${VESSELSTACK_SYSTEM_ROOT:-}/etc/systemd/system"

if [ "${SOCKETCAN_ENABLE:-false}" = true ]; then
    case "${SOCKETCAN_INTERFACE:-}" in
        can[0-9]*|vcan[0-9]*) ;;
        *) echo "Invalid SOCKETCAN_INTERFACE" >&2; exit 1 ;;
    esac
    case "${SOCKETCAN_BITRATE:-}" in
        ''|*[!0-9]*) echo "Invalid SOCKETCAN_BITRATE" >&2; exit 1 ;;
    esac
    command -v ip >/dev/null || { echo "SocketCAN requires the ip command" >&2; exit 1; }
    [ -e "/sys/class/net/$SOCKETCAN_INTERFACE" ] || {
        echo "SocketCAN interface not detected: $SOCKETCAN_INTERFACE" >&2
        exit 1
    }
    sed -e "s|@CAN_INTERFACE@|$SOCKETCAN_INTERFACE|g" \
        -e "s|@CAN_BITRATE@|$SOCKETCAN_BITRATE|g" \
        "$VESSELSTACK_ROOT/systemd/vesselstack-socketcan.service.in" \
        > "$SYSTEMD_DIR/vesselstack-socketcan.service"
    systemctl daemon-reload
    systemctl enable --now vesselstack-socketcan.service
fi

if [ "${AIS_ENABLE:-false}" = true ]; then
    [ -e "${AIS_DEVICE:-}" ] || { echo "AIS device not detected: ${AIS_DEVICE:-unset}" >&2; exit 1; }
fi
