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
    case "${VESSELSTACK_CAN_CREATE_VCAN:-false}" in
        true|false) ;;
        *) echo "VESSELSTACK_CAN_CREATE_VCAN must be true or false" >&2; exit 1 ;;
    esac
    command -v ip >/dev/null || { echo "SocketCAN requires the ip command" >&2; exit 1; }
    can_pre_modprobe=/bin/true
    can_pre_create=/bin/true
    case "$SOCKETCAN_INTERFACE" in
        vcan*)
            # Virtual CAN has no bitrate and no "type can": never pass them.
            can_link_args=
            if [ "${VESSELSTACK_CAN_CREATE_VCAN:-false}" = true ]; then
                # The leading "-" tolerates an interface that already exists.
                can_pre_modprobe="-/usr/sbin/modprobe vcan"
                can_pre_create="-/usr/sbin/ip link add dev $SOCKETCAN_INTERFACE type vcan"
            elif [ ! -e "${VESSELSTACK_SYS_CLASS_NET:-/sys/class/net}/$SOCKETCAN_INTERFACE" ]; then
                echo "Virtual CAN interface not found: $SOCKETCAN_INTERFACE (create it with 'ip link add dev $SOCKETCAN_INTERFACE type vcan' or set VESSELSTACK_CAN_CREATE_VCAN=true)" >&2
                exit 1
            fi
            ;;
        *)
            case "${SOCKETCAN_BITRATE:-}" in
                ''|*[!0-9]*) echo "Invalid SOCKETCAN_BITRATE" >&2; exit 1 ;;
            esac
            [ -e "${VESSELSTACK_SYS_CLASS_NET:-/sys/class/net}/$SOCKETCAN_INTERFACE" ] || {
                echo "SocketCAN interface not detected: $SOCKETCAN_INTERFACE" >&2
                exit 1
            }
            can_link_args=" type can bitrate $SOCKETCAN_BITRATE restart-ms 100"
            ;;
    esac
    sed -e "s|@CAN_INTERFACE@|$SOCKETCAN_INTERFACE|g" \
        -e "s|@CAN_LINK_ARGS@|$can_link_args|g" \
        -e "s|@CAN_PRE_MODPROBE@|$can_pre_modprobe|g" \
        -e "s|@CAN_PRE_CREATE@|$can_pre_create|g" \
        "$VESSELSTACK_ROOT/systemd/vesselstack-socketcan.service.in" \
        > "$SYSTEMD_DIR/vesselstack-socketcan.service"
    systemctl daemon-reload
    systemctl enable --now vesselstack-socketcan.service
fi

if [ "${AIS_ENABLE:-false}" = true ]; then
    [ -e "${AIS_DEVICE:-}" ] || { echo "AIS device not detected: ${AIS_DEVICE:-unset}" >&2; exit 1; }
fi
