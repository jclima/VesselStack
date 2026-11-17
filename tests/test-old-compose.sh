#!/bin/bash
# Validates the Compose template with real OLD Docker Compose releases taken from Docker's official CLI
# images, pinned by their multi-arch index digest: the oldest version this release is checked with
# (2.10.2, docker 20.10.17) and the one Debian packages (2.26.1, docker 26.0). It guards against a
# Compose option or template feature newer than that: `config` needs no Docker daemon.
# Old Compose has no `config --no-env-resolution`, so a root holding an empty config/nodered.env is
# supplied for the Node-RED profile, exactly as install.sh does.
# Keep COMPOSE_OLDEST_CHECKED in install.sh equal to the first entry below.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
IMAGES=(
    "2.10.2 docker:20.10.17-cli@sha256:6c5c8b70d0de524ff092921c05ebe9c1b0d05c29962d6717666b29049e52aefe"
    "2.26.1 docker:26.0-cli@sha256:9b65b01f62284b489fffb15421ff547185290a66ef6ae6456bd7d97fe1f06b3c"
)
STUB=$(mktemp -d)
EMPTY=$(mktemp -d)
trap 'rm -rf -- "$STUB" "$EMPTY"' EXIT
install -d "$STUB/config"
: > "$STUB/config/nodered.env"
grep -qx "COMPOSE_OLDEST_CHECKED=${IMAGES[0]%% *}" "$ROOT/install.sh" \
    || { echo "COMPOSE_OLDEST_CHECKED in install.sh does not match the oldest image in this test" >&2; exit 1; }

IMAGE=; EXTRA_ENV=(); STUB_DIR=$STUB
old_compose() {
    docker run --rm -v "$ROOT":/src:ro -v "$STUB_DIR":/stub:ro -w /src -e VESSELSTACK_ROOT=/stub ${EXTRA_ENV[@]+"${EXTRA_ENV[@]}"} "$IMAGE" docker compose "$@"
}

env_args=(--env-file vesselstack.example.env -f templates/compose.yml)
all_profiles=(--profile signalk --profile ais --profile metrics --profile nodered)
for entry in "${IMAGES[@]}"; do
    want=${entry%% *}; IMAGE=${entry#* }; EXTRA_ENV=(); STUB_DIR=$STUB
    version=$(old_compose version --short)
    [ "${version#v}" = "$want" ] || { echo "expected Docker Compose $want in $IMAGE, got: $version" >&2; exit 1; }
    echo "Old Docker Compose $version"
    old_compose "${env_args[@]}" config --quiet
    old_compose "${all_profiles[@]}" "${env_args[@]}" config --quiet
    default_images=$(old_compose "${env_args[@]}" config --images | sort -u)
    all_images=$(old_compose "${all_profiles[@]}" "${env_args[@]}" config --images | sort -u)
    [ -n "$default_images" ] && [ -n "$all_images" ] || { echo "Compose listed no images" >&2; exit 1; }
    # Every profile adds images; none may disappear.
    [ "$(comm -23 <(printf '%s\n' "$default_images") <(printf '%s\n' "$all_images"))" = "" ] \
        || { echo "the default image list is not part of the all-profiles list" >&2; exit 1; }
    # The Node-RED env file is required: without it the profile does not validate.
    STUB_DIR=$EMPTY
    if old_compose --profile nodered "${env_args[@]}" config --quiet >/dev/null 2>&1; then
        echo "the nodered profile validated without config/nodered.env" >&2; exit 1
    fi
    STUB_DIR=$STUB
    # A component version override reaches the image list.
    EXTRA_ENV=(-e GRAFANA_VERSION=13.0.2)
    old_compose "${env_args[@]}" config --images | grep -qx 'grafana/grafana:13.0.2' \
        || { echo "GRAFANA_VERSION did not reach the image list" >&2; exit 1; }
done
echo "old Compose template checks passed"
