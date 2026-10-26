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
ln -s /usr/bin/python3 "$APP_ROOT/boat-chat/.venv/bin/python"
# The memory index code and knowledge files, and the state directory it writes to.
install -d "$APP_ROOT/boat-chat/knowledge" "$DATA_ROOT/boat-chat"
cp "$REPO_ROOT/boat-chat/memory_index.py" "$APP_ROOT/boat-chat/"
cp "$REPO_ROOT"/boat-chat/knowledge/*.md "$APP_ROOT/boat-chat/knowledge/"
cp "$REPO_ROOT/scripts/memory-index.sh" "$APP_ROOT/installer/scripts/memory-index.sh"
printf 'services: {}\n' > "$APP_ROOT/compose.yml"
printf '0.3.0\n' > "$APP_ROOT/config/installed-version"
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

archive=$(run_ctl backup "$BACKUP_ROOT" | tail -n 1)
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
run_ctl start
test -s "$DATA_ROOT/mosquitto/passwords" || {
    echo 'Lifecycle start did not create the MQTT password file' >&2
    exit 1
}
grep -q 'RESULT healthy' <<<"$doctor_output"

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
