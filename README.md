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
git checkout v1.4.0
python3 vesselstack-wizard.py
```

Open `http://127.0.0.1:8088`, or use the SSH tunnel documented in the guide.
The wizard prints a per-run token on start; the page it serves sends that token
(`X-Wizard-Token`) with every API call. Requests are refused unless the `Host`
header is `127.0.0.1`/`localhost`/`[::1]` on the bound port, any `Origin` is the
same loopback origin, the token matches, and saves use `Content-Type:
application/json` (this blocks cross-site form posts and DNS rebinding). Open the
printed URL itself; a bookmarked older page needs a reload after restarting the
wizard. The dependency-free wizard binds only to loopback, shows relevant host
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
git checkout v1.4.0
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
| `VESSELSTACK_DATA_BIND_ADDRESS` | IPv4 address InfluxDB, Prometheus and MQTT publish on; default `127.0.0.1` (this device only) |
| `VESSELSTACK_WEB_BIND_ADDRESS` | IPv4 address Grafana and Heimdall publish on; default `0.0.0.0` (LAN dashboards), `127.0.0.1` to restrict |
| `BOAT_CHAT_ACCESS_TOKEN` | `GENERATE` (default) creates a random token, preserved on re-install; required for chat, status and data APIs |
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
| `SOCKETCAN_INTERFACE`, `SOCKETCAN_BITRATE` | CAN device and NMEA 2000 bitrate (`vcan*` interfaces ignore the bitrate) |
| `VESSELSTACK_CAN_CREATE_VCAN` | `true` creates a missing `vcan*` interface at boot (home-lab replay only; default `false`) |
| `AIS_ENABLE` | Opt in to the AIS-catcher Compose profile |
| `AIS_DEVICE` | USB bus or receiver device mapped into AIS-catcher |
| `AIS_CATCHER_ARGS` | Reviewed AIS-catcher receiver/output arguments |
| `INFLUXDB_ORG` | InfluxDB organization |
| `INFLUXDB_RAW_BUCKET` | Full-resolution SignalK data; default `signalk` |
| `INFLUXDB_HISTORY_BUCKET` | One-minute retained history; default `signalk_1m` |
| `INFLUXDB_HOME_ASSISTANT_BUCKET` | Home Assistant history bucket |
| `INFLUXDB_AIS_BUCKET` | Short-retention AIS history bucket |
| `INFLUXDB_RAW_RETENTION` | Raw-bucket retention: `720h`, `30d`, `8w`, or `0` (forever). Empty (default) = `30d` on a new install and **no change** to an existing bucket; see "Raw data retention" |
| `*_PASSWORD`, `*_TOKEN` | Keep `GENERATE` for generated values; every generated secret is preserved from the installed config on re-runs |
| `BOAT_CHAT_PROVIDER` | `local` works without an external LLM |
| `TELEGRAM_ENABLE` | Run the optional Telegram worker; requires its token and allowed chat IDs in Boat Chat Settings |
| `TELEMETRY_INDEXER_ENABLE` | Refresh Boat Chat's telemetry memory every five minutes |
| `CONTROL_PANEL_HOST`, `CONTROL_PANEL_PORT` | Administration listener; defaults to loopback on `8780` |

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

The dry run also validates the upgrade path from the installed version (so an
unsupported upgrade or a downgrade is reported before anything is overwritten)
and the Compose template. With `--offline` it also checks the wheelhouse and the
locally loaded container images (see Offline install).

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

This creates the Mosquitto password file, starts Docker services, creates the
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
enabled hardware and firewall policy, creates the MQTT password file when
missing, starts services, and idempotently provisions InfluxDB history.
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

Start, restart and the backup restart path all run `scripts/bootstrap-influx.sh`,
which first waits (up to 120 s, `INFLUXDB_READY_TIMEOUT`) for InfluxDB to report
healthy. A failed `bucket list` is retried and then reported as an error; a
bucket is created only when a *successful* listing lacks it. The admin token is
handed to the `influx` CLI through the environment, never as a command-line
argument. The one-minute downsample task averages **numeric fields only**
(string and boolean fields such as navigation state or relay status are dropped
before `mean()`, which would otherwise fail the whole run); an existing task from
an older release is updated in place to this version.

#### Raw data retention

The full-resolution bucket (`INFLUXDB_RAW_BUCKET`) used to keep data forever,
which eventually fills the boat's disk. New installs now create it with a 30-day
retention (`INFLUXDB_RAW_RETENTION`, default `30d`); the one-minute history
bucket keeps one year as before. **Upgrades never change an existing bucket
implicitly**, because shortening retention permanently deletes older raw data.
To apply a window to an existing install, set `INFLUXDB_RAW_RETENTION` (for
example `90d`, or `0` to keep forever) in `vesselstack.env` or the control panel
and run `sudo vesselstackctl restart` (or the installer with `--start`); the
bootstrap step then runs `influx bucket update`. Back up first. Removing the
value later does not restore deleted data and leaves the bucket's current
retention as is. `vesselstackctl doctor` warns when the data filesystem is more
than 80 % full (`VESSELSTACK_DISK_WARN_PERCENT` overrides the threshold) and
prints the size of the data directory.

