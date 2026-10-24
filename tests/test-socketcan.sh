#!/bin/bash
# SocketCAN unit rendering for physical (can*) and virtual (vcan*) interfaces.

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT
FAKE_BIN="$TEST_ROOT/bin"
APP_ROOT="$TEST_ROOT/app"
SYSTEM_ROOT="$TEST_ROOT/system-root"
FAKE_SYS="$TEST_ROOT/sys-class-net"
UNIT="$SYSTEM_ROOT/etc/systemd/system/vesselstack-socketcan.service"
install -d "$FAKE_BIN" "$APP_ROOT/systemd" "$SYSTEM_ROOT/etc/systemd/system" "$FAKE_SYS"
cp "$REPO_ROOT/systemd/vesselstack-socketcan.service.in" "$APP_ROOT/systemd/"
for command in ip systemctl; do printf '#!/bin/bash\nexit 0\n' > "$FAKE_BIN/$command"; chmod +x "$FAKE_BIN/$command"; done

render() {
    # render INTERFACE CREATE_VCAN -> runs configure-hardware.sh; status is the script's
    rm -f "$UNIT"
    cat > "$TEST_ROOT/env" <<EOF2
VESSELSTACK_ROOT=$APP_ROOT
SOCKETCAN_ENABLE=true
SOCKETCAN_INTERFACE=$1
SOCKETCAN_BITRATE=250000
VESSELSTACK_CAN_CREATE_VCAN=$2
EOF2
    env PATH="$FAKE_BIN:$PATH" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" VESSELSTACK_SYS_CLASS_NET="$FAKE_SYS" \
        "$REPO_ROOT/scripts/configure-hardware.sh" "$TEST_ROOT/env" 2>"$TEST_ROOT/err"
}
fail() { echo "FAIL: $*" >&2; exit 1; }

# vcan, created on demand: no bitrate, no "type can".
render vcan0 true
grep -qx 'ExecStartPre=-/usr/sbin/modprobe vcan' "$UNIT"
grep -qx 'ExecStartPre=-/usr/sbin/ip link add dev vcan0 type vcan' "$UNIT"
grep -qx 'ExecStart=/usr/sbin/ip link set vcan0 up' "$UNIT"
grep '^Exec' "$UNIT" | grep -qE 'bitrate|type can|restart-ms' && fail "vcan unit passes physical-CAN arguments"
grep -q '@CAN' "$UNIT" && fail "unrendered placeholder in unit"

# vcan that must already exist.
if render vcan0 false; then fail "missing vcan accepted without VESSELSTACK_CAN_CREATE_VCAN"; fi
grep -q "ip link add dev vcan0 type vcan" "$TEST_ROOT/err"
touch "$FAKE_SYS/vcan0"
render vcan0 false
grep -qx 'ExecStart=/usr/sbin/ip link set vcan0 up' "$UNIT"
grep -q 'link add' "$UNIT" && fail "existing vcan must not be created"

# Physical CAN keeps bitrate and restart-ms and is never auto-created.
if render can0 true; then fail "missing can0 accepted"; fi
grep -q 'not detected' "$TEST_ROOT/err"
touch "$FAKE_SYS/can0"
render can0 true
grep -qx 'ExecStart=/usr/sbin/ip link set can0 up type can bitrate 250000 restart-ms 100' "$UNIT"
grep -q 'link add' "$UNIT" && fail "physical CAN must not be created"
if render eth0 false; then fail "invalid interface accepted"; fi
if render vcan0 maybe; then fail "invalid VESSELSTACK_CAN_CREATE_VCAN accepted"; fi

echo "socketcan checks passed"
