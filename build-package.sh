#!/bin/bash
# Build a self-contained public tarball from COMMITTED, tracked files only.
#
# The package is produced by `git archive` of HEAD, so untracked or ignored
# files (virtualenvs, boat-chat/boat_facts.json, env files, memory/data, logs,
# anything private lying in the working tree) can never leak into it, and the
# tarball corresponds exactly to the embedded commit. Uncommitted edits to
# tracked files are not included (a warning is printed).

set -euo pipefail
umask 022

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
VERSION=$(<"$SCRIPT_DIR/VERSION")
PACKAGE_NAME="vesselstack-$VERSION"
STAGE=$(mktemp -d)
OUTPUT="$SCRIPT_DIR/generated/$PACKAGE_NAME.tar.gz"

cleanup() {
    rm -rf -- "$STAGE"
}
trap cleanup EXIT

sha256_of() {
    if command -v sha256sum >/dev/null; then sha256sum "$@"; else shasum -a 256 "$@"; fi
}

git_in_repo() {
    # Fixed config: ignore system/global/repo attribute and filter surprises.
    git -c core.attributesFile=/dev/null -c safe.directory="$SCRIPT_DIR" -C "$SCRIPT_DIR" "$@"
}

git_in_repo rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
    echo "build-package.sh must run from a git checkout (it packages committed files only)" >&2
    exit 1
}
COMMIT=$(git_in_repo rev-parse --verify HEAD)
COMMIT_TIME=$(git_in_repo show -s --format=%cI HEAD)
if [ -n "$(git_in_repo status --porcelain --untracked-files=no)" ]; then
    if [ "${VESSELSTACK_BUILD_REQUIRE_CLEAN:-false}" = true ]; then
        echo "Tracked files have uncommitted changes and VESSELSTACK_BUILD_REQUIRE_CLEAN=true" >&2
        exit 1
    fi
    echo "WARNING: tracked files have uncommitted changes; the package contains commit $COMMIT, not those edits" >&2
fi

install -d "$SCRIPT_DIR/generated" "$STAGE/$PACKAGE_NAME"
git_in_repo archive --format=tar --prefix="$PACKAGE_NAME/" HEAD \
    | tar --no-same-owner -C "$STAGE" -xf -
# Brand/runtime presentation files are explicit release requirements. Keep this
# gate close to the archive extraction so a renamed or forgotten asset cannot
# silently disappear from an offline package.
for required_asset in \
    wizard/favicon.svg \
    control-panel/static/favicon.svg control-panel/static/index.html control-panel/static/styles.css \
    boat-chat/static/favicon.svg boat-chat/static/index.html boat-chat/static/styles.css \
    brand/icon.svg brand/icon.png brand/wordmark.svg brand/github-banner.svg brand/github-banner.png brand/README.md; do
    [ -s "$STAGE/$PACKAGE_NAME/$required_asset" ] || {
        echo "Refusing to package without required brand asset: $required_asset" >&2
        exit 1
    }
done
# Defence in depth: tracked-only does not excuse a tracked private artifact.
for private in boat-chat/boat_facts.json boat-chat/boat-chat.env vesselstack.env .mcp.json boat-chat/.venv boat-chat/memory boat-chat/data; do
    if [ -e "$STAGE/$PACKAGE_NAME/$private" ]; then
        echo "Refusing to package tracked private artifact: $private" >&2
        exit 1
    fi
done

if grep -RInE --exclude=build-package.sh --exclude=test-distribution.sh \
    '(BOAT_MMSI="[1-9][0-9]+"|BOAT_CALLSIGN="[A-Z0-9]+"|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY)' \
    "$STAGE/$PACKAGE_NAME"; then
    echo "Refusing to package populated identity or credential fields" >&2
    exit 1
fi

printf '%s\n' "$COMMIT" > "$STAGE/$PACKAGE_NAME/COMMIT"
{
    echo "version=$VERSION"
    echo "commit=$COMMIT"
    echo "commit_time=$COMMIT_TIME"
} > "$STAGE/$PACKAGE_NAME/BUILD_INFO"

# bsdtar on macOS would otherwise add AppleDouble "._*" entries, which the
# one-line installer's archive check (rightly) refuses.
export COPYFILE_DISABLE=1
# Reproducible archive: two builds of the same commit give identical bytes. GNU tar can
# sort entries and pin owner and mtime; bsdtar (macOS) cannot, so there the archive is
# valid but byte-for-byte reproducibility is not promised (the CI build on Linux is
# the authoritative one).
COMMIT_EPOCH=$(git_in_repo show -s --format=%ct HEAD)
if tar --version 2>/dev/null | grep -q 'GNU tar'; then
    LC_ALL=C tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$COMMIT_EPOCH" \
        -cf - -C "$STAGE" "$PACKAGE_NAME" | gzip -n > "$OUTPUT"
else
    tar -cf - -C "$STAGE" "$PACKAGE_NAME" | gzip -n > "$OUTPUT"
fi
(
    cd "$SCRIPT_DIR/generated"
    sha256_of "$PACKAGE_NAME.tar.gz" > "$PACKAGE_NAME.tar.gz.sha256"
    cat "$PACKAGE_NAME.tar.gz.sha256"
)

# The one-line bootstrap is published next to the tarball so that
# .../releases/latest/download/get-vesselstack.sh always matches the release it
# comes from: a copy of the committed script with this release's version baked in.
BOOTSTRAP="$SCRIPT_DIR/generated/get-vesselstack.sh"
sed "s/^DEFAULT_VERSION=\".*\"/DEFAULT_VERSION=\"$VERSION\"/" \
    "$STAGE/$PACKAGE_NAME/scripts/get-vesselstack.sh" > "$BOOTSTRAP"
chmod 0755 "$BOOTSTRAP"
grep -qx "DEFAULT_VERSION=\"$VERSION\"" "$BOOTSTRAP" || {
    echo "Could not bake version $VERSION into get-vesselstack.sh" >&2
    exit 1
}
bash -n "$BOOTSTRAP"
[ "$(tail -n 1 "$BOOTSTRAP")" = 'main "$@"' ] || {
    echo "get-vesselstack.sh must end with a single main call" >&2
    exit 1
}
(
    cd "$SCRIPT_DIR/generated"
    sha256_of get-vesselstack.sh > get-vesselstack.sh.sha256
    cat get-vesselstack.sh.sha256
)
echo "commit  $COMMIT"
echo "$OUTPUT"
echo "$BOOTSTRAP"
