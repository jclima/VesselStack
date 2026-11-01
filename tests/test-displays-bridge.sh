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
HA="vs-bridge-ha-$SUFFIX"
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
    docker rm -f "$BROKER" "$SIGNALK" "$HA" "$BRIDGE" >/dev/null 2>&1 || true
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
HA_TOKEN=$(openssl rand -hex 16)
HA_BAD_TOKEN=$(openssl rand -hex 16)
COUNT=0
fail() { echo "FAIL: $*" >&2; docker logs "$BRIDGE" >&2 2>&1 || true; exit 1; }
pass() { COUNT=$((COUNT + 1)); }

# One instrument and one notification, fast heartbeat and staleness so the test stays short.
# The bridge container runs with a fixed UTC+05:30 zone (POSIX TZ syntax, so no tz database is needed)
# and the night window is built around the time it will see: it spans local now -2 h .. +2 h (it
# may cross midnight), so the retained clock must say night is on.
BRIDGE_TZ='<+0530>-5:30'
python3 - "$ROOT/config/displays.json" <<'PY'
import json, sys
from datetime import datetime, timedelta, timezone
cfg = json.load(open("displays/displays.example.json"))
local = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
cfg["night_schedule"] = {"start": (local - timedelta(hours=2)).strftime("%H:%M"),
                         "end": (local + timedelta(hours=2)).strftime("%H:%M")}
cfg["displays"]["helm"]["ui"] = {"header": True, "night": "schedule"}
cfg["heartbeat_s"] = 1
cfg["instruments"] = {"sog": dict(cfg["instruments"]["sog"], stale_after_s=3, min_interval_ms=100)}
cfg["notifications"] = {"anchor": {"path": "notifications.navigation.anchor", "label": "Anchor"}}
cfg["displays"]["helm"]["pages"] = [{"title": "Nav", "layout": "grid-2x2", "items": ["sog"]},
                                    {"title": "Status", "layout": "status-grid", "items": ["sog", "anchor"]}]
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

# The pinned, hash-checked requirements are installed exactly the way install.sh
# does it: with the same helper, from a shell running under install.sh's
# "umask 077", as root. Everything below then runs as an unprivileged uid
# (like the service user), so an unreadable environment fails this test.
UNPRIV=(--user 1000:1000)
docker volume create "$VENV_VOL" >/dev/null
docker run --rm -v "$VENV_VOL:/venv" -v "$REPO_ROOT/scripts/install-displays-venv.sh:/install-venv.sh:ro" \
    -v "$REPO_ROOT/displays/requirements.txt:/requirements.txt:ro" "$PYIMAGE" \
    bash -c 'umask 077 && bash /install-venv.sh /venv /requirements.txt' >/dev/null \
    || fail "could not install displays/requirements.txt with install-displays-venv.sh"
[ -z "$(docker run --rm "${UNPRIV[@]}" -v "$VENV_VOL:/venv:ro" "$PYIMAGE" \
    sh -c 'find /venv ! -perm -o=r; find /venv -type d ! -perm -o=x; /venv/bin/python -c "import paho.mqtt.client, websockets"' 2>&1)" ] \
    || fail "the environment is not readable and importable by an unprivileged user"
# Re-running with the requirements satisfied needs no network (offline re-apply).
docker run --rm --network none -v "$VENV_VOL:/venv" \
    -v "$REPO_ROOT/scripts/install-displays-venv.sh:/install-venv.sh:ro" \
    -v "$REPO_ROOT/displays/requirements.txt:/requirements.txt:ro" "$PYIMAGE" \
    bash -c 'umask 077 && bash /install-venv.sh /venv /requirements.txt' >/dev/null 2>&1 \
    || fail "re-running the venv helper offline failed although the requirements are satisfied"
pass

