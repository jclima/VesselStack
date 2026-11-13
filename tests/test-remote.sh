#!/bin/bash
# Optional Tailscale remote access: vesselstackctl remote enable|status|disable with a fake tailscale,
# apt-get, curl and systemctl. Nothing touches the network; run as root on Linux.

set -euo pipefail
export VESSELSTACK_NONINTERACTIVE=true

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT

APP_ROOT="$TEST_ROOT/opt/vesselstack"
DATA_ROOT="$TEST_ROOT/opt/vesselstack-data"
FAKE_BIN="$TEST_ROOT/bin"
TS_BIN="$TEST_ROOT/ts-bin"
SYSTEM_ROOT="$TEST_ROOT/system-root"
TS_STATE="$TEST_ROOT/ts-state"
LOG="$TEST_ROOT/calls.log"
install -d "$APP_ROOT/config" "$APP_ROOT/common" "$DATA_ROOT" "$FAKE_BIN" "$TS_BIN" "$TS_STATE" \
    "$SYSTEM_ROOT/etc/systemd/system" "$SYSTEM_ROOT/usr/local/sbin"
: > "$LOG"
cp "$REPO_ROOT/common/lifecycle_lock.py" "$REPO_ROOT/common/config_registry.py" "$APP_ROOT/common/"
printf 'ID=debian\nVERSION_CODENAME=bookworm\n' > "$SYSTEM_ROOT/etc/os-release"

cat > "$APP_ROOT/config/vesselstack.env" <<EOT
VESSELSTACK_ROOT=$APP_ROOT
VESSELSTACK_DATA=$DATA_ROOT
BOAT_NAME='My Boat'
CONTROL_PANEL_HOST=127.0.0.1
CONTROL_PANEL_PORT=8780
REMOTE_ACCESS=off
REMOTE_PANEL_SERVE=true
EOT
cat > "$APP_ROOT/config/control-panel.env" <<EOT
CONTROL_PANEL_HOST=127.0.0.1
CONTROL_PANEL_PORT=8780
CONTROL_PANEL_TOKEN=test-panel-token
CONTROL_PANEL_ALLOWED_HOSTS=other.example
EOT
chmod 0600 "$APP_ROOT/config/vesselstack.env" "$APP_ROOT/config/control-panel.env"

# The fake tailscale only appears on PATH once the fake apt-get has "installed" it.
cat > "$TS_BIN/tailscale" <<'EOT'
#!/bin/bash
printf 'tailscale %s\n' "$*" >> "$FAKE_LOG"
state=$FAKE_TS_STATE
case "${1:-}" in
    status)
        if [ -e "$state/up" ]; then
            echo '{"BackendState":"Running","Self":{"DNSName":"boat.example-tailnet.ts.net.","Online":true},"TailscaleIPs":["100.64.0.1","fd7a:115c:a1e0::1"]}'
        else
            echo '{"BackendState":"NeedsLogin"}'
        fi ;;
    up)
        if [ "${2:-}" = --help ]; then
            echo '  --auth-key string'
            [ -z "${FAKE_TS_FILE_KEY:-}" ] || echo '        node authorization key; if it begins with "file:", then it is a path to a file'
            exit 0
        fi
        for argument in "$@"; do
            case "$argument" in
                --auth-key=file:*) wc -c < "${argument#--auth-key=file:}" > "$state/key-length" ;;
            esac
        done
        touch "$state/up" ;;
    serve)
        case "${2:-}" in
            reset) rm -f "$state/serve" ;;
            status) [ ! -e "$state/serve" ] || echo 'https://boat.example-tailnet.ts.net:8780 (tailnet only)|-- / proxy http://127.0.0.1:8780' ;;
            *) touch "$state/serve" ;;
        esac ;;
    down) rm -f "$state/up" ;;
    logout) rm -f "$state/up" ;;
esac
exit 0
EOT
cat > "$FAKE_BIN/apt-get" <<'EOT'
#!/bin/bash
printf 'apt-get %s\n' "$*" >> "$FAKE_LOG"
case "${1:-}" in
    install) ln -sf "$FAKE_TS_BIN/tailscale" "$(dirname "$0")/tailscale" ;;
    purge) rm -f "$(dirname "$0")/tailscale" ;;
