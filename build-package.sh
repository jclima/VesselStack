#!/bin/bash
# Build a self-contained public tarball without live settings or vessel data.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
VERSION=$(<"$SCRIPT_DIR/VERSION")
PACKAGE_NAME="vesselstack-$VERSION"
STAGE=$(mktemp -d)
GENERATED_DIR="$SCRIPT_DIR/generated"
OUTPUT="$GENERATED_DIR/$PACKAGE_NAME.tar.gz"
install -d "$GENERATED_DIR"
ARCHIVE_TMP=$(mktemp "$GENERATED_DIR/.${PACKAGE_NAME}.tar.gz.tmp.XXXXXX")
CHECKSUM_TMP=$(mktemp "$GENERATED_DIR/.${PACKAGE_NAME}.tar.gz.sha256.tmp.XXXXXX")

cleanup() {
    rm -rf -- "$STAGE"
    rm -f -- "$ARCHIVE_TMP" "$CHECKSUM_TMP"
}
trap cleanup EXIT

install -d "$STAGE/$PACKAGE_NAME"

# Keep this manifest deliberately explicit. Runtime configuration, generated
# output, tests, and repository metadata are not part of a public install.
release_files=(LICENSE NOTICE README.md SECURITY.md THIRD_PARTY_NOTICES.md CHANGELOG.md VALIDATION.md CONTRIBUTING.md VERSION \
    install.sh build-package.sh vesselstack.example.env \
    boat-chat/BOAT_CHAT_AGENT.md boat-chat/POWER_MONITORING.md boat-chat/README.md \
    boat-chat/app.py boat-chat/boat_facts.json boat-chat/knowledge/answering.md \
    boat-chat/knowledge/telemetry.md boat-chat/memory_index.py boat-chat/requirements.txt \
    boat-chat/static/app.js boat-chat/static/favicon.svg boat-chat/static/index.html \
    boat-chat/static/styles.css boat-chat/telegram_bot.py boat-chat/telemetry_cache.py \
    boat-chat/telemetry_indexer.py control-panel/app.py control-panel/static/app.js \
    control-panel/static/index.html control-panel/static/styles.css \
    scripts/bootstrap-influx.sh scripts/configure-hardware.sh scripts/install-signalk-native.sh \
    scripts/migrate.sh scripts/vesselstack-firewall scripts/vesselstackctl \
    systemd/vesselstack-chat.service.in systemd/vesselstack-control-panel.service.in \
    systemd/vesselstack-firewall.service.in systemd/vesselstack-signalk.service.in \
    systemd/vesselstack-socketcan.service.in templates/compose.yml \
    templates/grafana/dashboards/system-health.json \
    templates/grafana/provisioning/dashboards/vesselstack.yml \
    templates/grafana/provisioning/datasources/vesselstack.yml \
    templates/homeassistant/blueprints/automation/vesselstack/bilge-active.yaml \
    templates/homeassistant/blueprints/automation/vesselstack/low-battery.yaml \
    templates/homeassistant/blueprints/automation/vesselstack/shore-power-lost.yaml \
    templates/homeassistant/lovelace-vesselstack.example.yaml templates/mosquitto.conf \
    templates/prometheus.yml)