# Run the publisher regressions in the exact venv used by the bridge.  These are
# intentionally a hard gate here: the host's dependency-free unit run may skip
# them, but the integration image must prove the pinned Paho 2.1.0 callback and
# publish behavior.
docker run --rm --network none "${UNPRIV[@]}" -v "$REPO_ROOT:/source:ro" -v "$VENV_VOL:/venv:ro" "$PYIMAGE" \
    /venv/bin/python /source/tests/test_mqtt_publisher.py 2>&1 | tee "$TEST_ROOT/mqtt-publisher.log"
grep -q 'Ran [0-9][0-9]* tests' "$TEST_ROOT/mqtt-publisher.log" \
    || fail "publisher regression suite did not run"
! grep -q 'skipped=' "$TEST_ROOT/mqtt-publisher.log" \
    || fail "publisher regression suite skipped a critical Paho case"
pass

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

# run_bridge [ENV_FILE [CONFIG_FILE]]
run_bridge() {
    docker rm -f "$BRIDGE" >/dev/null 2>&1 || true
    docker run -d --name "$BRIDGE" --network "$NETWORK" --env-file "${1:-$DOCKER_ENV}" "${UNPRIV[@]}" \
        -e PYTHONDONTWRITEBYTECODE=1 -e "TZ=$BRIDGE_TZ" \
        -v "$REPO_ROOT/displays:/app/displays:ro" -v "${2:-$ROOT/config/displays.json}:/cfg/displays.json:ro" \
        -v "$VENV_VOL:/venv:ro" "$PYIMAGE" /venv/bin/python /app/displays/bridge.py >/dev/null
}

# 1. The bridge starts while SignalK is still down: status online, signalk
# disconnected, instrument missing (all from the real broker, as a display).
run_bridge
wait_for vesselstack/v1/bridge/status '^online$' 20
wait_for vesselstack/v1/bridge/signalk '^disconnected$' 10
wait_for vesselstack/v1/instruments/sog '"st":"missing"' 10
wait_for vesselstack/v1/notifications/anchor '"v":null,"u":"","st":"missing","sev":"unknown"' 10
# Alarm summary: the one counted item (the notification) is unknown, retained, ids only.
wait_for vesselstack/v1/alarms/summary '^\{"max":"unknown","na":0,"nw":0,"nu":1,"ids":\[\],"ts":"[0-9TZ:-]+"\}$' 10
# Bridge clock (a header display and a schedule are configured): retained, exact shape, the
# bridge's local wall clock (UTC+05:30 here) and the night flag of the active window.
wait_for vesselstack/v1/bridge/clock '^\{"hm":"[0-2][0-9]:[0-5][0-9]","night":true,"ts":"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"\}$' 10
clock=$(read1 "$HELM" "$HELM_PASS" vesselstack/v1/bridge/clock)
python3 - "$clock" <<'PY' || fail "the bridge clock does not show the local time of the bridge's zone: $clock"
import json, sys
from datetime import datetime, timedelta, timezone
body = json.loads(sys.argv[1])
assert list(body) == ["hm", "night", "ts"], body
want = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
got = datetime.strptime(body["hm"], "%H:%M")
diff = abs((got.hour * 60 + got.minute) - (want.hour * 60 + want.minute)) % 1440
assert min(diff, 1440 - diff) <= 1, (body["hm"], want.strftime("%H:%M"))
PY
# The clock is retained (a late subscriber gets it at once) and read-only for displays.
client mosquitto_pub -h "$BROKER" -u "$HELM" -P "$HELM_PASS" \
    -t vesselstack/v1/bridge/clock -m '{"hm":"00:00","night":false,"ts":"forged"}' -r -q 1 >/dev/null 2>&1 || true
[ "$(read1 "$HELM" "$HELM_PASS" vesselstack/v1/bridge/clock | grep -c forged)" = 0 ] \
    || fail "a display account could publish the bridge clock"
pass

