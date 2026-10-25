#!/bin/bash

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
bash -n "$ROOT/install.sh" "$ROOT/build-package.sh" "$ROOT/scripts/bootstrap-influx.sh" \
    "$ROOT/scripts/vesselstackctl" "$ROOT/scripts/install-signalk-native.sh" \
    "$ROOT/scripts/configure-hardware.sh" "$ROOT/scripts/vesselstack-firewall" \
    "$ROOT/scripts/migrate.sh" "$ROOT/tests/test-migrations.sh" \
    "$ROOT/tests/test-clean-install.sh" "$ROOT/tests/test-image-manifests.sh" \
    "$ROOT/tests/test-service-integration.sh" "$ROOT/tests/test-displays-mosquitto.sh" \
    "$ROOT/tests/test-displays-bridge.sh" "$ROOT/tests/test-mosquitto-upgrade.sh" \
    "$ROOT/tests/test-displays-envfile-systemd.sh" "$ROOT/scripts/install-displays-venv.sh" \
    "$ROOT/tests/test-displays-esphome-config.sh" "$ROOT/tests/compile-display-firmware.sh"
python3 -m py_compile "$ROOT/scripts/pin-displays-requirements.py"
python3 -m py_compile "$ROOT/control-panel/app.py"
python3 -m py_compile "$ROOT/vesselstack-wizard.py"
for f in "$ROOT"/displays/*.py "$ROOT"/tests/fixtures/*.py; do python3 -m py_compile "$f"; done
python3 -m json.tool "$ROOT/displays/boards/registry.json" >/dev/null
python3 "$ROOT/displays/displayctl.py" validate >/dev/null
# The ESPHome image pin lives in displays/esphome/IMAGE only; the README must quote the same digest.
IMAGE_PIN=$(tr -d '[:space:]' < "$ROOT/displays/esphome/IMAGE")
grep -Eq '^ghcr\.io/esphome/esphome:[0-9.]+@sha256:[0-9a-f]{64}$' <<<"$IMAGE_PIN"
grep -qF "$IMAGE_PIN" "$ROOT/README.md"
grep -qF "esphome==$(tr -d '[:space:]' < "$ROOT/displays/esphome/VERSION")" "$ROOT/README.md"
grep -qF ":$(tr -d '[:space:]' < "$ROOT/displays/esphome/VERSION")@" <<<"$IMAGE_PIN"
shellcheck "$ROOT/install.sh" "$ROOT/build-package.sh" "$ROOT/scripts/bootstrap-influx.sh" \
    "$ROOT/scripts/vesselstackctl" "$ROOT/scripts/install-signalk-native.sh" \
    "$ROOT/scripts/configure-hardware.sh" "$ROOT/scripts/vesselstack-firewall" \
    "$ROOT/scripts/migrate.sh" "$ROOT/tests/test-migrations.sh" \
    "$ROOT/tests/test-clean-install.sh" "$ROOT/tests/test-image-manifests.sh" \
    "$ROOT/tests/test-service-integration.sh" "$ROOT/tests/test-displays-mosquitto.sh" \
    "$ROOT/tests/test-displays-bridge.sh" "$ROOT/tests/test-lifecycle.sh" \
    "$ROOT/tests/test-mosquitto-upgrade.sh" "$ROOT/tests/test-displays-envfile-systemd.sh" \
    "$ROOT/scripts/install-displays-venv.sh" "$ROOT/tests/test-displays-esphome-config.sh" "$ROOT/tests/compile-display-firmware.sh"
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
test -s "$ROOT/systemd/vesselstack-displays-bridge.service.in"
grep -q '^EnvironmentFile=@VESSELSTACK_ROOT@/config/displays-bridge.env$' \
    "$ROOT/systemd/vesselstack-displays-bridge.service.in"
grep -q '^ExecStart=@VESSELSTACK_ROOT@/displays/.venv/bin/python @VESSELSTACK_ROOT@/displays/bridge.py$' \
    "$ROOT/systemd/vesselstack-displays-bridge.service.in"
# Every displays dependency is pinned exactly and by hash (installed with --require-hashes).
REQ="$ROOT/displays/requirements.txt"
test "$(grep -cE '^[A-Za-z]' "$REQ")" -ge 2
test "$(grep -cE '^[A-Za-z0-9._-]+==[0-9][0-9.]* \\$' "$REQ")" -eq "$(grep -cE '^[A-Za-z]' "$REQ")"
if grep -vE '^$|^#|^[A-Za-z0-9._-]+==[0-9][0-9.]* \\$|^    --hash=sha256:[0-9a-f]{64}( \\)?$' "$REQ"; then
    echo "displays/requirements.txt must contain only hash-pinned requirements" >&2
    exit 1
fi
grep -q -- '--require-hashes' "$ROOT/scripts/install-displays-venv.sh"
grep -q '^RestartPreventExitStatus=2 3$' "$ROOT/systemd/vesselstack-displays-bridge.service.in"
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
