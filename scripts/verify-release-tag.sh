#!/usr/bin/env bash
# Verify that a release tag is an annotated tag SSH-signed by an allowed release key.
#
# Usage: scripts/verify-release-tag.sh TAG [ALLOWED_SIGNERS_FILE [EXPECTED_COMMIT]]
#
# ALLOWED_SIGNERS_FILE defaults to .github/allowed_signers at the repository root; pass ""
# to keep the default while giving EXPECTED_COMMIT.
# EXPECTED_COMMIT, when given, must be a full commit SHA (40 or 64 hex characters, no
# abbreviations or names) and must be the commit the tag points at. The release workflow
# passes the commit it checked out and builds, so the signature approves exactly that commit.
#
# Checks, in order: the ref resolves to one tag object (resolved once; every later check
# inspects that object, never the ref again); it is an annotated tag; its own "tag" header
# equals TAG (so a signed v0.6.1 cannot be pushed as refs/tags/v0.6.0); it points directly at
# a commit (not a tree, blob or another tag); the SSH signature is Good from an allowed key;
# the signer is the release principal; and the commit equals EXPECTED_COMMIT when given.
# Run it inside a clone that contains the tag. Exit status 0 means a Good signature from a
# principal listed in the file; anything else exits non-zero with an error on stderr.
#
# The signer must also be the release principal, jc@linuxzone.org unless the environment
# variable VESSELSTACK_RELEASE_PRINCIPAL names another one (used by the tests).
set -euo pipefail

expected_principal=${VESSELSTACK_RELEASE_PRINCIPAL:-jc@linuxzone.org}

die() { echo "verify-release-tag: $*" >&2; exit 1; }

if [ "$#" -lt 1 ] || [ "$#" -gt 3 ]; then die "usage: $0 TAG [ALLOWED_SIGNERS_FILE [EXPECTED_COMMIT]]"; fi
tag=$1
case "$tag" in
    ''|-*) die "invalid tag name: $tag" ;;
esac

root=$(git rev-parse --show-toplevel 2>/dev/null) || die "not inside a git repository"
signers=${2:-}
if [ -z "$signers" ]; then signers=$root/.github/allowed_signers; fi
expected_commit=${3:-}
if [ "$#" -ge 3 ]; then
    case "$expected_commit" in
        *[!0-9a-fA-F]*|'') die "expected commit must be a full hexadecimal commit SHA: $expected_commit" ;;
    esac
    if [ "${#expected_commit}" -ne 40 ] && [ "${#expected_commit}" -ne 64 ]; then
        die "expected commit must be a full 40 or 64 character SHA, not an abbreviation: $expected_commit"
    fi
    expected_commit=$(printf '%s' "$expected_commit" | tr 'A-F' 'a-f')
    expected_commit=$(git rev-parse --verify --quiet "${expected_commit}^{commit}") \
        || die "expected commit $3 is not a commit in this repository"
fi
[ -f "$signers" ] || die "allowed signers file not found: $signers"
command -v ssh-keygen >/dev/null 2>&1 || die "ssh-keygen is required"

ref="refs/tags/$tag"
# Resolve the ref to its object once. Everything below uses $tagobj, so a ref that moves
# during the run cannot make the signature and the binding checks refer to different objects.
tagobj=$(git rev-parse --verify --quiet "$ref") || die "tag $tag does not exist"
kind=$(git cat-file -t "$tagobj")
[ "$kind" = "tag" ] || die "$tag is a $kind reference (a lightweight tag), not an annotated tag object; release tags must be signed annotated tags"

body=$(git cat-file tag "$tagobj")
# Header lines only (up to the first blank line); the message body is never consulted.
header=$(printf '%s\n' "$body" | sed '/^$/q')
inner_name=$(printf '%s\n' "$header" | sed -n 's/^tag //p' | head -n 1)
[ "$inner_name" = "$tag" ] || die "name mismatch: ref refs/tags/$tag holds a signed tag object named '$inner_name'; the tag object's own name must equal $tag"
target_type=$(printf '%s\n' "$header" | sed -n 's/^type //p' | head -n 1)
[ "$target_type" = "commit" ] || die "$tag points at a $target_type, not directly at a commit; release tags must tag a commit"

# Explicit program and format so a repository-local signer (for example a 1Password agent
# helper) can never take part in verification.
status=0
output=$(git -c gpg.format=ssh -c "gpg.ssh.allowedSignersFile=$signers" -c gpg.ssh.program=ssh-keygen \
    verify-tag --raw "$tagobj" 2>&1) || status=$?

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
if [ -z "$principal" ] || [ -z "$fingerprint" ]; then die "could not parse signer from: $line"; fi

[ "$principal" = "$expected_principal" ] || die "$tag is signed by $principal, but the release principal is $expected_principal"

tag_commit=$(git rev-parse --verify "${tagobj}^{commit}") || die "cannot resolve the commit of $tag"
if [ -n "$expected_commit" ]; then
    [ "$tag_commit" = "$expected_commit" ] || die "$tag points at commit $tag_commit, but the expected commit is $expected_commit"
fi

echo "OK: $tag is signed by $principal (key $fingerprint), commit $tag_commit"
