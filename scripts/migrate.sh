#!/bin/bash
# Upgrade the recorded configuration schema between releases.
#
# Usage: migrate.sh [--check] ENV_FILE TARGET_VERSION
#   --check   Validate that the upgrade is possible (no changes). The installer
#             runs this in preflight, before any file is overwritten.
#
# Data-driven: any upgrade from MIN_UPGRADE_FROM or newer is accepted. Only
# releases that ship scripts/migrations/<version>.sh run extra steps; otherwise
# just the version marker changes. Downgrades are never supported.

set -euo pipefail

CHECK=0
if [ "${1:-}" = --check ]; then CHECK=1; shift; fi
ENV_FILE=${1:?Usage: migrate.sh [--check] ENV_FILE TARGET_VERSION}
TARGET_VERSION=${2:?Usage: migrate.sh [--check] ENV_FILE TARGET_VERSION}
MIN_UPGRADE_FROM=0.1.0
MIGRATIONS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/migrations
# shellcheck disable=SC1090
source "$ENV_FILE"

version_file="$VESSELSTACK_ROOT/config/installed-version"
current_version=$MIN_UPGRADE_FROM
[ -r "$version_file" ] && current_version=$(<"$version_file")

semver='^[0-9]+\.[0-9]+\.[0-9]+$'
for version in "$current_version" "$TARGET_VERSION"; do
    [[ "$version" =~ $semver ]] || {
        echo "Unrecognised version '$version' (expected MAJOR.MINOR.PATCH); cannot plan a migration" >&2
        exit 1
    }
done
version_lt() { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n 1)" = "$1" ]; }

if [ "$current_version" = "$TARGET_VERSION" ]; then
    steps=()
elif version_lt "$TARGET_VERSION" "$current_version"; then
    {
        echo "Downgrade from $current_version to $TARGET_VERSION is not supported: configuration and data may already use the newer schema."
        echo "Restore a backup made before the upgrade (vesselstackctl restore) or install $current_version or newer."
    } >&2
    exit 1
elif version_lt "$current_version" "$MIN_UPGRADE_FROM"; then
    echo "No supported migration path from $current_version to $TARGET_VERSION (oldest supported: $MIN_UPGRADE_FROM)" >&2
    exit 1
else
    steps=()
    if [ -d "$MIGRATIONS_DIR" ]; then
        while IFS= read -r script; do
            version=$(basename "$script" .sh)
            [[ "$version" =~ $semver ]] || continue
            if version_lt "$current_version" "$version" && ! version_lt "$TARGET_VERSION" "$version"; then
                steps+=("$script")
            fi
        done < <(find "$MIGRATIONS_DIR" -maxdepth 1 -type f -name '*.sh' | sort -V)
    fi
    for script in ${steps[@]+"${steps[@]}"}; do
        [ -x "$script" ] && [ ! -L "$script" ] || {
            echo "Migration step is not an executable regular file: $script" >&2
            exit 1
        }
    done
fi

if [ "$CHECK" -eq 1 ]; then
    echo "Migration path $current_version -> $TARGET_VERSION is supported (${#steps[@]} migration step(s))"
    exit 0
fi

for script in ${steps[@]+"${steps[@]}"}; do
    echo "Running migration $(basename "$script")"
    "$script" "$ENV_FILE"
done

printf '%s\n' "$TARGET_VERSION" > "$version_file.tmp"
chmod 0644 "$version_file.tmp"
mv -f -- "$version_file.tmp" "$version_file"
printf 'Configuration schema is at %s\n' "$TARGET_VERSION"
