#!/bin/bash

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT
install -d "$TEST_ROOT/config"
printf 'VESSELSTACK_ROOT=%q\n' "$TEST_ROOT" > "$TEST_ROOT/config/vesselstack.env"

"$ROOT/scripts/migrate.sh" "$TEST_ROOT/config/vesselstack.env" 1.2.0 >/dev/null
grep -qx '1.2.0' "$TEST_ROOT/config/installed-version"

printf '1.0.1\n' > "$TEST_ROOT/config/installed-version"
"$ROOT/scripts/migrate.sh" "$TEST_ROOT/config/vesselstack.env" 1.2.0 >/dev/null
grep -qx '1.2.0' "$TEST_ROOT/config/installed-version"

printf '9.0.0\n' > "$TEST_ROOT/config/installed-version"
if "$ROOT/scripts/migrate.sh" "$TEST_ROOT/config/vesselstack.env" 1.2.0 2>/dev/null; then
    echo "Migration unexpectedly accepted a future schema" >&2
    exit 1
fi

echo "migration checks passed"
