#!/bin/bash
# Prepare (on a connected machine) or load (on the vessel) an offline install bundle.
#
#   prepare-offline-bundle.sh [--output DIR] [--platform TAG]... [--python-version X.Y]
#                             [--native] [--image-platform linux/arm64] [--list]
#   prepare-offline-bundle.sh --verify DIR     check MANIFEST.sha256 and the release version
#   prepare-offline-bundle.sh --load DIR       verify, then `docker load` every image
#
# The bundle holds a wheelhouse built from the hash-pinned boat-chat/requirements.lock
# (only wheels whose sha256 the lock lists), a `docker save` tarball for every image
# the Compose template (all profiles) and the Mosquitto helper use, BUNDLE_INFO
# (which records the lock sha256) and MANIFEST.sha256 with a checksum for each file.
# It contains no configuration, credentials or vessel data. --verify and --load
# refuse a bundle whose files differ from the manifest, that holds unlisted files, or
# whose lock hash or wheels do not match this release; as root they also refuse a
# bundle that is not root-owned and free of group/world write access.

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
LOCK="$ROOT/boat-chat/requirements.lock"

usage() { sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

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

sha256_only() { sha256_of "$1" | cut -d' ' -f1; }

require_lock() {
    [ -s "$LOCK" ] || {
        echo "Missing $LOCK: the hash-pinned lock must ship with the release (regenerate it with scripts/update-requirements-lock.sh)" >&2
        return 1
    }
}

# Every component from the path up to / must be root-owned and not group/world
# writable, otherwise an unprivileged account could swap what root installs or loads.
require_root_controlled() {
    local label=$1 path=$2 resolved owner mode
    resolved=$(readlink -f -- "$path") || { echo "Cannot resolve $label: $path" >&2; return 1; }
    path=$resolved
    while :; do
        owner=$(stat -c %u -- "$path")
        mode=$(stat -c %a -- "$path")
        if [ "$owner" != 0 ] || [ $((8#$mode & 8#022)) -ne 0 ]; then
            echo "Refusing to use $label ($resolved) as root: $path must be root-owned and not group/world-writable (uid $owner, mode $mode). Copy the bundle to a root-controlled location, for example: sudo chown -R root:root BUNDLE && sudo chmod -R go-w BUNDLE" >&2
            return 1
        fi
        [ "$path" != / ] || break
        path=$(dirname -- "$path")
    done
}

# Wheels must be wheels and carry a sha256 that the release lock lists.
check_wheels_against_lock() {
    local dir=$1 file digest
    for file in "$dir"/*; do
        [ -e "$file" ] || continue
        case "$file" in
            *.whl) ;;
            *) echo "Refusing non-wheel file in the wheelhouse: ${file##*/}" >&2; return 1 ;;
        esac
        digest=$(sha256_only "$file")
        grep -qE -- "--hash=sha256:$digest( |$)" "$LOCK" || {
            echo "Wheel ${file##*/} is not listed in boat-chat/requirements.lock (sha256 $digest)" >&2
            return 1
        }
    done
}

verify_bundle() {
    BUNDLE=$(readlink -f -- "$BUNDLE") || { echo "Cannot resolve bundle path" >&2; exit 1; }
    [ -f "$BUNDLE/MANIFEST.sha256" ] || { echo "Not an offline bundle (no MANIFEST.sha256): $BUNDLE" >&2; exit 1; }
    require_lock || exit 1
    if [ "$(id -u)" -eq 0 ]; then
        require_root_controlled "offline bundle" "$BUNDLE" || exit 1
        local loose
        loose=$(find "$BUNDLE" \( ! -uid 0 -o -perm /022 \) -print -quit)
        [ -z "$loose" ] || {
            echo "Refusing to use the offline bundle as root: $loose must be root-owned and not group/world-writable" >&2
            exit 1
        }
    fi
    local odd
    odd=$(find "$BUNDLE" ! -type f ! -type d -print -quit)
    [ -z "$odd" ] || { echo "Refusing bundle: $odd is not a regular file or directory" >&2; exit 1; }
    # The manifest must list exactly the files present: no extras, no missing entries.
    local listed present
    listed=$(sed -n 's/^[0-9a-f]\{64\}  \(.*\)$/\1/p' "$BUNDLE/MANIFEST.sha256" | LC_ALL=C sort)
    [ "$(wc -l < "$BUNDLE/MANIFEST.sha256")" -eq "$(printf '%s\n' "$listed" | grep -c .)" ] || {
        echo "Refusing bundle: MANIFEST.sha256 has malformed lines" >&2
        exit 1
    }
    if printf '%s\n' "$listed" | grep -qE '(^|/)\.\.(/|$)|^/'; then
        echo "Refusing bundle: MANIFEST.sha256 lists an unsafe path" >&2
        exit 1
    fi
    present=$(cd "$BUNDLE" && find . -type f ! -name MANIFEST.sha256 | sed 's|^\./||' | LC_ALL=C sort)
    [ "$listed" = "$present" ] || {
        echo "Refusing bundle: files differ from MANIFEST.sha256 (unlisted or missing files):" >&2
        diff <(printf '%s\n' "$listed") <(printf '%s\n' "$present") >&2 || true
        exit 1
    }
    check_manifest "$BUNDLE"
    # Anchor the manifest to this release: the lock it was built from must be ours.
    local bundle_version bundle_lock
    bundle_version=$(sed -n 's/^release=//p' "$BUNDLE/BUNDLE_INFO")
    [ "$bundle_version" = "$VERSION" ] || {
        echo "Bundle was built for release '$bundle_version' but this is release $VERSION; rebuild the bundle" >&2
        exit 1
    }
    bundle_lock=$(sed -n 's/^lock_sha256=//p' "$BUNDLE/BUNDLE_INFO")
    if ! { [ -n "$bundle_lock" ] && [ "$bundle_lock" = "$(sha256_only "$LOCK")" ]; }; then
        echo "Bundle was built from a different requirements.lock than this release's; rebuild the bundle" >&2
        exit 1
    fi
    check_wheels_against_lock "$BUNDLE/wheelhouse"
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

require_lock || exit 1
# Only wheels whose sha256 the release lock lists are fetched; never source distributions.
download_args=(download --disable-pip-version-check --dest "$OUTPUT/wheelhouse"
    -r "$LOCK" --require-hashes --only-binary=:all:)
if [ "$NATIVE" -eq 0 ]; then
    download_args+=(--python-version "$PYTHON_VERSION" --implementation cp)
    for platform in "${PLATFORMS[@]}"; do download_args+=(--platform "$platform"); done
fi
python3 -m pip "${download_args[@]}"
check_wheels_against_lock "$OUTPUT/wheelhouse"

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
    echo "requirements_sha256=$(sha256_only "$ROOT/boat-chat/requirements.txt")"
    echo "lock_sha256=$(sha256_only "$LOCK")"
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
