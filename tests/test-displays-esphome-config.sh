#!/bin/bash
# Renders ESPHome projects for the example display and for harder variants (several
# pages, both layouts, Home Assistant API, hostile labels and a Wi-Fi password full
# of substitution syntax) and validates each with the pinned ESPHome image:
#   docker run ... <pinned image> config /config/<id>.yaml
# The containers run with the same hardening flags firmware.py uses (CONTAINER_FLAGS), and
# one extra check runs the config with the flash flags (read-only project, tmpfs scratch).
# Needs Docker and network access for the first image pull (about 1.2 GB).

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
IMAGE=$(tr -d '[:space:]' < "$REPO_ROOT/displays/esphome/IMAGE")
case "$IMAGE" in
    ghcr.io/esphome/esphome:*@sha256:*) ;;
    *) echo "displays/esphome/IMAGE must be a digest-pinned reference" >&2; exit 1 ;;
esac
WORK=$(mktemp -d)
FLASH_PROJECT=""
cleanup() {
    # The flash check hands the project to root (see below); take it back so rm can delete it.
    if [ -n "$FLASH_PROJECT" ] && [ "$(id -u)" -ne 0 ]; then
        docker run --rm --entrypoint chown -v "$FLASH_PROJECT":/p "$IMAGE" -R "$(id -u):$(id -g)" /p >/dev/null 2>&1 || true
    fi
    rm -rf -- "$WORK"
}
trap cleanup EXIT

# The flags firmware.py really passes, so a change there is exercised here.
read_flags() {  # portable (no mapfile): one flag per line from firmware.py
    python3 -I -c '
import sys
sys.path.insert(0, sys.argv[1] + "/displays")
import firmware
print("\n".join(getattr(firmware, sys.argv[2])))
' "$REPO_ROOT" "$1"
}
CONTAINER_FLAGS=()
while IFS= read -r flag; do CONTAINER_FLAGS+=("$flag"); done < <(read_flags CONTAINER_FLAGS)
UPLOAD_SCRATCH_FLAGS=()
while IFS= read -r flag; do UPLOAD_SCRATCH_FLAGS+=("$flag"); done < <(read_flags UPLOAD_SCRATCH_FLAGS)
[ "${#CONTAINER_FLAGS[@]}" -ge 4 ] || { echo "could not read CONTAINER_FLAGS from displays/firmware.py" >&2; exit 1; }

# flash relies on 'esphome upload --file'; fail here if an image bump drops it.
if ! docker run --rm "${CONTAINER_FLAGS[@]}" "$IMAGE" upload --help | grep -q -- '--file'; then
    echo "the pinned ESPHome image no longer supports 'esphome upload --file'" >&2
    exit 1
fi

python3 -I - "$REPO_ROOT" "$WORK" <<'PY'
import copy, json, sys
from pathlib import Path
repo, work = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(repo / "displays"))
import lifecycle, render

example = json.loads((repo / "displays" / "displays.example.json").read_text())
HOSTILE = '$x ${y} ${1+1} <% 1+1 %> "q" \\ #: \'s'
WIFI = {"ssid": "ssid $x ${y}", "password": "pa$$w${mqtt_username}<% 1 %>"}

def variant(name, mutate=None, wifi=None):
    cfg = copy.deepcopy(example)
    if mutate:
        mutate(cfg)
    root = work / name
    (root / "config").mkdir(parents=True)
    (root / "config" / "displays.json").write_text(json.dumps(cfg, indent=2))
    secrets = {"schema": 1, "bridge": {}, "displays": {
        d: {"mqtt_password": "mqtt-" + d} for d in cfg["displays"]}}
    if wifi:
        secrets["wifi"] = wifi
    (root / "config" / "displays.secrets.json").write_text(json.dumps(secrets))
    ctx = lifecycle.Context(root=root, require_root=False, out=lambda text: None)
    for display_id in cfg["displays"]:
        render.render(ctx, display_id, allow_missing_wifi=wifi is None, warn=lambda text: None)
    return cfg

def complex_(cfg):
    inst = cfg["instruments"]
    inst["sog"]["label"] = HOSTILE[:16]
    inst["sog"]["unit"] = "$x\\\""
    inst["depth"]["label"] = "<% 1+1 %>"
    inst["depth"]["unit"] = "${y}"
    cfg["displays"]["helm"]["home_assistant_api"] = True
    cfg["displays"]["helm"]["pages"] = [
        {"title": "Nav", "layout": "grid-2x2", "items": ["sog", "depth", "awa"]},
        {"title": "Big", "layout": "single", "items": ["house_v"]},
        {"title": "Two", "layout": "grid-2x2", "items": ["house_v", "sog"]},
    ]
    cfg["displays"]["aux"] = {"name": "Aux", "board": "waveshare-esp32-s3-touch-lcd-2.8c",
                              "pages": [{"title": "One", "layout": "single", "items": ["depth"]}]}

