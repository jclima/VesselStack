#!/bin/bash

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
bash -n "$ROOT/install.sh" "$ROOT/build-package.sh" "$ROOT/scripts/bootstrap-influx.sh" \
    "$ROOT/scripts/vesselstackctl" "$ROOT/scripts/install-signalk-native.sh" \
    "$ROOT/scripts/configure-hardware.sh" "$ROOT/scripts/vesselstack-firewall" \
    "$ROOT/scripts/migrate.sh" "$ROOT/scripts/mosquitto-credentials.sh" "$ROOT/tests/test-migrations.sh" \
    "$ROOT/tests/test-clean-install.sh" "$ROOT/tests/test-image-manifests.sh" \
    "$ROOT/tests/test-service-integration.sh" "$ROOT/scripts/prepare-offline-bundle.sh" \
    "$ROOT/tests/test-build-package.sh" "$ROOT/tests/test-socketcan.sh" "$ROOT/tests/test-offline-install.sh" "$ROOT/tests/test-update-launcher.sh" \
    "$ROOT/scripts/update-requirements-lock.sh"
python3 -m py_compile "$ROOT/control-panel/app.py"
python3 -m py_compile "$ROOT/vesselstack-wizard.py"
shellcheck "$ROOT/install.sh" "$ROOT/build-package.sh" "$ROOT/scripts/bootstrap-influx.sh" \
    "$ROOT/scripts/vesselstackctl" "$ROOT/scripts/install-signalk-native.sh" \
    "$ROOT/scripts/configure-hardware.sh" "$ROOT/scripts/vesselstack-firewall" \
    "$ROOT/scripts/migrate.sh" "$ROOT/scripts/mosquitto-credentials.sh" "$ROOT/tests/test-migrations.sh" \
    "$ROOT/tests/test-clean-install.sh" "$ROOT/tests/test-image-manifests.sh" \
    "$ROOT/tests/test-service-integration.sh" "$ROOT/scripts/prepare-offline-bundle.sh" \
    "$ROOT/tests/test-build-package.sh" "$ROOT/tests/test-socketcan.sh" "$ROOT/tests/test-offline-install.sh" "$ROOT/tests/test-update-launcher.sh" \
    "$ROOT/scripts/update-requirements-lock.sh"
test -s "$ROOT/boat-chat/requirements.lock"
grep -q -- '--hash=sha256:' "$ROOT/boat-chat/requirements.lock"
grep -q 'GENERATE' "$ROOT/vesselstack.example.env"
grep -q 'INFLUXDB_HISTORY_BUCKET' "$ROOT/vesselstack.example.env"
grep -q 'verify-backup' "$ROOT/scripts/vesselstackctl"
grep -q 'CONTROL_PANEL_TOKEN' "$ROOT/control-panel/app.py"
test -s "$ROOT/systemd/vesselstack-control-panel.service.in"
test -s "$ROOT/systemd/vesselstack-chat-telegram.service.in"
test -s "$ROOT/systemd/vesselstack-telemetry-indexer.service.in"
test -s "$ROOT/systemd/vesselstack-telemetry-indexer.timer"
test -s "$ROOT/docs/INSTALLATION.md"
test -s "$ROOT/docs/BOM.md"
test -s "$ROOT/wizard/index.html"
test -s "$ROOT/docs/images/wizard-preflight.png"
test -s "$ROOT/docs/images/wizard-nmea.png"
grep -q 'VesselStack InfluxDB' "$ROOT/templates/grafana/provisioning/datasources/vesselstack.yml"
python3 -m json.tool "$ROOT/templates/grafana/dashboards/system-health.json" >/dev/null
grep -q 'cr.signalk.io/signalk/signalk-server:${SIGNALK_VERSION}' "$ROOT/templates/compose.yml"
grep -q 'ghcr.io/jvde-github/ais-catcher:v0.70' "$ROOT/vesselstack.example.env"
test "$(find "$ROOT/templates/homeassistant/blueprints" -name '*.yaml' | wc -l)" -eq 3
grep -q 'sensor.vesselstack_battery_voltage' \
    "$ROOT/templates/homeassistant/lovelace-vesselstack.example.yaml"