# 2. SignalK appears (requires the bearer token), streams 10 kn for ~8 s, then exits.
docker run -d --name "$SIGNALK" --network "$NETWORK" "${UNPRIV[@]}" \
    -v "$REPO_ROOT/tests/fixtures:/fx:ro" -v "$VENV_VOL:/venv:ro" "$PYIMAGE" \
    /venv/bin/python /fx/fake_signalk.py --port 3000 --duration 32 --alarm-at 8 --clear-at 18 --token "$SK_TOKEN" >/dev/null
wait_for vesselstack/v1/bridge/signalk '^connected$' 40
wait_for vesselstack/v1/instruments/sog '"v":10\.0,"u":"kn","st":"ok"' 15
# The notification is "not raised" (ok) once SignalK is connected, then follows the fake's
# alarm and clear; the message text never reaches MQTT.
wait_for vesselstack/v1/notifications/anchor '"st":"ok","sev":"ok"' 15
wait_for vesselstack/v1/alarms/summary '"max":"ok","na":0,"nw":0,"nu":0,"ids":\[\]' 15
wait_for vesselstack/v1/notifications/anchor '"st":"ok","sev":"alarm"' 15
wait_for vesselstack/v1/alarms/summary '"max":"alarm","na":1,"nw":0,"nu":0,"ids":\["anchor"\]' 15
[ "$(read1 "$HELM" "$HELM_PASS" vesselstack/v1/notifications/anchor | grep -c 'Anchor alarm')" = 0 ] \
    || fail "the notification message text was published"
wait_for vesselstack/v1/notifications/anchor '"st":"ok","sev":"ok"' 25
wait_for vesselstack/v1/alarms/summary '"max":"ok","na":0,"nw":0,"nu":0,"ids":\[\]' 15
[ "$(read1 "$HELM" "$HELM_PASS" vesselstack/v1/alarms/summary | grep -c 'Anchor')" = 0 ] \
    || fail "the alarm summary carried a label or message text"
pass

# 3. ACL: the bridge cannot publish to a display's status topic; the display can.
client mosquitto_pub -h "$BROKER" -u "$BRIDGE_USER" -P "$BRIDGE_PASS" \
    -t "vesselstack/v1/displays/$HELM/status" -m forged-by-bridge -r -q 1 >/dev/null 2>&1 || true
[ -z "$(read1 "$ADMIN_USER" "$ADMIN_PASS" "vesselstack/v1/displays/$HELM/status")" ] \
    || fail "the bridge account could publish to a display status topic"
client mosquitto_pub -h "$BROKER" -u "$HELM" -P "$HELM_PASS" \
    -t vesselstack/v1/alarms/forged -m '{"max":"alarm"}' -r -q 1 >/dev/null 2>&1 || true
[ -z "$(read1 "$ADMIN_USER" "$ADMIN_PASS" vesselstack/v1/alarms/forged)" ] \
    || fail "a display account could publish under vesselstack/v1/alarms"
client mosquitto_pub -h "$BROKER" -u "$HELM" -P "$HELM_PASS" \
    -t "vesselstack/v1/displays/$HELM/status" -m online -r -q 1 >/dev/null 2>&1 || true
[ "$(read1 "$ADMIN_USER" "$ADMIN_PASS" "vesselstack/v1/displays/$HELM/status")" = online ] \
    || fail "the display account could not publish its own status"
pass

# 4. The fake SignalK exits: signalk disconnected, sog becomes stale (stale_after_s=3
# plus one heartbeat), value cleared.
wait_for vesselstack/v1/bridge/signalk '^disconnected$' 30
wait_for vesselstack/v1/instruments/sog '"v":null,"u":"kn","st":"stale"' 15
wait_for vesselstack/v1/notifications/anchor '"v":null,"u":"","st":"stale","sev":"unknown"' 15
wait_for vesselstack/v1/alarms/summary '"max":"unknown","na":0,"nw":0,"nu":1,"ids":\[\]' 15
[ "$(read1 "$HELM" "$HELM_PASS" vesselstack/v1/bridge/status)" = online ] \
    || fail "bridge status should stay online while only SignalK is down"
pass

