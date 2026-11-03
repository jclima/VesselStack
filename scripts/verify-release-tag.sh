#!/usr/bin/env bash
# Verify that a release tag is an annotated tag SSH-signed by an allowed release key.
#
# Usage: scripts/verify-release-tag.sh TAG [ALLOWED_SIGNERS_FILE]
#
# ALLOWED_SIGNERS_FILE defaults to .github/allowed_signers at the repository root.
# Run it inside a clone that contains the tag. Exit status 0 means a Good signature from a
# principal listed in the file; anything else exits non-zero with an error on stderr.
#
# The signer must also be the release principal, jc@linuxzone.org unless the environment
# variable VESSELSTACK_RELEASE_PRINCIPAL names another one (used by the tests).
set -euo pipefail

expected_principal=${VESSELSTACK_RELEASE_PRINCIPAL:-jc@linuxzone.org}

die() { echo "verify-release-tag: $*" >&2; exit 1; }

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || die "usage: $0 TAG [ALLOWED_SIGNERS_FILE]"
tag=$1
case "$tag" in
    ''|-*) die "invalid tag name: $tag" ;;
esac

root=$(git rev-parse --show-toplevel 2>/dev/null) || die "not inside a git repository"
signers=${2:-$root/.github/allowed_signers}
[ -f "$signers" ] || die "allowed signers file not found: $signers"
command -v ssh-keygen >/dev/null 2>&1 || die "ssh-keygen is required"

ref="refs/tags/$tag"
git rev-parse --verify --quiet "$ref" >/dev/null || die "tag $tag does not exist"
kind=$(git cat-file -t "$ref")
[ "$kind" = "tag" ] || die "$tag is a $kind reference (a lightweight tag), not an annotated tag object; release tags must be signed annotated tags"

# Explicit program and format so a repository-local signer (for example a 1Password agent
# helper) can never take part in verification.
status=0
output=$(git -c gpg.format=ssh -c "gpg.ssh.allowedSignersFile=$signers" -c gpg.ssh.program=ssh-keygen \
    verify-tag --raw "$ref" 2>&1) || status=$?

if [ "$status" -ne 0 ]; then
    if printf '%s\n' "$output" | grep -qi 'no signature'; then
        die "$tag is unsigned (annotated tag without a signature)"
    fi
    die "signature verification failed for $tag (not signed by an allowed key in $signers): $output"
fi

line=$(printf '%s\n' "$output" | grep -E '^Good "git" signature for ' | tail -n 1 || true)
[ -n "$line" ] || die "no Good signature reported for $tag: $output"

principal=${line#Good \"git\" signature for }
principal=${principal%% with *}
fingerprint=${line##* key }
[ -n "$principal" ] && [ -n "$fingerprint" ] || die "could not parse signer from: $line"

[ "$principal" = "$expected_principal" ] || die "$tag is signed by $principal, but the release principal is $expected_principal"

echo "OK: $tag is signed by $principal (key $fingerprint)"