#### Rotating secrets

`INFLUXDB_PASSWORD`, `INFLUXDB_TOKEN` and `GRAFANA_ADMIN_PASSWORD` are read by
InfluxDB and Grafana only when their data volumes are first created, so they are
**read-only in the control panel**: editing the file alone would make the config
disagree with the running service. To rotate one deliberately, change it inside
the service first, then update `config/vesselstack.env`, then re-apply:

- Grafana: `docker exec vesselstack-grafana grafana cli admin reset-admin-password NEW`
- InfluxDB password: `docker exec -e INFLUX_TOKEN=... vesselstack-influxdb influx user password -n boatadmin -p NEW`
- InfluxDB admin token: create a new all-access token with `influx auth create`,
  put it in `INFLUXDB_TOKEN`, run `sudo ./install.sh --config /opt/vesselstack/config/vesselstack.env --start`
  (updates Boat Chat and Grafana), then delete the old token with `influx auth delete`.

`MQTT_PASSWORD` is editable: the Mosquitto password file is created only when
missing and otherwise updated in place (other users are kept) when the
username/password changed since the last run, so re-running the installer no
longer resets MQTT credentials. Update Home Assistant's MQTT integration to match.

#### Virtual CAN for home-lab replay

On a lab machine without a CAN adapter you can replay a recorded log into a
virtual CAN interface and let SignalK read it as if it were the boat bus. Set

```bash
SOCKETCAN_ENABLE="true"
SOCKETCAN_INTERFACE="vcan0"
VESSELSTACK_CAN_CREATE_VCAN="true"   # creates vcan0 (modprobe vcan + ip link add) at boot
```

For `vcan*` interfaces VesselStack renders `ip link set vcan0 up` only: virtual
CAN has no bitrate and rejects `type can`/`restart-ms`, so `SOCKETCAN_BITRATE` is
ignored. With the default `VESSELSTACK_CAN_CREATE_VCAN=false` the interface must
already exist (`sudo ip link add dev vcan0 type vcan`). Physical `can*` interfaces
are unchanged (bitrate 250000 by default, never auto-created). Then replay a
log you recorded yourself with `candump -l can0` using `can-utils`:

```bash
sudo apt install can-utils
canplayer -I /path/to/your-candump.log vcan0=can0   # add -l i to loop forever
```

Point the SignalK NMEA 2000/SocketCAN connection at `vcan0`. Recorded logs
contain vessel identifiers and positions: keep them outside the repository (the
public-tree check rejects `.log` files) and never commit or share them.

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

Open `http://HOST:8765`. Chat requires the access token
(`BOAT_CHAT_ACCESS_TOKEN` in `config/boat-chat.env`) and refuses requests without
it unless `BOAT_CHAT_ALLOW_OPEN=true` is set. The `local` provider returns gathered context without
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

The installer generates `BOAT_CHAT_ACCESS_TOKEN` (random, preserved on
re-install) and writes it to `config/boat-chat.env`. Chat, feedback, session,
live status, insights, and maintenance endpoints all require it in the
`X-Boat-Chat-Token` header; the web UI asks for it through **Connect** and keeps
it only in session storage, and the Telegram bridge sends it automatically. Find
it with:

```bash
sudo grep '^BOAT_CHAT_ACCESS_TOKEN=' /opt/vesselstack/config/boat-chat.env
```

An empty token **fails closed**: all of those endpoints return 401. A reviewed,
trusted installation can opt out by adding `BOAT_CHAT_ALLOW_OPEN=true` to
`config/boat-chat.env`; this key is deliberately not editable from the Settings
page. `BOAT_CHAT_REQUIRE_ACCESS_TOKEN` is retired and no longer disables
protection.

#### Settings and configuration precedence

Boat Chat reads two files; both Boat Chat and the Telegram bridge resolve them
the same way (`boat-chat/env_config.py`):

1. **Settings-page overrides** (highest): `$VESSELSTACK_DATA/boat-chat/boat-chat.env`,
   selected by `BOAT_CHAT_ENV`. This is the only settings file the service user
   can write.
2. **Base configuration**: `config/boat-chat.env`, written by the installer and
   control panel and loaded by systemd into the environment.

Saving a value on the Settings page that equals the base value stores no
override. Saving a Boat Chat field in the control panel removes that key from
the override file so the new value takes effect, and the panel marks any field
that is currently overridden. Settings writes require `BOAT_CHAT_SETTINGS_TOKEN`
from any address; with no settings token they are accepted from loopback only
(private-LAN addresses are no longer trusted). `TELEGRAM_ALLOWED_CHAT_IDS` is a
secret setting and is never returned by `GET /api/settings`.

