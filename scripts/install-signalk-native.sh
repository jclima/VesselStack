#!/bin/bash
# Install a pinned SignalK release without changing the host Node.js runtime.

set -euo pipefail

ENV_FILE=${1:-/opt/vesselstack/config/vesselstack.env}
# shellcheck disable=SC1090
source "$ENV_FILE"
SYSTEMD_DIR="${VESSELSTACK_SYSTEM_ROOT:-}/etc/systemd/system"

[ "${SIGNALK_MODE:-existing}" = native ] || exit 0
[ "$(id -u)" -eq 0 ] || { echo "Native SignalK installation requires root" >&2; exit 1; }
command -v node >/dev/null || { echo "Native SignalK requires Node.js 22 or newer" >&2; exit 1; }
command -v npm >/dev/null || { echo "Native SignalK requires npm" >&2; exit 1; }
node_major=$(node -p 'process.versions.node.split(".")[0]')
[ "$node_major" -ge 22 ] || { echo "Native SignalK requires Node.js 22 or newer" >&2; exit 1; }

signalk_root="$VESSELSTACK_ROOT/signalk-server"
signalk_data="$VESSELSTACK_DATA/signalk"
install -d -m 0755 "$signalk_root"
install -d -m 0750 -o "$VESSELSTACK_USER" -g "$VESSELSTACK_USER" "$signalk_data"
# An empty SIGNALK_VERSION follows the release default (common/component_versions.py).
component_versions=""
for candidate in "$(dirname -- "${BASH_SOURCE[0]}")/../common/component_versions.py" "$VESSELSTACK_ROOT/common/component_versions.py"; do
    [ -r "$candidate" ] && { component_versions=$candidate; break; }
done
[ -n "$component_versions" ] || { echo "Missing common/component_versions.py; cannot work out the Signal K version." >&2; exit 1; }
signalk_version=${SIGNALK_VERSION:-}
if [ -n "$signalk_version" ]; then
    python3 -I "$component_versions" --validate signalk "$signalk_version" >&2 || exit 1
else
    signalk_version=$(python3 -I "$component_versions" --default signalk)
fi
npm install --prefix "$signalk_root" --omit=dev "signalk-server@${signalk_version#v}"
chown -R root:root "$signalk_root"

sed -e "s|@VESSELSTACK_USER@|$VESSELSTACK_USER|g" \
    -e "s|@SIGNALK_ROOT@|$signalk_root|g" \
    -e "s|@SIGNALK_DATA@|$signalk_data|g" \
    "$VESSELSTACK_ROOT/systemd/vesselstack-signalk.service.in" \
    > "$SYSTEMD_DIR/vesselstack-signalk.service"
systemctl daemon-reload
systemctl enable vesselstack-signalk.service
