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
printf '1.3.0\n' > "$APP_ROOT/config/installed-version"
for helper in configure-hardware.sh bootstrap-influx.sh; do
    printf '#!/bin/bash\nexit 0\n' > "$APP_ROOT/installer/scripts/$helper"
    chmod +x "$APP_ROOT/installer/scripts/$helper"
done

for command in docker systemctl curl journalctl; do
cat > "$FAKE_BIN/$command" <<'EOF'
#!/bin/bash
printf '%s %s\n' "$(basename "$0")" "$*" >> "$(dirname "$0")/calls.log"
if [ "$(basename "$0")" = curl ]; then
    printf '%s\n' '{"ok":true,"ready":true}'
fi
if [ "${1:-}" = compose ] && [[ " $* " == *" --status running --services "* ]]; then
    printf '%s\n' homeassistant influxdb grafana prometheus mosquitto heimdall
fi
if [ "${1:-}" = compose ] && [[ " $* " == *" --status running --quiet "* ]]; then
    echo fake-container-id
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

run_ctl() {
    env PATH="$FAKE_BIN:$PATH" VESSELSTACK_ROOT="$APP_ROOT" \
        VESSELSTACK_SYSTEM_ROOT="$SYSTEM_ROOT" \
        VESSELSTACK_ENV="$APP_ROOT/config/vesselstack.env" \
        "$REPO_ROOT/scripts/vesselstackctl" "$@"
}

archive=$(run_ctl backup "$BACKUP_ROOT" | tail -n 1)
[ -s "$archive" ]
[ -s "$archive.sha256" ]
run_ctl verify-backup "$archive" | grep -q 'Backup verified'
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
urls_output=$(run_ctl urls)
version_output=$(run_ctl version)
status_output=$(run_ctl status)
doctor_output=$(run_ctl doctor)
grep -q '127.0.0.1:9876' <<<"$urls_output"
grep -q 'installed: 1.3.0' <<<"$version_output"
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

printf 'changed\n' > "$DATA_ROOT/state.txt"
run_ctl restore "$archive" --yes >/dev/null
grep -qx 'version one' "$DATA_ROOT/state.txt"

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

echo "lifecycle backup and restore checks passed"
