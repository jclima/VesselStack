#!/bin/bash

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT

APP_ROOT="$TEST_ROOT/opt/vesselstack"
DATA_ROOT="$TEST_ROOT/opt/vesselstack-data"
BACKUP_ROOT="$TEST_ROOT/backups"
FAKE_BIN="$TEST_ROOT/bin"
SYSTEM_ROOT="$TEST_ROOT/system-root"
install -d "$APP_ROOT/config" "$APP_ROOT/installer/scripts" "$DATA_ROOT" "$FAKE_BIN" \
    "$SYSTEM_ROOT/etc/systemd/system" "$SYSTEM_ROOT/usr/local/sbin"

cat > "$APP_ROOT/config/vesselstack.env" <<EOF
VESSELSTACK_ROOT=$APP_ROOT
VESSELSTACK_DATA=$DATA_ROOT
VESSELSTACK_BACKUP=$BACKUP_ROOT
VESSELSTACK_USER=$(id -un)
SIGNALK_URL=http://127.0.0.1:3000
HOME_ASSISTANT_URL=http://127.0.0.1:8123
INFLUXDB_URL=http://127.0.0.1:8086
BOAT_CHAT_PORT=9876
GRAFANA_PORT=9877
HEIMDALL_PORT=9878
MQTT_USERNAME=test-user
MQTT_PASSWORD=test-password
EOF
printf 'version one\n' > "$DATA_ROOT/state.txt"
ln -s state.txt "$DATA_ROOT/current-state"
install -d "$APP_ROOT/boat-chat/.venv/bin"
ln -s /usr/bin/python3 "$APP_ROOT/boat-chat/.venv/bin/python3"
printf 'services:\n  mosquitto:\n    image: eclipse-mosquitto:2.1.2-alpine\n' > "$APP_ROOT/compose.yml"
cp -R "$REPO_ROOT/displays" "$APP_ROOT/displays"
install -d "$APP_ROOT/displays/.venv/bin" "$APP_ROOT/displays/__pycache__"
printf 'rebuildable\n' > "$APP_ROOT/displays/.venv/bin/python"
printf 'cache\n' > "$APP_ROOT/displays/__pycache__/x.pyc"
install -d -m 0700 "$APP_ROOT/build/displays/helm"
printf 'wifi_password: secret\n' > "$APP_ROOT/build/displays/helm/secrets.yaml"
install -d -m 0700 "$APP_ROOT/build/displays/helm/firmware"
printf 'firmware with embedded credentials\n' > "$APP_ROOT/build/displays/helm/firmware/firmware.factory.bin"
ln -s /usr/bin/python3 "$APP_ROOT/boat-chat/.venv/bin/python"
# The memory index code and knowledge files, and the state directory it writes to.
install -d "$APP_ROOT/boat-chat/knowledge" "$DATA_ROOT/boat-chat"
cp "$REPO_ROOT/boat-chat/memory_index.py" "$APP_ROOT/boat-chat/"
cp "$REPO_ROOT"/boat-chat/knowledge/*.md "$APP_ROOT/boat-chat/knowledge/"
cp "$REPO_ROOT/scripts/memory-index.sh" "$APP_ROOT/installer/scripts/memory-index.sh"
printf '0.3.0\n' > "$APP_ROOT/config/installed-version"
# The real credential helper runs against the fake docker below.
for helper in configure-hardware.sh bootstrap-influx.sh; do
    # shellcheck disable=SC2016
    printf '#!/bin/bash\nprintf "%%s\\n" "$(basename "$0")" >> "%s/helper.log"\nexit 0\n' "$TEST_ROOT" > "$APP_ROOT/installer/scripts/$helper"
    chmod +x "$APP_ROOT/installer/scripts/$helper"
done

for command in docker systemctl curl journalctl; do
cat > "$FAKE_BIN/$command" <<'EOF'
#!/bin/bash
printf '%s %s\n' "$(basename "$0")" "$*" >> "$(dirname "$0")/calls.log"
[ -z "${FAKE_CALL_LOG:-}" ] || printf '%s %s\n' "$(basename "$0")" "$*" >> "$FAKE_CALL_LOG"
if [ "$(basename "$0")" = curl ]; then
    if [ -n "${FAKE_MEMORY_CHUNKS:-}" ]; then
        printf '{"ok":true,"ready":true,"readiness":{"layers":{"memory":{"ready":true,"chunks":%s}}}}\n' "$FAKE_MEMORY_CHUNKS"
    else
        printf '%s\n' '{"ok":true,"ready":true}'
    fi
fi
if [ "${1:-}" = compose ] && [[ " $* " == *" --status running --services "* ]]; then
    printf '%s\n' homeassistant influxdb grafana prometheus mosquitto heimdall
fi
if [ "${1:-}" = compose ] && [[ " $* " == *" --status running --quiet "* ]]; then
    echo fake-container-id
fi
if [ "${1:-}" = compose ] && [[ " $* " == *" --script check_config "* ]]; then
    [ -z "${FAKE_HA_CHECK_HOOK:-}" ] || eval "$FAKE_HA_CHECK_HOOK"
    [ -z "${FAKE_HA_CHECK_FAIL:-}" ] || exit 1
fi
if [ "${1:-}" = run ]; then
    for argument in "$@"; do
        case "$argument" in
            *:/auth)
                host_path=${argument%:/auth}
                # Emulate the hashing container: promote passwords.new as 1883:1883 0600.
                if [ -f "$host_path/passwords.new" ]; then
                    mv -f "$host_path/passwords.new" "$host_path/passwords"
                else
                    printf 'test-password-file\n' > "$host_path/passwords"
                fi
                chmod 0600 "$host_path/passwords"
                if [ "$(id -u)" -eq 0 ]; then chown 1883:1883 "$host_path/passwords"; fi
                ;;
        esac
    done
fi
exit 0
EOF
    chmod +x "$FAKE_BIN/$command"
done

# Records how privileges are dropped for the memory index, then really drops them.
cat > "$FAKE_BIN/runuser" <<EOF
#!/bin/bash
printf '%s\\n' "\$*" >> "$TEST_ROOT/runuser.log"
# Drop this stub's directory wherever it sits in PATH, or exec would find itself again.
PATH=\$(printf '%s' "\$PATH" | tr ':' '\\n' | grep -vxF "$FAKE_BIN" | paste -sd:)
exec runuser "\$@"
EOF
chmod +x "$FAKE_BIN/runuser"

# Network stubs for `urls`: listening sockets (FAKE_SS_FILE, or fail with FAKE_SS_FAIL=1),
# addresses (a bridge and a veth that must be ignored) and the host name. Documentation
# addresses only.
cat > "$FAKE_BIN/ss" <<'EOF'
#!/bin/bash
[ -z "${FAKE_SS_FAIL:-}" ] || exit 1
cat "${FAKE_SS_FILE:?}"
EOF
cat > "$FAKE_BIN/ip" <<'EOF'
#!/bin/bash
if [[ " $* " == *" route "* ]]; then
    echo "default via 192.0.2.1 dev ${FAKE_DEFAULT_ROUTE_IF:-eth0}"
    exit 0
fi
cat <<'IPS'
1: lo    inet 127.0.0.1/8 scope host lo
2: eth0    inet 192.0.2.10/24 brd 192.0.2.255 scope global eth0
3: wlan0    inet 192.0.2.77/24 brd 192.0.2.255 scope global wlan0
4: docker0    inet 198.51.100.1/24 brd 198.51.100.255 scope global docker0
5: veth1a2b3c    inet 198.51.100.9/24 scope global veth1a2b3c
IPS
EOF
printf '#!/bin/bash\necho testboat.example\n' > "$FAKE_BIN/hostname"
chmod +x "$FAKE_BIN/ss" "$FAKE_BIN/ip" "$FAKE_BIN/hostname"
write_ss() {
    { printf 'State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n'
      for socket in "$@"; do printf 'LISTEN 0      4096   %s      0.0.0.0:*\n' "$socket"; done; } > "$TEST_ROOT/ss.txt"
}
export FAKE_SS_FILE="$TEST_ROOT/ss.txt"
write_ss '*:9876' '[::]:9877' 0.0.0.0:9878 127.0.0.1:8086 127.0.0.1:9090 127.0.0.1:1883 127.0.0.1:8780

# Fake mountpoint: only paths listed (one per line) in $FAKE_MOUNTS are mounted.
cat > "$FAKE_BIN/mountpoint" <<'EOF'
#!/bin/bash
path=${2:-$1}
[ -n "${FAKE_MOUNTS:-}" ] && grep -Fxq -- "$path" "$FAKE_MOUNTS"
EOF
chmod +x "$FAKE_BIN/mountpoint"
: > "$TEST_ROOT/mounts"
printf 'unit one\n' > "$SYSTEM_ROOT/etc/systemd/system/vesselstack-chat.service"
printf '#!/bin/sh\necho ctl-one\n' > "$SYSTEM_ROOT/usr/local/sbin/vesselstackctl"
printf '#!/bin/sh\n' > "$SYSTEM_ROOT/usr/local/sbin/vesselstack-firewall"
install -d "$SYSTEM_ROOT/etc/vesselstack"
printf 'UPDATE_KEY=one\n' > "$SYSTEM_ROOT/etc/vesselstack/update.env"
printf '#!/bin/bash\necho "$@" > "%s/installer-ran"\n' "$TEST_ROOT" > "$APP_ROOT/installer/install.sh"
chmod +x "$APP_ROOT/installer/install.sh"
chown 1234:1234 "$DATA_ROOT/state.txt"

run_ctl() {
    env PATH="$FAKE_BIN:$PATH" VESSELSTACK_ROOT="$APP_ROOT" \
        VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
        VESSELSTACK_ENV="$APP_ROOT/config/vesselstack.env" \
        FAKE_MOUNTS="$TEST_ROOT/mounts" \
        "$REPO_ROOT/scripts/vesselstackctl" "$@"
}

install -d -m 0750 -o 65534 -g 65534 "$DATA_ROOT/prometheus"
printf 'tsdb\n' > "$DATA_ROOT/prometheus/lifecycle-marker"
archive=$(run_ctl backup "$BACKUP_ROOT" | tail -n 1)
tar -tzf "$archive" | grep -q '/prometheus/lifecycle-marker$' || {
    echo 'Backup is missing the Prometheus data directory' >&2
    exit 1
}
[ -s "$archive" ]
[ -s "$archive.sha256" ]
run_ctl verify-backup "$archive" | grep -q 'Backup verified'

# `update` passes offline mode through to the release's install.sh (flag or env).
UPDATE_SOURCE="$TEST_ROOT/update-source"
install -d "$UPDATE_SOURCE"
printf '#!/bin/bash\necho "$*" >> "%s/update-args"\n' "$TEST_ROOT" > "$UPDATE_SOURCE/install.sh"
chmod +x "$UPDATE_SOURCE/install.sh"
run_ctl update "$UPDATE_SOURCE" >/dev/null
test "$(wc -l < "$TEST_ROOT/update-args")" -eq 2
if grep -q -- '--offline' "$TEST_ROOT/update-args"; then
    echo 'update passed --offline without being asked' >&2
    exit 1
fi
grep -q -- '--dry-run' "$TEST_ROOT/update-args"
grep -q -- '--start' "$TEST_ROOT/update-args"
: > "$TEST_ROOT/update-args"
run_ctl update --offline "$UPDATE_SOURCE" >/dev/null
test "$(grep -c -- '--offline' "$TEST_ROOT/update-args")" -eq 2
: > "$TEST_ROOT/update-args"
VESSELSTACK_OFFLINE=true run_ctl update "$UPDATE_SOURCE" >/dev/null
test "$(grep -c -- '--offline' "$TEST_ROOT/update-args")" -eq 2
if VESSELSTACK_OFFLINE=maybe run_ctl update "$UPDATE_SOURCE" >/dev/null 2>&1; then
    echo 'update accepted an invalid VESSELSTACK_OFFLINE' >&2
    exit 1
fi
if run_ctl update --bogus "$UPDATE_SOURCE" >/dev/null 2>&1; then
    echo 'update accepted an unknown option' >&2
    exit 1
fi
if run_ctl backup "$DATA_ROOT/recursive-backups" >/dev/null 2>&1; then
    echo 'Backup accepted a destination inside the data tree' >&2
    exit 1
fi
archive_listing=$(tar -tzf "$archive")  # not piped into grep -q: SIGPIPE trips pipefail
if grep -q '/displays/.venv\|/displays/__pycache__' <<<"$archive_listing"; then
    echo 'Rebuildable displays virtual environment or bytecode was included in backup' >&2
    exit 1
fi
if grep -q '/build/' <<<"$archive_listing"; then
    echo 'Generated firmware build directories (with secrets) were included in backup' >&2
    exit 1
fi
if ! grep -q '/displays/bridge.py' <<<"$archive_listing"; then
    echo 'Displays sources are missing from the backup' >&2
    exit 1
fi
if tar -tzf "$archive" | grep -q '/boat-chat/.venv'; then
    echo 'Rebuildable virtual environment was included in backup' >&2
    exit 1
fi
for extra in etc/systemd/system/vesselstack-chat.service usr/local/sbin/vesselstackctl \
    usr/local/sbin/vesselstack-firewall etc/vesselstack/update.env; do
    tar -tzf "$archive" | grep -q "${SYSTEM_ROOT#/}/$extra\$" || {
        echo "Backup is missing $extra" >&2
        exit 1
    }
done

# /mnt and /media destinations must be mounted unless explicitly overridden.
unmounted=/mnt/vesselstack-lifecycle-test
trap 'rm -rf -- "$TEST_ROOT" "$unmounted"' EXIT
if run_ctl backup "$unmounted/backups" >/dev/null 2>&1; then
    echo 'Backup wrote to an unmounted /mnt destination' >&2
    exit 1
fi
test ! -e "$unmounted"
printf '%s\n' "$unmounted" > "$TEST_ROOT/mounts"
run_ctl backup "$unmounted/backups" >/dev/null
: > "$TEST_ROOT/mounts"
rm -rf -- "$unmounted"
override_archive=$(run_ctl backup --allow-unmounted "$unmounted/backups" 2>/dev/null | tail -n 1)
test -s "$override_archive"
rm -rf -- "$unmounted"

# Boat Chat memory index: `reindex` builds it as the service user, the helper is idempotent
# and a damaged environment is reported with the one-line fix. Done before the first doctor
# run because an empty index is a doctor failure.
run_ctl help | grep -q 'reindex  *Rebuild Boat Chat'
if run_ctl reindex extra >/dev/null 2>&1; then
    echo 'reindex accepted an extra argument' >&2
    exit 1
fi
reindex_output=$(run_ctl reindex)
grep -Eq 'memory index: indexed \([1-9][0-9]* chunks\)' <<<"$reindex_output"
test -s "$DATA_ROOT/boat-chat/memory/boat_memory.sqlite"
grep -q -- "^-u $(id -un) -- env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/nonexistent PYTHONDONTWRITEBYTECODE=1 BOAT_CHAT_STATE_DIR=$DATA_ROOT/boat-chat .*/memory_index.py rebuild\$" "$TEST_ROOT/runuser.log"
ensure_output=$("$APP_ROOT/installer/scripts/memory-index.sh" "$APP_ROOT/config/vesselstack.env" ensure)
grep -Eq 'memory index: unchanged \([1-9][0-9]* chunks\)' <<<"$ensure_output"

