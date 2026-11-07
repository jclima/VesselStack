# Instrument displays (preview)

Purpose: reference for the optional ESP32 touch-screen instrument displays: bridge service, device lifecycle, firmware build and flash, layouts, thresholds, MQTT topics, supported boards, and limitations.

[Back to README](../README.md) · [Documentation index](README.md)

VesselStack is gaining configurable live-instrument displays: small ESP32 touch
screens that show Signal K values (speed, depth, wind angle, battery voltage and
so on) on pages you define. The first board is the Waveshare
ESP32-S3-Touch-LCD-2.8C, planned to run ESPHome with LVGL. The board registry
and page layouts are data, so more boards and layouts can be added later.

**Status: preview.** The configuration schema, validation, unit conversions,
the MQTT topic/payload and freshness logic, the board registry, the MQTT
account/ACL provisioning (`displayctl.py sync-mqtt`), the **device lifecycle
commands** (`vesselstackctl display list|add|remove|rotate|wifi|status`), the
**Signal K-to-MQTT bridge service** and the **ESPHome project generator**
(`vesselstackctl display render|export|build|flash`, see [Firmware: generate, build and
flash](#firmware-generate-build-and-flash)) exist today (`displays/`). With
`DISPLAYS_ENABLE=true` the bridge publishes your configured instruments to
Mosquitto. The Waveshare 2.8C has been verified on real hardware: bring-up
(colours, orientation, round safe area and touch coordinates) needed no knob
changes, and the path Signal K, bridge, MQTT, screen worked end to end on a
Raspberry Pi with a firmware built on the Pi and flashed over USB serial. Not
yet verified on hardware: OTA updates, long-term stability and `.local` (mDNS)
name resolution. Building and flashing on the Pi is optional
(`vesselstackctl display build|flash`, see [Building and flashing on the
Pi](#building-and-flashing-on-the-pi)); building on a laptop remains the
primary documented path. The always-on Mosquitto ACL does affect existing installations
(see [MQTT access control](MQTT.md)). `displays/` is copied to
`$VESSELSTACK_ROOT/displays` by the installer.

**Network and offline requirements.** Compose publishes MQTT on loopback by
default (`VESSELSTACK_DATA_BIND_ADDRESS=127.0.0.1`, see [Network
security](NETWORK.md#network-security) and [Reaching VesselStack from other
devices](NETWORK.md#reaching-vesselstack-from-other-devices)), so a display on the boat
LAN can only reach the broker after you publish it on the network. The only
supported ways are the `all` LAN preset (`sudo vesselstackctl lan all --yes`)
or setting `VESSELSTACK_DATA_BIND_ADDRESS` to a trusted LAN IPv4 address and
re-running the installer. Both move MQTT, InfluxDB and Prometheus together
(Prometheus has no login): there is no MQTT-only option yet, so keep the
firewall enabled and the boat network trusted. The default `dashboards`
preset (and `lan local`) sets the address back to `127.0.0.1`, which cuts
displays off again. The broker always
enforces the ACL and each display has its own read-only account. The displays
bridge dependencies (`displays/requirements.txt`, installed with
`--require-hashes` into `$VESSELSTACK_ROOT/displays/.venv`) are fetched from
PyPI and are not part of the offline bundle yet: `install.sh --offline` with
`DISPLAYS_ENABLE=true` is refused, before anything changes, unless that
environment already exists.

## Displays bridge service

`vesselstack-displays-bridge.service` (`displays/bridge.py`) subscribes to the
Signal K websocket stream (`vessels.self` only, only the paths in
`displays.json`), converts and rounds values with the same tested logic as the
topic schema below, and publishes them retained to Mosquitto. It is opt-in and
adds no listening port.

Enable it:

1. Set `DISPLAYS_ENABLE=true` in `/opt/vesselstack/config/vesselstack.env` (or
   the Displays section of the Control Panel). If Signal K security is on, also
   set `DISPLAYS_SIGNALK_TOKEN` to a Signal K access token (read access is
   enough).
2. Run `sudo ./install.sh --config /opt/vesselstack/config/vesselstack.env
   --start` (or Control Panel Apply). On the first run the installer seeds
   `/opt/vesselstack/config/displays.json` (mode 0644, no secrets) with the
   example instruments and an **empty** `displays` map, so no display account is
   created implicitly. It never overwrites an existing `displays.json`, and
   because it only seeds when the file is missing, editing is entirely yours.
3. Edit `displays.json` (see [Configuration and validation](#configuration-and-validation)),
   check it with `python3 /opt/vesselstack/displays/displayctl.py validate
   /opt/vesselstack/config/displays.json`, then re-run the installer. Each
   re-run re-syncs the MQTT accounts, rewrites the bridge environment and
   restarts the bridge if it is running (`--start` also starts it).

What changes on the host: the installer creates a Python virtual environment in
`/opt/vesselstack/displays/.venv` and installs `paho-mqtt==2.1.0` and
`websockets==17.3` from `displays/requirements.txt` (only when
`DISPLAYS_ENABLE=true`) with `pip install --require-hashes`: every release file
(sdist, pure-Python wheel and all Linux x86_64/aarch64/armv7 wheels) is pinned
by sha256, generated from PyPI by `scripts/pin-displays-requirements.py`, so a
tampered or substituted package is refused; review that diff like any other
dependency change when bumping a version. This runs through
`scripts/install-displays-venv.sh` *before* anything is rendered: it needs the
internet (PyPI) only the first time, an offline first install aborts with
"Could not install the displays bridge dependencies" and the installation
untouched, and once the pinned versions are installed a re-run (for example a
Control Panel Apply while offline) needs no network. The installer runs under
`umask 077`, so the helper builds the environment under `umask 022` and then
normalises it to `u=rwX,go=rX`: the environment and the copied `displays/`
sources stay **root-owned** (the service user can run but not modify the code it
executes) and readable by the service user; this also repairs an environment
that an earlier run left `0700` (the symptom was `status=203/EXEC` and a
restart loop). Boat Chat's environment is created as before and owned by the
service user. The installer also writes `config/displays-bridge.env` (`root:root`,
mode 0600; systemd reads it as PID 1 before dropping to the service account, so
the service user never needs read access; it contains the bridge MQTT password
and the optional Signal K token), renders and enables the unit, and starts it with
`--start`. A render-only run (Control Panel Apply) runs `systemctl try-restart`
on the bridge so a running bridge picks up the rewritten environment; a stopped
bridge is only started by `--start` or `vesselstackctl start`. The unit runs as the unprivileged service account with the same
hardening as Boat Chat plus a read-only filesystem view; it only reads
`displays.json`. The bridge MQTT account may only publish
`vesselstack/v1/instruments/#`, `vesselstack/v1/notifications/#`,
`vesselstack/v1/alarms/#`, `vesselstack/v1/ha/#` and `vesselstack/v1/bridge/#`.
With Home Assistant items (`ha` in `displays.json`) the bridge also opens one
websocket to `HOME_ASSISTANT_URL`; see [Home Assistant items](#home-assistant-items).
The environment file then also holds `DISPLAYS_HA_TOKEN` and `HOME_ASSISTANT_URL`
(written only when the token is set). The installer preflight (`--dry-run` too)
warns, without failing, when `displays.json` defines `ha` items but
`DISPLAYS_HA_TOKEN` is empty or `HOME_ASSISTANT_URL` is not a usable
`http(s)://host:port` URL, and rejects a token with a line break or control character.

Behaviour: on start it publishes retained `vesselstack/v1/bridge/status`
`online` (and `offline` on a clean stop; Mosquitto publishes the retained last
will `offline` if the bridge dies), reports `vesselstack/v1/bridge/signalk`
`connected` or `disconnected`, and reconnects to Signal K with exponential
backoff (1 s up to 30 s with jitter). While Signal K is unreachable the clock
keeps running, so instruments turn `stale` after their `stale_after_s`; they
read `missing` until the first value arrives. Treat `bridge/status` as the
authority: if the bridge dies hard, `bridge/signalk` keeps its last retained
value. A single Signal K message the bridge cannot process is logged (at most once a
minute, with a count of the ones suppressed) and dropped; it does not cost the
Signal K connection. The MQTT client queues at most 1000 messages while the
broker is down, bounding memory. An invalid `displays.json` or settings (exit
status 2) or a missing Python dependency (exit status 3) make the bridge exit
with a clear journal message and stay stopped (`RestartPreventExitStatus=2 3`);
fix the problem and run `sudo systemctl restart
vesselstack-displays-bridge` (or `vesselstackctl start`, or the installer with
`--start`). Other failures are retried every 10 seconds.

Verify:

```bash
sudo vesselstackctl status            # "Displays bridge   healthy"
sudo vesselstackctl logs displays     # connects, instrument count; no secrets
# watch the retained state as the MQTT administrator; the password is passed to the
# container through its environment, never on the host command line
sudo bash -c 'source /opt/vesselstack/config/vesselstack.env &&
  export MQTT_USERNAME MQTT_PASSWORD &&
  docker exec -e MQTT_USERNAME -e MQTT_PASSWORD vesselstack-mosquitto \
    sh -c "mosquitto_sub -v -W 5 -u \"\$MQTT_USERNAME\" -P \"\$MQTT_PASSWORD\" -t vesselstack/v1/#"'
```

Troubleshooting:

- `bridge/status` is `offline` or missing: `sudo systemctl status
  vesselstack-displays-bridge` and `vesselstackctl logs displays`. An
  `invalid displays configuration` line names the offending JSON path;
  `MQTT connection refused` means the bridge password in
  `displays-bridge.env` no longer matches the broker: re-run the installer.
  `missing Python dependency` means `displays/.venv` is gone: re-run the
  installer with `DISPLAYS_ENABLE=true` (needs the internet once).
  `status=203/EXEC` means the service user cannot execute
  `displays/.venv/bin/python`; re-run the installer, which repairs the modes.
  The unit stays stopped after exit status 2 or 3 until you restart it.
- `bridge/signalk` is `disconnected`: check `SIGNALK_URL` answers
  (`curl -fsS "$SIGNALK_URL/signalk"`); an HTTP 401 in the log means set
  `DISPLAYS_SIGNALK_TOKEN`.
- Every instrument is `stale` or `missing` while the bridge is online and
  connected: the Signal K `path` in `displays.json` is not being produced, or the
  token cannot read it.
- `bridge/homeassistant` is `disabled` and Home Assistant items show `--`:
  `DISPLAYS_HA_TOKEN` is empty or Home Assistant rejected it; see
  [Home Assistant items](#home-assistant-items).

Stop or remove: `sudo systemctl stop vesselstack-displays-bridge` stops it until
the next `vesselstackctl start`/installer `--start`. Set `DISPLAYS_ENABLE=false`
and run `sudo ./install.sh --config ... --start` to disable and stop the unit,
delete `config/displays-bridge.env`, and drop the bridge and display MQTT
accounts from the ACL (rollback). `displays.json`, `displays.secrets.json` and
`displays/.venv` are left in place; `vesselstackctl uninstall` removes the unit.

Backups: `vesselstackctl backup` includes `config/displays-bridge.env` and
`config/displays.secrets.json`, which contain secrets, like the other `*.env`
files; keep archives private. It excludes `displays/.venv` and `__pycache__`
(rebuilt by the installer).

## Managing displays

The Control Panel **Displays** tab shows each display, a preview of its pages, its firmware state and
readiness and the data list, edits the theme and pages (`set-pages`), restores older settings
(`restore-config`) and sends firmware to the screen (`display deploy`, rollback with `flash --previous`);
see the [Control Panel guide](CONTROL_PANEL.md#displays). Editing is deterministic: restoring a backed-up
`displays.json` renders the same source hash again, which is what makes `flash --previous` safe afterwards.
**Downgrading** to a VesselStack without these themes or pages: run `set-theme ID classic` first and
reflash. The panel runs `deploy` and rollback as an operation that holds the lifecycle slot, so backups,
updates and settings changes wait until it ends.
It runs `display list --json`, `show ID --json`, `themes --json`, `catalog [--live] --json` and
`serial-ports --json`. Editing from the tab is not available yet; use the commands below.

`vesselstackctl display COMMAND ...` (root, uses the installed configuration)
and `python3 displays/displayctl.py COMMAND ...` (with `VESSELSTACK_ROOT` set;
`MQTT_USERNAME`/`MQTT_PASSWORD` from `vesselstack.env` for commands that touch
the broker) are the same tool. `validate`, `sync-mqtt` and `bridge-env` behave
as before and are also reachable through `vesselstackctl display`.

```bash
sudo vesselstackctl display add helm --board waveshare-esp32-s3-touch-lcd-2.8c --name Helm
sudo vesselstackctl display wifi            # prompts for SSID and (hidden) password
sudo vesselstackctl display list
sudo vesselstackctl display status
sudo vesselstackctl display rotate helm
sudo vesselstackctl display remove helm --yes
```

| Command | What it changes |
| --- | --- |
| `list [--json]` | Nothing. Table of id, name, board, page count, MQTT username, whether generated credentials exist (`SECRETS`) and whether Wi-Fi credentials are stored (`WIFI`). Works without root if the files are readable; without root the secrets/Wi-Fi columns show `unknown`. Never prints secret values. `--json` prints only `{"schema": 1, "displays": [{"id", "name", "board", "pages"}]}` (an empty list when `displays.json` does not exist yet), which is what the Control Panel Displays tab uses to find the displays. |
| `add ID --board BOARD [--name NAME] [--home-assistant-api]` | Appends the display to `displays.json` with one default page: title `Main`, the board's largest layout, and the first N instruments in `displays.json` order (N = layout capacity, fewer if you have fewer instruments). It refuses an existing id, an unknown board or a configuration with no instruments, and validates the whole resulting file before writing. It then syncs MQTT like `sync-mqtt`: creates the display password in `displays.secrets.json`, rewrites the ACL/passwords and signals the broker. It prints the MQTT username (`vsdisp-ID`) and next steps, never the password. Edit the pages in `displays.json` afterwards and check with `validate`; then generate the firmware project with `render` or `export` (see [Firmware: generate, build and flash](#firmware-generate-build-and-flash)). |
| `remove ID --yes` | Deletes the display from `displays.json` and its entry from `displays.secrets.json`, then syncs: the broker reload disconnects the connected device and refuses it afterwards. Without `--yes` it refuses. Adding the same id again generates new credentials. The Wi-Fi block and other displays are untouched. It also deletes the display's whole build tree (`build/displays/ID/`: rendered project with `secrets.yaml`, stored firmware with the compiled-in credentials, and old-layout `ID.new`/`ID.prev`; exactly those names, never another display's) and says so; it first waits for a running build or flash of that display (30 s, then fails without changing anything). |
| `rotate ID` | Regenerates every generated secret of that display (the MQTT password, the OTA password and the Home Assistant API encryption key), then syncs. Other displays are untouched. The device is refused until it is **reflashed** (over USB) with the new credentials. |
| `render ID`, `export ID` | Generate the ESPHome project for a display; see [Firmware: generate, build and flash](#firmware-generate-build-and-flash). Root. |
| `build ID` | Optional. Renders, then compiles the firmware on this machine in a one-shot container of the pinned ESPHome image and stores it with a manifest; see [Building and flashing on the Pi](#building-and-flashing-on-the-pi). Root. |
| `flash ID (--serial DEVICE \| --ota HOST) [--previous] [--force]` | Optional. Uploads the stored firmware (never compiles) over USB serial or Wi-Fi; `--previous` rolls back; refuses firmware built with credentials that are no longer current unless `--force`. Root. |
| `themes [--json]` | Nothing. Lists the display themes with a one-line description; `--json` prints the full palettes. `displayctl.py themes` itself needs no root; through `vesselstackctl` run it with `sudo` like the other `display` commands. See [Themes](#themes). |
| `set-theme ID THEME` | Root. Sets `displays.ID.ui.theme` in `displays.json` (validated and written atomically like the other changing commands); `classic` deletes the key instead (and the `ui` object when that leaves it empty), so the file stays readable by older versions. An unknown theme or display is refused with exit status 2 and the valid ids. It takes the per-display build lock first and the configuration lock inside it (see the lock table below), so it waits for a running build or flash of that display (30 s). It changes nothing on the device: it prints the next steps, `display build ID` then `display flash ID ...`. Setting the theme a display already has changes nothing. |
| `catalog [--live] [--json]` | Nothing. The data list a page can pick from: about forty common Signal K values (see [Data catalog](#data-catalog)), each with the right unit for `BOAT_UNITS`, merged with what is already in `displays.json`. `--live` also asks the configured `SIGNALK_URL` once which values really have data. `--json` is what the Control Panel reads. Needs no root if `displays.json` is readable. |
| `show ID [--json]` | Nothing. Everything needed to edit one display: its pages, `ui` and theme, the board geometry, the layouts the board offers (capacity, minimum items, accepted item types, electrical slot roles), a preview box for every item computed by `render.py` (the Control Panel never repeats layout maths), the configuration `revision` (sha256 of `displays.json`) and a `readiness` block (displays enabled, MQTT password and Wi-Fi stored as yes/no, MQTT reachable from the network, Docker, ESPHome image and toolchain cache present, free disk bytes, stored firmware `none`/`current`/`outdated`, last flash). Never prints a secret value. |
| `set-pages ID --if-revision SHA --stdin` | Root. Replaces the pages of a display from a JSON request on standard input (`{"pages": [...], "theme"?, "new_instruments"?, "new_notifications"?, "instrument_edits"?}`) and prints a JSON result. See [Editing pages](#editing-pages-backups-and-restore). |
| `history [--json]` | Root. Lists the `displays.json` backups, newest first. |
| `restore-config STAMP [--if-revision SHA]` | Root. Puts the backup `STAMP` back as `displays.json` (validated; the file it replaces is backed up first) and prints a JSON result. |
| `serial-ports [--json]` | Nothing. The USB serial devices under `/dev/serial/by-id` that `flash --serial` would accept. |
| `wifi [--open] [--clear]` | Reads the SSID and password from the terminal (password hidden) or, when stdin is not a terminal, from two lines on stdin (for automation); they are never accepted as command-line arguments. SSID: 1-32 bytes of printable UTF-8. Password: 8-63 printable ASCII characters; an empty password (open network) is accepted only with `--open`. Stored once for all displays as `"wifi": {"ssid", "password"}` in `displays.secrets.json` (mode 0600, inside `config/`, so **included in `vesselstackctl backup`**; keep archives private). `--clear` removes it and also deletes `secrets.yaml` from every already rendered tree of every display id under `build/displays/` (`ID/project`, `ID/project.new`, `ID/project.prev`, an old-layout `ID/secrets.yaml`, and the old siblings `ID.new/` and `ID.prev/`), each under that display's build lock without following symlinks (a display whose build is running is skipped with a warning); it does this even when no Wi-Fi credentials were stored, so a rerun after a failed or partial clear still cleans up; a changed password prints a notice instead. In both cases projects rendered and firmware built earlier still contain the old Wi-Fi credentials until `render`/`build` runs again (the firmware cannot be scrubbed; `flash` refuses it without `--force`). Nothing is echoed. |
| `status` | Reads the retained topics `vesselstack/v1/bridge/status`, `vesselstack/v1/bridge/signalk` and `vesselstack/v1/displays/vsdisp-ID/status` through the running Mosquitto container and prints the bridge state, the Signal K link state and each display's last retained state (`online`/`offline` as published by the bridge or device; `unknown`/`never seen` means no retained message exists, so the bridge or device has not connected since the broker's data was last cleared). It exits 1 if the broker container is not running. |

Behaviour that applies to the changing commands (`add`, `remove`, `rotate`,
`wifi`):

- They need root and take an exclusive lock (`flock` on
  `config/.displays.lock`, mode 0600). A second command waits up to 10 seconds
  for the lock and then fails with a message, changing nothing. Run
  `sync-mqtt`/the installer separately if you want to be sure no edit is in
  flight.
- Files are read and written without following symbolic links (a symlinked
  `displays.json`, secrets file, lock file or `config/` directory is
  refused), written atomically (temporary file created exclusively, fsynced,
  renamed) and keep their modes: `displays.json` 0644 and
  `displays.secrets.json` 0600, both root-owned. `displays.json` is rewritten
  with two-space indentation and key order preserved; hand-made formatting is
  not kept.
- If the MQTT sync fails after the files were updated (for example the Docker
  image is unavailable), the command exits 1 and names the retry:
  `vesselstackctl display sync-mqtt`.
- With `DISPLAYS_ENABLE=false` the files are still edited but no MQTT sync
  runs; the command says so, and the accounts take effect once displays are
  enabled and the installer or `sync-mqtt` runs. With it enabled the bridge
  already publishes every configured instrument, so `add` and `remove` need no
  bridge restart.
- Exit status: `0` success, `1` operational error (not root, lock timeout,
  broker down, sync failed), `2` usage or validation error (unknown id or
  board, invalid Wi-Fi value, missing `--yes`, invalid configuration).

`status` passes the broker administrator credentials to `mosquitto_sub`
inside the container as a mode-0600 options file (`-o`) streamed over the
`docker exec -i` standard input, never as command-line arguments or `-e`
environment variables, so they do not appear in `ps` on the host or in the
container. The temporary file exists only for the duration of the command and
is removed when it ends.

Rollback: `remove` and `rotate` are not reversible (the old password is gone);
restore `config/displays.json` and `config/displays.secrets.json` from a
`vesselstackctl backup` archive and run `vesselstackctl display sync-mqtt`.

## Data catalog

`displays/catalog.json` (schema 1, read by `displays/catalog.py`, standard library only) lists the common
Signal K values a page can show, so the Control Panel Displays tab can offer a list instead of asking
you to type paths. Each entry has a `key`, a `path` (a `*` segment stands for an instance: `electrical.batteries.*.voltage`),
a `group` (Navigation, Wind, Depth & water, Electrical, Tanks, Engine, Environment, Notifications), a `label`
(`{0}` is the matched instance), an `icon` from [Icons](#icons), a `quantity`, `precision`, `stale_after_s`,
optional `min_interval_ms`, a `range` per unit system (used as the instrument `min`/`max`, so the entry can
sit on a bar or an arc) and `suggested_thresholds` per unit system. The catalog has 38 instruments and 4
notifications (anchor, man overboard, fire, flooding).

The quantity decides the conversion and unit from `BOAT_UNITS` (`metric` or `us_customary`; the legacy
`nautical` value is read as `metric`, like the rest of VesselStack; unset or anything else is
`us_customary`): depth `m` or `ft`, temperature `K->C` or `K->F`, fluid pressure `bar` or `psi`, barometric
pressure `hPa`, volume `L` or `gal`, flow `L/h` or `gal/h`, speed `kn`, heading `deg_360`, angle `deg_signed`,
ratio `%`, `rpm`, charge `Ah`, distance `nmi`, engine hours `h`; volts, amperes and watts are shown as they
are. Only conversions that exist in `displays/conversions.py` are used.

**Live data.** `catalog --live` makes one `GET {SIGNALK_URL}/signalk/v1/api/vessels/self` (Bearer token only when
`DISPLAYS_SIGNALK_TOKEN` is set, redirects refused, 4 MiB cap, no proxy; the whole request - connect, headers and body - is bounded by a 10 s total, so a server that answers one byte at a time or never answers ends as "the Signal K server answered too slowly" after 10 s) and keeps only
`{path, si_units, age_s, value}` of numeric leaves, plus the names (never the text) of active
notifications. Positions (`*.position*`), destinations, `navigation.anchor.position`, and anything named
`mmsi`, `uuid`, `name` or `callsign` are never kept, and the raw document is neither stored nor cached.
Only the configured `SIGNALK_URL` is contacted. If the server cannot be reached the command still succeeds and
says why (`live.ok` is false). Concrete entries are marked `live` or `no_live_data`; a wildcard entry
expands to one row per live match (`electrical.batteries.house.voltage`, `...start.voltage`), or appears once
as a template with a default instance you can edit (`house`, `main`, `0`); live numeric values the catalog
does not know come under **Uncatalogued** with defaults taken from `meta.units`: no limits, no icon, the last path
segment as the label (16 characters, glyphs the display font lacks removed), unit `none` and no unit text when
the units are unknown. A row already in `displays.json` (same path and conversion) is shown as configured and
lists the displays that use it, never added twice.

**Suggested limits are never applied by default.** An instrument with `thresholds` counts in the boat-wide alarm
summary and can raise the alarm banner and take over the screen, so `suggested_thresholds` are shown as
suggestions only; a row's `definition` has none, and `materialise(..., apply_thresholds=True)` is the only way
to get them. New notifications are written with `"alarm": false` for the same reason. Oil pressure has no
suggestion because it reads 0 with the engine stopped.

## Editing pages, backups and restore

The Control Panel Displays tab calls only fixed `vesselstackctl display ...` commands. The write commands
(`set-pages`, `restore-config`) print JSON; a failure prints `{"ok": false, "code": N, "errors": [...]}` on
standard output and the first error on standard error.

| Exit status | Meaning |
| --- | --- |
| `0` | Done. |
| `1` | Operational error (not root, configuration lock timeout, unreadable files). |
| `2` | Invalid request or configuration: the `errors` list holds every problem; nothing was written. |
| `3` | `--if-revision` no longer matches `displays.json` (someone changed it); `current_revision` is in the reply. |
| `4` | A build or flash for the display is running; try again when it has finished. |

`set-pages` takes the per-display build lock **without waiting** (a running build or flash makes it refuse with
status 4, unlike `set-theme`, which waits 30 s), then the configuration lock, checks the revision, applies
the request to a copy, validates the whole result (`schema.validate_config` and the font glyph check) and writes it
atomically with `lifecycle.write_config`. The request is `pages` (required, replaces the display's pages),
`theme` (a theme id; `classic` removes the key), `new_instruments` and `new_notifications` (id to definition; an existing id with
a different definition is refused), and `instrument_edits` (id to `{field: value}`, `null` removes a field;
`path` cannot be edited). The result has `changed`, the new `revision`, the `backup` stamp, `added_instruments`,
`added_notifications`, `new_alarm_sources` (items that now count in the alarm summary), `rebuild_needed` (displays
whose firmware would change: rebuild and reflash them) and `bridge_restart`.

The displays bridge reads `displays.json` only when it starts (`bridge.main` loads it once). So when anything the
bridge reads changed (instruments, notifications, Home Assistant items, heartbeat, night schedule, or whether any display wants the
clock through `ui.header` or `ui.night`; not pages or themes), and `DISPLAYS_ENABLE=true`, the command runs `systemctl try-restart vesselstack-displays-bridge.service`.
`bridge_restart` is `not_needed`, `skipped_displays_disabled`, `restarted` or `failed: ...` (the file is written either way; restart
the bridge by hand if it failed). A change to pages or the theme alone never restarts it.

**Backups.** Before every change `set-pages` and `restore-config` copy the current `displays.json` to
`$VESSELSTACK_ROOT/config/displays-backups/displays-STAMP.json` (`STAMP` is UTC `YYYYMMDDTHHMMSSZ`, with `-2`,
`-3` for changes within one second). The directory is `root` `0700`, the files `0600`, symlinks are refused, and the newest 20
are kept. The backups hold the same content as `displays.json` and no secrets, are included in `vesselstackctl backup`
with the rest of `config/`, and are never committed (`displays-backups` is in `.gitignore` and `scripts/check-public-tree.py`).
`history` lists them (stamp, size, revision); `restore-config STAMP` accepts only a strictly formed stamp of an existing backup,
validates it against the current version (a backup that is no longer valid is refused with status 2), backs the current file up
and writes the backup byte for byte. `restore-config` **refuses (status 2) a backup whose set of displays differs from the current
`displays.json`**, naming the added or removed ids, because it never touches MQTT accounts, the ACL, secrets or build trees: a removed
display would keep its broker credentials and a resurrected one would have no account. Run `sudo vesselstackctl display add ID ...`
or `display remove ID` to make the sets match, then restore again. The ACL and password files derive from the display ids only
(`mqtt_auth.load_display_ids`); `set-pages` cannot change them (it edits one existing display and the shared definitions), and the
result of `restore-config` carries `"mqtt_sync": "not_needed"` (or `synced` / `failed: ...` if the file was unreadable and the sync ran). **Rollback** of any edit: `sudo vesselstackctl display history`, then
`sudo vesselstackctl display restore-config STAMP`; rebuild and reflash the displays named in `rebuild_needed` if the firmware
was already rebuilt.

`show` reads the stored firmware manifest and compares it with a read-only re-render (no secret is created or printed):
`outdated` means the configuration (or, when Wi-Fi and credentials are complete, the credentials) changed since it was built.
Without root the build folder is unreadable and the status is `unknown`. `readiness.last_flash` comes from
`build/displays/ID/last-flash.json` (see "Deploy and rollback"); `null` when nothing was ever uploaded with this version. It has
`ok`, `method` (`serial` or `ota`), `at`, `version`, `built_at_utc`, `ota_target` (the most recent Wi-Fi target when it is an IP
address or host name; a USB device path, a URL or anything else is never passed on) and `credentials_changed` (the credentials
differ from those compiled into the image that was flashed; `null` when unknown). `readiness.firmware.rollback_possible` is true
when `firmware.prev/` holds the image that ran before the last upload.

## Firmware: generate, build and flash

`vesselstackctl display render ID` and `vesselstackctl display export ID`
generate a complete, validated ESPHome project for one display from
`displays.json`, the board registry and the stored secrets. Compiling and
flashing are normally done on a laptop (below). `vesselstackctl display build`
and `flash` can do both on the Pi instead ([Building and flashing on the
Pi](#building-and-flashing-on-the-pi)); neither adds a Docker Compose profile
or a service, so `compose up` can never start ESPHome.

```bash
sudo vesselstackctl display wifi                       # once, shared by all displays
sudo vesselstackctl display render helm                # writes /opt/vesselstack/build/displays/helm/project/
(umask 077; ssh pi 'sudo vesselstackctl display export helm --stdout' > helm.tar.gz)   # laptop copy, private
```

| Command | What it does |
| --- | --- |
| `render ID [--allow-missing-wifi]` | Root. Generates the project into `$VESSELSTACK_ROOT/build/displays/ID/project/` and prints that path. Fails with a clear message if the display, its MQTT password (run `sync-mqtt` first) or the Wi-Fi credentials are missing; `--allow-missing-wifi` writes placeholder Wi-Fi values and warns (for CI and golden tests only; never flash such a build). Missing per-display secrets (OTA password, API key) are generated and stored under the same lock as the other lifecycle commands. |
| `export ID (--output FILE \| --stdout) [--allow-missing-wifi]` | Root. Renders, then writes a deterministic `tar.gz` (sorted entries, fixed mtime, uid/gid 0, modes 0700/0600) containing `ID/`: the device file `ID.yaml`, `secrets.yaml`, the board and app packages, the fonts and their license, and a short `README.txt` with the build and flash commands. `--output` opens the file's directory without following a symlink (a symlinked directory is refused), creates FILE in that directory exclusively with mode 0600 and refuses an existing path or a symlink. When run through `sudo` it gives the file to the invoking user (`SUDO_UID`/`SUDO_GID`, only when both are numeric and name an existing account), still mode 0600, **only if** that very directory (checked by `fstat` on the descriptor the file is created through) is owned by that user and not writable by group or others; otherwise the file stays `root:root` 0600 and the command prints a hint (`sudo chown "$USER" FILE`, or use `--stdout` under `umask 077`). This stops a user whose `sudo` rights are limited to `vesselstackctl` from choosing a path such as `/etc/cron.d/x` and ending up owning a root-created file there. Group-writable home directories (umask 002) therefore also keep root ownership: use the hint. `--stdout` refuses a terminal. With `--stdout` the file is created by your shell, not by `vesselstackctl`: redirect inside `(umask 077; ...)` as shown, otherwise the archive, which holds secrets, is created world-readable under the usual umask 022. Warnings go to stderr, so stdout carries only the archive. |

`ID.yaml` is generated and holds no secrets. It composes `boards/<board>.yaml`
(hardware only), `app/vesselstack.yaml` (Wi-Fi, OTA, MQTT) and, only when the
display has `"home_assistant_api": true`, `app/api.yaml`, then adds the
generated interface: one LVGL page per configured page, `single` (one big
tile) and `grid-2x2` tiles inside the board's centred content box, a tile per
item with its label, value and unit, and a value format of `precision`
decimals. With more than one page, a tap anywhere shows the next page and
wraps around; with one page there is no navigation. The display subscribes
only to the instruments it shows. A tile shows `--` unless the retained
`bridge/status` is `online`, MQTT is connected, the last message for that
instrument has `st` equal to `ok`, **and** a message arrived within
`stale_after_s + 2 x heartbeat_s` seconds (computed per tile).

`secrets.yaml` holds `wifi_ssid`, `wifi_password`, `mqtt_host`,
`mqtt_password`, `ota_password` and, only with the Home Assistant API,
`api_encryption_key`. `ota_password` and `api_encryption_key` (32 random bytes,
base64; always generated so enabling the API later needs no new key) are
created on first render in `displays.secrets.json`, and `display rotate`
regenerates all of the display's generated secrets.

Where files live:

| Path | Owner | Mode | Notes |
| --- | --- | --- | --- |
| `$VESSELSTACK_ROOT/build/` and `build/displays/` | `root:root` | `0700` | Deliberately **not** under `$VESSELSTACK_DATA` (service-user owned: root writing there is a symlink hazard). Created and swapped with directory-relative, no-follow operations; a symlink anywhere in the path is refused. Files the build container produced are read only after a regular-file check and with `O_NONBLOCK|O_NOFOLLOW`, so a FIFO planted at an artifact path is refused instead of hanging the build. |
| `build/displays/.ID.lock` | `root:root` | `0600` | Per-display build lock (no-follow `flock`). `render`, `export`, `build`, `flash` (and `remove`) of the same display hold it for their whole run, so two of them can never delete, rename or half-read each other's tree, or let a build rotate `firmware/` while `flash` is uploading it. A second command waits, says so, and fails after 30 s with "another build/flash for ID is running" (builds are long; try again when the first has finished). Different displays do not block each other. `set-pages` and `restore-config` take it without waiting and refuse with exit status 4 instead. It is separate from the 10 s `config/.displays.lock`; the build lock is always taken first and the configuration lock only briefly inside it, so the two cannot deadlock. The empty lock file stays in place; it holds no data. |
| `build/displays/ID/project/` | `root:root` | dirs `0700`, files `0600` | The self-contained ESPHome project, including `secrets.yaml`, and the **only** directory the compile container mounts (as `/config`, writable, so never trusted afterwards: `flash` re-renders into a private staging directory instead). Rendering again keeps the previous project as `project.prev/` (one generation). |
| `build/displays/ID/firmware/` and `firmware.prev/` | `root:root` | dirs `0700`, files `0600` | Only after `display build`: `firmware.factory.bin`, `firmware.ota.bin` and `manifest.json` of the newest and the previous build. They sit **beside** `project/`, never inside it and never mounted into a container, so a compromised build (the toolchain and components are downloaded into a persistent cache) cannot rewrite the stored images or their manifests, nor poison the `--previous` rollback. `render` replaces only `project/` and never touches these directories. |

Upgrading from the earlier layout (project files directly in `build/displays/ID/`, with `ID.new` and `ID.prev` beside it): the next `render`, `export` or `build` of that display moves to the layout above. `firmware/` and `firmware.prev/` are already in the right place and are kept; the superseded project files (which contain plaintext secrets) and `ID.new`/`ID.prev` are deleted and the project is rendered again. `flash` refuses an old-layout directory and tells you to run `render` once. Rollback to the older layout is not supported; rebuild with `display build`.

The build tree, including any stored firmware, is excluded from `vesselstackctl
backup` (it is regenerable from `config/`, and contains secrets; compiled
firmware embeds the Wi-Fi, MQTT and OTA secrets, so it is as sensitive as the
archive below) and from the public tree, and
`uninstall --purge-data` removes it with `$VESSELSTACK_ROOT`. Treat an export
archive like a password: it contains the Wi-Fi password and the display's MQTT,
OTA and API secrets. `export` prints a warning to stderr; move archives over SSH
only, never commit or e-mail them, and delete them after flashing.

Pinned ESPHome. The firmware is built with exactly one ESPHome release,
`2026.10.0`, and the generated device file requires it
(`esphome: min_version: 2026.10.0`). The container image is pinned by digest
(the multi-architecture index digest; arm64 and amd64 are present):

```text
ghcr.io/esphome/esphome:2026.10.0@sha256:1dff19a59070cdb523b76576cdc74d200f862d43c1407809088855920cf57145
```

The single source of truth is `displays/esphome/IMAGE` (with
`displays/esphome/VERSION`); `render` reads it for the exported `README.txt`,
`tests/test-displays-esphome-config.sh` and CI use the same file, and
`tests/test-distribution.sh` fails if this README quotes a different pin. To
bump: pick the new release, `docker buildx imagetools inspect
ghcr.io/esphome/esphome:NEW` for the index digest, update `IMAGE`, `VERSION` and
the two places in this README, re-run `tests/test-displays-esphome-config.sh`
and a `compile`, then re-flash displays at your own pace (firmware and
generator must match, so regenerate and reflash after an update).

Build and flash on a laptop:

```bash
tar -xzf helm.tar.gz && cd helm
docker run --rm -v "$PWD":/config -v vesselstack-esphome-cache:/cache \
  ghcr.io/esphome/esphome:2026.10.0@sha256:1dff19a59070cdb523b76576cdc74d200f862d43c1407809088855920cf57145 \
  compile /config/helm.yaml
```

- **Linux:** the same image can flash directly; add `--device=/dev/ttyACM0` and
  use `run /config/helm.yaml --device /dev/ttyACM0` instead of `compile`.
- **macOS and Windows:** Docker Desktop cannot pass USB serial devices into
  containers. Compile with Docker as above, or install ESPHome natively
  (`pipx install esphome==2026.10.0` or `pip install esphome==2026.10.0`) and run
  `esphome run helm/helm.yaml` (first flash over USB). Alternatively upload the
  `firmware.factory.bin` that `compile` creates with <https://web.esphome.io>
  (Chrome or Edge). The first compile downloads the ESP-IDF toolchain (about
  2 GB into the cache volume; measured on a laptop: about 4 minutes cold, 2 minutes with a warm cache); each display's build adds
  about 600 MB under `.esphome/` in its project directory, and the firmware is
  about 1.1 MB (13% of flash).
- **Later updates over Wi-Fi (OTA):** `esphome upload helm.yaml --device
  <display-ip>` from the project directory (it uses the OTA password in
  `secrets.yaml`). The OTA password changes only with `rotate`, and the display
  keeps the old one until it is reflashed: after a `rotate` (and a re-render,
  export or rebuild) reflash over USB. Changed Wi-Fi credentials do not change
  the OTA password, but the display cannot join the new network, so OTA cannot
  reach it either: reflash over USB.
- **Rollback:** reflash a previous project (`project.prev/`, an older export, or a
  firmware binary you kept), or on the Pi `display flash ID ... --previous`
  (see [Building and flashing on the Pi](#building-and-flashing-on-the-pi)). A display refused by the broker (after `rotate` or
  `remove`) or on changed Wi-Fi credentials always needs a USB reflash (the display never reaches the
  network or the broker, so there is nothing to update over the air).

### Building and flashing on the Pi

Building on the Pi is **optional**. Prefer the laptop export when you have one:
it is faster, needs no extra disk on the boat computer, and Docker Desktop on
macOS or Windows is fine for compiling. Use the Pi when there is no laptop,
when you want one-command rebuilds after editing `displays.json`, or to push
OTA updates from the boat network.

```bash
sudo vesselstackctl display build helm                  # render + compile, prints elapsed time
sudo vesselstackctl display flash helm --serial /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_XX-if00
sudo vesselstackctl display flash helm --ota 192.0.2.50
sudo vesselstackctl display flash helm --ota helm.local --previous   # roll back to the previous build
```

| Command | What it does |
| --- | --- |
| `build ID` | Root. Renders the project exactly like `render` (so it fails with the same message if Wi-Fi is not configured), checks free disk space, then runs **one** `docker run --rm` container of the pinned image (`displays/esphome/IMAGE`) with `esphome compile`. The project directory is mounted at `/config` and the named volume `vesselstack-esphome-cache` at `/cache` (toolchain cache). Compiler output streams to the terminal. On success it stores `firmware.factory.bin` and `firmware.ota.bin` plus `manifest.json` in `build/displays/ID/firmware/` (directory 0700, files 0600), moves the previous `firmware/` to `firmware.prev/` (exactly one previous build is kept), removes the roughly 600 MB scratch tree `.esphome/`, and prints the elapsed time. If the image is not present it is pulled first (about 250 MB download, 1.2 GB on disk; a note is printed). A failed compile leaves the stored firmware untouched. |
| `flash ID --serial DEVICE` | Root. Verifies the stored `firmware.factory.bin` against `manifest.json` (sha256 and size), then copies exactly those verified bytes into a fresh root-only `0700` temporary directory under `$VESSELSTACK_ROOT/build/` (re-verifying the copy's sha256) and runs a one-shot container with `--device DEVICE`, `--network none` (USB needs no network) and `esphome upload /config/ID.yaml --device DEVICE --file /fw.bin`, where only that copy is mounted, read-only, at `/fw.bin`. `/config` is **not** `project/` (the compile container could write to it): `flash` re-renders the project from `displays.json`, the registry and the secrets into a fresh root-only `0700` staging directory under `$VESSELSTACK_ROOT/build/` (see "Re-rendered project" below) and mounts only that, read-only. ESPHome's scratch space (`/tmp`, `/cache`) is tmpfs. The staging directory is removed afterwards. Does not compile and never pulls the image. |
| `flash ID --ota HOST` | Root. Verifies the stored `firmware.ota.bin` the same way, then runs a one-shot container with `--network host` (the only mode that needs it: it must reach the display on the LAN) and `esphome upload ... --device HOST --file /fw.bin` (the verified copy and the re-rendered staged project, as above). The OTA password is read by ESPHome from `secrets.yaml` inside the container; it is never on a command line. |
| `flash ... --previous` | Flashes `firmware.prev/` instead (verified against its own manifest). Refused when there is no previous build. |
| `flash ... --force` | Skips the credential check below. |

**Credential check.** Compiled firmware embeds the Wi-Fi, MQTT, OTA and API secrets, and the source hash deliberately ignores them, so after `display rotate`, `display wifi`, `display wifi --clear` or a `remove` followed by `add` the stored images still carry credentials that are no longer current: the device would never join Wi-Fi or be accepted by the broker. `build` therefore records `secrets_sha256` (the sha256 of the `secrets.yaml` it compiled in; the hash only, never a secret) in `manifest.json`. Before uploading, `flash` renders the current secrets in memory (as `render` would, including generating a missing OTA password or API key) and refuses with "credentials changed since this firmware was built; run `display build ID`" when the fingerprints differ, when the credentials cannot be rendered (Wi-Fi cleared, display removed) or when the manifest predates the fingerprint. For `--previous` the refusal says the older build may carry revoked or outdated credentials. `--force` flashes anyway, for example after you restored the old credentials on purpose. **Re-rendered project.** `project/` is writable by the compile container, so a compromised build could edit `ID.yaml` there (add `external_components`, `includes`, `packages`, lambdas, or another OTA target) and have the privileged, host-network upload parse it. `flash` therefore never reads `project/`. Under the build lock it renders the project again from `displays.json`, the registry and the secrets (rendering is deterministic), compares the hash of that source with `source_sha256` in the manifest and refuses with "configuration changed since this firmware was built; run `display build ID`" when they differ (also for `--previous` when the older build came from a different configuration), then writes it into a fresh `0700` directory made with `mkdtemp` under `$VESSELSTACK_ROOT/build/` (`.flash-*`, files `0600`, no symlink followed) and mounts only that directory, read-only, plus the verified firmware copy. `--force` skips the credential and configuration comparisons and uploads the freshly re-rendered current project; it still needs the credentials to be renderable (a cleared Wi-Fi setting fails even with `--force`). `project/` and its `secrets.yaml` no longer matter to `flash`, so a missing or scrubbed `project/` is fine. Rollback: `git revert` the change; nothing is stored differently on disk.

How the flash is done, and why it flashes exactly the recorded bytes: ESPHome
2026.10.0 `upload` accepts `--file` (`esphome/__main__.py`, parser option
`--file`, "Manually specify the binary file to upload"). Over serial,
`upload_using_esptool` then writes that file at offset `0x0` with `esptool
write-flash`, so the **factory image** (bootloader, partition table and
application) is written, which is what a first flash or a recovery needs. Over
the air, `_upload_via_native_api` uploads `args.file` instead of the build
output, so the **OTA image** (application only) is sent. In both cases ESPHome
still loads `/config/ID.yaml` and `secrets.yaml` to validate the
configuration (and, for OTA, to read the password and port), but it does not
compile and does not look at `.esphome/`. Before starting the container,
`flash` reads the image without following symbolic links and refuses to continue
unless its sha256 and size match `manifest.json`, and the container only ever
sees a copy of those verified bytes, so a build or a swapped file cannot change
what is uploaded after the check. Why `flash` can mount the project read-only:
verified with the pinned image, `esphome upload --file` against an unreachable
host with the project mounted `:ro` at `/config` still loads the configuration and reaches the
connection attempt; its only complaints are about the cache and
`/config/.esphome/storage`, which `flash` redirects to tmpfs
(`--tmpfs /tmp --tmpfs /cache -e ESPHOME_DATA_DIR=/tmp/esphome-data`). Serial
upload with a read-only project (and `--network none`) is expected to behave the same but is not
verified without hardware.

`manifest.json` records `image` and `image_digest` (the pinned ESPHome image),
`esphome_version`, `vesselstack_version`, `device_name`, `source_sha256` (a hash
of the rendered project without `secrets.yaml` and `README.txt`, so it changes
when the configuration changes but never depends on a secret), the sha256 and
size of both images, `secrets_sha256` (see the credential check) and `built_at_utc`. It contains no secrets.

### Deploy and rollback

```bash
sudo vesselstackctl display deploy helm --ota 192.0.2.50 [--progress]
sudo vesselstackctl display deploy helm --serial /dev/serial/by-id/usb-...
```

`deploy ID (--ota HOST | --serial DEVICE) [--progress]` is what the Control Panel's **Apply to screen** runs. It holds **one** build
lock from the render to the end of the upload and does `build` (only when needed) then `flash`. The compile is skipped when
`firmware/` is intact, was built from today's source (`source_sha256`) and today's credentials (`secrets_sha256`) and the pinned
image is present, so a retry after a failed upload does not rebuild. With `--progress` it prints `STAGE: render`, `STAGE: compile`
(followed by a note when skipped), `STAGE: upload` and `STAGE: done`. Everything the ESPHome tools print passes through a
redactor first: the Wi-Fi SSID and password and the MQTT, OTA and API secrets (also JSON-, URL- and shell-quoted) are replaced with
`[redacted]` before a line leaves the command. Known failures end with a single `To fix:` line: an unreachable host or a timeout (check
power and Wi-Fi, use the IP address instead of a `.local` name), an authentication failure (the credentials changed after the display
was flashed: flash over USB, with the exact command), no space on the device, Docker missing, the image missing, a full disk, a
busy lock. Exit status: `0` done, `1` failed, `2` invalid target. A build made by `deploy` replaces `firmware/` **without** moving it
to `firmware.prev/` when `firmware/` was never uploaded (according to `last-flash.json`), so `firmware.prev/` stays the image the
device last ran; without a `last-flash.json` it rotates as `build` does. `firmware/` is only rotated into `firmware.prev/` when it is intact (a readable manifest), and an existing `firmware.prev/` is never deleted when there is no `firmware/` to replace it. If the upload worked but `last-flash.json` cannot be written, the old record is removed (a stale one would name the wrong image) and a warning is printed. `--ota` accepts an IP address (normalised; no `%` scope id) or a host name, and rejects the names ESPHome reserves (`bootsel`, `mqtt`, `mqttip`, `ota`, `serial`, `COM*`) in any letter case. Redaction also covers the spelling used in `secrets.yaml` (non-ASCII characters as `\u` escapes).

**Control Panel notes.** Apply is not blocked by a missing ESPHome builder image: the readiness checklist shows it as a to-do and, when the computer is offline, the deploy fails with a `To fix:` hint (accepted behaviour). A Wi-Fi upload to an address other than the last successful one needs `confirm_new_target` (the image holds the Wi-Fi password); the dialog asks for an explicit tick. The rollback button is offered only when `firmware.prev/` was built from the current settings and credentials, i.e. after **History → Restore** of the matching saved copy. `build` and `flash` themselves behave as before.

**`last-flash.json`** (`build/displays/ID/`, root `0600`, written atomically without following symlinks by `flash` and `deploy`,
only after a successful upload; a failed upload records nothing): `{"schema": 1, "current": {...}, "previous": {...} | null}`
where each entry has `method`, `target` (the IP, host name or USB device), `image_sha256` (the uploaded image), `source_sha256`,
`secrets_sha256` (hashes only), `at`, `version` and `built_at_utc`. Uploading the same image again only updates `current`. It
holds no secret and is never committed (`.gitignore`, `scripts/check-public-tree.py`).

**Rollback** (put the previous firmware back on the screen):

```bash
sudo vesselstackctl display history                       # find the backup made before the edit
sudo vesselstackctl display restore-config STAMP          # bring the old settings back
sudo vesselstackctl display flash helm --previous --ota 192.0.2.50      # or --serial DEVICE
```

Rendering is deterministic, so after the restore the old firmware's `source_sha256` matches again and `flash --previous` passes its
checks without `--force`. It is refused (and says why) if the credentials changed since that image was built. The
Control Panel offers the same steps (Displays tab, History).

Requirements and limits:

- **Disk.** About 2 GB for the toolchain cache (the `vesselstack-esphome-cache`
  volume, created by the first build), about 1.2 GB for the ESPHome image, and
  about 600 MB of scratch space per build (removed afterwards; the stored
  firmware is about 1.1 MB per image). `build` refuses to start unless the
  filesystem holding Docker's data directory (`docker info` `DockerRootDir`, else
  `/var/lib/docker`) has at least 3 GB free for a first build (cache volume
  missing) or 1 GB otherwise.
- **Time (estimates).** About 10-15 minutes for the first build on a Raspberry
  Pi 5 (image pull, toolchain download, full compile) and 3-6 minutes with a warm
  cache. These are estimates for a Pi 5, not measurements; the laptop figures
  above (measured) are about 4 minutes cold and 2 minutes warm, and `display
  build` itself measured 99 s cold (including the toolchain download) and 31 s
  warm in Docker Desktop on an Apple-silicon laptop. A Pi 4 is slower.
- **Serial device.** DEVICE must be an absolute path under `/dev` that is a
  character device named `/dev/tty*` (for example `/dev/ttyACM0`), or a symbolic
  link under `/dev/serial/by-id/` that resolves to one (preferred: the name
  survives re-plugging). Any other symbolic link is refused. No service account
  or `dialout` group membership is needed: `vesselstackctl` runs as root and the
  container gets only that one device (`--device`). If the board is not
  detected, hold BOOT while tapping RESET to enter download mode.
- **OTA.** HOST is an IP address or an RFC 1123 host name (no URL, port or
  path). The display must be powered, on Wi-Fi and reachable from the Pi on the
  LAN. OTA uses the OTA password in the project's `secrets.yaml`. That password
  changes only with `display rotate`: after a rotate and a re-render or
  rebuild, the running firmware no longer matches it, so flash over USB. Changed
  Wi-Fi credentials do not change the OTA password; the display just cannot join
  the new network until it is reflashed over USB.
- **Rollback.** `flash ID ... --previous` re-sends the previous build, subject
  to the same credential check (a rollback build that predates a `rotate` or
  Wi-Fi change is refused without `--force`, because it would not connect).
  OTA rollback works only while the OTA password in `secrets.yaml` is the one
  that build was compiled with; serial always works. Anything older than one build
  needs an earlier export or a rebuild.
- **Security.** The container runs as root inside Docker (the image default) with
  all capabilities dropped and `no-new-privileges`; the compile container has no
  device and the default network (it downloads the toolchain), the serial
  container has only the device and `--network none`, and the OTA container uses the host network to
  reach the LAN. Neither mounts `project/`: both mount a freshly re-rendered, root-only staging directory read-only. Stored firmware embeds the Wi-Fi, MQTT and OTA secrets, hence
  mode 0600 in a 0700 root-owned tree, and it is **not included in
  `vesselstackctl backup`** (`build/` is excluded; it is reproducible with
  `display build`).
- **Cleanup.** `docker volume rm vesselstack-esphome-cache` frees the toolchain
  cache (the next build downloads it again); `docker image rm` of the pinned image
  frees the rest. `vesselstackctl uninstall` leaves the volume and says so;
  `uninstall --purge-data --yes` also removes it (best effort).
- **Rollback of this feature.** Stop using `build` and `flash`: nothing runs
  unless you call them. Delete `$VESSELSTACK_ROOT/build/` and the volume to
  reclaim the space; installed displays keep running whatever firmware they have.
- The image version is the single pin described below; after bumping it, run
  `display build` again, and re-flash at your own pace.

Security and behaviour decisions:

- **No fallback access point and no captive portal.** They would add an
  always-listening configuration surface to a boat network device. If the Wi-Fi
  credentials change, reflash over USB. OTA is `platform: esphome` with the
  generated `ota_password`. The Home Assistant `api:` is included only per
  display, with `encryption` and `reboot_timeout: 0s`. MQTT discovery, IP
  discovery, the log topic and the display's own backlight entity are off or
  internal because the display's ACL allows only reading `bridge/` and
  `instruments/` topics and writing its own `status` topic. Its birth and will
  messages are retained on `vesselstack/v1/displays/vsdisp-ID/status`.
- **User text cannot become code.** ESPHome 2026.10 evaluates `$var`, `${expr}`
  and `<% %>` in **every** string, including values read from `secrets.yaml`,
  with an unsandboxed Jinja environment, and has no `$$` escape (verified with
  `esphome config`: `$$x` is still substituted). The only suppression is the
  `!literal` tag, so labels, units, glyph lists and every secret are emitted as
  `!literal` double-quoted YAML scalars (non-ASCII as `\u` escapes) and never
  placed inside a C++ lambda. A Wi-Fi password such as `pa$$w${x}` or a label
  such as `${1+1} <% x %>` therefore arrives unchanged;
  `tests/test-displays-esphome-config.sh` checks that against the real ESPHome.
  Instrument ids are `[a-z][a-z0-9_]*` and are the only user-derived text in
  identifiers.
- **Font coverage.** The tiles use Roboto Condensed (SIL OFL 1.1, bundled in
  `displays/esphome/fonts/` with its license; local files, so builds work
  offline). The glyph list is computed from the characters actually used in
  labels and units plus digits, `.`, `-` and space. A character the font lacks
  is rejected by `render`, `export` and `displayctl.py validate` with the
  instrument and code point.
  Icons use a second bundled font, see [Icons](#icons).
- **`mqtt_host` and mDNS.** The generated MQTT client uses lwIP's resolver
  (`esphome/components/mqtt/mqtt_client.cpp`, `start_dnslookup_`). On ESP-IDF
  ESPHome enables `CONFIG_LWIP_DNS_SUPPORT_MDNS_QUERIES` by default
  (`esphome/components/esp32/__init__.py`, option `enable_lwip_mdns_queries`;
  present in a compiled `sdkconfig`) and the `network` component auto-loads
  `mdns`, so `vesselstack.local` should resolve by multicast DNS. That is
  **not verified on the display hardware**, depends on the Pi advertising its
  name (Avahi) and on the access point passing multicast, so `render` warns
  when `mqtt_host` ends in `.local`; use a fixed IP address or a DNS name.
- The broker connection is plaintext MQTT on port 1883 (see the limitations
  below).

Optional local tuning of a board is done by editing the exported copy's board
file (`boards/*.yaml`), which survives until you export again. A package
override that survives updates is not offered: ESPHome appends list entries such
as `display:` from separate packages instead of merging them by `id` (checked
with `esphome config` on 2026.10.0), so an override file cannot adjust the
existing display.

### Hardware bring-up checklist (Waveshare ESP32-S3-Touch-LCD-2.8C)

The configuration validates and compiles with the pinned ESPHome, and it has
been run on one real 2.8C unit: red, green, blue and white were correct,
orientation, the round safe area and touch coordinates were consistent, and no
knob needed changing. Other units (or a different hardware revision) may differ,
so on first boot check in this order and turn only the matching knob in `boards/waveshare-esp32-s3-touch-lcd-2.8c.yaml` of the
exported project (or in `displays/esphome/boards/` for all future exports):

| Symptom | Knob |
| --- | --- |
| Screen stays blank or backlight only | Try `spi_mode: MODE3` with `data_rate: 2MHz`; check `cs_pin`, `reset_pin` (IO expander numbers) |
| Red and blue swapped | Swap the `red` and `blue` lists under `data_pins` |
| Garbled or noisy picture | `byte_order: little_endian`, then `pixel_mode: 16bit` |
| Inverted colours | `invert_colors: true` |
| Mirrored left to right | In `init_sequence` change `[0xC7, 0x00]` to `[0xC7, 0x04]` |
| Upside down or rotated | Display `rotation` (or the `transform` block) |
| Picture shifted | Swap the horizontal and/or vertical front and back porch values |
| Shimmering edges | `pclk_inverted: true` |
| Drift or tearing while Wi-Fi is busy | Lower `pclk_frequency` to `16MHz`, then `14MHz` |
| Touch axes wrong | The `touchscreen` `transform` (`swap_xy`, `mirror_x`, `mirror_y`) |

The `mipi_rgb` model is `CUSTOM` on purpose: ESPHome's `ST7701S` model prepends
its own init table and appends a C7 command in the wrong register bank. ESPHome
itself appends the `3A`, `36`, `20`/`21`, `11` and `29` commands, so the board
file's `init_sequence` omits them. Delays are written as `- delay 120ms`.
Please report the settings that worked on your unit.

## Configuration and validation

The runtime configuration lives at `$VESSELSTACK_ROOT/config/displays.json`.
Start from [`displays/displays.example.json`](../displays/displays.example.json).
It is JSON with `schema` (must be `1`), `mqtt_host` (hostname or IP literal),
`heartbeat_s` (1-60, default 5), `instruments`, optional `notifications`, optional
`ha` (see [Home Assistant items](#home-assistant-items)), optional
`night_schedule` and `displays`:

- An instrument (`^[a-z][a-z0-9_]{0,31}$`) has a Signal K `path` for
  `vessels.self` (numeric scalar values only), `label` (1-16 characters),
  `convert` (one named conversion), `unit` (up to 8 characters), `precision`
  (0-3), `stale_after_s` (1-3600 and at least 2 x `heartbeat_s`) and optional
  `min_interval_ms` (100-60000, default 1000), `thresholds`, `min`, `max`
  and `alarm` (see [Thresholds and severity colours](#thresholds-and-severity-colours)). Instruments, notifications and Home Assistant items also take an optional `icon` (see [Icons](#icons)).
- A display (`^[a-z]([a-z0-9-]{0,22}[a-z0-9])?$`: 1-24 characters, starting with a letter and not ending in a hyphen) has a `name`, a `board` from
  `displays/boards/registry.json`, optional `home_assistant_api` (boolean),
  optional `ui` (see [Alarm summary and takeover](#alarm-summary-and-takeover) and
  [Touch, header, night mode and dimming](#touch-header-night-mode-and-dimming)) and
  1-8 `pages`. A page has a `title` (1-16 characters), a `layout` the board
  supports (`single` holds 1 item, `grid-2x2` holds 4, `list-6` holds 6,
  `status-grid` holds 1-9, `tank-bars` holds 1-4, `gauge-arc` holds 1, `electrical` holds 3-5) and unique `items` naming existing instruments or,
  on layouts that accept them (`status-grid`, and the shore slot of `electrical`), notifications and Home Assistant items. Each layout
  declares the item types it accepts; `single`, `grid-2x2`, `list-6`, `tank-bars` and
  `gauge-arc` take instruments and numeric Home Assistant items; only `status-grid` and the
  shore slot of `electrical` take binary and state Home Assistant items. On `electrical` the position of an item
  in `items` decides its role (see [The `electrical` layout](#the-electrical-layout)).
- A notification (see [Notifications](#notifications)) has the same id rules
  as an instrument; ids are unique across instruments, notifications and Home
  Assistant items.
- Conversions: `none`, `m/s->kn`, `m/s->km/h`, `m/s->mph`, `K->C`, `K->F`,
  `rad->deg`, `rad->deg_signed` (-180..180], `rad->deg_360` [0,360), `m->ft`,
  `m->fathom`, `m->nmi`, `ratio->%`, `Pa->hPa`, `Pa->bar`, `Pa->psi`, `m3->L`,
  `m3->gal` (US), `m3/s->L/h`, `m3/s->gal/h`, `Hz->rpm`, `C->Ah`, `s->min`,
  `s->h`, `negate` (changes the sign; for a current sensor wired the wrong way
  round, see [The `electrical` layout](#the-electrical-layout)).

Unknown keys are rejected at every level, booleans are not accepted where
integers are expected, and all errors are reported together with a JSON-path
location. Validate with the standard library only (no extra packages):

```bash
python3 displays/displayctl.py validate                  # the example file
python3 displays/displayctl.py validate /path/to/displays.json
```

Exit status is `0` valid, `1` validation errors (nothing is written), `2`
usage, file or JSON parse errors.

### Thresholds and severity colours

An instrument may carry optional `thresholds` so its tile changes colour with
the value. All numbers are in **display units** (after `convert`, for example
knots or feet, not Signal K SI units) and must be finite JSON numbers (booleans
are rejected):

```json
"house_v": {"path": "electrical.batteries.house.voltage", "label": "House", "convert": "none",
            "unit": "V", "precision": 2, "stale_after_s": 30,
            "thresholds": {"alarm_below": 11.8, "warn_below": 12.2, "warn_above": 14.8,
                           "alarm_above": 15.2, "hysteresis": 0.1},
            "min": 10, "max": 16}
```

- `thresholds` holds at least one of `alarm_below`, `warn_below`, `warn_above`
  and `alarm_above`, plus an optional `hysteresis`. The bounds present must be
  ordered `alarm_below <= warn_below < warn_above <= alarm_above`, and
  `hysteresis` is `0 <=` it `<` the smallest gap between the bounds present
  (equal bounds therefore allow only `0`). Any single bound is allowed, for
  example only `warn_below` for a fresh-water tank.
- A value below `warn_below` is `warn`, below `alarm_below` is `alarm`; above
  `warn_above` is `warn`, above `alarm_above` is `alarm`; otherwise `ok`. A
  value exactly on a bound is still on the safe side. The alarm bounds are
  checked first. The value tested is the **rounded** published value, so the
  colour always matches the digits on screen.
- Hysteresis applies only when the severity gets less severe: leaving `alarm`
  (or `warn`) needs the value to clear the bound by `hysteresis`. With
  `alarm_below: 11.8` and `hysteresis: 0.1` an `alarm` ends at 11.9 or above
  (not at 11.8). Getting more severe is immediate. After the value was
  `stale` or `missing` there is no memory: the first value is judged on the
  bounds alone.
- `alarm` (boolean, meaning "this instrument may raise an alarm", default true
  when `thresholds` is present, false otherwise) decides whether the instrument
  counts in the [alarm summary](#alarm-summary-and-takeover). `min` and `max`
  (finite, `min < max`) are validated; they scale the bars and the arc of the
  [`tank-bars` and `gauge-arc` layouts](#the-tank-bars-and-gauge-arc-layouts)
  and are required for instruments on those layouts. They change nothing on the
  other layouts.
- Colours on the tile (background / value text): `ok` green `0x1E7B34` /
  white, `warn` amber `0xF2A900` / black, `alarm` red `0xC62828` / white. A
  tile that shows `--` (bridge offline, MQTT disconnected, `st` not `ok` or no
  message within the staleness threshold) is always grey `0x37474F` / `0x9AA5B1`,
  never green. Instruments without `thresholds` keep the plain `0x101820`
  tile and their generated firmware is unchanged.
- The `single` and `grid-2x2` layouts support colours. Only the tile background
  and the value digits change; the label and unit keep their grey.

[`displays/displays.dashboard.example.json`](../displays/displays.dashboard.example.json)
is an example with thresholds on depth, battery voltages and tank levels
(a status page, tank bars and a fuel arc). New firmware is needed to see colours: `vesselstackctl
display build ID` and then `display flash ID --ota HOST` or `--serial DEVICE`.
Rollback: remove `thresholds` from the instruments and rebuild and reflash, or
flash the previous firmware (`display flash ID --previous ...`).

### Notifications

A Signal K notification (an alarm or warning such as the anchor alarm or a low
battery notification) can be shown as a coloured status cell on a `status-grid`
page. Add a top-level `notifications` object to `displays.json`:

```json
"notifications": {
  "anchor": {"path": "notifications.navigation.anchor", "label": "Anchor"},
  "low_battery": {"path": "notifications.electrical.batteries.house.voltage",
                  "label": "Low battery", "absent": "ok", "alarm": true}
}
```

- `path` must start with `notifications.` (own vessel, Signal K path syntax),
  `label` is 1-16 printable characters in the bundled font (checked like
  instrument labels), `absent` is `ok` (default) or `unknown` and `alarm` is a
  boolean (default true) that decides whether the notification counts in the
  [alarm summary](#alarm-summary-and-takeover).
  Ids use the instrument id rules and must not clash with an instrument id.
- State mapping: the notification value is an object with a `state`.
  `normal` and `nominal` show **OK** (green), `alert` and `warn` show **WARN**
  (amber), `alarm` and `emergency` show **ALARM** (red). A cleared
  notification (value `null`) follows `absent`. Any other state string, or a
  value that is not such an object, is `unknown` and shows grey `--`. The
  notification `message` text is never read, published or shown.
- `absent`: Signal K only reports a notification while it exists, so silence
  means either "nothing is wrong" or "the source is not running". `absent:
  "ok"` treats a notification that has not been raised, or has been cleared, as
  healthy (right for alarms Signal K itself generates, such as the anchor alarm);
  `absent: "unknown"` shows grey `--` instead (right when a missing notification
  could just as well mean the monitoring plugin is not running). Either way,
  while Signal K is disconnected every notification is `stale` and grey, and
  nothing is ever green on a stale or unknown state.
- After Signal K (re)connects, notifications that nobody has reported on stay
  `unknown` for 3 s so that an alarm that is still active is shown red as soon
  as Signal K re-sends it instead of flashing green first.

Topic: each notification is published retained to
`vesselstack/v1/notifications/<id>` as compact JSON,
`{"v":null,"u":"","st":"ok","sev":"alarm","ts":"2026-01-01T00:00:00.000Z","age":0.4}`
(`v` is always `null` and `u` empty; `sev` is always present). `st` is `ok`
while Signal K is connected, `stale` once it disconnects (everything heard is
forgotten and Signal K re-sends active notifications on reconnect) and `missing`
before the first connection. Items are republished every `heartbeat_s`; the
display blanks a cell (grey `--`) after 3 x `heartbeat_s` without a message,
when the bridge is offline, or when MQTT is disconnected. The bridge subscribes
to these paths with a 1000 ms period; instruments still only publish numeric
values. The bridge needs the `notifications/#` ACL rule (see [MQTT access
control](MQTT.md)).

### Alarm summary and takeover

The bridge condenses all alarm-relevant items into one retained message,
`vesselstack/v1/alarms/summary`, and a display may use it to take over the
screen with a flashing banner.

```json
{"max":"alarm","na":1,"nw":1,"nu":0,"ids":["anchor","depth"],"ts":"2026-01-01T00:00:00Z"}
```

- **What counts:** only items with `alarm: true`. Instruments default to true
  when they have `thresholds` and to false otherwise; notifications default to
  true. A stale or never-heard item counts as `unknown`.
- `na`, `nw`, `nu` are the numbers of items that are `alarm`, `warn` and
  `unknown`. `max` is the highest of `alarm` > `warn` > `unknown` > `ok`:
  `unknown` ranks **above** `ok`, so a dead Signal K feed or stale sensor is never
  reported as all clear. With no counted item at all the topic is not
  published.
- `ids` lists the alarm ids first, then the warn ids, each group in
  `displays.json` order (instruments, then notifications), at most 16. Only
  ids are published: never labels, units, values or notification messages.
  `ts` is the bridge's UTC time.
- Published (retained, QoS 1) on every change and every `heartbeat_s`. On a
  clean stop (SIGTERM) the bridge publishes a last summary with every counted
  item `unknown` (`max` `unknown`, `na` 0), so a stopped bridge never leaves a
  retained `ok` or `alarm`. After a crash or power loss the retained message
  stays, which is why displays also require `bridge/status` `online` and a
  summary newer than 3 x `heartbeat_s`.
- Needs the `alarms/#` ACL rule (see [MQTT access control](MQTT.md)).

Per-display option in `displays.json` (default off; nothing about the firmware
changes without it):

```json
"ui": {"alarm_takeover": true, "alarm_page": 1}
```

- `alarm_takeover` (boolean, default false) turns the banner on. It is rejected
  when nothing in the configuration counts towards the summary.
- `alarm_page` (integer index into `pages`, default 0, validated) is the page
  shown when an alarm starts. Unknown `ui` keys are rejected.

Behaviour of a display with `alarm_takeover`:

- The firmware subscribes to the summary (the display ACL allows reading it).
  A red banner (0xC62828, 96 px high, as wide as the board's content area, 340
  px on the Waveshare 2.8C, centred on top of every page) shows the alarm count
  and the fixed word `ALARM` or `ALARMS` (`99+` above 99) and the name of the
  first alarm. The name is chosen from the display's own items by compile-time
  `!literal` branches (the summary carries ids only); an alarm that is not an
  item of this display shows the count only. The banner flashes (500 ms).
- It is shown only while the newest summary has `max` `alarm`, is fresher than
  3 x `heartbeat_s`, the bridge is `online` and MQTT is connected. Otherwise it
  is hidden (never green, never stuck).
- Takeover is edge-triggered, when the alarm count rises or `max` changes to
  `alarm` (also when the first summary after boot or after a stale period is an
  alarm): the display switches to `alarm_page`, shows the banner and sets the
  backlight to 100 %. You can still navigate away. Tapping the banner hides it
  locally until the next such edge. Nothing is sent back: **there is no
  acknowledgement in this version, the bridge and the alarm state are not
  affected and displays stay read-only**; acknowledging from a display is a
  possible future feature.
- Glyphs for the banner are included in the generated fonts (about 16 kB more
  flash and 1.4 kB RAM on the dashboard example).

[`displays/displays.dashboard.example.json`](../displays/displays.dashboard.example.json)
enables it with `alarm_page` set to the Status page. **Upgrade:** update
VesselStack, let the installer (or `sudo vesselstackctl display sync-mqtt`)
rewrite the ACL, restart the bridge (the installer does), then `display build ID`
and `display flash ID` for each display that gets the option. Existing firmware
ignores the summary. **Rollback:** remove `ui`, rebuild and reflash (or flash
the previous firmware); the bridge's extra topic is harmless to older displays.

### Touch, header, night mode and dimming

Per-display `ui` options (all optional; a display without them gets exactly the
firmware it got before, byte for byte):

| `ui` key | Type, default | Meaning |
| --- | --- | --- |
| `alarm_takeover`, `alarm_page` | see [Alarm summary and takeover](#alarm-summary-and-takeover) | alarm banner |
| `swipe` | boolean, false | swipe left shows the next page, swipe right the previous one; a short tap still shows the next page |
| `header` | boolean, false | clock, status dots, page title and page dots in the round bands above and below the content area |
| `night` | `off` (default), `manual`, `schedule` | night palette: `manual` is toggled by a long press, `schedule` follows the top-level `night_schedule` and a long press overrides it |
| `day_brightness` | integer 5-100, default 100 | backlight in percent by day (and when `night` is `off`) |
| `night_brightness` | integer 5-100, default 20, not above `day_brightness` | backlight in percent at night; also the idle-dim level when set |
| `dim_after_s` | integer 0-3600, default 0 (never) | dim after this many seconds without a touch |
| `theme` | `classic` (default), `high-contrast`, `daylight`, `ocean` | colour theme; see [Themes](#themes). Absent means `classic`. An unknown id is a validation error that lists the valid ids |

`night: schedule` needs the top-level `night_schedule`, `{"start": "21:30",
"end": "06:15"}`: 24-hour `HH:MM` on the boat's wall clock, `start` inclusive
and `end` exclusive, `start` and `end` different. A window whose start is later
than its end crosses midnight. A bad time, a missing end or an unknown key is a
validation error. `header` needs a board with a `ring` entry in the registry
(see below). Unknown `ui` keys are still rejected.

```json
"night_schedule": {"start": "21:30", "end": "06:15"},
"displays": {"helm": {"ui": {"alarm_takeover": true, "alarm_page": 1, "swipe": true,
  "header": true, "night": "schedule", "night_brightness": 15, "dim_after_s": 120}}}
```

**Touch.** Every page has a transparent full-screen touch object above the
content. Without the new keys a tap anywhere is an `on_click` that shows the
next page, as before. With `swipe`, `night` (other than `off`) or `dim_after_s`
the object uses `on_short_click` instead, because LVGL also sends a click after
a long press and a night toggle must not turn the page. `swipe` adds
`on_swipe_left` and `on_swipe_right` (explicit page ids, wrapping around the
first and last page). `night` adds `on_long_press`, which switches night on or
off (also on a one-page display, which then gets a touch object without paging
actions). A press that only wakes a dimmed screen, and a gesture that already
acted, set a flag that makes the rest of that touch do nothing, so waking the
display never turns the page and a swipe is never followed by a second page
change even if the touch controller also reports a click. The alarm banner sits
above this layer on the LVGL top layer and a tap on it still hides it.

**Header** (the Waveshare 2.8C has `"ring": {"header_y": 14, "header_h": 52,
"footer_y": 414}` in `displays/boards/registry.json`; a board without `ring`
cannot use `header`, and rectangular boards are not supported yet). The round
screen is narrow at the top, so the top row (y 14) holds the clock `HH:MM` and
three 10 px dots, the page title (the `!literal` page `title`, drawn in the small
font, up to 200 px wide) sits in a second row at y 40, and one 8 px dot per page
with the current page highlighted sits at y 414, below the 340 x 340 content
area (y 70-410). Nothing overlaps the content area or the alarm banner. The
dots, left to right, are Signal K (bridge reports `connected`), the bridge
(`bridge/status` `online`) and MQTT (the display's own connection): green is
good, red is bad and grey is unknown. The Signal K dot is retained data, so it is
only ever green while the bridge is online and MQTT is connected; a stopped
bridge shows grey, never a stale green. When `displays.json` has any `ha` item a
fourth dot (Home Assistant, 14 px to the right of the MQTT dot) follows the retained
`bridge/homeassistant`: green `connected`, red `disconnected`, grey `disabled` or not
yet known; like the Signal K dot it is only coloured while the bridge is online. Without
`ha` items nothing is generated for it. Page titles are checked against the
bundled font like labels (`validate` and `display render` report a glyph the
font lacks), but only when `header` is on.

**Clock source.** The display has no clock of its own and does not use SNTP:
offshore it may have no internet, and a second time source could disagree with
the boat. The bridge publishes the Pi's wall clock to the retained topic
`vesselstack/v1/bridge/clock`, `{"hm":"14:05","night":false,"ts":"2026-10-25T21:05:30Z"}`
(`hm` local `HH:MM`, `night` the schedule flag, `ts` UTC), when the minute or
the flag changes and every `heartbeat_s`, only if some display uses `header` or
`night: schedule`. The zone is `BOAT_TIMEZONE` (an IANA name, written to
`displays-bridge.env` by the installer when set; an unknown name is logged and
ignored) or, if absent, the Pi's system zone (`timedatectl`). The night window
compares wall-clock minutes, so a daylight saving change moves the window with
the clock. **The Pi's time must be right**: keep `chrony` (or `systemd-timesyncd`)
running and, without internet, feed it from a GPS/PPS source; a Pi that boots
with a wrong clock shows a wrong time and switches night at the wrong hour. The
firmware does not display the received string: it reads the two numbers, checks
them (hour 0-23, minute 0-59) and draws digits it formats itself. The clock
reads `--:--` when the message is older than 3 x `heartbeat_s`, the bridge is
offline, MQTT is down or the payload is malformed. The night flag is then held
at its last value (a dead bridge at night must not light up the cabin); use a
long press to override. The display ACL already allows reading `bridge/#` and
nothing else is written by the displays.

**Night.** `night` swaps the palette: tiles black, labels dim red-brown
(`0x6A1600`), values `0xB02400`, severity fills `0x2A0800` (ok), `0x7A3E00`
amber (warn) and `0xB00000` (alarm, with brighter text so it stays clearly red),
unknown stays black with dim text, and the header and page dots turn
red/amber. Nothing blue or green is used at night. The colours are applied by
guarded lambdas that run only when the state changes (the severity colours are
re-evaluated on every flip). A long press toggles night; with `schedule` that
is a manual override that lasts until the schedule flag next changes (the
next `start` or `end` time), with `manual` it lasts until the next
long press or reboot (nothing is stored; a reboot starts in day mode until the
bridge clock arrives). The alarm banner is never themed: it keeps the day red
and white at night, and an alarm sets the backlight to 100 % while the banner is
shown.

**Dimming.** One controller sets the backlight from, in this order: the alarm
banner (100 %), idle (`dim_after_s` seconds without a touch, using the LVGL
inactivity time polled four times a second) dims to `night_brightness` if you
set it, else to 10 %, never brighter than the current level; then night
(`night_brightness`); then day (`day_brightness`). It writes the backlight only
when the target changes. A touch restores the level at once, without paging.
LVGL is never paused, so alarms keep updating a dimmed screen (and, on a rising
alarm, light it at 100 % and show the banner). The backlight stays an
`internal` light: nothing is published.

**Cost on the dashboard example** (swipe, header, night schedule and dimming on
top of the alarm banner): about 16.7 kB more flash and 2.1 kB more RAM than the
previous dashboard example (flash 1131019 to 1148119 bytes, RAM 118491 to 120595
bytes in the ESPHome build report).

**To verify on hardware** (not verified yet, and the reason for the defensive
touch handling): that a swipe on the GT911 does not also deliver a click, how
reliable swipe left/right is on the round panel, the 400 ms default long-press
time, readability of the night colours in a dark cabin, backlight steps with
the LEDC PWM at 5-20 % and that the clock follows the Pi. The only bridge-side
change is the new retained topic; older firmware ignores it.

**Upgrade and rollback.** Update VesselStack, restart the bridge (the installer
does and passes `BOAT_TIMEZONE`), add the keys to the display and
`night_schedule`, then `display build ID` and `display flash ID`. To go back,
remove the `ui` keys and `night_schedule`, rebuild and reflash (or flash the
previous firmware); the extra topic is harmless to older displays.

### The `status-grid` layout

`status-grid` shows 1-9 instruments, notifications and Home Assistant items, one cell each, inside
the board's content area (340 x 340 px on the Waveshare 2.8C) with 6 px gaps:

| Items | Grid (columns x rows) | Cell size |
| --- | --- | --- |
| 1-4 | 2 x 2 | 167 x 167 px |
| 5-6 | 3 x 2 | 109 x 167 px |
| 7-9 | 3 x 3 | 109 x 109 px |

Every cell has the item label (small font). A notification cell shows one of
the fixed words **OK**, **WARN**, **ALARM** or **--** in a bold font and its
background follows the severity (grey when stale, unknown or offline). An
instrument cell shows the value and unit as on the other layouts and is
coloured by its severity if it has `thresholds`, otherwise the plain tile
colour. A binary or state Home Assistant cell shows its own fixed text and severity
colour (see [Home Assistant items](#home-assistant-items)); a numeric one is an instrument cell.
All text is fixed in the firmware; labels are `!literal` scalars and no
text is built from MQTT data. Words are shown in a 48 px bold font in 2 x 2
grids and 30 px otherwise. Nothing was measured on the hardware yet: long
labels are clipped with an ellipsis.
[`displays/displays.dashboard.example.json`](../displays/displays.dashboard.example.json)
has a `Status` page. A rebuild and reflash (`display build` and `display flash`)
is needed; firmware built before this release ignores notification topics.
Rollback: remove the `status-grid` pages and the `notifications` and reflash,
or flash the previous firmware.

### The `tank-bars` and `gauge-arc` layouts

Two layouts draw a reading as a level instead of digits only. Every instrument
on them needs `min` and `max` (in display units, after `convert`); the check
names the item and the layout (`instrument 'fuel' needs min and max on layout
'tank-bars'`).

| Layout | Items | Boards | What is drawn | Required |
| --- | --- | --- | --- | --- |
| `tank-bars` | 1-4 instruments | round and rect | 1-4 vertical bars side by side in the content area (340 x 340 px on the Waveshare 2.8C): the label above (small font, `!literal`, clipped with an ellipsis), a 60 px wide, 210 px high bar and the value with its unit below (36 px bold digits, unit in the small font). The columns share the width evenly (85 px each for four bars). | `min` and `max` on every item |
| `gauge-arc` | 1 instrument | round only | One 270 degree arc, 24 px wide and 440 px across (screen size minus 20 px on each side), with the value in the middle (the 96 px digits of `single`), the label above it and the unit below it. Optional zone arcs, 6 px wide just inside the track, show the warn (amber) and alarm (red) ranges of the instrument's `thresholds`. | `min` and `max`; `thresholds` optional |

On a rectangular board `gauge-arc` is refused: `gauge-arc needs a round board;
use single`.

- **Scaling.** A bar has the range 0..100: `round(100 * (v - min) / (max - min))`,
  clamped to 0..100 and rounded half away from zero (`lroundf`). The arc has the
  range 0..1000 on the same formula, so one tenth of a percent moves it. The
  firmware reads the reading as a number from the payload (`v`); nothing is
  parsed from text and no text is built from MQTT data. The text shows the
  value as the other layouts do, with the instrument's `precision` and `unit`
  (`62%`). Use `precision` 0 or 1 on narrow bars: the value row is clipped to
  its column, and with four bars (85 px) `100%` or `62.5%` should fit (estimated from the font size, not measured on the panel).
- **Colour.** The bar or arc indicator follows the severity of the instrument
  with the same freshness gate and guarded repaint as the tiles: ok green
  `0x1E7B34`, warn amber `0xF2A900`, alarm red `0xC62828`, unknown grey
  `0x37474F`. An instrument without `thresholds` has no severity, so a fresh
  reading is drawn neutral blue `0x2E86C1` (never green).
- **Stale, offline or unknown.** When the bridge is offline, MQTT is
  disconnected, `st` is not `ok` or nothing arrived within the staleness
  threshold, the bar or arc goes to 0, the indicator turns grey and the text
  is `--` (the unit stays). It never shows the last value and is never green.
- **Zone arcs.** Angles are computed when the firmware is generated, from
  `thresholds`, `min` and `max`: `alarm_below` is red from `min`, `warn_below`
  amber up to it (from `alarm_below` when both are set), `warn_above` amber from
  it (up to `alarm_above` when both are set) and `alarm_above` red up to `max`.
  Zones outside the range, or shorter than one degree, are not drawn. They are
  static (hysteresis is not shown).
- **Angles.** LVGL measures angles clockwise from 3 o'clock. The arc normally
  runs from 135 to 45 degrees (left of the bottom, over the top, to the right
  of the bottom) and leaves a 90 degree gap at the **bottom**. With `ui.header`
  the header sits at the top of the ring, so the arc runs from 315 to 225
  degrees and the gap is at the **top**: the value then grows clockwise from the
  upper right, round the bottom, to the upper left. The arc ends are about 85
  px from the top of the screen, below the header band (y 14 to 66); a
  unit test checks that the arc band does not touch the clock, status dots,
  title or page dots. The bars only use the content area, so they never meet
  the header.
- **Night.** With `ui.night` the labels, value text, bar and arc tracks, zone
  arcs and indicator colours switch to the red/amber night palette through the
  same guarded repaint as the tiles (no blue; the neutral colour becomes dim
  amber).
- **Signal K tank paths.** Signal K publishes tanks as
  `tanks.<type>.<id>.currentLevel`, a **ratio from 0 to 1**, for example
  `tanks.fuel.0.currentLevel`, `tanks.freshWater.0.currentLevel`,
  `tanks.blackWater.0.currentLevel` (also `wasteWater`, `lubrication`, `liveWell`,
  `baitWell`, `ballast`, `gas`). Use `"convert": "ratio->%"`, `"unit": "%"`,
  `"min": 0`, `"max": 100`. To find the paths on your boat open the Signal K
  admin UI (Data Browser, filter `tanks`) or read
  `http://<pi>:3000/signalk/v1/api/vessels/self/tanks`; the `<id>` is whatever
  your tank sensor plugin or N2K gateway reports (`0`, `1`, ... or a name). If
  you prefer litres, use `currentVolume` with `m3->L` and set `max` to the tank
  capacity.
- **Cost.** On the dashboard example (a Tanks page with three bars and a Fuel
  arc with two zones added to the previous example, which had a grid page
  instead) the ESPHome build report went from 1148119 to 1183939 bytes of flash
  (+35.8 kB: the 36 px font, the LVGL bar and arc code) and from 120595 to
  121739 bytes of RAM (+1.1 kB).

[`displays/displays.dashboard.example.json`](../displays/displays.dashboard.example.json)
has a `Tanks` page (`tank-bars`: fuel, water and waste, with thresholds fuel
`warn_below` 25 / `alarm_below` 10, water `warn_below` 20, waste `warn_above` 75
/ `alarm_above` 90) and a `Fuel` page (`gauge-arc`). To use them: add the
instruments with the right `path`, `convert`, `min` and `max`, put them on a
page, run `vesselstackctl display validate`, then rebuild and reflash
(`display build ID`, then `display flash ID --ota HOST` or `--serial DEVICE`);
firmware built before this release cannot draw these layouts. Rollback: change
the pages back to another layout (or remove them), rebuild and reflash, or flash
the previous firmware (`display flash ID --previous ...`). Not verified on the
hardware yet: how the bars and the arc look on the round panel, the column
widths with long units, and the arc ends near the header.

### The `electrical` layout

One page for the house battery on the round board: the state of charge as the
outer ring, the voltage in big digits in the middle, then the SoC, the current
with a fixed `CHG`/`DSG` word, an optional shore power pill and an optional
solar line. `items` map **by position** to the slots below, so the order is
fixed; the error names the role (`pages[2].items[1] (soc): instrument 'batt_soc'
needs min and max on layout 'electrical'`).

| Position | Role | Accepts | Needs | Shown as |
| --- | --- | --- | --- | --- |
| 1 | `voltage` | instrument | - | Label above, value in the 56 px bold digits of `grid-2x2`, unit beside it |
| 2 | `soc` | instrument | `min` and `max` | The 440 px outer ring (the `gauge-arc` ring: same arc, zones, colours and angles) and a small line `label value unit` |
| 3 | `current` | instrument | - | Small line `label value unit` plus the word `CHG` or `DSG` |
| 4 (optional) | `shore` | instrument or notification | - | A pill at the bottom of the text column |
| 5 (optional) | `solar` | instrument | - | Small line `label value unit` |

A page has 3, 4 or 5 items. Optional slots are trailing: a `solar` item needs a
`shore` item before it (use a shore notification or instrument even if you only
want to see the solar line). The layout is round only (`electrical needs a
round board; use grid-2x2` on a rectangular board). It is never chosen as the
default page of `display add`.

- **Signal K paths.** `electrical.batteries.<id>.voltage` (volts, `convert`
  `none`, `unit` `V`, `precision` 2), `electrical.batteries.<id>.capacity.stateOfCharge`
  (a **ratio 0 to 1**: `convert` `ratio->%`, `unit` `%`, `min` 0, `max` 100) and
  `electrical.batteries.<id>.current` (amperes, `none`, `unit` `A`, `precision`
  1). `<id>` is what your battery monitor plugin or N2K gateway reports (`house`,
  `0`, ...); check the Data Browser in the Signal K admin UI (filter
  `electrical`). Solar controllers usually publish
  `electrical.solar.<id>.current`. For a shore pill see below.
- **SoC ring.** The ring is the `gauge-arc` ring of the SoC instrument: the
  indicator takes the severity colour of its `thresholds` (green, amber, red),
  is neutral blue when it has none (never green) and the static zone arcs
  follow the thresholds. With `ui.header` the gap moves to the top (arc 315 to
  225 degrees) so the header and the page dots stay clear, as on `gauge-arc`.
  Only the SoC colours the ring: thresholds on the voltage, current or solar
  instruments still feed the alarm summary and banner but do not colour this page.
- **Voltage and text column.** The text column is 300 px wide and 188 to
  198 px high (3 to 5 items), centred on the screen and inside the free circle
  of the ring (188 px radius); a unit test checks the corners against the circle
  and the header and page-dot areas. The voltage uses the 56 px digits (the
  font `grid-2x2` uses; a display with an `electrical` page gets it). Fonts,
  glyph sets and the other layouts are otherwise unchanged. Estimated from font sizes, not measured on the panel.
- **Current and sign convention.** Signal K reports the battery current as
  positive while **charging** and negative while discharging; the value is shown
  with a minus sign when negative. The word next to it is `CHG` above +0.05 A,
  `DSG` below -0.05 A and empty inside that band (and when stale). It is built
  from three fixed `!literal` branches of one freshness-gated state, never from
  text. If your shunt is wired the other way, set `"convert": "negate"` on the
  current instrument: the bridge flips the sign before rounding (never `-0.0`),
  so the number, `CHG`/`DSG`, any `thresholds` and `alarm` all use the corrected
  sign. `negate` works on any instrument.
- **Shore pill.** With a **notification** (for example
  `notifications.electrical.shorePower`, which you create with a Signal K rule or
  plugin; the path in the example is a placeholder) the pill is coloured by
  severity and shows `OK`, `WARN`, `ALARM` or `--` exactly like a `status-grid`
  cell; use `"absent": "unknown"` unless a missing notification really means
  shore power is fine. With an **instrument** (any number, for example a shore
  current or power in amperes) the pill reads `ON` (green) when the value is
  above 0.5, `OFF` (neutral blue) otherwise and `--` (grey) when stale; shore
  power being absent is not an alarm. The pill only shows these fixed words and
  the item's label is the text beside it.
- **Stale, offline or unknown.** Every number blanks to `--` as on the other
  layouts, the current word is empty, the ring is empty and grey, and the pill
  is `--` and grey. The words and colours are derived from the same freshness
  gate as the severity colours, so a stale reading never shows `CHG`, `DSG`,
  `ON`, `OK` or green.
- **Night.** With `ui.night` the text, the ring track, the zone arcs and the
  pill follow the red/amber night palette through the same guarded repaint as
  the other layouts.
- **Cost.** Adding the Electrical page to the dashboard example (compared with
  the same example without it) changed the ESPHome build report from 1183939
  to 1199131 bytes of flash (+15.2 kB) and from 121739 to 123835 bytes of RAM (+2.1 kB).

[`displays/displays.dashboard.example.json`](../displays/displays.dashboard.example.json)
has an `Electrical` page: `house_v` (the voltage the Nav page already used),
`batt_soc` (thresholds `warn_below` 30 / `alarm_below` 20), `batt_a`, the
`shore` Home Assistant binary item (`alarm: false`, so a lost shore connection does not
raise the takeover banner; the entity is an example, change or remove it) and
`solar_a`. To use it: add the instruments with the right `path`, `convert`,
`min` and `max`, put them on a page in this order, run `vesselstackctl display
validate`, then rebuild and reflash (`display build ID`, then `display flash ID
--ota HOST` or `--serial DEVICE`); firmware built before this release cannot
draw the layout. Rollback: remove the page (or change its layout), rebuild and
reflash, or flash the previous firmware (`display flash ID --previous ...`).
Not verified on the hardware yet: how the page looks on the round panel, the
row spacing and the 56 px digits against the ring.

### Home Assistant items

Home Assistant (HA) entities, such as a bilge pump switch, a high water sensor, the
shore power state or a summary sensor you built in HA, can be shown next to Signal K
data. The bridge keeps one websocket open to HA, reads only the entities named in
`ha`, and publishes a severity and a small index per item, never the state text. This
is an optional extra: Signal K instruments and notifications work whether or not HA is
reachable. It has been verified against a fake HA server in the integration tests, not
yet against a real Home Assistant.

**Create the token.** In HA create a dedicated user (Settings, People, Users), sign in
as that user and create a long-lived access token (Profile, Security, Long-lived
access tokens). **HA has no per-entity permissions:** a long-lived token is not
read-only and not limited to the entities you list in `ha`. A leaked token can read
every entity and call every service, so it can operate every device in your HA (locks,
switches, alarm panels). A non-admin user only prevents configuration changes
(users, integrations, add-ons); it does not restrict devices. Keep the token on the
Pi (root-only), never reuse it elsewhere, and revoke it in HA (Profile, Security) if it
leaks. The bridge itself only reads: it subscribes to the configured entities and never
calls a service.
Then set it, with the URL if HA is not on the Pi:

- `DISPLAYS_HA_TOKEN` in `/opt/vesselstack/config/vesselstack.env`, the Control Panel
  (Displays section, hidden field) or the terminal wizard (advanced settings, hidden
  input); and `HOME_ASSISTANT_URL` (default `http://127.0.0.1:8123`; `https://` is
  used as `wss://`, credentials in the URL are refused).
- **Cleartext warning.** The token is sent in the first websocket message. Over
  `http://` (`ws://`) to a host that is not loopback (`localhost`, `127.0.0.0/8`,
  `::1`) it crosses the network unencrypted; the installer preflight
  (`displayctl.py preflight`, `--dry-run` too) warns about it, without failing. Use
  `https://`, or keep HA on the Pi. `wss://` verifies the certificate against the
  system trust store; there is no custom-CA option. For a self-signed HA, point the bridge
  at HA on loopback or add the CA to the Pi's system store.
- **Redirects are refused.** The HA (and Signal K) websocket upgrade does not follow HTTP
  redirects (websockets 17.3 would follow them, even to another host, which would
  hand the token to that host); point `HOME_ASSISTANT_URL` at the final address.
- Re-run the installer (or Control Panel Apply). The token is written only to the
  root-only 0600 `config/displays-bridge.env` (included in backups, like the Signal K
  token) and sent once in the websocket `auth` message; the bridge never logs it,
  and removes it from any log text. With no token the bridge logs one warning,
  does not connect, and the items show `--`.

**Configure the items.** A top-level `ha` object in `displays.json`; ids follow the
instrument id rules and are unique across instruments, notifications and `ha`:

```json
"ha": {
  "bilge": {"entity_id": "binary_sensor.vesselstack_bilge_pump", "label": "Bilge", "kind": "binary",
            "states": [{"match": "on", "sev": "warn", "text": "RUNNING"},
                       {"match": "off", "sev": "ok", "text": "OFF"}]},
  "engine": {"entity_id": "sensor.engine_alarm_status", "label": "Engine", "kind": "state",
             "states": [{"match": "all clear", "sev": "ok", "text": "CLEAR"}],
             "default": {"sev": "alarm", "text": "ALARM"}},
  "watch": {"entity_id": "sensor.boat_watch_summary", "label": "Watch", "kind": "state",
            "states": [{"prefix": "anchor: clear", "sev": "ok", "text": "CLEAR"}],
            "default": {"sev": "warn", "text": "CHECK"}},
  "cabin_t": {"entity_id": "sensor.cabin_temperature", "label": "Cabin", "kind": "numeric",
              "unit": "C", "precision": 1, "thresholds": {"warn_above": 30, "alarm_above": 40},
              "min": 0, "max": 50, "alarm": false}
}
```

- `entity_id` (`domain.object_id`), `label` (1-16 printable characters in the
  bundled font), `kind` (`binary`, `state` or `numeric`) and `alarm` (boolean,
  default true: whether the item counts in the [alarm
  summary](#alarm-summary-and-takeover)) are common.
- `binary` and `state` items list 1-8 `states`; each has exactly one of `match` (the
  whole state) or `prefix` (the start of it), a `sev` (`ok`, `warn` or `alarm`) and a
  `text` of 1-10 printable characters shown in the cell. Comparison ignores case and
  surrounding spaces, and the **first** matching entry wins. A state no entry matches
  takes `default` (`sev` and `text`). `state` requires `default`; a `binary` item
  without one shows `?` in amber (never green by accident). The `text` and `label` are
  checked against the bundled font like other labels.
- `numeric` items work like instruments: `unit` and `precision` are required, with
  optional `thresholds`, `min` and `max` ([Thresholds and severity
  colours](#thresholds-and-severity-colours)); the state must parse as a finite
  number. They are accepted on every layout that accepts instruments (a bar or an arc
  also needs `min` and `max`) and show the value and unit with threshold colours.
- **Unknown values.** A state of `unknown`, `unavailable`, `none` or empty (and an
  entity HA does not know, or a number that does not parse) is *missing*: the cell
  shows grey `--` and the severity is `unknown`; those words cannot be used in
  `match` or `prefix`. An unmatched state is not missing: it takes `default`.
- Freshness: while the bridge is connected to HA the item is `ok` (or `missing`);
  when the connection drops every item becomes `stale` and everything heard is
  forgotten. A display greys a cell after 3 x `heartbeat_s` without a message, when the
  bridge is offline or when MQTT is disconnected, like notifications.
- Binary and state items are only accepted on `status-grid` and, for `binary` items, in
  the `shore` slot of an `electrical` page, where the text appears in the pill. The
  validator names the item and the layout otherwise.

Home Assistant state strings are never displayed. The firmware only shows the
`label` and the compile-time `text` that the item's index `k` selects (one generated
`if` per index; `-1` is the default text), or a number for numeric items. Texts and
labels are `!literal` scalars. Keep texts short: on a 3-column `status-grid` about 7 characters fit before they are
clipped (not measured on the hardware). A cell, or the shore pill, uses the same
guarded green/amber/red colours as notifications (day and night palettes), grey when
unknown, stale or offline.

**Topics.** Each item is published retained, at most once a second when only a number
changes and every `heartbeat_s`, to `vesselstack/v1/ha/<id>` as
`{"v":null,"k":0,"u":"","st":"ok","sev":"warn","ts":"...","age":1.2}`: `v` is the
number of a numeric item (else `null`), `k` the index of the matched `states` entry
(`-1`: the default; `null` for numeric and missing items), `st` is `ok`, `missing` or
`stale`, and `ts` is the bridge's time of the last state received. The retained
`vesselstack/v1/bridge/homeassistant` is `connected`, `disconnected` or `disabled`
(the token is missing or HA rejected it). The ACL needs the new `vesselstack/v1/ha/#`
rule (see [MQTT access control](MQTT.md)). The bridge uses HA's
`subscribe_entities` and falls back to `get_states` plus `state_changed` events on an
HA that lacks it; it reconnects with a 1-30 s jittered backoff, and after
`auth_invalid` retries every 5 minutes.

**Limits and flooding.** One HA websocket message may be at most 2 MiB (larger frames
close the connection and it reconnects). `subscribe_entities` only sends the entities
named in `ha`, so this is ample; the `get_states` fallback (an HA from before 2022 that lacks
`subscribe_entities`) returns every entity of the installation, so a very large
installation can exceed 2 MiB there and will not connect: upgrade HA to use
`subscribe_entities`. The client keeps only the latest state per configured entity
(a repeated or flapping entity, or a hostile frame, cannot grow memory or the work per
pass) and the bridge applies the queued states once per 0.25 s pass and re-evaluates the
alarm summary once per pass. A state that came and went inside one pass is not published
separately; the item topic, the summary and the takeover always show the current state.
The systemd unit sets `MemoryHigh=96M` and `MemoryMax=128M` so a runaway bridge is
restarted instead of starving the Pi.

**One item can make the boat-wide summary `unknown`.** An item counts in the
[alarm summary](#alarm-summary-and-takeover) unless it has `"alarm": false`. While HA
is down or restarting, or when `DISPLAYS_HA_TOKEN` is missing or rejected, every
counted HA item is `unknown`, so the summary `max` is at least `unknown` (never
`ok`). Set `"alarm": false` on informational items (cabin temperature, weather,
health) so an HA restart does not change the boat-wide state; keep the default for items
you want a dead feed to flag.

**What changes.** Opt-in per item. The bridge process gains one outbound websocket
to HA and one thread; Mosquitto's ACL gains `ha/#` (applied by the next installer or
`vesselstackctl display sync-mqtt`). Display firmware must be rebuilt and reflashed
(`display build`, `display flash`) to show `ha` items; firmware built earlier ignores
them. The dashboard example adds seven items (bilge, high water, engine, shore, watch,
weather, health) and reworks the Status page; on it the build report went from
1199131 to 1212499 bytes of flash (+13.4 kB) and from 123835 to 127227 bytes of RAM
(+3.4 kB) relative to the Electrical-page example. How the cells look on the panel is
not verified on the hardware.

**Troubleshooting.** `sudo vesselstackctl logs displays` shows `Home Assistant
connected (entities)` (or the fallback line) when it works. `bridge/homeassistant`
`disabled` with "rejected the access token" means the token is wrong, revoked or its
user was deleted: create a new token and re-run the installer; `DISPLAYS_HA_TOKEN is
empty` means it was never set. `disconnected` repeating means HA is unreachable at
`HOME_ASSISTANT_URL` (check it from the Pi; the bridge runs on the host, so
`127.0.0.1` is right only when HA listens there). An item stuck on `--` with the bridge
`connected`: the `entity_id` is wrong, not visible to the token's user, or its state is
`unknown`/`unavailable`. Close reasons and exception text from HA are never logged (class name and numeric code only). An unexpected word means the state matched an earlier entry or
fell to `default`. Rollback: remove the `ha` items (or clear `DISPLAYS_HA_TOKEN`),
re-run the installer, rebuild and reflash, or flash the previous firmware; the `ha/#`
ACL rule is harmless.

### Icons

An instrument, a notification or a Home Assistant item may carry an optional
`"icon": "<name>"`, drawn next to its label. The name must be one of the Material
Symbols (Outlined) icons bundled with VesselStack; `displayctl.py validate`,
`render` and `build` reject anything else and list a few valid names. The bundled
names are the keys of `displays/esphome/fonts/icons.json` (name to Unicode code
point):

`air`, `anchor`, `battery_alert`, `battery_charging_full`, `battery_full`, `bolt`, `check_circle`, `directions_boat`, `electrical_services`, `engineering`, `error`, `explore`, `home`, `local_gas_station`, `location_on`, `navigation`, `notifications`, `oil_barrel`, `power`, `sailing`, `sensors`, `settings`, `solar_power`, `speed`, `thermostat`, `warning`, `water`, `water_drop`, `wifi`.

```json
"fuel": {"path": "tanks.fuel.0.currentLevel", "label": "Fuel", "icon": "local_gas_station", ...}
```

- **Placement.** `grid-2x2` and `single` tiles, `gauge-arc` and `electrical` rows
  show the icon (28 px) left of the label (an `electrical` page keeps the names
  aligned by reserving the icon column in every row once one item has an icon);
  `tank-bars` columns show it above the label (28 px); a `status-grid` of up to
  six items shows it above the label (40 px with 2 x 2 cells, 28 px with 3 x 2),
  a 3 x 3 grid (7-9 items) left of the label (28 px) with the label narrowed to
  the rest of the cell. Items without an icon render exactly as before.
- **Colour.** An icon has the colour of its label (grey by day, dark red in the
  night palette, repainted by the existing night script) and never changes with
  severity. Icons are static glyph labels written at render time (`!literal`
  Unicode escapes); nothing from MQTT is shown through them.
- **Firmware cost.** The font is only added to a display's project when an item on
  one of its pages has an icon (the render then also copies
  `MaterialSymbols-subset.ttf` and its license into the project and the export
  archive), with one 4 bpp font per size in use (`font_icons` at 28 px,
  `font_icons_lg` at 40 px) holding exactly the used glyphs. Without icons the
  generated files are byte-identical to earlier releases. `displays.dashboard.example.json`
  uses 18 different icons: the example firmware grows from 1212499 to 1221991 bytes of flash (+9492, about 9.3 kB) and from 127227 to 127459 bytes of RAM (+232).
- **Licence.** The font is a subset of Google's Material Symbols (Apache License
  2.0). `displays/esphome/fonts/LICENSE-MaterialSymbols.txt` carries the licence
  text and `THIRD_PARTY_NOTICES.md` the source (the upstream commit) and the
  modifications.
- **Regenerating the subset.** `scripts/build-icon-font.py` rebuilds
  `MaterialSymbols-subset.ttf` and `icons.json` from the upstream variable font
  (one static instance: FILL 1, wght 500, GRAD 0, opsz 48, then subset to the
  names listed in the script). It needs `fontTools` in a throw-away virtual
  environment and the upstream `MaterialSymbolsOutlined[FILL,GRAD,opsz,wght].ttf`
  and `.codepoints` files from the commit named in `THIRD_PARTY_NOTICES.md`; it is
  not run at install time or in CI, and the tests only check that every code
  point in `icons.json` is in the committed font. To add an icon, add its name to
  `ICONS` in the script, run it, and commit the three regenerated files.
- **Limits and rollback.** Hardware appearance (legibility of the 28 px icons,
  alignment in the round corners) is not verified on a device. Firmware must be
  rebuilt and reflashed to show icons. Rollback: remove the `icon` keys, rebuild
  and reflash, or flash the previous firmware.

### Themes

Display colours and tile shape come from `displays/themes.json`, which is installed with the rest
of `displays/`. It holds four themes. Choose one per display with `ui.theme`
(`sudo vesselstackctl display set-theme ID THEME`; list them with `sudo vesselstackctl display themes [--json]`):

| Theme | Look | Day contrast (value / label on tile) |
| --- | --- | --- |
| `classic` | The original: slate `#101820` tiles on a black page, pale grey labels. The default. | 17.9 / 7.2 |
| `high-contrast` | For direct sunlight: black page and tiles, pure white values, light `#D0D8E0` labels, a 2 px light tile border, stronger severity colours. | 21.0 / 14.6 |
| `daylight` | Light `#E4E8EC` page with white tiles and dark text; severity tiles use pastel green, amber and red with dark text. A 1 px grey tile border separates the tiles. | 17.9 / 9.8 |
| `ocean` | Near-black navy page with navy `#12305A` tiles, blue-grey `#8FA8C4` labels and white values. | 13.2 / 5.4 (severity tiles are darker than in `classic`, with white text) |

Night mode is the same on every theme in this version: the red night palette of `classic`, so night
vision survives whatever the day theme. A themed display repaints to it when night starts, including
the page background of the light themes and the tile border (which turns dark red at night, so a
bright border never defeats night mode). The alarm banner is never themed.

**Applying a theme.** `set-theme` only edits `displays.json`. The theme is compiled into the
firmware: run `display build ID` and `display flash ID ...` for the display to change. Until then the
device keeps its old colours (and `flash` refuses a stored firmware built from a different
configuration, as for any other change). A display on `classic`, with or without `"theme": "classic"`,
generates a byte-identical device file to the one it generated before themes existed
(`tests/fixtures/displays/golden/` is checked for both), so such a display never needs reflashing for
this feature.

**Cost.** A theme only changes constants in the generated code. `classic` emits nothing new; a theme with a tile border adds one
`lv_obj_set_style_border_color` call per tile to the night script, and a theme whose page background
differs between day and night (`daylight`, `ocean`) adds one `lv_obj_set_style_bg_color` call per page.
Measured with a full `esphome compile` (pinned image 2026.10.0, `displays.dashboard.example.json`,
every layout, header and night schedule): static RAM is identical for all four themes (127,459 bytes,
37.3 %), and flash grows by 616 bytes for `daylight`, 552 for `high-contrast` and 248 for `ocean`
over `classic` (1,221,991 bytes, 15.0 % of the app partition). Not measured on a device.

A theme has a `description`, `day` and `night` blocks of roles (`page_bg`, `tile`, `label`, `value`, `clock`,
`title`, `dot_ok`, `dot_bad`, `dot_unknown`, `page_hi`, `page_lo`, `track`), `sev` and `gauge`
palettes for each of day and night (`unknown`, `ok`, `warn`, `alarm`, plus `neutral` for gauges;
`sev` entries are `[tile background, text]` pairs), and a `shape` block (`tile_radius`, `tile_pad`,
`tile_border_width`, `tile_border`; no border colour is written while the width is 0). Colours are
`"#RRGGBB"`. The alarm banner and its white text are deliberately not themed, and fonts are not part
of a theme.

`displays/schema.py` (`load_themes`, `validate_themes`) rejects unknown keys, bad colour strings and
missing roles, and enforces: stale/unknown colours are never green (hue 75-165 degrees unless nearly
grey); night colours are red to amber (hue 0-50 degrees, red the strongest channel) or near-black;
day `ok`, `warn` and `alarm` are green, amber and red; and, for the day palette, value text on the
tile is at least 4.5:1, label text at least 3:1 and each severity pair at least 4.5:1 (3:1 for the
muted `unknown` pair; WCAG contrast ratio). The theme's label colour is also drawn on severity tiles,
so every theme except `classic` needs the day label to reach 3:1 on each day severity
tile background (`classic` is 2.13 / 1.25 / 2.25 on ok / warn / alarm and is the only exemption, unchanged; `ocean` uses
deep tiles with white text: ok `#145A27` 3.40 (white 8.33), warn `#6E4900` 3.27 (8.03), alarm `#9E1B1B` 3.26 (8.00),
unknown `#3A5272` 3.26 (text `#E6EEF7` 6.82), label contrast first, text contrast in brackets). The dim night palette is not held to the contrast floors.
Every built-in theme passes these rules; a custom `themes.json` is not supported (the file is part of
the installed code and is replaced on update).

**Limits.** Colours were checked by the validator and by generating and validating (`esphome config`)
firmware for every theme, not by looking at a device; real-panel legibility, in particular of the
light themes in sunlight and the pastel severity tiles, is not verified. The label colour is not
changed by severity, so on severity tiles it keeps the theme's label colour: readable on the
`daylight` and `high-contrast` tiles, but weak on the saturated green, amber and red tiles of
`classic` (the amber one is about 1.2:1; the large value text stays black or white and
readable). The `high-contrast` warn tile was a bright amber that made its label unreadable; it is now
dark amber `#8A5A00` with white text.

**Upgrade and rollback.** Nothing changes until you run `set-theme` and reflash. To go back, run
`set-theme ID classic` (it removes the key), then `display build ID` and `display flash ID ...`, or
flash the previous firmware (`display flash ID --previous`). **Run `set-theme ID classic` on every
display before downgrading VesselStack to a version without themes**: those versions reject the
`theme` key as unknown (checked against 0.6.1: `$.displays.helm.ui.theme: unknown key`), so the configuration
is invalid and the displays bridge exits at start-up with status 2, which its systemd unit does not restart
(`RestartPreventExitStatus=2 3`). A backup from `displays-backups` taken before the theme was set is another way back.
Restoring only `displays/themes.json` and `displays/render.py` is not enough for the same reason.

### Board registry schema 2

`displays/boards/registry.json` uses `schema` 2, where each layout is an
object `{"capacity", "min_items", "shapes": ["round","rect"], "types":
["instrument","notification","ha"]}` (the allowed item types). `schema` 1
registries (a layout is just an integer capacity) are still accepted and mean
`min_items` 1, both shapes and instruments only. A layout may also have `slots`
(used by `electrical`): a list with one `{"role", "types", "needs", "optional"}`
entry per position of `items` (`needs` from `numeric`, `min_max`; `optional`
slots are trailing; `capacity` must equal the number of slots and `min_items`
the number of required ones), which the validator turns into the per-role errors. A board may only list layouts
that support its `shape`. A board may also have an optional `ring`
(`{"header_y", "header_h", "footer_y"}`, pixels): the bands that `ui.header` uses.
The header band (at least 50 px high) must end above the content area and the
10 px page indicator must lie between the content area and the bottom edge;
the registry validator checks it. `vesselstackctl display add` still picks the largest
layout that takes only instruments for the default page (`tank-bars`,
`gauge-arc` and `electrical` are listed after `grid-2x2` so they never win a tie; they need
`min` and `max` or slots); `status-grid` is chosen only when nothing else fits.

## MQTT topics and freshness

Values are published retained to `vesselstack/v1/instruments/<instrument_id>`
as compact JSON: `{"v":6.4,"u":"kn","st":"ok","ts":"2026-01-01T00:00:00.000Z","age":0.4}`.
`v` is converted, then rounded to `precision`, and is `null` unless `st` is
`ok`. `ts` is the Signal K timestamp (or `null`); `age` is seconds since the
bridge received the value, measured on the bridge clock, not the Signal K
timestamp. Other reserved topics are `vesselstack/v1/bridge/status`,
`vesselstack/v1/bridge/signalk`, `vesselstack/v1/bridge/clock` (see
[Touch, header, night mode and dimming](#touch-header-night-mode-and-dimming)) and
`vesselstack/v1/displays/<username>/status`.
Notifications use their own retained topics, `vesselstack/v1/notifications/<id>`
(see [Notifications](#notifications)), Home Assistant items `vesselstack/v1/ha/<id>` (see
[Home Assistant items](#home-assistant-items)) and `vesselstack/v1/bridge/homeassistant`; the combined alarm state is
`vesselstack/v1/alarms/summary` (see [Alarm summary and takeover](#alarm-summary-and-takeover)).

- `sev` (optional): instruments that have `thresholds` also publish
  `"sev":"ok"|"warn"|"alarm"|"unknown"`, placed after `st` (for example
  `{"v":12.1,"u":"V","st":"ok","sev":"warn","ts":"...","age":0.4}`). It is
  `unknown` whenever `st` is not `ok`. Instruments without `thresholds` have no
  `sev` field and their payloads are byte-for-byte unchanged, so this is
  backward compatible: firmware built before this feature ignores `sev` and
  keeps working, but new firmware (a rebuild and reflash) is needed to show the
  colours. A change of `sev` is published like a value change (at most once per
  `min_interval_ms`).
- `ok`: a value was received within `stale_after_s`.
- `stale`: nothing received for more than `stale_after_s`; published
  immediately on the transition, with `v` null.
- `missing`: no value has been received since the bridge started.
- A changed value is published at most once per `min_interval_ms`; unchanged
  values are not republished except by the `heartbeat_s` heartbeat, which also
  refreshes `age`. Null, non-numeric, non-finite and boolean Signal K values are
  ignored, as are values whose magnitude exceeds 1e15 after conversion.

## Supported boards

| Board | Screen | Layouts | Status |
| --- | --- | --- | --- |
| Waveshare ESP32-S3-Touch-LCD-2.8C (`waveshare-esp32-s3-touch-lcd-2.8c`) | 480x480 round, 70 px safe inset, 340x340 content area, header and footer bands (`ring`) | `single`, `grid-2x2`, `status-grid`, `tank-bars`, `gauge-arc`, `electrical` | Config validated and compiles with ESPHome 2026.10.0 (`esphome config` in CI); builds on a laptop or, optionally, on the Pi (`display build`, `display flash`; verified on real hardware: colours, orientation, safe area, touch and Signal K to screen end to end after `display build` and `display flash --serial`; OTA, long-term stability and `.local` resolution not yet verified; see the [bring-up checklist](#hardware-bring-up-checklist-waveshare-esp32-s3-touch-lcd-28c)) |

`list-6` is defined but not offered on the round board: `validate_registry` rejects any board whose `layouts` lists a layout the firmware generator cannot render (`schema.SUPPORTED_LAYOUTS`, shared with `render.py`).

**Adding a board versus adding a layout.** A new board is data only: an entry in `displays/boards/registry.json` that lists already supported layouts and its ESPHome package. A new layout is code: teach `displays/render.py` to draw it and add its id to `SUPPORTED_LAYOUTS` in `displays/schema.py`, otherwise the registry is refused.

**Rounding.** Instrument and Home Assistant numeric values use the same rounding (half away from zero, never `-0.0`, an integer at `precision` 0, a value beyond 1e15 is treated as missing). A heading (`rad->deg_360`) that rounds up to 360 is shown as 0. A numeric Home Assistant item at `precision` 0 is published as an integer (`3`, not `3.0`) in the MQTT payload.

## Planned limitations

- Version 1 uses plaintext MQTT on the local network; it provides no
  confidentiality or strong authentication against other devices on that
  network.
- With the VesselStack firewall enabled, displays must be on a trusted
  interface that can reach the Raspberry Pi's MQTT port.
- Building on a laptop is the primary documented path; building and flashing on the
  Pi (`display build`, `display flash`) is optional.
  Only the `single`, `grid-2x2`, `status-grid`, `tank-bars`, `gauge-arc` and `electrical` layouts are generated.
- The runtime `displays.json`, `displays.secrets.json`, ESPHome
  `secrets.yaml`, `.esphome/` and `displays/build/` output, `$VESSELSTACK_ROOT/build/` and firmware
  binaries (`.bin`, `.elf`, `.uf2`) are git-ignored and rejected by
  `scripts/check-public-tree.py`; never commit them.
