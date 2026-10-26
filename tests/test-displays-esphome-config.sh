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

def status(cfg):
    inst = cfg["instruments"]
    inst["depth"]["thresholds"] = {"alarm_below": 4, "warn_below": 8, "hysteresis": 0.5}
    inst["house_v"]["thresholds"] = {"alarm_below": 11.8, "warn_below": 12.2, "warn_above": 14.8,
                                     "alarm_above": 15.2}
    inst["sog"]["label"] = HOSTILE[:16]
    cfg["notifications"] = {
        "anchor": {"path": "notifications.navigation.anchor", "label": "Anchor"},
        "batt_low": {"path": "notifications.electrical.batteries.house.voltage", "label": "<% 1+1 %>",
                     "absent": "unknown"},
        "bilge": {"path": "notifications.bilge.main", "label": "${y} Bilge"},
    }
    cfg["displays"]["helm"]["pages"] = [
        {"title": "Few", "layout": "status-grid", "items": ["anchor", "depth", "batt_low"]},
        {"title": "Many", "layout": "status-grid",
         "items": ["anchor", "sog", "depth", "batt_low", "house_v", "awa", "bilge"]},
        {"title": "Nav", "layout": "grid-2x2", "items": ["sog", "depth", "awa", "house_v"]},
    ]

def alarm(cfg):
    status(cfg)
    cfg["notifications"]["bilge"]["label"] = "${y} Bilge"
    cfg["instruments"]["sog"]["label"] = HOSTILE[:16]
    cfg["displays"]["helm"]["ui"] = {"alarm_takeover": True, "alarm_page": 1}

def ui_full(cfg):
    alarm(cfg)
    cfg["notifications"]["bilge"]["label"] = "Bilge"
    cfg["displays"]["helm"]["pages"][0]["title"] = '$x ${y} "q"'
    cfg["displays"]["helm"]["pages"].append({"title": "Tanks", "layout": "single", "items": ["house_v"]})
    cfg["displays"]["helm"]["ui"].update(swipe=True, header=True, night="schedule", night_brightness=15,
                                         dim_after_s=120)
    cfg["night_schedule"] = {"start": "21:30", "end": "06:15"}

def ui_manual(cfg):
    thresholds(cfg)
    cfg["displays"]["helm"]["ui"] = {"night": "manual", "day_brightness": 80}

def ui_single(cfg):
    cfg["displays"]["helm"]["pages"] = [{"title": "One", "layout": "single", "items": ["sog"]}]
    cfg["displays"]["helm"]["ui"] = {"night": "manual", "dim_after_s": 30, "header": True, "swipe": True}

def gauges(cfg):
    tank = {"convert": "ratio->%", "unit": "%", "precision": 0, "stale_after_s": 60, "min": 0, "max": 100}
    inst = cfg["instruments"]
    inst["fuel"] = dict(tank, path="tanks.fuel.0.currentLevel", label=HOSTILE[:16], unit="${y}",
                        thresholds={"alarm_below": 10, "warn_below": 25, "hysteresis": 2})
    inst["water"] = dict(tank, path="tanks.freshWater.0.currentLevel", label="<% 1+1 %>",
                         thresholds={"warn_below": 20})
    inst["waste"] = dict(tank, path="tanks.blackWater.0.currentLevel", label="Waste",
                         thresholds={"warn_above": 75, "alarm_above": 90, "hysteresis": 2})
    inst["lpg"] = dict(tank, path="tanks.lpg.0.currentLevel", label="LPG")
    cfg["displays"]["helm"]["pages"] = [
        {"title": "Tanks", "layout": "tank-bars", "items": ["fuel", "water", "waste", "lpg"]},
        {"title": "Fuel", "layout": "gauge-arc", "items": ["fuel"]},
        {"title": "Nav", "layout": "grid-2x2", "items": ["sog", "depth", "awa", "fuel"]},
    ]

def gauges_ui(cfg):
    gauges(cfg)
    cfg["displays"]["helm"]["pages"][0]["title"] = '$x ${y} "q"'
    cfg["displays"]["helm"]["ui"] = {"swipe": True, "header": True, "night": "schedule", "night_brightness": 15,
                                     "dim_after_s": 120, "alarm_takeover": True}
    cfg["night_schedule"] = {"start": "21:30", "end": "06:15"}

