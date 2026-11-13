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
                --auth-key=file:*) wc -c < "${argument#--auth-key=file:}" > "$state/key-length"; dirname "${argument#--auth-key=file:}" > "$state/key-dir" ;;
            esac
        done
        [ -z "${FAKE_TS_KILL_PARENT:-}" ] || kill -TERM "$PPID"
        [ -z "${FAKE_TS_UP_FAIL:-}" ] || exit 1
        touch "$state/up" ;;
    serve)
        case "${2:-}" in
            reset) rm -f "$state/serve" ;;
            --https=8780) [ "${3:-}" != off ] || rm -f "$state/serve" ;;
            --https=1880) [ "${3:-}" != off ] || rm -f "$state/serve-nr" ;;
            --bg) if [ "${3:-}" = --https=1880 ]; then touch "$state/serve-nr"; else touch "$state/serve"; fi ;;
            status)
                [ ! -e "$state/serve" ] || echo 'https://boat.example-tailnet.ts.net:8780 (tailnet only)|-- / proxy http://127.0.0.1:8780'
                [ ! -e "$state/serve-nr" ] || echo 'https://boat.example-tailnet.ts.net:1880 (tailnet only)|-- / proxy http://127.0.0.1:1881' ;;
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
grep -q 'hostname=vesselstack --accept-dns=false' <<<"$dry"
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
grep -q '^tailscale up --hostname=vesselstack --accept-dns=false$' "$LOG" || fail "tailscale up not called with the neutral default host name and --accept-dns=false"
if grep -q 'my-boat' "$LOG"; then fail "boat name leaked into the tailnet host name"; fi
grep -q '^curl --proto =https --proto-redir =https ' "$LOG" || fail "curl did not restrict protocols"
if grep '^curl' "$LOG" | grep -vq -- '--proto =https --proto-redir =https'; then fail "a curl call lacks the https-only flags"; fi
[ "$(cat "$SYSTEM_ROOT/var/lib/vesselstack/tailscale-panel-host")" = boat.example-tailnet.ts.net:8780 ] || fail "added panel host not recorded"
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

# Node-RED enabled: its own tailnet-only handler on 1880 is added and shown; nothing is published while it is off.
if grep -q '^tailscale serve --bg --https=1880' "$LOG"; then fail "Node-RED was published while disabled"; fi
printf 'NODERED_ENABLE=true\nNODERED_PORT=1881\n' >> "$APP_ROOT/config/vesselstack.env"
run_ctl remote enable --dry-run | grep -q 'tailscale serve --bg --https=1880 http://127.0.0.1:1881' || fail "dry run omits the Node-RED handler"
run_ctl remote enable >/dev/null
grep -q '^tailscale serve --bg --https=1880 http://127.0.0.1:1881$' "$LOG" || fail "Node-RED serve not configured"
human=$(run_ctl remote status)
grep -q 'Node-RED URL: https://boat.example-tailnet.ts.net:1880' <<<"$human" || fail "status lacks the Node-RED URL: $human"
python3 -I -c 'import json,sys; assert json.loads(sys.argv[1])["nodered_url"]=="https://boat.example-tailnet.ts.net:1880"' "$(run_ctl remote status --json)"

