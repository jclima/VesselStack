#!/bin/bash

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TEST_ROOT=$(mktemp -d)
ENV_FILE="$TEST_ROOT/vesselstack.env"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$ROOT/templates/compose.yml" \
    -f "$ROOT/tests/compose.integration.yml")
INFLUX_TEST_PORT=48086
GRAFANA_TEST_PORT=44000
PROMETHEUS_TEST_PORT=49090

cleanup() {
    "${COMPOSE[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
    if command -v sudo >/dev/null; then
        sudo rm -rf -- "$TEST_ROOT"
    else
        rm -rf -- "$TEST_ROOT"
    fi
}
trap cleanup EXIT

install -d "$TEST_ROOT/config/grafana" "$TEST_ROOT/data"
sudo install -d -m 0750 -o 1000 -g 1000 "$TEST_ROOT/data/influxdb"
sudo install -d -m 0750 -o 472 -g 0 "$TEST_ROOT/data/grafana"
cp -a "$ROOT/templates/grafana/provisioning" "$TEST_ROOT/config/grafana/"
cp -a "$ROOT/templates/grafana/dashboards" "$TEST_ROOT/config/grafana/"
cp "$ROOT/templates/prometheus.yml" "$TEST_ROOT/config/prometheus.yml"
cp "$ROOT/vesselstack.example.env" "$ENV_FILE"
sed -i \
    -e "s|^VESSELSTACK_ROOT=.*|VESSELSTACK_ROOT=\"$TEST_ROOT\"|" \
    -e "s|^VESSELSTACK_DATA=.*|VESSELSTACK_DATA=\"$TEST_ROOT/data\"|" \
    -e 's|^INFLUXDB_PASSWORD=.*|INFLUXDB_PASSWORD="integration-password"|' \
    -e 's|^INFLUXDB_TOKEN=.*|INFLUXDB_TOKEN="integration-token-0123456789"|' \
    -e 's|^GRAFANA_ADMIN_PASSWORD=.*|GRAFANA_ADMIN_PASSWORD="integration-grafana"|' \
    -e "s|^INFLUXDB_URL=.*|INFLUXDB_URL=\"http://127.0.0.1:$INFLUX_TEST_PORT\"|" \
    -e "s|^INFLUXDB_PORT=.*|INFLUXDB_PORT=\"$INFLUX_TEST_PORT\"|" \
    -e 's|^INFLUXDB_CONTAINER_NAME=.*|INFLUXDB_CONTAINER_NAME="vesselstack-test-influxdb"|' \
    -e "s|^GRAFANA_PORT=.*|GRAFANA_PORT=\"$GRAFANA_TEST_PORT\"|" \
    -e "s|^PROMETHEUS_PORT=.*|PROMETHEUS_PORT=\"$PROMETHEUS_TEST_PORT\"|" \
    "$ENV_FILE"

"${COMPOSE[@]}" up -d influxdb prometheus grafana
ready=0
for _attempt in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:$INFLUX_TEST_PORT/health" >/dev/null && \
        curl -fsS "http://127.0.0.1:$GRAFANA_TEST_PORT/api/health" >/dev/null; then
        ready=1
        break
    fi
    sleep 2
done
[ "$ready" -eq 1 ] || {
    "${COMPOSE[@]}" ps --all
    "${COMPOSE[@]}" logs --no-color influxdb grafana prometheus
    exit 1
}
curl -fsS "http://127.0.0.1:$INFLUX_TEST_PORT/health" >/dev/null
curl -fsS "http://127.0.0.1:$GRAFANA_TEST_PORT/api/health" >/dev/null

"$ROOT/scripts/bootstrap-influx.sh" "$ENV_FILE"
second_bootstrap=$("$ROOT/scripts/bootstrap-influx.sh" "$ENV_FILE")
if grep -q 'Created task' <<<"$second_bootstrap"; then
    echo "Influx bootstrap created a duplicate task" >&2
    exit 1
fi
docker exec vesselstack-test-influxdb influx bucket list \
    --org vesselstack --token integration-token-0123456789 --name signalk_1m \
    --hide-headers | grep -q signalk_1m
docker exec vesselstack-test-influxdb influx task list \
    --org vesselstack --token integration-token-0123456789 \
    --hide-headers | grep -q vesselstack-downsample-signalk

curl -fsS -u boatadmin:integration-grafana \
    "http://127.0.0.1:$GRAFANA_TEST_PORT/api/datasources/uid/vesselstack-influx" | \
    jq -e '.name == "VesselStack InfluxDB"' >/dev/null
curl -fsS -u boatadmin:integration-grafana \
    "http://127.0.0.1:$GRAFANA_TEST_PORT/api/dashboards/uid/vesselstack-system-health" | \
    jq -e '.dashboard.title == "VesselStack System Health"' >/dev/null

echo "service integration checks passed"
