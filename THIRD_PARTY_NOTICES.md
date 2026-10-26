# Third-Party Software

VesselStack orchestrates and integrates third-party software but does not
relicense those projects. Each component remains subject to its own license.

Key runtime components include Home Assistant, SignalK, InfluxDB, Grafana,
Prometheus, Eclipse Mosquitto, Heimdall, Docker, Python, sqlite-vec, and, when
the opt-in displays bridge is enabled, paho-mqtt (Eclipse Public License 2.0 /
Eclipse Distribution License 1.0) and websockets (BSD-3-Clause). Image and Python
dependency versions are declared in `templates/compose.yml`,
`boat-chat/requirements.txt`, and `displays/requirements.txt`.

Before distributing a release, review the licenses and notices shipped by the
exact dependency versions and container images. Preserve all upstream license,
copyright, and attribution notices.

VesselStack's Apache-2.0 license applies only to original VesselStack material;
it does not override or replace any upstream license.

## Bundled fonts

`displays/esphome/fonts/` contains the static Regular and Bold instances of
Roboto Condensed, Copyright 2011 The Roboto Project Authors
(<https://github.com/googlefonts/roboto-classic>), distributed by Google Fonts
(<https://github.com/google/fonts/tree/main/ofl/robotocondensed>) under the SIL
Open Font License, Version 1.1. The license text is kept next to the fonts in
`displays/esphome/fonts/OFL.txt`. The generated display firmware embeds glyph
bitmaps rendered from these fonts; the firmware is built with ESPHome (GPLv3 and
MIT components) and LVGL (MIT), which are downloaded at build time and not
distributed by VesselStack.

## Bundled icon font

`displays/esphome/fonts/MaterialSymbols-subset.ttf` is derived from Material
Symbols (Outlined), Copyright Google LLC, from the `google/material-design-icons`
repository (<https://github.com/google/material-design-icons>), commit
`10ecc14790612ca8fd164e3f4ffe9c16fc50d5b7`
(`variablefont/MaterialSymbolsOutlined[FILL,GRAD,opsz,wght].ttf`), licensed under
the Apache License, Version 2.0. The license text is kept next to the font in
`displays/esphome/fonts/LICENSE-MaterialSymbols.txt`. Modifications: one static
instance was created from the variable font (FILL 1, wght 500, GRAD 0, opsz 48) and
subset to the icons listed in `displays/esphome/fonts/icons.json` (layout features
and hinting removed). `scripts/build-icon-font.py` reproduces the file from the
upstream font.