def gauges_one(cfg):
    gauges(cfg)
    cfg["displays"]["helm"]["pages"] = [{"title": "Tank", "layout": "tank-bars", "items": ["lpg"]}]

def electrical(cfg):
    inst = cfg["instruments"]
    inst["batt_v"] = {"path": "electrical.batteries.house.voltage", "label": HOSTILE[:16], "convert": "none",
                      "unit": "${y}", "precision": 2, "stale_after_s": 30,
                      "thresholds": {"alarm_below": 11.8, "warn_below": 12.2, "warn_above": 14.8, "hysteresis": 0.05}}
    inst["batt_soc"] = {"path": "electrical.batteries.house.capacity.stateOfCharge", "label": "<% 1+1 %>",
                        "convert": "ratio->%", "unit": "%", "precision": 0, "stale_after_s": 60, "min": 0, "max": 100,
                        "thresholds": {"alarm_below": 20, "warn_below": 30, "hysteresis": 2}}
    inst["batt_a"] = {"path": "electrical.batteries.house.current", "label": "${y} Amps", "convert": "negate",
                      "unit": "A", "precision": 1, "stale_after_s": 30}
    inst["shore_a"] = {"path": "electrical.shore.current", "label": "$x Shore", "convert": "none", "unit": "A",
                       "precision": 1, "stale_after_s": 30}
    inst["solar_a"] = {"path": "electrical.solar.main.current", "label": "Solar", "convert": "none", "unit": "A",
                       "precision": 1, "stale_after_s": 30}
    cfg["notifications"] = {"shore": {"path": "notifications.electrical.shorePower", "label": "${y} Shore",
                                      "absent": "unknown"}}
    cfg["displays"]["helm"]["pages"] = [
        {"title": "Power", "layout": "electrical", "items": ["batt_v", "batt_soc", "batt_a"]},
        {"title": "Shore", "layout": "electrical", "items": ["batt_v", "batt_soc", "batt_a", "shore_a"]},
        {"title": "Full", "layout": "electrical", "items": ["batt_v", "batt_soc", "batt_a", "shore", "solar_a"]},
        {"title": "Nav", "layout": "grid-2x2", "items": ["sog", "depth", "awa", "batt_v"]},
    ]

def electrical_ui(cfg):
    electrical(cfg)
    cfg["displays"]["helm"]["pages"][0]["title"] = '$x ${y} "q"'
    cfg["displays"]["helm"]["ui"] = {"swipe": True, "header": True, "night": "schedule", "night_brightness": 15,
                                     "dim_after_s": 120, "alarm_takeover": True}
    cfg["night_schedule"] = {"start": "21:30", "end": "06:15"}

def electrical_min(cfg):
    electrical(cfg)
    cfg["displays"]["helm"]["pages"] = cfg["displays"]["helm"]["pages"][:1]

def ha_items(cfg):
    electrical(cfg)
    cfg["notifications"] = {"anchor": {"path": "notifications.navigation.anchor", "label": "Anchor"}}
    cfg["ha"] = {
        "bilge_pump": {"entity_id": "binary_sensor.vesselstack_bilge_pump", "label": "<% 1+1 %>", "kind": "binary",
                       "states": [{"match": "on", "sev": "warn", "text": "$x ${y}"},
                                  {"match": "off", "sev": "ok", "text": "OFF"}]},
        "engine": {"entity_id": "sensor.engine_alarm_status", "label": "${y} Engine", "kind": "state",
                   "states": [{"match": "all clear", "sev": "ok", "text": "CLEAR"},
                              {"prefix": "dock:", "sev": "ok", "text": "<% 2 %>"}],
                   "default": {"sev": "alarm", "text": "ALARM"}},
        "cabin_t": {"entity_id": "sensor.cabin_temperature", "label": "Cabin", "kind": "numeric", "unit": "${y}",
                    "precision": 1, "thresholds": {"warn_above": 30, "alarm_above": 40}, "min": 0, "max": 50},
        "shore_ha": {"entity_id": "binary_sensor.vesselstack_shore_power", "label": "$x Shore", "kind": "binary",
                     "alarm": False, "states": [{"match": "on", "sev": "ok", "text": "ON"},
                                                {"match": "off", "sev": "ok", "text": "OFF"}]},
    }
    cfg["displays"]["helm"]["pages"] = [
        {"title": "Status", "layout": "status-grid",
         "items": ["anchor", "bilge_pump", "engine", "cabin_t", "batt_v"]},
        {"title": "Power", "layout": "electrical", "items": ["batt_v", "batt_soc", "batt_a", "shore_ha", "solar_a"]},
        {"title": "Temp", "layout": "gauge-arc", "items": ["cabin_t"]},
    ]
    cfg["displays"]["helm"]["ui"] = {"swipe": True, "header": True, "night": "schedule", "alarm_takeover": True}
    cfg["night_schedule"] = {"start": "21:30", "end": "06:15"}

