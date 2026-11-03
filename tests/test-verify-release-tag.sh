#!/usr/bin/env bash
# Tests scripts/verify-release-tag.sh with throwaway keys in a temporary repository.
# Never touches the caller's git configuration or signing agent.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SCRIPT="$ROOT/scripts/verify-release-tag.sh"
WORK=$(mktemp -d)
trap 'rm -rf -- "$WORK"' EXIT

# Isolate from the caller's and the repository's git config (which may use a 1Password signer).
export HOME="$WORK/home"
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1
unset GIT_DIR GIT_WORK_TREE SSH_AUTH_SOCK
mkdir -p "$HOME"

ssh-keygen -q -t ed25519 -N '' -C test-allowed -f "$WORK/allowed"
ssh-keygen -q -t ed25519 -N '' -C test-other -f "$WORK/other"

PRINCIPAL=release@example.test
export VESSELSTACK_RELEASE_PRINCIPAL=$PRINCIPAL
pubkey() { cut -d' ' -f1,2 "$1.pub"; }
{
    echo "# comments and blank lines must be accepted"
    echo
    printf '%s namespaces="git" %s\n' "$PRINCIPAL" "$(pubkey "$WORK/allowed")"
} > "$WORK/signers"
printf '%s namespaces="git" %s\n' "other@example.test" "$(pubkey "$WORK/allowed")" > "$WORK/signers-wrong-principal"

REPO="$WORK/repo"
git init -q "$REPO"
cd "$REPO"
G=(git -c user.name=Tester -c "user.email=$PRINCIPAL" -c tag.gpgSign=false -c commit.gpgSign=false)
"${G[@]}" commit -q --allow-empty -m test

sign_args() { echo "-c gpg.format=ssh -c user.signingkey=$1.pub -c gpg.ssh.program=ssh-keygen -c tag.gpgSign=true"; }
# shellcheck disable=SC2046  # sign_args output is intentionally word-split.
"${G[@]}" $(sign_args "$WORK/allowed") tag -s -m "allowed" v1.0.0
"${G[@]}" tag -a -m "unsigned" v1.0.1
"${G[@]}" tag v1.0.2
# shellcheck disable=SC2046
"${G[@]}" $(sign_args "$WORK/other") tag -s -m "other" v1.0.3

failures=0
expect() { # expect pass|fail DESCRIPTION expected-message-fragment -- command...
    local want=$1 desc=$2 frag=$3 out rc=0
    shift 4
    out=$("$@" 2>&1) || rc=$?
    if [ "$want" = pass ] && [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -qF -- "$frag"; then
        echo "ok   - $desc"
    elif [ "$want" = fail ] && [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -qF -- "$frag"; then
        echo "ok   - $desc"
    else
        echo "FAIL - $desc (rc=$rc): $out" >&2
        failures=$((failures + 1))
    fi
}

expect pass "(a) tag signed by an allowed key passes" "signed by $PRINCIPAL" -- "$SCRIPT" v1.0.0 "$WORK/signers"
expect fail "(b) unsigned annotated tag fails" "unsigned" -- "$SCRIPT" v1.0.1 "$WORK/signers"
expect fail "(c) lightweight tag fails" "lightweight" -- "$SCRIPT" v1.0.2 "$WORK/signers"
expect fail "(d) tag signed by a different key fails" "verification failed" -- "$SCRIPT" v1.0.3 "$WORK/signers"
expect fail "(e) wrong principal fails" "release principal is $PRINCIPAL" -- "$SCRIPT" v1.0.0 "$WORK/signers-wrong-principal"
expect fail "missing tag fails" "does not exist" -- "$SCRIPT" v9.9.9 "$WORK/signers"
expect fail "missing signers file fails" "not found" -- "$SCRIPT" v1.0.0 "$WORK/nonexistent"
# Default path: .github/allowed_signers at the repository root.
mkdir -p .github
cp "$WORK/signers" .github/allowed_signers
expect pass "default signers file is .github/allowed_signers" "signed by $PRINCIPAL" -- "$SCRIPT" v1.0.0

if [ "$failures" -ne 0 ]; then
    echo "$failures verify-release-tag test(s) failed" >&2
    exit 1
fi
echo "verify-release-tag tests passed"
