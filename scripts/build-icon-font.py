#!/usr/bin/env python3
"""Rebuild the bundled display icon font (not run in CI, not needed at install time).

Instantiates one static instance of the upstream Material Symbols Outlined variable font
(FILL 1, wght 500, GRAD 0, opsz 48), subsets it to the icons listed in ICONS, and writes
displays/esphome/fonts/MaterialSymbols-subset.ttf and displays/esphome/fonts/icons.json
(name -> codepoint, hex without prefix).

Needs fontTools in a throw-away virtual environment (the repository does not depend on it):
  python3 -m venv /tmp/iconvenv && /tmp/iconvenv/bin/pip install fonttools
  /tmp/iconvenv/bin/python -I scripts/build-icon-font.py UPSTREAM.ttf UPSTREAM.codepoints

Get the two upstream files from the pinned commit named in THIRD_PARTY_NOTICES.md
(variablefont/MaterialSymbolsOutlined[FILL,GRAD,opsz,wght].ttf and the matching .codepoints).
Treat them as untrusted downloads and keep them outside the repository.
"""
import json
import sys
from pathlib import Path

ICONS = (
    "air", "anchor", "battery_alert", "battery_charging_full", "battery_full", "bolt",
    "check_circle", "directions_boat", "electrical_services", "engineering", "error", "explore",
    "home", "local_gas_station", "location_on", "navigation", "notifications", "oil_barrel",
    "power", "sailing", "sensors", "settings", "solar_power", "speed", "thermostat", "warning",
    "water", "water_drop", "wifi",
)
AXES = {"FILL": 1, "wght": 500, "GRAD": 0, "opsz": 48}
OUT_DIR = Path(__file__).resolve().parent.parent / "displays" / "esphome" / "fonts"


def main(argv):
    if len(argv) != 3:
        raise SystemExit(__doc__)
    from fontTools import subset
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer

    names = {}
    for line in Path(argv[2]).read_text().splitlines():
        parts = line.split()
        if len(parts) == 2:
            names[parts[0]] = parts[1]
    missing = [name for name in ICONS if name not in names]
    if missing:
        raise SystemExit(f"not in the upstream codepoints file: {', '.join(missing)}")
    icons = {name: names[name] for name in sorted(ICONS)}
    font = instancer.instantiateVariableFont(TTFont(argv[1]), AXES)
    options = subset.Options()
    options.layout_features = []          # icons are drawn by codepoint; no ligatures needed
    options.hinting = False
    options.notdef_outline = False
    options.name_IDs = [0, 1, 2, 3, 4, 5, 6, 13, 14]   # keep the copyright and license names
    options.glyph_names = False
    subsetter = subset.Subsetter(options)
    subsetter.populate(unicodes=[int(code, 16) for code in icons.values()])
    subsetter.subset(font)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    font.save(OUT_DIR / "MaterialSymbols-subset.ttf")
    (OUT_DIR / "icons.json").write_text(json.dumps(icons, indent=2) + "\n")
    print(f"{len(icons)} icons, {(OUT_DIR / 'MaterialSymbols-subset.ttf').stat().st_size} bytes")


if __name__ == "__main__":
    main(sys.argv)
