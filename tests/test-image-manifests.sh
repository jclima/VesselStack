#!/bin/bash

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
mapfile -t images < <(
    docker compose --profile signalk --profile ais \
        --env-file "$ROOT/vesselstack.example.env" \
        -f "$ROOT/templates/compose.yml" config --images | sort -u
)
manifest=""
trap 'rm -f -- "$manifest"' EXIT

inspect_manifest() {
    local image=$1 destination=$2 attempt
    for attempt in 1 2 3; do
        if docker manifest inspect "$image" > "$destination"; then
            return 0
        fi
        if [ "$attempt" -lt 3 ]; then
            echo "Registry query failed for $image; retrying ($attempt/3)" >&2
            sleep "$((attempt * 2))"
        fi
    done
    echo "Unable to retrieve manifest after 3 attempts: $image" >&2
    return 1
}

for image in "${images[@]}"; do
    manifest=$(mktemp)
    inspect_manifest "$image" "$manifest"
    jq -e '[.manifests[]?.platform | select(.os == "linux" and .architecture == "arm64")] | length > 0' \
        "$manifest" >/dev/null || {
        echo "Image has no Linux ARM64 manifest: $image" >&2
        exit 1
    }
    rm -f -- "$manifest"
    manifest=""
    echo "Linux ARM64 available: $image"
done
