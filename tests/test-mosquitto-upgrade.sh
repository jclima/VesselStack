#!/bin/bash
# Upgrade-shaped Docker test for the Mosquitto configuration move.
#
# A release-1.3.0 broker (single-file bind mount of config/mosquitto.conf, no ACL,
# legacy passwords in the broker data directory) is running. The new release
# renders its files WITHOUT recreating the container (what a Control Panel Apply
# or "install.sh" without --start does). The old container must survive a
# "docker restart" (as after a reboot) unchanged. Only when the container is
# recreated from the real templates/compose.yml mounts do the ACL and the new
# accounts take effect.
#
# On Linux run as root (sudo). On macOS (non-root) a one-shot root container copies
# the synced auth files into a named volume with the real ownership, as in
# tests/test-displays-mosquitto.sh.

# shellcheck disable=SC2016  # $SYS topics and jq programs are intentionally literal
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$REPO_ROOT"
IMAGE=$(sed -n 's/^ *image: \(eclipse-mosquitto:[^ ]*\) *$/\1/p' templates/compose.yml | head -n 1)
[ -n "$IMAGE" ] || { echo "Cannot find pinned Mosquitto image" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }
IS_ROOT=0
[ "$(id -u)" -eq 0 ] && IS_ROOT=1
if [ "$IS_ROOT" -eq 0 ] && [ "$(uname -s)" = Linux ]; then
    echo "Run with sudo on Linux so broker-owned files can be written" >&2
    exit 1
fi

SUFFIX="$$-$RANDOM"
NETWORK="vs-upgrade-test-$SUFFIX"
BROKER="vs-upgrade-broker-$SUFFIX"
TEST_ROOT=$(mktemp -d)
ROOT="$TEST_ROOT/root"
DATA="$TEST_ROOT/data"
MDIR="$ROOT/config/mosquitto-auth"
PDIR="$DATA/mosquitto"
if [ "$IS_ROOT" -eq 1 ]; then AUTH_SRC="$MDIR"; else AUTH_SRC="$BROKER-auth"; fi

cleanup() {
    docker rm -f "$BROKER" >/dev/null 2>&1 || true
    docker network rm "$NETWORK" >/dev/null 2>&1 || true
    [ "$IS_ROOT" -eq 1 ] || docker volume rm "$AUTH_SRC" >/dev/null 2>&1 || true
    rm -rf -- "$TEST_ROOT" 2>/dev/null || \
        docker run --rm -v "$TEST_ROOT:/t" --entrypoint /bin/sh "$IMAGE" -c 'rm -rf /t/* /t/.[!.]*' \
            >/dev/null 2>&1 || true
    rm -rf -- "$TEST_ROOT" 2>/dev/null || true
}
trap cleanup EXIT

ADMIN_USER=boatadmin
ADMIN_PASS=$(openssl rand -hex 16)
COUNT=0
fail() { echo "FAIL: $*" >&2; docker logs "$BROKER" >&2 2>&1 || true; exit 1; }
pass() { COUNT=$((COUNT + 1)); }

client() { docker run --rm --network "$NETWORK" "$IMAGE" "$@"; }
can_read() { client mosquitto_sub -h "$BROKER" -u "$1" -P "$2" -t "$3" -C 1 -W 3 >/dev/null 2>&1; }
refused() { ! client mosquitto_pub -h "$BROKER" -u "$1" -P "$2" -t refused/test -m x -q 1 >/dev/null 2>&1; }
wait_ready() {
    for _ in $(seq 1 30); do
        client mosquitto_sub -h "$BROKER" -u "$ADMIN_USER" -P "$ADMIN_PASS" -t '$SYS/broker/version' \
            -C 1 -W 2 >/dev/null 2>&1 && return 0
        sleep 1
    done
    fail "broker did not become ready"
}
secret() {
    python3 - "$ROOT/config/displays.secrets.json" "$@" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(d["bridge"]["mqtt_password"] if sys.argv[2] == "bridge"
      else d["displays"][sys.argv[3]]["mqtt_password"])
PY
}

docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull "$IMAGE" >/dev/null
docker network create "$NETWORK" >/dev/null

