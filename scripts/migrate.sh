#!/bin/bash
# Upgrade the recorded configuration schema between releases.
#
# Usage: migrate.sh [--check] ENV_FILE TARGET_VERSION
#   --check   Validate that the upgrade is possible (no changes). The installer
#             runs this in preflight, before any file is overwritten.
#
# Data-driven: any upgrade from MIN_UPGRADE_FROM or newer is accepted. Only
# releases that ship scripts/migrations/<version>.sh run extra steps; otherwise
# just the version marker changes. Downgrades are never supported (apart from
# the documented 1.x -> 0.x renumbering shim below).

set -euo pipefail

CHECK=0
if [ "${1:-}" = --check ]; then CHECK=1; shift; fi
ENV_FILE=${1:?Usage: migrate.sh [--check] ENV_FILE TARGET_VERSION}
TARGET_VERSION=${2:?Usage: migrate.sh [--check] ENV_FILE TARGET_VERSION}
MIN_UPGRADE_FROM=0.0.1
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

# One-time renumbering shim (2026-10-25): releases 1.0.0-1.4.1 were renumbered to
# 0.0.1-0.4.1 (1.0.0 -> 0.0.1, 1.0.1 -> 0.0.2, 1.M.P -> 0.M.P for M >= 1). A machine
# that recorded a former 1.x version is treated as the matching 0.x version when
# the target is a 0.x release, so it upgrades instead of being refused as a
# downgrade. Anything above the mapped range (for example 1.5.0 -> 0.5.0) still
# compares as newer than the target and is refused as usual. Remove once no 1.x
# installation can remain.
if [[ "$current_version" =~ ^1\.([0-9]+)\.([0-9]+)$ ]] && [[ "$TARGET_VERSION" == 0.* ]]; then
    legacy_version=$current_version
    if [ "${BASH_REMATCH[1]}" -eq 0 ]; then
        current_version="0.0.$((BASH_REMATCH[2] + 1))"
    else
        current_version="0.${BASH_REMATCH[1]}.${BASH_REMATCH[2]}"
    fi
    echo "Installed version $legacy_version was renumbered; treating it as $current_version" >&2
fi

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
        if ! { [ -x "$script" ] && [ ! -L "$script" ]; }; then
            echo "Migration step is not an executable regular file: $script" >&2
            exit 1
        fi
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
