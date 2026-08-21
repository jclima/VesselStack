#!/bin/bash
# Apply explicit, ordered configuration migrations between releases.

set -euo pipefail

ENV_FILE=${1:?Usage: migrate.sh ENV_FILE TARGET_VERSION}
TARGET_VERSION=${2:?Usage: migrate.sh ENV_FILE TARGET_VERSION}
# shellcheck disable=SC1090
source "$ENV_FILE"

version_file="$VESSELSTACK_ROOT/config/installed-version"
current_version=0.1.0
[ -r "$version_file" ] && current_version=$(<"$version_file")

case "$current_version:$TARGET_VERSION" in
    "$TARGET_VERSION:$TARGET_VERSION") ;;
    0.1.0:1.0.0|0.1.0:1.0.1)
        # The 1.0 installer renders new bucket, integration, hardware, and
        # firewall defaults before this migration runs. This explicit edge
        # records that the old layout was accepted and upgraded.
        ;;
    *)
        echo "No supported migration path from $current_version to $TARGET_VERSION" >&2
        exit 1
        ;;
esac

printf '%s\n' "$TARGET_VERSION" > "$version_file.tmp"
chmod 0644 "$version_file.tmp"
mv -f -- "$version_file.tmp" "$version_file"
printf 'Configuration schema is at %s\n' "$TARGET_VERSION"
