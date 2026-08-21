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

The installer generates credentials, renders a pinned Docker Compose stack,
creates a confined systemd service for Boat Chat, creates configurable InfluxDB
buckets, provisions the one-minute downsample task expected by Boat Chat,
provisions Grafana datasources and a system-health dashboard, and installs the
`vesselstackctl` lifecycle command.

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
2. Confirm ports `80`, `443`, `1883`, `8086`, `8123`, `8765`, `9090`, and
   `43000` are free.
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
| `BOAT_TIMEZONE` | IANA timezone, such as `America/Los_Angeles` |
| `BOAT_UNITS` | `us_customary` or `metric` |
| `VESSELSTACK_USER` | Existing non-root service account |
| `VESSELSTACK_UID`, `VESSELSTACK_GID` | Derived from the service account by the installer |
| `VESSELSTACK_ROOT` | Application path; default `/opt/vesselstack` |
| `VESSELSTACK_DATA` | Persistent container-data path |
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

Never commit `vesselstack.env`. Generated secrets are stored in mode-600 files
under `/opt/vesselstack/config/` and are not printed.

## 5. Run preflight

The dry run checks commands, Docker Compose, configuration, paths, and the
service account without writing system configuration:

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
enables (but does not start) `vesselstack-chat.service`, and validates Compose.

Review the rendered files:

```bash
sudo docker compose \
  --env-file /opt/vesselstack/config/vesselstack.env \
  -f /opt/vesselstack/compose.yml config

sudo systemd-analyze verify /etc/systemd/system/vesselstack-chat.service
```

## 7. Start VesselStack

```bash
sudo ./install.sh --config vesselstack.env --start
```

This creates the Mosquitto password file, starts Docker services, creates the
configured InfluxDB buckets and one-minute downsample task, and starts Boat
Chat. Allow several minutes for initial image downloads and Home Assistant.

## 8. Verify the installation

```bash
sudo docker compose \
  --env-file /opt/vesselstack/config/vesselstack.env \
  -f /opt/vesselstack/compose.yml ps

sudo systemctl status vesselstack-chat.service --no-pager
curl -fsS http://127.0.0.1:8765/health | jq
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

### Heimdall

Open `http://HOST` and add trusted-network links to Home Assistant, Grafana,
SignalK, Boat Chat, InfluxDB, and Prometheus.

## Network security

Compose publishes several ports on every host interface. At minimum:

- restrict InfluxDB `8086`, Grafana `43000`, Prometheus `9090`, and MQTT `1883`
  to trusted management networks;
- keep Home Assistant, SignalK, and Boat Chat off the public internet unless
  protected by an authenticated reverse proxy;
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

Restore only during a maintenance window. This replaces installed config and
data after checksum and archive-path validation:

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

The installer records the configuration schema in
`/opt/vesselstack/config/installed-version` and accepts only explicit migration
edges. It refuses unknown or future schemas instead of guessing. The 1.0
migration supports 0.1 installations and retains legacy InfluxDB names when
the new bucket settings are absent.

## Stopping or removing services

Stop services without deleting persistent data:

```bash
sudo systemctl stop vesselstack-chat.service
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

See `CHANGELOG.md` for release notes and `VALIDATION.md` for the full evidence
and hardware-dependent commissioning limits.
