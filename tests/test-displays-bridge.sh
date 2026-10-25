#!/bin/bash
# Docker integration test for the displays bridge: real Mosquitto with the real
# ACL from `displayctl sync-mqtt`, a fake SignalK websocket server, and
# displays/bridge.py running in a python container with the pinned requirements.
# On Linux run as root (sudo), exactly like tests/test-displays-mosquitto.sh; on
# macOS a root container copies the synced auth files into a Docker volume with
# the real broker ownership.

# shellcheck disable=SC2016  # regexes are intentionally literal
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$REPO_ROOT"
IMAGE=$(sed -n 's/^ *image: \(eclipse-mosquitto:[^ ]*\) *$/\1/p' "$REPO_ROOT/templates/compose.yml" | head -n 1)
PYIMAGE=${BRIDGE_TEST_PYTHON_IMAGE:-python:3.11-slim}
[ -n "$IMAGE" ] || { echo "Cannot find pinned Mosquitto image" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
IS_ROOT=0
[ "$(id -u)" -eq 0 ] && IS_ROOT=1
if [ "$IS_ROOT" -eq 0 ] && [ "$(uname -s)" = Linux ]; then
    echo "Run with sudo on Linux so broker-owned files can be written" >&2
    exit 1
fi

SUFFIX="$$-$RANDOM"
NETWORK="vs-bridge-test-$SUFFIX"
BROKER="vs-bridge-broker-$SUFFIX"
SIGNALK="vs-bridge-signalk-$SUFFIX"
BRIDGE="vs-bridge-bridge-$SUFFIX"
VENV_VOL="vs-bridge-venv-$SUFFIX"
TEST_ROOT=$(mktemp -d)
ROOT="$TEST_ROOT/root"
DATA="$TEST_ROOT/data"
MDIR="$ROOT/config/mosquitto-auth"
PDIR="$DATA/mosquitto"
install -d -m 0755 "$ROOT/config" "$ROOT/displays"
install -d -m 0750 "$DATA" "$PDIR"
install -m 0644 "$REPO_ROOT/templates/compose.yml" "$ROOT/compose.yml"
install -d -m 0755 "$ROOT/config/mosquitto"
install -m 0644 "$REPO_ROOT/templates/mosquitto.conf" "$ROOT/config/mosquitto/mosquitto.conf"

if [ "$IS_ROOT" -eq 1 ]; then AUTH_SRC="$MDIR"; else AUTH_SRC="$BROKER-auth"; fi

cleanup() {
    docker rm -f "$BROKER" "$SIGNALK" "$BRIDGE" >/dev/null 2>&1 || true
    docker network rm "$NETWORK" >/dev/null 2>&1 || true
    docker volume rm "$VENV_VOL" >/dev/null 2>&1 || true
    [ "$IS_ROOT" -eq 1 ] || docker volume rm "$AUTH_SRC" >/dev/null 2>&1 || true
    rm -rf -- "$TEST_ROOT" 2>/dev/null || \
        docker run --rm -v "$TEST_ROOT:/t" --entrypoint /bin/sh "$IMAGE" -c 'rm -rf /t/* /t/.[!.]*' \
            >/dev/null 2>&1 || true
    rm -rf -- "$TEST_ROOT" 2>/dev/null || true
}
trap cleanup EXIT

ADMIN_USER=boatadmin
ADMIN_PASS=$(openssl rand -hex 16)
SK_TOKEN=$(openssl rand -hex 16)
COUNT=0
fail() { echo "FAIL: $*" >&2; docker logs "$BRIDGE" >&2 2>&1 || true; exit 1; }
pass() { COUNT=$((COUNT + 1)); }

# One instrument, fast heartbeat and staleness so the test stays short.
python3 - "$ROOT/config/displays.json" <<'PY'
import json, sys
cfg = json.load(open("displays/displays.example.json"))
cfg["heartbeat_s"] = 1
cfg["instruments"] = {"sog": dict(cfg["instruments"]["sog"], stale_after_s=3, min_interval_ms=100)}
cfg["displays"]["helm"]["pages"] = [{"title": "Nav", "layout": "grid-2x2", "items": ["sog"]}]
json.dump(cfg, open(sys.argv[1], "w"), indent=2)
PY
VESSELSTACK_ROOT="$ROOT" VESSELSTACK_DATA="$DATA" MQTT_USERNAME="$ADMIN_USER" \
    MQTT_PASSWORD="$ADMIN_PASS" DISPLAYS_ENABLE=true \
    DISPLAYS_BRIDGE_MQTT_USERNAME=vesselstack-bridge \
    python3 displays/displayctl.py sync-mqtt --container "$BROKER" >/dev/null \
    || fail "sync-mqtt failed"

secret() { # secret bridge | secret display ID
    python3 - "$ROOT/config/displays.secrets.json" "$@" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(d["bridge"]["mqtt_password"] if sys.argv[2] == "bridge"
      else d["displays"][sys.argv[3]]["mqtt_password"])
PY
}
BRIDGE_USER=vesselstack-bridge; BRIDGE_PASS=$(secret bridge)
HELM=vsdisp-helm; HELM_PASS=$(secret display helm)

if [ "$IS_ROOT" -eq 0 ]; then
    docker volume create "$AUTH_SRC" >/dev/null
    docker run --rm -v "$AUTH_SRC:/auth" -v "$MDIR:/src:ro" --entrypoint /bin/sh "$IMAGE" -c \
        'cp /src/acl /src/passwords /auth/; chown 0:1883 /auth; chmod 0750 /auth; chown 1883:1883 /auth/acl /auth/passwords; chmod 0600 /auth/acl /auth/passwords' >/dev/null
fi

client() { docker run --rm --network "$NETWORK" "$IMAGE" "$@"; }
# read1 USER PASS TOPIC : first (retained) message, empty when none within 3 s
read1() { client mosquitto_sub -h "$BROKER" -u "$1" -P "$2" -t "$3" -C 1 -W 3 2>/dev/null || true; }
# wait_for TOPIC REGEX SECONDS [USER PASS] : poll as the helm display until it matches
wait_for() {
    local topic=$1 regex=$2 seconds=$3 end last=
    end=$((SECONDS + seconds))
    while [ "$SECONDS" -lt "$end" ]; do
        last=$(read1 "$HELM" "$HELM_PASS" "$topic")
        if grep -Eq "$regex" <<<"$last"; then return 0; fi
        sleep 1
    done
    fail "$topic did not match /$regex/ within ${seconds}s (last: ${last:-none})"
}

docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull "$IMAGE" >/dev/null
docker network create "$NETWORK" >/dev/null

# Pinned requirements, installed once into a volume shared by the fake SignalK
# server and the bridge (both use the same websockets).
docker volume create "$VENV_VOL" >/dev/null
docker run --rm -v "$VENV_VOL:/venv" -v "$REPO_ROOT/displays/requirements.txt:/requirements.txt:ro" \
    "$PYIMAGE" sh -c 'python -m venv /venv && /venv/bin/pip install -q --disable-pip-version-check -r /requirements.txt' \
    >/dev/null || fail "could not install displays/requirements.txt"

docker run -d --name "$BROKER" --network "$NETWORK" \
    -v "$ROOT/config/mosquitto:/mosquitto/config:ro" \
    -v "$PDIR:/mosquitto/data" -v "$AUTH_SRC:/mosquitto/auth:ro" "$IMAGE" >/dev/null
for _ in $(seq 1 30); do
    client mosquitto_sub -h "$BROKER" -u "$ADMIN_USER" -P "$ADMIN_PASS" -t '$SYS/broker/version' \
        -C 1 -W 2 >/dev/null 2>&1 && break
    sleep 1
done
client mosquitto_sub -h "$BROKER" -u "$ADMIN_USER" -P "$ADMIN_PASS" -t '$SYS/broker/version' \
    -C 1 -W 2 >/dev/null 2>&1 || fail "broker did not become ready"

# Bridge environment exactly as install.sh renders it (displayctl bridge-env), then
# translated to a raw docker env file (systemd quoting removed) with host paths and
# hostnames replaced by their container equivalents. No secret is ever on argv.
ENV_FILE="$ROOT/config/displays-bridge.env"
SIGNALK_URL="http://$SIGNALK:3000" MQTT_PORT=1883 DISPLAYS_BRIDGE_MQTT_USERNAME="$BRIDGE_USER" \
    DISPLAYS_SIGNALK_TOKEN="$SK_TOKEN" \
    python3 displays/displayctl.py bridge-env --root "$ROOT" --output "$ENV_FILE" >/dev/null \
    || fail "bridge-env failed"
[ "$(python3 -c 'import os,sys;print(oct(os.stat(sys.argv[1]).st_mode & 0o777))' "$ENV_FILE")" = 0o600 ] \
    || fail "displays-bridge.env is not mode 0600"
pass
DOCKER_ENV="$TEST_ROOT/docker.env"
python3 - "$ENV_FILE" "$DOCKER_ENV" "DISPLAYS_CONFIG=/cfg/displays.json" "MQTT_HOST=$BROKER" <<'PY'
import json, sys
overrides = dict(arg.split("=", 1) for arg in sys.argv[3:])
values = {}
for line in open(sys.argv[1]):
    key, raw = line.rstrip("\n").split("=", 1)
    values[key] = json.loads(raw)  # systemd double-quoted form is JSON-compatible here
values.update(overrides)
with open(sys.argv[2], "w") as out:
    for key, value in values.items():
        out.write(f"{key}={value}\n")
PY
chmod 0600 "$DOCKER_ENV"

run_bridge() {
    docker rm -f "$BRIDGE" >/dev/null 2>&1 || true
    docker run -d --name "$BRIDGE" --network "$NETWORK" --env-file "$DOCKER_ENV" \
        -e PYTHONDONTWRITEBYTECODE=1 \
        -v "$REPO_ROOT/displays:/app/displays:ro" -v "$ROOT/config/displays.json:/cfg/displays.json:ro" \
        -v "$VENV_VOL:/venv:ro" "$PYIMAGE" /venv/bin/python /app/displays/bridge.py >/dev/null
}

# 1. The bridge starts while SignalK is still down: status online, signalk
# disconnected, instrument missing (all from the real broker, as a display).
run_bridge
wait_for vesselstack/v1/bridge/status '^online$' 20
wait_for vesselstack/v1/bridge/signalk '^disconnected$' 10
wait_for vesselstack/v1/instruments/sog '"st":"missing"' 10
pass

# 2. SignalK appears (requires the bearer token), streams 10 kn for ~8 s, then exits.
docker run -d --name "$SIGNALK" --network "$NETWORK" \
    -v "$REPO_ROOT/tests/fixtures:/fx:ro" -v "$VENV_VOL:/venv:ro" "$PYIMAGE" \
    /venv/bin/python /fx/fake_signalk.py --port 3000 --duration 8 --token "$SK_TOKEN" >/dev/null
wait_for vesselstack/v1/bridge/signalk '^connected$' 40
wait_for vesselstack/v1/instruments/sog '"v":10\.0,"u":"kn","st":"ok"' 15
pass

# 3. ACL: the bridge cannot publish to a display's status topic; the display can.
client mosquitto_pub -h "$BROKER" -u "$BRIDGE_USER" -P "$BRIDGE_PASS" \
    -t "vesselstack/v1/displays/$HELM/status" -m forged-by-bridge -r -q 1 >/dev/null 2>&1 || true
[ -z "$(read1 "$ADMIN_USER" "$ADMIN_PASS" "vesselstack/v1/displays/$HELM/status")" ] \
    || fail "the bridge account could publish to a display status topic"
client mosquitto_pub -h "$BROKER" -u "$HELM" -P "$HELM_PASS" \
    -t "vesselstack/v1/displays/$HELM/status" -m online -r -q 1 >/dev/null 2>&1 || true
[ "$(read1 "$ADMIN_USER" "$ADMIN_PASS" "vesselstack/v1/displays/$HELM/status")" = online ] \
    || fail "the display account could not publish its own status"
pass

# 4. The fake SignalK exits: signalk disconnected, sog becomes stale (stale_after_s=3
# plus one heartbeat), value cleared.
wait_for vesselstack/v1/bridge/signalk '^disconnected$' 30
wait_for vesselstack/v1/instruments/sog '"v":null,"u":"kn","st":"stale"' 15
[ "$(read1 "$HELM" "$HELM_PASS" vesselstack/v1/bridge/status)" = online ] \
    || fail "bridge status should stay online while only SignalK is down"
pass

# 5. SignalK returns: the bridge reconnects by itself and values flow again.
docker rm -f "$SIGNALK" >/dev/null 2>&1 || true
docker run -d --name "$SIGNALK" --network "$NETWORK" \
    -v "$REPO_ROOT/tests/fixtures:/fx:ro" -v "$VENV_VOL:/venv:ro" "$PYIMAGE" \
    /venv/bin/python /fx/fake_signalk.py --port 3000 --duration 40 --token "$SK_TOKEN" >/dev/null
wait_for vesselstack/v1/bridge/signalk '^connected$' 60
wait_for vesselstack/v1/instruments/sog '"v":10\.0,"u":"kn","st":"ok"' 15
pass

# 6. Killing the bridge (no clean shutdown) fires the retained last will.
docker kill "$BRIDGE" >/dev/null
wait_for vesselstack/v1/bridge/status '^offline$' 30
pass

# 7. A graceful SIGTERM publishes offline itself and exits 0, with no secrets logged.
run_bridge
wait_for vesselstack/v1/bridge/status '^online$' 20
docker stop -t 15 "$BRIDGE" >/dev/null
[ "$(docker inspect -f '{{.State.ExitCode}}' "$BRIDGE")" = 0 ] || fail "SIGTERM did not exit 0"
wait_for vesselstack/v1/bridge/status '^offline$' 10
wait_for vesselstack/v1/bridge/signalk '^disconnected$' 10
logs=$(docker logs "$BRIDGE" 2>&1)
for secret in "$SK_TOKEN" "$BRIDGE_PASS"; do
    ! grep -Fq "$secret" <<<"$logs" || fail "a secret appeared in the bridge log"
done
grep -q 'starting: 1 instrument' <<<"$logs" || fail "bridge log is missing the startup summary"
pass

# 8. An invalid displays.json stops the bridge with a clear message and a non-zero exit.
echo '{"schema": 1}' > "$TEST_ROOT/bad.json"
code=0
out=$(docker run --rm --env-file "$DOCKER_ENV" -e DISPLAYS_CONFIG=/cfg/bad.json \
    -v "$REPO_ROOT/displays:/app/displays:ro" -v "$TEST_ROOT/bad.json:/cfg/bad.json:ro" \
    -v "$VENV_VOL:/venv:ro" "$PYIMAGE" /venv/bin/python -B /app/displays/bridge.py 2>&1) || code=$?
[ "$code" -eq 2 ] || fail "invalid config exited $code, expected 2"
grep -q 'invalid displays configuration' <<<"$out" || fail "invalid config message missing"
pass

echo "displays bridge integration checks passed ($COUNT groups)"
