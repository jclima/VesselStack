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
| Displays bridge (opt-in, `DISPLAYS_ENABLE=true`) | Publishes configured SignalK instruments to MQTT for instrument displays | systemd service `vesselstack-displays-bridge`; no listener |

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
| `DISPLAYS_BRIDGE_MQTT_USERNAME` | Bridge account name, 1-64 letters, numbers, dot, underscore or hyphen; default `vesselstack-bridge`; must differ from `MQTT_USERNAME` (the Control Panel rejects a collision when displays are enabled, and the installer preflight rejects it) |
| `DISPLAYS_SIGNALK_TOKEN` | Optional SignalK access token for the displays bridge, sent as `Authorization: Bearer`; leave empty unless SignalK security requires it; must not contain line breaks or any other control character (the Control Panel and the installer both refuse them) |

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
command line. The plaintext list exists only briefly as `root:root` `0600`
before hashing and is never readable by the broker; the hashing container then
hands only the hashed file to `1883:1883`.

Where the files live and who owns them:

| Path | Owner | Mode | Notes |
| --- | --- | --- | --- |
| `$VESSELSTACK_ROOT/config/mosquitto/` | `root:root` | `0755` | mounted **read-only** as a directory at `/mosquitto/config`; holds `mosquitto.conf` (`root:root` `0644`) |
| `$VESSELSTACK_ROOT/config/mosquitto-auth/` | `root:1883` | `0750` | mounted **read-only** into the broker at `/mosquitto/auth` |
| `acl`, `passwords`, `acl.prev`, `passwords.prev` inside it | `1883:1883` | `0600` | the broker's user owns them, as Mosquitto expects |
| `$VESSELSTACK_ROOT/config/mosquitto.conf` (legacy) | unchanged | unchanged | no longer written or mounted by this release; left in place so a container created by an older release (and a downgrade) keeps working |

The Mosquitto configuration moved from the single-file mount
`config/mosquitto.conf` to the directory `config/mosquitto/` on purpose: a
single-file bind mount keeps pointing at the inode it was created with, and
`restart: unless-stopped` re-resolves the bind source on every restart, so
replacing that file under a still-running old container would send it into a
crash loop (it would load the new configuration without the `/mosquitto/auth`
mount). The installer refuses a symlinked `config/mosquitto` (or
`mosquitto.conf` inside it) and creates the directory `root:root` `0755`.

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
`$VESSELSTACK_DATA/mosquitto`. The hashing container runs with
`--network none --cap-drop ALL --cap-add CHOWN --security-opt
no-new-privileges` and `umask 077`; the plaintext file and any
`passwords.new.*` file `mosquitto_passwd` derives from it (such as its
`.backup.XXXXXX` copy) are removed on every outcome. The Mosquitto image must
be present to hash passwords; if it is missing and cannot be pulled (for
example offline), the installer stops with a clear message before changing any
file. `sync-mqtt`, `bridge-env` and the lifecycle commands all hold the same
exclusive lock (`config/.displays.lock`, 10 s wait), so a Control Panel Apply
cannot interleave with a concurrent `displayctl add`.

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
  directory, so treat backups as secrets. `vesselstackctl display remove`
  deletes the display's entry here as well. Credentials of a display removed
  from `displays.json` by hand stop working immediately but remain in that file
  (so a temporarily broken or missing `displays.json` cannot destroy them);
  delete the entry by hand, or re-add and then `remove` the display, to retire
  it permanently. With displays enabled and no
  `displays.json`, only the admin and bridge accounts exist.
- The sync tool sets the ownership and modes above (it must run as root),
  keeps `acl.prev` and `passwords.prev` copies, and signals the running broker
  to reload (`SIGHUP`), which also disconnects removed accounts. Because the
  auth directory is mounted as a directory, replaced files reach the running
  broker without a restart.
