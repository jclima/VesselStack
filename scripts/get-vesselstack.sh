#!/bin/bash
# VesselStack guided installer (bootstrap).
#
#   curl -fsSL https://github.com/jclima/VesselStack/releases/latest/download/get-vesselstack.sh | sudo bash
#   curl -fsSL https://github.com/jclima/VesselStack/releases/download/v0.5.2/get-vesselstack.sh | sudo bash
#
# Cautious alternative: download it, read it, then run it.
#   curl -fsSLO https://github.com/jclima/VesselStack/releases/latest/download/get-vesselstack.sh
#   sudo bash get-vesselstack.sh
#
# What it does: checks the computer, installs the Debian packages VesselStack needs
# (and asks before installing Docker Engine from Docker's official apt repository),
# downloads the release tarball and its
# .sha256 from the GitHub release, verifies the checksum, unpacks it root-owned,
# asks its configuration questions in the terminal (works over plain SSH, no browser), and
# then runs install.sh (--dry-run first). Re-running is safe. Options: see --help.
# Without a terminal (or to script it): --yes, --defaults, --set KEY=VALUE, --config FILE.
#
# The whole script is wrapped in functions and ends with a single `main "$@"`
# line, so a truncated download cannot half-execute.

# shellcheck disable=SC1090,SC1091  # os-release is read from a path (overridable for tests).
set -euo pipefail
umask 022

# Baked from VERSION by build-package.sh (and checked by tests/test-distribution.sh).
DEFAULT_VERSION="0.5.2"
GITHUB_RELEASES="https://github.com/jclima/VesselStack/releases/download"
APT_PACKAGES=(ca-certificates curl jq openssl python3 python3-venv rsync tar gzip)

# Docker Engine comes from Docker's own apt repository (https://docs.docker.com/engine/install/debian/).
# Fingerprint of Docker's package signing key; the downloaded key must match it exactly.
DOCKER_KEY_FPR="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
DOCKER_REPO_BASE="https://download.docker.com/linux"
DOCKER_PACKAGES=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin)
# Unofficial packages that conflict with Docker's own (docs.docker.com, "Uninstall old versions"),
# plus Debian's own docker-cli/docker-compose-v2, which ship the same files.
DOCKER_CONFLICTS=(docker.io docker-cli docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc)
# Conflicts that provide or replace the engine itself (they may be running containers).
DOCKER_ENGINE_CONFLICTS=(docker.io docker-cli podman-docker containerd runc)
# Locations are overridable only so the tests never touch the real system.
OS_RELEASE_FILE=${VESSELSTACK_OS_RELEASE:-/etc/os-release}
APT_KEYRING_DIR=${VESSELSTACK_APT_KEYRING_DIR:-/etc/apt/keyrings}
APT_SOURCES_DIR=${VESSELSTACK_APT_SOURCES_DIR:-/etc/apt/sources.list.d}
APT_MAIN_SOURCES=${VESSELSTACK_APT_SOURCES_FILE:-/etc/apt/sources.list}
DOCKER_DATA_DIR=${VESSELSTACK_DOCKER_DATA_DIR:-/var/lib/docker}

# Global state (set by parse_args / the steps).
ASSUME_YES=0
DEFAULTS_ONLY=0
WEB_WIZARD=0
SETS=()
VERSION=${VESSELSTACK_VERSION:-}
RELEASE_BASE=/opt/vesselstack-release
NO_WIZARD=0
CONFIG_ARG=
OFFLINE=0
BUNDLE=
TARBALL_ARG=
PIN_SHA256=
BASE_URL=
WIZARD_PORT=
WIZARD_DIR=
WIZARD_ACCOUNT=vesselstack-wizard
WIZARD_TMPUSER=
CONFIG_MAX_BYTES=65536
INSTALL_ROOT=${VESSELSTACK_ROOT:-/opt/vesselstack}
PROMPT_SOURCE=${VESSELSTACK_BOOTSTRAP_ANSWERS:-/dev/tty}
DOCTOR_WAIT=${VESSELSTACK_BOOTSTRAP_DOCTOR_WAIT:-30}
case "$DOCTOR_WAIT" in
    "" | *[!0-9]*) echo "VESSELSTACK_BOOTSTRAP_DOCTOR_WAIT must be a whole number of seconds" >&2; exit 2 ;;
esac
PROMPT_OPEN=0
STAGE=
RELEASE_DIR=
ENV_FILE=
UPGRADE=0
PKG_MODE=apt
APT_UPDATED=0
DOCKER_KEY_DIR=
DOCKER_DISTRO=
DOCKER_CODENAME=
DOCKER_ARCH=
DOCKER_INSTALLED_NOW=0
HEALTHY=1
INSTALLED_VERSION=
CHANGES=()

# ---------------------------------------------------------------- output

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_BOLD=$'\033[1m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_OFF=$'\033[0m'
else
    C_BOLD=; C_GREEN=; C_YELLOW=; C_RED=; C_OFF=
fi
say()     { printf '%s\n' "$*"; }
info()    { printf '    %s\n' "$*"; }
ok()      { printf '  %s[ok]%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn()    { printf '  %s[!]%s  %s\n' "$C_YELLOW" "$C_OFF" "$*" >&2; }
changed() { CHANGES+=("$*"); printf '  %s[changed]%s %s\n' "$C_BOLD" "$C_OFF" "$*"; }
step()    { printf '\n%s== Step %s of 6: %s ==%s\n' "$C_BOLD" "$1" "$2" "$C_OFF"; }
die() {
    printf '\n%sERROR:%s %s\n' "$C_RED" "$C_OFF" "$1" >&2
    shift
    for line in "$@"; do printf '       %s\n' "$line" >&2; done
    exit 1
}

usage() {
    cat <<EOF
VesselStack guided installer

Usage: sudo bash get-vesselstack.sh [options]
   or: curl -fsSL URL/get-vesselstack.sh | sudo bash
   or: curl -fsSL URL/get-vesselstack.sh | sudo bash -s -- [options]

Options:
  --version X.Y.Z     Install this release (default: $DEFAULT_VERSION; env VESSELSTACK_VERSION)
  --yes, -y           Ask nothing; answer yes to every question and take the settings from
                      --config / --set / the defaults (for scripts and headless installs).
                      Existing installs are upgraded only with this flag or an explicit yes.
  --defaults          Ask nothing; every question gets its own default answer (an existing
                      install is NOT upgraded and nothing is removed from Docker) and the
                      settings are the defaults plus any --set.
  --set KEY=VALUE     Set one setting without prompts (repeatable), checked with the wizard's
                      rules; on top of --config, a saved configuration or the defaults.
                      Avoid secrets here (they show in the process list): leave them to be
                      generated. Example: --set BOAT_NAME=Foo --set SOCKETCAN_ENABLE=true
  --config FILE       Existing vesselstack.env to use (copied to a root-only location)
  --no-wizard         Skip the wizard; use --config FILE
  --cli, --terminal   Ask the configuration questions in this terminal (the default)
  --web-wizard        Use the optional browser wizard instead (loopback only; needs a browser
                      on the Pi or an ssh -L tunnel)
  --offline           No internet: needs --bundle DIR and a local --tarball FILE
  --bundle DIR        Offline bundle made by scripts/prepare-offline-bundle.sh
  --tarball FILE      Use a local vesselstack-X.Y.Z.tar.gz (its .sha256 must sit next to it)
  --sha256 HASH       Also require the tarball to match this sha256 (from a source you trust)
  --dir DIR           Where releases are unpacked (default: $RELEASE_BASE)
  --wizard-port N     Port for the browser wizard (only with --web-wizard; default: a random port)
  --base-url URL      Release download base, https only (default: $GITHUB_RELEASES)
  --help, -h          Show this help

Nothing is installed or changed before you have seen what will happen and, unless
you pass --yes, confirmed. Re-running is safe: finished steps are reported and skipped.
Run it on the Raspberry Pi itself (or over SSH, use ssh -t so questions can be answered),
as root (sudo). Without any terminal use --yes, --defaults, --set or --config.
EOF
}

# ---------------------------------------------------------------- prompts

# Reads answers from the terminal even when the script itself comes from a pipe
# (curl | sudo bash), because stdin is then the script text.
open_prompt() {
    [ "$PROMPT_OPEN" -eq 1 ] && return 0
    if { exec 3<"$PROMPT_SOURCE"; } 2>/dev/null; then
        PROMPT_OPEN=1
        return 0
    fi
    return 1
}

# ask_yn "Question?" default(y|n): returns 0 for yes.
ask_yn() {
    local question=$1 default=${2:-n} reply hint
    if [ "$ASSUME_YES" -eq 1 ]; then
        say "  $question -> yes (--yes)"
        return 0
    fi
    if [ "$DEFAULTS_ONLY" -eq 1 ]; then
        if [ "$default" = y ]; then say "  $question -> yes (default)"; return 0; fi
        say "  $question -> no (default)"
        return 1
    fi
    open_prompt || die "A question needs an answer but there is no terminal to ask on." \
        "Run again from a terminal (over SSH use ssh -t), or add --yes or --defaults (see --help)."
    hint="[y/N]"
    [ "$default" = y ] && hint="[Y/n]"
    while :; do
        printf '  %s %s ' "$question" "$hint" >&2
        if ! read -r reply <&3; then
            die "No answer received (input ended)."
        fi
        case "${reply:-$default}" in
            y|Y|yes|YES|Yes) return 0 ;;
            n|N|no|NO|No) return 1 ;;
            *) say "  Please answer y or n." >&2 ;;
        esac
    done
}

# ask_choice "Question" default a|b ...: prints the chosen letter.
ask_choice() {
    local question=$1 default=$2 reply
    shift 2
    if [ "$ASSUME_YES" -eq 1 ] || [ "$DEFAULTS_ONLY" -eq 1 ]; then printf '%s\n' "$default"; return 0; fi
    open_prompt || die "A question needs an answer but there is no terminal to ask on." \
        "Run again from a terminal (over SSH use ssh -t), or add --yes or --defaults."
    while :; do
        printf '  %s [%s] (default %s) ' "$question" "$*" "$default" >&2
        read -r reply <&3 || die "No answer received (input ended)."
        reply=${reply:-$default}
        for option in "$@"; do
            if [ "$reply" = "$option" ]; then printf '%s\n' "$reply"; return 0; fi
        done
        say "  Please choose one of: $*" >&2
    done
}

