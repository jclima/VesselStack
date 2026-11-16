# Third-Party Software

VesselStack orchestrates and integrates third-party software but does not
relicense those projects. Each component remains subject to its own license.

Key runtime components include Home Assistant, Signal K, InfluxDB, Grafana,
Prometheus, Eclipse Mosquitto, Homepage (gethomepage.dev, GPL-3.0; separately distributed container image, not bundled), Docker, Python, sqlite-vec, and, when
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

## signalk-vesseltwin

Managed Signal K installs may preload `signalk-vesseltwin` 0.2.3 from npm using
an immutable SHA-512 pin. It is an optional third-party plugin; upstream
license and notices remain authoritative. See the [upstream repository](https://github.com/jclima/signalk-vesseltwin)
and its Apache-2.0 license. The pinned package declares no runtime dependencies.

## QR Code generator library (Python)

`common/qrcodegen.py` is Project Nayuki's QR Code generator library for Python,
version 1.8.0, vendored unmodified with its MIT license header intact
(Copyright (c) Project Nayuki, <https://www.nayuki.io/page/qr-code-generator-library>).
It renders the pairing QR codes printed by `vesselstackctl panel-link` and shown in
the Control Panel; it has no dependencies. The license permits copying, modification
and redistribution provided the copyright and permission notice stay with the file.

## Optional images and packages pulled at install time

VesselStack does not include any component in this section in its release archive.
Each is pulled when you enable the matching feature, so its own license and notices
apply. Licenses below are taken from the package metadata on npm or from the upstream
repository's license file; the container images may also carry other software, so read
the notices shipped in the image you run.

| Component | Pinned version | Pinned in | License | How it arrives |
| --- | --- | --- | --- | --- |
| Node-RED (`nodered/node-red` image) | `5.0.9-debian` | `templates/compose.yml` | Apache-2.0 | Docker image, when `NODERED_ENABLE=true` |
| `@flowfuse/node-red-dashboard` | 1.36.1 | `scripts/configure-nodered.py` | Apache-2.0 | npm, preinstalled into the Node-RED data folder |
| `node-red-contrib-home-assistant-websocket` | 0.83.0 | `scripts/configure-nodered.py` | MIT | npm, preinstalled into the Node-RED data folder |
| Prometheus (`prom/prometheus` image) | `v2.55.0` | `templates/compose.yml` | Apache-2.0 | Docker image |
| node-exporter (`quay.io/prometheus/node-exporter` image) | `v1.10.2` | `templates/compose.yml` | Apache-2.0 | Docker image |
| AIS-catcher (`ghcr.io/jvde-github/ais-catcher` image) | `v0.70` | `templates/compose.yml` (`AIS_IMAGE`) | GPL-3.0 | Docker image, when `AIS_ENABLE=true` |
| `signalk-to-influxdb2` | 2.3.0 | `scripts/configure-signalk-datapath.py` | Apache-2.0 | npm, into the Signal K data folder |
| `signalk-anchoralarm-plugin` | 2.0.1 | `scripts/configure-signalk-datapath.py` | ISC | npm, marine essentials bundle |
| `@meri-imperiumi/signalk-logbook` | 0.15.1 | `scripts/configure-signalk-datapath.py` | MIT | npm, marine essentials bundle |
| `@meri-imperiumi/signalk-autostate` | 0.6.4 | `scripts/configure-signalk-datapath.py` | MIT | npm, marine essentials bundle |

The versions listed in this file are the release defaults. An owner can set a different version of
a container in the settings file (see "Component versions" in the README); VesselStack does not test
or ship those versions, and their licenses are their authors' own.

KIP, Freeboard-SK and the NMEA 0183 converter (`sk-to-nmea0183`) are not installed by
VesselStack. They ship inside the Signal K server image, which VesselStack configures
(`scripts/configure-signalk-datapath.py`) but does not modify. The Signal K server
itself (image tag `SIGNALK_VERSION`, default `v2.27.0` in `templates/compose.yml`) is pulled
as a Docker image and is distributed under Apache-2.0 by its authors.

## Tailscale

Remote access is off by default. Only if you run `sudo vesselstackctl remote enable`
does VesselStack install the `tailscale` package from Tailscale's own apt repository
(see `scripts/vesselstackctl`). VesselStack does not bundle or redistribute it. The
Tailscale client source repository (<https://github.com/tailscale/tailscale>) declares
BSD-3-Clause; the package you install and the Tailscale service remain subject to
Tailscale's own terms.
