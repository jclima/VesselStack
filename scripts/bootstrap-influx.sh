#!/bin/bash
# Create the retained buckets used by Boat Chat and integrations.

set -euo pipefail

ENV_FILE=${1:-/opt/vesselstack/config/vesselstack.env}
# shellcheck disable=SC1090
source "$ENV_FILE"

influx() {
    docker exec vesselstack-influxdb influx "$@" --org "$INFLUXDB_ORG" --token "$INFLUXDB_TOKEN"
}

ensure_bucket() {
    local name=$1
    local retention=$2

    if ! influx bucket list --name "$name" --hide-headers 2>/dev/null | grep -q .; then
        influx bucket create --name "$name" --retention "$retention" >/dev/null
        printf 'Created bucket %s (%s)\n' "$name" "$retention"
    fi
}

ensure_bucket "$INFLUXDB_HOME_ASSISTANT_BUCKET" 720h
ensure_bucket "$INFLUXDB_HISTORY_BUCKET" 8760h
ensure_bucket "$INFLUXDB_AIS_BUCKET" 168h

task_name="vesselstack-downsample-$INFLUXDB_RAW_BUCKET"
if ! influx task list --hide-headers 2>/dev/null | grep -Fq "$task_name"; then
    task_file=$(mktemp)
    trap 'rm -f -- "$task_file"' EXIT
    cat > "$task_file" <<EOF
option task = {name: "$task_name", every: 1m, offset: 10s}

from(bucket: "$INFLUXDB_RAW_BUCKET")
    |> range(start: -task.every)
    |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
    |> to(bucket: "$INFLUXDB_HISTORY_BUCKET", org: "$INFLUXDB_ORG")
EOF
    container_task_file=/tmp/vesselstack-downsample.flux
    docker cp "$task_file" "vesselstack-influxdb:$container_task_file"
    influx task create --file "$container_task_file" >/dev/null
    docker exec vesselstack-influxdb rm -f -- "$container_task_file"
    printf 'Created task %s\n' "$task_name"
fi