"$ROOT/scripts/vesselstackctl" --help | grep -q 'backup \[--allow-unmounted\] \[DIRECTORY\]'
if grep -RInE --exclude=test-distribution.sh \
    --exclude-dir=.git --exclude-dir=generated \
    '(BOAT_MMSI="[1-9][0-9]+"|BOAT_CALLSIGN="[A-Z0-9]+"|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY)' "$ROOT"; then
    echo "Distribution contains populated vessel identity or private key data" >&2
    exit 1
fi
for unit in vesselstack-chat vesselstack-chat-telegram vesselstack-telemetry-indexer; do
    for directive in ProtectSystem=strict ProtectHome=true NoNewPrivileges=true PrivateTmp=true \
        'ReadWritePaths=@VESSELSTACK_DATA@/boat-chat' 'BOAT_CHAT_STATE_DIR=@VESSELSTACK_DATA@/boat-chat' \
        'BOAT_CHAT_ENV=@VESSELSTACK_DATA@/boat-chat/boat-chat.env'; do
        grep -qxF "$directive" "$ROOT/systemd/$unit.service.in" \
            || grep -qxF "Environment=$directive" "$ROOT/systemd/$unit.service.in" \
            || { echo "$unit.service.in is missing $directive" >&2; exit 1; }
    done
done
# shellcheck disable=SC2016  # Literal pattern, not an expansion.
if grep -n 'chown -R "\$VESSELSTACK_USER:\$VESSELSTACK_USER" "\$VESSELSTACK_ROOT/boat-chat"' "$ROOT/install.sh"; then
    echo "install.sh must not make application code/venv writable by the service user" >&2
    exit 1
fi
grep -q 'BOAT_CHAT_ACCESS_TOKEN="GENERATE"' "$ROOT/vesselstack.example.env"
for pattern in '${VESSELSTACK_DATA_BIND_ADDRESS:-127.0.0.1}:${INFLUXDB_PORT:-8086}:8086' \
    '${VESSELSTACK_DATA_BIND_ADDRESS:-127.0.0.1}:${PROMETHEUS_PORT:-9090}:9090' \
    '${VESSELSTACK_DATA_BIND_ADDRESS:-127.0.0.1}:${MQTT_PORT:-1883}:1883'; do
    grep -qF "$pattern" "$ROOT/templates/compose.yml" \
        || { echo "compose.yml must publish $pattern on the data bind address" >&2; exit 1; }
done
grep -q 'INFLUXDB_RAW_RETENTION' "$ROOT/vesselstack.example.env"
grep -qF 'DOCKER_INFLUXDB_INIT_RETENTION: ${INFLUXDB_RAW_RETENTION:-30d}' "$ROOT/templates/compose.yml"
# The Influx admin token must never be a command-line argument.
if grep -nE -- '--token' "$ROOT/scripts/bootstrap-influx.sh"; then
    echo "bootstrap-influx.sh must pass the token via INFLUX_TOKEN, not argv" >&2
    exit 1
fi
grep -qF '@CAN_LINK_ARGS@' "$ROOT/systemd/vesselstack-socketcan.service.in"
grep -q 'VESSELSTACK_CAN_CREATE_VCAN' "$ROOT/vesselstack.example.env"
test -x "$ROOT/scripts/prepare-offline-bundle.sh"
python3 -m unittest "$ROOT/tests/test_access_security.py"
# Quarantined legacy state must live outside the service-writable data tree, and
# root must never execute the update helper from a source checkout.
# shellcheck disable=SC2016  # Literal pattern, not an expansion.
if grep -n 'mktemp -d "\$VESSELSTACK_DATA/boat-chat-legacy' "$ROOT/install.sh"; then
    echo "install.sh must not create the legacy quarantine inside VESSELSTACK_DATA" >&2
    exit 1
fi
grep -qF '$LIBEXEC_DIR/deploy-chat.py' "$ROOT/install.sh"
grep -qF 'exec python3 -I "$HELPER"' "$ROOT/scripts/vesselstack-update"
bash -n "$ROOT/scripts/vesselstack-update"
shellcheck "$ROOT/scripts/vesselstack-update"
echo "distribution static checks passed"
