#!/bin/bash
# Wait for InfluxDB, then idempotently create/update the buckets and the
# downsample task used by Boat Chat and integrations.
#
# Safety rules:
#   * a failed `bucket list` is an ERROR (retried, then fatal), never "empty";
#     a bucket is created only when a successful listing lacks it
#   * the admin token is passed to the influx CLI via the environment
#     (INFLUX_TOKEN), never on a command line
#   * retention of an existing bucket changes only when explicitly configured

# shellcheck disable=SC2153  # Variables come from the sourced config.
set -euo pipefail
umask 077

ENV_FILE=${1:-/opt/vesselstack/config/vesselstack.env}
# shellcheck disable=SC1090
source "$ENV_FILE"
: "${INFLUXDB_CONTAINER_NAME:=vesselstack-influxdb}"
: "${INFLUXDB_RAW_RETENTION:=}"
: "${INFLUXDB_READY_TIMEOUT:=120}"
: "${INFLUXDB_RETRIES:=5}"
: "${INFLUXDB_RETRY_DELAY:=2}"
TASK_VERSION=2

export INFLUX_TOKEN=$INFLUXDB_TOKEN
export INFLUX_ORG=$INFLUXDB_ORG

fail() { echo "ERROR: $*" >&2; exit 1; }

influx() {
    # Values are taken from the client environment (-e NAME without a value), so
    # neither the token nor anything else secret appears in any argv. Commands
    # that take --org pick INFLUX_ORG up; id-based commands ignore it.
    docker exec -e INFLUX_TOKEN -e INFLUX_ORG "$INFLUXDB_CONTAINER_NAME" influx "$@"
}

wait_ready() {
    local deadline=$((SECONDS + INFLUXDB_READY_TIMEOUT))
    echo "Waiting for InfluxDB (/health, up to ${INFLUXDB_READY_TIMEOUT}s)..."
    while :; do
        if [ "$(docker inspect -f '{{.State.Running}}' "$INFLUXDB_CONTAINER_NAME" 2>/dev/null || true)" = true ] \
            && docker exec "$INFLUXDB_CONTAINER_NAME" influx ping >/dev/null 2>&1; then
            echo "InfluxDB is ready"
            return 0
        fi
        [ "$SECONDS" -lt "$deadline" ] || fail "InfluxDB did not become healthy within ${INFLUXDB_READY_TIMEOUT}s; check 'vesselstackctl logs containers'"
        sleep "$INFLUXDB_RETRY_DELAY"
    done
}

# Retry a command until it succeeds; print its stdout. Failure is fatal.
retry() {
    local what=$1 attempt output
    shift
    for attempt in $(seq 1 "$INFLUXDB_RETRIES"); do
        if output=$("$@" 2>/dev/null); then
            printf '%s' "$output"
            return 0
        fi
        sleep $((attempt * INFLUXDB_RETRY_DELAY))
    done
    fail "could not $what after $INFLUXDB_RETRIES attempts (check the InfluxDB token and 'vesselstackctl logs containers')"
}

BUCKETS_JSON=
refresh_buckets() {
    BUCKETS_JSON=$(retry "list InfluxDB buckets" influx bucket list --json)
    jq -e 'type == "array"' <<<"$BUCKETS_JSON" >/dev/null \
        || fail "unexpected response while listing InfluxDB buckets"
}

bucket_id() {
    jq -r --arg name "$1" '[.[] | select(.name == $name)][0].id // empty' <<<"$BUCKETS_JSON"
}

ensure_bucket() {
    local name=$1 retention=$2
    if [ -z "$(bucket_id "$name")" ]; then
        retry "create bucket $name" influx bucket create --name "$name" --retention "$retention" >/dev/null
        printf 'Created bucket %s (%s)\n' "$name" "$retention"
        refresh_buckets
    fi
}

wait_ready
refresh_buckets

# The raw bucket is created by InfluxDB's first-run setup. On a fresh install
# that already used INFLUXDB_RAW_RETENTION (default 30d). On an existing install
# its retention is changed ONLY when INFLUXDB_RAW_RETENTION is set explicitly:
# shortening retention deletes data older than the new window, so an upgrade
# must never do it implicitly.
raw_id=$(bucket_id "$INFLUXDB_RAW_BUCKET")
[ -n "$raw_id" ] || fail "raw bucket $INFLUXDB_RAW_BUCKET does not exist (InfluxDB setup did not complete)"
if [ -n "$INFLUXDB_RAW_RETENTION" ]; then
    retry "set retention on $INFLUXDB_RAW_BUCKET" \
        influx bucket update --id "$raw_id" --retention "$INFLUXDB_RAW_RETENTION" >/dev/null
    printf 'Raw bucket %s retention set to %s (INFLUXDB_RAW_RETENTION)\n' \
        "$INFLUXDB_RAW_BUCKET" "$INFLUXDB_RAW_RETENTION"
else
    echo "Raw bucket retention not configured; existing retention left unchanged"
fi

ensure_bucket "$INFLUXDB_HOME_ASSISTANT_BUCKET" 720h
ensure_bucket "$INFLUXDB_HISTORY_BUCKET" 8760h
ensure_bucket "$INFLUXDB_AIS_BUCKET" 168h

task_name="vesselstack-downsample-$INFLUXDB_RAW_BUCKET"
# Only numeric fields can be averaged: mean() on a string or boolean field makes
# the whole task run fail. types.isType() is evaluated per row, so string/bool
# fields (e.g. navigation state, relay status) are dropped before aggregation.
task_file=$(mktemp)
trap 'rm -f -- "$task_file"' EXIT
cat > "$task_file" <<FLUX
// vesselstack-task-version: $TASK_VERSION
import "types"

option task = {name: "$task_name", every: 1m, offset: 10s}

from(bucket: "$INFLUXDB_RAW_BUCKET")
    |> range(start: -task.every)
    |> filter(fn: (r) => types.isType(v: r._value, type: "float") or types.isType(v: r._value, type: "int") or types.isType(v: r._value, type: "uint"))
    |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
    |> to(bucket: "$INFLUXDB_HISTORY_BUCKET", org: "$INFLUXDB_ORG")
FLUX

tasks_json=$(retry "list InfluxDB tasks" influx task list --json)
jq -e 'type == "array"' <<<"$tasks_json" >/dev/null || fail "unexpected response while listing InfluxDB tasks"
task_id=$(jq -r --arg name "$task_name" '[.[] | select(.name == $name)][0].id // empty' <<<"$tasks_json")
task_current=$(jq -r --arg name "$task_name" '[.[] | select(.name == $name)][0].flux // empty' <<<"$tasks_json")
container_task_file=/tmp/vesselstack-downsample.flux
if [ -z "$task_id" ] || ! grep -Fq "vesselstack-task-version: $TASK_VERSION" <<<"$task_current"; then
    docker cp "$task_file" "$INFLUXDB_CONTAINER_NAME:$container_task_file"
    if [ -z "$task_id" ]; then
        retry "create task $task_name" influx task create --file "$container_task_file" >/dev/null
        printf 'Created task %s\n' "$task_name"
    else
        retry "update task $task_name" influx task update --id "$task_id" --file "$container_task_file" >/dev/null
        printf 'Updated task %s to version %s\n' "$task_name" "$TASK_VERSION"
    fi
    docker exec "$INFLUXDB_CONTAINER_NAME" rm -f -- "$container_task_file"
fi