# ---------------------------------------------------------------- helpers

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --yes|-y) ASSUME_YES=1; shift ;;
            --defaults) DEFAULTS_ONLY=1; shift ;;
            --cli|--terminal) WEB_WIZARD=0; shift ;;
            --web-wizard) WEB_WIZARD=1; shift ;;
            --set)
                [ $# -ge 2 ] || die "--set needs KEY=VALUE"
                # Only the shape is checked here (so a typo fails before anything is installed);
                # the wizard checks the key and the value with its full rules. The value is
                # never echoed: it may be a secret.
                local set_re='^[A-Z][A-Z0-9_]*=[A-Za-z0-9 _.,:/@+()-]*$'
                [[ "$2" =~ $set_re ]] || die "--set needs KEY=VALUE with an upper-case setting name (e.g. BOAT_NAME=Foo)." \
                    "Values may contain only letters, digits, spaces and . , : / @ + ( ) - _   (setting: ${2%%=*})"
                SETS+=("$2"); shift 2 ;;
            --version) [ $# -ge 2 ] || die "--version needs a value"; VERSION=$2; shift 2 ;;
            --no-wizard) NO_WIZARD=1; shift ;;
            --config) [ $# -ge 2 ] || die "--config needs a file"; CONFIG_ARG=$2; shift 2 ;;
            --offline) OFFLINE=1; shift ;;
            --bundle) [ $# -ge 2 ] || die "--bundle needs a directory"; BUNDLE=$2; shift 2 ;;
            --tarball) [ $# -ge 2 ] || die "--tarball needs a file"; TARBALL_ARG=$2; shift 2 ;;
            --sha256) [ $# -ge 2 ] || die "--sha256 needs a value"; PIN_SHA256=$2; shift 2 ;;
            --dir) [ $# -ge 2 ] || die "--dir needs a directory"; RELEASE_BASE=$2; shift 2 ;;
            --wizard-port) [ $# -ge 2 ] || die "--wizard-port needs a number"; WIZARD_PORT=$2; shift 2 ;;
            --base-url) [ $# -ge 2 ] || die "--base-url needs a URL"; BASE_URL=$2; shift 2 ;;
            -h|--help) usage; exit 0 ;;
            *) usage >&2; die "Unknown option: $1" ;;
        esac
    done
    if [ -z "$VERSION" ] && [ -n "$TARBALL_ARG" ]; then
        # A local tarball named vesselstack-X.Y.Z.tar.gz tells us its version.
        local name=${TARBALL_ARG##*/}
        name=${name#vesselstack-}
        name=${name%.tar.gz}
        if [[ "$name" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then VERSION=$name; fi
    fi
    VERSION=${VERSION:-$DEFAULT_VERSION}
    VERSION=${VERSION#v}
    [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Version must look like 0.4.0 (got: $VERSION)"
    if [ -n "$WIZARD_PORT" ] && { ! [[ "$WIZARD_PORT" =~ ^[0-9]{4,5}$ ]] || [ "$WIZARD_PORT" -lt 1024 ] || [ "$WIZARD_PORT" -gt 65535 ]; }; then
        die "--wizard-port must be a number from 1024 to 65535"
    fi
    if [ -n "$PIN_SHA256" ] && ! [[ "$PIN_SHA256" =~ ^[0-9a-fA-F]{64}$ ]]; then die "--sha256 must be 64 hex characters"; fi
    BASE_URL=${BASE_URL:-$GITHUB_RELEASES/v$VERSION}
    BASE_URL=${BASE_URL%/}
    case "$BASE_URL" in https://*) ;; *) die "--base-url must start with https://" ;; esac
    if [ "$OFFLINE" -eq 1 ]; then
        [ -n "$BUNDLE" ] || die "--offline needs --bundle DIR (made by scripts/prepare-offline-bundle.sh)"
        if [ -z "$TARBALL_ARG" ]; then TARBALL_ARG=vesselstack-$VERSION.tar.gz; fi
    fi
    if [ -n "$BUNDLE" ] && [ "$OFFLINE" -ne 1 ]; then die "--bundle only makes sense together with --offline"; fi
    if [ "$NO_WIZARD" -eq 1 ] && [ -z "$CONFIG_ARG" ]; then die "--no-wizard needs --config FILE"; fi
    if [ -n "$CONFIG_ARG" ]; then NO_WIZARD=1; fi
    if [ -n "$WIZARD_PORT" ] && [ "$WEB_WIZARD" -ne 1 ]; then die "--wizard-port only applies to the browser wizard: add --web-wizard."; fi
    if [ "$WEB_WIZARD" -eq 1 ]; then
        if [ "$NO_WIZARD" -eq 1 ] || [ "${#SETS[@]}" -gt 0 ] || [ "$DEFAULTS_ONLY" -eq 1 ]; then
            die "--web-wizard cannot be combined with --config, --no-wizard, --set or --defaults." \
                "Those options set the configuration without questions; the browser wizard is for answering them."
        fi
    fi
    case "$RELEASE_BASE" in /*) ;; *) die "--dir must be an absolute path" ;; esac
}

sha256_file() { sha256sum "$1" | awk '{print $1}'; }

# Walk from a path up to / and fail unless every existing component is root-owned
# and not group/world-writable (so an unprivileged user cannot swap files we trust).
require_root_controlled() {
    local path=$1 resolved uid mode
    resolved=$(realpath -m -- "$path")
    while :; do
        if [ -e "$resolved" ]; then
            read -r uid mode < <(stat -c '%u %a' -- "$resolved")
            if [ "$uid" != 0 ] || [ $(( 0$mode & 0022 )) -ne 0 ]; then
                return 1
            fi
        fi
        [ "$resolved" = / ] && return 0
        resolved=$(dirname -- "$resolved")
    done
}

nearest_existing() {
    local path=$1
    while [ ! -e "$path" ] && [ "$path" != / ]; do path=$(dirname -- "$path"); done
    printf '%s\n' "$path"
}

pkg_installed() { [ "$(dpkg-query -W -f='${db:Status-Status}' "$1" 2>/dev/null || true)" = installed ]; }

# Lists are refreshed once, before anything asks apt what is available.
apt_update_once() {
    [ "$APT_UPDATED" -eq 0 ] || return 0
    DEBIAN_FRONTEND=noninteractive apt-get update </dev/null || die "apt-get update failed. Check the network and /etc/apt/sources.list."
    APT_UPDATED=1
}

# Prints the candidate version apt would install, or nothing.
apt_candidate() {
    apt-cache policy "$1" 2>/dev/null | awk '/^ *Candidate:/ && $2 != "(none)" {print $2; exit}'
}

compose_ok() { command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; }

# ---------------------------------------------------------------- Docker Engine

# Decides which of Docker's repositories applies. Sets DOCKER_DISTRO (debian|ubuntu),
# DOCKER_CODENAME and DOCKER_ARCH, or stops with a plain explanation. The architecture is
# the package architecture (dpkg), not the kernel's: 32-bit Raspberry Pi OS often runs on
# a 64-bit kernel.
docker_platform() {
    local id='' id_like='' codename='' ubuntu_codename=''
    if [ -r "$OS_RELEASE_FILE" ]; then
        id=$(. "$OS_RELEASE_FILE" && printf '%s' "${ID:-}")
        id_like=$(. "$OS_RELEASE_FILE" && printf '%s' "${ID_LIKE:-}")
        codename=$(. "$OS_RELEASE_FILE" && printf '%s' "${VERSION_CODENAME:-}")
        ubuntu_codename=$(. "$OS_RELEASE_FILE" && printf '%s' "${UBUNTU_CODENAME:-}")
    fi
    DOCKER_ARCH=$(dpkg --print-architecture 2>/dev/null || true)
    if [ "$DOCKER_ARCH" = armhf ] || [ "$id" = raspbian ]; then
        die "This is 32-bit Raspberry Pi OS (package architecture '${DOCKER_ARCH:-armhf}'). VesselStack needs the 64-bit version." \
            "Docker Engine v28 was the last major release for 32-bit Raspberry Pi OS (armhf); newer releases are 64-bit only." \
            "(uname -m can say aarch64 on a 32-bit system; 'dpkg --print-architecture' is what counts.)" \
            "Re-flash the card or SSD with 'Raspberry Pi OS (64-bit)' using Raspberry Pi Imager (Pi 3, 4, 5 and Zero 2 W can run it)," \
            "restore your files and run this installer again. Nothing was changed."
    fi
    case "$DOCKER_ARCH" in
        arm64|amd64) ;;
        *) die "Docker's apt repository supports 64-bit ARM (arm64) and x86-64 (amd64); this system reports '${DOCKER_ARCH:-unknown}'." \
               "Install Docker Engine and the Compose plugin yourself (https://docs.docker.com/engine/install/), then re-run. Nothing was changed." ;;
    esac
    case "$id" in
        debian) DOCKER_DISTRO=debian; DOCKER_CODENAME=$codename ;;
        ubuntu) DOCKER_DISTRO=ubuntu; DOCKER_CODENAME=${ubuntu_codename:-$codename} ;;
        *)
            case " $id_like " in
                *" ubuntu "*) DOCKER_DISTRO=ubuntu; DOCKER_CODENAME=$ubuntu_codename ;;
                *) die "Automatic Docker installation supports Debian, Raspberry Pi OS (64-bit) and Ubuntu; this system is '${id:-unknown}'." \
                       "Install Docker Engine and the Compose plugin for your system (https://docs.docker.com/engine/install/), then re-run. Nothing was changed." ;;
            esac ;;
    esac
    [ -n "$DOCKER_CODENAME" ] || die "Could not tell which $DOCKER_DISTRO release this is (no VERSION_CODENAME in $OS_RELEASE_FILE)." \
        "Install Docker Engine from https://docs.docker.com/engine/install/$DOCKER_DISTRO/ and re-run. Nothing was changed."
}

# Prints what a Docker daemon here already holds ("3 containers and 1 volume"), or nothing.
docker_data_summary() {
    local containers=0 volumes=0
    if command -v docker >/dev/null 2>&1 && timeout 20 docker info >/dev/null 2>&1; then
        containers=$({ timeout 20 docker ps -aq 2>/dev/null || true; } | wc -l | tr -d ' ')
        volumes=$({ timeout 20 docker volume ls -q 2>/dev/null || true; } | wc -l | tr -d ' ')
    else
        # The daemon is not running: look at what is on disk.
        if [ -d "$DOCKER_DATA_DIR/containers" ]; then
            containers=$(find "$DOCKER_DATA_DIR/containers" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')
        fi
        if [ -d "$DOCKER_DATA_DIR/volumes" ]; then
            volumes=$(find "$DOCKER_DATA_DIR/volumes" -mindepth 1 -maxdepth 1 ! -name metadata.db | wc -l | tr -d ' ')
        fi
    fi
    if [ "$containers" -gt 0 ] || [ "$volumes" -gt 0 ]; then
        printf '%s container(s) and %s volume(s)' "$containers" "$volumes"
    fi
    return 0
}

# Lists apt source files other than ours that already use download.docker.com.
docker_foreign_sources() {
    local file
    for file in "$APT_SOURCES_DIR"/*.list "$APT_SOURCES_DIR"/*.sources; do
        [ -f "$file" ] || continue
        [ "$file" != "$APT_SOURCES_DIR/docker.sources" ] || continue
        if grep -qs 'download\.docker\.com' "$file"; then printf '%s\n' "$file"; fi
    done
    return 0
}

# Downloads Docker's signing key into a private directory and checks it before anything
# on the system is touched. The file must hold exactly ONE key and it must be the pinned one
# (apt trusts every key in a Signed-By file).
docker_fetch_key() {
    local url=$DOCKER_REPO_BASE/$DOCKER_DISTRO/gpg gpg_out pubs primary
    require_root_controlled "$APT_KEYRING_DIR" || die "$APT_KEYRING_DIR (or a parent directory) is not root-owned, or is writable by others." \
        "Fix its ownership and permissions, then re-run. Nothing was changed."
    install -d -m 0755 -o root -g root "$APT_KEYRING_DIR"
    DOCKER_KEY_DIR=$APT_KEYRING_DIR/.docker-key-verify
    rm -rf -- "$DOCKER_KEY_DIR"
    install -d -m 0700 -o root -g root "$DOCKER_KEY_DIR" "$DOCKER_KEY_DIR/gnupg"
    curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 60 \
        -o "$DOCKER_KEY_DIR/docker.asc" "$url" </dev/null \
        || die "Could not download Docker's package signing key." \
               "Failing command: curl --proto '=https' --tlsv1.2 -fsSL $url" \
               "Check the internet connection and the clock, then re-run. Nothing was changed."
    gpg_out=$(GNUPGHOME=$DOCKER_KEY_DIR/gnupg gpg --batch --no-tty --show-keys --with-colons -- "$DOCKER_KEY_DIR/docker.asc" 2>/dev/null) \
        || die "gpg could not read the downloaded key file." \
               "Failing command: gpg --show-keys --with-colons $DOCKER_KEY_DIR/docker.asc" \
               "Nothing was changed."
    pubs=$(printf '%s\n' "$gpg_out" | awk -F: '$1 == "pub" {n++} END {print n + 0}')
    primary=$(printf '%s\n' "$gpg_out" | awk -F: '$1 == "pub" {p = 1; next} p && $1 == "fpr" {print $10; exit}')
    if [ "$pubs" != 1 ] || [ "$primary" != "$DOCKER_KEY_FPR" ]; then
        die "Refusing to trust the downloaded Docker key: it does not match Docker's published fingerprint." \
            "  expected: $DOCKER_KEY_FPR" \
            "  found:    ${primary:-none} ($pubs key(s) in the file)" \
            "The download may have been tampered with, or Docker rotated its key. Compare with" \
            "https://docs.docker.com/engine/install/$DOCKER_DISTRO/ before trying again. Nothing was changed."
    fi
    ok "Docker's signing key verified (fingerprint $DOCKER_KEY_FPR)"
}

# Installs the verified key, writes /etc/apt/sources.list.d/docker.sources (deb822, as in
# Docker's instructions) and refreshes the package lists.
docker_setup_repo() {
    local keyfile=$APT_KEYRING_DIR/docker.asc srcfile=$APT_SOURCES_DIR/docker.sources foreign f
    if ! command -v gpg >/dev/null 2>&1; then
        apt_update_once
        DEBIAN_FRONTEND=noninteractive apt-get install -y gpg </dev/null \
            || die "apt-get could not install gpg, which is needed to check Docker's signing key." \
                   "Failing command: apt-get install -y gpg" "Nothing else was changed."
        changed "installed the Debian package gpg"
    fi
    if [ -f "$APT_MAIN_SOURCES" ] && grep -qs 'download\.docker\.com' "$APT_MAIN_SOURCES"; then
        die "$APT_MAIN_SOURCES already lists download.docker.com, which would clash with the repository entry this installer writes." \
            "Remove that line (Docker's instructions use $APT_SOURCES_DIR/docker.sources instead), then re-run. Nothing was changed."
    fi
    docker_fetch_key
    foreign=$(docker_foreign_sources)
    if [ -n "$foreign" ]; then
        say "  Other apt source files already point at download.docker.com:"
        while IFS= read -r f; do info "$f"; done <<<"$foreign"
        info "A second entry with a different key makes 'apt-get update' fail, so they must be set aside."
        ask_yn "Rename them to *.vesselstack-saved (apt ignores those; rename back to undo)?" y \
            || die "Stopped. Nothing was changed." "Remove or disable those files yourself, then re-run."
        while IFS= read -r f; do
            mv -- "$f" "$f.vesselstack-saved"
            changed "set aside $f (now $f.vesselstack-saved)"
        done <<<"$foreign"
    fi
    install -d -m 0755 -o root -g root "$APT_SOURCES_DIR"
    if ! cmp -s "$DOCKER_KEY_DIR/docker.asc" "$keyfile"; then
        install -m 0644 -o root -g root "$DOCKER_KEY_DIR/docker.asc" "$keyfile"
        changed "installed Docker's signing key at $keyfile"
    fi
    cat > "$DOCKER_KEY_DIR/docker.sources" <<SOURCES
Types: deb
URIs: $DOCKER_REPO_BASE/$DOCKER_DISTRO
Suites: $DOCKER_CODENAME
Components: stable
Architectures: $DOCKER_ARCH
Signed-By: $keyfile
SOURCES
    if ! cmp -s "$DOCKER_KEY_DIR/docker.sources" "$srcfile"; then
        install -m 0644 -o root -g root "$DOCKER_KEY_DIR/docker.sources" "$srcfile"
        changed "added Docker's apt repository ($srcfile: $DOCKER_DISTRO $DOCKER_CODENAME $DOCKER_ARCH)"
    fi
    DEBIAN_FRONTEND=noninteractive apt-get update </dev/null \
        || die "apt-get update failed after adding Docker's repository." \
               "Failing command: apt-get update" \
               "Left in place: $srcfile and $keyfile (nothing else was installed or removed)." \
               "Check the internet connection and the clock, then re-run (safe to repeat)." \
               "To undo: sudo rm $srcfile $keyfile"
    APT_UPDATED=1
}

# Installs Docker Engine and the Compose v2 plugin. Called only when they are not both there.
install_docker() {
    local conflicts=() engine_conflicts=() pkg mode data would_remove extras=() name conflicts_label=
    local keyfile=$APT_KEYRING_DIR/docker.asc srcfile=$APT_SOURCES_DIR/docker.sources
    local -a packages
    docker_platform
    for pkg in "${DOCKER_CONFLICTS[@]}"; do
        if pkg_installed "$pkg"; then conflicts+=("$pkg"); fi
    done
    for pkg in "${conflicts[@]}"; do
        case " ${DOCKER_ENGINE_CONFLICTS[*]} " in *" $pkg "*) engine_conflicts+=("$pkg") ;; esac
    done
    if [ "${#engine_conflicts[@]}" -gt 0 ] || ! command -v docker >/dev/null 2>&1; then
        mode=full
        packages=("${DOCKER_PACKAGES[@]}")
    else
        mode=plugin
        packages=(docker-compose-plugin)
    fi

    say ""
    say "  Docker Engine and the Compose v2 plugin are not both installed."
    info "VesselStack runs Home Assistant, InfluxDB, Grafana and the other services as"
    info "containers, so it needs them. This installs Docker's own packages from Docker's"
    info "official apt repository, as described at https://docs.docker.com/engine/install/$DOCKER_DISTRO/:"
    info "  - repository: $DOCKER_REPO_BASE/$DOCKER_DISTRO ($DOCKER_CODENAME, $DOCKER_ARCH), signed-by $keyfile"
    info "  - signing key: downloaded over https and checked against fingerprint $DOCKER_KEY_FPR"
    info "  - packages: ${packages[*]}"
    info "  - the docker service is enabled and started"
    info "No docker group change is made: this installer runs as root and does not need it."
    if [ "$OFFLINE" -eq 1 ] || [ "$PKG_MODE" != apt ]; then
        die "Docker with Compose v2 is required but missing, and it cannot be installed automatically here." \
            "Install Docker Engine and the compose plugin (https://docs.docker.com/engine/install/$DOCKER_DISTRO/), then re-run."
    fi
    ask_yn "Add Docker's apt repository and install ${packages[*]} now?" y || die "Docker is required. Nothing more was changed." \
        "Install it yourself (see docs/INSTALLATION.md) and re-run."

    if [ "${#conflicts[@]}" -gt 0 ]; then
        say ""
        say "  These installed packages conflict with Docker's own and must be removed first: ${conflicts[*]}"
        info "They are removed with 'apt-get remove' (not purge): images, containers and volumes"
        info "under $DOCKER_DATA_DIR stay on disk, and Docker's engine reuses that directory."
        # Ask apt what else would go with them (packages that depend on them).
        would_remove=$(DEBIAN_FRONTEND=noninteractive apt-get -s remove "${conflicts[@]}" </dev/null 2>/dev/null \
            | awk '$1 == "Remv" {print $2}' || true)
        for name in $would_remove; do
            case " ${conflicts[*]} " in *" $name "*) ;; *) extras+=("$name") ;; esac
        done
        if [ "${#extras[@]}" -gt 0 ]; then
            warn "apt would also remove packages that depend on them: ${extras[*]}"
            conflicts_label="${conflicts[*]} (and, because they depend on them: ${extras[*]})"
        else
            conflicts_label="${conflicts[*]}"
        fi
        data=
        if [ "${#engine_conflicts[@]}" -gt 0 ]; then data=$(docker_data_summary); fi
        if [ -n "$data" ]; then
            warn "A Docker daemon on this machine already holds $data."
            if [ "$ASSUME_YES" -eq 1 ]; then
                die "Not removing ${conflicts[*]}: Docker here already holds $data, and --yes is not enough to remove it." \
                    "Back up what you need, then run again from a terminal and confirm, or remove the packages yourself:" \
                    "  sudo apt-get remove ${conflicts[*]}" \
                    "Nothing was changed."
            fi
            ask_yn "Remove $conflicts_label anyway? The containers stop while Docker is replaced." n \
                || die "Stopped. Nothing was changed." "Back up your containers first, or remove the packages yourself, then re-run."
        else
            ask_yn "Remove $conflicts_label now?" n \
                || die "Docker cannot be installed next to them. Nothing was changed." \
                       "Remove them yourself (sudo apt-get remove ${conflicts[*]}) and re-run."
        fi
    fi

    if [ "$mode" = plugin ]; then
        # Docker's engine is already there: add only the Compose plugin. The repository is
        # set up first when apt does not know the package yet.
        apt_update_once
        if [ -z "$(apt_candidate docker-compose-plugin)" ]; then docker_setup_repo; fi
        [ -n "$(apt_candidate docker-compose-plugin)" ] || die "Docker's repository does not offer docker-compose-plugin for $DOCKER_DISTRO $DOCKER_CODENAME ($DOCKER_ARCH)." \
            "Install the Compose plugin as described at https://docs.docker.com/compose/install/linux/ and re-run."
    else
        docker_setup_repo
        [ -n "$(apt_candidate docker-ce)" ] || die "Docker's repository is configured but does not offer docker-ce for $DOCKER_DISTRO $DOCKER_CODENAME ($DOCKER_ARCH)." \
            "That release may not be supported yet: see https://download.docker.com/linux/$DOCKER_DISTRO/dists/" \
            "Left in place: $srcfile and $keyfile (undo: sudo rm $srcfile $keyfile)."
    fi
    if [ "${#conflicts[@]}" -gt 0 ]; then
        DEBIAN_FRONTEND=noninteractive apt-get remove -y "${conflicts[@]}" </dev/null \
            || die "apt-get could not remove the conflicting packages." \
                   "Failing command: apt-get remove -y ${conflicts[*]}" \
                   "Docker's repository is configured; nothing from it was installed. Fix the apt error above and re-run."
        changed "removed conflicting packages: ${conflicts[*]}${extras[*]:+ (and dependents: ${extras[*]})}"
        hash -r
    fi
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}" </dev/null \
        || die "apt-get could not install: ${packages[*]}" \
               "Failing command: apt-get install -y ${packages[*]}" \
               "Docker's repository is configured ($srcfile); some packages may be half-installed." \
               "Re-run this installer (safe to repeat) or run: sudo apt-get install -f"
    changed "installed ${packages[*]}"
    hash -r
    DOCKER_INSTALLED_NOW=1
}

# Starts the daemon if needed, then (after a fresh install) proves the result: docker
# --version, Compose major version >= 2 and a daemon that answers (docker info pulls nothing).
docker_start_and_verify() {
    local out major i
    if command -v systemctl >/dev/null 2>&1 && ! systemctl is-active --quiet docker 2>/dev/null; then
        systemctl enable --now docker </dev/null || die "Could not start the Docker service." \
            "Failing command: systemctl enable --now docker" "Check: systemctl status docker; journalctl -u docker"
        changed "enabled and started the docker service"
    fi
    [ "$DOCKER_INSTALLED_NOW" -eq 1 ] || return 0
    out=$(docker --version 2>&1) || die "docker --version failed after the installation." "Output: $out" \
        "Docker's packages are installed; check 'dpkg -l docker-ce docker-ce-cli' and re-run."
    ok "$out"
    out=$(docker compose version 2>&1) || die "docker compose version failed after the installation." "Output: $out" \
        "Check 'dpkg -l docker-compose-plugin' (installed from $APT_SOURCES_DIR/docker.sources) and re-run."
    major=$(printf '%s\n' "$out" | sed -nE 's/.*[Vv]ersion v?([0-9]+)\..*/\1/p' | head -n 1)
    if [ -n "$major" ] && [ "$major" -lt 2 ]; then
        die "Docker Compose is version $major, but VesselStack needs Compose v2 or newer." "Output: $out" \
            "Make sure no old docker-compose package is installed, then re-run."
    fi
    ok "$out"
    for i in 1 2 3 4 5 6 7 8 9 10; do
        if docker info >/dev/null 2>&1; then break; fi
        if [ "$i" -eq 10 ]; then
            die "The Docker daemon does not answer (docker info failed)." \
                "Check: systemctl status docker; journalctl -u docker -n 50" \
                "The packages are installed; fix the daemon, then re-run (safe to repeat)."
        fi
        sleep 1
    done
    ok "the Docker daemon answers (docker info)"
}

# shellcheck disable=SC2317,SC2329  # Invoked through the EXIT trap.
cleanup() {
    if [ -n "$STAGE" ] && [ -d "$STAGE" ]; then rm -rf -- "$STAGE"; fi
    if [ -n "$WIZARD_DIR" ] && [ -d "$WIZARD_DIR" ]; then rm -rf -- "$WIZARD_DIR"; fi
    if [ -n "$DOCKER_KEY_DIR" ] && [ -d "$DOCKER_KEY_DIR" ]; then rm -rf -- "$DOCKER_KEY_DIR"; fi
    remove_wizard_user
}

# Removes the temporary wizard account (if this run created one), also after a failure.
# shellcheck disable=SC2317,SC2329  # Invoked through the EXIT trap.
remove_wizard_user() {
    local name=$WIZARD_TMPUSER
    [ -n "$name" ] || return 0
    if id -u "$name" >/dev/null 2>&1; then
        if command -v pkill >/dev/null 2>&1; then pkill -KILL -u "$name" >/dev/null 2>&1 || true; fi
        userdel "$name" >/dev/null 2>&1 || printf 'WARNING: could not remove the temporary account %s; remove it with: sudo userdel %s\n' "$name" "$name" >&2
    fi
    WIZARD_TMPUSER=
}

# Creates the dedicated, unshared account the wizard runs as when there is no real SUDO_USER.
# (Never "nobody": other daemons share that identity and could tamper with the wizard output.)
create_wizard_user() {
    if id -u "$WIZARD_ACCOUNT" >/dev/null 2>&1; then
        # Left over from an interrupted run: it is ours by name only, so replace it with a fresh account.
        WIZARD_TMPUSER=$WIZARD_ACCOUNT
        remove_wizard_user
        if id -u "$WIZARD_ACCOUNT" >/dev/null 2>&1; then
            die "The account '$WIZARD_ACCOUNT' already exists and could not be removed." \
                "Remove it with: sudo userdel $WIZARD_ACCOUNT   (or use --no-wizard --config FILE)."
        fi
    fi
    WIZARD_TMPUSER=$WIZARD_ACCOUNT
    useradd --system --no-create-home --shell /usr/sbin/nologin "$WIZARD_ACCOUNT" \
        || die "Could not create the temporary wizard account '$WIZARD_ACCOUNT'." \
               "Run with sudo from a normal account, or use --no-wizard --config FILE."
    changed "created the temporary system account '$WIZARD_ACCOUNT' for the wizard (removed again when this script ends)"
}

# KEY=VALUE env files are never sourced here. This reads them with shlex after a
# conservative syntax check (no command substitution, expansions or metacharacters).
read -r -d '' ENVTOOL <<'PY' || true
import re, shlex, sys

def parse(path):
    values = {}
    with open(path, encoding="utf-8") as handle:
        for number, raw in enumerate(handle, 1):
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
            if not match:
                raise ValueError(f"line {number}: expected KEY=VALUE")
            key, value = match.groups()
            single = double = escaped = False
            for char in value:
                if escaped:
                    escaped = False
                elif char == "\\" and not single:
                    escaped = True
                elif char == "'" and not double:
                    single = not single
                elif char == '"' and not single:
                    double = not double
                elif not single and char in "$`":
                    raise ValueError(f"line {number}: shell expansion is not allowed in {key}")
                elif not single and not double and char in ";&|<>()":
                    raise ValueError(f"line {number}: unquoted shell metacharacter in {key}")
            if single or double:
                raise ValueError(f"line {number}: unbalanced quote in {key}")
            tokens = shlex.split(value, posix=True)
            if len(tokens) > 1:
                raise ValueError(f"line {number}: {key} must be a single value")
            values[key] = tokens[0] if tokens else ""
    return values

mode, path = sys.argv[1], sys.argv[2]
try:
    values = parse(path)
except (OSError, ValueError) as exc:
    print(f"{path}: {exc}", file=sys.stderr)
    sys.exit(1)
if mode == "get":
    for key in sys.argv[3:]:
        print(values.get(key, ""))
PY

# Re-validates a staged file with the wizard's own rules (known keys only, allowed characters,
# enums and booleans). Runs as root from the root-owned release tree; python -I ignores the
# environment and the current directory.
validate_staged() {
    python3 -I "$RELEASE_DIR/vesselstack-wizard.py" --validate "$1"
}

# Copies an untrusted file ONCE into a root-owned staging path: opened without following
# a symlink (and without blocking on a FIFO), must be a regular file of at most N bytes.
# Everything afterwards (lint, install) uses only the staged copy, never the original.
read -r -d '' STAGETOOL <<'PY' || true
import os, stat, sys

src, dest, limit = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
except OSError as exc:
    sys.exit(f"{src}: cannot open (missing, or a symlink): {exc.strerror}")
with os.fdopen(fd, "rb") as handle:
    info = os.fstat(handle.fileno())
    if not stat.S_ISREG(info.st_mode):
        sys.exit(f"{src}: not a regular file")
    if info.st_size > limit:
        sys.exit(f"{src}: larger than {limit} bytes")
    data = handle.read(limit + 1)
if len(data) > limit:
    sys.exit(f"{src}: larger than {limit} bytes")
out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(out, "wb") as handle:
    os.fchown(handle.fileno(), 0, 0)
    handle.write(data)
PY

stage_untrusted() { python3 -I -c "$STAGETOOL" "$1" "$2" "$CONFIG_MAX_BYTES"; }

env_get() { python3 -I -c "$ENVTOOL" get "$1" "$2"; }

# Safe tarball inspection: only regular files and directories, everything under
# vesselstack-<version>/, no absolute or .. paths, no setuid bits, sane size.
read -r -d '' TARCHECK <<'PY' || true
import sys, tarfile

path, prefix = sys.argv[1], sys.argv[2]
limit_files, limit_bytes = 20000, 1 << 30
count = total = 0
try:
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            count += 1
            total += max(member.size, 0)
            name = member.name
            parts = name.split("/")
            if name.startswith("/") or ".." in parts or "\\" in name or "\n" in name or "\0" in name:
                raise ValueError(f"unsafe path in archive: {name!r}")
            if parts[0] != prefix:
                raise ValueError(f"unexpected top-level entry: {name!r}")
            if not (member.isreg() or member.isdir()):
                raise ValueError(f"not a plain file or directory (links and devices are refused): {name!r}")
            if member.mode & 0o7000:
                raise ValueError(f"setuid/setgid/sticky bit set: {name!r}")
            if count > limit_files or total > limit_bytes:
                raise ValueError("archive is unreasonably large")
except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
    print(f"archive refused: {exc}", file=sys.stderr)
    sys.exit(1)
if count == 0:
    print("archive refused: empty", file=sys.stderr)
    sys.exit(1)
PY

# Command to re-run this script: the saved file when there is one, otherwise the full
# download-and-run form (when piped, $0 is just "bash" and there is no file to re-run).
rerun_cmd() {
    if [ -f "$0" ]; then
        printf 'sudo bash %q %s' "$0" "$*"
    else
        printf "curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 %s/v%s/get-vesselstack.sh | sudo bash -s -- %s" \
            "$GITHUB_RELEASES" "$DEFAULT_VERSION" "$*"
    fi
}

# ---------------------------------------------------------------- steps

step_check_computer() {
    step 1 "Check this computer"
    [ "$(uname -s)" = Linux ] || die "VesselStack installs on Linux (Raspberry Pi OS or Debian). This is $(uname -s)." \
        "Run this on the Raspberry Pi, not on a Mac or Windows PC."
    if [ "$(id -u)" -ne 0 ]; then
        die "This installer must run as root, because it installs packages and system services." \
            "Re-run it with sudo, for example:" \
            "  curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 $GITHUB_RELEASES/v$DEFAULT_VERSION/get-vesselstack.sh | sudo bash" \
            "or, if you saved the script first:  sudo bash get-vesselstack.sh"
    fi
    ok "running as root"
    local id='' id_like='' pretty='this system' arch
    # shellcheck disable=SC1091  # The system's own file.
    if [ -r "$OS_RELEASE_FILE" ]; then
        id=$(. "$OS_RELEASE_FILE" && printf '%s' "${ID:-}")
        id_like=$(. "$OS_RELEASE_FILE" && printf '%s' "${ID_LIKE:-}")
        pretty=$(. "$OS_RELEASE_FILE" && printf '%s' "${PRETTY_NAME:-this system}")
    fi
    case " $id $id_like " in
        *" debian "*|*" raspbian "*|*" ubuntu "*)
            ok "operating system: $pretty (Debian family)" ;;
        *)
            warn "$pretty is not Raspberry Pi OS or Debian. VesselStack is only tested there."
            warn "Automatic package installation will be skipped; you must install the prerequisites yourself."
            ask_yn "Continue anyway?" n || die "Stopped. Nothing was changed." ;;
    esac
    arch=$(uname -m)
    case "$arch" in
        aarch64|arm64) ok "processor: $arch (64-bit Raspberry Pi)" ;;
        *) warn "processor is $arch. VesselStack is built and tested for 64-bit Raspberry Pi (aarch64); other CPUs are unsupported." ;;
    esac
    # A 32-bit Raspberry Pi OS can run on a 64-bit kernel: uname says aarch64, the packages are armhf.
    if [ "$(dpkg --print-architecture 2>/dev/null || true)" = armhf ]; then
        warn "The installed system is 32-bit (armhf packages), even if the kernel is 64-bit."
        warn "VesselStack needs 64-bit Raspberry Pi OS; Docker no longer publishes new Engine releases for 32-bit."
    fi
    PKG_MODE=apt
    case " $id $id_like " in
        *" debian "*|*" raspbian "*|*" ubuntu "*) ;;
        *) PKG_MODE=manual ;;
    esac
    if [ "$PKG_MODE" = apt ] && ! command -v apt-get >/dev/null 2>&1; then PKG_MODE=manual; fi
}