# 5. SignalK returns: the bridge reconnects by itself and values flow again.
docker rm -f "$SIGNALK" >/dev/null 2>&1 || true
docker run -d --name "$SIGNALK" --network "$NETWORK" "${UNPRIV[@]}" \
    -v "$REPO_ROOT/tests/fixtures:/fx:ro" -v "$VENV_VOL:/venv:ro" "$PYIMAGE" \
    /venv/bin/python /fx/fake_signalk.py --port 3000 --duration 40 --token "$SK_TOKEN" >/dev/null
wait_for vesselstack/v1/bridge/signalk '^connected$' 60
wait_for vesselstack/v1/instruments/sog '"v":10\.0,"u":"kn","st":"ok"' 15
pass

# 6. Killing the bridge (no clean shutdown) fires the retained last will.
docker kill "$BRIDGE" >/dev/null
wait_for vesselstack/v1/bridge/status '^offline$' 30
pass

# 7. A graceful SIGTERM publishes offline itself and exits 0, with no secrets logged. With
# SignalK connected the summary is "ok" first; the clean stop leaves "unknown", not a stale "ok".
docker rm -f "$SIGNALK" >/dev/null 2>&1 || true
docker run -d --name "$SIGNALK" --network "$NETWORK" "${UNPRIV[@]}" \
    -v "$REPO_ROOT/tests/fixtures:/fx:ro" -v "$VENV_VOL:/venv:ro" "$PYIMAGE" \
    /venv/bin/python /fx/fake_signalk.py --port 3000 --duration 90 --alarm-at 1000 --clear-at 1001 --token "$SK_TOKEN" >/dev/null
run_bridge
wait_for vesselstack/v1/bridge/status '^online$' 20
wait_for vesselstack/v1/alarms/summary '"max":"ok","na":0,"nw":0,"nu":0' 40
docker stop -t 15 "$BRIDGE" >/dev/null
[ "$(docker inspect -f '{{.State.ExitCode}}' "$BRIDGE")" = 0 ] || fail "SIGTERM did not exit 0"
wait_for vesselstack/v1/bridge/status '^offline$' 10
wait_for vesselstack/v1/bridge/signalk '^disconnected$' 10
wait_for vesselstack/v1/alarms/summary '"max":"unknown","na":0,"nw":0,"nu":1' 10
logs=$(docker logs "$BRIDGE" 2>&1)
for secret in "$SK_TOKEN" "$BRIDGE_PASS"; do
    ! grep -Fq "$secret" <<<"$logs" || fail "a secret appeared in the bridge log"
done
grep -q 'starting: 1 instrument' <<<"$logs" || fail "bridge log is missing the startup summary"
pass

# 8. An invalid displays.json stops the bridge with a clear message and a non-zero exit.
echo '{"schema": 1}' > "$TEST_ROOT/bad.json"
code=0
out=$(docker run --rm "${UNPRIV[@]}" --env-file "$DOCKER_ENV" -e DISPLAYS_CONFIG=/cfg/bad.json \
    -v "$REPO_ROOT/displays:/app/displays:ro" -v "$TEST_ROOT/bad.json:/cfg/bad.json:ro" \
    -v "$VENV_VOL:/venv:ro" "$PYIMAGE" /venv/bin/python -B /app/displays/bridge.py 2>&1) || code=$?
[ "$code" -eq 2 ] || fail "invalid config exited $code, expected 2"
grep -q 'invalid displays configuration' <<<"$out" || fail "invalid config message missing"
pass