urls_output=$(run_ctl urls)
version_output=$(run_ctl version)
status_output=$(run_ctl status)
doctor_output=$(run_ctl doctor)
grep -Eq 'OK   Boat Chat memory index: [1-9][0-9]* chunks' <<<"$doctor_output"
grep -q '127.0.0.1:9876' <<<"$urls_output" || grep -q ':9876' <<<"$urls_output"
grep -q 'Boat Chat .*ALL interfaces' <<<"$urls_output"
grep -q 'InfluxDB .*this device only' <<<"$urls_output"
# urls reports the REAL bind state and copy-pasteable addresses for other devices.
grep -q 'testboat.local .*mDNS' <<<"$urls_output"
grep -q '192.0.2.10 .*interface eth0' <<<"$urls_output"
grep -Fq 'http://testboat.local:9876   (from other devices' <<<"$urls_output"
grep -Fq 'http://192.0.2.10:9876' <<<"$urls_output"
grep -Fq 'http://192.0.2.77:9877' <<<"$urls_output"
grep -Fq 'http://192.0.2.10:9878' <<<"$urls_output"
grep -Fq 'http://127.0.0.1:9876   (on this device)' <<<"$urls_output"
for hidden in 198.51.100 http://0.0.0.0; do
    if grep -qF "$hidden" <<<"$urls_output"; then
        echo "urls printed an address that is not usable from other devices: $hidden" >&2
        exit 1
    fi