step_preflight() {
    step 2 "Preflight checks"
    local free_mb mem_mb target
    target=$(nearest_existing "$RELEASE_BASE")
    free_mb=$(df -Pm -- "$target" | awk 'NR==2 {print $4}')
    if [ "${free_mb:-0}" -lt 2048 ]; then
        die "Only ${free_mb:-0} MiB free where VesselStack will be unpacked ($target); at least 2048 MiB is needed, and far more for the data." \
            "Free up space or use a larger disk (an SSD is recommended), then re-run."
    elif [ "$free_mb" -lt 8192 ]; then
        warn "${free_mb} MiB free. That is enough to install but tight once container images and telemetry grow; 8 GiB or more is recommended."
        ask_yn "Continue anyway?" y || die "Stopped. Nothing was changed."
    else
        ok "disk space: ${free_mb} MiB free"
    fi
    mem_mb=$(awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
    if [ "${mem_mb:-0}" -lt 1800 ]; then
        warn "This computer has about ${mem_mb} MiB of memory. 4 GB or more is recommended; a smaller Pi may be slow or run out of memory."
        ask_yn "Continue anyway?" y || die "Stopped. Nothing was changed."
    else
        ok "memory: ${mem_mb} MiB"
    fi
    if [ "$OFFLINE" -eq 1 ]; then
        ok "offline mode: skipping the internet check"
    elif [ -n "$TARBALL_ARG" ]; then
        ok "using a local release tarball: skipping the internet check"
    elif curl -fsSI --proto '=https' --proto-redir '=https' --tlsv1.2 --connect-timeout 10 --max-time 20 https://github.com >/dev/null 2>&1; then
        ok "internet: github.com reachable"
    else
        die "Cannot reach https://github.com. Check the network (and DNS) and re-run." \
            "No internet on this machine (for example on the boat)? Use the offline path:" \
            "  sudo bash get-vesselstack.sh --offline --bundle DIR --tarball vesselstack-$VERSION.tar.gz" \
            "(see docs/DEPLOYMENT.md)"
    fi
    if [ -r "$INSTALL_ROOT/config/installed-version" ]; then
        INSTALLED_VERSION=$(<"$INSTALL_ROOT/config/installed-version")
        INSTALLED_VERSION=${INSTALLED_VERSION//[[:space:]]/}
        if [ "$INSTALLED_VERSION" = "$VERSION" ]; then
            say ""
            ok "VesselStack $INSTALLED_VERSION is already installed in $INSTALL_ROOT; nothing to do."
            info "Check it with:  sudo vesselstackctl doctor"
            info "Show addresses: sudo vesselstackctl urls"
            exit 0
        fi
        # One-time renumbering shim (2026-10-25): former 1.x releases are now 0.x
        # (1.0.0 -> 0.0.1, 1.0.1 -> 0.0.2, 1.M.P -> 0.M.P). Same mapping as migrate.sh.
        COMPARE_VERSION=$INSTALLED_VERSION
        if [[ "$INSTALLED_VERSION" =~ ^1\.([0-9]+)\.([0-9]+)$ && "$VERSION" == 0.* ]]; then
            if [ "${BASH_REMATCH[1]}" -eq 0 ]; then
                COMPARE_VERSION="0.0.$((BASH_REMATCH[2] + 1))"
            else
                COMPARE_VERSION="0.${BASH_REMATCH[1]}.${BASH_REMATCH[2]}"
            fi
            info "Installed version $INSTALLED_VERSION was renumbered; it is treated as $COMPARE_VERSION."
        fi
        if [ "$COMPARE_VERSION" = "$VERSION" ]; then
            UPGRADE=1
            say ""
            warn "VesselStack $INSTALLED_VERSION is installed and is the same release as $VERSION under the new numbering."
            info "The configuration marker is updated by 'vesselstackctl update', which runs a preflight check and a verified backup first."
            info "Nothing will be overwritten until you confirm that, later in this run."
            return 0
        fi
        if [ "$(printf '%s\n%s\n' "$COMPARE_VERSION" "$VERSION" | sort -V | head -n1)" != "$COMPARE_VERSION" ]; then
            die "VesselStack $INSTALLED_VERSION is installed, which is newer than $VERSION. Downgrades are not supported." \
                "Restore a backup instead (see the README), or pass the newer --version."
        fi
        UPGRADE=1
        say ""
        warn "VesselStack $INSTALLED_VERSION is already installed. This will be an UPGRADE to $VERSION."
        info "Your configuration, passwords and data are kept. The upgrade is done by the built-in"
        info "command 'vesselstackctl update', which runs a preflight check and a verified backup first."
        info "Nothing will be overwritten until you confirm that, later in this run."
    else
        ok "no existing installation found in $INSTALL_ROOT (fresh install)"
    fi
}

step_prerequisites() {
    step 3 "Install prerequisites"
    local missing=() pkg
    if [ "$PKG_MODE" = apt ]; then
        for pkg in "${APT_PACKAGES[@]}"; do
            pkg_installed "$pkg" || missing+=("$pkg")
        done
        if [ "${#missing[@]}" -eq 0 ]; then
            ok "required packages already installed (${APT_PACKAGES[*]})"
        elif [ "$OFFLINE" -eq 1 ]; then
            die "Offline mode cannot install packages. Missing: ${missing[*]}" \
                "Install them while the machine has internet (sudo apt-get install ${missing[*]}), or from a local mirror, then re-run."
        else
            say "  Installing with apt-get: ${missing[*]}"
            apt_update_once
            DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}" </dev/null \
                || die "apt-get could not install: ${missing[*]}"
            changed "installed Debian packages: ${missing[*]}"
        fi
    else
        info "Skipping apt (not a Debian-family system). Checking for the tools instead."
    fi
    for pkg in curl jq openssl python3 rsync tar sha256sum; do
        command -v "$pkg" >/dev/null 2>&1 || die "Required command is missing: $pkg" "Install it, then re-run."
    done
    python3 -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null \
        || die "Python 3.9 or newer with the venv module is required (Debian package python3-venv)." "Install it, then re-run."
    ok "tools present: curl jq openssl python3 (+venv) rsync tar"

    if compose_ok; then
        ok "Docker and Docker Compose v2 are installed"
    elif [ "$OFFLINE" -eq 1 ] || [ "$PKG_MODE" != apt ]; then
        say ""
        say "  Docker Engine and the Compose v2 plugin are not both installed."
        die "Docker with Compose v2 is required but missing, and it cannot be installed automatically here." \
            "Install Docker Engine and the compose plugin (https://docs.docker.com/engine/install/debian/), then re-run."
    else
        install_docker
    fi
    docker_start_and_verify
}

# Copies the tarball into a root-owned staging directory, verifies it there, and
# only ever unpacks that verified copy.
step_download() {
    step 4 "Download and verify VesselStack $VERSION"
    local name=vesselstack-$VERSION.tar.gz expected_hash expected_name actual
    if [ ! -e "$RELEASE_BASE" ]; then
        install -d -m 0755 -o root -g root "$RELEASE_BASE"
        changed "created $RELEASE_BASE (root:root, mode 755)"
    fi
    require_root_controlled "$RELEASE_BASE" \
        || die "$RELEASE_BASE (or a parent directory) is not root-owned, or is writable by others." \
               "Releases are installed as root, so this location must be root-controlled. Use --dir to pick another."
    STAGE=$(mktemp -d "$RELEASE_BASE/.stage.XXXXXX")
    chmod 0700 "$STAGE"

    if [ -n "$TARBALL_ARG" ]; then
        [ -f "$TARBALL_ARG" ] || die "Tarball not found: $TARBALL_ARG"
        [ -f "$TARBALL_ARG.sha256" ] || die "Checksum file not found: $TARBALL_ARG.sha256" \
            "Copy vesselstack-$VERSION.tar.gz.sha256 from the same release next to the tarball."
        install -m 0600 -o root -g root "$TARBALL_ARG" "$STAGE/$name"
        install -m 0600 -o root -g root "$TARBALL_ARG.sha256" "$STAGE/$name.sha256"
        ok "using local files $TARBALL_ARG and $TARBALL_ARG.sha256"
    else
        say "  Downloading $BASE_URL/$name"
        local file
        for file in "$name" "$name.sha256"; do
            curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 --retry 3 --retry-delay 2 --connect-timeout 15 \
                -o "$STAGE/$file" "$BASE_URL/$file" </dev/null \
                || die "Download failed: $BASE_URL/$file" \
                       "Check that release v$VERSION exists and that github.com is reachable. Nothing was installed."
        done
    fi

    read -r expected_hash expected_name _ < "$STAGE/$name.sha256" || true
    expected_hash=${expected_hash:-}
    expected_name=${expected_name#\*}
    [[ "$expected_hash" =~ ^[0-9a-f]{64}$ ]] || die "The .sha256 file is not a valid sha256 checksum line. Nothing was installed."
    [ "$expected_name" = "$name" ] || die "The .sha256 file is for '$expected_name', not '$name'. Nothing was installed."
    actual=$(sha256_file "$STAGE/$name")
    if [ "$actual" != "$expected_hash" ]; then
        die "CHECKSUM MISMATCH: the downloaded file does not match its .sha256." \
            "  expected $expected_hash" \
            "  actual   $actual" \
            "The download is corrupt or has been tampered with. Nothing was installed. Try again."
    fi
    if [ -n "$PIN_SHA256" ]; then
        [ "${PIN_SHA256,,}" = "$actual" ] || die "The tarball does not match the --sha256 you supplied." \
            "  expected ${PIN_SHA256,,}" "  actual   $actual" "Nothing was installed."
        ok "sha256 also matches the value you pinned with --sha256"
    fi
    ok "checksum verified"
    info "sha256 $actual"
    info "(Record this value. The .sha256 file comes from the same release as the tarball, so it"
    info " detects corruption but cannot prove the release itself is genuine; compare it with the"
    info " value shown on the GitHub release page, or pin it with --sha256.)"

    RELEASE_DIR=$RELEASE_BASE/$VERSION
    if [ -d "$RELEASE_DIR" ] && [ -f "$RELEASE_DIR/.tarball.sha256" ] \
        && [ "$(<"$RELEASE_DIR/.tarball.sha256")" = "$actual" ] && require_root_controlled "$RELEASE_DIR"; then
        ok "already unpacked and verified: $RELEASE_DIR (reusing)"
        return 0
    fi

    python3 -I -c "$TARCHECK" "$STAGE/$name" "vesselstack-$VERSION" \
        || die "The archive failed the safety check (see above). Nothing was unpacked or installed."
    ok "archive layout is safe (plain files only, no absolute, '..' or link entries)"
    install -d -m 0755 "$STAGE/x"
    tar -xzf "$STAGE/$name" -C "$STAGE/x" --no-same-owner --no-same-permissions </dev/null
    [ -d "$STAGE/x/vesselstack-$VERSION" ] || die "Unexpected archive layout. Nothing was installed."
    chown -R root:root "$STAGE/x/vesselstack-$VERSION"
    chmod -R go-w "$STAGE/x/vesselstack-$VERSION"
    if [ ! -x "$STAGE/x/vesselstack-$VERSION/install.sh" ] || [ ! -f "$STAGE/x/vesselstack-$VERSION/vesselstack-wizard.py" ]; then
        die "The archive does not look like a VesselStack release (install.sh or the wizard is missing)."
    fi
    [ "$(tr -d '[:space:]' < "$STAGE/x/vesselstack-$VERSION/VERSION")" = "$VERSION" ] \
        || die "The archive's VERSION file does not say $VERSION. Nothing was installed."
    printf '%s\n' "$actual" > "$STAGE/x/vesselstack-$VERSION/.tarball.sha256"
    if [ -e "$RELEASE_DIR" ] || [ -L "$RELEASE_DIR" ]; then
        local aside
        aside=$RELEASE_DIR.replaced-$(date +%Y%m%d%H%M%S)
        mv -- "$RELEASE_DIR" "$aside"
        changed "moved the previous $RELEASE_DIR aside to $aside (not deleted)"
    fi
    mv -- "$STAGE/x/vesselstack-$VERSION" "$RELEASE_DIR"
    changed "unpacked release to $RELEASE_DIR (owner root, not writable by others)"
    local commit=
    if [ -r "$RELEASE_DIR/COMMIT" ]; then commit=$(<"$RELEASE_DIR/COMMIT"); fi
    [ -z "$commit" ] || info "release commit $commit"
}

describe_ssh_access() {
    local host ip user
    host=$(hostname 2>/dev/null || echo raspberrypi)
    ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
    user=${SUDO_USER:-}
    [ -n "$user" ] || user=YOUR_USER
    say ""
    say "  The wizard is a web page that only this Raspberry Pi can reach (loopback only)."
    say "  Open it in one of two ways:"
    say ""
    say "  The wizard prints one address ending in #token=... (a one-time key that stays in your"
    say "  browser and is never sent in a request). Open exactly that address:"
    say ""
    say "    A) On this Pi, in a browser: open the address printed below."
    say ""
    say "    B) From your laptop. Open a NEW terminal on the laptop and run:"
    say "         ssh -L $WIZARD_PORT:127.0.0.1:$WIZARD_PORT $user@${ip:-$host}"
    if [ -n "$ip" ]; then say "       (or $user@$host.local if your network resolves that name)"; fi
    say "       keep that terminal open, then open the same printed address"
    say "       (http://127.0.0.1:$WIZARD_PORT/#token=...) in the laptop's browser."
    say ""
    say "  When you press 'Save configuration' on the last page, the wizard closes by itself and"
    say "  this installer continues. Press Ctrl-C here to cancel."
    say ""
}

# Runs the wizard as an unprivileged user in a private 0700 directory owned by that user.
# The user is the one who invoked sudo; with no (or root) SUDO_USER a temporary dedicated
# system account is created for the run and removed on exit (never the shared "nobody").
run_wizard() {
    local user=${SUDO_USER:-} port=$WIZARD_PORT
    if [ -z "$user" ] || [ "$user" = root ] || ! id -u "$user" >/dev/null 2>&1 || [ "$(id -u "$user")" -eq 0 ]; then
        if ! command -v useradd >/dev/null 2>&1 || ! command -v userdel >/dev/null 2>&1; then
            die "useradd/userdel are missing (package passwd)." "Use --no-wizard --config FILE."
        fi
        create_wizard_user
        user=$WIZARD_ACCOUNT
    fi
    id -u "$user" >/dev/null 2>&1 || die "Cannot run the wizard unprivileged: user '$user' does not exist." \
        "Run with sudo from a normal account, or use --no-wizard --config FILE."
    command -v runuser >/dev/null 2>&1 || die "runuser is missing (package util-linux)." \
        "Install it, or use --no-wizard --config FILE."
    WIZARD_DIR=$(mktemp -d "$RELEASE_BASE/.wizard.XXXXXX")
    chown "$user" "$WIZARD_DIR"
    chmod 0700 "$WIZARD_DIR"
    if [ -z "$port" ]; then
        port=$(python3 -I -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])') \
            || die "Could not pick a free port. Try --wizard-port 8089."
    fi
    WIZARD_PORT=$port
    describe_ssh_access
    info "the wizard runs as user '$user' (not root); it cannot write anywhere but its private directory"
    if ! runuser -u "$user" -- env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$WIZARD_DIR" LANG=C.UTF-8 \
            python3 -I "$RELEASE_DIR/vesselstack-wizard.py" --port "$port" --output "$WIZARD_DIR/vesselstack.env" --exit-after-save </dev/null; then
        die "The wizard could not run. Is port $port already in use? Try --wizard-port 8089."
    fi
}

# Runs the terminal wizard (or, without questions, its non-interactive mode) as root: it has
# no network surface, no web server and no token, so there is nothing to protect by dropping
# privileges. Its output lands in a private root-only directory and then goes through exactly
# the same copy, lint and re-validation as the browser wizard's output (finish_wizard_output).
# Argument: an already staged env file to start from (optional).
run_terminal_wizard() {
    local base=${1:-} help_text pair
    local -a args=(--cli)
    help_text=$(python3 -I "$RELEASE_DIR/vesselstack-wizard.py" --help 2>&1 || true)
    case "$help_text" in
        *--cli*) ;;
        *) die "Release $VERSION is older than the terminal wizard and cannot ask questions in the terminal." \
               "Use --web-wizard, or pass --config FILE (see vesselstack.example.env), or install a newer --version." ;;
    esac
    WIZARD_DIR=$(mktemp -d "$RELEASE_BASE/.wizard.XXXXXX")
    chmod 0700 "$WIZARD_DIR"
    if [ -n "$base" ]; then args+=(--base "$base"); fi
    for pair in "${SETS[@]+"${SETS[@]}"}"; do args+=(--set "$pair"); done
    args+=(--output "$WIZARD_DIR/vesselstack.env")
    if [ "$ASSUME_YES" -eq 1 ] || [ "$DEFAULTS_ONLY" -eq 1 ] || [ "${#SETS[@]}" -gt 0 ]; then
        say "  Configuring without questions (--yes/--defaults/--set)."
        env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$WIZARD_DIR" LANG=C.UTF-8 \
            python3 -I "$RELEASE_DIR/vesselstack-wizard.py" "${args[@]}" --non-interactive </dev/null \
            || die "The configuration was refused (see above). Nothing was installed." \
                   "Check the --set values against vesselstack.example.env."
    else
        open_prompt || die "A question needs an answer but there is no terminal to ask on." \
            "Run again from a terminal (over SSH use ssh -t), or add --yes or --defaults (see --help)."
        say "  Starting the setup questions. Press Enter to accept the answer in [brackets]; ? explains a question."
        say ""
        env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$WIZARD_DIR" LANG=C.UTF-8 \
            python3 -I "$RELEASE_DIR/vesselstack-wizard.py" "${args[@]}" --input-fd 3 </dev/null \
            || die "The setup questions were not finished (cancelled, or the input ended). Nothing was installed." \
                   "Run this script again to start over."
    fi
}

