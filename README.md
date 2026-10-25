# VesselStack

Self-hosted monitoring, automation, and diagnostics for connected boats.
VesselStack targets Raspberry Pi OS and Debian-family boat computers and
combines Home Assistant, SignalK-compatible history, Grafana, MQTT,
infrastructure monitoring, and a local diagnostic assistant.

New build? Start with the [hardware bill of materials](docs/BOM.md), then follow
the illustrated [end-to-end installation and commissioning guide](docs/INSTALLATION.md).
It covers Raspberry Pi 5 assembly, marine power, isolated NMEA 2000/SocketCAN,
backbone connection, acceptance tests, backup, and rollback.

## Guided first installation

After installing the OS and Docker prerequisites from the end-to-end guide:

```bash
git clone https://github.com/jclima/VesselStack.git
cd VesselStack
git checkout v1.3.0
python3 vesselstack-wizard.py
```

Open `http://127.0.0.1:8088`, or use the SSH tunnel documented in the guide.
The dependency-free wizard binds only to loopback, shows relevant host
prerequisites, and writes a complete private `vesselstack.env`. It does not
change boot firmware, firewall rules, marine wiring, or start services.

> [!WARNING]
> VesselStack is not certified navigation, collision-avoidance, engine-control,
> alarm, or life-safety equipment. Maintain independent marine instruments and
> alarms. Never depend on this software as the sole source of safety data.

## What VesselStack installs

| Component | Purpose | Default address |
|---|---|---|
| Home Assistant | Automations, dashboards, notifications | `http://HOST:8123` |
| InfluxDB 2.9 | Time-series storage | `http://HOST:8086` |
| Grafana 13 | Historical dashboards | `http://HOST:43000` |
| Prometheus | Infrastructure metrics | `http://HOST:9090` |
| Mosquitto | Authenticated MQTT broker | `HOST:1883` |
| Heimdall | Local service launcher | `http://HOST` |
| Boat Chat | Telemetry-backed diagnostic assistant | `http://HOST:8765` |
| Control Panel | Component installation, configuration, and lifecycle | `http://127.0.0.1:8780` |

The installer generates credentials, renders a pinned Docker Compose stack,
creates a confined systemd service for Boat Chat, creates configurable InfluxDB
buckets, provisions the one-minute downsample task expected by Boat Chat,
provisions Grafana datasources and a system-health dashboard, and installs the
`vesselstackctl` lifecycle command. It also installs a local-first, token-
authenticated Control Panel that can inspect and operate every managed
container and host service, edit redacted configuration, run preflight, apply
configuration, create backups, and apply a reviewed release update.

All container tags are release-pinned and checked for Linux ARM64 availability;
VesselStack does not rely on floating `latest` tags.

The installer supports an existing SignalK server, an opt-in
pinned SignalK container, or an opt-in pinned native SignalK installation. It
also provides hardware-probed SocketCAN and AIS-catcher modules. Hardware
modules remain disabled unless explicitly configured. It also includes generic
Home Assistant alert/dashboard templates and an optional untrusted-interface
firewall policy.

## Supported platform

- Raspberry Pi 4/5 with Raspberry Pi OS 64-bit, or Debian/Ubuntu with systemd
- At least 4 GB RAM; 8 GB recommended for the full stack
- SSD or NVMe storage strongly recommended
- Docker Engine with Docker Compose v2
- Python 3 with `venv`
- Existing SignalK server or a selected VesselStack SignalK install mode
- Dedicated non-root service account

Installation downloads container images and a Python dependency, so temporary
internet access is required.

## Before installation

1. Back up existing Home Assistant, InfluxDB, Grafana, MQTT, and SignalK data.
2. Confirm ports `80`, `443`, `1883`, `8086`, `8123`, `8765`, `8780`, `9090`,
   and `43000` are free. Port `8780` binds to loopback by default.
3. If using `SIGNALK_MODE=existing`, confirm SignalK responds:

   ```bash
   curl -fsS http://127.0.0.1:3000/signalk
   ```

4. Put `/opt/vesselstack-data` on SSD/NVMe storage.
5. Do not deploy while underway or while relying on the computer for an anchor
   watch or alarm.

## 1. Install prerequisites

On Raspberry Pi OS or Debian/Ubuntu:

```bash
sudo apt update
sudo apt install -y ca-certificates curl git jq openssl python3 python3-venv
```

Install Docker Engine and Compose v2 using Docker's instructions for your OS:
<https://docs.docker.com/engine/install/>.

```bash
sudo docker version
sudo docker compose version
```

Do not combine installation with a broad OS upgrade. Treat OS/kernel upgrades
as separate maintenance with a separate rollback plan.

## 2. Create the service account

The default configuration uses a locked `boat` account:

```bash
sudo useradd --system --create-home \
  --home-dir /var/lib/vesselstack \
  --shell /usr/sbin/nologin boat
```

If it already exists, keep it and set `VESSELSTACK_USER` accordingly. It does
not need membership in the `docker` group.

## 3. Download VesselStack

```bash
git clone https://github.com/jclima/VesselStack.git
cd VesselStack
```

For a stable release, use a published tag:

```bash
git fetch --tags
git checkout v1.3.0
```

## 4. Configure the vessel

```bash
cp vesselstack.example.env vesselstack.env
chmod 600 vesselstack.env
nano vesselstack.env
```

