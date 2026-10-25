#!/bin/bash
# The release tarball must contain committed files only, and name its commit.

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT
REPO="$TEST_ROOT/repo"
install -d "$REPO"

# Hermetic copy of the current tree (tracked + not-yet-committed, minus ignored)
# committed into a scratch repository, so the test needs no particular history.
( cd "$ROOT" && git ls-files -z --cached --others --exclude-standard | grep -zv '^generated/' | tar --null -T - -cf - ) \
    | tar -C "$REPO" -xf -
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
git -C "$REPO" init -q
git -C "$REPO" add -A
git -C "$REPO" commit -q -m fixture
COMMIT=$(git -C "$REPO" rev-parse HEAD)

# Private and stray files, none tracked: ignored paths and plain untracked files.
install -d "$REPO/boat-chat/.venv/bin" "$REPO/boat-chat/memory" "$REPO/boat-chat/data" "$REPO/.local"
printf 'private facts\n' > "$REPO/boat-chat/boat_facts.json"
printf 'secret\n' > "$REPO/vesselstack.env"
printf 'secret\n' > "$REPO/boat-chat/boat-chat.env"
printf 'x\n' > "$REPO/boat-chat/.venv/bin/python"
printf 'x\n' > "$REPO/boat-chat/memory/chat.sqlite"
printf 'x\n' > "$REPO/boat-chat/data/telemetry.sqlite"
printf 'x\n' > "$REPO/.local/notes"
printf 'untracked\n' > "$REPO/untracked-private.txt"
printf 'untracked\n' > "$REPO/boat-chat/untracked-private.py"
# An uncommitted edit to a tracked file must not be packaged either.
printf '\nUNCOMMITTED-EDIT\n' >> "$REPO/README.md"

"$REPO/build-package.sh" > "$TEST_ROOT/build.out" 2> "$TEST_ROOT/build.err"
VERSION=$(<"$REPO/VERSION")
TARBALL="$REPO/generated/vesselstack-$VERSION.tar.gz"
test -s "$TARBALL"
tar -tzf "$TARBALL" > "$TEST_ROOT/listing"

for leaked in /boat_facts.json /vesselstack.env /boat-chat.env /.venv /memory/ /data/ /.local untracked-private; do
    if grep -qF -- "$leaked" "$TEST_ROOT/listing"; then
        echo "Package contains private or untracked file matching: $leaked" >&2
        exit 1
    fi
done
grep -q "^vesselstack-$VERSION/install.sh$" "$TEST_ROOT/listing"
grep -q "^vesselstack-$VERSION/boat-chat/app.py$" "$TEST_ROOT/listing"
grep -q "^vesselstack-$VERSION/boat-chat/boat_facts.example.json$" "$TEST_ROOT/listing"
tar -xzOf "$TARBALL" "vesselstack-$VERSION/README.md" | grep -q 'UNCOMMITTED-EDIT' && {
    echo "Package contains an uncommitted edit" >&2
    exit 1
}
grep -q 'uncommitted changes' "$TEST_ROOT/build.err"

# The tarball names the commit it was built from, and the build prints sha256 + commit.
test "$(tar -xzOf "$TARBALL" "vesselstack-$VERSION/COMMIT")" = "$COMMIT"
tar -xzOf "$TARBALL" "vesselstack-$VERSION/BUILD_INFO" | grep -qx "commit=$COMMIT"
tar -xzOf "$TARBALL" "vesselstack-$VERSION/BUILD_INFO" | grep -qx "version=$VERSION"
grep -qx "commit  $COMMIT" "$TEST_ROOT/build.out"
expected=$(python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$TARBALL")
grep -q "^$expected" "$TEST_ROOT/build.out"
grep -q "^$expected  vesselstack-$VERSION.tar.gz$" "$TARBALL.sha256"

# The bootstrap is emitted with the release version baked in, plus its sha256.
BOOTSTRAP="$REPO/generated/get-vesselstack.sh"
test -x "$BOOTSTRAP"
grep -qx "DEFAULT_VERSION=\"$VERSION\"" "$BOOTSTRAP"
test "$(tail -n 1 "$BOOTSTRAP")" = 'main "$@"'
bash -n "$BOOTSTRAP"
diff <(sed 's/^DEFAULT_VERSION=".*"//' "$BOOTSTRAP") \
    <(tar -xzOf "$TARBALL" "vesselstack-$VERSION/scripts/get-vesselstack.sh" | sed 's/^DEFAULT_VERSION=".*"//') >/dev/null
( cd "$REPO/generated" && sha256sum -c get-vesselstack.sh.sha256 >/dev/null )
grep -q "^[0-9a-f]\{64\}  get-vesselstack.sh$" "$REPO/generated/get-vesselstack.sh.sha256"
grep -q "get-vesselstack.sh" "$TEST_ROOT/build.out"

# Strict mode refuses a dirty tracked tree.
if VESSELSTACK_BUILD_REQUIRE_CLEAN=true "$REPO/build-package.sh" >/dev/null 2>&1; then
    echo "Strict build accepted a dirty tracked tree" >&2
    exit 1
fi
# A checkout-less copy cannot build (nothing to prove what was packaged).
install -d "$TEST_ROOT/nogit"
cp "$REPO/build-package.sh" "$REPO/VERSION" "$TEST_ROOT/nogit/"
if "$TEST_ROOT/nogit/build-package.sh" >/dev/null 2>&1; then
    echo "Build succeeded outside a git checkout" >&2
    exit 1
fi

echo "build package checks passed"