`BOAT_CHAT_WEB_SEARCH=true` enables the OpenAI Responses API web-search tool
when the OpenAI provider is selected. This can send the question and grounded
context to an external service and may incur provider charges; it is disabled
by default. Boat Chat sessions, audits, feedback, and maintenance tasks stay in
the private SQLite memory database
(`$VESSELSTACK_DATA/boat-chat/memory/boat_memory.sqlite`, power observations in
`$VESSELSTACK_DATA/boat-chat/data/power_tracking.sqlite`) and are excluded from
release packages. Settings values containing a newline, carriage return or NUL
byte are rejected (HTTP 400, nothing written), matching the control panel.

`vesselstackctl urls` prints the effective bind address of each service (read
from `config/boat-chat.env`, `config/control-panel.env` and the Compose bind
variables, never by sourcing service-writable files), e.g. whether Boat Chat is
reachable on all interfaces or only this device.

Hardening of service-writable paths: the control panel clears stale Settings
overrides without following symlinks (it refuses a symlinked override file,
writes through a random `O_EXCL` temporary file and sets mode/owner on the open
descriptor).

**Data-directory ownership (no check-then-use race).** `VESSELSTACK_DATA` is
`root:<service group>` mode 0750: the service user can traverse it but cannot
create, rename, replace or symlink anything directly in it. Only the leaf mount
roots belong to the accounts that must write them: `influxdb` (uid/gid 1000),
`grafana` (uid 472, gid 0) and `boat-chat` (service user; native SignalK state
`signalk` likewise). Intermediate directories the installer writes through
(`homeassistant`, `homeassistant/blueprints/...`) must be root-owned and not
group/world-writable. Before touching anything the installer refuses, with a
message naming the path, a symlink at any managed entry (`influxdb`, `grafana`,
`boat-chat`, `signalk`, `mosquitto`, `homeassistant/...`) or a managed
intermediate directory that is not root-owned; fix it as the message says and
rerun. Because the service user cannot alter the parent, a symlink cannot be
swapped in between the check and the `install -d`/`chown`. Legacy runtime
state is copied and chowned in a root-only staging directory and published with
`rename(2)` (which never follows a symlink at the destination); a destination
that already exists or is a symlink sends the legacy copy to quarantine instead.
Upgrade: installs from earlier 1.4.0 builds had a service-owned data root; the
first run of this installer takes ownership of the data root (not recursive:
existing database files keep their owners) and moves old quarantine directories
out of it. Rollback: `chown -R` is not needed; reinstalling the previous release
makes the data root service-owned again, and restoring a backup keeps recorded
owners.

Security layout (1.3.x hardening): the installed application code, its Python
virtualenv and the generated `boat_facts.json` are owned by `root` and are not
writable by the service account (`boat_facts.json` is `root:SERVICE 0640`).
Everything Boat Chat writes at runtime (memory/session database, power database,
Telegram state and the Settings-page `boat-chat.env`) lives under
`$VESSELSTACK_DATA/boat-chat` (default `/opt/vesselstack-data/boat-chat`,
selected by `BOAT_CHAT_STATE_DIR`). The chat, Telegram and telemetry-indexer
units run with `ProtectSystem=strict`, `ProtectHome=true`, `NoNewPrivileges=true`,
`PrivateTmp=true` and `ReadWritePaths=$VESSELSTACK_DATA/boat-chat`. Consequences:
`VESSELSTACK_ROOT`/`VESSELSTACK_DATA` may not be under `/home` or `/root`, and the
`codex_cli`/`claude_cli` providers cannot use binaries or credentials kept in a
home directory (install them system-wide and set `BOAT_CHAT_CODEX_BIN` /
`BOAT_CHAT_CLAUDE_BIN` in the root-owned `config/boat-chat.env`; these two
settings are no longer accepted from the web Settings page or the Settings
`boat-chat.env`). On the first install/upgrade the installer stops the chat
workers, moves any legacy `boat-chat/memory`, `boat-chat/data` and
`boat-chat/boat-chat.env` into the new state directory (refusing symlinks),
recreates the virtualenv as root and restarts the services only with `--start`.
Rollback: restore the pre-update backup (`vesselstackctl restore`) or reinstall the
previous release; the legacy files are moved, not copied, so keep the backup
taken by `vesselstackctl update`. `systemd-analyze verify` was not run in the
development environment; run it on the Pi after upgrading
(`systemd-analyze verify /etc/systemd/system/vesselstack-chat*.service`).

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

## Network security

Compose now publishes InfluxDB `8086`, Prometheus `9090`, and MQTT `1883` on
`127.0.0.1` by default (`VESSELSTACK_DATA_BIND_ADDRESS`). Grafana `43000` and
Heimdall `80/443` remain reachable on every interface by default
(`VESSELSTACK_WEB_BIND_ADDRESS=0.0.0.0`) because the documented dashboards are
used over the boat LAN; set it to `127.0.0.1` to keep them local. Grafana still
reaches InfluxDB and Prometheus by service name, and host-networked Home
Assistant reaches MQTT on loopback. AIS ports are unchanged. At minimum:

- restrict Grafana `43000` and Heimdall to trusted management networks, and set
  `VESSELSTACK_DATA_BIND_ADDRESS` to a trusted LAN/VPN IPv4 address only if
  external devices must publish to MQTT or InfluxDB;
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
Review the policy without root or any change using
`vesselstack-firewall print` (renders the exact `iptables-restore` payloads).
Chain contents are replaced atomically (`iptables-restore --noflush`), so an
existing chain is never briefly empty. The host (INPUT) rules are applied first;
`DOCKER-USER` only exists once Docker is running, so the service waits up to 60 s
for it (`TimeoutStartSec=180`) and, if Docker is still absent, creates the chain
itself. Until Docker starts no container ports are published, so that window
exposes nothing. A missing untrusted interface no longer aborts: rules are
installed anyway (with a warning) and take effect when the interface appears.

Strict mode (opt-in, default off) is for boats where nothing should be reachable
over Wi-Fi except what you list:

```bash
VESSELSTACK_FIREWALL_STRICT="true"
VESSELSTACK_FIREWALL_ALLOW_TCP="22"        # comma-separated ports or LOW:HIGH ranges
VESSELSTACK_FIREWALL_ALLOW_UDP=""
```

On the untrusted interface strict mode drops everything except replies to
connections the boat started, ICMP/ICMPv6, DHCP client replies and the
allow-lists. That covers host-networked SignalK `3000`, AIS, Boat Chat `8765`
(INPUT) and published container ports such as Heimdall `80/443` and Grafana
(`DOCKER-USER`, matched on the published port). The allow-list therefore opens
ports on both paths; the default `22` only lets SSH in. Keep Ethernet, a VPN or
console access before enabling it: a wrong interface name or allow-list can lock
you out of Wi-Fi access, and remote `ssh` on that interface needs `22` listed.
Rollback: set `VESSELSTACK_FIREWALL_STRICT="false"` and
`sudo systemctl restart vesselstack-firewall.service` (or `vesselstack-firewall
remove`). IPv6 is restricted only for INPUT; Docker's IPv6 forwarding is not
managed. The rules were rendered and tested but not loaded into a live kernel
in the development environment; confirm with `sudo iptables -S
VESSELSTACK-INPUT` on the Pi.

Disable and remove its chains with:

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
the application or data tree, including through a symlink. Destinations under
`/media` or `/mnt` must be a mounted filesystem (`mountpoint -q`), so a missing
USB drive can no longer silently fill the SD card; pass
`vesselstackctl backup --allow-unmounted DIR` (or set
`VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=true`, which also applies to
`vesselstackctl update`) to override. A warning is printed when the destination
is on the same filesystem as the data. Besides the application and data trees the
archive now includes `/etc/systemd/system/vesselstack-*`,
`/usr/local/sbin/vesselstack{ctl,-firewall}` and `/etc/vesselstack/update.env`
when they exist, so a restore across versions is complete.

The whole stack, including Home Assistant, stays stopped for the copy. Home
Assistant, Grafana, InfluxDB and Boat Chat all keep SQLite or write-ahead-log
state in the data tree, and a file copy of a running database is not
crash-consistent, so partial stops were rejected in favour of a consistent
archive.

Restore only during a maintenance window. Verification rejects path traversal,
absolute or escaping links, device nodes, FIFOs and any setuid, setgid or sticky
member (a legitimate backup never contains one) before anything is extracted:

```bash
sudo vesselstackctl restore /path/to/vesselstack-TIMESTAMP.tar.gz --yes
```

Restore no longer extracts over the live tree. It (1) verifies the archive,
(2) checks free space (staging holds a second full copy next to the live trees;
the previous trees are kept too, so you need room for old + new), (3) extracts
as root with the recorded numeric owners into a private `.vesselstack-restore.*`
directory beside the data directory, (4) stops the stack, (5) renames the live
trees to `/opt/vesselstack.pre-restore-STAMP` and
`/opt/vesselstack-data.pre-restore-STAMP` and moves the staged trees in (so
InfluxDB WAL/shard files created after the backup cannot mix with the restored
ones), (6) restores the unit files, control scripts and `update.env` by rename,
and (7) runs the restored bundled installer
(`/opt/vesselstack/installer/install.sh --config ... --start`) to rebuild the
Boat Chat virtualenv (not part of the backup), re-render units and start the
stack. This step needs network access for `pip`; use `--skip-installer` to
only start services and run the printed installer command later. If the swap
fails the previous trees are put back. `/opt/vesselstack` and
`/opt/vesselstack-data` must be plain directories on one filesystem with their
parent; restore refuses a mount point (restore manually in that case).

The `*.pre-restore-STAMP` trees are kept until you delete them (they hold the
pre-restore config, secrets and databases, and are not covered by `uninstall
--purge-data`). Verify the system first, then
`sudo rm -rf -- /opt/vesselstack.pre-restore-STAMP /opt/vesselstack-data.pre-restore-STAMP`.
Rollback of a restore: stop the stack, move the current trees aside and rename
the `.pre-restore-STAMP` trees back.

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

