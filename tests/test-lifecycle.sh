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
LOG_FILE="$TEST_ROOT/commands.log"
: > "$LOG_FILE"

cat > "$FAKE_BIN/id" <<'EOF'
#!/bin/bash
[ "${1:-}" = -u ] && { echo 0; exit 0; }
exit 0
EOF
chmod +x "$FAKE_BIN/id"

cat > "$FAKE_BIN/docker" <<'EOF'
#!/bin/bash
printf 'docker %s\n' "$*" >> "$VESSELSTACK_TEST_LOG"
if [ "${1:-}" = compose ] && [[ " $* " == *" --status running --quiet "* ]]; then
    echo fake-container-id
fi
exit 0
EOF
chmod +x "$FAKE_BIN/docker"

for command in systemctl curl; do
    cat > "$FAKE_BIN/$command" <<'EOF'
#!/bin/bash
printf '%s %s\n' "$(basename "$0")" "$*" >> "$VESSELSTACK_TEST_LOG"
exit 0
EOF
    chmod +x "$FAKE_BIN/$command"
done

run_ctl() {
    env PATH="$FAKE_BIN:$PATH" VESSELSTACK_ROOT="$APP_ROOT" VESSELSTACK_TEST_LOG="$LOG_FILE" \
        VESSELSTACK_ENV="$APP_ROOT/config/vesselstack.env" \
        "$REPO_ROOT/scripts/vesselstackctl" "$@"
}

run_ctl start >/dev/null
grep -q 'docker compose.*up -d homeassistant influxdb grafana prometheus mosquitto heimdall' "$LOG_FILE"
: > "$LOG_FILE"

run_ctl component remove grafana >/dev/null
grep -q 'docker compose.*rm --stop --force grafana' "$LOG_FILE"
grep -q '^VESSELSTACK_DISABLED_COMPONENTS=grafana$' "$APP_ROOT/config/vesselstack.env"
: > "$LOG_FILE"
run_ctl start >/dev/null
grep -q 'docker compose.*up -d homeassistant influxdb prometheus mosquitto heimdall' "$LOG_FILE"
! grep -q 'up -d.*grafana' "$LOG_FILE"
: > "$LOG_FILE"
run_ctl component add grafana >/dev/null
grep -q 'docker compose.*up -d grafana' "$LOG_FILE"
grep -q '^VESSELSTACK_DISABLED_COMPONENTS=' "$APP_ROOT/config/vesselstack.env"
[ -z "$(. "$APP_ROOT/config/vesselstack.env"; printf '%s' "${VESSELSTACK_DISABLED_COMPONENTS:-}")" ]

sed "s/^VESSELSTACK_DISABLED_COMPONENTS=.*/VESSELSTACK_DISABLED_COMPONENTS=grafana,grafana/" \
    "$APP_ROOT/config/vesselstack.env" > "$APP_ROOT/config/vesselstack.env.tmp"
mv "$APP_ROOT/config/vesselstack.env.tmp" "$APP_ROOT/config/vesselstack.env"
if run_ctl start 2>/dev/null; then exit 1; fi
grep -q 'VESSELSTACK_DISABLED_COMPONENTS=' "$APP_ROOT/config/vesselstack.env"
sed "s/^VESSELSTACK_DISABLED_COMPONENTS=.*/VESSELSTACK_DISABLED_COMPONENTS=/" \
    "$APP_ROOT/config/vesselstack.env" > "$APP_ROOT/config/vesselstack.env.tmp"
mv "$APP_ROOT/config/vesselstack.env.tmp" "$APP_ROOT/config/vesselstack.env"

before=$(sha256sum "$APP_ROOT/config/vesselstack.env")
if run_ctl component remove unknown 2>/dev/null; then exit 1; fi
[ "$before" = "$(sha256sum "$APP_ROOT/config/vesselstack.env")" ]

sed "s/^VESSELSTACK_DISABLED_COMPONENTS=.*/VESSELSTACK_DISABLED_COMPONENTS=homeassistant,influxdb,grafana,prometheus,mosquitto,heimdall/" \
    "$APP_ROOT/config/vesselstack.env" > "$APP_ROOT/config/vesselstack.env.tmp"
mv "$APP_ROOT/config/vesselstack.env.tmp" "$APP_ROOT/config/vesselstack.env"
: > "$LOG_FILE"
run_ctl start >/dev/null
! grep -q 'up -d' "$LOG_FILE"

archive=$(run_ctl backup "$BACKUP_ROOT" | tail -n 1)
[ -s "$archive" ]
[ -s "$archive.sha256" ]
run_ctl verify-backup "$archive" | grep -q 'Backup verified'

printf 'changed\n' > "$DATA_ROOT/state.txt"
run_ctl restore "$archive" --yes >/dev/null
grep -qx 'version one' "$DATA_ROOT/state.txt"

echo "lifecycle backup and restore checks passed"
