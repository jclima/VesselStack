#!/bin/bash
# Prepare (on a connected machine) or load (on the vessel) an offline install bundle.
#
#   prepare-offline-bundle.sh [--output DIR] [--platform TAG]... [--python-version X.Y]
#                             [--native] [--image-platform linux/arm64] [--list]
#   prepare-offline-bundle.sh --verify DIR     check MANIFEST.sha256 and the release version
#   prepare-offline-bundle.sh --load DIR       verify, then `docker load` every image
#
# The bundle holds a wheelhouse for boat-chat/requirements.txt, a `docker save`
# tarball for every image the Compose template (all profiles) and the Mosquitto
# helper use, and MANIFEST.sha256 with a checksum for each file. It contains no
# configuration, credentials or vessel data.

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
VERSION=$(<"$ROOT/VERSION")
OUTPUT="$ROOT/generated/vesselstack-offline-$VERSION"
PLATFORMS=()
PYTHON_VERSION=${VESSELSTACK_PIP_PYTHON_VERSION:-3.11}
NATIVE=0
LIST_ONLY=0
IMAGE_PLATFORM=${VESSELSTACK_IMAGE_PLATFORM:-linux/arm64}
MODE=build
BUNDLE=

usage() { sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --output) OUTPUT=$2; shift 2 ;;
        --platform) PLATFORMS+=("$2"); shift 2 ;;
        --python-version) PYTHON_VERSION=$2; shift 2 ;;
        --native) NATIVE=1; shift ;;
        --image-platform) IMAGE_PLATFORM=$2; shift 2 ;;
        --list) LIST_ONLY=1; shift ;;
        --verify) MODE=verify; BUNDLE=$2; shift 2 ;;
        --load) MODE=load; BUNDLE=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done
if [ "${#PLATFORMS[@]}" -eq 0 ]; then
    # Raspberry Pi OS (64-bit). Override with --platform for another target.
    PLATFORMS=(manylinux2014_aarch64 manylinux_2_28_aarch64 manylinux_2_17_aarch64)
fi

sha256_of() {
    if command -v sha256sum >/dev/null; then sha256sum "$@"; else shasum -a 256 "$@"; fi
}
check_manifest() {
    ( cd "$1" && if command -v sha256sum >/dev/null; then sha256sum -c MANIFEST.sha256; else shasum -a 256 -c MANIFEST.sha256; fi )
}

image_list() {
    local mosquitto
    # shellcheck disable=SC2016  # literal pattern, not an expansion
    mosquitto=$(sed -n 's/^MOSQUITTO_IMAGE=\${MOSQUITTO_IMAGE:-\(.*\)}$/\1/p' "$ROOT/scripts/mosquitto-credentials.sh")
    [ -n "$mosquitto" ] || { echo "Cannot determine the Mosquitto helper image" >&2; return 1; }
    {
        docker compose --profile signalk --profile ais --env-file "$ROOT/vesselstack.example.env" \
            -f "$ROOT/templates/compose.yml" config --images
        echo "$mosquitto"
    } | sort -u
}

verify_bundle() {
    [ -f "$BUNDLE/MANIFEST.sha256" ] || { echo "Not an offline bundle (no MANIFEST.sha256): $BUNDLE" >&2; exit 1; }
    check_manifest "$BUNDLE"
    local bundle_version
    bundle_version=$(sed -n 's/^release=//p' "$BUNDLE/BUNDLE_INFO")
    [ "$bundle_version" = "$VERSION" ] || {
        echo "Bundle was built for release '$bundle_version' but this is release $VERSION; rebuild the bundle" >&2
        exit 1
    }
}

case "$MODE" in
    verify) verify_bundle; echo "Bundle verified: $BUNDLE"; exit 0 ;;
    load)
        command -v docker >/dev/null || { echo "docker is required to load images" >&2; exit 1; }
        verify_bundle
        for archive in "$BUNDLE"/images/*.tar; do
            docker load -i "$archive"
        done
        echo "Images loaded. Install with: sudo VESSELSTACK_WHEELHOUSE=$BUNDLE/wheelhouse ./install.sh --offline --start"
        exit 0 ;;
esac

command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
IMAGES=$(image_list)
if [ "$LIST_ONLY" -eq 1 ]; then printf '%s\n' "$IMAGES"; exit 0; fi
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

rm -rf -- "$OUTPUT"
install -d -m 0755 "$OUTPUT/wheelhouse" "$OUTPUT/images"

download_args=(download --disable-pip-version-check --dest "$OUTPUT/wheelhouse" -r "$ROOT/boat-chat/requirements.txt")
if [ "$NATIVE" -eq 0 ]; then
    # Cross-platform download: only prebuilt wheels can be fetched for another target.
    download_args+=(--only-binary=:all: --python-version "$PYTHON_VERSION" --implementation cp)
    for platform in "${PLATFORMS[@]}"; do download_args+=(--platform "$platform"); done
fi
python3 -m pip "${download_args[@]}"

while IFS= read -r image; do
    [ -n "$image" ] || continue
    docker pull --platform "$IMAGE_PLATFORM" "$image"
    docker save -o "$OUTPUT/images/$(printf '%s' "$image" | tr '/:@' '___').tar" "$image"
done <<<"$IMAGES"

{
    echo "release=$VERSION"
    echo "created=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "python_version=$([ "$NATIVE" -eq 1 ] && echo native || echo "$PYTHON_VERSION")"
    echo "pip_platforms=$([ "$NATIVE" -eq 1 ] && echo native || echo "${PLATFORMS[*]}")"
    echo "image_platform=$IMAGE_PLATFORM"
    echo "requirements_sha256=$(sha256_of "$ROOT/boat-chat/requirements.txt" | cut -d' ' -f1)"
    # shellcheck disable=SC2001  # one image per line
    sed 's/^/image=/' <<<"$IMAGES"
} > "$OUTPUT/BUNDLE_INFO"
( cd "$OUTPUT" && find . -type f | LC_ALL=C sort | sed 's|^\./||' \
    | while IFS= read -r file; do sha256_of "$file"; done > "$OUTPUT.manifest.tmp" )
mv -- "$OUTPUT.manifest.tmp" "$OUTPUT/MANIFEST.sha256"

check_manifest "$OUTPUT" >/dev/null
echo "Offline bundle written to $OUTPUT"
echo "Copy it to the vessel, then: sudo scripts/prepare-offline-bundle.sh --load $OUTPUT"
echo "and: sudo VESSELSTACK_WHEELHOUSE=$OUTPUT/wheelhouse ./install.sh --offline --start"