| Setting | Description |
|---|---|
| `BOAT_NAME` | Name displayed by Boat Chat |
| `BOAT_TYPE` | Vessel make/model or general type |
| `BOAT_MMSI`, `BOAT_CALLSIGN` | Optional identity fields |
| `*_PORT` | Host ports for Grafana, Heimdall HTTP/HTTPS, InfluxDB, Prometheus, MQTT, and AIS |
| `INFLUXDB_CONTAINER_NAME` | Advanced override for existing/custom Compose deployments |
| `BOAT_TIMEZONE` | IANA timezone, such as `America/Los_Angeles` |
| `BOAT_UNITS` | `us_customary` or `metric` |
| `VESSELSTACK_USER` | Existing non-root service account |
| `VESSELSTACK_UID`, `VESSELSTACK_GID` | Derived from the service account by the installer |
| `VESSELSTACK_ROOT` | Application path; default `/opt/vesselstack` |
| `VESSELSTACK_DATA` | Persistent container-data path |
| `VESSELSTACK_BACKUP` | Backup destination; must be outside the application and data trees |
| `SIGNALK_MODE` | `existing`, `docker`, or `native` |
| `SIGNALK_VERSION` | Pinned SignalK release used by managed modes |
| `SIGNALK_URL` | Existing SignalK endpoint |
| `HOME_ASSISTANT_TOKEN` | Optional long-lived token used by Boat Chat |
| `SOCKETCAN_ENABLE` | Opt in only after the CAN interface is present |
| `SOCKETCAN_INTERFACE`, `SOCKETCAN_BITRATE` | CAN device and NMEA 2000 bitrate |
| `AIS_ENABLE` | Opt in to the AIS-catcher Compose profile |
| `AIS_DEVICE` | USB bus or receiver device mapped into AIS-catcher |
| `AIS_CATCHER_ARGS` | Reviewed AIS-catcher receiver/output arguments |
| `INFLUXDB_ORG` | InfluxDB organization |
| `INFLUXDB_RAW_BUCKET` | Full-resolution SignalK data; default `signalk` |
| `INFLUXDB_HISTORY_BUCKET` | One-minute retained history; default `signalk_1m` |
| `INFLUXDB_HOME_ASSISTANT_BUCKET` | Home Assistant history bucket |
| `INFLUXDB_AIS_BUCKET` | Short-retention AIS history bucket |
| `*_PASSWORD`, `*_TOKEN` | Keep `GENERATE` for generated values |
| `BOAT_CHAT_PROVIDER` | `local` works without an external LLM |
| `TELEGRAM_ENABLE` | Run the optional Telegram worker; requires its token and allowed chat IDs in Boat Chat Settings |
| `TELEMETRY_INDEXER_ENABLE` | Refresh Boat Chat's telemetry memory every five minutes |
| `CONTROL_PANEL_HOST`, `CONTROL_PANEL_PORT` | Administration listener; defaults to loopback on `8780` |
| `MQTT_USERNAME`, `MQTT_PASSWORD` | Mosquitto administrator account; the username may contain letters, numbers, `.`, `_` and `-` (1-64), and a custom password must not contain `:` or leading/trailing whitespace |
| `DISPLAYS_ENABLE` | `false` by default; `true` adds the displays bridge and per-display MQTT accounts (see [MQTT access control](#mqtt-access-control)) |
| `DISPLAYS_BRIDGE_MQTT_USERNAME` | Bridge account name; default `vesselstack-bridge`; must differ from `MQTT_USERNAME` |

Never commit `vesselstack.env`. Generated secrets are stored in mode-600 files
under `/opt/vesselstack/config/` and are not printed.

## 5. Run preflight

The dry run checks commands, Docker Compose, configuration, paths, and the
service account without writing system configuration. It also rejects invalid
listener addresses, out-of-range ports, and duplicate ports among enabled
components before Docker or systemd encounters a harder-to-diagnose bind error:

```bash
sudo ./install.sh --config vesselstack.env --dry-run
```

Resolve every error before continuing.

For native SignalK, install Node.js 22 or newer and npm before preflight. For
SocketCAN or AIS, connect the adapter first: preflight intentionally fails when
an enabled device is not detected.

## 6. Render and review

```bash
sudo ./install.sh --config vesselstack.env
```

This copies the application to `/opt/vesselstack`, creates persistent paths,
generates credentials, creates Boat Chat's Python environment, installs and
enables (but does not start) Boat Chat and the configured optional workers, and
validates Compose. Telegram is disabled by default; the telemetry indexer is
enabled by default so recent boat data can be recalled in chat.

Review the rendered files:

```bash
sudo docker compose \
  --env-file /opt/vesselstack/config/vesselstack.env \
  -f /opt/vesselstack/compose.yml config

sudo systemd-analyze verify /etc/systemd/system/vesselstack-chat.service
sudo systemd-analyze verify /etc/systemd/system/vesselstack-chat-telegram.service
sudo systemd-analyze verify /etc/systemd/system/vesselstack-telemetry-indexer.service
sudo systemd-analyze verify /etc/systemd/system/vesselstack-telemetry-indexer.timer
sudo systemd-analyze verify /etc/systemd/system/vesselstack-control-panel.service
```

## 7. Start VesselStack

```bash
sudo ./install.sh --config vesselstack.env --start
```

This renders the Mosquitto ACL and hashed password file into
`/opt/vesselstack/config/mosquitto-auth/` (see
[MQTT access control](#mqtt-access-control)), starts Docker services, recreates
the Mosquitto container when its configuration changed, creates the
configured InfluxDB buckets and one-minute downsample task, and starts Boat
Chat plus each enabled worker. Allow several minutes for initial image downloads
and Home Assistant.

## 8. Verify the installation

Start with the operator commands; they use the URLs and ports in your rendered
configuration:

```bash
sudo vesselstackctl status
sudo vesselstackctl doctor
sudo vesselstackctl urls
```

`status` reports every required and enabled Compose service separately and
exits nonzero when a required service or endpoint is unhealthy, so one running
container cannot hide another that failed or stopped.
`vesselstackctl start` is also a complete first-start path: it configures
enabled hardware and firewall policy, creates the MQTT ACL and password files when
either is missing, starts services, and idempotently provisions InfluxDB history.
`doctor` adds dependency, Compose, free-space, service, and endpoint checks and
finishes with either `RESULT healthy` or `RESULT attention required`. Use
`vesselstackctl logs chat`, `logs containers`, or `logs all` for recent
diagnostic output without remembering systemd or Compose commands.

For direct checks:

```bash
sudo docker compose \
  --env-file /opt/vesselstack/config/vesselstack.env \
  -f /opt/vesselstack/compose.yml ps

sudo systemctl status vesselstack-chat.service --no-pager
sudo systemctl status vesselstack-control-panel.service --no-pager
curl -fsS http://127.0.0.1:8765/health | jq
curl -fsS http://127.0.0.1:8780/health | jq
curl -fsS http://127.0.0.1:8086/health | jq
curl -fsS http://127.0.0.1:43000/api/health | jq
curl -fsS http://127.0.0.1:8123/manifest.json >/dev/null
```

Check for restart loops:

```bash
sudo docker compose \
  --env-file /opt/vesselstack/config/vesselstack.env \
  -f /opt/vesselstack/compose.yml ps --all
sudo journalctl -u vesselstack-chat.service --since "15 minutes ago" --no-pager
```

## 9. Complete first-run setup

### Home Assistant

Open `http://HOST:8123` and finish onboarding. Configure a notification
provider, MQTT at `127.0.0.1:1883`, and sensors appropriate to the boat's
SignalK paths. VesselStack installs reusable low-battery, bilge/high-water, and
shore-power-loss blueprints under **Settings → Automations → Blueprints**.
Create automations from them only after validating the selected entities,
thresholds, durations, and notification actions.

The installer also places
`/opt/vesselstack-data/homeassistant/vesselstack-dashboard.example.yaml` as a
starter Lovelace view. Copy it into a dashboard only after replacing every
`sensor.vesselstack_*` example with a real entity from the vessel. VesselStack
does not silently enable safety alerts or assume thresholds.

### SignalK and InfluxDB

With `SIGNALK_MODE=docker`, VesselStack starts the official pinned SignalK
image and persists its settings in `/opt/vesselstack-data/signalk`. With
`SIGNALK_MODE=native`, it installs the pinned npm package under
`/opt/vesselstack/signalk-server` and runs it as the locked VesselStack user.
`SIGNALK_MODE=existing` never modifies SignalK. The managed modes use the same
first-run admin onboarding at `http://HOST:3000`.

When AIS is enabled, AIS-catcher exposes its viewer on port `8100` and an NMEA
TCP feed on port `5011`. Add a SignalK NMEA 0183 TCP client connection to
`127.0.0.1:5011`. When SocketCAN is enabled, VesselStack validates the named
interface and installs a 250-kbit/s-by-default systemd unit; configure the
matching SignalK NMEA 2000 connection in SignalK's admin UI.

Configure a SignalK InfluxDB 2-compatible output plugin with values from
`/opt/vesselstack/config/vesselstack.env`:

- URL: `http://127.0.0.1:8086`
- organization: `INFLUXDB_ORG`
- token: `INFLUXDB_TOKEN`
- full-resolution bucket: `INFLUXDB_RAW_BUCKET` (default `signalk`)

The installer provisions a one-minute mean downsample task into
`INFLUXDB_HISTORY_BUCKET`. Confirm it is running before depending on Boat
Chat's long-term engine, fuel, or battery analysis:

```bash
sudo docker exec vesselstack-influxdb influx task list \
  --org vesselstack --token "$(sudo sed -n 's/^INFLUXDB_TOKEN=//p' /opt/vesselstack/config/vesselstack.env)"
```

Upgrades from 0.x retain any previously configured InfluxDB bucket names
when those settings are absent, so existing history remains accessible.

### Grafana

Open `http://HOST:43000`. Retrieve the generated initial password locally:

```bash
sudo grep '^GRAFANA_ADMIN_PASSWORD=' \
  /opt/vesselstack/config/vesselstack.env
```

The installer provisions `VesselStack InfluxDB`, `VesselStack Prometheus`, and
the **VesselStack / VesselStack System Health** starter dashboard. Use that as
the operational baseline, then add vessel dashboards for the SignalK paths
available on the boat. Provisioned dashboards are editable in Grafana; keep
reusable versions in the repository so reinstallations remain reproducible.

### Boat Chat

Open `http://HOST:8765`. The `local` provider returns gathered context without
sending it to an external model. Configure an optional provider in Settings.
Retrieve the settings token locally when needed:

```bash
sudo grep '^BOAT_CHAT_SETTINGS_TOKEN=' \
  /opt/vesselstack/config/boat-chat.env
```

Boat Chat retains this settings credential only for the current browser tab;
closing the tab clears it.

Boat Chat rejects oversized or malformed API requests and sends defensive
browser headers while remaining embeddable in the Home Assistant dashboard.
It also provides deterministic time-window and signal planning, durable local
sessions, answer feedback, selectable response modes, chart-ready evidence,
relative AIS context, recent-trip insights, and a local maintenance tracker.
The interface rate-limits each client and runs at most two chat requests at a
time so slow model calls cannot exhaust the Raspberry Pi.

Set `BOAT_CHAT_ACCESS_TOKEN` in Boat Chat Settings to protect chat, live status,
insights, feedback, and maintenance data. The browser keeps the entered token
only in session storage. Without an access token, ordinary chat remains
available but live status, insights, and maintenance endpoints stay locked.

`BOAT_CHAT_WEB_SEARCH=true` enables the OpenAI Responses API web-search tool
when the OpenAI provider is selected. This can send the question and grounded
context to an external service and may incur provider charges; it is disabled
by default. Boat Chat sessions, audits, feedback, and maintenance tasks stay in
the private SQLite memory database under the configured data directory and are
excluded from release packages.

### Control Panel

The Control Panel is the browser administration surface for VesselStack. It
shows container and systemd state, starts/stops/restarts individual components,
edits settings by component, runs installer preflight, renders or installs the
saved configuration, provisions InfluxDB history, creates consistent backups,
and applies an already downloaded, reviewed, and root-controlled release.

It deliberately stays available when the managed stack is stopped. The
`vesselstackctl` command remains the recovery path if the panel is unavailable.

The default listener is loopback-only. From an administrator workstation, use
an SSH tunnel or a trusted VPN rather than exposing the panel publicly:

```bash
ssh -L 8780:127.0.0.1:8780 boat-admin@HOST
```

Then open `http://127.0.0.1:8780`. Retrieve the generated token locally and
enter it on the unlock screen:

```bash
sudo grep '^CONTROL_PANEL_TOKEN=' \
  /opt/vesselstack/config/control-panel.env
```

The token is stored in browser session storage, cleared by the **Lock** button
or when the tab closes, and sent only in the `X-VesselStack-Token` request
header. Configuration APIs never return secret values: they report only whether
a secret is configured, and a blank submitted secret preserves its current
value. Use the explicit **Clear stored value** checkbox to remove one. Every
configuration save creates mode-600 rollback copies under
`/opt/vesselstack-data/control-panel/config-backups/`. Operation commands are
fixed allowlisted argument arrays; the panel does not expose an arbitrary shell.
While one operation is running, other operation and component controls are
disabled to prevent overlapping lifecycle changes.

Clearing an optional integration token disables that credential. Clearing a
required installer-managed password or token causes the installer to generate
a replacement the next time configuration is applied; dependent clients must
then be updated with the new credential.

Saving settings writes mode-600 configuration atomically but does not restart
services. Review changes, run **Preflight**, then use **Apply configuration** or
**Install & start**. Hardware probes and the existing SignalK connectivity
check still gate installer actions. Listener changes require a shell restart:

```bash
sudo systemctl restart vesselstack-control-panel.service
```

The panel intentionally skips restarting itself while an operation request is
in flight. After applying a release that changes Control Panel code, run the
same restart command to load the new backend.

The Telegram and telemetry-indexer toggles take effect after **Install &
start** (or another installer run). Configure the Telegram bot token and allowed
chat IDs in the Telegram section before enabling its worker. Disabled workers
are shown as disabled and cannot be started accidentally from the component
controls. Operators can
inspect these workers with:

```bash
sudo vesselstackctl logs telegram
sudo vesselstackctl logs indexer
systemctl list-timers vesselstack-telemetry-indexer.timer
```

The service runs as root because Docker, systemd, installation, backup, and
hardware operations require host privileges. Keep the token private, retain
loopback binding unless a trusted authenticated access layer is in place, and
never expose port `8780` directly to the internet. To disable the panel without
affecting telemetry services:

```bash
sudo systemctl disable --now vesselstack-control-panel.service
```

Re-enable it with `sudo systemctl enable --now
vesselstack-control-panel.service`. CLI configuration and lifecycle commands
continue to work while it is disabled.

### Heimdall

Open `http://HOST` and add trusted-network links to Home Assistant, Grafana,
SignalK, Boat Chat, InfluxDB, and Prometheus.

## MQTT access control

Mosquitto always enforces an access list (`acl_file /mosquitto/auth/acl`,
`password_file /mosquitto/auth/passwords`). Every `install.sh` run that is not
`--dry-run` (including render-only runs such as Control Panel "Apply
configuration"), and `vesselstackctl start` when the files are missing, render
the list and hashed passwords through
`python3 displays/displayctl.py sync-mqtt`. Passwords are hashed inside the
pinned Mosquitto image (`mosquitto_passwd -U`) and are never placed on a
command line.

Where the files live and who owns them:

| Path | Owner | Mode | Notes |
| --- | --- | --- | --- |
| `$VESSELSTACK_ROOT/config/mosquitto-auth/` | `root:1883` | `0750` | mounted **read-only** into the broker at `/mosquitto/auth` |
| `acl`, `passwords`, `acl.prev`, `passwords.prev` inside it | `1883:1883` | `0600` | the broker's user owns them, as Mosquitto expects |

The directory is root-owned and not writable by the broker (uid/gid 1883), so a
compromised or misbehaving broker cannot create, remove, rename or replace
entries there, in particular it cannot plant symlinks for the root-run sync
code to follow. The files are owned by the broker user only because Mosquitto
warns about files it does not own and states that future versions will refuse
to load them; the broker cannot swap them out because the directory and mount
are read-only. The sync code is hardened accordingly: it refuses a symlinked
`config/` or `mosquitto-auth` directory, refuses (as root) a directory that is
not owned by root or is group/world writable, opens everything relative to a
directory file descriptor with `O_NOFOLLOW`, creates temporary files with
`O_CREAT|O_EXCL|O_NOFOLLOW`, sets owner and mode on the open file descriptor,
refuses symlinks or non-regular files at `acl`, `passwords` and their `.new`,
`.prev` and `.prev.tmp` names, and never touches
`$VESSELSTACK_DATA/mosquitto`. The Mosquitto image must be present to hash
passwords; if it is missing and cannot be pulled (for example offline), sync
stops with a clear message before changing any file.

With `DISPLAYS_ENABLE=false` (the default) only the administrator account
(`MQTT_USERNAME`) exists: it has `readwrite #` and `read $SYS/#`, so Home
Assistant, SignalK and other existing clients that use that account keep
working. With `DISPLAYS_ENABLE=true` these accounts are added:

| Account | Read | Write |
| --- | --- | --- |
| `MQTT_USERNAME` (admin) | `#`, `$SYS/#` | `#` |
| `DISPLAYS_BRIDGE_MQTT_USERNAME` (default `vesselstack-bridge`) | `vesselstack/v1/displays/+/status` | `vesselstack/v1/instruments/#`, `vesselstack/v1/bridge/#` |
| `vsdisp-<display id>`, one per display in `config/displays.json` | `vesselstack/v1/instruments/#`, `vesselstack/v1/bridge/#` | `vesselstack/v1/displays/vsdisp-<display id>/status` only |

Display accounts are read-only for instrument data and cannot read other MQTT
topics such as `homeassistant/#`. Any other MQTT account is granted nothing.

Operational notes:

- Bridge and display passwords are generated once (`secrets.token_urlsafe(32)`)
  into `$VESSELSTACK_ROOT/config/displays.secrets.json` (mode 0600) and are
  reused on every later run; this file is inside the backed-up `config/`
  directory, so treat backups as secrets. Credentials of a display removed from
  `displays.json` stop working immediately but remain in that file (so a
  temporarily broken or missing `displays.json` cannot destroy them); delete
  the entry by hand to retire it permanently. With displays enabled and no
  `displays.json`, only the admin and bridge accounts exist.
- The sync tool sets the ownership and modes above (it must run as root),
  keeps `acl.prev` and `passwords.prev` copies, and signals the running broker
  to reload (`SIGHUP`), which also disconnects removed accounts. Because the
  auth directory is mounted as a directory, replaced files reach the running
  broker without a restart.
- `install.sh --dry-run` (the Control Panel "Preflight") exits before any
  rendering: it does not write files, pull images or signal containers. Apply
  configuration and install do sync, and may therefore pull the pinned
  Mosquitto image on first use and send `SIGHUP` to a running broker.
- Upgrade impact: the first `install.sh --start`, `vesselstackctl update` or
  any other run that starts the stack recreates the Mosquitto container once
  (a brief MQTT interruption) so the new configuration and the new read-only
  mount take effect. A render-only `install.sh` run updates the files in
  `config/mosquitto-auth/`, but a broker still running the previous container
  keeps its old configuration and mounts until it is recreated.
- Legacy files: releases that kept `passwords`, `acl` and their `.prev`/`.new`
  copies in `$VESSELSTACK_DATA/mosquitto/` leave them there untouched and
  unused (the broker's persistence database stays in that directory). They are
  safe to delete by hand once the new configuration works; VesselStack never
  deletes or follows anything in that broker-writable directory.
- Manually added MQTT users are not preserved: every non-dry-run `install.sh`
  and `vesselstackctl update` rebuild the password file from `MQTT_USERNAME` and the
  displays accounts, and any account not in the ACL has no access. Add other
  clients by sharing the administrator account or extend VesselStack.
- Accounts are rebuilt when you change `DISPLAYS_ENABLE`, the bridge name or
  `displays.json` and run `sudo ./install.sh --config ...` (render-only runs sync too; add `--start`
  to recreate the broker). Names must
  match `[A-Za-z0-9_.-]{1,64}` and cannot collide; invalid input or a hashing
  failure aborts with an error and leaves the previous live files in place.
  An existing `MQTT_PASSWORD` containing `:` or leading/trailing spaces is
  rejected because `mosquitto_passwd` would store a different password; change
  it in the Control Panel first.
- Rollback: set `DISPLAYS_ENABLE=false` and run `sudo ./install.sh --config
  ... --start`; the ACL returns to administrator-only. Downgrading to a release
  without this feature: its Mosquitto configuration points at the legacy
  `$VESSELSTACK_DATA/mosquitto/passwords` path and its installer recreates the
  files there, so the broker works again after the downgrade; the
  `config/mosquitto-auth/` directory and `displays.secrets.json` are left in
  place. `vesselstackctl restore` extracts with `--numeric-owner` so the
  `root:1883` and `1883:1883` ownership survives a restore.
- Troubleshooting: if the broker log contains "owner is not mosquitto",
  "world readable" or "permission denied" for `/mosquitto/auth/...`, re-run
  `sudo ./install.sh --config /opt/vesselstack/config/vesselstack.env` to
  rewrite the files with the correct ownership. If sync reports that the image
  "could not be pulled", connect the boat to the internet once (or load the
  image with `docker load`) and run it again; nothing is changed before then.
  "refusing to use ... symbolic link" or "group or world writable" means the
  auth directory was tampered with: inspect and remove it, then re-run.
- Verify with `sudo tests/test-displays-mosquitto.sh` (real broker in Docker;
  as root it verifies the real ownership, otherwise a one-shot container copies
  the files into a Docker volume with that ownership first)
  and `python3 -m unittest discover -s tests -p 'test_displays*.py'`.

## Instrument displays (preview)

VesselStack is gaining configurable live-instrument displays: small ESP32 touch
screens that show SignalK values (speed, depth, wind angle, battery voltage and
so on) on pages you define. The first board is the Waveshare
ESP32-S3-Touch-LCD-2.8C, planned to run ESPHome with LVGL. The board registry
and page layouts are data, so more boards and layouts can be added later.

**Status: preview.** The configuration schema, validation, unit conversions,
the MQTT topic/payload and freshness logic, the board registry and the MQTT
account/ACL provisioning (`displayctl.py sync-mqtt`, wired into `install.sh` and
`vesselstackctl`) exist today (`displays/`). The bridge service, Compose wiring
and firmware do not exist yet, so enabling displays only creates broker accounts
and credentials. The always-on Mosquitto ACL does affect existing installations
(see [MQTT access control](#mqtt-access-control)). `displays/` is copied to
`$VESSELSTACK_ROOT/displays` by the installer.

### Configuration and validation

The runtime configuration will live at `$VESSELSTACK_ROOT/config/displays.json`.
Start from [`displays/displays.example.json`](displays/displays.example.json).
It is JSON with `schema` (must be `1`), `mqtt_host` (hostname or IP literal),
`heartbeat_s` (1-60, default 5), `instruments` and `displays`:

- An instrument (`^[a-z][a-z0-9_]{0,31}$`) has a SignalK `path` for
  `vessels.self` (numeric scalar values only), `label` (1-16 characters),
  `convert` (one named conversion), `unit` (up to 8 characters), `precision`
  (0-3), `stale_after_s` (1-3600 and at least 2 x `heartbeat_s`) and optional
  `min_interval_ms` (100-60000, default 1000).
- A display (`^[a-z][a-z0-9-]{0,23}$`) has a `name`, a `board` from
  `displays/boards/registry.json`, optional `home_assistant_api` (boolean) and
  1-8 `pages`. A page has a `title` (1-16 characters), a `layout` the board
  supports (`single` holds 1 item, `grid-2x2` holds 4, `list-6` holds 6) and
  unique `items` naming existing instruments.
- Conversions: `none`, `m/s->kn`, `m/s->km/h`, `m/s->mph`, `K->C`, `K->F`,
  `rad->deg`, `rad->deg_signed` (-180..180], `rad->deg_360` [0,360), `m->ft`,
  `m->fathom`, `m->nmi`, `ratio->%`, `Pa->hPa`, `Pa->bar`, `Pa->psi`, `m3->L`,
  `m3->gal` (US), `m3/s->L/h`, `m3/s->gal/h`, `Hz->rpm`, `C->Ah`, `s->min`,
  `s->h`.

Unknown keys are rejected at every level, booleans are not accepted where
integers are expected, and all errors are reported together with a JSON-path
location. Validate with the standard library only (no extra packages):

```bash
python3 displays/displayctl.py validate                  # the example file
python3 displays/displayctl.py validate /path/to/displays.json
```

Exit status is `0` valid, `1` validation errors (nothing is written), `2`
usage, file or JSON parse errors.

### MQTT topics and freshness

Values are published retained to `vesselstack/v1/instruments/<instrument_id>`
as compact JSON: `{"v":6.4,"u":"kn","st":"ok","ts":"2026-01-01T00:00:00.000Z","age":0.4}`.
`v` is converted, then rounded to `precision`, and is `null` unless `st` is
`ok`. `ts` is the SignalK timestamp (or `null`); `age` is seconds since the
bridge received the value, measured on the bridge clock, not the SignalK
timestamp. Other reserved topics are `vesselstack/v1/bridge/status`,
`vesselstack/v1/bridge/signalk` and `vesselstack/v1/displays/<username>/status`.

- `ok`: a value was received within `stale_after_s`.
- `stale`: nothing received for more than `stale_after_s`; published
  immediately on the transition, with `v` null.
- `missing`: no value has been received since the bridge started.
- A changed value is published at most once per `min_interval_ms`; unchanged
  values are not republished except by the `heartbeat_s` heartbeat, which also
  refreshes `age`. Null, non-numeric, non-finite and boolean SignalK values are
  ignored, as are values whose magnitude exceeds 1e15 after conversion.

### Supported boards

| Board | Screen | Layouts | Status |
| --- | --- | --- | --- |
| Waveshare ESP32-S3-Touch-LCD-2.8C (`waveshare-esp32-s3-touch-lcd-2.8c`) | 480x480 round, 70 px safe inset, 340x340 content area | `single`, `grid-2x2` | Hardware unverified |

`list-6` is defined but not offered on the round board.

### Planned limitations

- Version 1 uses plaintext MQTT on the local network; it provides no
  confidentiality or strong authentication against other devices on that
  network.
- With the VesselStack firewall enabled, displays must be on a trusted
  interface that can reach the Raspberry Pi's MQTT port.
- The primary planned build path is exporting from the Pi and compiling on a
  laptop; no firmware, flashing or over-the-air flow ships in this preview.
- The runtime `displays.json`, `displays.secrets.json`, ESPHome
  `secrets.yaml`, `.esphome/` and `displays/build/` output, and firmware
  binaries (`.bin`, `.elf`, `.uf2`) are git-ignored and rejected by
  `scripts/check-public-tree.py`; never commit them.

## Network security

Compose publishes several ports on every host interface. At minimum:

- restrict InfluxDB `8086`, Grafana `43000`, Prometheus `9090`, and MQTT `1883`
  to trusted management networks;
- keep Home Assistant, SignalK, and Boat Chat off the public internet unless
  protected by an authenticated reverse proxy;
- keep the root-privileged Control Panel loopback-only or behind a trusted VPN
  and authenticated proxy;
- use ZeroTier, Tailscale, or WireGuard for remote access; and
- keep `/opt/vesselstack/config/*.env` readable only by root or the required
  service account.

VesselStack does not install firewall rules because interface names and trusted
network ranges differ between boats by default. To enable its conservative
untrusted-interface policy, set:

```bash
VESSELSTACK_FIREWALL_ENABLE="true"
VESSELSTACK_UNTRUSTED_INTERFACE="wlan0"
```

Preflight requires that interface to exist. The policy blocks MQTT, InfluxDB,
Prometheus, and Grafana administrative/data ports arriving on that interface,
including Docker-forwarded traffic, while leaving Home Assistant, SignalK,
Boat Chat, AIS viewer, HTTP/HTTPS, loopback, VPN, and outbound traffic intact.
Review the rendered policy in `scripts/vesselstack-firewall` against the boat's
network design before enabling it. Disable and remove its chains with:

```bash
sudo systemctl disable --now vesselstack-firewall.service
sudo vesselstack-firewall remove
```

## Backups

Create a consistent backup. The stack is stopped briefly to avoid copying live
database files and is restarted automatically if it was running:

```bash
sudo vesselstackctl backup
sudo vesselstackctl verify-backup /path/to/vesselstack-TIMESTAMP.tar.gz
```

Backup creation runs the same checksum, path, link, and special-file verifier
automatically. Rebuildable Boat Chat virtual-environment and bytecode files are
excluded; application source, configuration, memory, and data remain included.
The destination is resolved before writing and is rejected if it falls inside
the application or data tree, including through a symlink.

Restore only during a maintenance window. This replaces installed config and
data after checksum and archive validation. Verification rejects path
traversal, absolute or escaping links, device nodes, and FIFOs before anything
is extracted:

```bash
sudo vesselstackctl restore /path/to/vesselstack-TIMESTAMP.tar.gz --yes
```

Back up at least:

```text
/opt/vesselstack/config/
/opt/vesselstack-data/homeassistant/
/opt/vesselstack-data/influxdb/
/opt/vesselstack-data/grafana/
/opt/vesselstack-data/mosquitto/
/opt/vesselstack-data/heimdall/
```

Keep an encrypted copy off the boat and test restoration. InfluxDB needs an
application-consistent backup or a snapshot made while stopped; blindly copying
live database files is insufficient.

## Updating

Back up configuration and data, then use a reviewed release:

```bash
cd VesselStack
git fetch --tags
git checkout <reviewed-release-tag>

sudo ./install.sh \
  --config /opt/vesselstack/config/vesselstack.env \
  --dry-run

sudo ./install.sh \
  --config /opt/vesselstack/config/vesselstack.env
```

Review Compose and release notes before applying container changes. Never
upgrade with the original example containing `GENERATE`; use the installed
configuration so credentials remain stable.

Once a reviewed release directory is downloaded, the lifecycle command can
perform preflight, a verified pre-update backup, installation, and startup:

```bash
sudo vesselstackctl update /path/to/VesselStack-release
```

For Control Panel updates, stage the reviewed release beneath its restricted
root first. The panel rejects symlinks, missing release metadata, paths outside
this directory, files owned by another user, and group/world-writable content:

```bash
sudo install -d -m 0755 /var/lib/vesselstack/releases
sudo cp -a /path/to/vesselstack-x.y.z /var/lib/vesselstack/releases/
sudo chown -R root:root /var/lib/vesselstack/releases/vesselstack-x.y.z
sudo chmod -R go-w /var/lib/vesselstack/releases/vesselstack-x.y.z
```

Then enter `/var/lib/vesselstack/releases/vesselstack-x.y.z` in the panel.
Shell-initiated `vesselstackctl update` remains available for a trusted operator
and is not limited to the panel staging directory.

The installer records the configuration schema in
`/opt/vesselstack/config/installed-version` and accepts only explicit migration
edges. It refuses unknown or future schemas instead of guessing. The 1.0
migration supports 0.1 installations and retains legacy InfluxDB names when
the new bucket settings are absent.

## Stopping or removing services

Stop services without deleting persistent data:

```bash
sudo systemctl stop vesselstack-chat.service
sudo systemctl stop vesselstack-control-panel.service
sudo docker compose \
  --env-file /opt/vesselstack/config/vesselstack.env \
  -f /opt/vesselstack/compose.yml down
```

Remove services while preserving configuration and data:

```bash
sudo vesselstackctl uninstall
```

After verifying an off-boat backup, default-path installations can also remove
all configuration and data with the explicit destructive confirmation. Purge
confirmation and default-path safety checks run before any service is stopped
or file is removed:

```bash
sudo vesselstackctl uninstall --purge-data --yes
```

## Troubleshooting

### A prerequisite is missing

Install the reported command and rerun `--dry-run`. Confirm `docker compose`
works; the deprecated standalone `docker-compose` command is insufficient.

### A port is already allocated

```bash
sudo ss -lntup
```

Stop the conflicting service or deliberately change the published port in the
template and this README together.

### Boat Chat cannot read telemetry

```bash
curl -fsS http://127.0.0.1:3000/signalk
sudo journalctl -u vesselstack-chat.service -n 100 --no-pager
```

Confirm SignalK and InfluxDB values in
`/opt/vesselstack/config/boat-chat.env`.

### Mosquitto restarts repeatedly

```bash
sudo ls -lnd /opt/vesselstack/config/mosquitto-auth
sudo ls -ln /opt/vesselstack/config/mosquitto-auth/
sudo docker logs vesselstack-mosquitto --tail 100
```

The directory must be `root:1883` mode `0750`, and `passwords` and `acl` must
both exist and be owned by `1883:1883` with mode `0600`. To repair them, re-run
`sudo ./install.sh --config /opt/vesselstack/config/vesselstack.env` (add
`--start` to restart the stack); the sync rewrites the files and fixes the
modes. A client that
connects but receives nothing, or whose publishes vanish, is usually missing
from the ACL (see [MQTT access control](#mqtt-access-control)).

### Container mount permissions fail

Inspect the affected container log and mount. Do not recursively change all
ownership: containers use different internal user IDs.

## Developer validation

GitHub Actions uses `actions/checkout@v6`, which runs on Node.js 24 and keeps
persisted checkout credentials outside the repository Git configuration.
External registry manifest checks retry short transport failures three times;
missing Linux ARM64 support still fails the release gate.

```bash
tests/test-distribution.sh
sudo tests/test-lifecycle.sh
tests/test-migrations.sh
sudo tests/test-clean-install.sh
tests/test-image-manifests.sh
tests/test-service-integration.sh
sudo tests/test-displays-mosquitto.sh
docker compose --env-file vesselstack.example.env \
  -f templates/compose.yml config --quiet
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 displays/displayctl.py validate
python3 -m unittest discover -s tests -p 'test_control_panel.py' -v
```

Build a sanitized release:

```bash
./build-package.sh
(cd generated && sha256sum -c vesselstack-1.3.0.tar.gz.sha256)
```

The checksum records only the archive basename, so the archive and `.sha256`
file can be downloaded and verified together from any directory.

## Documentation policy

Every repository change must update this README in the same pull request. CI
enforces this for pull requests. Keep commands, versions, paths, features,
limitations, verification, and rollback guidance synchronized with code.

## License and security

VesselStack is Apache-2.0 licensed. See `LICENSE` and `NOTICE`. Report security
issues according to `SECURITY.md`; never publish credentials, tokens, vessel
positions, or exploitable findings in a public issue.

### Privacy before publishing or sharing

Tracked files and release packages contain placeholders only. Runtime
configuration, vessel facts, positions, MMSIs, call signs, AIS target records,
hostnames, private network addresses, credentials, telemetry, backups, and Boat
Chat memory must remain outside the repository. Before sharing a fork or
diagnostic bundle, inspect both the current files and Git history; deleting a
value in a later commit does not remove it from earlier commits or releases.

The distribution test rejects populated identity fields and private key
material. This is a safety net, not a substitute for reviewing staged changes.

## Version 1.0 release scope

1. **Complete:** InfluxDB schema names and runtime Boat Chat branding are
   configurable, with legacy defaults retained only for 0.x upgrades.
2. **Complete:** Existing, pinned native, and pinned Docker SignalK modes are
   included.
3. **Complete:** Opt-in SocketCAN and AIS-catcher modules include hardware
   probes and stay disabled by default.
4. **Complete:** Generic battery, bilge, and shore-power alert blueprints plus
   a Lovelace vessel dashboard template are included.
5. **Complete:** Grafana datasource/dashboard and InfluxDB downsample
   provisioning are included.
6. **Complete:** backup/restore, guarded upgrades, firewall, versioned
   migrations, and uninstall are included.
7. **Complete:** CI exercises clean rendering, service startup/provisioning,
   migration, packaging, and recovery. The same non-invasive install and
   recovery gates pass on a Linux ARM64 host running 64-bit Debian.
8. **Complete:** A loopback-first authenticated Control Panel manages component
   status, configuration, installation, updates, backups, and lifecycle while
   preserving CLI recovery access.

See `CHANGELOG.md` for release notes and `VALIDATION.md` for the full evidence
and hardware-dependent commissioning limits.

## Current development improvements

The owner overview now distinguishes missing/stale readings and contradictory
health indicators, displays observation age, refreshes periodically and retains
explicitly outdated information during connection loss. Chat preserves per-answer
evidence and feedback, renders responses as text, and handles cancellation and
conversation restoration. Complex explanations keep computed evidence intact.
VesselStack's configured URLs, buckets, timezone and branding remain supported.

Live status remains protected by a dedicated access token by default. Configure
`BOAT_CHAT_ACCESS_TOKEN` in private Boat Chat settings before connecting the owner
UI. `BOAT_CHAT_REQUIRE_ACCESS_TOKEN=false` is an explicit opt-out for a reviewed,
trusted installation; it is not the public default. Settings writes retain their
separate protection. Integration settings survive provider-settings saves.

Fuel economy now uses complete, finite, nonnegative fuel/RPM/speed observations
from the same minute and states matched coverage. A reusable comparison is:

```bash
python3 scripts/cruise-efficiency.py --days 90 --minimum 15 --maximum 40 --step 2.5
```

The command requires the deployment's private Influx settings and configured
history bucket. It reports combined US gal/h, nm/gal, gal/100 nm, mean RPM and
sample support; SOG/current effects and transitions remain. `--json` is supported.
`vesselstackctl status`/`doctor` now require Boat Chat's `ok` and `ready` flags,
rather than accepting any successful HTTP response.

For development from another machine, use a clean source checkout and follow
[Development and safe updates](docs/DEVELOPMENT.md). Git pulls update source only.
The existing lifecycle updater remains the release-update path; an optional
application-only helper offers preview, tests, per-file backup and guarded rollback
without touching infrastructure. Dependencies and database migrations remain
separate reviewed work. Source facts now live in `boat_facts.example.json`; actual
`boat_facts.json` and other operational data are ignored, not published.

### One-command application updates

Install the stable launcher and a private per-machine configuration once:

```bash
sudo install -m 0755 scripts/vesselstack-update /usr/local/sbin/vesselstack-update
sudo install -d -m 0755 /etc/vesselstack
sudo install -m 0600 templates/update.env.example /etc/vesselstack/update.env
sudoedit /etc/vesselstack/update.env
```

Set the separate source/live paths and actual chat/Telegram unit names. After
pushing changes to the source checkout's upstream branch, run on the boat:

```bash
sudo vesselstack-update          # pull, test, preview; no service restart
sudo vesselstack-update --apply  # pull, test, back up, apply and verify
```

If Telegram code changes, an already-active configured worker also restarts;
a stopped worker remains stopped. Restart units are saved in the backup receipt
and used during rollback. Restore a printed backup directory with
`sudo vesselstack-update --rollback /path/to/backup-directory`.

These commands update Boat Chat Python/static assets. Dependencies, migrations,
service definitions, dashboards and container/infrastructure changes require the
separate reviewed release workflow. The launcher/config stay installed locally
across Git pulls; reinstall the launcher deliberately when its interface changes.
See [development/update details](docs/DEVELOPMENT.md).
