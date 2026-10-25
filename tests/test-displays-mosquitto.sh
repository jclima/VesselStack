#!/bin/bash
# Real-broker check of the always-on Mosquitto ACL and per-display accounts.
# Needs Docker and the pinned eclipse-mosquitto image. On Linux it must run as
# root (sudo) so the broker-owned 1883:1883 files can be written and verified.

# shellcheck disable=SC2016  # $SYS topics and regexes are intentionally literal
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
IMAGE=$(sed -n 's/^ *image: \(eclipse-mosquitto:[^ ]*\) *$/\1/p' "$REPO_ROOT/templates/compose.yml" | head -n 1)
[ -n "$IMAGE" ] || { echo "Cannot find pinned Mosquitto image" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
IS_ROOT=0
[ "$(id -u)" -eq 0 ] && IS_ROOT=1
if [ "$IS_ROOT" -eq 0 ] && [ "$(uname -s)" = Linux ]; then
    echo "Run with sudo on Linux so broker-owned files can be written" >&2
    exit 1
fi

SUFFIX="$$-$RANDOM"
NETWORK="vs-acl-test-$SUFFIX"
BROKER="vs-acl-broker-$SUFFIX"
TEST_ROOT=$(mktemp -d)
ROOT="$TEST_ROOT/root"
DATA="$TEST_ROOT/data"
MDIR="$DATA/mosquitto"
install -d -m 0755 "$ROOT/config"
install -d -m 0750 "$DATA"
install -m 0644 "$REPO_ROOT/templates/compose.yml" "$ROOT/compose.yml"
install -m 0644 "$REPO_ROOT/templates/mosquitto.conf" "$ROOT/config/mosquitto.conf"

cleanup() {
    docker rm -f "$BROKER" "$BROKER-sub" >/dev/null 2>&1 || true
    docker network rm "$NETWORK" >/dev/null 2>&1 || true
    # Files may be owned by uid 1883; remove them from inside a container if needed.
    rm -rf -- "$TEST_ROOT" 2>/dev/null || \
        docker run --rm -v "$TEST_ROOT:/t" --entrypoint /bin/sh "$IMAGE" -c 'rm -rf /t/* /t/.[!.]*' \
            >/dev/null 2>&1 || true
    rm -rf -- "$TEST_ROOT" 2>/dev/null || true
}
trap cleanup EXIT

ADMIN_USER=boatadmin
ADMIN_PASS=$(openssl rand -hex 16)
COUNT=0
fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { COUNT=$((COUNT + 1)); }

write_displays() {
    python3 - "$ROOT/config/displays.json" "$@" <<'PY'
import json, sys
path, ids = sys.argv[1], sys.argv[2:]
cfg = json.load(open("displays/displays.example.json"))
tpl = cfg["displays"]["helm"]
cfg["displays"] = {i: dict(tpl, name=i.title()) for i in ids}
json.dump(cfg, open(path, "w"), indent=2)
PY
}

sync_mqtt() {
    (cd "$REPO_ROOT" && VESSELSTACK_ROOT="$ROOT" VESSELSTACK_DATA="$DATA" \
        MQTT_USERNAME="$ADMIN_USER" MQTT_PASSWORD="$ADMIN_PASS" \
        DISPLAYS_ENABLE="$1" DISPLAYS_BRIDGE_MQTT_USERNAME=vesselstack-bridge \
        python3 displays/displayctl.py sync-mqtt --container "$BROKER") >/dev/null \
        || fail "sync-mqtt ($1) failed"
}

secret() { # secret bridge | secret display ID
    python3 - "$ROOT/config/displays.secrets.json" "$@" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(d["bridge"]["mqtt_password"] if sys.argv[2] == "bridge"
      else d["displays"][sys.argv[3]]["mqtt_password"])
PY
}

check_files() {
    local f mode owner
    for f in acl passwords; do
        mode=$(python3 -c 'import os,sys;print(oct(os.stat(sys.argv[1]).st_mode & 0o777))' "$MDIR/$f")
        [ "$mode" = 0o600 ] || fail "$f mode is $mode"
        if [ "$IS_ROOT" -eq 1 ]; then
            owner=$(python3 -c 'import os,sys;s=os.stat(sys.argv[1]);print(f"{s.st_uid}:{s.st_gid}")' "$MDIR/$f")
            [ "$owner" = 1883:1883 ] || fail "$f owner is $owner"
        fi
    done
    [ ! -e "$MDIR/acl.new" ] && [ ! -e "$MDIR/passwords.new" ] || fail "temp files left behind"
    grep -q '^[^:]*:\$7\$' "$MDIR/passwords" || fail "passwords are not hashed"
    pass
}

client() { # client TOOL ARGS... (run on the test network)
    docker run --rm --network "$NETWORK" "$IMAGE" "$@"
}

# can_read USER PASS TOPIC : subscribe and expect the retained message
can_read() {
    client mosquitto_sub -h "$BROKER" -u "$1" -P "$2" -t "$3" -C 1 -W 4 >/dev/null 2>&1
}
# put USER PASS TOPIC MSG : retained publish; result observed by the admin
put() {
    client mosquitto_pub -h "$BROKER" -u "$1" -P "$2" -t "$3" -m "$4" -r -q 1 >/dev/null 2>&1 || true
}
admin_sees() {
    can_read "$ADMIN_USER" "$ADMIN_PASS" "$1"
}
admin_put() { put "$ADMIN_USER" "$ADMIN_PASS" "$1" "$2"; }
clear_retained() { client mosquitto_pub -h "$BROKER" -u "$ADMIN_USER" -P "$ADMIN_PASS" -t "$1" -n -r >/dev/null 2>&1 || true; }

expect_read()   { can_read "$1" "$2" "$3" || fail "$1 should read $3"; pass; }
expect_noread() { ! can_read "$1" "$2" "$3" || fail "$1 must not read $3"; pass; }
expect_write()  { put "$1" "$2" "$3" "w-$RANDOM"; admin_sees "$3" || fail "$1 should write $3"; pass; }
expect_nowrite() { put "$1" "$2" "$3" "w-$RANDOM"; ! admin_sees "$3" || fail "$1 must not write $3"; pass; }
expect_refused() {
    ! client mosquitto_pub -h "$BROKER" -u "$1" -P "$2" -t refused/test -m x -q 1 >/dev/null 2>&1 \
        || fail "$1 should be refused"
    pass
}
wait_ready() {
    for _ in $(seq 1 30); do
        client mosquitto_sub -h "$BROKER" -u "$ADMIN_USER" -P "$ADMIN_PASS" -t '$SYS/broker/version' \
            -C 1 -W 2 >/dev/null 2>&1 && return 0
        sleep 1
    done
    docker logs "$BROKER" >&2 || true
    fail "broker did not become ready"
}
start_broker() {
    docker run -d --name "$BROKER" --network "$NETWORK" \
        -v "$ROOT/config/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" \
        -v "$MDIR:/mosquitto/data" "$IMAGE" >/dev/null
    wait_ready
}

docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull "$IMAGE" >/dev/null
docker network create "$NETWORK" >/dev/null

# 1. Displays enabled: admin, bridge, two displays.
write_displays helm aux
sync_mqtt true
check_files
[ "$(grep -c '^user ' "$MDIR/acl")" -eq 4 ] || fail "expected 4 ACL users"
start_broker
BRIDGE=vesselstack-bridge; BRIDGE_PASS=$(secret bridge)
HELM=vsdisp-helm; HELM_PASS=$(secret display helm)
AUX=vsdisp-aux; AUX_PASS=$(secret display aux)

# Admin: everything, including $SYS.
admin_put homeassistant/test admin-ok
expect_read "$ADMIN_USER" "$ADMIN_PASS" homeassistant/test
expect_read "$ADMIN_USER" "$ADMIN_PASS" '$SYS/broker/version'

# A passworded user that is not in the ACL gets nothing.
docker run --rm -v "$MDIR:/mosquitto/data" --entrypoint /bin/sh "$IMAGE" -c \
    'printf "unlisted:unlistedpw\n" > /tmp/u && chmod 600 /tmp/u && mosquitto_passwd -U /tmp/u && cat /tmp/u >> /mosquitto/data/passwords && chown 1883:1883 /mosquitto/data/passwords' >/dev/null
docker kill -s HUP "$BROKER" >/dev/null
sleep 1
expect_noread unlisted unlistedpw homeassistant/test
expect_nowrite unlisted unlistedpw vesselstack/v1/instruments/sog

# Display account.
admin_put vesselstack/v1/instruments/sog '{"v":5}'
admin_put vesselstack/v1/bridge/status online
expect_read "$HELM" "$HELM_PASS" vesselstack/v1/instruments/sog
expect_read "$HELM" "$HELM_PASS" vesselstack/v1/bridge/status
expect_noread "$HELM" "$HELM_PASS" homeassistant/test
expect_noread "$HELM" "$HELM_PASS" '$SYS/broker/version'
expect_write "$HELM" "$HELM_PASS" vesselstack/v1/displays/vsdisp-helm/status
expect_nowrite "$HELM" "$HELM_PASS" vesselstack/v1/displays/vsdisp-aux/status
expect_nowrite "$HELM" "$HELM_PASS" vesselstack/v1/instruments/depth
expect_nowrite "$HELM" "$HELM_PASS" vesselstack/v1/bridge/other
expect_nowrite "$HELM" "$HELM_PASS" homeassistant/helm-write
expect_write "$AUX" "$AUX_PASS" vesselstack/v1/displays/vsdisp-aux/status
expect_noread "$HELM" "$HELM_PASS" vesselstack/v1/displays/vsdisp-aux/status

# Bridge account.
expect_write "$BRIDGE" "$BRIDGE_PASS" vesselstack/v1/instruments/stw
expect_write "$BRIDGE" "$BRIDGE_PASS" vesselstack/v1/bridge/signalk
expect_read "$BRIDGE" "$BRIDGE_PASS" vesselstack/v1/displays/vsdisp-helm/status
expect_nowrite "$BRIDGE" "$BRIDGE_PASS" vesselstack/v1/displays/vsdisp-nav/status
expect_noread "$BRIDGE" "$BRIDGE_PASS" homeassistant/test
expect_noread "$BRIDGE" "$BRIDGE_PASS" vesselstack/v1/instruments/sog

# Add a display: sync + HUP makes the new account work with no restart.
write_displays helm aux nav
sync_mqtt true
check_files
NAV_PASS=$(secret display nav)
sleep 1
expect_write vsdisp-nav "$NAV_PASS" vesselstack/v1/displays/vsdisp-nav/status
# Existing passwords are stable across syncs.
[ "$(secret display helm)" = "$HELM_PASS" ] || fail "display password was regenerated"
expect_read "$HELM" "$HELM_PASS" vesselstack/v1/instruments/sog

# Remove a display: a connected client is disconnected and new logins refused.
docker run -d --name "$BROKER-sub" --network "$NETWORK" "$IMAGE" \
    mosquitto_sub -h "$BROKER" -u "$AUX" -P "$AUX_PASS" -t 'vesselstack/v1/instruments/#' -W 60 >/dev/null
sleep 2
[ "$(docker inspect -f '{{.State.Running}}' "$BROKER-sub")" = true ] || fail "subscriber did not stay connected"
write_displays helm nav
sync_mqtt true
check_files
for _ in $(seq 1 10); do
    [ "$(docker inspect -f '{{.State.Running}}' "$BROKER-sub")" = false ] && break
    sleep 1
done
[ "$(docker inspect -f '{{.State.Running}}' "$BROKER-sub")" = false ] || fail "removed display stayed connected after HUP"
pass
expect_refused "$AUX" "$AUX_PASS"
docker inspect -f '{{.State.Running}}' "$BROKER" | grep -qx true || fail "broker died after HUP"
pass

# Disable displays: admin only; a container restart picks up everything.
sync_mqtt false
check_files
[ "$(grep -c '^user ' "$MDIR/acl")" -eq 1 ] || fail "disabled ACL should list only the admin"
docker restart "$BROKER" >/dev/null
wait_ready
expect_refused "$HELM" "$HELM_PASS"
expect_refused "$BRIDGE" "$BRIDGE_PASS"
expect_read "$ADMIN_USER" "$ADMIN_PASS" homeassistant/test
clear_retained homeassistant/test

echo "displays mosquitto ACL checks passed ($COUNT assertions)"
