# VesselStack

![VesselStack banner](brand/github-banner.png)

VesselStack is a private, self-hosted companion for your boat. It keeps useful
boat data, dashboards, automation, and diagnostics together on a Raspberry Pi
you control—at home, in the lab, or aboard. It is designed to keep working on
the boat's local network when the internet is unavailable.

## Contents

- [Where to go next](#where-to-go-next)
- [What you get](#what-you-get)
- [Optional integrations](#optional-integrations): VesselTwin and instrument displays
- [Quick start for an internet-connected Pi](#quick-start-for-an-internet-connected-pi)
- [Quick start: one command on the Raspberry Pi](#quick-start-one-command-on-the-raspberry-pi) and [manual guided installation](#advanced-manual-guided-installation)
- [First-run setup](#9-complete-first-run-setup): Home Assistant, Signal K, InfluxDB, Grafana, Boat Chat, Control Panel
- [MQTT access control](#mqtt-access-control), [network access](#reaching-vesselstack-from-other-devices) and [network security](#network-security)
- [Backups](#backups), [Updating](#updating), [Stopping or removing services](#stopping-or-removing-services)
- [Troubleshooting](#troubleshooting)
- [Developer validation](#developer-validation), [Documentation policy](#documentation-policy), [License and security](#license-and-security)

## Where to go next

| I want to… | Read |
|---|---|
| Understand the hardware and buy parts | [Hardware bill of materials](docs/BOM.md) |
| Install on a new or existing Pi | [Quick start](#quick-start-one-command-on-the-raspberry-pi), then [Installation](docs/INSTALLATION.md) |
| Do first boot, SSH and token steps | [Getting started](docs/GETTING_STARTED.md) |
| Use or secure the Control Panel | [Control Panel](docs/CONTROL_PANEL.md) |
| Set up Boat Chat, units and timezone | [Boat Chat setup](docs/BOAT_CHAT.md), providers in [Boat Chat providers](boat-chat/README.md) |
| Reach dashboards from other devices | [Network access and security](docs/NETWORK.md) |
| Manage MQTT accounts and access lists | [MQTT access control](docs/MQTT.md) |
| Update, roll back, or install offline | [Updating and offline install](docs/UPDATING.md), [Deployment runbook](docs/DEPLOYMENT.md) |
| Connect VesselTwin | [VesselTwin integration](docs/VESSELTWIN.md) |
| Add ESP32 instrument displays | [Instrument displays (preview)](docs/DISPLAYS.md) |
| Develop, test, or cut a release | [Development guide](docs/DEVELOPMENT.md) |
| See every document | [Documentation index](docs/README.md) |

## What you get

- A local view of boat data from Signal K-compatible sources and supported NMEA
  2000/SocketCAN hardware. An isolated NMEA 2000 interface is preferred (safest); a
  non-isolated one such as the PiCAN-M works with caveats; see the
  [hardware guide](docs/BOM.md).
- History and dashboards through InfluxDB, Grafana, MQTT, and Home Assistant.
- Boat Chat, a local diagnostic assistant with an optional provider you choose.
- A guided installer, backups, health checks, and an offline release path.

VesselStack is monitoring and diagnostics software. It is not certified
navigation, collision avoidance, engine control, or a sole alarm system.

![VesselStack local data flow](docs/images/vesselstack-map.svg)

### What runs locally

![VesselStack architecture](docs/images/vesselstack-hero.svg)

| Purpose | Local service or view | Typical use |
|---|---|---|
| Boat data | Signal K-compatible history and MQTT | Collect and share readings on the boat LAN |
| History | InfluxDB and Grafana | Explore trends and dashboards |
| Home automation | Home Assistant | Connect boat signals to automations |
| Diagnostics | Boat Chat | Ask questions about the data and system |
| Operations | Control Panel and `vesselstackctl` | Check health, configure, update, and back up |

Boat Chat defaults to a local provider. Other providers are optional and may
need internet access or their own credentials; VesselStack does not promise
that every provider works offline. The optional [instrument displays preview](docs/DISPLAYS.md)
is another separately enabled local feature.

### Component lifecycle

The Control Panel and `vesselstackctl` manage six core components—Home
Assistant, InfluxDB, Grafana, Prometheus, Mosquitto, and Heimdall—as a
persistent catalog. Add or remove components without deleting retained data;
membership survives restarts, upgrades, and reinstalls. Review and apply
changes in the component view at `http://127.0.0.1:8780` through an SSH tunnel,
or use the corresponding `vesselstackctl` commands. Hardware and systemd
services remain configuration-driven. See [Control Panel lab testing](docs/CONTROL_PANEL_TESTING.md)
for acceptance steps.

## Optional integrations

These features are off or inert until you deliberately enable them. Neither is needed for the core stack.

### Optional VesselTwin connection

An optional VesselTwin connection uses the `signalk-vesseltwin` plugin, pairing,
and explicit reading opt-ins. Managed Docker/native SignalK modes preload the pinned
plugin by default (0.5.x lab prerelease); VesselStack does not enable, pair, or authorize readings for you.
See the [VesselTwin integration guide](docs/VESSELTWIN.md)
for setup and [VesselTwin](https://www.vesseltwin.io/) for maintenance documents,
equipment and logbook records.

### Instrument displays (preview)

Optional ESP32 touch-screen displays that show Signal K values (speed, depth, wind,
battery voltage and so on) on pages you define. The first supported board is the
Waveshare ESP32-S3-Touch-LCD-2.8C running ESPHome with LVGL. This is a preview and
the bridge service runs only when `DISPLAYS_ENABLE=true`.

```bash
sudo vesselstackctl display list
sudo vesselstackctl display status
```

Lifecycle commands are `vesselstackctl display list|add|remove|rotate|wifi|status|render|export|build|flash`.
See [Instrument displays (preview)](docs/DISPLAYS.md) for setup, firmware, layouts, the pinned ESPHome version and limitations, and [displays/README.md](displays/README.md) for the code layout.

## Choose your path

| If you are… | Start here |
|---|---|
| Building a new Pi | [Hardware bill of materials](docs/BOM.md), then the [installation guide](docs/INSTALLATION.md) |
| Adding VesselStack to an existing Pi | [Getting started](docs/GETTING_STARTED.md), then [installation](docs/INSTALLATION.md) |
| Releasing to a lab and an offline boat | [Deployment runbook](docs/DEPLOYMENT.md) |
| Developing or validating changes | [Development guide](docs/DEVELOPMENT.md) |

## Quick start for an internet-connected Pi

The normal first install is headless: the Pi needs no screen, keyboard, or
browser. From your laptop, SSH to the Pi with a terminal allocation, then run
the release installer and answer its short questions:

```bash
ssh -t YOUR_USER@raspberrypi.local
curl -fsSL https://github.com/jclima/VesselStack/releases/latest/download/get-vesselstack.sh | sudo bash
```

The installer checks the host before changing it. It may install Debian
prerequisites and, after asking, Docker Engine and Compose v2 from Docker's
official apt repository. It then downloads and checksum-verifies the release,
asks for boat and service settings, and offers a final review before starting
VesselStack. `sudo` is required because installation writes system packages,
services, and `/opt/vesselstack`; the questions explain each change. Secrets
are generated or entered without echo and are not printed in the review.

The command above installs the latest stable release. For the `0.5.7` lab
prerelease, follow the pinned-release instructions in the
[installation guide](docs/INSTALLATION.md).
For a boat with no internet, prepare the release and offline bundle in the lab
and follow the [offline deployment path](docs/DEPLOYMENT.md).

## Before and after installation

Before installation, decide where the Pi will live, how it will be powered,
and whether its data disk and backups are separate from the boot media. If you
connect to NMEA 2000, confirm the bus is correctly powered and terminated and
that you have chosen the interface and its power source deliberately
(an isolated interface is preferred (safest); a non-isolated one such as the
PiCAN-M works with caveats, see docs/BOM.md). Commission on the bench first.

After installation, use the printed URLs from a trusted local device, run
`sudo vesselstackctl doctor`, and confirm the dashboards show the signals you expect.
Keep the generated credentials per host, schedule backups to a separate device,
and test restoring one before relying on the system. Read the deployment guide
before carrying an update to a boat without internet access.

### Update from the Control Panel (0.5.7 lab release)

An authenticated Control Panel session can check releases from the public
`jclima/VesselStack` repository. Stable releases are shown by default; enable
**Include lab prereleases** to include lab releases in the list. The panel shows
the installed/running version, release channel,
release notes, and a link to the release; it does not display a checksum value.
It accepts no arbitrary URL or filesystem path and does
not let the browser override the backup destination.

Before an update, mount the configured external backup device under `/media` or
`/mnt` and confirm the displayed path is the one you intend. The panel's **Back
up & install** flow
asks for confirmation, verifies the downloaded SHA-256 manifest, safely extracts
the release, runs preflight, then makes and verifies the backup. A failed or
unmounted backup stops the update; the backup path and bounded operation log
remain available for recovery. There is no automatic rollback promise: use the
explicit CLI `vesselstackctl verify-backup` and `vesselstackctl restore` path in
the deployment guide when recovery is needed. `--require-mounted` defeats
`ALLOW_UNMOUNTED` for this flow.

The detached updater restarts the Control Panel and verifies its new version and
boot before the browser reconnects. Settings also provides a standalone
**Restart Control Panel** action. The panel uses ordinary HTTP; release
checksums provide corruption detection, not signed provenance, and the 0.5.7
lab tag is explicitly unsigned and not latest. See the full reference below for
backup, recovery, and update details.

The browser updater (0.5.x lab prerelease) was introduced in `0.5.5`. Installations on `0.5.4` or
earlier must use the CLI installer/update once; installations on `0.5.5` or
`0.5.6` can upgrade to `0.5.7` through the Control Panel with lab prereleases
enabled.
The panel serializes its own update/configuration/component operations, but
unrelated CLI commands are separate maintenance actions and are not globally
serialized by the panel; do not run them concurrently with an update.

The strict backup flag requires a mounted external destination under `/media`
or `/mnt`; browser input cannot override that destination. The transient
updater job, status, and staging files under `/var/lib/vesselstack` are
intentionally excluded from backups; user configuration and data are backed up.
If an update fails, the verified pre-update backup is kept for explicit
`vesselstackctl verify-backup`/`restore` recovery. The browser does not promise
automatic restoration.

## Find your next answer

The [documentation index](docs/README.md) maps common tasks to the right guide,
including first boot, access, offline operation, troubleshooting, privacy, and
contribution. This README keeps the install steps, backups, removal and
troubleshooting; deeper reference lives in `docs/`. For Boat Chat provider
configuration see [Boat Chat providers](boat-chat/README.md); for first-run access, settings,
timezone and units see [Boat Chat setup](docs/BOAT_CHAT.md); for power sources and
instrument-reading coverage see [Power monitoring](boat-chat/POWER_MONITORING.md).

## Quick start: one command on the Raspberry Pi

**Docker is installed for you.** If Docker Engine with the Compose v2 plugin is missing,
the script asks once and then follows Docker's official apt-repository instructions
(<https://docs.docker.com/engine/install/debian/>; the Ubuntu page for Ubuntu):

- it checks the system first: 64-bit Raspberry Pi OS, Debian 12 (bookworm) or 13
  (trixie), or Ubuntu; **32-bit Raspberry Pi OS (armhf) is refused** (see below);
- it downloads Docker's signing key over HTTPS, checks that the file holds exactly
  one key and that its fingerprint is `9DC8 5822 9FC7 DD38 854A  E2D8 8D81 803C 0EBF CD88`,
  and stops without changing anything if it does not match;
- it installs the key as `/etc/apt/keyrings/docker.asc`, writes
  `/etc/apt/sources.list.d/docker.sources` (Docker's deb822 entry for your release and
  CPU), runs `apt-get update` and installs `docker-ce docker-ce-cli containerd.io
  docker-buildx-plugin docker-compose-plugin`; it enables and starts the `docker`
  service and verifies `docker --version`, `docker compose version` (v2 or newer) and
  `docker info` (nothing is pulled);
- if Docker is already installed but the Compose plugin is missing, only
  `docker-compose-plugin` is added from the same repository;
- if Docker and Compose v2 are already present, it does nothing to Docker;
- unofficial packages that conflict with Docker's own (`docker.io`, `docker-cli`,
  `docker-compose`, `docker-compose-v2`, `docker-doc`, `docker-buildx`,
  `podman-docker`, `containerd`, `runc`) are **never removed silently**: the script
  lists the ones that are installed (plus any dependent packages apt would remove with
  them, taken from `apt-get -s remove`) and asks; `--yes` accepts. They are removed with
  `apt-get remove` (not purge), so `/var/lib/docker` is kept. If a Docker daemon
  already holds containers or volumes, `--yes` is not enough: the script stops, and you
  must confirm interactively (back up first) or remove the packages yourself;
- an older `docker.list` (the manual steps of v0.4.0) or any other apt source that
  already points at `download.docker.com` is renamed to `*.vesselstack-saved` after
  asking, because two entries with different keys make `apt-get update` fail;
- no `docker` group change is made: the installer runs as root and VesselStack's
  service account does not need Docker access;
- `--offline` cannot install Docker; install it first while online.

If a step fails, the script prints the failing command and what it left behind;
re-running is safe. **32-bit Raspberry Pi OS:** Docker Engine v28 was the last release
for it, so VesselStack needs 64-bit Raspberry Pi OS. Re-flash the card or SSD with
"Raspberry Pi OS (64-bit)" in Raspberry Pi Imager (Pi 3, 4, 5 and Zero 2 W support it);
`dpkg --print-architecture` must say `arm64` (`uname -m` can say `aarch64` on a 32-bit
system running a 64-bit kernel).

To undo the Docker part (your containers and data stay unless you delete them):

```bash
sudo apt-get remove docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo rm -f /etc/apt/sources.list.d/docker.sources /etc/apt/keyrings/docker.asc
sudo apt-get update
# optional, deletes ALL images, containers and volumes: sudo rm -rf /var/lib/docker /var/lib/containerd
```

**Headless is the normal case: the Pi needs no screen, keyboard or browser.** On a
Raspberry Pi running 64-bit Raspberry Pi OS (or Debian) with internet access, SSH into
it from your laptop (use `ssh -t` so the installer can ask its questions on that
session), paste this one line and answer the questions in the same terminal:

```bash
ssh -t YOUR_USER@raspberrypi.local
curl -fsSL https://github.com/jclima/VesselStack/releases/latest/download/get-vesselstack.sh | sudo bash
```

(Or paste the second line into a terminal on the Pi itself. No address is opened and no
tunnel is needed.) The questions are grouped in short sections, each with a plain-language
explanation and a sensible default in `[brackets]`: **Enter** accepts the default, **?**
explains the question, **b** goes back one question, and a final review screen lists every
choice (never any secret) with *save*, *edit a section* and *quit*. Example, abridged:

```text
== Step 5 of 6: Configure ==
  Starting the setup questions. Press Enter to accept the answer in [brackets]; ? explains a question.

VesselStack setup. Press Enter to accept the answer in [brackets]. ? explains a question, b goes back one question.
Everything stays on this computer; no secrets are shown or printed.
This computer: aarch64, 7.6 GiB RAM, Docker ready; CAN: no can0 yet; network: eth0, wlan0.

[1/9] Your boat
  Names shown in dashboards and Boat Chat. Everything stays on this Raspberry Pi.
  Boat name [My Boat]: Sea Wolf
  Boat type [Motor vessel]: Sailing yacht
    1) metric             metres, km/h, degrees C
    2) us_customary       feet, mph, degrees F
    3) nautical           knots, nautical miles, metres/fathoms
  Units - choose 1-3 [metric]: 3
    Detected from this computer: Europe/Oslo
  Timezone [Europe/Oslo]:
  Add optional vessel details (MMSI, call sign, length, beam)? [y/N]:

[2/9] Signal K (the boat data hub)
  How should Signal K run? - choose 1-3 [docker]:

[3/9] NMEA 2000 (CAN bus)
    No can0 interface found yet (normal before the CAN HAT overlay is installed). You can enable it later.
  Enable SocketCAN / NMEA 2000? [y/N]: ?
    An isolated CAN interface is preferred (safest); a non-isolated one such as the PiCAN-M works with caveats (see docs/BOM.md). Needs a powered and terminated backbone and the can0 device present. ...
  Enable SocketCAN / NMEA 2000? [y/N]:
...
Advanced settings (service account, folders, ports, container and bucket names, passwords)? [y/N]:

Review (secrets are never shown):
  1. Your boat
      Boat name                                            Sea Wolf
      Units                                                nautical
  ...
  [s] save and continue   [e] edit a section   [q] quit without saving - choose [s]:

  Install and start VesselStack now? [Y/n]:
...
  Addresses (for information; use them from any computer on the boat network when you like):
    Grafana (history dashboards)   http://192.0.2.10:43000
    Home Assistant                 http://192.0.2.10:8123
    Boat Chat                      http://192.0.2.10:8765
  Show the Boat Chat access token (it is printed only when you run this):
    sudo grep '^BOAT_CHAT_ACCESS_TOKEN=' /opt/vesselstack/config/boat-chat.env
  Control panel (administration; it listens on this Pi only, port 8780). From your own computer:
    ssh -L 8780:127.0.0.1:8780 YOUR_USER@192.0.2.10
```

Everything printed at the end is information only: nothing in the install needs a
browser, and nothing is opened for you.

The `releases/latest/download/` form works only once a release carrying
`get-vesselstack.sh` has been published **and marked latest**. Until then (and while a
release is still being install-tested) use the pinned form shown next, which works for
any published release, including a pre-release.

Releases are built by the tag-triggered GitHub workflow (read-only build job, separate
write-only publish job, draft first, reproducible tarball on Linux); maintainers follow the
release checklist in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

To install an exact release instead (recommended once you have tested one):

```bash
curl -fsSL https://github.com/jclima/VesselStack/releases/download/v0.5.7/get-vesselstack.sh | sudo bash
# or: curl -fsSL .../get-vesselstack.sh | sudo VESSELSTACK_VERSION=0.5.7 bash
# or: curl -fsSL .../get-vesselstack.sh | sudo bash -s -- --version 0.5.7
```

**No prompts at all** (scripts, a Pi without a terminal, repeatable installs):

```bash
# defaults, plus the settings you name (repeat --set; names as in vesselstack.example.env)
curl -fsSL https://github.com/jclima/VesselStack/releases/latest/download/get-vesselstack.sh \
  | sudo bash -s -- --yes --set BOAT_NAME=Foo --set SOCKETCAN_ENABLE=true
# every question's own default answer, nothing asked
curl -fsSL https://github.com/jclima/VesselStack/releases/latest/download/get-vesselstack.sh | sudo bash -s -- --defaults
# a ready-made vesselstack.env (optionally with --set overrides)
sudo bash get-vesselstack.sh --yes --config ./vesselstack.env --set BOAT_NAME=Foo
```

`--yes` answers yes to every question (Docker installation, final confirmation, upgrade of
an installed system) and takes the settings from `--config`, then `--set`, else the
defaults. `--defaults` gives every question its own default instead: it installs Docker
and VesselStack on a fresh Pi, but leaves an installed system alone (the upgrade question
defaults to no) and never removes conflicting Docker packages. With neither, and no
terminal to ask on (for example `curl | sudo bash` over `ssh` without `-t`), the installer
stops before changing anything and lists these options.

### Headless Raspberry Pi: open the panel from your laptop

Use Raspberry Pi Imager to prepare **Raspberry Pi OS Lite (64-bit)**. In its
customization screen, set a hostname, username, password and Wi-Fi details, and
turn on SSH. Boot the Pi, then connect once from your laptop with
`ssh -t YOUR_USER@YOUR_PI.local` (use the Pi's router-assigned IP if the name does
not resolve). You do not need a screen, keyboard or SSH tunnel on the Pi.

Run the guided installer above in that terminal. In **Who can reach what**, choose
**Trusted network — token required** for the Control Panel. This choice is separate
from the dashboard preset: choose **local** for the dashboards if you want their
managed listeners kept on the Pi. The installer prints the panel's real address,
such as `http://YOUR_PI.local:8780`, and the command to retrieve its unlock token.
Open that address in your laptop's browser and paste the token into **Unlock**.

For an existing installation with this command available:

```bash
sudo vesselstackctl panel-access lan
sudo grep '^CONTROL_PANEL_TOKEN=' /opt/vesselstack/config/control-panel.env
```

The command restarts only the Control Panel. It preserves its token and port and
does not change any other service's listener or firewall rules. Use the address it
prints; `0.0.0.0` is a listen setting, not a browser address. Check the current
setting with `sudo vesselstackctl panel-access`. To undo network access:

```bash
sudo vesselstackctl panel-access local
```

For unattended **fresh installs**, `--panel-lan` is shorthand for
`--set CONTROL_PANEL_HOST=0.0.0.0`. Combine it with `--set LAN_ACCESS=local` to keep
the preset-managed dashboards and databases local. `LAN_ACCESS=local` does not
restrict Signal K's host-networked listener; see the network limitations below.
The panel option alone leaves every other network setting unchanged. For upgrades,
use `panel-access lan` after updating instead of passing configuration overrides
to the installer.

Network access binds the panel to **all IPv4 interfaces**. Use it only on a trusted
lab or boat network: its token is still required, but ordinary HTTP does not encrypt
administration traffic. Do not forward this port from the internet. An enabled
strict firewall may need an explicit rule for the panel port on the relevant
interface; setup never silently opens firewall ports. Local-only remains the default.

The guided installer explains each step in plain language and asks before it
does anything beyond installing ordinary Debian packages:

1. checks the computer (Linux, root, Debian family, 64-bit ARM; warns, does not
   fail, on other systems), disk space, memory and internet access, and detects an
   existing installation (an existing install is **never overwritten silently**: it
   is offered the built-in `vesselstackctl update` upgrade, with its backup);
2. installs the packages VesselStack needs with `apt-get` (`ca-certificates curl jq
   openssl python3 python3-venv rsync tar gzip`; no `git`) and, if Docker Engine or
   the Compose v2 plugin is missing, **asks** before adding Docker's official apt
   repository (key fingerprint checked) and installing `docker-ce docker-ce-cli
   containerd.io docker-buildx-plugin docker-compose-plugin` (details and undo steps in
   the Docker box above; conflicting unofficial packages are only removed after you
   confirm, 32-bit Raspberry Pi OS is refused);
3. downloads `vesselstack-<version>.tar.gz` and its `.sha256` from the GitHub
   release, verifies the checksum, refuses unsafe archives (absolute or `..` paths,
   links, devices, setuid bits) and unpacks to `/opt/vesselstack-release/<version>`,
   owned by root and not writable by others;
4. asks the configuration questions **in the terminal** (the terminal wizard,
   `vesselstack-wizard.py --cli`, the default). It reads your answers from the terminal
   (`/dev/tty`, so the piped `curl | sudo bash` form works), validates every answer
   immediately with the same rules as the browser wizard (`DEFAULTS`, `validate()`,
   `render()`; stricter checks only where `install.sh` enforces the same limits, such as
   the boat name characters), and offers sections for the boat (name, type, units, timezone
   detected from the system, optional and private MMSI, call sign, length and beam),
   Signal K (docker, existing or native; docker recommended), NMEA 2000 / CAN
   (`SOCKETCAN_ENABLE`, interface, bit rate and the home-lab virtual-CAN option
   `VESSELSTACK_CAN_CREATE_VCAN`), AIS, Home Assistant (URL, optional hidden token), Boat
   Chat provider (default `local`) and Telegram, the firewall (untrusted interface, enable,
   strict mode, allowed ports), raw-data retention (`INFLUXDB_RAW_RETENTION`; empty means
   30 days on a new install and no change to an existing database) and who can reach the
   dashboards (`local`, `dashboards` or `all`; see "Reaching VesselStack from other devices").
   Ports, container and bucket names, folders, the service account and the generated
   secrets sit behind "Advanced settings? [y/N]". Passwords and tokens default to being
   generated during installation, are read without echo when you do type one and are
   never printed or logged. It detects the architecture, Docker, `can0` and the network
   interface names to inform defaults and hints; it never turns hardware on by itself.
   The browser wizard asks the same "Who should be able to reach the dashboards?" question
   (`local`, `dashboards` default, `all`) on its network step. The page sends only the
   preset name (`LAN_ACCESS`); the server validates it (an unknown name is refused with HTTP 400,
   nothing is written) and expands it with the terminal wizard's `LAN_PRESETS` mapping
   into `VESSELSTACK_WEB_BIND_ADDRESS`, `VESSELSTACK_DATA_BIND_ADDRESS`, `BOAT_CHAT_HOST` and `HOMEASSISTANT_BIND_ADDRESS`;
   the preset wins over explicit bind values, `LAN_ACCESS` is never written to the env file
   and the independent Control Panel access choice is preserved. Choosing `all` shows the warning that Prometheus
   has no login and recommends the firewall. The loopback, Host/Origin, URL-fragment token
   and content-type protections are unchanged.
   The terminal wizard has no network surface (no web server, no token, no URL), so it
   runs as root directly, in a private root-only `0700` directory; its output is then
   treated exactly like the browser wizard's: copied once into the root-only staging
   directory, linted, re-validated as root with `vesselstack-wizard.py --validate FILE`
   (known settings only, allowed characters, enum and boolean values) and installed
   root-only, mode 600. `--config FILE` gets the same treatment (opened without following
   symlinks, a regular file of at most 64 KiB, copied once, only the staged copy linted,
   validated and used); with `--set` the staged copy is the base the values are applied
   to. Optional browser wizard: add `--web-wizard`; it is unchanged (loopback only on a
   random free port, `--wizard-port N`, run as an unprivileged user or the temporary
   `vesselstack-wizard` system account, a `#token=` address printed once, open on the Pi
   or through the printed `ssh -L` tunnel) and cannot be combined with `--yes`-style
   configuration options (`--config`, `--set`, `--defaults`). Limitation: the Docker line
   in a wizard's preflight reflects what that wizard sees; `install.sh` performs the
   authoritative checks;
5. shows a summary of the chosen configuration (no secrets), asks for confirmation,
   runs `install.sh --dry-run`, then `install.sh --start`, then `vesselstackctl
   doctor`, and prints `vesselstackctl urls` plus, as plain text, the service
   addresses to use from another computer (host name or IP), the command that shows
   the Boat Chat access token and either the panel network address or its local-only access hint
   (information only), where the generated tokens are stored and the next steps.
   It ends with a list of everything it changed.

Options (`sudo bash get-vesselstack.sh --help`): `--yes` (ask nothing, answer yes),
`--defaults` (ask nothing, each question's default), `--panel-lan` (share only the
panel listener on a fresh install), `--set KEY=VALUE` (repeatable;
avoid secrets here, they would show in the process list: leave them to be generated),
`--config FILE` (use an existing `vesselstack.env`; `--no-wizard --config FILE` is the
same), `--cli` / `--terminal` (the default terminal questions), `--web-wizard` (optional
browser wizard), `--version X.Y.Z`,
`--offline --bundle DIR [--tarball FILE]` (no internet, see
[Offline install](#offline-install-no-internet-on-the-boat)), `--sha256 HASH`
(pin the tarball to a checksum you obtained elsewhere), `--dir DIR`,
`--wizard-port N` (browser wizard only). `--set` values are checked early for shape and
fully (key, characters, enum, ranges) by the wizard before `install.sh` runs; an unknown
key or invalid value stops the installer without installing anything (Debian packages
and Docker may already have been installed by the earlier steps; re-running is safe).
`--set` cannot change the configuration of an installed system (an upgrade keeps it).
With a saved configuration from an earlier run, `--yes` and `--defaults` reuse it and
`--set` is applied on top of it (the old file is kept as `vesselstack.env.previous`). Re-running is safe: finished steps are reported
and skipped, an already installed version just reports "nothing to do". Rollback of this
installer change: use the previous release's `get-vesselstack.sh`; nothing else changed.

**What `curl | sudo bash` trusts.** It runs a script downloaded from GitHub as
root. You are trusting GitHub, TLS (the script enforces HTTPS with TLS 1.2+ for the
downloads) and the VesselStack release you fetch it from. The tarball's `.sha256`
is published in the *same* release, so it protects against corruption and partial
downloads but not against a compromised release or account: compare the printed
sha256 with the one on the GitHub release page, or pass `--sha256`. The whole
script is wrapped in functions and ends with a single `main "$@"` call, so a
truncated download cannot half-run. If you prefer to read it first (recommended):

```bash
curl -fsSLO https://github.com/jclima/VesselStack/releases/latest/download/get-vesselstack.sh
less get-vesselstack.sh
sudo bash get-vesselstack.sh
```

The script is `scripts/get-vesselstack.sh` in this repository; each release ships
a copy with that release's version baked in as the default, plus
`get-vesselstack.sh.sha256`. Rollback: nothing outside the listed changes is
touched; `sudo vesselstackctl stop` stops the services and the README's
[uninstall](#stopping-or-removing-services) and restore sections cover the rest.
Releases are unpacked next to each other and never deleted; remove
`/opt/vesselstack-release/<version>` yourself when you no longer need it.

## Advanced: manual guided installation

Prefer to run each step yourself? After installing the OS and Docker prerequisites
from the end-to-end guide:

```bash
git clone https://github.com/jclima/VesselStack.git
cd VesselStack
git checkout v0.5.7
python3 vesselstack-wizard.py --cli     # terminal questions (no browser)
# python3 vesselstack-wizard.py        # or the browser wizard
```

The terminal wizard (`--cli`) asks in the terminal and writes `vesselstack.env`; add
`--defaults` and `--set KEY=VALUE` to configure without questions, `--base FILE` to start from an
existing file. For the browser wizard open the address it prints (`http://127.0.0.1:8088/#token=...`), or use the
SSH tunnel documented in the guide and open the same address on your workstation.
The wizard accepts `--port N`, `--output FILE` (where to write the env file) and
`--exit-after-save` (stop after the first successful save; the one-line installer
uses it) and `--validate FILE` (check an existing env file with the wizard's rules,
exit 0 if valid, 1 otherwise, no server started; `VESSELSTACK_CAN_CREATE_VCAN` is now a
known wizard setting, default `false`). Rollback: revert this change; nothing is
persisted except the temporary account, which is removed on exit (`sudo userdel
vesselstack-wizard` if a run was killed with SIGKILL). It prints a per-run address of the form `http://127.0.0.1:PORT/#token=...`; open
that exact address. The token is carried in the URL fragment (never sent to the
server, never present in any served file); `wizard/app.js` reads it, removes it
from the address bar with `history.replaceState` and sends it as `X-Wizard-Token`
with every API call (`/api/system` and `/api/save` refuse requests without it).
The wizard needs no root privileges; run it as a normal user. Requests are refused unless the `Host`
header is `127.0.0.1`/`localhost`/`[::1]` on the bound port, any `Origin` is the
same loopback origin, the token matches, and saves use `Content-Type:
application/json` (this blocks cross-site form posts and DNS rebinding). The env
file is created mode 600 from the start and never through a symlink. Open the
printed URL itself (including the `#token=` part); a bookmarked older address stops working when the wizard restarts. The dependency-free wizard binds only to loopback, shows relevant host
prerequisites, and writes a complete private `vesselstack.env`. It does not
change boot firmware, firewall rules, marine wiring, or start services. Then run
`sudo ./install.sh --config vesselstack.env --dry-run` and `--start` as described
in the sections below.

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

Install Docker Engine and Compose v2 from Docker's official apt repository
(<https://docs.docker.com/engine/install/debian/> for 64-bit Raspberry Pi OS and
Debian), or let the one-line installer do it (see "Quick start" above).

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

For the pinned 0.5.7 lab prerelease, use its annotated unsigned tag (it is not
the latest release):

```bash
git fetch --tags
git checkout v0.5.7
```

This lab prerelease is for authorized lab use only; it is unsigned, not latest,
and physical Raspberry Pi testing is pending. It is not a stable release.

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
| `HOMEASSISTANT_BIND_ADDRESS` | IPv4 address Home Assistant (host-networked, `8123`) listens on; default `0.0.0.0`. Any other value makes the installer manage one delimited `http:` block in `${VESSELSTACK_DATA}/homeassistant/configuration.yaml` (see "Reaching VesselStack from other devices"). SignalK has no equivalent setting |
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
| `SIGNALK_VESSELTWIN_PRELOAD` | Managed Docker/native modes preload immutable `signalk-vesseltwin` 0.2.3 (default `true`); existing mode ignores it, and `false` skips without uninstalling |
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
| `PROMETHEUS_RETENTION` | Prometheus metrics retention: `720h`, `30d` or `8w`; default `30d`; see "Prometheus data and retention" |
| `INFLUXDB_RAW_RETENTION` | Raw-bucket retention: `720h`, `30d`, `8w`, or `0` (forever). Empty (default) = `30d` on a new install and **no change** to an existing bucket; see "Raw data retention" |
| `*_PASSWORD`, `*_TOKEN` | Keep `GENERATE` for generated values; every generated secret is preserved from the installed config on re-runs |
| `BOAT_CHAT_PROVIDER` | `local` works without an external LLM |
| `TELEGRAM_ENABLE` | Run the optional Telegram worker; requires its token and allowed chat IDs in Boat Chat Settings |
| `TELEMETRY_INDEXER_ENABLE` | Enable five-minute current/power sampling and thirty-minute history refreshes |
| `NODE_EXPORTER_ENABLE` | Optional private host metrics exporter; disabled by default; available in the wizard's advanced settings |
| `CONTROL_PANEL_HOST`, `CONTROL_PANEL_PORT` | Administration listener; defaults to loopback on `8780` |
| `MQTT_USERNAME`, `MQTT_PASSWORD` | Mosquitto administrator account; the username may contain letters, numbers, `.`, `_` and `-` (1-64), and a custom password must not contain `:` or leading/trailing whitespace |
| `DISPLAYS_ENABLE` | `false` by default; `true` adds the displays bridge and per-display MQTT accounts (see [MQTT access control](#mqtt-access-control)) |
| `DISPLAYS_BRIDGE_MQTT_USERNAME` | Bridge account name, 1-64 letters, numbers, dot, underscore or hyphen; default `vesselstack-bridge`; must differ from `MQTT_USERNAME` (the Control Panel rejects a collision when displays are enabled, and the installer preflight rejects it) |
| `DISPLAYS_SIGNALK_TOKEN` | Optional SignalK access token for the displays bridge, sent as `Authorization: Bearer`; leave empty unless SignalK security requires it; must not contain line breaks or any other control character (the Control Panel and the installer both refuse them) |
| `DISPLAYS_HA_TOKEN` | Optional long-lived access token of a dedicated Home Assistant user (a leaked token can operate every device in that Home Assistant; see [Home Assistant items](docs/DISPLAYS.md#home-assistant-items)), used only by the displays bridge for Home Assistant items (`ha` in `displays.json`, see [Home Assistant items](docs/DISPLAYS.md#home-assistant-items)); the bridge connects to `HOME_ASSISTANT_URL` (default `http://127.0.0.1:8123`). Stored only in the root-only 0600 `config/displays-bridge.env`, never logged; same rule as the SignalK token: no line breaks or other control characters. Different from `HOME_ASSISTANT_TOKEN` (Boat Chat) |
| Terminal wizard and `--set` | The `DISPLAYS_*` settings are asked under "Advanced settings" (the bridge user name and the two tokens only when displays are enabled; the tokens are hidden and optional) and can be set headless, for example `--set DISPLAYS_ENABLE=true`; they are kept when you re-run the wizard |

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
`doctor` adds dependency, Compose, free-space, service, memory-index and endpoint
checks and finishes with either `RESULT healthy` or `RESULT attention required`.
`urls` shows who can really reach each service (see "Reaching VesselStack from
other devices"). If Boat Chat stays "not ready", `sudo vesselstackctl reindex`
rebuilds its memory index (see Troubleshooting). Use
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
image and persists its settings in `/opt/vesselstack-data/signalk` (owned by
uid/gid 1000, the image's `node` user, so the container can write it). With
`SIGNALK_MODE=native`, it installs the pinned npm package under
`/opt/vesselstack/signalk-server` and runs it as the locked VesselStack user.
`SIGNALK_MODE=existing` never modifies SignalK. The managed modes use the same
first-run admin onboarding at `http://HOST:3000`.

Managed Docker and native installs preload the pinned, zero-runtime-dependency
`signalk-vesseltwin` 0.2.3 package before SignalK starts. The archive is
integrity-checked before publication; an existing plugin directory is preserved
and never overwritten, so a boat that already has the plugin updates it from the
Signal K App Store. When a re-run newly preloads the plugin into an
already-running Docker Signal K container, the installer restarts that container
so the plugin loads. The preload also lists the plugin in
`signalk/package.json` `dependencies` (created if missing, other keys kept) so a
later Signal K App Store `npm install --save` does not prune it. Preloading is
separate from plugin enablement, pairing, and reading switches, which remain
explicit owner actions (enable the plugin in **Server > Plugin Config** after
either a preload or an App Store install).

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

#### Prometheus data and retention

Prometheus stores its metrics in `<data>/prometheus` (bind mount at `/prometheus`;
the image runs as uid/gid 65534, so the installer creates it `65534:65534`, mode
0750, before `compose up`). It survives container re-creation and is included in
`vesselstackctl backup` and `restore` with the rest of the data directory.
`PROMETHEUS_RETENTION` (default `30d`; `720h`, `30d` or `8w`; `0` is not accepted)
sets `--storage.tsdb.retention.time`, so metrics older than the window are deleted
automatically. `vesselstackctl doctor` fails when the directory is not writable by
uid 65534 (fix: rerun the installer, or `sudo chown -R 65534:65534 <data>/prometheus`).
Upgrade note: releases before 0.4.4 kept metrics in an anonymous Docker volume. That
volume is left untouched; after updating, Prometheus starts with an empty store and the
old history stays in the orphaned volume until you remove it with `docker volume prune`
(nothing is copied or deleted automatically). Rollback: reinstall 0.4.3 (it uses the old
volume again; new metrics stay in `<data>/prometheus`).

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

Open `http://HOST:8765` and enter the access token (`BOAT_CHAT_ACCESS_TOKEN` in
`config/boat-chat.env`); the default `local` provider sends nothing to an external model.
Settings, the settings token, telemetry refresh, optional host metrics, timezone and units
are in [Boat Chat setup](docs/BOAT_CHAT.md); providers are in [Boat Chat providers](boat-chat/README.md).

### Control Panel

The Control Panel is the browser administration surface. It listens on loopback by default
(`http://127.0.0.1:8780` through an SSH tunnel); `sudo vesselstackctl panel-access lan|local`
changes only that bind. See [Control Panel](docs/CONTROL_PANEL.md) for the full guide.

### Heimdall

Open `http://HOST` and add trusted-network links to Home Assistant, Grafana,
SignalK, Boat Chat, InfluxDB, and Prometheus.

## MQTT access control

Mosquitto always enforces an access list and hashed passwords, rendered by `install.sh` and
`vesselstackctl start`; plaintext secrets are never placed on a command line. File locations,
display accounts and rotation are in [MQTT access control](docs/MQTT.md).

## Reaching VesselStack from other devices

The installer asks who should reach the dashboards (`local`, `dashboards` (default) or `all`);
change it later with `sudo vesselstackctl lan`. The Control Panel is independent and stays on
loopback unless you run `sudo vesselstackctl panel-access lan`. The full preset table and the
limits for host-networked containers are in [Network access and security](docs/NETWORK.md).

## Network security

InfluxDB, Prometheus and MQTT bind to `127.0.0.1` by default; Grafana and Heimdall are LAN-reachable.
Keep Home Assistant, Signal K and Boat Chat off the public internet. The minimum checklist is in
[Network access and security](docs/NETWORK.md#network-security).

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

Back up first, then update through the guided path:

```bash
sudo vesselstackctl backup
sudo vesselstackctl update
```

To update from a reviewed release tag with `install.sh --dry-run` first, and to roll back from the
verified pre-update backup, see [Updating and offline install](docs/UPDATING.md).

### Offline install (no internet on the boat)

Prepare a bundle on a connected machine, carry it to the boat, and install with
`sudo bash get-vesselstack.sh --offline --bundle /path/to/offline-bundle --tarball /path/to/vesselstack-0.5.7.tar.gz`.
The bundle must be root-owned and not group/world-writable. Steps: [Updating and offline install](docs/UPDATING.md#offline-install-no-internet-on-the-boat).

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

This also removes the installed `vesselstack-update` launcher and its root-owned
`deploy-chat.py` helper.

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

### Boat Chat reports "not ready" (memory index empty)

Symptom: `curl -fsS http://127.0.0.1:8765/health | jq .readiness` shows
`layers.memory = {"ready": false, "chunks": 0}`, `vesselstackctl status` says
`Boat Chat unavailable or not ready` and `vesselstackctl doctor` prints `FAIL Boat
Chat memory index is empty`. Releases before 0.4.3 never built the index on a
fresh install. 0.4.3 and later build it during installation, on upgrade and on
`vesselstackctl start`. To fix an install that predates that, or one whose build
failed (the installer printed `WARN: ... memory index could not be built`):

```bash
sudo vesselstackctl reindex      # builds it as the service user; prints "memory index: ... (N chunks)"
sudo vesselstackctl doctor
```

If `reindex` itself fails, its message names the cause (missing Python
environment: rerun the installer; state directory missing or a file owned by
root: the `chown` command it prints).

### SignalK not running / EACCES on package.json

Symptom (`SIGNALK_MODE=docker`): the `vesselstack-signalk` container restarts or
exits and `sudo docker logs vesselstack-signalk` shows
`EACCES: permission denied, open '/home/node/.signalk/package.json'`. The
`signalk-server` image runs SignalK as its `node` user (uid/gid 1000, config
directory `/home/node/.signalk`), but `<data>/signalk` was created by Docker as
`root:root`. No release before 0.4.3 created it; 0.4.3 and later create it
as `1000:1000` mode 0750 before `compose up` and repair a root-owned one on
every installer run, so rerunning the installer (`sudo vesselstackctl update` or
the installer from the release) fixes it. `sudo vesselstackctl doctor` also
detects the condition and reports it. Start with the read-only doctor output
and inspect the exact configured SignalK data directory and ownership. The
installer repairs only the known state conservatively: an empty or root-owned
directory with safe contents may be repaired, while symlinks, hard links,
special files, or another owner are left untouched with instructions. Resolve
the indicated cause, then rerun the supported installer repair. Do not
blanket-chown the SignalK tree or restart the container while ownership and
symlink state are still unknown.

### Container mount permissions fail

Inspect the affected container log and mount. Do not recursively change all
ownership: containers use different internal user IDs.

## Developer validation

Run `python3 -m unittest discover -s tests -p 'test_*.py'` and the shell suites under `tests/`
(protected temporary roots, CI jobs, and the ESPHome config test) as described in
[Development: Developer validation](docs/DEVELOPMENT.md#developer-validation).

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

## Development and release history

Historical release scope and the unreleased review-fix notes are kept in
[Development: Historical notes](docs/DEVELOPMENT.md#historical-notes). Release notes are in `CHANGELOG.md`.