# --- 1. The release-1.3.0 broker -------------------------------------------------
install -d -m 0755 "$ROOT/config"
install -d -m 0750 "$DATA" "$PDIR"
cat > "$ROOT/config/mosquitto.conf" <<'CONF'
persistence true
persistence_location /mosquitto/data/
listener 1883
allow_anonymous false
password_file /mosquitto/data/passwords
CONF
chmod 0644 "$ROOT/config/mosquitto.conf"
# Exactly how the 1.3.0 installer created the administrator account.
docker run --rm -v "$PDIR:/mosquitto/data" "$IMAGE" mosquitto_passwd -b -c \
    /mosquitto/data/passwords "$ADMIN_USER" "$ADMIN_PASS" >/dev/null 2>&1
docker run --rm -v "$PDIR:/mosquitto/data" "$IMAGE" mosquitto_passwd -b \
    /mosquitto/data/passwords legacyonly legacy-pass >/dev/null 2>&1
docker run --rm -v "$PDIR:/mosquitto/data" --entrypoint /bin/sh "$IMAGE" -c \
    'chown 1883:1883 /mosquitto/data/passwords; chmod 0600 /mosquitto/data/passwords'
docker run -d --name "$BROKER" --network "$NETWORK" --restart unless-stopped \
    -v "$ROOT/config/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" \
    -v "$PDIR:/mosquitto/data" "$IMAGE" >/dev/null
wait_ready
can_read legacyonly legacy-pass '$SYS/broker/version' || fail "legacy account should work on the old broker"
pass

# --- 2. The new release renders its files, but does not recreate the container ----
python3 - "$ROOT/config/displays.json" <<'PY'
import json, sys
cfg = json.load(open("displays/displays.example.json"))
cfg["displays"] = {"helm": cfg["displays"]["helm"]}
json.dump(cfg, open(sys.argv[1], "w"), indent=2)
PY
LEGACY_SUM=$(sha256sum "$ROOT/config/mosquitto.conf" 2>/dev/null || shasum -a 256 "$ROOT/config/mosquitto.conf")
VESSELSTACK_ROOT="$ROOT" VESSELSTACK_DATA="$DATA" MQTT_USERNAME="$ADMIN_USER" \
    MQTT_PASSWORD="$ADMIN_PASS" DISPLAYS_ENABLE=true DISPLAYS_BRIDGE_MQTT_USERNAME=vesselstack-bridge \
    python3 displays/displayctl.py sync-mqtt --compose templates/compose.yml \
    --container "$BROKER" >/dev/null || fail "sync-mqtt failed"
# What install.sh does for the configuration (see install.sh: MOSQUITTO_CONF_DIR).
install -d -m 0755 "$ROOT/config/mosquitto"
install -m 0644 templates/mosquitto.conf "$ROOT/config/mosquitto/mosquitto.conf"
[ "$(sha256sum "$ROOT/config/mosquitto.conf" 2>/dev/null || shasum -a 256 "$ROOT/config/mosquitto.conf")" = "$LEGACY_SUM" ] \
    || fail "the legacy config/mosquitto.conf was modified"
grep -q '^acl_file' "$ROOT/config/mosquitto/mosquitto.conf" || fail "new configuration lacks the ACL"
pass

# Docker Desktop (macOS) cannot chown bind mounts: give the broker the real ownership via a volume.
emulate_ownership() {
    [ "$IS_ROOT" -eq 1 ] && return 0
    docker volume create "$AUTH_SRC" >/dev/null
    local attempt
    for attempt in 1 2 3 4 5; do
        docker run --rm -v "$AUTH_SRC:/auth" -v "$MDIR:/src:ro" --entrypoint /bin/sh "$IMAGE" -c \
            'cp /src/acl /src/passwords /auth/ && chown 0:1883 /auth && chmod 0750 /auth && chown 1883:1883 /auth/acl /auth/passwords && chmod 0600 /auth/acl /auth/passwords' \
            >/dev/null 2>&1 && break
        [ "$attempt" -lt 5 ] || fail "could not copy the auth files into the volume"
        sleep 1
    done
}
emulate_ownership

