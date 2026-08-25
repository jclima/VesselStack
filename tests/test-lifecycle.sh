#!/bin/bash

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT

APP_ROOT="$TEST_ROOT/opt/vesselstack"
DATA_ROOT="$TEST_ROOT/opt/vesselstack-data"
BACKUP_ROOT="$TEST_ROOT/backups"
FAKE_BIN="$TEST_ROOT/bin"
install -d "$APP_ROOT/config" "$DATA_ROOT" "$FAKE_BIN"

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
EOF
printf 'version one\n' > "$DATA_ROOT/state.txt"
ln -s state.txt "$DATA_ROOT/current-state"
install -d "$APP_ROOT/boat-chat/.venv/bin"
ln -s /usr/bin/python3 "$APP_ROOT/boat-chat/.venv/bin/python3"
printf 'services: {}\n' > "$APP_ROOT/compose.yml"
printf '1.0.1\n' > "$APP_ROOT/config/installed-version"

for command in docker systemctl curl; do
    cat > "$FAKE_BIN/$command" <<'EOF'
#!/bin/bash
if [ "${1:-}" = compose ] && [[ " $* " == *" --status running --quiet "* ]]; then
    echo fake-container-id
fi
exit 0
EOF
    chmod +x "$FAKE_BIN/$command"
done

run_ctl() {
    env PATH="$FAKE_BIN:$PATH" VESSELSTACK_ROOT="$APP_ROOT" \
        VESSELSTACK_ENV="$APP_ROOT/config/vesselstack.env" \
        "$REPO_ROOT/scripts/vesselstackctl" "$@"
}

archive=$(run_ctl backup "$BACKUP_ROOT" | tail -n 1)
[ -s "$archive" ]
[ -s "$archive.sha256" ]
run_ctl verify-backup "$archive" | grep -q 'Backup verified'
if tar -tzf "$archive" | grep -q '/boat-chat/.venv'; then
    echo 'Rebuildable virtual environment was included in backup' >&2
    exit 1
fi
urls_output=$(run_ctl urls)
version_output=$(run_ctl version)
status_output=$(run_ctl status)
doctor_output=$(run_ctl doctor)
grep -q '127.0.0.1:9876' <<<"$urls_output"
grep -q 'installed: 1.0.1' <<<"$version_output"
grep -q 'Boat Chat service' <<<"$status_output"
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

printf 'changed\n' > "$DATA_ROOT/state.txt"
run_ctl restore "$archive" --yes >/dev/null
grep -qx 'version one' "$DATA_ROOT/state.txt"

echo "lifecycle backup and restore checks passed"