for path in "${release_files[@]}"; do
    case "$path" in /*|../*|*/../*) echo "Invalid release manifest path: $path" >&2; exit 1 ;; esac
    current="$SCRIPT_DIR"
    old_ifs=$IFS; IFS=/
    for component in $path; do
        [ -L "$current/$component" ] && { echo "Symlink in release manifest path: $path" >&2; exit 1; }
        current="$current/$component"
    done
    IFS=$old_ifs
    [ -f "$SCRIPT_DIR/$path" ] || { echo "Missing release file: $path" >&2; exit 1; }
    mkdir -p "$STAGE/$PACKAGE_NAME/$(dirname "$path")"
    case "$path" in
        install.sh|build-package.sh|scripts/*)
            [ -x "$SCRIPT_DIR/$path" ] || { echo "Release executable is not executable: $path" >&2; exit 1; }
            mode=0755 ;;
        *) mode=0644 ;;
    esac
    install -m "$mode" "$SCRIPT_DIR/$path" "$STAGE/$PACKAGE_NAME/$path"
done

# Signal K and AIS-catcher are optional at runtime, but their installers and
# pinned service definitions are mandatory release-package contents. Fail the
# build instead of publishing a tarball that silently drops either component.
required_component_files=(
    templates/compose.yml
    vesselstack.example.env
    scripts/install-signalk-native.sh
    systemd/vesselstack-signalk.service.in
)
for component_file in "${required_component_files[@]}"; do
    [ -s "$STAGE/$PACKAGE_NAME/$component_file" ] || {
        echo "Refusing to package without required component file: $component_file" >&2
        exit 1
    }
done
grep -q '^  signalk:$' "$STAGE/$PACKAGE_NAME/templates/compose.yml" || {
    echo "Refusing to package without the Signal K Compose service" >&2
    exit 1
}
grep -q '^  ais-catcher:$' "$STAGE/$PACKAGE_NAME/templates/compose.yml" || {
    echo "Refusing to package without the AIS-catcher Compose service" >&2
    exit 1
}
for pin in SIGNALK_VERSION AIS_IMAGE; do
    value=$(sed -n "s/^${pin}=\"\{0,1\}\([^\"]*\)\"\{0,1\}$/\1/p" "$STAGE/$PACKAGE_NAME/vesselstack.example.env")
    [ -n "$value" ] || { echo "Refusing to package without a non-empty $pin" >&2; exit 1; }
    case "$value" in
        latest|*:latest|*/latest) echo "Refusing floating $pin: $value" >&2; exit 1 ;;
    esac
    if [ "$pin" = SIGNALK_VERSION ]; then
        printf '%s\n' "$value" | grep -Eq '^v?[0-9]+\.[0-9]+\.[0-9]+$' || { echo "Refusing unpinned $pin: $value" >&2; exit 1; }
    else
        printf '%s\n' "$value" | grep -Eq '^ghcr\.io/jvde-github/ais-catcher:(v)?[0-9]+\.[0-9]+(\.[0-9]+)?$|^ghcr\.io/jvde-github/ais-catcher@sha256:[a-fA-F0-9]{64}$' || { echo "Refusing unpinned $pin: $value" >&2; exit 1; }
    fi
done
grep -Fq "image: cr.signalk.io/signalk/signalk-server:\${SIGNALK_VERSION}" "$STAGE/$PACKAGE_NAME/templates/compose.yml" || exit 1
grep -Fq "image: \${AIS_IMAGE}" "$STAGE/$PACKAGE_NAME/templates/compose.yml" || exit 1
grep -Fq 'profiles: ["signalk"]' "$STAGE/$PACKAGE_NAME/templates/compose.yml" || exit 1
grep -Fq 'profiles: ["ais"]' "$STAGE/$PACKAGE_NAME/templates/compose.yml" || exit 1
grep -Fq "signalk-server@\${SIGNALK_VERSION#v}" "$STAGE/$PACKAGE_NAME/scripts/install-signalk-native.sh" || exit 1

if grep -RInE --exclude=build-package.sh \
    '(BOAT_MMSI="[1-9][0-9]+"|BOAT_CALLSIGN="[A-Z0-9]+"|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY)' \
    "$STAGE/$PACKAGE_NAME"; then
    echo "Refusing to package populated identity or credential fields" >&2
    exit 1
fi

tar -czf "$ARCHIVE_TMP" -C "$STAGE" "$PACKAGE_NAME"
for component_file in "${required_component_files[@]}"; do
    tar -tzf "$ARCHIVE_TMP" "$PACKAGE_NAME/$component_file" >/dev/null || {
        echo "Release archive is missing required component file: $component_file" >&2
        exit 1
    }
done
(
    cd "$GENERATED_DIR"
    sha256sum "$(basename "$ARCHIVE_TMP")" | sed "s/$(basename "$ARCHIVE_TMP")/$PACKAGE_NAME.tar.gz/" > "$CHECKSUM_TMP"
)
mv -f -- "$ARCHIVE_TMP" "$OUTPUT"
mv -f -- "$CHECKSUM_TMP" "$OUTPUT.sha256"
echo "$OUTPUT"
