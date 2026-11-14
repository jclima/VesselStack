#!/bin/bash
# Docker integration test: the pinned Node-RED image with the pinned Dashboard 2.0 and the real
# templates/node-red/settings.js must refuse the dashboard live channel (socket.io) without the crew sign-in.
# Needs Docker and network access (npm install of the dashboard). Uses fake passphrases only.

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SETTINGS=${NODERED_SETTINGS_UNDER_TEST:-$ROOT/templates/node-red/settings.js}
DASHBOARD_VERSION=1.36.1
IMAGE=$(sed -n 's/^[[:space:]]*image:[[:space:]]*\(nodered\/node-red:[^[:space:]]*\)[[:space:]]*$/\1/p' "$ROOT/templates/compose.yml" | head -n 1)
[ -n "$IMAGE" ] || { echo "cannot read the Node-RED image from templates/compose.yml" >&2; exit 1; }

WORK=$(mktemp -d)
CTR="vs-nodered-auth-$$"
cleanup() {
    docker rm -f "$CTR" >/dev/null 2>&1 || true
    # the data dir is owned by uid 1000; remove it from a container so no sudo is needed
    docker run --rm -u 0:0 -v "$WORK:/w" --entrypoint sh "$IMAGE" -c 'rm -rf /w/data' >/dev/null 2>&1 || true
    rm -rf -- "$WORK" 2>/dev/null || true
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; docker logs --tail 40 "$CTR" >&2 2>&1 || true; exit 1; }

install -d -m 0755 "$WORK/data"
docker run --rm -u 0:0 -v "$WORK/data:/data" --entrypoint sh "$IMAGE" -c 'chown 1000:1000 /data'

echo "Installing @flowfuse/node-red-dashboard@$DASHBOARD_VERSION into the data dir"
docker run --rm -u 1000:1000 -v "$WORK/data:/data" -w /data --entrypoint npm "$IMAGE" \
    install --ignore-scripts --save-exact --no-audit --no-fund "@flowfuse/node-red-dashboard@$DASHBOARD_VERSION" >/dev/null
docker run --rm -u 0:0 -v "$WORK/data:/data" -v "$ROOT/tests/fixtures/nodered-dashboard-flows.json:/flows.json:ro" \
    --entrypoint sh "$IMAGE" -c 'cp /flows.json /data/flows.json && chown 1000:1000 /data/flows.json'

CREW_PASS="crew-$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
ADMIN_PASS="admin-$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
hash_of() {
    docker run --rm -u 1000:1000 --entrypoint node "$IMAGE" -e \
        "console.log(require('/usr/src/node-red/node_modules/bcryptjs').hashSync(process.argv[1], 8))" "$1"
}
umask 077
{
    echo "NODERED_ADMIN_PASSWORD_HASH=$(hash_of "$ADMIN_PASS")"
    echo "NODERED_DASHBOARD_PASSWORD_HASH=$(hash_of "$CREW_PASS")"
    echo "NODERED_CREDENTIAL_SECRET=test-secret-$$"
} > "$WORK/nodered.env"
chmod 0600 "$WORK/nodered.env"
umask 022

PORT=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
docker run -d --name "$CTR" -u 1000:1000 --init -p "127.0.0.1:$PORT:1880" --env-file "$WORK/nodered.env" \
    -v "$WORK/data:/data" -v "$SETTINGS:/vesselstack/settings.js:ro" \
    "$IMAGE" --settings /vesselstack/settings.js >/dev/null

for _ in $(seq 1 90); do
    docker logs "$CTR" 2>&1 | grep -q 'Created socket.io server' && break
    [ "$(docker inspect -f '{{.State.Running}}' "$CTR")" = true ] || fail "Node-RED exited"
    sleep 1
done
docker logs "$CTR" 2>&1 | grep -q 'Created socket.io server' || fail "Node-RED did not create the dashboard socket.io server"

BASE="http://127.0.0.1:$PORT"
for path in /flows /dashboard /dashboard/_setup; do
    code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$BASE$path")
    [ "$code" = 401 ] || fail "$path returned $code without credentials, expected 401"
done
echo "HTTP routes require the sign-in"

CREW_PASS=$CREW_PASS python3 -I - "$BASE/dashboard/socket.io/" <<'PY'
import base64, json, os, sys, urllib.error, urllib.request

BASE = sys.argv[1]
GOOD = os.environ["CREW_PASS"]


def req(url, data=None, auth=None, origin=None):
    r = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if auth:
        r.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    if origin:
        r.add_header("Origin", origin)
    if data:
        r.add_header("Content-Type", "text/plain;charset=UTF-8")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def session(auth=None, origin=None):
    st, body = req(BASE + "?EIO=4&transport=polling", auth=auth, origin=origin)
    if st != 200:
        return "handshake-%d" % st, body
    sid = json.loads(body[1:])["sid"]
    q = BASE + "?EIO=4&transport=polling&sid=" + sid
    req(q, data=b"40", auth=auth, origin=origin)
    _, first = req(q, auth=auth, origin=origin)
    seen = first
    if first.startswith('40{"sid"'):
        for _ in range(4):
            seen += req(q, auth=auth, origin=origin)[1]
            if "ui-config" in seen or "SECRET-WIDGET-VALUE" in seen:
                break
    return first, seen


def check_refused(label, **kw):
    first, seen = session(**kw)
    assert first.startswith("44") and "crew sign-in required" in first, "%s: expected a refusal, got %r" % (label, first[:80])
    assert "ui-config" not in seen and "SECRET-WIDGET-VALUE" not in seen, "%s: leaked widget data" % label
    print("refused:", label)


check_refused("no auth")
check_refused("wrong password", auth="crew:wrong-password-xx")
check_refused("admin user with the crew passphrase", auth="admin:" + GOOD)
first, seen = session(auth="crew:" + GOOD)
assert first.startswith('40{"sid"'), "crew was not connected: %r" % first[:80]
assert "ui-config" in seen or "SECRET-WIDGET-VALUE" in seen, "crew connected but received no dashboard data"
print("connected: crew")
first, _ = session(auth="crew:" + GOOD, origin="http://evil.example")
assert first.startswith("44"), "cross-origin page was not refused: %r" % first[:80]
print("refused: crew from a cross-origin page")
PY

WS_JS='
const [url, msg, want] = process.argv.slice(1)
const t = setTimeout(() => { console.log("TIMEOUT"); process.exit(2) }, 8000)
const ws = new WebSocket(url)
ws.onopen = () => { if (msg) ws.send(msg) }
ws.onmessage = e => { const d = String(e.data); console.log(d); if (d.includes(want)) { clearTimeout(t); process.exit(0) } }
ws.onerror = () => { console.log("ERROR"); process.exit(3) }
ws.onclose = () => { console.log("CLOSED"); clearTimeout(t); process.exit(4) }
'
out=$(docker exec "$CTR" node -e "$WS_JS" 'ws://127.0.0.1:1880/dashboard/socket.io/?EIO=4&transport=websocket' 40 'VesselStack: refused' 2>&1) \
    || fail "a direct websocket to the dashboard was not refused: $out"
grep -q '^44' <<<"$out" || fail "direct websocket: expected a 44 packet, got: $out"
echo "refused: direct websocket"
out=$(docker exec "$CTR" node -e "$WS_JS" 'ws://127.0.0.1:1880/comms' '{"subscribe":"*"}' '"auth":"fail"' 2>&1) \
    || fail "an unauthenticated editor /comms subscription was not refused: $out"
echo "refused: unauthenticated /comms"

echo "nodered dashboard auth tests passed"
