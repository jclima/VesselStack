#!/bin/bash
# Renders ESPHome projects for the example display and for harder variants (several
# pages, both layouts, Home Assistant API, hostile labels and a Wi-Fi password full
# of substitution syntax) and validates each with the pinned ESPHome image:
#   docker run ... <pinned image> config /config/<id>.yaml
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
trap 'rm -rf -- "$WORK"' EXIT

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

variant("example")
variant("complex", complex_, WIFI)
PY

fail=0
check() {
    local variant=$1 display=$2 dir="$WORK/$1/build/displays/$2/project"
    echo "== esphome config: $variant/$display"
    if ! docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$dir":/config "$IMAGE" \
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
check complex helm
check complex aux

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