def dashboard_icons(cfg):
    # The dashboard example with icons on instruments, notifications and Home Assistant items, in every layout.
    cfg.clear()
    cfg.update(json.loads((repo / "displays" / "displays.dashboard.example.json").read_text()))
    assert any("icon" in item for item in cfg["instruments"].values())

variant("example")
variant("icons", dashboard_icons)
variant("ha", ha_items)
variant("electrical", electrical)
variant("electricalui", electrical_ui)
variant("electricalmin", electrical_min)
variant("gauges", gauges)
variant("gaugesui", gauges_ui)
variant("gaugesone", gauges_one)
variant("thresholds", thresholds)
variant("status", status)
variant("alarm", alarm)
variant("ui", ui_full)
variant("uimanual", ui_manual)
variant("uisingle", ui_single)
variant("complex", complex_, WIFI)
PY

fail=0
check() {
    # stdout (the dumped configuration the content assertions grep) and stderr (ESPHome's INFO and
    # WARNING lines) go to separate files: through one pipe the two streams interleave at buffer
    # boundaries, and a status line can land in the middle of a dumped line and split it.
    local variant=$1 display=$2 dir="$WORK/$1/build/displays/$2/project"
    local out="$WORK/$variant-$display.log" err="$WORK/$variant-$display.err"
    echo "== esphome config: $variant/$display"
    if ! docker run --rm "${CONTAINER_FLAGS[@]}" --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$dir":/config "$IMAGE" \
            config --show-secrets /config/"$display".yaml > "$out" 2> "$err"; then
        sed -n '1,60p' "$err" "$out" >&2
        fail=1
        return
    fi
    grep -q 'Configuration is valid' "$err" "$out" || { cat "$err" "$out" >&2; fail=1; }
    # Anything ESPHome warns about the generated project (except two known upstream notices) is a bug.
    if grep -hE '^WARNING' "$err" "$out" | grep -vE "transparency_key|'ota' password wastes"; then
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
        config /config/helm.yaml > "$WORK/flash-flags.log" 2> "$WORK/flash-flags.err" \
        || ! grep -q 'Configuration is valid' "$WORK/flash-flags.err" "$WORK/flash-flags.log"; then
    cat "$WORK/flash-flags.err" "$WORK/flash-flags.log" >&2
    echo "config failed with the flash container flags" >&2
    fail=1
fi
check complex helm
check complex aux
check thresholds helm
check status helm
check alarm helm
check ui helm
check uimanual helm
check uisingle helm
check gauges helm
check gaugesui helm
check gaugesone helm
check electrical helm
check electricalui helm
check electricalmin helm
check ha helm
check icons helm
# Icons: the Material Symbols subset font at the two sizes, glyphs as private-use characters, static labels
# repainted by the night palette; configurations without icons carry none of it.
log="$WORK/icons-helm.log"
for needle in 'path: /config/fonts/MaterialSymbols-subset.ttf' 'id: font_icons' 'text_font: font_icons' \
        'id: p1_anchor_icon' 'lv_obj_set_style_text_color(id(p1_anchor_icon)' \
        'lv_obj_set_style_text_color(id(p2_fuel_icon)' 'lv_obj_set_style_text_color(id(p4_batt_soc_icon)'; do
    grep -qF "$needle" "$log" || { echo "missing from the validated icons config: $needle" >&2; fail=1; }
done
python3 -I - "$log" <<'PY' || fail=1
# The dump lists the glyphs of a font as "- <chars>" and every icon label as "text: <char>": each label's
# character must be one of the icon font's glyphs, and all of them are private-use characters.
import re, sys
lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
glyphs, labels = set(), set()
for i, line in enumerate(lines):
    if re.match(r"\s+id: font_icons\w*$", line):
        j = i + 1
        while lines[j].strip() != "glyphs:":
            j += 1
        j += 1
        while lines[j].startswith("      - "):
            glyphs.update(lines[j][8:])
            j += 1
    if re.match(r"\s+id: p\d+_\w+_icon$", line):
        match = re.match(r"\s+text: (.)$", lines[i + 1])
        assert match, "icon label text is not a single character: %r" % lines[i + 1]
        labels.add(match.group(1))
assert glyphs and labels, "no icon glyphs or icon labels in the validated config"
assert all("\ue000" <= ch <= "\uf8ff" for ch in glyphs | labels), "icon characters are not private-use"
assert labels == glyphs, "icon font glyphs %r differ from the labels %r" % (sorted(glyphs), sorted(labels))
print("icon fonts: %d glyphs, all used by icon labels" % len(glyphs))
PY
for plain in example status alarm ui electrical ha; do
    if grep -qE 'MaterialSymbols|font_icons|_icon\b' "$WORK/$plain-helm.log"; then echo "icon code present without icons ($plain)" >&2; fail=1; fi
done
# Home Assistant items: a compile-time text per index k, literal user text, the connection dot, a numeric arc.
log="$WORK/ha-helm.log"
for needle in 'topic: vesselstack/v1/ha/bilge_pump' 'topic: vesselstack/v1/ha/cabin_t' 'ins_bilge_pump_k' \
        'topic: vesselstack/v1/bridge/homeassistant' 'id(ha_state)' 'id: p0_dot_ha' \
        'lv_obj_set_style_bg_color(id(p0_bilge_pump_tile)' 'lv_obj_set_style_bg_color(id(p1_shore_ha_tile)' \
        'lv_arc_set_value(id(p2_arc)'; do
    grep -qF "$needle" "$log" || { echo "missing from the validated ha config: $needle" >&2; fail=1; }
done
# shellcheck disable=SC2016
for needle in '$x ${y}' '<% 1+1 %>' '${y} Engine' '<% 2 %>'; do
    grep -qF "$needle" "$log" || { echo "hostile Home Assistant text was altered: $needle" >&2; fail=1; }
done
for word in OFF CLEAR ALARM; do
    grep -qE "text: [\"']?${word}[\"']?\$" "$log" || { echo "Home Assistant text $word missing" >&2; fail=1; }
done
for plain in example status alarm ui electrical; do
    if grep -qE 'bridge/homeassistant|ha_state|vesselstack/v1/ha/' "$WORK/$plain-helm.log"; then echo "Home Assistant code present without ha items ($plain)" >&2; fail=1; fi
done
# Electrical page: SoC arc, fixed CHG / DSG and pill words as literals, guarded pill colours, literal user text.
for elec in electrical electricalui electricalmin; do
    log="$WORK/$elec-helm.log"
    for needle in 'lv_arc_set_value(id(p0_arc)' 'id: p0_batt_a_word' 'ins_batt_a_num' 'ins_batt_soc_gdrawn' \
            'lv_obj_set_style_arc_color(id(p0_arc), lv_color_hex(c), LV_PART_INDICATOR)'; do
        grep -qF "$needle" "$log" || { echo "missing from the validated $elec config: $needle" >&2; fail=1; }
    done
    for word in CHG DSG; do
        grep -qE "text: [\"']?${word}[\"']?\$" "$log" || { echo "current word $word missing ($elec)" >&2; fail=1; }
    done
    # shellcheck disable=SC2016
    grep -qF '${y}' "$log" || { echo "hostile electrical label or unit was altered ($elec)" >&2; fail=1; }
    grep -qF '<% 1+1 %>' "$log" || { echo "hostile electrical label was altered ($elec)" >&2; fail=1; }
done
for elec in electrical electricalui; do
    log="$WORK/$elec-helm.log"
    for needle in 'ins_shore_a_sdrawn' 'lv_obj_set_style_bg_color(id(p1_shore_a_tile)' \
            'lv_obj_set_style_bg_color(id(p2_shore_tile)' 'id: p2_solar_a_value'; do
        grep -qF "$needle" "$log" || { echo "missing from the validated $elec config: $needle" >&2; fail=1; }
    done
    grep -qE "text: [\"']?ON[\"']?\$" "$log" || { echo "shore word ON missing ($elec)" >&2; fail=1; }
done
grep -qF 'lv_obj_set_style_arc_color(id(p0_zone0)' "$WORK/electricalui-helm.log" \
    || { echo "night recolouring of the SoC zone arcs missing" >&2; fail=1; }
if grep -qE 'ins_shore_a_sdrawn|p0_batt_a_word' "$WORK/example-helm.log"; then echo "electrical code present without an electrical page" >&2; fail=1; fi
# Bars and arcs: LVGL widgets, numeric levels from the payload, guarded colours, literal user text.
for gauge in gauges gaugesui; do
    log="$WORK/$gauge-helm.log"
    for needle in 'lv_bar_set_value(id(p0_fuel_bar)' 'lv_arc_set_value(id(p1_arc)' \
            'lv_obj_set_style_bg_color(id(p0_fuel_bar), lv_color_hex(c), LV_PART_INDICATOR)' \
            'lv_obj_set_style_arc_color(id(p1_arc), lv_color_hex(c), LV_PART_INDICATOR)' 'ins_fuel_gdrawn'; do
        grep -qF "$needle" "$log" || { echo "missing from the validated $gauge config: $needle" >&2; fail=1; }
    done
    # shellcheck disable=SC2016
    grep -qF '${y}' "$log" || { echo "hostile gauge label or unit was altered ($gauge)" >&2; fail=1; }
    grep -qF '<% 1+1 %>' "$log" || { echo "hostile gauge label was altered ($gauge)" >&2; fail=1; }
done
grep -qF 'lv_obj_set_style_arc_color(id(p1_zone0)' "$WORK/gaugesui-helm.log" \
    || { echo "night recolouring of the zone arcs missing" >&2; fail=1; }
for plain in example thresholds status alarm ui uimanual uisingle complex; do
    if grep -qE 'lv_bar_set_value|lv_arc_set_value' "$WORK/$plain-helm.log"; then echo "bar or arc code present without a tank-bars or gauge-arc page ($plain)" >&2; fail=1; fi
done
for word in ALARM WARN OK; do
    grep -qE "text: [\"']?${word}[\"']?\$" "$WORK/status-helm.log" || { echo "status word $word missing from the validated config" >&2; fail=1; }
done
grep -q 'lv_obj_set_style_bg_color(id(p1_anchor_tile)' "$WORK/status-helm.log" \
    || { echo "notification colour lambda missing from the validated status config" >&2; fail=1; }
# shellcheck disable=SC2016
grep -qF '${y} Bilge' "$WORK/status-helm.log" || { echo "hostile notification label was altered" >&2; fail=1; }
# The takeover banner: top layer object, summary subscription, backlight, fixed word, literal names.
log="$WORK/alarm-helm.log"
grep -q 'id(alarm_banner)' "$log" || { echo "alarm banner missing from the validated alarm config" >&2; fail=1; }
grep -q 'vesselstack/v1/alarms/summary' "$log" || { echo "alarm summary subscription missing" >&2; fail=1; }
grep -q 'light.turn_on\|board_backlight' "$log" || { echo "backlight action missing from the alarm config" >&2; fail=1; }
# shellcheck disable=SC2016
grep -qF '${y} Bilge' "$log" || { echo "hostile label was altered in the alarm banner" >&2; fail=1; }
for plain in example thresholds status complex; do
    if grep -q 'alarm_banner' "$WORK/$plain-helm.log"; then echo "alarm banner present without ui.alarm_takeover ($plain)" >&2; fail=1; fi
done
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