done
grep -q 'Grafana .*ALL interfaces' <<<"$urls_output"
grep -q 'Heimdall .*ALL interfaces' <<<"$urls_output"
grep -q 'Prometheus .*this device only; NO login' <<<"$urls_output"
grep -Fq 'to change: sudo vesselstackctl lan all' <<<"$urls_output"
grep -q 'MQTT .*this device only' <<<"$urls_output"
grep -Fq 'ssh -L 8780:127.0.0.1:8780 USER@HOST' <<<"$urls_output"
grep -q 'SignalK .*existing server, not managed here: http://127.0.0.1:3000' <<<"$urls_output"
if grep -q 'Control Panel .*ALL interfaces' <<<"$urls_output"; then
    echo 'urls reported the control panel as reachable from other devices' >&2
    exit 1
fi
# A specific-address listener, a service not seen listening, and host-networked
# SignalK/Home Assistant (docker mode) take the real sockets, not the client URLs.
sed 's|^SIGNALK_URL=.*|SIGNALK_URL=http://127.0.0.1:3000|' "$APP_ROOT/config/vesselstack.env" > "$TEST_ROOT/urls.env"
printf 'SIGNALK_MODE=docker\n' >> "$TEST_ROOT/urls.env"
run_urls() {
    env PATH="$FAKE_BIN:$PATH" VESSELSTACK_ROOT="$APP_ROOT" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
        VESSELSTACK_ENV="$TEST_ROOT/urls.env" "$REPO_ROOT/scripts/vesselstackctl" urls
}
write_ss '*:9876' 192.0.2.10:8086 127.0.0.1:3000 '*:8123' 0.0.0.0:9878
docker_urls=$(run_urls)
grep -q 'SignalK .*this device only' <<<"$docker_urls"
grep -q 'Home Assistant .*ALL interfaces' <<<"$docker_urls"
grep -Fq 'http://192.0.2.10:8123' <<<"$docker_urls"
grep -q 'InfluxDB .*interface 192.0.2.10 only' <<<"$docker_urls"
grep -q 'Grafana .*(from configuration, not seen listening)' <<<"$docker_urls"
write_ss '*:9876' '*:3000' '*:8123'
grep -q 'SignalK .*ALL interfaces' <<<"$(run_urls)"
# Without usable socket information everything falls back to the configuration and says so.
fallback_urls=$(FAKE_SS_FAIL=1 run_urls)
grep -q 'listening sockets could not be inspected' <<<"$fallback_urls"
grep -q 'InfluxDB .*this device only (from configuration)' <<<"$fallback_urls"
grep -q 'Boat Chat .*ALL interfaces.*(from configuration)' <<<"$fallback_urls"
grep -q 'SignalK .*ALL interfaces.*(assumed: host networking, not verified)' <<<"$fallback_urls"
write_ss '*:9876' '[::]:9877' 0.0.0.0:9878 127.0.0.1:8086 127.0.0.1:9090 127.0.0.1:1883 127.0.0.1:8780
grep -q 'installed: 0.3.0' <<<"$version_output"
grep -q 'Boat Chat service' <<<"$status_output"
grep -q '^prometheus[[:space:]]\+healthy$' <<<"$status_output"
grep -q '^Displays bridge[[:space:]]\+disabled$' <<<"$status_output"
: > "$FAKE_BIN/calls.log"
run_ctl start
if grep -q 'displays-bridge' "$FAKE_BIN/calls.log"; then
    echo 'Disabled displays bridge was started' >&2
    exit 1
fi
AUTH_DIR="$APP_ROOT/config/mosquitto-auth"
test -s "$AUTH_DIR/passwords" || {
    echo 'Lifecycle start did not create the MQTT password file' >&2
    exit 1
}
test -s "$AUTH_DIR/acl" || {
    echo 'Lifecycle start did not create the MQTT ACL file' >&2
    exit 1
}
grep -qx 'user test-user' "$AUTH_DIR/acl"
grep -q '^topic readwrite #$' "$AUTH_DIR/acl"
if grep -q 'vsdisp-\|vesselstack-bridge' "$AUTH_DIR/acl"; then
    echo 'Displays are disabled but the ACL grants display accounts' >&2
    exit 1
fi
file_mode() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"; }
test "$(file_mode "$AUTH_DIR")" = 750
test "$(file_mode "$AUTH_DIR/acl")" = 600
test "$(file_mode "$AUTH_DIR/passwords")" = 600
if [ "$(id -u)" -eq 0 ]; then
    test "$(stat -c '%u:%g' "$AUTH_DIR")" = 0:1883
    test "$(stat -c '%u:%g' "$AUTH_DIR/acl")" = 1883:1883
    test "$(stat -c '%u:%g' "$AUTH_DIR/passwords")" = 1883:1883
fi
# Root-run sync never creates anything in the broker-writable data directory.
test ! -e "$DATA_ROOT/mosquitto/acl"
test ! -e "$DATA_ROOT/mosquitto/passwords"
grep -q 'RESULT healthy' <<<"$doctor_output"

# Enabled displays bridge: status, start, stop and logs include the unit.
printf 'DISPLAYS_ENABLE=true\n' >> "$APP_ROOT/config/vesselstack.env"
enabled_status=$(run_ctl status)
grep -q '^Displays bridge[[:space:]]\+healthy$' <<<"$enabled_status"
: > "$FAKE_BIN/calls.log"
run_ctl start
grep -qx 'systemctl start vesselstack-displays-bridge.service' "$FAKE_BIN/calls.log"
: > "$FAKE_BIN/calls.log"
run_ctl logs displays >/dev/null
grep -qx 'journalctl -u vesselstack-displays-bridge.service -n 100 --no-pager' "$FAKE_BIN/calls.log"
: > "$FAKE_BIN/calls.log"
run_ctl logs >/dev/null
grep -q 'vesselstack-displays-bridge.service' "$FAKE_BIN/calls.log"
if run_ctl logs bogus >/dev/null 2>&1; then
    echo 'logs accepted an unknown target' >&2
    exit 1