# Copies a wizard's output once into the root-only staging directory, lints and re-validates
# that copy with the wizard's own rules, and installs only it (root, mode 600).
finish_wizard_output() {
    local source=$1
    if [ ! -e "$source" ] && [ ! -L "$source" ]; then
        die "The wizard was closed before a configuration was saved. Nothing was installed." "Run this script again to start over."
    fi
    stage_untrusted "$source" "$STAGE/wizard.env" \
        || die "The wizard output was refused (see above). Nothing was installed."
    python3 -I -c "$ENVTOOL" lint "$STAGE/wizard.env" || die "The saved configuration is invalid (see above)."
    validate_staged "$STAGE/wizard.env" || die "The saved configuration is invalid (see above). Nothing was installed."
    install -m 0600 -o root -g root "$STAGE/wizard.env" "$ENV_FILE"
    rm -rf -- "$WIZARD_DIR"
    WIZARD_DIR=
    changed "configuration saved to $ENV_FILE (root only, mode 600)"
}

step_configure() {
    step 5 "Configure"
    ENV_FILE=$RELEASE_BASE/vesselstack.env
    if [ "$UPGRADE" -eq 1 ]; then
        ok "upgrade: the installed configuration ($INSTALL_ROOT/config/vesselstack.env) is kept; the wizard is skipped"
        return 0
    fi
    if [ -n "$CONFIG_ARG" ]; then
        # Copy once into the root-only staging dir, then lint and use ONLY that copy.
        stage_untrusted "$CONFIG_ARG" "$STAGE/config.env" \
            || die "The configuration file was refused (see above)." "It must be a regular file (not a symlink) of at most $CONFIG_MAX_BYTES bytes."
        python3 -I -c "$ENVTOOL" lint "$STAGE/config.env" \
            || die "The configuration file was refused (see above). It must contain only KEY=VALUE lines."
        validate_staged "$STAGE/config.env" \
            || die "The configuration file was refused (see above). It may only set known VesselStack settings with plain values." \
                   "Compare it with vesselstack.example.env."
        if [ "${#SETS[@]}" -gt 0 ]; then
            say "  Applying ${#SETS[@]} --set value(s) on top of $CONFIG_ARG."
            run_terminal_wizard "$STAGE/config.env"
            finish_wizard_output "$WIZARD_DIR/vesselstack.env"
            return 0
        fi
        install -m 0600 -o root -g root "$STAGE/config.env" "$ENV_FILE.new"
        mv -f -- "$ENV_FILE.new" "$ENV_FILE"
        changed "copied $CONFIG_ARG to $ENV_FILE (root only, mode 600)"
        ok "using the supplied configuration"
        return 0
    fi
    if [ -f "$ENV_FILE" ] && [ ! -L "$ENV_FILE" ]; then
        say "  A configuration saved by an earlier run exists: $ENV_FILE"
        local choice
        choice=$(ask_choice "Reuse it (r) or run the wizard again (w)?" r r w)
        if [ "$choice" = r ]; then
            # Treat the saved file like any other input: copy once to staging, check that copy.
            stage_untrusted "$ENV_FILE" "$STAGE/saved.env" \
                || die "The saved configuration was refused (see above); run again and choose w."
            python3 -I -c "$ENVTOOL" lint "$STAGE/saved.env" || die "The saved configuration is invalid; run again and choose w."
            validate_staged "$STAGE/saved.env" || die "The saved configuration is invalid (see above); run again and choose w."
            if [ "${#SETS[@]}" -gt 0 ]; then
                say "  Applying ${#SETS[@]} --set value(s) on top of the saved configuration."
                run_terminal_wizard "$STAGE/saved.env"
                cp -p -- "$ENV_FILE" "$ENV_FILE.previous"
                changed "kept the old configuration as $ENV_FILE.previous"
                finish_wizard_output "$WIZARD_DIR/vesselstack.env"
                return 0
            fi
            ok "reusing the saved configuration"
            return 0
        fi
        mv -- "$ENV_FILE" "$ENV_FILE.previous"
        changed "kept the old configuration as $ENV_FILE.previous"
    fi
    if [ "$WEB_WIZARD" -eq 1 ]; then
        say "  Starting the browser configuration wizard on this Pi (--web-wizard)."
        run_wizard
        finish_wizard_output "$WIZARD_DIR/vesselstack.env"
        return 0
    fi
    run_terminal_wizard
    finish_wizard_output "$WIZARD_DIR/vesselstack.env"
}