# 9. Home Assistant items, against a fake Home Assistant websocket server (not a real one). The
# configuration gains two binary items; SignalK keeps running throughout.
HA_CONFIG="$TEST_ROOT/displays-ha.json"
python3 - "$ROOT/config/displays.json" "$HA_CONFIG" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
cfg["ha"] = {
    "bilge": {"entity_id": "binary_sensor.vesselstack_bilge_pump", "label": "Bilge", "kind": "binary",
              "states": [{"match": "on", "sev": "warn", "text": "RUNNING"}, {"match": "off", "sev": "ok", "text": "OFF"}]},
    "high_water": {"entity_id": "binary_sensor.vesselstack_high_water", "label": "High water", "kind": "binary",
                   "states": [{"match": "on", "sev": "alarm", "text": "HIGH"}, {"match": "off", "sev": "ok", "text": "DRY"}]},
}
cfg["displays"]["helm"]["pages"][1]["items"] = ["sog", "anchor", "bilge", "high_water"]
json.dump(cfg, open(sys.argv[2], "w"), indent=2)
PY
ha_env() { # ha_env FILE TOKEN : the bridge environment with the Home Assistant settings appended
    cp "$DOCKER_ENV" "$1"
    printf 'HOME_ASSISTANT_URL=http://%s:8123\nDISPLAYS_HA_TOKEN=%s\n' "$HA" "$2" >> "$1"
    chmod 0600 "$1"
}
start_signalk() {
    docker rm -f "$SIGNALK" >/dev/null 2>&1 || true
    docker run -d --name "$SIGNALK" --network "$NETWORK" "${UNPRIV[@]}" \
        -v "$REPO_ROOT/tests/fixtures:/fx:ro" -v "$VENV_VOL:/venv:ro" "$PYIMAGE" \
        /venv/bin/python /fx/fake_signalk.py --port 3000 --duration 400 --alarm-at 1000 --clear-at 1001 \
        --token "$SK_TOKEN" >/dev/null
}
start_ha() { # start_ha [fake_homeassistant.py options...]
    docker rm -f "$HA" >/dev/null 2>&1 || true
    docker run -d --name "$HA" --network "$NETWORK" "${UNPRIV[@]}" \
        -v "$REPO_ROOT/tests/fixtures:/fx:ro" -v "$VENV_VOL:/venv:ro" "$PYIMAGE" \
        /venv/bin/python /fx/fake_homeassistant.py --port 8123 --token "$HA_TOKEN" "$@" >/dev/null
}
start_signalk

# 9a. A wrong token: Home Assistant is "disabled", the bridge keeps running, SignalK items stay ok,
# and neither token is ever logged.
start_ha --state binary_sensor.vesselstack_bilge_pump=on
ha_env "$TEST_ROOT/ha-bad.env" "$HA_BAD_TOKEN"
run_bridge "$TEST_ROOT/ha-bad.env" "$HA_CONFIG"
wait_for vesselstack/v1/bridge/homeassistant '^disabled$' 40
wait_for vesselstack/v1/bridge/signalk '^connected$' 40
wait_for vesselstack/v1/instruments/sog '"v":10\.0,"u":"kn","st":"ok"' 15
wait_for vesselstack/v1/ha/bilge '"v":null,"k":null,"u":"","st":"missing","sev":"unknown"' 10
[ "$(read1 "$HELM" "$HELM_PASS" vesselstack/v1/bridge/status)" = online ] || fail "the bridge went offline after a bad Home Assistant token"
logs=$(docker logs "$BRIDGE" 2>&1)
grep -q 'rejected the access token' <<<"$logs" || fail "the bad token was not reported"
for secret in "$HA_BAD_TOKEN" "$HA_TOKEN" "$SK_TOKEN" "$BRIDGE_PASS"; do
    ! grep -Fq "$secret" <<<"$logs" || fail "a secret appeared in the bridge log (bad token)"
    ! docker logs "$HA" 2>&1 | grep -Fq "$secret" || fail "a secret appeared in the fake Home Assistant log"
done
pass

# 9b. A good token: the bilge pump runs (warn, k 0) and stops (ok, k 1); the high water switch
# trips later and the alarm summary counts it; then Home Assistant goes away (stale) while the
# SignalK items stay ok.
start_ha --state binary_sensor.vesselstack_bilge_pump=on --state binary_sensor.vesselstack_bilge_pump=off@20 \
    --state binary_sensor.vesselstack_high_water=off --state binary_sensor.vesselstack_high_water=on@40 \
    --duration 62
