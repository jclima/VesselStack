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
printf 'services: {}\n' > "$APP_ROOT/compose.yml"
printf '1.3.0\n' > "$APP_ROOT/config/installed-version"
# The real credential helper runs against the fake docker below.
cp "$REPO_ROOT/scripts/mosquitto-credentials.sh" "$APP_ROOT/installer/scripts/mosquitto-credentials.sh"
for helper in configure-hardware.sh bootstrap-influx.sh; do
    printf '#!/bin/bash\nexit 0\n' > "$APP_ROOT/installer/scripts/$helper"
    chmod +x "$APP_ROOT/installer/scripts/$helper"
done

for command in docker systemctl curl; do
cat > "$FAKE_BIN/$command" <<'EOF'
#!/bin/bash
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
            *:/mosquitto/data)
                host_path=${argument%:/mosquitto/data}
                install -d "$host_path"
                printf 'test-password-file\n' > "$host_path/passwords"
                ;;
        esac
    done
fi
exit 0
EOF
    chmod +x "$FAKE_BIN/$command"
done

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

archive=$(run_ctl backup "$BACKUP_ROOT" | tail -n 1)
[ -s "$archive" ]
[ -s "$archive.sha256" ]
run_ctl verify-backup "$archive" | grep -q 'Backup verified'
if run_ctl backup "$DATA_ROOT/recursive-backups" >/dev/null 2>&1; then
    echo 'Backup accepted a destination inside the data tree' >&2
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

urls_output=$(run_ctl urls)
version_output=$(run_ctl version)
status_output=$(run_ctl status)
doctor_output=$(run_ctl doctor)
grep -q '127.0.0.1:9876' <<<"$urls_output" || grep -q ':9876' <<<"$urls_output"
grep -q 'Boat Chat .*ALL interfaces' <<<"$urls_output"
grep -q 'InfluxDB .*this device only' <<<"$urls_output"
grep -q 'installed: 1.3.0' <<<"$version_output"
grep -q 'Boat Chat service' <<<"$status_output"
grep -q '^prometheus[[:space:]]\+healthy$' <<<"$status_output"
run_ctl start
test -s "$DATA_ROOT/mosquitto/passwords" || {
    echo 'Lifecycle start did not create the MQTT password file' >&2
    exit 1
}
grep -q 'RESULT healthy' <<<"$doctor_output"

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

touch "$SYSTEM_ROOT/etc/systemd/system/vesselstack-chat.service"
if run_ctl uninstall --purge-data >/dev/null 2>&1; then
    echo 'Purge uninstall accepted missing --yes confirmation' >&2
    exit 1
fi
test -e "$SYSTEM_ROOT/etc/systemd/system/vesselstack-chat.service"
run_ctl uninstall >/dev/null
test ! -e "$SYSTEM_ROOT/etc/systemd/system/vesselstack-chat.service"
test -e "$DATA_ROOT/state.txt"

if run_ctl status unexpected >/dev/null 2>&1; then
    echo 'Lifecycle command silently ignored an extra argument' >&2
    exit 1
fi

echo "lifecycle backup and restore checks passed"
