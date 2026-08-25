# VesselStack

Self-hosted monitoring, automation, and diagnostics for connected boats.
VesselStack targets Raspberry Pi OS and Debian-family boat computers and
combines Home Assistant, SignalK-compatible history, Grafana, MQTT,
infrastructure monitoring, and a local diagnostic assistant.

> [!WARNING]
> VesselStack is not certified navigation, collision-avoidance, engine-control,
> alarm, or life-safety equipment. Maintain independent marine instruments and
> alarms. Never depend on this software as the sole source of safety data.

## What version 1.0 installs

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

The 1.0 installer supports an existing SignalK server, an opt-in
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
git checkout v1.0.1
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
all configuration and data with the explicit destructive confirmation:

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

```bash
tests/test-distribution.sh
sudo tests/test-lifecycle.sh
tests/test-migrations.sh
sudo tests/test-clean-install.sh
tests/test-image-manifests.sh
tests/test-service-integration.sh
docker compose --env-file vesselstack.example.env \
  -f templates/compose.yml config --quiet
python3 -m unittest discover -s tests -p 'test_boat_chat.py' -v
python3 -m unittest discover -s tests -p 'test_control_panel.py' -v
```

Build a sanitized release:

```bash
./build-package.sh
(cd generated && sha256sum -c vesselstack-1.0.1.tar.gz.sha256)
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
