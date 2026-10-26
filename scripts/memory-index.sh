#!/bin/bash
# Build or refresh the Boat Chat memory index AS THE SERVICE USER.
#
# Usage: memory-index.sh ENV_FILE ensure|rebuild|status
#   ensure   build only when the index is missing, empty or the bundled knowledge
#            files changed (idempotent; used by install, start and the indexer)
#   rebuild  always rebuild (vesselstackctl reindex)
#   status   print the number of indexed chunks
#
# Boat Chat reports "not ready" until this index has chunks. The index lives in the
# service-writable state directory, so it is created by the service user with the same
# minimal environment the systemd units use; root never opens the SQLite file (a
# root-owned -wal/-shm file would break the chat service).

set -euo pipefail

ENV_FILE=${1:-}
ACTION=${2:-ensure}
if [ -z "$ENV_FILE" ] || [ ! -r "$ENV_FILE" ]; then
    echo "Usage: memory-index.sh ENV_FILE ensure|rebuild|status" >&2
    exit 2
fi
case "$ACTION" in
    ensure|rebuild|status) ;;
    *) echo "Usage: memory-index.sh ENV_FILE ensure|rebuild|status" >&2; exit 2 ;;
esac

# The installed configuration is root-owned and mode 0600, like for the other helpers.
# shellcheck disable=SC1090
source "$ENV_FILE"
: "${VESSELSTACK_ROOT:?Missing VESSELSTACK_ROOT}"
: "${VESSELSTACK_DATA:?Missing VESSELSTACK_DATA}"
: "${VESSELSTACK_USER:?Missing VESSELSTACK_USER}"

STATE_DIR="$VESSELSTACK_DATA/boat-chat"
PYTHON="$VESSELSTACK_ROOT/boat-chat/.venv/bin/python"
SCRIPT="$VESSELSTACK_ROOT/boat-chat/memory_index.py"
CHAT_ENV="$VESSELSTACK_ROOT/config/boat-chat.env"

fail() { echo "memory index: $*" >&2; exit 1; }

[ -x "$PYTHON" ] || fail "the Python environment is missing ($PYTHON)"
[ -r "$SCRIPT" ] || fail "$SCRIPT is missing"
[ -d "$STATE_DIR" ] || fail "the state directory $STATE_DIR does not exist"
service_uid=$(id -u "$VESSELSTACK_USER" 2>/dev/null) || fail "service user $VESSELSTACK_USER does not exist"
service_gid=$(id -g "$VESSELSTACK_USER")

# Read one value from the service-user-writable env file WITHOUT sourcing it, and accept
# only plain values (these two settings change what the index contains).
chat_env_value() {
    local value
    [ -r "$CHAT_ENV" ] || return 0
    value=$(grep -E "^$1=" "$CHAT_ENV" 2>/dev/null | tail -n 1 | cut -d= -f2- || true)
    value=${value#\'}; value=${value%\'}; value=${value#\"}; value=${value%\"}
    [[ "$value" =~ ^[A-Za-z0-9._/@:+-]+$ ]] && printf '%s' "$value"
    return 0
}

environment=(PATH=/usr/local/bin:/usr/bin:/bin HOME=/nonexistent PYTHONDONTWRITEBYTECODE=1
    "BOAT_CHAT_STATE_DIR=$STATE_DIR" "BOAT_CHAT_ENV=$STATE_DIR/boat-chat.env")
# The units load config/boat-chat.env; keep the index in step with an embedding model
# or database path set there (a mismatch would make search quality silently worse).
for key in BOAT_CHAT_EMBEDDING_MODEL BOAT_CHAT_MEMORY_DB; do
    value=$(chat_env_value "$key")
    [ -z "$value" ] || environment+=("$key=$value")
done

run_as_service_user() {
    if [ "$(id -u)" -ne 0 ]; then
        [ "$(id -u)" -eq "$service_uid" ] || fail "run as root or as $VESSELSTACK_USER"
        "$@"
    elif command -v runuser >/dev/null 2>&1; then
        runuser -u "$VESSELSTACK_USER" -- "$@"
    elif command -v setpriv >/dev/null 2>&1; then
        setpriv --reuid="$service_uid" --regid="$service_gid" --init-groups -- "$@"
    else
        fail "neither runuser nor setpriv is available to drop privileges"
    fi
}

index_command() {
    # A neutral working directory the service user can always enter.
    (cd / && run_as_service_user env -i "${environment[@]}" "$PYTHON" "$SCRIPT" "$1")
}

if ! output=$(index_command "$ACTION" 2>&1); then
    printf '%s\n' "$output" >&2
    fail "'$ACTION' failed"
fi

summary=$(python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except ValueError:
    raise SystemExit(1)
print(data.get("action", "indexed"), data.get("chunks", 0))
' <<<"$output") || fail "unexpected output from memory_index.py: $output"
read -r result chunks <<<"$summary"

if [ "$ACTION" = status ]; then
    printf '%s\n' "$chunks"
    exit 0
fi

# Everything the service user created must stay owned by it (never root).
db="$STATE_DIR/memory/boat_memory.sqlite"
for file in "$db" "$db-wal" "$db-shm"; do
    [ -e "$file" ] || continue
    [ "$(stat -c %u "$file")" = "$service_uid" ] || \
        fail "$file is not owned by $VESSELSTACK_USER; fix with: chown -R $VESSELSTACK_USER:$VESSELSTACK_USER '$STATE_DIR/memory'"
done
printf 'memory index: %s (%s chunks)\n' "$result" "$chunks"
[ "$chunks" -gt 0 ] || fail "the index has no content"
