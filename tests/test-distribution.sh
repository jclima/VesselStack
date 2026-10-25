#!/bin/bash

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
bash -n "$ROOT/install.sh" "$ROOT/build-package.sh" "$ROOT/scripts/bootstrap-influx.sh" \
    "$ROOT/scripts/vesselstackctl" "$ROOT/scripts/install-signalk-native.sh" \
    "$ROOT/scripts/configure-hardware.sh" "$ROOT/scripts/vesselstack-firewall" \
    "$ROOT/scripts/migrate.sh" "$ROOT/tests/test-migrations.sh" \
    "$ROOT/tests/test-clean-install.sh" "$ROOT/tests/test-image-manifests.sh" \
    "$ROOT/tests/test-service-integration.sh" "$ROOT/tests/test-displays-mosquitto.sh"
python3 -m py_compile "$ROOT/control-panel/app.py"
python3 -m py_compile "$ROOT/vesselstack-wizard.py"
for f in "$ROOT"/displays/*.py; do python3 -m py_compile "$f"; done
python3 -m json.tool "$ROOT/displays/boards/registry.json" >/dev/null
python3 "$ROOT/displays/displayctl.py" validate >/dev/null
shellcheck "$ROOT/install.sh" "$ROOT/build-package.sh" "$ROOT/scripts/bootstrap-influx.sh" \
    "$ROOT/scripts/vesselstackctl" "$ROOT/scripts/install-signalk-native.sh" \
    "$ROOT/scripts/configure-hardware.sh" "$ROOT/scripts/vesselstack-firewall" \
    "$ROOT/scripts/migrate.sh" "$ROOT/tests/test-migrations.sh" \
    "$ROOT/tests/test-clean-install.sh" "$ROOT/tests/test-image-manifests.sh" \
    "$ROOT/tests/test-service-integration.sh" "$ROOT/tests/test-displays-mosquitto.sh" \
    "$ROOT/tests/test-lifecycle.sh"
if grep -nE 'mosquitto_passwd -b' "$ROOT/install.sh" "$ROOT/scripts/vesselstackctl" "$ROOT"/displays/*.py; then
    echo "MQTT passwords must not be passed on a command line" >&2
    exit 1
fi
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
"$ROOT/scripts/vesselstackctl" --help | grep -q 'backup \[DIRECTORY\]'
if grep -RInE --exclude=test-distribution.sh \
    --exclude-dir=.git --exclude-dir=generated \
    '(BOAT_MMSI="[1-9][0-9]+"|BOAT_CALLSIGN="[A-Z0-9]+"|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY)' "$ROOT"; then
    echo "Distribution contains populated vessel identity or private key data" >&2
    exit 1
fi
bash -n "$ROOT/scripts/vesselstack-update"
shellcheck "$ROOT/scripts/vesselstack-update"
echo "distribution static checks passed"
