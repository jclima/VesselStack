#!/bin/bash
# Build a self-contained public tarball without live settings or vessel data.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SOURCE_ROOT=$SCRIPT_DIR
VERSION=$(<"$SCRIPT_DIR/VERSION")
PACKAGE_NAME="vesselstack-$VERSION"
STAGE=$(mktemp -d)
OUTPUT="$SCRIPT_DIR/generated/$PACKAGE_NAME.tar.gz"

install -d "$SCRIPT_DIR/generated"

cleanup() {
    rm -rf -- "$STAGE"
}
trap cleanup EXIT

install -d "$STAGE/$PACKAGE_NAME" "$STAGE/$PACKAGE_NAME/boat-chat"
tar -C "$SCRIPT_DIR" --exclude=.git --exclude=generated --exclude=boat-chat \
    --exclude=vesselstack.env -cf - . \
    | tar --no-same-owner -C "$STAGE/$PACKAGE_NAME" -xf -
tar -C "$SOURCE_ROOT/boat-chat" \
    --exclude=boat-chat.env --exclude=.venv --exclude=memory --exclude=data \
    --exclude='__pycache__' --exclude='*.pyc' -cf - . \
    | tar --no-same-owner -C "$STAGE/$PACKAGE_NAME/boat-chat" -xf -

if grep -RInE --exclude=build-package.sh \
    '(BOAT_MMSI="[1-9][0-9]+"|BOAT_CALLSIGN="[A-Z0-9]+"|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY)' \
    "$STAGE/$PACKAGE_NAME"; then
    echo "Refusing to package populated identity or credential fields" >&2
    exit 1
fi

tar -czf "$OUTPUT" -C "$STAGE" "$PACKAGE_NAME"
(
    cd "$SCRIPT_DIR/generated"
    sha256sum "$PACKAGE_NAME.tar.gz" > "$PACKAGE_NAME.tar.gz.sha256"
)
echo "$OUTPUT"
