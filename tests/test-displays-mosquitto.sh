#!/bin/bash
# Real-broker check of the always-on Mosquitto ACL and per-display accounts.
# Needs Docker and the pinned eclipse-mosquitto image. On Linux it must run as
# root (sudo) so the real ownership (directory root:1883 0750, files 1883:1883
# 0600) is produced and verified by the sync code itself. On macOS (non-root) a
# one-shot root container copies the synced files into a named Docker volume
# with the same real ownership, and the broker mounts that volume, because macOS
# bind mounts do not carry real uid/gid and would make the warning check vacuous.

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
MDIR="$ROOT/config/mosquitto-auth"
PDIR="$DATA/mosquitto"
install -d -m 0755 "$ROOT/config"
install -d -m 0750 "$DATA"
install -d -m 0750 "$PDIR"
install -m 0644 "$REPO_ROOT/templates/compose.yml" "$ROOT/compose.yml"
install -d -m 0755 "$ROOT/config/mosquitto"
install -m 0644 "$REPO_ROOT/templates/mosquitto.conf" "$ROOT/config/mosquitto/mosquitto.conf"

cleanup() {
    docker rm -f "$BROKER" "$BROKER-sub" >/dev/null 2>&1 || true
    docker network rm "$NETWORK" >/dev/null 2>&1 || true
    [ "$IS_ROOT" -eq 1 ] || docker volume rm "$AUTH_SRC" >/dev/null 2>&1 || true
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

# Where the broker reads its auth directory from: the real directory on a root
# (Linux) run, a volume with real ownership otherwise.
if [ "$IS_ROOT" -eq 1 ]; then AUTH_SRC="$MDIR"; else AUTH_SRC="$BROKER-auth"; fi

# Non-root hosts (macOS) cannot chown: have a root container copy the synced
# files into the volume with the ownership install.sh produces on Linux.
emulate_ownership() {
    [ "$IS_ROOT" -eq 1 ] && return 0
    docker volume create "$AUTH_SRC" >/dev/null
    # Docker Desktop bind mounts can briefly serve a stale view right after an
    # atomic rename, so retry the copy a few times.
    local attempt
    for attempt in 1 2 3 4 5; do
        docker run --rm -v "$AUTH_SRC:/auth" -v "$MDIR:/src:ro" --entrypoint /bin/sh "$IMAGE" -c \
            'rm -f /auth/acl /auth/passwords; cp /src/acl /src/passwords /auth/ && chown 0:1883 /auth && chmod 0750 /auth && chown 1883:1883 /auth/acl /auth/passwords && chmod 0600 /auth/acl /auth/passwords' \
            >/dev/null 2>&1 && break
        [ "$attempt" -lt 5 ] || fail "could not copy the auth files into the volume"
        sleep 1
    done
    # sync already signalled the broker before the volume was refreshed.
    docker kill -s HUP "$BROKER" >/dev/null 2>&1 || true
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
    mode=$(python3 -c 'import os,sys;print(oct(os.stat(sys.argv[1]).st_mode & 0o777))' "$MDIR")
    [ "$mode" = 0o750 ] || fail "auth directory mode is $mode"
    if [ "$IS_ROOT" -eq 1 ]; then
        owner=$(python3 -c 'import os,sys;s=os.stat(sys.argv[1]);print(f"{s.st_uid}:{s.st_gid}")' "$MDIR")
        [ "$owner" = 0:1883 ] || fail "auth directory owner is $owner"
    fi
    if [ -e "$MDIR/acl.new" ] || [ -e "$MDIR/passwords.new" ]; then fail "temp files left behind"; fi
    if [ -e "$PDIR/acl" ] || [ -e "$PDIR/passwords" ]; then fail "auth files leaked into the broker data directory"; fi
    grep -q '^[^:]*:\$7\$' "$MDIR/passwords" || fail "passwords are not hashed"
    pass
}

# The broker log must show no ownership warning or permission trouble for the
# auth files ("owner is not mosquitto" becomes a hard error in future releases).
check_broker_log() {
    local log
    log=$(docker logs "$BROKER" 2>&1)
    # On macOS bind mounts the persistence database (mosquitto.db in the data
    # directory) can always warn; there only the auth files are judged. On a
    # real-ownership (root) run every such message fails the test.
    local scope='/mosquitto/auth|passwords|acl'
    [ "$IS_ROOT" -eq 1 ] && scope=.
    if grep -Ei 'owner is not mosquitto|world readable|permission denied|unable to open' <<<"$log" \
        | grep -Eiq "$scope"; then
        echo "$log" >&2
        fail "broker log shows ownership or permission problems for the auth files"
    fi
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
        -v "$ROOT/config/mosquitto:/mosquitto/config:ro" \
        -v "$PDIR:/mosquitto/data" \
        -v "$AUTH_SRC:/mosquitto/auth:ro" "$IMAGE" >/dev/null
    wait_ready
}

docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull "$IMAGE" >/dev/null
docker network create "$NETWORK" >/dev/null

# 1. Displays enabled: admin, bridge, two displays.
write_displays helm aux
sync_mqtt true
emulate_ownership
check_files
[ "$(grep -c '^user ' "$MDIR/acl")" -eq 4 ] || fail "expected 4 ACL users"
start_broker
check_broker_log
# The auth directory is read-only inside the broker.
! docker exec "$BROKER" touch /mosquitto/auth/planted >/dev/null 2>&1 || fail "auth directory is writable in the broker"
pass
BRIDGE=vesselstack-bridge; BRIDGE_PASS=$(secret bridge)
HELM=vsdisp-helm; HELM_PASS=$(secret display helm)
AUX=vsdisp-aux; AUX_PASS=$(secret display aux)

# Admin: everything, including $SYS.
admin_put homeassistant/test admin-ok
expect_read "$ADMIN_USER" "$ADMIN_PASS" homeassistant/test
expect_read "$ADMIN_USER" "$ADMIN_PASS" '$SYS/broker/version'

# A passworded user that is not in the ACL gets nothing.
docker run --rm -v "$AUTH_SRC:/auth" --entrypoint /bin/sh "$IMAGE" -c \
    'printf "unlisted:unlistedpw\n" > /tmp/u && chmod 600 /tmp/u && mosquitto_passwd -U /tmp/u && cat /tmp/u >> /auth/passwords && chown 1883:1883 /auth/passwords && chmod 0600 /auth/passwords' >/dev/null
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
emulate_ownership
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
emulate_ownership
check_files
for _ in $(seq 1 10); do
    [ "$(docker inspect -f '{{.State.Running}}' "$BROKER-sub")" = false ] && break
    sleep 1
done
[ "$(docker inspect -f '{{.State.Running}}' "$BROKER-sub")" = false ] || fail "removed display stayed connected after HUP"
pass
expect_refused "$AUX" "$AUX_PASS"
check_broker_log
docker inspect -f '{{.State.Running}}' "$BROKER" | grep -qx true || fail "broker died after HUP"
pass

# Device lifecycle through the CLI: add, rotate, wifi, status, remove.
dctl() {
    (cd "$REPO_ROOT" && VESSELSTACK_ROOT="$ROOT" VESSELSTACK_DATA="$DATA" \
        MQTT_USERNAME="$ADMIN_USER" MQTT_PASSWORD="$ADMIN_PASS" DISPLAYS_ENABLE=true \
        python3 displays/displayctl.py "$@" --container "$BROKER" --allow-non-root)
}
CLI_OUT=$(dctl add ops --board waveshare-esp32-s3-touch-lcd-2.8c --name Ops 2>&1) || fail "displayctl add failed: $CLI_OUT"
grep -q 'MQTT username: vsdisp-ops' <<<"$CLI_OUT" || fail "add did not print the MQTT username"
OPS_PASS=$(secret display ops)
! grep -qF "$OPS_PASS" <<<"$CLI_OUT" || fail "add printed the display password"
emulate_ownership
check_files
sleep 1
expect_write vsdisp-ops "$OPS_PASS" vesselstack/v1/displays/vsdisp-ops/status
python3 -c 'import json,sys;p=json.load(open(sys.argv[1]))["displays"]["ops"]["pages"][0];assert p["layout"]=="grid-2x2" and len(p["items"])==4' \
    "$ROOT/config/displays.json" || fail "add did not create the default page"
pass

CLI_OUT=$(dctl rotate ops 2>&1) || fail "displayctl rotate failed: $CLI_OUT"
NEW_OPS_PASS=$(secret display ops)
[ "$NEW_OPS_PASS" != "$OPS_PASS" ] || fail "rotate kept the old password"
emulate_ownership
check_files
sleep 1
expect_refused vsdisp-ops "$OPS_PASS"
expect_write vsdisp-ops "$NEW_OPS_PASS" vesselstack/v1/displays/vsdisp-ops/status
expect_read "$HELM" "$HELM_PASS" vesselstack/v1/instruments/sog

printf 'Test Net\ntest-wifi-pass-1\n' | dctl wifi >/dev/null 2>&1 || fail "displayctl wifi failed"
CLI_OUT=$(dctl list 2>&1) || fail "displayctl list failed"
grep -q 'vsdisp-ops' <<<"$CLI_OUT" || fail "list does not show the new display"
! grep -qF -e "$NEW_OPS_PASS" -e test-wifi-pass-1 -e "$ADMIN_PASS" <<<"$CLI_OUT" || fail "list printed a secret"
pass

# status reads the retained topics through the real broker with the admin options file.
admin_put vesselstack/v1/bridge/status online
admin_put vesselstack/v1/bridge/signalk connected
admin_put vesselstack/v1/displays/vsdisp-ops/status online
CLI_OUT=$(dctl status 2>&1) || fail "displayctl status failed: $CLI_OUT"
grep -Eq '^Bridge +online$' <<<"$CLI_OUT" || fail "status did not show the bridge online: $CLI_OUT"
grep -Eq '^ops +online$' <<<"$CLI_OUT" || fail "status did not show ops online: $CLI_OUT"
grep -Eq '^helm +' <<<"$CLI_OUT" || fail "status did not list helm: $CLI_OUT"
! grep -qF "$ADMIN_PASS" <<<"$CLI_OUT" || fail "status printed the admin password"
! docker exec "$BROKER" ls /tmp 2>/dev/null | grep -q . || fail "status left a temporary options file in the broker container"
pass
clear_retained vesselstack/v1/bridge/status
clear_retained vesselstack/v1/bridge/signalk
clear_retained vesselstack/v1/displays/vsdisp-ops/status

docker run -d --name "$BROKER-sub" --network "$NETWORK" "$IMAGE" \
    mosquitto_sub -h "$BROKER" -u vsdisp-ops -P "$NEW_OPS_PASS" -t 'vesselstack/v1/instruments/#' -W 60 >/dev/null 2>&1 \
    || docker start "$BROKER-sub" >/dev/null
sleep 2
CLI_OUT=$(dctl remove ops 2>&1) && fail "remove without --yes should be refused"
CLI_OUT=$(dctl remove ops --yes 2>&1) || fail "displayctl remove failed: $CLI_OUT"
emulate_ownership
check_files
for _ in $(seq 1 10); do
    [ "$(docker inspect -f '{{.State.Running}}' "$BROKER-sub")" = false ] && break
    sleep 1
done
[ "$(docker inspect -f '{{.State.Running}}' "$BROKER-sub")" = false ] || fail "removed display stayed connected after HUP"
expect_refused vsdisp-ops "$NEW_OPS_PASS"
! grep -q 'vsdisp-ops' "$MDIR/acl" || fail "removed display is still in the ACL"
python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));assert "ops" not in d["displays"] and d["displays"]' \
    "$ROOT/config/displays.secrets.json" || fail "remove did not delete the display secrets"
pass
check_broker_log

# Disable displays: admin only; a container restart picks up everything.
sync_mqtt false
emulate_ownership
check_files
[ "$(grep -c '^user ' "$MDIR/acl")" -eq 1 ] || fail "disabled ACL should list only the admin"
docker restart "$BROKER" >/dev/null
wait_ready
expect_refused "$HELM" "$HELM_PASS"
expect_refused "$BRIDGE" "$BRIDGE_PASS"
expect_read "$ADMIN_USER" "$ADMIN_PASS" homeassistant/test
check_broker_log
clear_retained homeassistant/test

echo "displays mosquitto ACL checks passed ($COUNT assertions)"
