#!/usr/bin/env bash
set -euo pipefail

# This is intentionally opt-in: it mutates the host systemd manager and needs a
# real systemd PID 1.  CI runs it on a fresh Ubuntu VM with the opt-in enabled.
if [[ "${VESSELSTACK_SYSTEMD_UPDATE_TEST:-false}" != "true" ]]; then
    echo "SKIP: set VESSELSTACK_SYSTEMD_UPDATE_TEST=true to run the real-systemd acceptance test"
    exit 0
fi

fail() { TEST_FAILED=1; echo "FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "real systemd acceptance requires Linux"
[[ "$(id -u)" == 0 ]] || fail "real systemd acceptance requires root"
command -v systemctl >/dev/null || fail "systemctl is required"
command -v systemd-run >/dev/null || fail "systemd-run is required"
command -v curl >/dev/null || fail "curl is required"
command -v jq >/dev/null || fail "jq is required"

[[ "$(ps -p 1 -o comm= | tr -d ' ')" == "systemd" ]] || fail "PID 1 is not systemd"

# Refuse before touching the manager if either fixed name is already installed
# or loaded.  Cleanup is deliberately scoped to units proven absent here.
panel_load=$(systemctl show -p LoadState --value vesselstack-control-panel.service 2>/dev/null || true)
worker_load=$(systemctl show -p LoadState --value vesselstack-browser-update.service 2>/dev/null || true)
[[ "$panel_load" == "not-found" ]] || fail "vesselstack-control-panel.service is already installed/loaded or systemd is unavailable"
[[ "$worker_load" == "not-found" ]] || fail "vesselstack-browser-update.service is already installed/loaded or systemd is unavailable"
[[ ! -e /etc/systemd/system/vesselstack-control-panel.service ]] || fail "fixed panel unit already exists"
[[ ! -e /etc/systemd/system/vesselstack-browser-update.service ]] || fail "fixed browser worker unit already exists"

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
FIXTURE_BASE=/root
[[ -d "$FIXTURE_BASE" ]] || fail "trusted fixture base does not exist: $FIXTURE_BASE"
base_uid=$(stat -c '%u' -- "$FIXTURE_BASE")
base_mode=$(stat -c '%a' -- "$FIXTURE_BASE")
base_mode_int=$((8#$base_mode))
[[ "$base_uid" == 0 ]] || fail "trusted fixture base is not root-owned"
(( (base_mode_int & 18) == 0 )) || fail "trusted fixture base is group/other-writable"
FIXTURE="$FIXTURE_BASE/vesselstack-browser-test-$(cat /proc/sys/kernel/random/uuid)"
PANEL_UNIT=/etc/systemd/system/vesselstack-control-panel.service
CREATED_PANEL=0
STARTED_WORKER=0
PAUSED_WORKER=0
TEST_FAILED=0
diagnose_failure() {
    [[ "$TEST_FAILED" == 1 ]] || return 0
    echo "diagnostics: authenticated preflight status=${auth_status:-unavailable}; POST status=${post_status:-unavailable}" >&2
    if [[ -s "${FIXTURE:-}/restart-response.json" ]]; then
        jq -r 'if type == "object" then (.error // .message // "no error field") else "non-object response" end' "$FIXTURE/restart-response.json" 2>/dev/null || echo "unparseable response body" >&2
    fi
    for unit in vesselstack-control-panel.service vesselstack-browser-update.service; do
        echo "diagnostics: journal $unit (last 50 lines)" >&2
        journalctl -u "$unit" -n 50 --no-pager 2>&1 || true
    done
    for path in "$FIXTURE_BASE" "$FIXTURE" "$FIXTURE/control-panel" "$FIXTURE/var" "$FIXTURE/var/lib" "$FIXTURE/var/lib/vesselstack"; do
        [[ -e "$path" ]] && stat -c '%n uid=%u mode=%a' -- "$path" 2>/dev/null || true
    done >&2
}
cleanup() {
    set +e
    diagnose_failure
    [[ "$PAUSED_WORKER" == 1 ]] && kill -CONT "$worker_pid" >/dev/null 2>&1
    [[ "$STARTED_WORKER" == 1 ]] && systemctl stop vesselstack-browser-update.service >/dev/null 2>&1
    [[ "$CREATED_PANEL" == 1 ]] && systemctl stop vesselstack-control-panel.service >/dev/null 2>&1
    [[ "$CREATED_PANEL" == 1 ]] && rm -f -- "$PANEL_UNIT" && systemctl daemon-reload >/dev/null 2>&1
    rm -rf -- "$FIXTURE"
}
trap cleanup EXIT

port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
token=$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')
version=$(tr -d '[:space:]' < "$REPO_ROOT/VERSION")

install -d -m 0700 -o root -g root "$FIXTURE" "$FIXTURE/config" "$FIXTURE/installer" "$FIXTURE/control-panel"
[[ "$(stat -c '%u' -- "$FIXTURE")" == 0 && "$(stat -c '%a' -- "$FIXTURE")" == 700 ]] || fail "fixture root is not root-owned mode 0700"
cp -a "$REPO_ROOT/control-panel/app.py" "$REPO_ROOT/control-panel/browser_updates.py" "$FIXTURE/control-panel/"
cp -a "$REPO_ROOT/control-panel/static" "$FIXTURE/control-panel/"
install -d -m 0755 "$FIXTURE/common"
cp -a "$REPO_ROOT/common/vesselstack_auth.py" "$FIXTURE/common/"
cp "$REPO_ROOT/VERSION" "$FIXTURE/installer/VERSION"
cat > "$FIXTURE/config/vesselstack.env" <<EOF
VESSELSTACK_ROOT=$FIXTURE
VESSELSTACK_BACKUP=$FIXTURE/backups
EOF
cat > "$FIXTURE/config/control-panel.env" <<EOF
CONTROL_PANEL_HOST=127.0.0.1
CONTROL_PANEL_PORT=$port
CONTROL_PANEL_TOKEN=$token
EOF
chown -R root:root "$FIXTURE"
chmod 0600 "$FIXTURE/config"/*.env

cat > "$PANEL_UNIT" <<EOF
[Unit]
Description=VesselStack browser-update acceptance panel

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=$FIXTURE
Environment=VESSELSTACK_ROOT=$FIXTURE
Environment=VESSELSTACK_VERSION=$version
EnvironmentFile=$FIXTURE/config/vesselstack.env
EnvironmentFile=$FIXTURE/config/control-panel.env
ExecStartPre=/bin/sleep 2
ExecStart=/usr/bin/python3 $FIXTURE/control-panel/app.py
Restart=no

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$PANEL_UNIT"
CREATED_PANEL=1
systemctl daemon-reload
systemctl start vesselstack-control-panel.service

base="http://127.0.0.1:$port"
auth=(-H "X-VesselStack-Token: $token")
for _ in {1..50}; do
    health=$(curl --max-time 1 -fsS "$base/health" 2>/dev/null || true)
    [[ "$(jq -r '.ok // false' <<<"$health")" == true ]] && break
    sleep 0.1
done
[[ "$(jq -r '.version // empty' <<<"$health")" == "$version" ]] || fail "fixture panel did not become healthy"
old_boot=$(jq -r '.boot_id' <<<"$health")
old_pid=$(systemctl show -p MainPID --value vesselstack-control-panel.service)
[[ "$old_pid" =~ ^[1-9][0-9]*$ ]] || fail "could not identify old panel PID"

auth_status=$(curl --max-time 2 -sS -o "$FIXTURE/auth-response.json" -w '%{http_code}' "${auth[@]}" "$base/api/operation" 2>/dev/null || true)
[[ "$auth_status" == 200 ]] || fail "authenticated fixture API request failed (HTTP $auth_status)"

# The response is allowed to disappear with the old listener.  The durable
# operation file is the source of truth for the request identity.
post_status=$(curl --max-time 2 -sS -o "$FIXTURE/restart-response.json" -w '%{http_code}' "${auth[@]}" -H 'Content-Type: application/json' -d '{"action":"restart-panel"}' "$base/api/action" 2>/dev/null || true)
operation_file="$FIXTURE/var/lib/vesselstack/browser-update.json"
for _ in {1..50}; do
    [[ -s "$operation_file" ]] && break
    sleep 0.1
done
if [[ ! -s "$operation_file" ]]; then
    TEST_FAILED=1
    fail "restart did not persist an operation (HTTP $post_status)"
fi
operation_id=$(jq -r '.id // empty' "$operation_file")
[[ "$operation_id" =~ ^[0-9]+$ ]] || fail "durable operation has no id"

# During the startup window, both configuration and a second action must be
# refused while the worker's flock is held.  If the listener is unavailable,
# retry until the new listener is reachable; do not treat downtime as success.
worker_pid=""
for _ in {1..40}; do
    worker_pid=$(systemctl show -p MainPID --value vesselstack-browser-update.service 2>/dev/null || true)
    [[ "$worker_pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$worker_pid" 2>/dev/null && break
    sleep 0.1
done
[[ "$worker_pid" =~ ^[1-9][0-9]*$ ]] || fail "transient browser-update worker did not start"
STARTED_WORKER=1

# Hold the helper's flock while its systemctl child finishes replacing the
# panel.  This creates a deterministic healthy-new-panel/old-operation window:
# no admission probe can race the worker's completion.
for _ in {1..40}; do
    kill -0 "$old_pid" 2>/dev/null || break
    sleep 0.1
done
! kill -0 "$old_pid" 2>/dev/null || fail "old panel PID did not die during restart"
kill -STOP "$worker_pid"
PAUSED_WORKER=1
kill -0 "$worker_pid" 2>/dev/null || fail "stopped helper PID is not observable"
worker_cgroup=$(systemctl show -p ControlGroup --value vesselstack-browser-update.service)
panel_cgroup=$(systemctl show -p ControlGroup --value vesselstack-control-panel.service)
[[ -n "$worker_cgroup" && -n "$panel_cgroup" && "$worker_cgroup" != "$panel_cgroup" ]] || fail "worker and panel do not have separate cgroups"

config_refused=0
action_refused=0
for _ in {1..50}; do
    code=$(curl --max-time 1 -sS -o "$FIXTURE/config-response.json" -w '%{http_code}' "${auth[@]}" -H 'Content-Type: application/json' -d '{"settings":{"BOAT_NAME":"during-restart"}}' "$base/api/config" 2>/dev/null || true)
    [[ "$code" == 400 ]] && grep -qi 'Another operation' "$FIXTURE/config-response.json" && config_refused=1
    code=$(curl --max-time 1 -sS -o "$FIXTURE/action-response.json" -w '%{http_code}' "${auth[@]}" -H 'Content-Type: application/json' -d '{"action":"restart-panel"}' "$base/api/action" 2>/dev/null || true)
    [[ "$code" == 400 ]] && grep -qi 'Another operation' "$FIXTURE/action-response.json" && action_refused=1
    [[ "$config_refused" == 1 && "$action_refused" == 1 ]] && break
    sleep 0.1
done
[[ "$config_refused" == 1 ]] || fail "configuration was not refused while restart was running"
[[ "$action_refused" == 1 ]] || fail "second action was not refused while restart was running"

kill -CONT "$worker_pid"
PAUSED_WORKER=0

new_health=''
for _ in {1..100}; do
    new_health=$(curl --max-time 1 -fsS "$base/health" 2>/dev/null || true)
    if [[ "$(jq -r '.ok // false' <<<"$new_health")" == true && "$(jq -r '.boot_id // empty' <<<"$new_health")" != "$old_boot" ]]; then
        break
    fi
    sleep 0.1
done
[[ "$(jq -r '.boot_id // empty' <<<"$new_health")" != "$old_boot" ]] || fail "panel did not acquire a new boot identity"
new_pid=$(systemctl show -p MainPID --value vesselstack-control-panel.service)
[[ "$new_pid" != "$old_pid" ]] || fail "panel PID did not change"
! kill -0 "$old_pid" 2>/dev/null || fail "old panel PID is still alive"

for _ in {1..100}; do
    state=$(jq -r '.state // empty' "$operation_file" 2>/dev/null || true)
    phase=$(jq -r '.phase // empty' "$operation_file" 2>/dev/null || true)
    [[ "$state" == succeeded && "$phase" == complete ]] && break
    [[ "$state" == failed ]] && fail "durable restart operation failed"
    sleep 0.1
done
[[ "$(jq -r '.state' "$operation_file")" == succeeded ]] || fail "operation did not complete"
[[ "$(jq -r '.id' "$operation_file")" == "$operation_id" ]] || fail "operation identity changed"
[[ "$(jq -r '.running_version' "$operation_file")" == "$version" ]] || fail "operation completed without expected version"
[[ "$(jq -r '.boot_id' "$operation_file")" != "$old_boot" ]] || fail "operation completed with old boot identity"
api_operation=$(curl --max-time 2 -fsS "${auth[@]}" "$base/api/operation")
[[ "$(jq -r '.id' <<<"$api_operation")" == "$operation_id" ]] || fail "API returned a different operation"
[[ "$(jq -r '.state' <<<"$api_operation")" == succeeded ]] || fail "API operation is not succeeded"
echo "real systemd browser-update acceptance passed (version $version)"