# Disable without --purge: only our serve handler off, host removed by exact name (the owner's own entries stay),
# panel restarted before tailscale goes down, package kept.
set_hosts() { sed -i "s|^CONTROL_PANEL_ALLOWED_HOSTS=.*|CONTROL_PANEL_ALLOWED_HOSTS=$1|" "$APP_ROOT/config/control-panel.env"; }
set_hosts 'other.example,mine.example-tailnet.ts.net:8780,boat.example-tailnet.ts.net:8780'
: > "$LOG"
disable_out=$(run_ctl remote disable 2>&1)
if grep -q '^tailscale serve reset' "$LOG"; then fail "disable used serve reset"; fi
grep -q '^tailscale serve --https=8780 off$' "$LOG" || fail "disable did not remove only our serve handler"
grep -q '^tailscale serve --https=1880 off$' "$LOG" || fail "disable did not remove the Node-RED handler"
sed -i 's/^NODERED_ENABLE=true$/NODERED_ENABLE=false/' "$APP_ROOT/config/vesselstack.env"
grep -q '^tailscale down$' "$LOG" || fail "disable did not go down for a VesselStack-installed Tailscale"
grep -q 'SSH session' <<<"$disable_out" || fail "no SSH warning before tailscale down"
[ "$(env_value "$APP_ROOT/config/control-panel.env" CONTROL_PANEL_ALLOWED_HOSTS)" = 'other.example,mine.example-tailnet.ts.net:8780' ] || fail "allowed host not removed by exact name"
[ ! -e "$SYSTEM_ROOT/var/lib/vesselstack/tailscale-panel-host" ] || fail "host record not removed"
restart_line=$(grep -n 'systemctl restart vesselstack-control-panel' "$LOG" | head -n 1 | cut -d: -f1)
off_line=$(grep -n '^tailscale serve --https=8780 off$' "$LOG" | head -n 1 | cut -d: -f1)
down_line=$(grep -n '^tailscale down$' "$LOG" | head -n 1 | cut -d: -f1)
if ! { [ "$restart_line" -lt "$off_line" ] && [ "$off_line" -lt "$down_line" ]; }; then fail "order must be restart, serve off, down ($restart_line $off_line $down_line)"; fi
set_hosts other.example
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

# Hostname: --hostname overrides the default and is validated; a bad name changes nothing.
for bad in 'My Boat' '-boat' 'boat-' 'UPPER' 'a_b' '' "$(printf 'a%.0s' $(seq 64))"; do
    if run_ctl remote enable --dry-run --hostname "$bad" >/dev/null 2>&1; then fail "accepted invalid --hostname '$bad'"; fi
done
grep -q 'hostname=sea-1 ' <<<"$(run_ctl remote enable --dry-run --hostname sea-1)" || fail "--hostname not honoured"

key_dir_gone() { [ -s "$TS_STATE/key-dir" ] || return 1; [ ! -e "$(cat "$TS_STATE/key-dir")" ]; }
# A failed or interrupted join removes the key file (the EXIT/TERM trap, not only the success path).
rm -f "$TS_STATE/key-dir"
if printf '%s\n' "$KEY" | FAKE_TS_FILE_KEY=1 FAKE_TS_UP_FAIL=1 run_ctl remote enable --auth-key-stdin >/dev/null 2>&1; then fail "enable succeeded though tailscale up failed"; fi
key_dir_gone || fail "key directory left behind after a failed join"
rm -f "$TS_STATE/key-dir"
printf '%s\n' "$KEY" | FAKE_TS_FILE_KEY=1 FAKE_TS_KILL_PARENT=1 run_ctl remote enable --auth-key-stdin >/dev/null 2>&1 || true
key_dir_gone || fail "key directory left behind after the command was terminated"
if grep -rq "SECRETVALUE123" "$TEST_ROOT"; then fail "auth key found on disk after failures"; fi
rm -f "$TS_STATE/up"

# Purge is gated by the marker: a Tailscale VesselStack did not install stays.
mv "$MARKER" "$MARKER.away"
set_hosts 'other.example,boat.example-tailnet.ts.net:8780'
: > "$LOG"
purge_out=$(run_ctl remote disable --purge 2>&1)
grep -q 'not installed by VesselStack' <<<"$purge_out" || fail "purge without marker said: $purge_out"
grep -q 'WARN no record' <<<"$purge_out" || fail "missing host record gave no WARN: $purge_out"
[ "$(env_value "$APP_ROOT/config/control-panel.env" CONTROL_PANEL_ALLOWED_HOSTS)" = other.example ] || fail "fallback pattern did not remove the tailnet entry"
grep -q '^tailscale serve --https=8780 off$' "$LOG" || fail "owner-installed disable skipped serve off"
if grep -q '^tailscale down' "$LOG"; then fail "disable took down a Tailscale the owner installed"; fi
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