- `install.sh --dry-run` (the Control Panel "Preflight", and the check that
  `vesselstackctl update` runs before it takes a backup) never writes files,
  pulls images, takes the lock or signals containers, but it now runs
  `displayctl.py preflight`, which rejects up front everything the sync would
  reject: an administrator `MQTT_PASSWORD` containing `:` or leading/trailing
  whitespace (a generated one is always fine), invalid account names, a bridge
  name that collides with `MQTT_USERNAME` or a `vsdisp-<id>` account, an
  invalid `displays.json` (when `DISPLAYS_ENABLE=true`), and a missing
  Mosquitto image. A missing image is a failure only when the registry cannot
  be reached either (`docker manifest inspect`); the message names the
  `docker pull` to run once while online. With a reachable registry it is only a
  warning ("will be pulled"), so the documented first-install flow (dry run,
  then `--start`) still works on a boat that has not pulled anything yet. A real
  run repeats the same checks and pulls a missing image *before* it changes any
  file, then syncs the MQTT access files **before** it installs the new
  `compose.yml` and `config/mosquitto/mosquitto.conf`, so a failure leaves the
  previous broker definition in place. Apply configuration and install may
  pull the pinned Mosquitto image on first use and send `SIGHUP` to a running
  broker.
- Upgrade impact: the Mosquitto container is recreated (a brief MQTT
  interruption) by the first `install.sh --start`, `vesselstackctl start` or
  `vesselstackctl update` after the upgrade, because its compose definition
  changed (new configuration directory and read-only auth mount). **Until then
  the old container keeps its old configuration and mounts, also across reboots
  and `docker restart`:** it still bind-mounts the untouched legacy
  `config/mosquitto.conf` and reads the legacy `passwords` file in
  `$VESSELSTACK_DATA/mosquitto`, so existing clients keep working, but the new
  ACL, the bridge account and the display accounts do **not** exist on that
  broker. A render-only run (Control Panel "Apply configuration", or
  `install.sh --config ...` without `--start`) writes the new files but never
  recreates containers; it prints a `NOTICE` when the running broker predates
  the new definition (and when the displays bridge is enabled but not running)
  telling you to run `sudo vesselstackctl start`. In particular, enabling
  displays through Apply needs that start before the bridge can connect.
  Further configuration-only changes are applied by the same commands: the
  installer records the hash of the last applied `mosquitto.conf` in
  `config/.mosquitto-conf.sha256` and force-recreates the broker when a later
  `--start` (or `vesselstackctl start`) sees a different file, even if the file
  was rendered by an earlier render-only run.
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
  rejected (by the preflight, before anything changes) because
  `mosquitto_passwd` would store a different password; change it in the
  Control Panel first.
- Rollback: set `DISPLAYS_ENABLE=false` and run `sudo ./install.sh --config
  ... --start`; the ACL returns to administrator-only. Downgrading to a release
  without this feature: this release never modifies the legacy
  `config/mosquitto.conf`, so the older release finds it as it left it; its
  Mosquitto configuration points at the legacy
  `$VESSELSTACK_DATA/mosquitto/passwords` path and its installer recreates the
  files there, so the broker works again after the downgrade; the
  `config/mosquitto/`, `config/mosquitto-auth/` directories and
  `displays.secrets.json` are left in place. `vesselstackctl restore` extracts with `--numeric-owner` so the
  `root:1883` and `1883:1883` ownership survives a restore.
- Troubleshooting: if the broker log contains "owner is not mosquitto",
  "world readable" or "permission denied" for `/mosquitto/auth/...`, re-run
  `sudo ./install.sh --config /opt/vesselstack/config/vesselstack.env` to
  rewrite the files with the correct ownership. If sync reports that the image
  "could not be pulled", connect the boat to the internet once (or load the
  image with `docker load`) and run it again; nothing is changed before then.
  "refusing to use ... symbolic link" or "group or world writable" means the
  auth directory was tampered with: inspect and remove it, then re-run.
- Verify the upgrade path with `sudo tests/test-mosquitto-upgrade.sh`: a
  release-1.3.0 broker (single-file conf, legacy passwords, no ACL) must
  survive the new render plus `docker restart`, and only the container
  recreated from the real `compose.yml` mounts enforces the ACL.
