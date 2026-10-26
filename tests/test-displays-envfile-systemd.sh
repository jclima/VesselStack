#!/bin/bash
# Real systemd parse of displays-bridge.env: a SignalK token full of characters
# that are special to shells and to systemd ($ ` " \ ' # ; % and spaces) must
# arrive in the service environment unchanged. Uses a privileged Debian container
# that runs systemd as PID 1 and "systemd-run -p EnvironmentFile=".
#
# If the container cannot boot systemd (unsupported cgroup setup, rootless Docker)
# the check is skipped with a loud message instead of failing: the value is also
# covered by a parser-equivalent unit test (tests/test_displays_bridge.py).

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
SUFFIX="$$-$RANDOM"
IMAGE="vs-envfile-systemd-$SUFFIX"
NAME="vs-envfile-systemd-$SUFFIX"
WORK=$(mktemp -d)
cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker rmi -f "$IMAGE" >/dev/null 2>&1 || true
    rm -rf -- "$WORK"
}
trap cleanup EXIT

docker build -q -t "$IMAGE" - >/dev/null <<'DOCKERFILE'
FROM debian:bookworm-slim
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends systemd systemd-sysv dbus >/dev/null 2>&1 && apt-get clean
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
DOCKERFILE
docker run -d --name "$NAME" --privileged --cgroupns=host --tmpfs /run --tmpfs /run/lock \
    --tmpfs /tmp -v /sys/fs/cgroup:/sys/fs/cgroup:rw "$IMAGE" >/dev/null
booted=0
for _ in $(seq 1 40); do
    state=$(docker exec "$NAME" systemctl is-system-running 2>/dev/null || true)
    case "$state" in running|degraded) booted=1; break ;; esac
    sleep 1
done
if [ "$booted" -eq 0 ]; then
    echo "SKIP: systemd did not boot in the container; real EnvironmentFile parse not verified" >&2
    exit 0
fi

# Render the file with the real code, parse it with the real systemd, compare.
python3 - "$REPO_ROOT" "$WORK" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1] + "/displays")
import displayctl
tokens = ['a$b`c"d\\e\'f#g;h%i j  k', ' lead and trail ', '$HOME ${PATH} %h %n \\n \\x41 \\$',
          '\\', '"', "'", '#start', 'x\\"y', '\\\\', 'trail\\', 'semi;colon # not a comment']
open(sys.argv[2] + "/f.env", "w").write(
    "".join(displayctl.systemd_env_line(f"T{i}", t) for i, t in enumerate(tokens)))
json.dump(tokens, open(sys.argv[2] + "/tokens.json", "w"))
PY
docker cp "$WORK/f.env" "$NAME:/f.env"
docker exec "$NAME" systemd-run --wait --pipe --quiet -p EnvironmentFile=/f.env \
    /usr/bin/env -0 > "$WORK/env.bin"
python3 - "$WORK" <<'PY'
import json, sys
tokens = json.load(open(sys.argv[1] + "/tokens.json"))
env = dict(item.split("=", 1) for item in open(sys.argv[1] + "/env.bin", "rb").read().decode().split("\0") if "=" in item)
bad = [(i, t, env.get(f"T{i}")) for i, t in enumerate(tokens) if env.get(f"T{i}") != t]
if bad:
    raise SystemExit(f"systemd altered EnvironmentFile values: {bad}")
print(f"systemd EnvironmentFile round trip passed ({len(tokens)} values)")
PY

# The rendered bridge unit must verify cleanly under the container's systemd.
sed -e 's|@VESSELSTACK_USER@|vesselstack|g' -e 's|@VESSELSTACK_ROOT@|/opt/vesselstack|g' \
    "$REPO_ROOT/systemd/vesselstack-displays-bridge.service.in" > "$WORK/vesselstack-displays-bridge.service"
docker exec "$NAME" useradd --system --no-create-home vesselstack
docker exec "$NAME" mkdir -p /opt/vesselstack/displays/.venv/bin /opt/vesselstack/config
docker exec "$NAME" sh -c 'touch /opt/vesselstack/displays/bridge.py /opt/vesselstack/config/displays-bridge.env /opt/vesselstack/displays/.venv/bin/python && chmod 0755 /opt/vesselstack/displays/.venv/bin/python'
docker cp "$WORK/vesselstack-displays-bridge.service" "$NAME:/etc/systemd/system/vesselstack-displays-bridge.service"
verify_out=$(docker exec "$NAME" systemd-analyze verify /etc/systemd/system/vesselstack-displays-bridge.service 2>&1) || {
    echo "$verify_out" >&2
    echo "systemd-analyze verify rejected the rendered bridge unit" >&2
    exit 1
}
if [ -n "$verify_out" ]; then
    echo "$verify_out" >&2
    echo "systemd-analyze verify reported warnings for the rendered bridge unit" >&2
    exit 1
fi
echo "systemd-analyze verify passed for the rendered bridge unit"