Review Compose and release notes before applying container changes. Prefer the
installed configuration as input. If you do re-run with a file that still says
`GENERATE` (for example a fresh copy of the example), the installer now keeps
the already-installed InfluxDB, Grafana, MQTT, Boat Chat and control-panel
secrets instead of generating new ones. A non-`GENERATE` value you set
explicitly still wins.

Once a reviewed release directory is downloaded, the lifecycle command can
perform preflight, a verified pre-update backup, installation, and startup:

```bash
sudo vesselstackctl update /path/to/VesselStack-release
sudo vesselstackctl update --offline /path/to/VesselStack-release   # no network
```

For Control Panel updates, stage the reviewed release beneath its restricted
root first. The panel rejects symlinks, missing release metadata, paths outside
this directory, and any entry anywhere in the release tree (`scripts/`,
`boat-chat/`, `templates/`, `systemd/`, ...) that is a symlink, owned by a user
other than root/the panel account, or group/world-writable:

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
`/opt/vesselstack/config/installed-version`. Any upgrade from 0.1.0 or newer to a
newer release is accepted: when a release needs no data change only the version
marker moves, and releases that do need one ship an executable
`scripts/migrations/<version>.sh` that runs once, in version order. The 1.0
migration supports 0.1 installations and retains legacy InfluxDB names when the
new bucket settings are absent.

**Downgrades are not supported.** Configuration and data may already use the
newer schema, so `install.sh` (including `--dry-run`) and `migrate.sh` refuse to
install an older release over a newer one and change nothing. To go back, restore
a backup taken before the upgrade (`sudo vesselstackctl restore ...`, see
Backups) or install the newer release again. Unknown or malformed version
markers are refused instead of guessed at.

Install order is transactional. Preflight (`--dry-run` runs the same checks)
validates the upgrade path, the Compose template and, offline, the wheelhouse and
local images **before** any installed file is touched. The Python environment is
then built in a staging virtualenv next to the install; only if that succeeds are
the environment, units and code rewritten, the staged virtualenv swapped in, and
`installed-version` written last. A failed or offline-without-bundle run
therefore leaves the previous installation, `vesselstack.env`, units and version
marker exactly as they were (the installer says "Nothing was changed").
Rollback of a completed upgrade is a restore of the pre-update backup that
`vesselstackctl update` takes automatically.

### Offline install (no internet on the boat)

Prepare a bundle on the home lab or any connected machine, carry it to the
boat, then install without any network access:

```bash
# Connected machine (needs docker and python3 with pip). Defaults target
# Raspberry Pi OS 64-bit with Python 3.11 and linux/arm64 images.
scripts/prepare-offline-bundle.sh --output ./offline-bundle
#   other targets: --python-version 3.13  --platform manylinux_2_39_aarch64
#   same architecture as this machine:     --native
#   see the image list without downloading: --list

# Copy ./offline-bundle (wheelhouse/, images/, MANIFEST.sha256, BUNDLE_INFO) and
# a copy of this release to the boat. As root the bundle must be root-owned and
# not group/world-writable along its whole path, so make it so first:
sudo chown -R root:root /path/to/offline-bundle && sudo chmod -R go-w /path/to/offline-bundle
sudo scripts/prepare-offline-bundle.sh --load /path/to/offline-bundle   # verifies the bundle, docker load
sudo VESSELSTACK_WHEELHOUSE=/path/to/offline-bundle/wheelhouse \
  ./install.sh --config /opt/vesselstack/config/vesselstack.env --offline --dry-run
sudo VESSELSTACK_WHEELHOUSE=/path/to/offline-bundle/wheelhouse \
  ./install.sh --config /opt/vesselstack/config/vesselstack.env --offline --start
```

The bundle contains a wheelhouse (`pip download --require-hashes
--only-binary=:all:` of the release's hash-pinned `boat-chat/requirements.lock`
for the chosen platform, so only wheels whose sha256 the lock lists are fetched
and no source distribution is ever built), a `docker save` tarball for every
image in `templates/compose.yml` (all profiles) plus the Mosquitto helper image,
`BUNDLE_INFO` (release, platform, image list, `lock_sha256`) and
`MANIFEST.sha256`. It holds no configuration, credentials or vessel data.