def thresholds(cfg):
    inst = cfg["instruments"]
    inst["depth"]["thresholds"] = {"alarm_below": 4, "warn_below": 8, "hysteresis": 0.5}
    inst["house_v"]["thresholds"] = {"alarm_below": 11.8, "warn_below": 12.2, "warn_above": 14.8,
                                     "alarm_above": 15.2}
    inst["sog"]["label"] = HOSTILE[:16]
    cfg["displays"]["helm"]["pages"] = [
        {"title": "Nav", "layout": "grid-2x2", "items": ["sog", "depth", "awa", "house_v"]},
        {"title": "Big", "layout": "single", "items": ["house_v"]},
    ]

variant("example")
variant("thresholds", thresholds)
variant("complex", complex_, WIFI)
PY

fail=0
check() {
    local variant=$1 display=$2 dir="$WORK/$1/build/displays/$2/project"
    echo "== esphome config: $variant/$display"
    if ! docker run --rm "${CONTAINER_FLAGS[@]}" --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$dir":/config "$IMAGE" \
            config --show-secrets /config/"$display".yaml > "$WORK/$variant-$display.log" 2>&1; then
        sed -n '1,60p' "$WORK/$variant-$display.log" >&2
        fail=1
        return
    fi
    grep -q 'Configuration is valid' "$WORK/$variant-$display.log" || { cat "$WORK/$variant-$display.log" >&2; fail=1; }
    # Anything ESPHome warns about the generated project (except two known upstream notices) is a bug.
    if grep -E '^WARNING' "$WORK/$variant-$display.log" | grep -vE "transparency_key|'ota' password wastes"; then
        echo "unexpected ESPHome warning for $variant/$display" >&2
        fail=1
    fi
}
check example helm
# The flash container's view: project read-only, scratch on tmpfs, same hardening flags.
echo "== esphome config (flash flags): example/helm"
# In production the staged project is owned by root (0700/0600) and the container runs as root
# with --cap-drop ALL, so it reads the files as their owner (no CAP_DAC_OVERRIDE). Mirror that
# ownership here; a project owned by the unprivileged runner uid would be unreadable on Linux.
FLASH_PROJECT="$WORK/example/build/displays/helm/project"
if [ "$(id -u)" -eq 0 ]; then
    chown -R 0:0 "$FLASH_PROJECT"
else  # the chown helper needs CAP_CHOWN, so it runs without the hardening flags
    docker run --rm --entrypoint chown -v "$FLASH_PROJECT":/p "$IMAGE" -R 0:0 /p
fi
if ! docker run --rm "${CONTAINER_FLAGS[@]}" "${UPLOAD_SCRATCH_FLAGS[@]}" \
        -v "$FLASH_PROJECT":/config:ro "$IMAGE" \
        config /config/helm.yaml > "$WORK/flash-flags.log" 2>&1 \
        || ! grep -q 'Configuration is valid' "$WORK/flash-flags.log"; then
    cat "$WORK/flash-flags.log" >&2
    echo "config failed with the flash container flags" >&2
    fail=1
fi
check complex helm
check complex aux
check thresholds helm
grep -q 'lv_obj_set_style_bg_color(id(p1_content)' "$WORK/thresholds-helm.log" \
    || { echo "severity colour lambda missing from the validated thresholds config" >&2; fail=1; }

# User text and the Wi-Fi password must arrive in the validated configuration unchanged.
log="$WORK/complex-helm.log"
# shellcheck disable=SC2016  # the literal text contains $ on purpose
grep -qF 'ssid $x ${y}' "$log" || { echo "Wi-Fi SSID was altered" >&2; fail=1; }
# shellcheck disable=SC2016
grep -qF 'pa$$w${mqtt_username}<% 1 %>' "$log" || { echo "Wi-Fi password was altered" >&2; fail=1; }
grep -qF '<% 1+1 %>' "$log" || { echo "hostile label was altered" >&2; fail=1; }
grep -q '^api:' "$log" || { echo "API package missing" >&2; fail=1; }
if grep -q '^api:' "$WORK/example-helm.log"; then echo "API package present without the option" >&2; fail=1; fi

[ "$fail" -eq 0 ] || exit 1
echo "ESPHome configuration checks passed with $IMAGE"
