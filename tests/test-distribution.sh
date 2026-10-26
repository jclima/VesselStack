#!/bin/bash

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
bash -n "$ROOT/install.sh" "$ROOT/build-package.sh" "$ROOT/scripts/bootstrap-influx.sh" \
    "$ROOT/scripts/vesselstackctl" "$ROOT/scripts/install-signalk-native.sh" \
    "$ROOT/scripts/configure-hardware.sh" "$ROOT/scripts/vesselstack-firewall" \
    "$ROOT/scripts/migrate.sh" "$ROOT/scripts/memory-index.sh" "$ROOT/tests/test-migrations.sh" \
    "$ROOT/tests/test-clean-install.sh" "$ROOT/tests/test-image-manifests.sh" \
    "$ROOT/tests/test-service-integration.sh" "$ROOT/tests/test-displays-mosquitto.sh" \
    "$ROOT/tests/test-displays-bridge.sh" "$ROOT/tests/test-mosquitto-upgrade.sh" \
    "$ROOT/tests/test-displays-envfile-systemd.sh" "$ROOT/scripts/install-displays-venv.sh" \
    "$ROOT/tests/test-displays-esphome-config.sh" "$ROOT/tests/compile-display-firmware.sh" \
    "$ROOT/scripts/prepare-offline-bundle.sh" \
    "$ROOT/tests/test-build-package.sh" "$ROOT/tests/test-socketcan.sh" "$ROOT/tests/test-offline-install.sh" "$ROOT/tests/test-update-launcher.sh" \
    "$ROOT/scripts/update-requirements-lock.sh" "$ROOT/scripts/get-vesselstack.sh" "$ROOT/tests/test-get-vesselstack.sh"
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
    "$ROOT/scripts/migrate.sh" "$ROOT/scripts/memory-index.sh" "$ROOT/tests/test-migrations.sh" \
    "$ROOT/tests/test-clean-install.sh" "$ROOT/tests/test-image-manifests.sh" \
    "$ROOT/tests/test-service-integration.sh" "$ROOT/tests/test-displays-mosquitto.sh" \
    "$ROOT/tests/test-displays-bridge.sh" "$ROOT/tests/test-lifecycle.sh" \
    "$ROOT/tests/test-mosquitto-upgrade.sh" "$ROOT/tests/test-displays-envfile-systemd.sh" \
    "$ROOT/scripts/install-displays-venv.sh" "$ROOT/tests/test-displays-esphome-config.sh" "$ROOT/tests/compile-display-firmware.sh" \
    "$ROOT/scripts/prepare-offline-bundle.sh" \
    "$ROOT/tests/test-build-package.sh" "$ROOT/tests/test-socketcan.sh" "$ROOT/tests/test-offline-install.sh" "$ROOT/tests/test-update-launcher.sh" \
    "$ROOT/scripts/update-requirements-lock.sh" "$ROOT/scripts/get-vesselstack.sh" "$ROOT/tests/test-get-vesselstack.sh"
if grep -nE 'mosquitto_passwd -b' "$ROOT/install.sh" "$ROOT/scripts/vesselstackctl" "$ROOT"/scripts/*.sh "$ROOT"/displays/*.py; then
    echo "MQTT passwords must not be passed on a command line" >&2
    exit 1
fi
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
grep -q '^MemoryMax=128M$' "$ROOT/systemd/vesselstack-displays-bridge.service.in"
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
"$ROOT/scripts/vesselstackctl" --help | grep -q 'reindex  *Rebuild Boat Chat'
test -x "$ROOT/scripts/memory-index.sh"
# The memory index must be built by the installer (Boat Chat is not ready without it), and as
# the service user, never by root directly.
grep -qF 'scripts/memory-index.sh" "$VESSELSTACK_ROOT/config/vesselstack.env" ensure' "$ROOT/install.sh"
grep -qF 'runuser -u "$VESSELSTACK_USER"' "$ROOT/scripts/memory-index.sh"
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
# The one-line bootstrap: wrapped so a truncated download cannot half-execute, with
# the default version in step with VERSION (build-package.sh bakes it for the release).
test -x "$ROOT/scripts/get-vesselstack.sh"
test "$(tail -n 1 "$ROOT/scripts/get-vesselstack.sh")" = 'main "$@"'
grep -qx "DEFAULT_VERSION=\"$(<"$ROOT/VERSION")\"" "$ROOT/scripts/get-vesselstack.sh" \
    || { echo "scripts/get-vesselstack.sh DEFAULT_VERSION must equal VERSION" >&2; exit 1; }
if grep -nE '\beval\b|/tmp/' "$ROOT/scripts/get-vesselstack.sh" \
    || grep -n 'mktemp' "$ROOT/scripts/get-vesselstack.sh" | grep -v 'RELEASE_BASE'; then
    echo "get-vesselstack.sh must not use eval, /tmp or a mktemp outside the root-owned release directory" >&2
    exit 1
fi
test -x "$ROOT/tests/test-get-vesselstack.sh"
"$ROOT/scripts/get-vesselstack.sh" --help | grep -q -- '--offline'
# SignalK docker data directory: created for uid/gid 1000 only in docker mode and
# before compose up (Docker would otherwise create the mount source as root:root).
# shellcheck disable=SC2016  # Literal source text, not an expansion.
grep -q '^if \[ "\$SIGNALK_MODE" = docker \]; then$' "$ROOT/install.sh"
signalk_line=$(grep -n '^    ensure_signalk_docker_dir$' "$ROOT/install.sh" | cut -d: -f1)
compose_line=$(grep -n '^    compose_up=(compose up -d)$' "$ROOT/install.sh" | cut -d: -f1)
[ -n "$signalk_line" ] && [ -n "$compose_line" ] && [ "$signalk_line" -lt "$compose_line" ] \
    || { echo "install.sh must create the SignalK data directory before compose up" >&2; exit 1; }
grep -q '^SIGNALK_CONTAINER_UID=1000$' "$ROOT/install.sh"
# shellcheck disable=SC2016 # literal compose syntax
grep -qF '${VESSELSTACK_DATA}/prometheus:/prometheus' "$ROOT/templates/compose.yml"
# shellcheck disable=SC2016
grep -qF -- '--storage.tsdb.retention.time=${PROMETHEUS_RETENTION:-30d}' "$ROOT/templates/compose.yml"
grep -qF -- '--storage.tsdb.path=/prometheus' "$ROOT/templates/compose.yml"
grep -qF 'config/prometheus.yml:/etc/prometheus/prometheus.yml:ro' "$ROOT/templates/compose.yml"
grep -q '^PROMETHEUS_CONTAINER_UID=65534$' "$ROOT/install.sh"
# shellcheck disable=SC2016
prom_line=$(grep -n '^ensure_container_data_dir "$VESSELSTACK_DATA/prometheus"' "$ROOT/install.sh" | cut -d: -f1)
[ -n "$prom_line" ] && [ "$prom_line" -lt "$compose_line" ] \
    || { echo "install.sh must create the Prometheus data directory before compose up" >&2; exit 1; }
echo "distribution static checks passed"