Before installing or loading anything, `--verify DIR`, `--load DIR` and
`install.sh --offline` refuse the bundle unless: the manifest lists exactly the
files present (no extra, unlisted or missing files, no symlinks) and every
checksum matches; `BUNDLE_INFO` names this release and the sha256 of **this
release's** `requirements.lock` (the manifest is anchored to the release content,
not only to itself); every wheelhouse file is a `.whl` whose sha256 is in the
lock; and, when running as root, the bundle path chain and every file in it are
root-owned and not group/world-writable. `install.sh --offline` also requires
`VESSELSTACK_WHEELHOUSE` to be the `wheelhouse/` directory of such a bundle, then
runs `pip install --no-index --find-links $VESSELSTACK_WHEELHOUSE
--only-binary=:all: --require-hashes -r boat-chat/requirements.lock`, so even a
bundle forged consistently cannot install a wheel the signed release did not
pin. Containers start with `docker compose up -d --pull never`. If an image is
missing the installer lists it with the `docker load` hint: `--start` fails
before changing anything, while a plain render only warns. Offline mode never
contacts a registry or PyPI; SignalK/InfluxDB version bumps still need a new
bundle. Image tarballs are covered by the manifest and the root-controlled path
check but, unlike wheels, are not pinned by digest in the release (Compose images
are tag-pinned).

Online installs use the same lock (`--only-binary=:all: --require-hashes`), so
the Python environment is the same hash-verified set either way. The lock lists
Linux aarch64 and x86_64 wheels for Python 3.11-3.13; on another platform or
architecture the install fails with a clear message instead of building from
source. A release without `boat-chat/requirements.lock`, or a lock missing for
an offline bundle build, is refused before anything changes.

Maintainers: when `boat-chat/requirements.txt` changes, run
`scripts/update-requirements-lock.sh` (needs network; `--python-version` and
`--platform` select targets) and commit the regenerated lock. It is generated
trust-on-first-use from PyPI: review the diff and compare the hashes with the
digests PyPI publishes. `tests/test_requirements_lock.py` fails when the lock is
stale. `scripts/deploy-chat.py` treats a changed `requirements.lock` like a
changed `requirements.txt` and refuses the code-only deploy.

`sudo vesselstackctl update --offline /path/to/release` (or
`VESSELSTACK_OFFLINE=true sudo -E vesselstackctl update /path/to/release`) passes
`--offline` to both the preflight and the install of that release; any value other
than `true`/`false` in `VESSELSTACK_OFFLINE` is rejected. Without the flag or
variable the behaviour is unchanged apart from the hash-pinned lock. Rollback:
restore the pre-update backup; to return to the previous behaviour revert this
change (the lock file is inert for older releases).

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
sudo test -s /opt/vesselstack-data/mosquitto/passwords
sudo docker logs vesselstack-mosquitto --tail 100
```

### Container mount permissions fail

Inspect the affected container log and mount. Do not recursively change all
ownership: containers use different internal user IDs.

## Developer validation

GitHub Actions uses `actions/checkout@v6`, which runs on Node.js 24 and keeps
persisted checkout credentials outside the repository Git configuration.
External registry manifest checks retry short transport failures three times;
missing Linux ARM64 support still fails the release gate.

`tests/test-distribution.sh` runs `shellcheck` at full severity (including
info-level findings) on the shell scripts it lists, and they currently pass
with no findings. The style `A && B || handler` is avoided (SC2015); use
`if ! { A && B; }; then ... fi` instead.

Root-controlled test fixtures (`tests/test-offline-install.sh`,
`tests/test-update-launcher.sh`) are created under `/usr/local/share/vs-test`,
not `/opt`: the product refuses root-used paths whose chain is not root-owned
and group/world-writable-free, and GitHub runners ship `/opt` as mode 0777. The
tests verify the whole base chain is root-controlled and fail clearly otherwise;
the product check is unchanged. The directory is left empty after a run and can
be removed with `sudo rmdir /usr/local/share/vs-test`.

```bash
tests/test-distribution.sh
sudo tests/test-lifecycle.sh
tests/test-migrations.sh
sudo tests/test-clean-install.sh
tests/test-image-manifests.sh
tests/test-service-integration.sh
tests/test-build-package.sh
tests/test-socketcan.sh
sudo tests/test-offline-install.sh
sudo tests/test-update-launcher.sh
docker compose --env-file vesselstack.example.env \
  -f templates/compose.yml config --quiet
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m unittest discover -s tests -p 'test_control_panel.py' -v
```

Build a sanitized release:

```bash
./build-package.sh
(cd generated && sha256sum -c vesselstack-1.4.0.tar.gz.sha256)
```

The package is produced by `git archive` of the committed `HEAD`, so untracked
or ignored files (`.venv`, `boat-chat/boat_facts.json`, env files, memory/data,
logs) can never enter it, and uncommitted edits to tracked files are excluded
(with a warning; `VESSELSTACK_BUILD_REQUIRE_CLEAN=true` makes that an error). The
tarball contains `COMMIT` and `BUILD_INFO` naming the exact source commit, and
the build prints the sha256 and that commit. `tests/test-build-package.sh`
proves private and untracked files are not packaged. Building outside a git
checkout is refused.

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

**Upgrade notes (secure defaults).** After upgrading, re-run the installer so it
generates `BOAT_CHAT_ACCESS_TOKEN` and renders the new bind variables. Until it
does, Boat Chat answers 401 to chat, status and data requests, and Compose
publishes InfluxDB, Prometheus and MQTT on loopback. Reconnect each browser with
**Connect** using the new token. External devices that publish to MQTT or
InfluxDB over the LAN stop working until `VESSELSTACK_DATA_BIND_ADDRESS` is set
to a trusted address. Settings writes lose the old private-LAN trust (use the
settings token). Scoped read-only Influx tokens for Boat Chat and Grafana are
not implemented (deferred: InfluxDB cannot be given a chosen token value, so a
scoped token must be created after start and written back into the config and
two services, which is fragile); both still use the admin token.

**Verification.** `python3 -m unittest discover -s tests -p 'test_*.py'`,
`tests/test-distribution.sh`, `curl -i -X POST http://127.0.0.1:8765/api/chat`
(expect 401 without the token) and
`docker compose --env-file /opt/vesselstack/config/vesselstack.env -f /opt/vesselstack/compose.yml config`
(ports show `127.0.0.1:` for InfluxDB, Prometheus and MQTT).