# --- 3. Reboot: the old container restarts and must not notice the new files -------
docker restart "$BROKER" >/dev/null
wait_ready
[ "$(docker inspect -f '{{.State.Running}}' "$BROKER")" = true ] || fail "the old broker did not restart"
can_read legacyonly legacy-pass '$SYS/broker/version' || fail "the old broker lost its legacy configuration"
refused vsdisp-helm "$(secret display helm)" || fail "the new display account must not work on the old container"
if docker logs "$BROKER" 2>&1 | grep -Ei 'unable to open|error opening|password file|acl file' \
    | grep -Evi 'warning' | grep -q .; then
    docker logs "$BROKER" >&2
    fail "the old broker logged a configuration error after a restart"
fi
pass

# --- 4. Recreate from the real templates/compose.yml mosquitto mounts ------------
docker rm -f "$BROKER" >/dev/null
VOLUMES=$(VESSELSTACK_ROOT="$ROOT" VESSELSTACK_DATA="$DATA" \
    docker compose --env-file vesselstack.example.env -f templates/compose.yml config --format json \
    | jq -r '.services.mosquitto.volumes[] | "\(.source):\(.target)" + (if .read_only then ":ro" else "" end)')
[ "$(wc -l <<<"$VOLUMES" | tr -d ' ')" -eq 3 ] || fail "expected three mosquitto volumes in compose.yml: $VOLUMES"
grep -q ":/mosquitto/config:ro$" <<<"$VOLUMES" || fail "compose.yml does not mount config/mosquitto as a directory"
ARGS=()
while IFS= read -r volume; do
    case "$volume" in
        "$MDIR":*) volume="$AUTH_SRC:${volume#"$MDIR":}" ;;
    esac
    ARGS+=(-v "$volume")
done <<<"$VOLUMES"
docker run -d --name "$BROKER" --network "$NETWORK" --restart unless-stopped "${ARGS[@]}" "$IMAGE" >/dev/null
wait_ready
HELM_PASS=$(secret display helm)
BRIDGE_PASS=$(secret bridge)
can_read "$ADMIN_USER" "$ADMIN_PASS" '$SYS/broker/version' || fail "administrator must still work"
refused legacyonly legacy-pass || fail "accounts that exist only in the legacy password file must be gone"
client mosquitto_pub -h "$BROKER" -u vsdisp-helm -P "$HELM_PASS" \
    -t vesselstack/v1/displays/vsdisp-helm/status -m online -r -q 1 >/dev/null 2>&1 \
    || fail "the display account should publish its own status"
[ "$(client mosquitto_sub -h "$BROKER" -u "$ADMIN_USER" -P "$ADMIN_PASS" \
    -t vesselstack/v1/displays/vsdisp-helm/status -C 1 -W 3 2>/dev/null)" = online ] \
    || fail "the display status was not stored"
client mosquitto_pub -h "$BROKER" -u vsdisp-helm -P "$HELM_PASS" -t homeassistant/forbidden \
    -m x -r -q 1 >/dev/null 2>&1 || true
[ -z "$(client mosquitto_sub -h "$BROKER" -u "$ADMIN_USER" -P "$ADMIN_PASS" \
    -t homeassistant/forbidden -C 1 -W 2 2>/dev/null)" ] || fail "the ACL is not active: a display wrote outside its topic"
can_read vesselstack-bridge "$BRIDGE_PASS" vesselstack/v1/displays/vsdisp-helm/status \
    || fail "the bridge account should read display status"
! docker exec "$BROKER" touch /mosquitto/auth/planted >/dev/null 2>&1 || fail "auth directory is writable in the broker"
pass

# --- 5. The recreated container also survives a restart ---------------------------
docker restart "$BROKER" >/dev/null
wait_ready
can_read vsdisp-helm "$HELM_PASS" vesselstack/v1/bridge/status || true
client mosquitto_pub -h "$BROKER" -u vsdisp-helm -P "$HELM_PASS" \
    -t vesselstack/v1/displays/vsdisp-helm/status -m online2 -r -q 1 >/dev/null 2>&1 \
    || fail "the display account should still work after a restart"
pass

echo "mosquitto upgrade checks passed ($COUNT groups)"
