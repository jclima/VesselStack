#!/bin/bash

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TEST_ROOT=$(mktemp -d)
cleanup() { rm -rf -- "$TEST_ROOT"; }
trap cleanup EXIT

for path in LICENSE NOTICE README.md SECURITY.md THIRD_PARTY_NOTICES.md CHANGELOG.md VALIDATION.md CONTRIBUTING.md VERSION \
    install.sh vesselstack.example.env scripts systemd templates boat-chat control-panel build-package.sh; do
    cp -a "$ROOT/$path" "$TEST_ROOT/"
done
mkdir -p "$TEST_ROOT/generated"

archive=$("$TEST_ROOT"/build-package.sh)
name="vesselstack-$(cat "$TEST_ROOT/VERSION")"
checksum="$TEST_ROOT/generated/$name.tar.gz.sha256"
tar -tzf "$archive" > "$TEST_ROOT/entries"
tar -tvzf "$archive" > "$TEST_ROOT/listing"
grep -Eq "^-rwxr-xr-x .* $name/install.sh$" "$TEST_ROOT/listing"
grep -Eq "^-rwxr-xr-x .* $name/scripts/install-signalk-native.sh$" "$TEST_ROOT/listing"
for entry in "$name/install.sh" "$name/templates/compose.yml" \
    "$name/scripts/install-signalk-native.sh" "$name/systemd/vesselstack-signalk.service.in" \
    "$name/boat-chat/app.py" "$name/control-panel/app.py"; do
    grep -qx "$entry" "$TEST_ROOT/entries"
done
if grep -Eq '(^|/)(generated|release|\.git)(/|$)|(__pycache__|\.pyc$)' "$TEST_ROOT/entries"; then exit 1; fi
if grep -Eq '(^|/)(vesselstack\.env|boat-chat/(boat-chat\.env|\.venv|memory|data))(/|$)' "$TEST_ROOT/entries"; then exit 1; fi
(cd "$TEST_ROOT/generated" && sha256sum -c "$(basename "$checksum")")
grep -q "$name.tar.gz" "$checksum"

mkdir -p "$TEST_ROOT/boat-chat/.venv" "$TEST_ROOT/boat-chat/memory" \
    "$TEST_ROOT/boat-chat/data" "$TEST_ROOT/boat-chat/__pycache__" "$TEST_ROOT/.git" \
    "$TEST_ROOT/generated/release"
touch "$TEST_ROOT/vesselstack.env" "$TEST_ROOT/boat-chat/.venv/sentinel" \
    "$TEST_ROOT/boat-chat/memory/sentinel" "$TEST_ROOT/boat-chat/data/sentinel" \
    "$TEST_ROOT/boat-chat/__pycache__/sentinel.pyc" "$TEST_ROOT/.git/config" \
    "$TEST_ROOT/generated/release/sentinel"
touch "$TEST_ROOT/boat-chat/debug.log" "$TEST_ROOT/templates/config.backup" "$TEST_ROOT/root.env"
archive=$("$TEST_ROOT"/build-package.sh)
tar -tzf "$archive" > "$TEST_ROOT/entries"
if grep -Eq '(debug\.log|config\.backup|root\.env|sentinel|__pycache__|\.git)' "$TEST_ROOT/entries"; then exit 1; fi
archive_hash=$(sha256sum "$TEST_ROOT/generated/$name.tar.gz" | awk '{print $1}')
checksum_hash=$(sha256sum "$checksum" | awk '{print $1}')
expect_failure() {
    if "$TEST_ROOT/build-package.sh" >/dev/null 2>&1; then echo "expected failure" >&2; exit 1; fi
    test "$archive_hash" = "$(sha256sum "$TEST_ROOT/generated/$name.tar.gz" | awk '{print $1}')"
    test "$checksum_hash" = "$(sha256sum "$checksum" | awk '{print $1}')"
}

extract_root="$TEST_ROOT/extracted"
mkdir -p "$extract_root"
tar -xzf "$archive" -C "$extract_root"
(cd "$extract_root/$name" && ./build-package.sh >/dev/null)
(cd "$extract_root/$name/generated" && sha256sum -c "$name.tar.gz.sha256")

rm -f "$TEST_ROOT/scripts/install-signalk-native.sh"
ln -s /tmp/not-a-release-file "$TEST_ROOT/scripts/install-signalk-native.sh"
expect_failure
rm -f "$TEST_ROOT/scripts/install-signalk-native.sh"
cp -a "$ROOT/scripts/install-signalk-native.sh" "$TEST_ROOT/scripts/install-signalk-native.sh"
chmod -x "$TEST_ROOT/scripts/install-signalk-native.sh"
expect_failure
chmod +x "$TEST_ROOT/scripts/install-signalk-native.sh"
mv "$TEST_ROOT/templates" "$TEST_ROOT/templates.real"
ln -s templates.real "$TEST_ROOT/templates"
expect_failure
rm "$TEST_ROOT/templates"
mv "$TEST_ROOT/templates.real" "$TEST_ROOT/templates"

for missing in templates/compose.yml vesselstack.example.env scripts/install-signalk-native.sh systemd/vesselstack-signalk.service.in; do
    rm -f "$TEST_ROOT/$missing"; expect_failure; cp -a "$ROOT/$missing" "$TEST_ROOT/$missing"
done
for service in signalk ais-catcher; do
    sed -i.bak "/^  $service:$/d" "$TEST_ROOT/templates/compose.yml"; rm -f "$TEST_ROOT/templates/compose.yml.bak"
    expect_failure; cp -a "$ROOT/templates/compose.yml" "$TEST_ROOT/templates/compose.yml"
done
for pin in SIGNALK_VERSION AIS_IMAGE; do
    if [ "$pin" = SIGNALK_VERSION ]; then
        sed -i.bak "s/^$pin=.*/$pin=\"v2.27\"/" "$TEST_ROOT/vesselstack.example.env"; rm -f "$TEST_ROOT/vesselstack.example.env.bak"
        expect_failure; cp -a "$ROOT/vesselstack.example.env" "$TEST_ROOT/vesselstack.example.env"
    fi
    sed -i.bak "s/^$pin=.*/$pin=\"\"/" "$TEST_ROOT/vesselstack.example.env"; rm -f "$TEST_ROOT/vesselstack.example.env.bak"
    expect_failure; cp -a "$ROOT/vesselstack.example.env" "$TEST_ROOT/vesselstack.example.env"
    sed -i.bak "s/^$pin=.*/$pin=\"latest\"/" "$TEST_ROOT/vesselstack.example.env"; rm -f "$TEST_ROOT/vesselstack.example.env.bak"
    expect_failure; cp -a "$ROOT/vesselstack.example.env" "$TEST_ROOT/vesselstack.example.env"
    sed -i.bak "s/^$pin=.*/$pin=\"not-a-pin\"/" "$TEST_ROOT/vesselstack.example.env"; rm -f "$TEST_ROOT/vesselstack.example.env.bak"
    expect_failure; cp -a "$ROOT/vesselstack.example.env" "$TEST_ROOT/vesselstack.example.env"
done
echo "build-package checks passed"