fi
: > "$FAKE_BIN/calls.log"
run_ctl stop >/dev/null
grep -qx 'systemctl stop vesselstack-displays-bridge.service' "$FAKE_BIN/calls.log"
sed -i.bak '/^DISPLAYS_ENABLE=true$/d' "$APP_ROOT/config/vesselstack.env"
rm -f "$APP_ROOT/config/vesselstack.env.bak"

# Doctor and the memory index: an empty index fails with the one-line fix (asked of the
# running service when it reports readiness, else of the helper as the service user); an
# unusable helper only warns.
doctor_with() { env "$@" PATH="$FAKE_BIN:$PATH" VESSELSTACK_ROOT="$APP_ROOT" \
    VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" VESSELSTACK_ENV="$APP_ROOT/config/vesselstack.env" \
    "$REPO_ROOT/scripts/vesselstackctl" doctor 2>&1 || true; }
empty_output=$(doctor_with FAKE_MEMORY_CHUNKS=0)
grep -q 'FAIL Boat Chat memory index is empty (0 chunks)' <<<"$empty_output"
grep -Fq 'Fix: sudo vesselstackctl reindex' <<<"$empty_output"
grep -q 'RESULT attention required' <<<"$empty_output"
grep -Eq 'OK   Boat Chat memory index: 7 chunks' <<<"$(doctor_with FAKE_MEMORY_CHUNKS=7)"
mv "$DATA_ROOT/boat-chat/memory" "$DATA_ROOT/boat-chat/memory.away"
helper_empty_output=$(doctor_with)
grep -q 'FAIL Boat Chat memory index is empty (0 chunks)' <<<"$helper_empty_output"
rm -rf -- "$DATA_ROOT/boat-chat/memory"
mv "$DATA_ROOT/boat-chat/memory.away" "$DATA_ROOT/boat-chat/memory"
mv "$APP_ROOT/boat-chat/.venv/bin/python" "$APP_ROOT/boat-chat/.venv/bin/python.away"
unknown_output=$(doctor_with)
grep -q 'WARN could not determine the Boat Chat memory index state' <<<"$unknown_output"
if grep -q 'FAIL Boat Chat memory index' <<<"$unknown_output"; then
    echo 'doctor failed on an undeterminable memory index' >&2
    exit 1
fi
if run_ctl reindex >/dev/null 2>"$TEST_ROOT/reindex.stderr"; then
    echo 'reindex succeeded without a Python environment' >&2
    exit 1
fi
grep -q 'the Python environment is missing' "$TEST_ROOT/reindex.stderr"
mv "$APP_ROOT/boat-chat/.venv/bin/python.away" "$APP_ROOT/boat-chat/.venv/bin/python"

# `vesselstackctl lan`: presets map to the real bind settings, the files are rewritten
# atomically with owner and mode kept, the control panel is never touched, and the risky or
# conflicting cases are refused. Uses its own installation tree.
LAN_ROOT="$TEST_ROOT/lan"
install -d "$LAN_ROOT/config" "$LAN_ROOT/data/boat-chat"
sed -e "s|^VESSELSTACK_ROOT=.*|VESSELSTACK_ROOT=$LAN_ROOT|" -e "s|^VESSELSTACK_DATA=.*|VESSELSTACK_DATA=$LAN_ROOT/data|" \
    "$APP_ROOT/config/vesselstack.env" > "$LAN_ROOT/config/vesselstack.env"
printf 'BOAT_CHAT_HOST=0.0.0.0\nBOAT_CHAT_PORT=9876\nOPENAI_API_KEY=keep-this-secret\n' > "$LAN_ROOT/config/boat-chat.env"
printf 'CONTROL_PANEL_HOST=127.0.0.1\nCONTROL_PANEL_PORT=8780\nCONTROL_PANEL_TOKEN=panel-token\n' > "$LAN_ROOT/config/control-panel.env"
printf 'services: {}\n' > "$LAN_ROOT/compose.yml"
chmod 0600 "$LAN_ROOT"/config/*.env
if [ "$(id -u)" -eq 0 ]; then chown 1234:1234 "$LAN_ROOT/config/boat-chat.env"; fi
run_lan() {
    env PATH="$FAKE_BIN:$PATH" VESSELSTACK_ROOT="$LAN_ROOT" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
        VESSELSTACK_ENV="${LAN_ENV:-$LAN_ROOT/config/vesselstack.env}" FAKE_CALL_LOG="$TEST_ROOT/lan-calls.log" \
        "$REPO_ROOT/scripts/vesselstackctl" lan "$@"
}
run_lan_ha() {
    env PATH="$FAKE_BIN:$PATH" VESSELSTACK_ROOT="$LAN_ROOT" VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
        VESSELSTACK_ENV="$LAN_ROOT/config/vesselstack.env" FAKE_CALL_LOG="$TEST_ROOT/lan-calls.log" \
        "$REPO_ROOT/scripts/vesselstackctl" ha-http "$@"
}
run_ctl_ha() { run_lan_ha "$@"; }
lan_value() { sed -n "s/^$2=//p" "$1" | tail -n 1; }
lan_files_digest() { sha256sum "$LAN_ROOT"/config/*.env | sha256sum; }
: > "$TEST_ROOT/lan-calls.log"
grep -q 'Current LAN access: dashboards' <<<"$(run_lan)"
grep -Fq 'Control panel                   127.0.0.1 (never changed by this command)' <<<"$(run_lan)"
# Dry run: prints the plan, changes and starts nothing.
before=$(lan_files_digest)
dry_output=$(run_lan local --dry-run 2>&1)
grep -q 'LAN access: dashboards -> local' <<<"$dry_output"
grep -Eq 'VESSELSTACK_WEB_BIND_ADDRESS +0.0.0.0 +-> 127.0.0.1' <<<"$dry_output"
grep -Eq 'BOAT_CHAT_HOST \(boat-chat.env\) +0.0.0.0 +-> 127.0.0.1' <<<"$dry_output"
grep -q 'Dry run: nothing was changed' <<<"$dry_output"
grep -Eq 'HOMEASSISTANT_BIND_ADDRESS +0.0.0.0 +-> 127.0.0.1' <<<"$dry_output"
grep -q 'Home Assistant configuration.yaml: add the managed http block (server_host: 127.0.0.1)' <<<"$dry_output"
grep -q 'SignalK (3000) is host-networked and its server has no bind-address setting' <<<"$dry_output"
HA_CFG="$LAN_ROOT/data/homeassistant/configuration.yaml"
test ! -e "$HA_CFG"
[ "$(lan_files_digest)" = "$before" ] || { echo 'lan --dry-run changed the configuration' >&2; exit 1; }
test ! -s "$TEST_ROOT/lan-calls.log"
# local: all three bind settings move to loopback, other keys, mode and owner stay, and the
# stack is re-applied (compose up, chat restart).
run_lan local >/dev/null 2>&1
for pair in VESSELSTACK_WEB_BIND_ADDRESS=127.0.0.1 VESSELSTACK_DATA_BIND_ADDRESS=127.0.0.1; do
    test "$(lan_value "$LAN_ROOT/config/vesselstack.env" "${pair%%=*}")" = "${pair#*=}"
done
test "$(lan_value "$LAN_ROOT/config/boat-chat.env" BOAT_CHAT_HOST)" = 127.0.0.1
grep -qx 'OPENAI_API_KEY=keep-this-secret' "$LAN_ROOT/config/boat-chat.env"
grep -qx 'BOAT_CHAT_PORT=9876' "$LAN_ROOT/config/boat-chat.env"
test "$(stat -c %a "$LAN_ROOT/config/boat-chat.env")" = 600
test "$(stat -c %a "$LAN_ROOT/config/vesselstack.env")" = 600
if [ "$(id -u)" -eq 0 ]; then test "$(stat -c %u:%g "$LAN_ROOT/config/boat-chat.env")" = 1234:1234; fi
test "$(grep -c '^VESSELSTACK_WEB_BIND_ADDRESS=' "$LAN_ROOT/config/vesselstack.env")" -eq 1
grep -q ' up -d' "$TEST_ROOT/lan-calls.log"
grep -q 'systemctl restart vesselstack-chat.service' "$TEST_ROOT/lan-calls.log"
grep -qx 'CONTROL_PANEL_HOST=127.0.0.1' "$LAN_ROOT/config/control-panel.env"
grep -q 'Current LAN access: local' <<<"$(run_lan)"
# Home Assistant: local creates the stock configuration plus exactly one delimited http block,
# checks it inside the container and restarts only that container.
test "$(lan_value "$LAN_ROOT/config/vesselstack.env" HOMEASSISTANT_BIND_ADDRESS)" = 127.0.0.1
grep -qx 'default_config:' "$HA_CFG"
grep -qx '  server_host: 127.0.0.1' "$HA_CFG"
test "$(grep -c '^http:' "$HA_CFG")" -eq 1
test "$(grep -c 'BEGIN VESSELSTACK MANAGED http bind' "$HA_CFG")" -eq 1
test -f "$LAN_ROOT/data/homeassistant/automations.yaml"
grep -q 'exec -T homeassistant python3 -m homeassistant --script check_config' "$TEST_ROOT/lan-calls.log"
grep -q 'restart homeassistant' "$TEST_ROOT/lan-calls.log"
grep -qx 'configuration.yaml: managed:127.0.0.1' <<<"$(run_ctl_ha status)"
: > "$TEST_ROOT/lan-calls.log"
grep -q 'Already set' <<<"$(run_lan local 2>&1)"
test ! -s "$TEST_ROOT/lan-calls.log"
ha_before=$(sha256sum "$HA_CFG")
run_lan_ha sync >/dev/null 2>&1
test "$(sha256sum "$HA_CFG")" = "$ha_before"
test ! -s "$TEST_ROOT/lan-calls.log"
printf '\n# my own\nlogger:\n  default: warning\n' >> "$HA_CFG"
# dashboards restores the recommended state; the control panel is untouched throughout.
run_lan dashboards >/dev/null 2>&1
# The managed block is gone again; the owner's own lines and the stock content stay.
test "$(lan_value "$LAN_ROOT/config/vesselstack.env" HOMEASSISTANT_BIND_ADDRESS)" = 0.0.0.0
if grep -q 'VESSELSTACK\|server_host' "$HA_CFG"; then echo "unexpected content in $HA_CFG" >&2; exit 1; fi
grep -qx 'logger:' "$HA_CFG"
grep -qx 'default_config:' "$HA_CFG"
test "$(lan_value "$LAN_ROOT/config/vesselstack.env" VESSELSTACK_WEB_BIND_ADDRESS)" = 0.0.0.0
test "$(lan_value "$LAN_ROOT/config/vesselstack.env" VESSELSTACK_DATA_BIND_ADDRESS)" = 127.0.0.1
# An owner-managed http: section is never touched: the other settings still apply, nothing is
# written to configuration.yaml, and the exact manual step is printed.
printf 'http:\n  server_port: 8123\n' >> "$HA_CFG"
ha_before=$(sha256sum "$HA_CFG")
run_lan local >/dev/null 2>"$TEST_ROOT/lan.stderr"
test "$(sha256sum "$HA_CFG")" = "$ha_before"
grep -q 'made NO change\|VesselStack made NO change' "$TEST_ROOT/lan.stderr"
grep -q '    server_host: 127.0.0.1' "$TEST_ROOT/lan.stderr"
test "$(lan_value "$LAN_ROOT/config/vesselstack.env" VESSELSTACK_WEB_BIND_ADDRESS)" = 127.0.0.1
grep -qx 'configuration.yaml: conflict' <<<"$(run_ctl_ha status)"
run_lan dashboards >/dev/null 2>&1
test "$(sha256sum "$HA_CFG")" = "$ha_before"
sed -i '/^http:/,/^  server_port/d' "$HA_CFG"
if grep -q '^http:' "$HA_CFG"; then echo "unexpected content in $HA_CFG" >&2; exit 1; fi

# Hardening against the Home Assistant container (root, writes the same directory): every file
# operation refuses symlinks, hard links and unusual files, never follows a link at the
# destination, and keeps its backup in a root-only directory outside that tree.
HA_DIR="$LAN_ROOT/data/homeassistant"
HA_BACKUP="$SYSTEM_ROOT/var/lib/vesselstack/ha-http-backup"
HA_VICTIM="$TEST_ROOT/ha-victim"
HA_ENV="$LAN_ROOT/config/vesselstack.env"
ha_want() { sed -i "s/^HOMEASSISTANT_BIND_ADDRESS=.*/HOMEASSISTANT_BIND_ADDRESS=$1/" "$HA_ENV"; }
ha_fail() { echo "ha-http hardening: $1" >&2; exit 1; }
ha_victim_intact() { [ "$(cat "$HA_VICTIM")" = 'victim-secret-line' ] || ha_fail "victim file changed ($1)"; }
ha_no_leak() { if grep -rqF 'victim-secret-line' "$HA_DIR" "$HA_BACKUP" 2>/dev/null; then ha_fail "victim content leaked ($1)"; fi; }
printf 'victim-secret-line\n' > "$HA_VICTIM"
ha_want 127.0.0.1
# 1. symlink planted at configuration.yaml: refused, victim neither read nor written.
original_cfg=$(cat "$HA_CFG")
rm -f "$HA_CFG"; ln -s "$HA_VICTIM" "$HA_CFG"
run_lan_ha sync >/dev/null 2>"$TEST_ROOT/ha.stderr" || ha_fail 'sync with a symlink should refuse without failing'
grep -q 'made NO change\|NO change' "$TEST_ROOT/ha.stderr" || ha_fail 'no refusal message for a symlink'
[ -L "$HA_CFG" ] || ha_fail 'symlink was replaced'
ha_victim_intact symlink; ha_no_leak symlink
test "$(run_ctl_ha status | sed -n 's/^configuration.yaml: //p')" = unsafe
rm -f "$HA_CFG"; printf '%s\n' "$original_cfg" > "$HA_CFG"; chmod 0640 "$HA_CFG"
# 2. hard-linked file refused.
ln "$HA_CFG" "$TEST_ROOT/ha-hardlink"
run_lan_ha sync >/dev/null 2>&1 || ha_fail 'sync with a hard link should refuse without failing'
if grep -q VESSELSTACK "$HA_CFG"; then ha_fail 'hard-linked file was modified'; fi
test "$(run_ctl_ha status | sed -n 's/^configuration.yaml: //p')" = unsafe
rm -f "$TEST_ROOT/ha-hardlink"
# 3. symlinked homeassistant directory refused.
mv "$HA_DIR" "$HA_DIR.away"; ln -s "$HA_DIR.away" "$HA_DIR"
run_lan_ha sync >/dev/null 2>&1 || ha_fail 'sync with a symlinked directory should refuse without failing'
if grep -q VESSELSTACK "$HA_DIR.away/configuration.yaml"; then ha_fail 'symlinked directory target was modified'; fi
test "$(run_ctl_ha status | sed -n 's/^configuration.yaml: //p')" = unsafe
rm -f "$HA_DIR"; mv "$HA_DIR.away" "$HA_DIR"
# A group/world-writable directory is refused too.
chmod 0777 "$HA_DIR"
run_lan_ha sync >/dev/null 2>&1 || ha_fail 'sync with a writable directory should refuse without failing'
if grep -q VESSELSTACK "$HA_CFG"; then ha_fail 'writable directory was edited'; fi
chmod 0755 "$HA_DIR"
# 4. failing check_config: the previous file (content, mode, owner) comes back; the backup
# lives outside the Home Assistant directory with root-only mode while the check runs.
if [ "$(id -u)" -eq 0 ]; then chown 1234:1234 "$HA_CFG"; fi
before_sum=$(sha256sum < "$HA_CFG"); before_meta=$(stat -c '%a %u:%g' "$HA_CFG")
if FAKE_HA_CHECK_FAIL=1 FAKE_HA_CHECK_HOOK="stat -c %a '$HA_BACKUP' > '$TEST_ROOT/ha-backup-mode'; ls -A '$HA_BACKUP' > '$TEST_ROOT/ha-backup-list'; ls -A '$HA_DIR' > '$TEST_ROOT/ha-dir-list'" \
    run_lan_ha sync >/dev/null 2>"$TEST_ROOT/ha.stderr"; then ha_fail 'sync should fail when check_config rejects'; fi
grep -q 'previous file was restored' "$TEST_ROOT/ha.stderr" || ha_fail 'no restore message'
[ "$(sha256sum < "$HA_CFG")" = "$before_sum" ] || ha_fail 'content not restored'
[ "$(stat -c '%a %u:%g' "$HA_CFG")" = "$before_meta" ] || ha_fail 'mode/owner not restored'
[ "$(cat "$TEST_ROOT/ha-backup-mode")" = 700 ] || ha_fail 'backup directory is not mode 0700'
grep -q 'configuration.yaml.orig' "$TEST_ROOT/ha-backup-list" || ha_fail 'backup not in the root-only directory'
if grep -q 'orig\|absent\|meta' "$TEST_ROOT/ha-dir-list"; then ha_fail 'backup written inside the Home Assistant directory'; fi
if [ -n "$(ls -A "$HA_BACKUP")" ]; then ha_fail 'backup not cleaned up after restore'; fi
# 5. a symlink swapped in during the check: restore refuses, victim untouched, backup kept.
if FAKE_HA_CHECK_FAIL=1 FAKE_HA_CHECK_HOOK="rm -f '$HA_CFG'; ln -s '$HA_VICTIM' '$HA_CFG'" \
    run_lan_ha sync >/dev/null 2>"$TEST_ROOT/ha.stderr"; then ha_fail 'sync should fail (swap during check)'; fi
grep -q 'could NOT be restored' "$TEST_ROOT/ha.stderr" || ha_fail 'no manual-restore message after a swap'
ha_victim_intact swap; ha_no_leak swap
[ -L "$HA_CFG" ] || ha_fail 'swapped symlink was followed or replaced'
[ -f "$HA_BACKUP/configuration.yaml.orig" ] || ha_fail 'backup was not kept after a refused restore'
test "$(stat -c %a "$HA_BACKUP")" = 700
rm -f "$HA_CFG" "$HA_BACKUP"/*
# ... and the same with the directory swapped for a symlink.
printf '%s\n' "$original_cfg" > "$HA_CFG"
if FAKE_HA_CHECK_FAIL=1 FAKE_HA_CHECK_HOOK="mv '$HA_DIR' '$HA_DIR.away'; ln -s '$TEST_ROOT/ha-decoy' '$HA_DIR'" \
    run_lan_ha sync >/dev/null 2>"$TEST_ROOT/ha.stderr"; then ha_fail 'sync should fail (directory swap)'; fi
grep -q 'could NOT be restored' "$TEST_ROOT/ha.stderr" || ha_fail 'no manual-restore message after a directory swap'
rm -f "$HA_DIR"; mv "$HA_DIR.away" "$HA_DIR"; rm -f "$HA_BACKUP"/*
# 6. originally missing file + failing check: removed again (by name, relative to the directory).
rm -f "$HA_CFG"
if FAKE_HA_CHECK_FAIL=1 run_lan_ha sync >/dev/null 2>&1; then ha_fail 'sync should fail (created file)'; fi
[ ! -e "$HA_CFG" ] || ha_fail 'created file was not removed after a failing check'
# 7. normal replace/remove keeps the owner's lines, mode and owner.
printf '%s\n' "$original_cfg" > "$HA_CFG"; chmod 0640 "$HA_CFG"
if [ "$(id -u)" -eq 0 ]; then chown 1234:1234 "$HA_CFG"; fi
meta=$(stat -c '%a %u:%g' "$HA_CFG")
run_lan_ha sync >/dev/null 2>&1
grep -qx '  server_host: 127.0.0.1' "$HA_CFG"
[ "$(stat -c '%a %u:%g' "$HA_CFG")" = "$meta" ] || ha_fail 'mode/owner changed by sync'
ha_want 0.0.0.0
run_lan_ha sync >/dev/null 2>&1
if grep -q 'VESSELSTACK\|server_host' "$HA_CFG"; then ha_fail 'block not removed'; fi
[ "$(stat -c '%a %u:%g' "$HA_CFG")" = "$meta" ] || ha_fail 'mode/owner changed by removal'
[ "$(cat "$HA_CFG")" = "$original_cfg" ] || ha_fail 'user content changed'
ha_victim_intact final
test "$(lan_value "$LAN_ROOT/config/boat-chat.env" BOAT_CHAT_HOST)" = 0.0.0.0
grep -q 'Current LAN access: dashboards' <<<"$(run_lan)"
# all needs --yes (it opens Prometheus, which has no login); the dry run does not.
before=$(lan_files_digest)
if run_lan all >/dev/null 2>"$TEST_ROOT/lan.stderr"; then echo 'lan all ran without --yes' >&2; exit 1; fi
grep -q "repeat with --yes" "$TEST_ROOT/lan.stderr"
grep -q 'Prometheus has no login' "$TEST_ROOT/lan.stderr"
[ "$(lan_files_digest)" = "$before" ]
run_lan all --dry-run >/dev/null 2>&1
run_lan all --yes >/dev/null 2>&1
test "$(lan_value "$LAN_ROOT/config/vesselstack.env" VESSELSTACK_DATA_BIND_ADDRESS)" = 0.0.0.0
grep -q 'Current LAN access: all' <<<"$(run_lan)"
run_lan dashboards >/dev/null 2>&1
# A hand-edited combination is reported as custom.
set_value() { sed -i "s|^$2=.*|$2=$3|" "$1"; }
set_value "$LAN_ROOT/config/vesselstack.env" VESSELSTACK_DATA_BIND_ADDRESS 192.0.2.10
grep -q 'Current LAN access: custom' <<<"$(run_lan)"
run_lan dashboards >/dev/null 2>&1
# Refusals: unknown preset, extra argument, unknown option; nothing changes.
before=$(lan_files_digest)
for bad in bogus 'local dashboards' --bogus '0.0.0.0'; do
    # shellcheck disable=SC2086  # word splitting of the case is intended
    if run_lan $bad >/dev/null 2>&1; then echo "lan accepted: $bad" >&2; exit 1; fi
done
[ "$(lan_files_digest)" = "$before" ]
# Firewall interplay: with the firewall on and wlan0 untrusted, opening the LAN is refused
# (explicitly naming the interface and what is blocked), unless acknowledged; loopback is fine.
printf 'VESSELSTACK_FIREWALL_ENABLE=true\nVESSELSTACK_UNTRUSTED_INTERFACE=wlan0\n' >> "$LAN_ROOT/config/vesselstack.env"
run_lan local >/dev/null 2>&1
before=$(lan_files_digest)
if FAKE_DEFAULT_ROUTE_IF=wlan0 run_lan dashboards >/dev/null 2>"$TEST_ROOT/lan.stderr"; then echo 'lan opened the LAN against the firewall' >&2; exit 1; fi
grep -q 'treats wlan0 as untrusted' "$TEST_ROOT/lan.stderr"
grep -q 'blocked from Grafana 9877, InfluxDB 8086, Prometheus 9090 and MQTT 1883' "$TEST_ROOT/lan.stderr"
grep -q 'This machine reaches your network over wlan0 itself' "$TEST_ROOT/lan.stderr"
grep -q 'Refusing to change LAN access while the firewall would block it' "$TEST_ROOT/lan.stderr"
[ "$(lan_files_digest)" = "$before" ]
run_lan dashboards --dry-run >/dev/null 2>&1 && { echo 'dry run hid the firewall conflict' >&2; exit 1; }
run_lan dashboards --allow-firewall-conflict >/dev/null 2>&1
test "$(lan_value "$LAN_ROOT/config/boat-chat.env" BOAT_CHAT_HOST)" = 0.0.0.0
printf 'VESSELSTACK_FIREWALL_STRICT=true\nVESSELSTACK_FIREWALL_ALLOW_TCP=22\n' >> "$LAN_ROOT/config/vesselstack.env"
run_lan local >/dev/null 2>&1
if run_lan dashboards >/dev/null 2>"$TEST_ROOT/lan.stderr"; then echo 'lan opened the LAN against the strict firewall' >&2; exit 1; fi
grep -q 'everything except the allow-listed TCP ports (22)' "$TEST_ROOT/lan.stderr"
# The Boat Chat Settings page override wins over boat-chat.env: say so.
sed -i '/^VESSELSTACK_FIREWALL_/d' "$LAN_ROOT/config/vesselstack.env"
printf 'BOAT_CHAT_HOST=0.0.0.0\n' > "$LAN_ROOT/data/boat-chat/boat-chat.env"
run_lan dashboards >/dev/null 2>&1
override_output=$(run_lan local 2>&1)
grep -q "Settings page overrides BOAT_CHAT_HOST with 0.0.0.0" <<<"$override_output"
rm -f -- "$LAN_ROOT/data/boat-chat/boat-chat.env"
# A symlinked service-writable file is refused before anything is written.
run_lan dashboards >/dev/null 2>&1
mv "$LAN_ROOT/config/boat-chat.env" "$LAN_ROOT/config/boat-chat.real"
ln -s boat-chat.real "$LAN_ROOT/config/boat-chat.env"
before=$(sha256sum "$LAN_ROOT/config/vesselstack.env")
if run_lan local >/dev/null 2>"$TEST_ROOT/lan.stderr"; then echo 'lan edited through a symlink' >&2; exit 1; fi
grep -q 'unsafe' "$TEST_ROOT/lan.stderr"
[ "$(sha256sum "$LAN_ROOT/config/vesselstack.env")" = "$before" ]
rm -f -- "$LAN_ROOT/config/boat-chat.env"
mv "$LAN_ROOT/config/boat-chat.real" "$LAN_ROOT/config/boat-chat.env"

# Docker-mode SignalK: doctor names the root-owned bind-mount source, the symptom
# and the one-line fix when the container is not running; other modes stay silent.
if grep -q 'SignalK data directory' <<<"$doctor_output"; then
    echo 'doctor reported SignalK data directory outside docker mode' >&2
    exit 1
fi
DOCTOR_DATA="$TEST_ROOT/doctor-data"
install -d "$DOCTOR_DATA/signalk"
sed -e "s|^VESSELSTACK_DATA=.*|VESSELSTACK_DATA=$DOCTOR_DATA|" \
    "$APP_ROOT/config/vesselstack.env" > "$TEST_ROOT/doctor.env"
printf 'SIGNALK_MODE=docker\n' >> "$TEST_ROOT/doctor.env"
run_doctor() {
    env PATH="$FAKE_BIN:$PATH" VESSELSTACK_ROOT="$APP_ROOT" \
        VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" VESSELSTACK_ENV="$TEST_ROOT/doctor.env" \
        "$REPO_ROOT/scripts/vesselstackctl" doctor 2>&1 || true
}
if [ "$(id -u)" -eq 0 ]; then
    chown 0:0 "$DOCTOR_DATA/signalk"
    chmod 0755 "$DOCTOR_DATA/signalk"
    broken_output=$(run_doctor)
    grep -q 'FAIL SignalK container is not running and its data directory' <<<"$broken_output"
    grep -Fq "sudo chown -R 1000:1000 $DOCTOR_DATA/signalk && docker restart vesselstack-signalk" <<<"$broken_output"
    chown 1000:1000 "$DOCTOR_DATA/signalk"
    chmod 0750 "$DOCTOR_DATA/signalk"
    fixed_output=$(run_doctor)
    grep -q 'OK   SignalK data directory is writable' <<<"$fixed_output"
    if grep -q 'FAIL SignalK container' <<<"$fixed_output"; then
        echo 'doctor still reports the fixed SignalK data directory' >&2
        exit 1
    fi
fi

unsafe="$BACKUP_ROOT/unsafe.tar.gz"
python3 - "$unsafe" "${APP_ROOT#/}" <<'PY'
import io
import sys
import tarfile

with tarfile.open(sys.argv[1], "w:gz") as bundle:
    info = tarfile.TarInfo(f"vesselstack-backup/{sys.argv[2]}/../../escape")
    payload = b"unsafe\n"
    info.size = len(payload)
    bundle.addfile(info, io.BytesIO(payload))
PY
(cd "$BACKUP_ROOT" && sha256sum "$(basename "$unsafe")" > "$(basename "$unsafe").sha256")
if run_ctl verify-backup "$unsafe" >/dev/null 2>&1; then
    echo 'Unsafe traversal archive was accepted' >&2
    exit 1
fi

suid="$BACKUP_ROOT/suid.tar.gz"
python3 - "$suid" "${DATA_ROOT#/}" <<'PY'
import io
import sys
import tarfile

with tarfile.open(sys.argv[1], "w:gz") as bundle:
    info = tarfile.TarInfo(f"vesselstack-backup/{sys.argv[2]}/escalate")
    payload = b"x"
    info.size = len(payload)
    info.mode = 0o4755
    bundle.addfile(info, io.BytesIO(payload))
PY
(cd "$BACKUP_ROOT" && sha256sum "$(basename "$suid")" > "$(basename "$suid").sha256")
if run_ctl verify-backup "$suid" >/dev/null 2>&1; then
    echo 'setuid archive member was accepted' >&2
    exit 1
fi
if run_ctl restore "$suid" --yes >/dev/null 2>&1; then
    echo 'restore accepted a setuid archive' >&2
    exit 1
fi

# Restore swaps trees: files created after the backup must not mix into the result,
# and the previous trees are kept.
printf 'changed\n' > "$DATA_ROOT/state.txt"
printf 'wal after backup\n' > "$DATA_ROOT/created-after-backup.wal"
printf 'unit two\n' > "$SYSTEM_ROOT/etc/systemd/system/vesselstack-chat.service"
printf 'UPDATE_KEY=two\n' > "$SYSTEM_ROOT/etc/vesselstack/update.env"
rm -f -- "$TEST_ROOT/installer-ran"
restore_output=$(run_ctl restore "$archive" --yes)
grep -q 'Restore completed' <<<"$restore_output"
grep -qx 'version one' "$DATA_ROOT/state.txt"
test ! -e "$DATA_ROOT/created-after-backup.wal"
[ "$(stat -c %u:%g "$DATA_ROOT/state.txt")" = 1234:1234 ] || {
    echo 'Restore did not preserve numeric ownership' >&2
    exit 1
}
grep -qx 'unit one' "$SYSTEM_ROOT/etc/systemd/system/vesselstack-chat.service"
grep -qx 'UPDATE_KEY=one' "$SYSTEM_ROOT/etc/vesselstack/update.env"
grep -q -- '--start' "$TEST_ROOT/installer-ran"
pre_datas=("$DATA_ROOT".pre-restore-*)
pre_data=${pre_datas[0]}
grep -qx 'changed' "$pre_data/state.txt"
test -e "$pre_data/created-after-backup.wal"
test -d "$(printf '%s\n' "$APP_ROOT".pre-restore-* | head -n 1)"
for leftover in "$TEST_ROOT"/opt/.vesselstack-restore.*; do
    [ ! -e "$leftover" ] || { echo 'Restore left a staging directory behind' >&2; exit 1; }
done
rm -rf -- "$APP_ROOT".pre-restore-* "$DATA_ROOT".pre-restore-*

touch "$SYSTEM_ROOT/etc/systemd/system/vesselstack-chat.service" \
    "$SYSTEM_ROOT/etc/systemd/system/vesselstack-displays-bridge.service"
if run_ctl uninstall --purge-data >/dev/null 2>&1; then
    echo 'Purge uninstall accepted missing --yes confirmation' >&2
    exit 1
fi
test -e "$SYSTEM_ROOT/etc/systemd/system/vesselstack-chat.service"
uninstall_output=$(run_ctl uninstall)
grep -q 'docker volume rm vesselstack-esphome-cache' <<<"$uninstall_output"
if grep -q 'volume rm' "$FAKE_BIN/calls.log"; then
    echo 'Plain uninstall removed the ESPHome cache volume' >&2
    exit 1
fi
test ! -e "$SYSTEM_ROOT/etc/systemd/system/vesselstack-chat.service"
test ! -e "$SYSTEM_ROOT/etc/systemd/system/vesselstack-displays-bridge.service"
test -e "$DATA_ROOT/state.txt"

if run_ctl status unexpected >/dev/null 2>&1; then
    echo 'Lifecycle command silently ignored an extra argument' >&2
    exit 1
fi

# Core component membership is durable: removing a service removes only its
# container, leaves its data mount intact, and filters every later re-apply.
printf 'grafana data sentinel\n' > "$DATA_ROOT/grafana/component-sentinel"
: > "$FAKE_BIN/calls.log"
run_ctl component remove grafana >/dev/null
grep -q 'docker compose.*rm --stop --force grafana' "$FAKE_BIN/calls.log"
grep -qx 'grafana data sentinel' "$DATA_ROOT/grafana/component-sentinel"
grep -q '^VESSELSTACK_DISABLED_COMPONENTS=grafana$' "$APP_ROOT/config/vesselstack.env"
: > "$FAKE_BIN/calls.log"
run_ctl start >/dev/null
if grep -Eq 'compose .* up -d .*grafana|compose .* up -d grafana' "$FAKE_BIN/calls.log"; then
    echo 'start reintroduced removed Grafana' >&2
    exit 1
fi

# LAN changes re-apply only selected services, and re-adding the component starts
# it without disturbing the retained data directory.
printf 'VESSELSTACK_DISABLED_COMPONENTS=grafana\n' >> "$LAN_ROOT/config/vesselstack.env"
: > "$TEST_ROOT/lan-calls.log"
run_lan dashboards >/dev/null 2>&1
if grep -Eq 'compose .* up -d .*grafana|compose .* up -d grafana' "$TEST_ROOT/lan-calls.log"; then
    echo 'LAN re-apply started removed Grafana' >&2
    exit 1
fi
run_ctl component add grafana >/dev/null
grep -q 'docker compose.*up -d grafana' "$FAKE_BIN/calls.log"
grep -qx 'grafana data sentinel' "$DATA_ROOT/grafana/component-sentinel"

# Re-adding stateful services uses the existing provisioners and leaves their
# existing data roots in place.
printf 'influx data sentinel\n' > "$DATA_ROOT/influxdb/component-sentinel"
run_ctl component remove influxdb >/dev/null
run_ctl component remove mosquitto >/dev/null
: > "$TEST_ROOT/helper.log"
run_ctl component add influxdb >/dev/null
grep -q '^bootstrap-influx.sh$' "$TEST_ROOT/helper.log"
run_ctl component add mosquitto >/dev/null
grep -qx 'influx data sentinel' "$DATA_ROOT/influxdb/component-sentinel"

# Invalid membership and duplicate entries are rejected before any file or
# service mutation.
before=$(sha256sum "$APP_ROOT/config/vesselstack.env")
sed -i 's/^VESSELSTACK_DISABLED_COMPONENTS=.*/VESSELSTACK_DISABLED_COMPONENTS=grafana,grafana/' "$APP_ROOT/config/vesselstack.env"
if run_ctl start >/dev/null 2>&1; then echo 'duplicate component was accepted' >&2; exit 1; fi
[ "$before" = "$(sha256sum "$APP_ROOT/config/vesselstack.env")" ] || {
    echo 'duplicate validation mutated the config' >&2; exit 1;
}
sed -i 's/^VESSELSTACK_DISABLED_COMPONENTS=.*/VESSELSTACK_DISABLED_COMPONENTS=not-a-component/' "$APP_ROOT/config/vesselstack.env"
before=$(sha256sum "$APP_ROOT/config/vesselstack.env")
if run_ctl start >/dev/null 2>&1; then echo 'unknown component was accepted' >&2; exit 1; fi
[ "$before" = "$(sha256sum "$APP_ROOT/config/vesselstack.env")" ] || {
    echo 'unknown validation mutated the config' >&2; exit 1;
}

# With all six core services disabled, optional Docker SignalK/AIS remain
# selectable and no bare Compose up is emitted.
sed -i 's/^VESSELSTACK_DISABLED_COMPONENTS=.*/VESSELSTACK_DISABLED_COMPONENTS=homeassistant,influxdb,grafana,prometheus,mosquitto,heimdall/' "$APP_ROOT/config/vesselstack.env"
printf 'SIGNALK_MODE=docker\nAIS_ENABLE=true\n' >> "$APP_ROOT/config/vesselstack.env"
: > "$FAKE_BIN/calls.log"
run_ctl start >/dev/null
grep -q 'docker compose.*up -d signalk ais-catcher' "$FAKE_BIN/calls.log"
if grep -Eq 'docker compose.* up -d$|docker compose.* up -d --' "$FAKE_BIN/calls.log"; then
    echo 'all-disabled start emitted a bare Compose up' >&2
    exit 1
fi

echo "lifecycle backup and restore checks passed"
