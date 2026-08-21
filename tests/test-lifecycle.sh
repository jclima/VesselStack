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
EOF
printf 'version one\n' > "$DATA_ROOT/state.txt"
printf 'services: {}\n' > "$APP_ROOT/compose.yml"

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

printf 'changed\n' > "$DATA_ROOT/state.txt"
run_ctl restore "$archive" --yes >/dev/null
grep -qx 'version one' "$DATA_ROOT/state.txt"

echo "lifecycle backup and restore checks passed"