**Rollback.** Restore the previous release with the `vesselstack-update`
rollback; to temporarily restore the old network exposure set
`VESSELSTACK_DATA_BIND_ADDRESS="0.0.0.0"` and re-run the installer. The generated
`BOAT_CHAT_ACCESS_TOKEN` line is harmless to older releases.

The owner overview now distinguishes missing/stale readings and contradictory
health indicators, displays observation age, refreshes periodically and retains
explicitly outdated information during connection loss. Chat preserves per-answer
evidence and feedback, renders responses as text, and handles cancellation and
conversation restoration. Complex explanations keep computed evidence intact.
VesselStack's configured URLs, buckets, timezone and branding remain supported.

Live status remains protected by a dedicated access token by default. Configure
`BOAT_CHAT_ACCESS_TOKEN` in private Boat Chat settings before connecting the owner
UI. `BOAT_CHAT_ALLOW_OPEN=true` is an explicit opt-out for a reviewed, trusted
installation; it is not the public default. Settings writes retain their
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

**Upgrade notes (secrets, readiness, retention, update trust).**

- Re-running the installer no longer regenerates InfluxDB/Grafana/MQTT secrets;
  the control panel shows the init-only ones as read-only (see "Rotating
  secrets"). The Mosquitto password file is no longer truncated on every start.
- `bootstrap-influx.sh` waits for InfluxDB health and treats list errors as
  errors. The downsample task is updated once to the numeric-only version.
- New installs keep 30 days of raw data. Existing buckets are unchanged unless
  you set `INFLUXDB_RAW_RETENTION` (deletes older raw data; back up first).
- `vesselstack-update` now verifies signatures with a root-controlled keyring
  (`VERIFY_GNUPGHOME` / `VERIFY_ALLOWED_SIGNERS`). **Before the next update,
  import the maintainer key there**, or verification fails (the checkout
  owner's keyring is no longer consulted).
- Legacy `boat-chat/{memory,data,boat-chat.env}` left under the code tree
  because the new location already existed are moved to a root-only
  `/var/lib/vesselstack/quarantine/boat-chat-legacy-<timestamp>.XXXXXX` directory
  (see below) with a warning; nothing is deleted. Review, merge what you need and remove it.

**Verification.** `python3 -m unittest discover -s tests -p 'test_*.py'`,
`tests/test-distribution.sh`, `tests/test-clean-install.sh` and
`tests/test-update-launcher.sh` (as root, on Linux),
`vesselstackctl doctor` (disk line), and
`docker exec vesselstack-influxdb influx bucket list` after `restart`.

- The legacy quarantine moved out of the data tree: it is now
  `/var/lib/vesselstack/quarantine/boat-chat-legacy-<timestamp>.XXXXXX` (root
  0700, verified not to be a symlink), outside every service-writable path.
  Quarantine directories that earlier 1.4.0 builds created in
  `$VESSELSTACK_DATA` are moved there on the next install.
- The installer now installs `vesselstack-update` (`/usr/local/sbin`) and its
  helper (`/usr/local/libexec/vesselstack/deploy-chat.py`) root-owned; see
  "One-command application updates" for the new root-only rules.

**Rollback.** Install the previous release's scripts and re-run the installer;
secrets are preserved either way. Unset `INFLUXDB_RAW_RETENTION` (deleted raw data
is not recoverable; restore from backup if needed). A pre-existing downsample task
can be left as is. For the update trust change set `ALLOW_UNSIGNED_SOURCE="true"`
temporarily (home lab only) or import the key. The quarantine directory can be
deleted once reviewed.

### One-command application updates

`install.sh` installs the launcher (`/usr/local/sbin/vesselstack-update`) and the
helper it runs (`/usr/local/libexec/vesselstack/deploy-chat.py`), both
`root:root` mode 0755 in root-owned directories. **Bootstrap:** the first copy of
the helper comes from running the installer once (`sudo ./install.sh --config ...`);
until then the launcher refuses to run as root. New helper versions only arrive
the same way, i.e. through a reviewed release installed by `install.sh` /
`vesselstackctl update`, never from the update flow itself. Then create the
private per-machine configuration once:

```bash
sudo install -d -m 0755 /etc/vesselstack
sudo install -m 0600 templates/update.env.example /etc/vesselstack/update.env
sudoedit /etc/vesselstack/update.env
```

As root the launcher enforces: (1) `update.env` and every parent directory up to
`/` must be root-owned and not group/world-writable (the file is sourced as
root), and it must not live inside `SOURCE_DIRECTORY`; (2) it executes **only**
the installed helper, never `scripts/deploy-chat.py` from the checkout, and
only if that file and its whole path chain are root-owned and not
group/world-writable; (3) the helper itself also refuses to run as root from a
location an unprivileged user controls (this includes calling a source-checkout
copy directly with `sudo python3 .../scripts/deploy-chat.py`; use the installed
copy). Home-lab development as a normal user (non-root) still works: the
launcher then prefers `scripts/deploy-chat.py` from the checkout (falling back to
the installed copy) and applies none of the root-ownership checks; the helper's
own pull/verify/test logic is unchanged. Upgrade: run the installer once; the
old manually installed launcher keeps working only after the helper exists.
Rollback: reinstall the previous release (its installer places the previous
launcher/helper); nothing else changes.

Set the separate source/live paths and actual chat/Telegram unit names. After
pushing changes to the source checkout's upstream branch, run on the boat:

```bash
sudo vesselstack-update          # fetch, verify, test, preview; no service restart
sudo vesselstack-update --apply  # same, then back up, apply and verify
```

Trust model: pulled source is untrusted until verified. The launcher refuses to
fetch unless the checkout's `origin` equals `EXPECTED_REMOTE_URL` (default
`https://github.com/jclima/VesselStack.git`). **Root never runs `git archive` or
`git verify-*` inside the checkout**, whose `.git/config` filters,
`.git/info/attributes` and hooks the checkout owner controls. Instead root
fetches the pinned remote's branches and tags into its **own root-owned bare
mirror** (`UPDATE_MIRROR_DIR`, default `/var/lib/vesselstack/update-mirror`,
mode 0700, fsck-checked fetch; without `--pull` the checkout's refs are imported
into the mirror over file transport), requires the fetched commit there to have
a valid signature (`git verify-commit`) or a validly signed tag
(`git verify-tag`), and only then fast-forwards the checkout to that exact
commit. Verification runs
with a fixed configuration (`git -c core.hooksPath=/dev/null -c gpg.program=gpg`,
system and global git config ignored, no repository-local overrides) against a
**root-controlled** trust anchor, not the checkout owner's keyring:
`VERIFY_GNUPGHOME` (a GNUPGHOME holding only the maintainer public key) or
`VERIFY_ALLOWED_SIGNERS` (an SSH allowed-signers file) in `update.env`. When
running as root, that path and its parents must be root-owned and not
group/world-writable. After verification the tests and the deployed bytes both
come from the verified commit object, exported **as raw blobs** from the mirror
(`git ls-tree -r -z` + `git cat-file blob`; no `git archive`, so attributes,
`export-subst`, filters and textconv cannot change bytes; symlinks, submodules,
unsafe paths and modes other than 100644/100755 are refused) into a private tree
the test user cannot write, not the working tree; HEAD, the clean state and the tree
are re-checked after the tests and again just before apply, and any change aborts. `scripts/check-public-tree.py` and
the unit tests run as the checkout owner via `runuser`, never as root; a
root-owned checkout is refused. Import the maintainer signing key into that root-controlled keyring before first
use, e.g. `sudo install -d -m 700 /etc/vesselstack/gnupg` then
`sudo GNUPGHOME=/etc/vesselstack/gnupg gpg --import maintainer.asc`. Home-lab only: set `ALLOW_UNSIGNED_SOURCE="true"` in
`/etc/vesselstack/update.env` to accept unsigned source (a warning is printed);
do not enable it on a vessel others can push code to. The checkout owner can no longer
change what root deploys after verification, because root deploys the verified
commit's content. Upgrade note: 1.4.0 adds the mirror;
no action is required (it is created on first run) but a root-owned,
non-writable `/var/lib/vesselstack` path is enforced. Roll back
by restoring a printed backup (below) or by setting `ALLOW_UNSIGNED_SOURCE`
back to `false`.

If Telegram code changes, an already-active configured worker also restarts;
a stopped worker remains stopped. Restart units are saved in the backup receipt
and used during rollback. Restore a printed backup directory with
`sudo vesselstack-update --rollback /path/to/backup-directory`.

These commands update Boat Chat Python/static assets. Dependencies, migrations,
service definitions, dashboards and container/infrastructure changes require the
separate reviewed release workflow. The launcher, helper and config stay installed
locally across Git pulls; they change only when you run a reviewed release's
`install.sh` (the update flow never replaces its own helper).
See [development/update details](docs/DEVELOPMENT.md).
