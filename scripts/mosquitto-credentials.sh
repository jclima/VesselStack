#!/bin/bash
# Create or update the Mosquitto password file without resetting it needlessly.
#
# `mosquitto_passwd -c` truncates the file, so it is used only when the file is
# missing. Otherwise the configured user is updated in place (other users are
# kept) and only when MQTT_USERNAME/MQTT_PASSWORD changed since the last run,
# tracked by a SHA-256 fingerprint stored in the root-only config
# directory (never in the Mosquitto data directory).

set -euo pipefail
umask 077

ENV_FILE=${1:-/opt/vesselstack/config/vesselstack.env}
# shellcheck disable=SC1090
source "$ENV_FILE"
: "${VESSELSTACK_ROOT:?Missing VESSELSTACK_ROOT}"
: "${VESSELSTACK_DATA:?Missing VESSELSTACK_DATA}"
: "${MQTT_USERNAME:?Missing MQTT_USERNAME}"
: "${MQTT_PASSWORD:?Missing MQTT_PASSWORD}"
[ "$MQTT_PASSWORD" != GENERATE ] || { echo "MQTT_PASSWORD was never generated" >&2; exit 1; }

MOSQUITTO_IMAGE=${MOSQUITTO_IMAGE:-eclipse-mosquitto:2.1.2-alpine}
passwords="$VESSELSTACK_DATA/mosquitto/passwords"
fingerprint_file="$VESSELSTACK_ROOT/config/.mqtt-credentials.sha256"
fingerprint=$(printf '%s\n%s' "$MQTT_USERNAME" "$MQTT_PASSWORD" | sha256sum | cut -d' ' -f1)

if [ -s "$passwords" ] && [ -r "$fingerprint_file" ] \
    && [ "$(<"$fingerprint_file")" = "$fingerprint" ]; then
    echo "MQTT credentials unchanged; password file left as is"
    exit 0
fi

install -d -m 0750 "$VESSELSTACK_DATA/mosquitto"
mode_flags=(-b)
[ -s "$passwords" ] || mode_flags=(-b -c)
# The password is passed to mosquitto_passwd as an argument inside a short-lived
# container, exactly as before; it is not visible on the host process list
# beyond that container's lifetime.
docker run --rm -v "$VESSELSTACK_DATA/mosquitto:/mosquitto/data" \
    "$MOSQUITTO_IMAGE" mosquitto_passwd "${mode_flags[@]}" \
    /mosquitto/data/passwords "$MQTT_USERNAME" "$MQTT_PASSWORD"
printf '%s\n' "$fingerprint" > "$fingerprint_file"
chmod 0600 "$fingerprint_file"
if [ "${mode_flags[*]}" = "-b -c" ]; then
    echo "Created Mosquitto password file"
else
    echo "Updated Mosquitto credentials for $MQTT_USERNAME (other users preserved)"
fi
