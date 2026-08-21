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

for image in "${images[@]}"; do
    manifest=$(mktemp)
    docker manifest inspect "$image" > "$manifest"
    jq -e '[.manifests[]?.platform | select(.os == "linux" and .architecture == "arm64")] | length > 0' \
        "$manifest" >/dev/null || {
        echo "Image has no Linux ARM64 manifest: $image" >&2
        exit 1
    }
    rm -f -- "$manifest"
    manifest=""
    echo "Linux ARM64 available: $image"
done