show_summary() {
    local file=$1 key value
    say ""
    say "  Your configuration (secrets are not shown; passwords and tokens are generated by the installer):"
    for key in BOAT_NAME BOAT_TYPE BOAT_TIMEZONE BOAT_UNITS SIGNALK_MODE SOCKETCAN_ENABLE SOCKETCAN_INTERFACE \
        AIS_ENABLE TELEGRAM_ENABLE VESSELSTACK_FIREWALL_ENABLE VESSELSTACK_USER VESSELSTACK_ROOT VESSELSTACK_DATA VESSELSTACK_BACKUP; do
        value=$(env_get "$file" "$key")
        printf '    %-28s %s\n' "$key" "${value:-(default)}"
    done
    for key in BOAT_MMSI BOAT_CALLSIGN; do
        value=$(env_get "$file" "$key")
        if [ -n "$value" ]; then value='(set)'; else value='(not set)'; fi
        printf '    %-28s %s\n' "$key" "$value"
    done
}

ensure_service_account() {
    local user
    user=$(env_get "$ENV_FILE" VESSELSTACK_USER)
    user=${user:-boat}
    [[ "$user" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "VESSELSTACK_USER '$user' is not a valid account name."
    if id -u "$user" >/dev/null 2>&1; then
        [ "$(id -u "$user")" -ne 0 ] || die "VESSELSTACK_USER must not be root."
        ok "service account '$user' exists"
        return 0
    fi
    useradd --system --create-home --shell /usr/sbin/nologin "$user"
    changed "created the service account '$user' (system user, no login shell)"
}

step_install() {
    step 6 "Install and check"
    local install_args=(--config "$ENV_FILE") bundle_wheels='' offline_env=''
    if [ "$OFFLINE" -eq 1 ]; then
        [ -d "$BUNDLE" ] || die "Offline bundle not found: $BUNDLE"
        BUNDLE=$(readlink -f -- "$BUNDLE")
        say "  Verifying the offline bundle..."
        "$RELEASE_DIR/scripts/prepare-offline-bundle.sh" --verify "$BUNDLE" </dev/null \
            || die "The offline bundle did not verify (see above). Nothing was installed."
        ok "offline bundle verified"
        install_args+=(--offline)
        bundle_wheels=$BUNDLE/wheelhouse
        offline_env="VESSELSTACK_OFFLINE=true VESSELSTACK_WHEELHOUSE=$bundle_wheels "
    fi

    if [ "$UPGRADE" -eq 1 ]; then
        say ""
        say "  Ready to upgrade $INSTALLED_VERSION -> $VERSION with:  ${offline_env}vesselstackctl update $RELEASE_DIR"
        info "This runs a preflight check, makes a verified full backup, then installs and restarts services."
        info "Services are briefly unavailable during the restart. Do not do this while underway."
        if ! ask_yn "Upgrade now?" n; then
            say ""
            say "  Nothing was changed on the installed system. To upgrade later:"
            info "sudo ${offline_env}vesselstackctl update $RELEASE_DIR"
            exit 0
        fi
    else
        show_summary "$ENV_FILE"
        local user
        user=$(env_get "$ENV_FILE" VESSELSTACK_USER)
        say ""
        say "  Next: install VesselStack and start its services. This will:"
        info "- create the service account '${user:-boat}' if it does not exist"
        local app_root data_root
        app_root=$(env_get "$ENV_FILE" VESSELSTACK_ROOT)
        data_root=$(env_get "$ENV_FILE" VESSELSTACK_DATA)
        info "- copy the application to ${app_root:-/opt/vesselstack}, data in ${data_root:-/opt/vesselstack-data}"
        info "- generate passwords and tokens, install and start systemd units and containers"
        if [ "$OFFLINE" -eq 1 ]; then info "- load container images from the offline bundle (docker load)"; fi
        ask_yn "Install and start VesselStack now?" y || {
            say ""
            say "  Stopped before installing. Your configuration is saved. To continue later:"
            info "$(rerun_cmd --no-wizard --config "$ENV_FILE")"
            exit 0
        }
        ensure_service_account
    fi

    if [ "$OFFLINE" -eq 1 ]; then
        say "  Loading container images from the bundle (docker load)..."
        "$RELEASE_DIR/scripts/prepare-offline-bundle.sh" --load "$BUNDLE" </dev/null \
            || die "Loading the offline images failed. Nothing was installed."
        changed "loaded container images from $BUNDLE"
    fi

    local ctl
    if [ "$UPGRADE" -eq 1 ]; then
        ctl=$(find_ctl) || die "vesselstackctl was not found, although VesselStack $INSTALLED_VERSION is installed." \
            "Run the installer directly: sudo $RELEASE_DIR/install.sh --config $INSTALL_ROOT/config/vesselstack.env --dry-run"
        # The installed ctl may be an old release whose "update" only reads $1 as the
        # directory, so the directory goes first and the mode travels in the environment
        # (read by old ctls' child install.sh and by the new ctl alike).
        say "  Running: ${offline_env}vesselstackctl update $RELEASE_DIR"
        if [ "$OFFLINE" -eq 1 ]; then
            VESSELSTACK_OFFLINE=true VESSELSTACK_WHEELHOUSE=$bundle_wheels "$ctl" update "$RELEASE_DIR" </dev/null \
                || die "The update failed (see above). Your data and the pre-update backup are untouched." \
                       "See the README sections on updating and restoring."
        else
            "$ctl" update "$RELEASE_DIR" </dev/null \
                || die "The update failed (see above). Your data and the pre-update backup are untouched." \
                       "See the README sections on updating and restoring."
        fi
        changed "upgraded VesselStack $INSTALLED_VERSION -> $VERSION (backup made first by vesselstackctl update)"
    else
        say "  Preflight (changes nothing): install.sh ${install_args[*]} --dry-run"
        if [ -n "$bundle_wheels" ]; then
            VESSELSTACK_WHEELHOUSE=$bundle_wheels "$RELEASE_DIR/install.sh" "${install_args[@]}" --dry-run </dev/null \
                || die "The installer's preflight failed (see above). Nothing was installed." \
                       "Fix the problem, then re-run: $(rerun_cmd --no-wizard --config "$ENV_FILE")"
            say "  Installing and starting..."
            VESSELSTACK_WHEELHOUSE=$bundle_wheels "$RELEASE_DIR/install.sh" "${install_args[@]}" --start </dev/null \
                || die "The installation failed (see above). The installer is transactional: it makes no changes if the Python environment cannot be built." \
                       "After fixing the problem, re-run: $(rerun_cmd --no-wizard --config "$ENV_FILE")"
        else
            "$RELEASE_DIR/install.sh" "${install_args[@]}" --dry-run </dev/null \
                || die "The installer's preflight failed (see above). Nothing was installed." \
                       "Fix the problem, then re-run: $(rerun_cmd --no-wizard --config "$ENV_FILE")"
            say "  Installing and starting..."
            "$RELEASE_DIR/install.sh" "${install_args[@]}" --start </dev/null \
                || die "The installation failed (see above). The installer is transactional: it makes no changes if the Python environment cannot be built." \
                       "After fixing the problem, re-run: $(rerun_cmd --no-wizard --config "$ENV_FILE")"
        fi
        changed "installed VesselStack $VERSION to $(env_get "$ENV_FILE" VESSELSTACK_ROOT) and started its services"
    fi

    ctl=$(find_ctl) || die "Installed, but vesselstackctl was not found on the PATH. Try: /usr/local/sbin/vesselstackctl doctor"
    say ""
    say "  Checking health (vesselstackctl doctor)..."
    local healthy=1
    if ! "$ctl" doctor </dev/null; then
        if [ "$DOCTOR_WAIT" -gt 0 ]; then
            warn "Services can take a minute to start. Waiting ${DOCTOR_WAIT}s and checking again."
            sleep "$DOCTOR_WAIT"
        fi
        "$ctl" doctor </dev/null || healthy=0
    fi
    say ""
    say "  Where to find things:"
    "$ctl" urls </dev/null || true
    print_access_info
    if [ "$healthy" -eq 0 ]; then
        HEALTHY=0
        warn "doctor reported problems. Re-check with 'sudo vesselstackctl doctor' and 'sudo vesselstackctl status'."
    else
        ok "doctor reports a healthy installation"
    fi
}

final_report() {
    say ""
    say "$C_BOLD== Done ==$C_OFF"
    if [ "${#CHANGES[@]}" -gt 0 ]; then
        say "This run changed:"
        local item
        for item in "${CHANGES[@]}"; do info "- $item"; done
    else
        say "This run changed nothing on the system."
    fi
    say ""
    say "Next steps:"
    info "1. First-run setup of Home Assistant, Grafana and Boat Chat happens in a browser, whenever you like,"
    info "   on any computer on the boat network (the addresses are listed above; the README explains each one)."
    info "2. Connect NMEA 2000 only after the bench checks: docs/INSTALLATION.md, section 7."
    info "3. Make and verify a backup:  sudo vesselstackctl backup   then   sudo vesselstackctl verify-backup FILE"
    info "4. Later upgrades: re-run this installer with a newer --version, or use 'sudo vesselstackctl update DIR'."
    info "Roll back: 'sudo vesselstackctl stop' stops everything; see the README (restore, uninstall)."
}

# Plain-text information for a headless Pi: nothing here has to be opened now.
print_access_info() {
    local host ip where user web_bind web_port heimdall chat chat_host panel_port app_root cfg=$ENV_FILE
    # After an upgrade the settings live in the installed configuration, not in the staged one.
    if [ "$UPGRADE" -eq 1 ]; then cfg=$INSTALL_ROOT/config/vesselstack.env; fi
    host=$(hostname 2>/dev/null || true)
    host=${host:-raspberrypi}
    ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
    where=${ip:-$host}
    user=${SUDO_USER:-}
    if [ -z "$user" ] || [ "$user" = root ]; then user=YOUR_USER; fi
    web_bind=$(env_get "$cfg" VESSELSTACK_WEB_BIND_ADDRESS 2>/dev/null || true)
    web_port=$(env_get "$cfg" GRAFANA_PORT 2>/dev/null || true)
    heimdall=$(env_get "$cfg" HEIMDALL_PORT 2>/dev/null || true)
    chat=$(env_get "$cfg" BOAT_CHAT_PORT 2>/dev/null || true)
    chat_host=$(env_get "$cfg" BOAT_CHAT_HOST 2>/dev/null || true)
    panel_port=$(env_get "$cfg" CONTROL_PANEL_PORT 2>/dev/null || true)
    app_root=$(env_get "$cfg" VESSELSTACK_ROOT 2>/dev/null || true)
    web_bind=${web_bind:-0.0.0.0}; web_port=${web_port:-43000}; heimdall=${heimdall:-80}
    chat=${chat:-8765}; chat_host=${chat_host:-0.0.0.0}; panel_port=${panel_port:-8780}
    app_root=${app_root:-$INSTALL_ROOT}
    say ""
    say "  Addresses (for information; use them from any computer on the boat network when you like):"
    if [ "$web_bind" = 127.0.0.1 ]; then
        info "Dashboards are limited to this Pi (VESSELSTACK_WEB_BIND_ADDRESS=127.0.0.1); reach them through an SSH tunnel."
    else
        info "Grafana (history dashboards)   http://$where:$web_port"
        if [ "$heimdall" = 80 ]; then info "Heimdall (start page)          http://$where"; else info "Heimdall (start page)          http://$where:$heimdall"; fi
    fi
    info "Home Assistant                 http://$where:8123"
    if [ "$chat_host" = 127.0.0.1 ]; then
        info "Boat Chat                      this Pi only (BOAT_CHAT_HOST=127.0.0.1), port $chat"
    else
        info "Boat Chat                      http://$where:$chat"
    fi
    if [ -n "$ip" ]; then info "(or $host.local instead of $ip if your network resolves that name)"; fi
    say ""
    say "  Generated passwords and tokens are stored root-only under $app_root/config/."
    say "  Show the Boat Chat access token (it is printed only when you run this):"
    info "sudo grep '^BOAT_CHAT_ACCESS_TOKEN=' $app_root/config/boat-chat.env"
    say "  Control panel (administration; it listens on this Pi only, port $panel_port). From your own computer:"
    info "ssh -L $panel_port:127.0.0.1:$panel_port $user@$where"
    info "then browse to http://127.0.0.1:$panel_port on that computer; its token:"
    info "sudo grep '^CONTROL_PANEL_TOKEN=' $app_root/config/control-panel.env"
}

find_ctl() {
    if command -v vesselstackctl >/dev/null 2>&1; then command -v vesselstackctl; return 0; fi
    if [ -x /usr/local/sbin/vesselstackctl ]; then printf '%s\n' /usr/local/sbin/vesselstackctl; return 0; fi
    return 1
}

confirm_plan() {
    say ""
    say "${C_BOLD}VesselStack guided installer${C_OFF}  (release $VERSION)"
    say ""
    say "This will, step by step, and tell you before each change:"
    info "1. check this computer and the internet connection"
    info "2. install the Debian packages VesselStack needs (and ask before installing Docker)"
    info "3. download VesselStack $VERSION from GitHub and verify its sha256 checksum"
    info "4. ask a few configuration questions right here in the terminal (no browser needed)"
    info "5. run the VesselStack installer (a dry run first) and start the services"
    say ""
    if ! ask_yn "Continue?" y; then
        say "Stopped. Nothing was changed."
        exit 0
    fi
}

# Stops before anything is changed when questions would have to be asked but there is no
# terminal (for example `curl | sudo bash` over ssh without -t).
require_terminal_or_options() {
    if [ "$ASSUME_YES" -eq 1 ] || [ "$DEFAULTS_ONLY" -eq 1 ]; then return 0; fi
    if open_prompt; then return 0; fi
    die "No terminal is available to ask questions on, and no option says how to answer without one." \
        "Over SSH, connect with  ssh -t USER@HOST  and run the command again (the questions are answered there), or choose:" \
        "  --yes              answer yes to everything and use the defaults, plus any --set values" \
        "  --defaults         use every question's default answer and ask nothing" \
        "  --set KEY=VALUE    set one setting (repeatable), for example --set BOAT_NAME=Foo --set SOCKETCAN_ENABLE=true" \
        "  --config FILE      use a ready-made vesselstack.env (together with --yes)" \
        "Example: curl -fsSL URL/get-vesselstack.sh | sudo bash -s -- --yes --set BOAT_NAME=Foo" \
        "Nothing was changed."
}

main() {
    parse_args "$@"
    trap cleanup EXIT
    trap 'printf "\nInterrupted. Re-run the same command to continue; finished steps are skipped.\n" >&2; exit 130' INT TERM
    step_check_computer
    require_terminal_or_options
    confirm_plan
    step_preflight
    if [ "$UPGRADE" -eq 1 ] && [ "${#SETS[@]}" -gt 0 ]; then
        die "--set cannot change the configuration of an installed system: an upgrade keeps it." \
            "Edit $INSTALL_ROOT/config/vesselstack.env and re-run install.sh, or use the control panel. Nothing was changed."
    fi
    step_prerequisites
    step_download
    step_configure
    step_install
    final_report
    if [ "$HEALTHY" -eq 1 ]; then exit 0; fi
    exit 3
}

main "$@"
