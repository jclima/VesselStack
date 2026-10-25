#!/bin/bash

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT
install -d "$TEST_ROOT/config"
printf 'VESSELSTACK_ROOT=%q\n' "$TEST_ROOT" > "$TEST_ROOT/config/vesselstack.env"
ENV="$TEST_ROOT/config/vesselstack.env"
VERSION_FILE="$TEST_ROOT/config/installed-version"
CURRENT=$(<"$ROOT/VERSION")

migrate() { "$ROOT/scripts/migrate.sh" "$@" >/dev/null 2>"$TEST_ROOT/stderr"; }

# Fresh installs (no marker) and every older release upgrade to the current one;
# the version marker is the only change when no migration script exists.
rm -f "$VERSION_FILE"
migrate "$ENV" "$CURRENT"
grep -qx "$CURRENT" "$VERSION_FILE"
for from in 0.0.1 0.0.2 0.1.0 0.2.0 0.3.0; do
    printf '%s\n' "$from" > "$VERSION_FILE"
    migrate "$ENV" 0.4.0
    grep -qx '0.4.0' "$VERSION_FILE"
done

# --check changes nothing and reports the verdict.
printf '0.3.0\n' > "$VERSION_FILE"
migrate --check "$ENV" 0.4.0
grep -qx '0.3.0' "$VERSION_FILE"
# Re-running at the target is accepted.
printf '0.4.0\n' > "$VERSION_FILE"
migrate "$ENV" 0.4.0

# Downgrades and future schemas are refused with an explicit message, in both modes.
for flags in "" "--check"; do
    printf '9.0.0\n' > "$VERSION_FILE"
    # shellcheck disable=SC2086
    if "$ROOT/scripts/migrate.sh" $flags "$ENV" 0.4.0 2>"$TEST_ROOT/stderr"; then
        echo "Migration unexpectedly accepted a future schema" >&2
        exit 1
    fi
    grep -q 'Downgrade from 9.0.0 to 0.4.0 is not supported' "$TEST_ROOT/stderr"
    grep -qx '9.0.0' "$VERSION_FILE"
done
printf '0.4.0\n' > "$VERSION_FILE"
if migrate "$ENV" 0.3.0; then echo "Downgrade accepted" >&2; exit 1; fi
grep -q 'not supported' "$TEST_ROOT/stderr"
grep -qx '0.4.0' "$VERSION_FILE"
# Version ordering is numeric, not lexical (0.10.0 is newer than 0.9.0).
printf '0.10.0\n' > "$VERSION_FILE"
if migrate "$ENV" 0.9.0; then echo "0.10.0 -> 0.9.0 accepted" >&2; exit 1; fi
printf '0.9.0\n' > "$VERSION_FILE"
migrate "$ENV" 0.10.0
grep -qx '0.10.0' "$VERSION_FILE"
# Garbage markers are rejected rather than guessed at.
printf 'latest\n' > "$VERSION_FILE"
if migrate "$ENV" 0.4.0; then echo "Unrecognised version accepted" >&2; exit 1; fi

# Renumbering shim: a former 1.x marker is treated as the matching 0.x version
# (1.0.0 -> 0.0.1, 1.0.1 -> 0.0.2, 1.M.P -> 0.M.P) when the target is 0.x.
for pair in 1.0.0:0.0.1 1.0.1:0.0.2 1.1.0:0.1.0 1.2.0:0.2.0 1.3.0:0.3.0 1.4.0:0.4.0 1.4.1:0.4.1; do
    printf '%s\n' "${pair%%:*}" > "$VERSION_FILE"
    migrate --check "$ENV" 0.4.1
    grep -qx "${pair%%:*}" "$VERSION_FILE"
    migrate "$ENV" 0.4.1
    grep -qx '0.4.1' "$VERSION_FILE"
done
# Mapped versions still obey ordering: 1.5.0 would be 0.5.0 (newer than 0.4.1).
printf '1.5.0\n' > "$VERSION_FILE"
if migrate "$ENV" 0.4.1; then echo "1.5.0 -> 0.4.1 accepted" >&2; exit 1; fi
grep -q 'Downgrade' "$TEST_ROOT/stderr"
grep -qx '1.5.0' "$VERSION_FILE"
# The shim never applies to a 1.x target.
printf '1.4.1\n' > "$VERSION_FILE"
if migrate "$ENV" 1.4.0; then echo "1.x downgrade accepted" >&2; exit 1; fi

# A shipped migration script runs once, in order, for versions in (current, target].
FAKE="$TEST_ROOT/fake-scripts"
install -d "$FAKE/migrations"
cp "$ROOT/scripts/migrate.sh" "$FAKE/migrate.sh"
for step in 0.4.0 0.5.0 0.6.0; do
    printf '#!/bin/bash\necho "%s" >> %q\n' "$step" "$TEST_ROOT/ran" > "$FAKE/migrations/$step.sh"
    chmod +x "$FAKE/migrations/$step.sh"
done
printf '0.3.0\n' > "$VERSION_FILE"
"$FAKE/migrate.sh" "$ENV" 0.5.0 >/dev/null
test "$(tr '\n' ' ' < "$TEST_ROOT/ran")" = "0.4.0 0.5.0 "
grep -qx '0.5.0' "$VERSION_FILE"
# A failing step leaves the marker untouched.
printf '#!/bin/bash\nexit 1\n' > "$FAKE/migrations/0.6.0.sh"
if "$FAKE/migrate.sh" "$ENV" 0.6.0 >/dev/null 2>&1; then echo "Failed migration accepted" >&2; exit 1; fi
grep -qx '0.5.0' "$VERSION_FILE"
# A non-executable step is a preflight failure.
chmod -x "$FAKE/migrations/0.6.0.sh"
if "$FAKE/migrate.sh" --check "$ENV" 0.6.0 >/dev/null 2>&1; then echo "Non-executable step accepted" >&2; exit 1; fi

echo "migration checks passed"