esac
exit 0
EOT
cat > "$FAKE_BIN/curl" <<'EOT'
#!/bin/bash
printf 'curl %s\n' "$*" >> "$FAKE_LOG"
[ -z "${FAKE_OFFLINE:-}" ] || exit 6
out=''
while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done
[ -z "$out" ] || [ "$out" = /dev/null ] || printf 'fake-signing-key\n' > "$out"
exit 0
EOT
cat > "$FAKE_BIN/systemctl" <<'EOT'
#!/bin/bash
printf '%s %s\n' "$(basename "$0")" "$*" >> "$FAKE_LOG"
exit 0
EOT
cp "$FAKE_BIN/systemctl" "$FAKE_BIN/docker"
chmod +x "$TS_BIN/tailscale" "$FAKE_BIN"/*

run_ctl() {
    env PATH="$FAKE_BIN:$PATH" VESSELSTACK_ROOT="$APP_ROOT" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
        VESSELSTACK_ENV="$APP_ROOT/config/vesselstack.env" FAKE_LOG="$LOG" FAKE_TS_STATE="$TS_STATE" FAKE_TS_BIN="$TS_BIN" \
        "$REPO_ROOT/scripts/vesselstackctl" "$@"
}
fail() { echo "FAIL: $*" >&2; exit 1; }
count_calls() { grep -c -- "$1" "$LOG" || true; }
env_value() { sed -n "s/^$2=//p" "$1" | tail -n 1; }

MARKER="$SYSTEM_ROOT/var/lib/vesselstack/tailscale-installed-by-vesselstack"
LIST="$SYSTEM_ROOT/etc/apt/sources.list.d/tailscale.list"
KEYRING="$SYSTEM_ROOT/usr/share/keyrings/tailscale-archive-keyring.gpg"

# status without Tailscale installed: no lock needed, a clear answer, JSON that parses.
status_json=$(run_ctl remote status --json)
python3 -I -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["installed"] is False and d["online"] is False and d["panel_url"] == ""' "$status_json"
grep -q 'not installed' <<<"$(run_ctl remote status)"

# A dry run changes nothing.
dry=$(run_ctl remote enable --dry-run)
grep -q 'dry run' <<<"$dry"
grep -q 'hostname=my-boat' <<<"$dry"
[ ! -e "$MARKER" ] || fail "dry run wrote the marker"
[ "$(count_calls '^apt-get')" -eq 0 ] || fail "dry run called apt-get"
[ "$(env_value "$APP_ROOT/config/vesselstack.env" REMOTE_ACCESS)" = off ] || fail "dry run changed REMOTE_ACCESS"

# Offline: refuse cleanly, install nothing.
if offline_out=$(FAKE_OFFLINE=1 run_ctl remote enable 2>&1); then fail "enable succeeded while offline"; fi
grep -q 'needs internet\|cannot reach pkgs.tailscale.com' <<<"$offline_out" || fail "no offline message: $offline_out"
for leftover in "$MARKER" "$KEYRING" "$LIST"; do [ ! -e "$leftover" ] || fail "offline enable left $leftover"; done
[ "$(count_calls '^apt-get install')" -eq 0 ] || fail "offline enable installed a package"
if VESSELSTACK_OFFLINE=true run_ctl remote enable >/dev/null 2>&1; then fail "enable ignored VESSELSTACK_OFFLINE=true"; fi

# Enable: signed apt source, marker, login, serve on the tailnet only, allowed host, panel restart.
run_ctl remote enable >/dev/null
[ -e "$MARKER" ] || fail "install marker missing"
[ -s "$KEYRING" ] || fail "keyring missing"
grep -Fxq 'deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/debian bookworm main' "$LIST" || fail "apt source wrong"
grep -q '^apt-get install -y tailscale' "$LOG" || fail "package not installed"
grep -q '^tailscale up --hostname=my-boat$' "$LOG" || fail "tailscale up not called with the boat host name"
grep -q '^tailscale serve --bg --https=8780 http://127.0.0.1:8780$' "$LOG" || fail "serve not configured"
[ "$(env_value "$APP_ROOT/config/control-panel.env" CONTROL_PANEL_ALLOWED_HOSTS)" = 'other.example,boat.example-tailnet.ts.net:8780' ] || fail "allowed host not added"
[ "$(env_value "$APP_ROOT/config/vesselstack.env" REMOTE_ACCESS)" = tailscale ] || fail "REMOTE_ACCESS not recorded"
[ "$(env_value "$APP_ROOT/config/control-panel.env" CONTROL_PANEL_HOST)" = 127.0.0.1 ] || fail "panel no longer on loopback"
grep -q 'systemctl restart vesselstack-control-panel.service' "$LOG" || fail "panel not restarted"
[ "$(count_calls 'tailscale up')" -eq 1 ] || fail "unexpected extra tailscale up"
# Running enable again is idempotent (no second login, no duplicate host).
run_ctl remote enable >/dev/null
[ "$(count_calls '^tailscale up --hostname')" -eq 1 ] || fail "second enable logged in again"
[ "$(env_value "$APP_ROOT/config/control-panel.env" CONTROL_PANEL_ALLOWED_HOSTS)" = 'other.example,boat.example-tailnet.ts.net:8780' ] || fail "allowed host duplicated"

# Status: human and JSON.
human=$(run_ctl remote status)
grep -q 'Tailscale: online' <<<"$human" || fail "status text: $human"
grep -q 'Control Panel URL: https://boat.example-tailnet.ts.net:8780' <<<"$human" || fail "status URL: $human"
status_json=$(run_ctl remote status --json)
python3 -I -c '
import json, sys
d = json.loads(sys.argv[1])
assert d["installed"] and d["enabled"] and d["online"], d
assert d["name"] == "boat.example-tailnet.ts.net", d
assert d["tailnet_ips"] == ["100.64.0.1", "fd7a:115c:a1e0::1"], d
assert d["panel_url"] == "https://boat.example-tailnet.ts.net:8780", d
' "$status_json"

# Disable without --purge: serve reset, down, host removed (the owner's own entry stays), package kept.
run_ctl remote disable >/dev/null
grep -q '^tailscale serve reset$' "$LOG" || fail "disable did not reset serve"
grep -q '^tailscale down$' "$LOG" || fail "disable did not go down"
[ "$(env_value "$APP_ROOT/config/control-panel.env" CONTROL_PANEL_ALLOWED_HOSTS)" = other.example ] || fail "allowed host not removed cleanly"
[ "$(env_value "$APP_ROOT/config/vesselstack.env" REMOTE_ACCESS)" = off ] || fail "REMOTE_ACCESS not reset"
[ -e "$MARKER" ] || fail "disable without --purge removed the marker"
[ "$(count_calls '^apt-get purge')" -eq 0 ] || fail "disable without --purge removed the package"

# An auth key must never reach argv, env files or any file left behind.
KEY='tskey-auth-SECRETVALUE123'
if printf '%s\n' "$KEY" | run_ctl remote enable --auth-key-stdin >/dev/null 2>&1; then fail "--auth-key-stdin accepted without file: support"; fi
printf '%s\n' "$KEY" | FAKE_TS_FILE_KEY=1 run_ctl remote enable --auth-key-stdin >/dev/null
grep -q -- '--auth-key=file:' "$LOG" || fail "key was not passed as a file"
if grep -rq "SECRETVALUE123" "$TEST_ROOT"; then fail "auth key found on disk or in logs"; fi
[ "$(cat "$TS_STATE/key-length")" -eq "${#KEY}" ] || fail "tailscale did not read the key file"
run_ctl remote disable >/dev/null

# Purge is gated by the marker: a Tailscale VesselStack did not install stays.
mv "$MARKER" "$MARKER.away"
purge_out=$(run_ctl remote disable --purge)
grep -q 'not installed by VesselStack' <<<"$purge_out" || fail "purge without marker said: $purge_out"
[ "$(count_calls '^apt-get purge')" -eq 0 ] || fail "purge removed a package VesselStack did not install"
mv "$MARKER.away" "$MARKER"
run_ctl remote disable --purge >/dev/null
grep -q '^apt-get purge -y tailscale' "$LOG" || fail "purge did not remove the package"
for leftover in "$MARKER" "$LIST" "$KEYRING"; do [ ! -e "$leftover" ] || fail "purge left $leftover"; done
if PATH="$FAKE_BIN:$PATH" command -v tailscale >/dev/null; then fail "tailscale still present after purge"; fi

# uninstall disables remote access (and purges when VesselStack installed it).
run_ctl remote enable >/dev/null
[ -e "$MARKER" ] || fail "marker missing before uninstall"
run_ctl uninstall >/dev/null
[ ! -e "$MARKER" ] || fail "uninstall left the marker"
grep -q '^apt-get purge -y tailscale' "$LOG" || fail "uninstall did not purge Tailscale"
echo "remote access tests passed"