- `tests/test-displays-envfile-systemd.sh` boots systemd in a privileged
  Debian container and checks that a SignalK token containing dollar, backtick, quote, backslash,
  hash, semicolon and percent characters and spaces reaches the service environment unchanged through
  `EnvironmentFile=` (systemd does not expand `$` or `%` there), and that
  `systemd-analyze verify` accepts the rendered unit. If the container cannot
  boot systemd it skips loudly; `python3 -m unittest` also covers a
  parser-equivalent check. `sudo tests/test-displays-bridge.sh` builds the
  environment with the installer's helper under `umask 077` and runs the bridge
  as uid 1000, so an unreadable environment is caught.
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
the MQTT topic/payload and freshness logic, the board registry, the MQTT
account/ACL provisioning (`displayctl.py sync-mqtt`), the **device lifecycle
commands** (`vesselstackctl display list|add|remove|rotate|wifi|status`), the
**SignalK-to-MQTT bridge service** and the **ESPHome project generator**
(`vesselstackctl display render|export|build|flash`, see [Firmware: generate, build and
flash](#firmware-generate-build-and-flash)) exist today (`displays/`). With
`DISPLAYS_ENABLE=true` the bridge publishes your configured instruments to
Mosquitto. The generated firmware config validates and compiles but has not yet
run on real display hardware. Building and flashing on the Pi is optional
(`vesselstackctl display build|flash`, see [Building and flashing on the
Pi](#building-and-flashing-on-the-pi)); building on a laptop remains the
primary documented path. The always-on Mosquitto ACL does affect existing installations
(see [MQTT access control](#mqtt-access-control)). `displays/` is copied to
`$VESSELSTACK_ROOT/displays` by the installer.

### Displays bridge service

`vesselstack-displays-bridge.service` (`displays/bridge.py`) subscribes to the
SignalK websocket stream (`vessels.self` only, only the paths in
`displays.json`), converts and rounds values with the same tested logic as the
topic schema below, and publishes them retained to Mosquitto. It is opt-in and
adds no listening port.

Enable it:

1. Set `DISPLAYS_ENABLE=true` in `/opt/vesselstack/config/vesselstack.env` (or
   the Displays section of the Control Panel). If SignalK security is on, also
   set `DISPLAYS_SIGNALK_TOKEN` to a SignalK access token (read access is
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
and the optional SignalK token), renders and enables the unit, and starts it with
`--start`. A render-only run (Control Panel Apply) runs `systemctl try-restart`
on the bridge so a running bridge picks up the rewritten environment; a stopped
bridge is only started by `--start` or `vesselstackctl start`. The unit runs as the unprivileged service account with the same
hardening as Boat Chat plus a read-only filesystem view; it only reads
`displays.json`. The bridge MQTT account may only publish
`vesselstack/v1/instruments/#` and `vesselstack/v1/bridge/#`.

Behaviour: on start it publishes retained `vesselstack/v1/bridge/status`
`online` (and `offline` on a clean stop; Mosquitto publishes the retained last
will `offline` if the bridge dies), reports `vesselstack/v1/bridge/signalk`
`connected` or `disconnected`, and reconnects to SignalK with exponential
backoff (1 s up to 30 s with jitter). While SignalK is unreachable the clock
keeps running, so instruments turn `stale` after their `stale_after_s`; they
read `missing` until the first value arrives. Treat `bridge/status` as the
authority: if the bridge dies hard, `bridge/signalk` keeps its last retained
value. A single SignalK message the bridge cannot process is logged (at most once a
minute, with a count of the ones suppressed) and dropped; it does not cost the
SignalK connection. The MQTT client queues at most 1000 messages while the
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
  connected: the SignalK `path` in `displays.json` is not being produced, or the
  token cannot read it.

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

### Managing displays

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
| `list` | Nothing. Table of id, name, board, page count, MQTT username, whether generated credentials exist (`SECRETS`) and whether Wi-Fi credentials are stored (`WIFI`). Works without root if the files are readable; without root the secrets/Wi-Fi columns show `unknown`. Never prints secret values. |
| `add ID --board BOARD [--name NAME] [--home-assistant-api]` | Appends the display to `displays.json` with one default page: title `Main`, the board's largest layout, and the first N instruments in `displays.json` order (N = layout capacity, fewer if you have fewer instruments). It refuses an existing id, an unknown board or a configuration with no instruments, and validates the whole resulting file before writing. It then syncs MQTT like `sync-mqtt`: creates the display password in `displays.secrets.json`, rewrites the ACL/passwords and signals the broker. It prints the MQTT username (`vsdisp-ID`) and next steps, never the password. Edit the pages in `displays.json` afterwards and check with `validate`; then generate the firmware project with `render` or `export` (see [Firmware: generate, build and flash](#firmware-generate-build-and-flash)). |
| `remove ID --yes` | Deletes the display from `displays.json` and its entry from `displays.secrets.json`, then syncs: the broker reload disconnects the connected device and refuses it afterwards. Without `--yes` it refuses. Adding the same id again generates new credentials. The Wi-Fi block and other displays are untouched. It also deletes the display's whole build tree (`build/displays/ID/`: rendered project with `secrets.yaml`, stored firmware with the compiled-in credentials, and old-layout `ID.new`/`ID.prev`; exactly those names, never another display's) and says so; it first waits for a running build or flash of that display (30 s, then fails without changing anything). |
| `rotate ID` | Regenerates every generated secret of that display (the MQTT password, the OTA password and the Home Assistant API encryption key), then syncs. Other displays are untouched. The device is refused until it is **reflashed** (over USB) with the new credentials. |
| `render ID`, `export ID` | Generate the ESPHome project for a display; see [Firmware: generate, build and flash](#firmware-generate-build-and-flash). Root. |
| `build ID` | Optional. Renders, then compiles the firmware on this machine in a one-shot container of the pinned ESPHome image and stores it with a manifest; see [Building and flashing on the Pi](#building-and-flashing-on-the-pi). Root. |
| `flash ID (--serial DEVICE \| --ota HOST) [--previous] [--force]` | Optional. Uploads the stored firmware (never compiles) over USB serial or Wi-Fi; `--previous` rolls back; refuses firmware built with credentials that are no longer current unless `--force`. Root. |
| `wifi [--open] [--clear]` | Reads the SSID and password from the terminal (password hidden) or, when stdin is not a terminal, from two lines on stdin (for automation); they are never accepted as command-line arguments. SSID: 1-32 bytes of printable UTF-8. Password: 8-63 printable ASCII characters; an empty password (open network) is accepted only with `--open`. Stored once for all displays as `"wifi": {"ssid", "password"}` in `displays.secrets.json` (mode 0600, inside `config/`, so **included in `vesselstackctl backup`**; keep archives private). `--clear` removes it and also deletes `secrets.yaml` from every already rendered project under `build/displays/*/project` (a display whose build is running is skipped with a warning); a changed password prints a notice instead. In both cases projects rendered and firmware built earlier still contain the old Wi-Fi credentials until `render`/`build` runs again (the firmware cannot be scrubbed; `flash` refuses it without `--force`). Nothing is echoed. |
| `status` | Reads the retained topics `vesselstack/v1/bridge/status`, `vesselstack/v1/bridge/signalk` and `vesselstack/v1/displays/vsdisp-ID/status` through the running Mosquitto container and prints the bridge state, the SignalK link state and each display's last retained state (`online`/`offline` as published by the bridge or device; `unknown`/`never seen` means no retained message exists, so the bridge or device has not connected since the broker's data was last cleared). It exits 1 if the broker container is not running. |

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

### Firmware: generate, build and flash

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
| `export ID (--output FILE \| --stdout) [--allow-missing-wifi]` | Root. Renders, then writes a deterministic `tar.gz` (sorted entries, fixed mtime, uid/gid 0, modes 0700/0600) containing `ID/`: the device file `ID.yaml`, `secrets.yaml`, the board and app packages, the fonts and their license, and a short `README.txt` with the build and flash commands. `--output` creates FILE exclusively with mode 0600 and refuses an existing path or a symlink; when run through `sudo` it also gives the file to the invoking user (`SUDO_UID`/`SUDO_GID`, only when both are numeric and name an existing account), still mode 0600, so you can read and delete your own archive. `--stdout` refuses a terminal. With `--stdout` the file is created by your shell, not by `vesselstackctl`: redirect inside `(umask 077; ...)` as shown, otherwise the archive, which holds secrets, is created world-readable under the usual umask 022. Warnings go to stderr, so stdout carries only the archive. |

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
| `build/displays/.ID.lock` | `root:root` | `0600` | Per-display build lock (no-follow `flock`). `render`, `export`, `build`, `flash` (and `remove`) of the same display hold it for their whole run, so two of them can never delete, rename or half-read each other's tree, or let a build rotate `firmware/` while `flash` is uploading it. A second command waits, says so, and fails after 30 s with "another build/flash for ID is running" (builds are long; try again when the first has finished). Different displays do not block each other. It is separate from the 10 s `config/.displays.lock`; the build lock is always taken first and the configuration lock only briefly inside it, so the two cannot deadlock. The empty lock file stays in place; it holds no data. |
| `build/displays/ID/project/` | `root:root` | dirs `0700`, files `0600` | The self-contained ESPHome project, including `secrets.yaml`, and the **only** directory ever mounted into a container (as `/config`). Rendering again keeps the previous project as `project.prev/` (one generation). |
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

#### Building and flashing on the Pi

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
| `flash ID --serial DEVICE` | Root. Verifies the stored `firmware.factory.bin` against `manifest.json` (sha256 and size), then copies exactly those verified bytes into a fresh root-only `0700` temporary directory under `$VESSELSTACK_ROOT/build/` (re-verifying the copy's sha256) and runs a one-shot container with `--device DEVICE` and `esphome upload /config/ID.yaml --device DEVICE --file /fw.bin`, where only that copy is mounted, read-only, at `/fw.bin`. The project is mounted read-only at `/config`; ESPHome's scratch space (`/tmp`, `/cache`) is tmpfs. The temporary directory is removed afterwards. Does not compile and never pulls the image. |
| `flash ID --ota HOST` | Root. Verifies the stored `firmware.ota.bin` the same way, then runs a one-shot container with `--network host` and `esphome upload ... --device HOST --file /fw.bin` (the verified copy, as above). The OTA password is read by ESPHome from `secrets.yaml` inside the container; it is never on a command line. |
| `flash ... --previous` | Flashes `firmware.prev/` instead (verified against its own manifest). Refused when there is no previous build. |
| `flash ... --force` | Skips the credential check below. |

**Credential check.** Compiled firmware embeds the Wi-Fi, MQTT, OTA and API secrets, and the source hash deliberately ignores them, so after `display rotate`, `display wifi`, `display wifi --clear` or a `remove` followed by `add` the stored images still carry credentials that are no longer current: the device would never join Wi-Fi or be accepted by the broker. `build` therefore records `secrets_sha256` (the sha256 of the `secrets.yaml` it compiled in; the hash only, never a secret) in `manifest.json`. Before uploading, `flash` renders the current secrets in memory (as `render` would, including generating a missing OTA password or API key) and refuses with "credentials changed since this firmware was built; run `display build ID`" when the fingerprints differ, when the credentials cannot be rendered (Wi-Fi cleared, display removed) or when the manifest predates the fingerprint. For `--previous` the refusal says the older build may carry revoked or outdated credentials. `--force` flashes anyway, for example after you restored the old credentials on purpose. The check does not stop `flash` from running against a project that was rendered after the firmware was built; rebuild to bring both in step.

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
host with `-v project:/config:ro` still loads the configuration and reaches the
connection attempt; its only complaints are about the cache and
`/config/.esphome/storage`, which `flash` redirects to tmpfs
(`--tmpfs /tmp --tmpfs /cache -e ESPHOME_DATA_DIR=/tmp/esphome-data`). Serial
upload with a read-only project is expected to behave the same but is not
verified without hardware.

`manifest.json` records `image` and `image_digest` (the pinned ESPHome image),
`esphome_version`, `vesselstack_version`, `device_name`, `source_sha256` (a hash
of the rendered project without `secrets.yaml` and `README.txt`, so it changes
when the configuration changes but never depends on a secret), the sha256 and
size of both images, `secrets_sha256` (see the credential check) and `built_at_utc`. It contains no secrets.

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
  container has only the device, and the OTA container uses the host network to
  reach the LAN. Stored firmware embeds the Wi-Fi, MQTT and OTA secrets, hence
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

#### Hardware bring-up checklist (Waveshare ESP32-S3-Touch-LCD-2.8C)

The configuration validates and compiles with the pinned ESPHome, but it has
not been run on real hardware yet. On first boot, check in this order and turn
only the matching knob in `boards/waveshare-esp32-s3-touch-lcd-2.8c.yaml` of the
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

### Configuration and validation

The runtime configuration lives at `$VESSELSTACK_ROOT/config/displays.json`.
Start from [`displays/displays.example.json`](displays/displays.example.json).
It is JSON with `schema` (must be `1`), `mqtt_host` (hostname or IP literal),
`heartbeat_s` (1-60, default 5), `instruments` and `displays`:

- An instrument (`^[a-z][a-z0-9_]{0,31}$`) has a SignalK `path` for
  `vessels.self` (numeric scalar values only), `label` (1-16 characters),
  `convert` (one named conversion), `unit` (up to 8 characters), `precision`
  (0-3), `stale_after_s` (1-3600 and at least 2 x `heartbeat_s`) and optional
  `min_interval_ms` (100-60000, default 1000).
- A display (`^[a-z]([a-z0-9-]{0,22}[a-z0-9])?$`: 1-24 characters, starting with a letter and not ending in a hyphen) has a `name`, a `board` from
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
| Waveshare ESP32-S3-Touch-LCD-2.8C (`waveshare-esp32-s3-touch-lcd-2.8c`) | 480x480 round, 70 px safe inset, 340x340 content area | `single`, `grid-2x2` | Config validated and compiles with ESPHome 2026.10.0 (`esphome config` in CI); builds on a laptop or, optionally, on the Pi (`display build`, `display flash`; flashing is untested without hardware); hardware unverified (see the [bring-up checklist](#hardware-bring-up-checklist-waveshare-esp32-s3-touch-lcd-28c)) |

`list-6` is defined but not offered on the round board.

### Planned limitations

- Version 1 uses plaintext MQTT on the local network; it provides no
  confidentiality or strong authentication against other devices on that
  network.
- With the VesselStack firewall enabled, displays must be on a trusted
  interface that can reach the Raspberry Pi's MQTT port.
- The build path is exporting from the Pi and compiling on a laptop; building
  and flashing on the Pi (a Compose profile) is planned and not available yet.
  Only the `single` and `grid-2x2` layouts are generated.
- The runtime `displays.json`, `displays.secrets.json`, ESPHome
  `secrets.yaml`, `.esphome/` and `displays/build/` output, `$VESSELSTACK_ROOT/build/` and firmware
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

The optional display firmware build keeps its toolchain cache (about 2 GB) in the
Docker volume `vesselstack-esphome-cache`. A plain `uninstall` leaves it and
prints how to remove it (`docker volume rm vesselstack-esphome-cache`);
`--purge-data --yes` removes it as well (best effort).

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

The `displays-esphome-config` CI job pulls the image pinned in
`displays/esphome/IMAGE` and runs `tests/test-displays-esphome-config.sh` on every
push and pull request. GitHub Actions uses `actions/checkout@v6`, which runs on Node.js 24 and keeps
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
sudo tests/test-displays-bridge.sh
tests/test-displays-esphome-config.sh   # renders projects and runs the pinned ESPHome "config" (Docker, ~1.2 GB image)
tests/compile-display-firmware.sh       # optional, not in CI: full "esphome compile" (~2 GB cache, 2-5 min)
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
