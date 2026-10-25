#!/bin/bash
# Create or refresh the displays bridge virtual environment.
# Usage: install-displays-venv.sh VENV_DIRECTORY REQUIREMENTS_FILE
#
# The installer runs as root under "umask 077", but the bridge runs as the
# unprivileged service user and must be able to execute and import everything
# in the environment. The environment is therefore built under umask 022 in a
# subshell and its modes are normalised afterwards, which also repairs an
# environment that an earlier release left 0700/0600. It stays root-owned: the
# service user can read and run it but never modify the code it executes.
# Requirements are installed with --require-hashes; once they are satisfied pip
# needs no network, so re-running offline is harmless.

set -euo pipefail

[ $# -eq 2 ] || { echo "Usage: $0 VENV_DIRECTORY REQUIREMENTS_FILE" >&2; exit 2; }
VENV=$1
REQUIREMENTS=$2
[ -r "$REQUIREMENTS" ] || { echo "Missing requirements file: $REQUIREMENTS" >&2; exit 1; }

if ! (
    umask 022
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --disable-pip-version-check --require-hashes -r "$REQUIREMENTS"
); then
    echo "Could not install the displays bridge dependencies into $VENV." >&2
    echo "pip needs the internet (PyPI) the first time; nothing else has been changed." >&2
    echo "Connect the boat and run the installer again, or set DISPLAYS_ENABLE=false." >&2
    exit 1
fi
chmod -R u=rwX,go=rX "$VENV"
