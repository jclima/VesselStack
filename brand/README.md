# VesselStack brand

Canonical offline brand assets for the VesselStack owner-operated chart-room
identity:

- `icon.svg` — canonical Rising Decks icon: a navy rounded square with a wide
  white hull below two ascending teal decks.
- `wordmark.svg` — icon and VesselStack wordmark.
- `github-banner.svg` — 1280×640 social/banner artwork.
- `github-banner.png` — rendered 1280×640 upload-ready banner.
- `icon.png` — rendered 1024×1024 upload-ready icon.

Palette: canvas `#f4f8fa`, panels `#ffffff`/`#eaf2f6`, navy `#0a2341`,
secondary ink `#344c61`/`#526778`, lines `#d4e0e8`/`#778b9b`, teal `#006d89`,
success `#1e6f45`, warning `#8a6218`, and danger `#b3382e`.

Typography uses Archivo for headings, Public Sans for body text, and IBM Plex
Mono for data when those fonts are installed; every surface has system fallbacks
and ships without external font requests for offline use.

These are source and rendered assets for documentation and review. Uploading the
GitHub banner remains a manual GitHub setting change outside this asset inventory.

## Writing for VesselStack

1. Write to the boat owner as "you". Use plain words and short sentences, one idea each. Use the imperative for steps. Keep paragraphs to about five sentences.
2. Put the default and the recommendation first. Optional features say "off by default" and name the setting or command that turns them on.
3. Describe a feature or change by what it does, then **Operational impact**, **Verify** and **Rollback**.
4. Be honest about safety. Quote the canonical statement, "VesselStack is not certified navigation, collision-avoidance, engine-control, alarm, or life-safety equipment. Maintain independent marine instruments and alarms. Never depend on this software as the sole source of safety data.", and use it as written. Say plainly what is unverified. Never drop a warning, limit, rollback or command without linking to where it now lives.
5. Spell names exactly: VesselStack, Signal K (two words; `signalk` only in code, paths and package names), Control Panel, Boat Chat, Home Assistant, Node-RED, Tailscale, InfluxDB, Grafana, Prometheus, Mosquitto, Homepage, ESPHome, NMEA 2000, NMEA 0183, KIP, Freeboard-SK.
6. Put `vesselstackctl` and every command, path, setting and value in code format. Commands that touch the system start with `sudo`.
7. Write "pre-release" in prose; `prerelease` only inside code, flags or API names. Write "Raspberry Pi" on first use, then "the Pi", and "boat computer" when the host need not be a Pi.
8. Use SI units with a space: 12 V, 5 A, 64 MB, 10 s. Configuration values keep their code spelling.
9. Write placeholders in UPPER_CASE. Make example values fictional: the documentation address ranges 192.0.2.x, 198.51.100.x and 203.0.113.x, `raspberrypi.local`, and a display called `helm`. Never use a real MMSI, call sign, position, host name, token, serial number or log excerpt.
10. Give each topic one canonical home and link to it from other pages. Release history belongs in `CHANGELOG.md`.
11. Show the banner only at the top of the main README, with the alt text "VesselStack banner". Use local image files with relative paths and make no external image or font requests. The palette and typography are for interfaces and images, not for styling Markdown. Do not recolour or restyle the assets.
12. Use GitHub callouts sparingly: NOTE for status, WARNING for safety. Prefer commas, colons or full stops to em dashes in new text.