ha_env "$TEST_ROOT/ha-good.env" "$HA_TOKEN"
docker rm -f "$BRIDGE" >/dev/null 2>&1 || true
start_signalk   # the fake SignalK serves one connection, then stops: a fresh one for every bridge run
run_bridge "$TEST_ROOT/ha-good.env" "$HA_CONFIG"
wait_for vesselstack/v1/bridge/homeassistant '^connected$' 40
wait_for vesselstack/v1/bridge/signalk '^connected$' 40
wait_for vesselstack/v1/ha/bilge '"v":null,"k":0,"u":"","st":"ok","sev":"warn"' 20
wait_for vesselstack/v1/ha/high_water '"k":1,"u":"","st":"ok","sev":"ok"' 20
[ "$(read1 "$HELM" "$HELM_PASS" vesselstack/v1/ha/bilge | grep -ci 'RUNNING\|binary_sensor\|"on"')" = 0 ] \
    || fail "a Home Assistant state or text was published"
wait_for vesselstack/v1/ha/bilge '"k":1,"u":"","st":"ok","sev":"ok"' 30
wait_for vesselstack/v1/ha/high_water '"k":0,"u":"","st":"ok","sev":"alarm"' 30
# (d) the alarm summary includes the Home Assistant alarm
wait_for vesselstack/v1/alarms/summary '"max":"alarm","na":1,"nw":0,"nu":0,"ids":\["high_water"\]' 15
# (c) Home Assistant stops: its items are stale, SignalK is untouched
wait_for vesselstack/v1/bridge/homeassistant '^disconnected$' 40
wait_for vesselstack/v1/ha/bilge '"v":null,"k":null,"u":"","st":"stale","sev":"unknown"' 15
wait_for vesselstack/v1/ha/high_water '"st":"stale","sev":"unknown"' 15
wait_for vesselstack/v1/instruments/sog '"v":10\.0,"u":"kn","st":"ok"' 10
wait_for vesselstack/v1/alarms/summary '"max":"unknown"' 15
logs=$(docker logs "$BRIDGE" 2>&1)
for secret in "$HA_TOKEN" "$SK_TOKEN" "$BRIDGE_PASS"; do
    ! grep -Fq "$secret" <<<"$logs" || fail "a secret appeared in the bridge log (good token)"
done
pass

# 9c. A Home Assistant without subscribe_entities: the client falls back to get_states + state_changed events.
start_ha --no-subscribe-entities --state binary_sensor.vesselstack_bilge_pump=on \
    --state binary_sensor.vesselstack_bilge_pump=off@20 --state binary_sensor.vesselstack_high_water=off --duration 50
docker rm -f "$BRIDGE" >/dev/null 2>&1 || true
start_signalk
run_bridge "$TEST_ROOT/ha-good.env" "$HA_CONFIG"
wait_for vesselstack/v1/bridge/homeassistant '^connected$' 40
wait_for vesselstack/v1/ha/bilge '"k":0,"u":"","st":"ok","sev":"warn"' 20
wait_for vesselstack/v1/ha/bilge '"k":1,"u":"","st":"ok","sev":"ok"' 30
hlogs=$(docker logs "$HA" 2>&1)
grep -q 'subscribe_entities (refused)' <<<"$hlogs" || fail "the fallback test did not refuse subscribe_entities"
grep -q 'command: get_states' <<<"$hlogs" || fail "the client did not fall back to get_states"
grep -q 'command: subscribe_events (state_changed)' <<<"$hlogs" || fail "the client did not subscribe to state_changed events"
docker stop -t 15 "$BRIDGE" >/dev/null
[ "$(docker inspect -f '{{.State.ExitCode}}' "$BRIDGE")" = 0 ] || fail "SIGTERM with Home Assistant items did not exit 0"
pass

echo "displays bridge integration checks passed ($COUNT groups)"
