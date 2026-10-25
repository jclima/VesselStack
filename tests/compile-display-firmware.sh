#!/bin/bash
# Optional (not in CI): renders the example display with placeholder Wi-Fi values and
# runs a full "esphome compile" with the pinned image. The first run downloads the
# ESP-IDF toolchain (about 2 GB) into CACHE_DIR; the build adds about 600 MB.
# Usage: tests/compile-display-firmware.sh [CACHE_DIR]   (default: a Docker volume)

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
IMAGE=$(tr -d '[:space:]' < "$REPO_ROOT/displays/esphome/IMAGE")
CACHE=${1:-vesselstack-esphome-cache}
WORK=$(mktemp -d)
cleanup() {
    # The container runs as root; hand the build tree back so it can be removed.
    docker run --rm --entrypoint chown -v "$WORK":/work "$IMAGE" -R "$(id -u):$(id -g)" /work >/dev/null 2>&1 || true
    rm -rf -- "$WORK"
}
trap cleanup EXIT

python3 -I - "$REPO_ROOT" "$WORK" <<'PY'
import json, shutil, sys
from pathlib import Path
repo, work = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(repo / "displays"))
import lifecycle, render
(work / "config").mkdir()
shutil.copy(repo / "displays" / "displays.example.json", work / "config" / "displays.json")
(work / "config" / "displays.secrets.json").write_text(json.dumps(
    {"schema": 1, "bridge": {}, "displays": {"helm": {"mqtt_password": "compile-test"}}}))
ctx = lifecycle.Context(root=work, require_root=False, out=lambda text: None)
render.render(ctx, "helm", allow_missing_wifi=True, warn=lambda text: None)
PY

start=$(date +%s)
docker run --rm -v "$WORK/build/displays/helm/project":/config -v "$CACHE":/cache "$IMAGE" compile /config/helm.yaml
echo "compile finished in $(( $(date +%s) - start )) s"
ls -l "$WORK/build/displays/helm/project/.esphome/build/vs-helm/build/"*.bin 2>/dev/null || true
