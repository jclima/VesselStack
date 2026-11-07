# VesselStack

![VesselStack banner](brand/github-banner.png)

VesselStack is a private, self-hosted companion for your boat. It keeps useful
boat data, dashboards, automation, and diagnostics together on a Raspberry Pi
you control—at home, in the lab, or aboard. It is designed to keep working on
the boat's local network when the internet is unavailable.

## Contents

- [What it looks like](#what-it-looks-like), [what you need](#what-you-need) and [where to go next](#where-to-go-next)
- [What you get](#what-you-get) and [optional integrations](#optional-integrations): VesselTwin and instrument displays
- [Quick start: one command on the Raspberry Pi](#quick-start-one-command-on-the-raspberry-pi), [before and after installation](#before-and-after-installation) and [manual guided installation](#advanced-manual-guided-installation)
- [Upgrading to 0.6.0](#upgrading-to-060)
- [First-run setup](#9-complete-first-run-setup): Home Assistant, Signal K, InfluxDB, Grafana, Boat Chat, Control Panel
- [MQTT access control](#mqtt-access-control), [network access](#reaching-vesselstack-from-other-devices) and [network security](#network-security)
- [Backups](#backups), [Updating](#updating), [Stopping or removing services](#stopping-or-removing-services)
- [Troubleshooting](#troubleshooting)
- [Developer validation](#developer-validation), [Documentation policy](#documentation-policy), [License and security](#license-and-security), [Moved sections](#moved-sections)

## What it looks like

![Control Panel Home: services, backup and software status, one Back up now button and the component cards](docs/images/control-panel-home.png)

*The Control Panel Home tab shows whether everything is running, the last backup and
whether an update is known. [Control Panel guide](docs/CONTROL_PANEL.md).*

![Boat Chat Overview: boat condition, observations, what needs attention and suggested actions](docs/images/boat-chat-overview.png)

*Boat Chat answers questions from your boat's own recorded data. Its Overview shows
condition and what needs attention. [Boat Chat guide](docs/BOAT_CHAT.md).*

![Boat Chat on a phone in dark mode](docs/images/boat-chat-phone-dark.png)

*Both web apps work on a phone and follow the device's light or dark setting.*

## What you need

The reference build, from [docs/BOM.md](docs/BOM.md):

- Raspberry Pi 5 (8 GB; 4 GB is the minimum) with the Raspberry Pi Active Cooler.
- A 256 GB or larger high-endurance USB 3 SSD for the data disk.
- A dedicated, fused 12 V DC branch feeding an isolated or regulated marine
  supply that delivers a stable 5 V / 5 A to the Pi (the BOM allows a 10-32 V
  DC input).
- An NMEA 2000 interface, if you want boat data from the bus. A galvanically
  isolated interface is preferred; the PiCAN-M is field-proven on a boat but is
  not isolated and comes with grounding caveats.
- A separate backup drive, mounted outside the data disk.
- Optional: one Waveshare ESP32-S3-Touch-LCD-2.8C instrument display (a preview
  feature, see [Instrument displays](docs/DISPLAYS.md)).

A Raspberry Pi 4 also works, and a Debian or Ubuntu host with systemd is
supported. Setup needs the internet once; afterwards the stack runs on the boat
network without it.

## Where to go next

| I want to… | Read |
|---|---|
| Understand the hardware and buy parts | [Hardware bill of materials](docs/BOM.md) |
| Build a new Pi | [Hardware bill of materials](docs/BOM.md), then [Installation](docs/INSTALLATION.md) |
| Add VesselStack to an existing Pi | [Quick start](#quick-start-one-command-on-the-raspberry-pi), then [Getting started](docs/GETTING_STARTED.md) |
| Do first boot, SSH and token steps | [Getting started](docs/GETTING_STARTED.md) |
| Use or secure the Control Panel | [Control Panel](docs/CONTROL_PANEL.md) |
| Set up Boat Chat, units and timezone | [Boat Chat setup](docs/BOAT_CHAT.md), providers in [Boat Chat providers](boat-chat/README.md) |
| Reach dashboards from other devices | [Network access and security](docs/NETWORK.md) |
| Manage MQTT accounts and access lists | [MQTT access control](docs/MQTT.md) |
| Update, roll back, or install offline | [Updating](#updating), [Updating and offline install](docs/UPDATING.md) |
| Release to a lab Pi and an offline boat | [Deployment runbook](docs/DEPLOYMENT.md) |
| Connect VesselTwin | [VesselTwin integration](docs/VESSELTWIN.md) |
| Add ESP32 instrument displays | [Instrument displays (preview)](docs/DISPLAYS.md) |
| Develop, test, or cut a release | [Development guide](docs/DEVELOPMENT.md) |
| See every document | [Documentation index](docs/README.md) |

Power sources and instrument-reading coverage for Boat Chat are in
[Power monitoring](boat-chat/POWER_MONITORING.md).

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
and explicit reading opt-ins. Managed Docker/native Signal K modes preload the pinned
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

Lifecycle commands are `vesselstackctl display list|add|remove|rotate|wifi|status|render|export|build|flash|themes|set-theme|catalog|show|set-pages|history|restore-config|serial-ports`.
See [Instrument displays (preview)](docs/DISPLAYS.md) for setup, firmware, layouts, the pinned ESPHome version and limitations, and [displays/README.md](displays/README.md) for the code layout.

Display colours come from `displays/themes.json`, which offers four themes: `classic` (the default and the original look), `high-contrast` (black with white values and a light tile border, for sunlight), `daylight` (light tiles with dark text) and `ocean` (navy tiles). Pick one per display:

```bash
sudo vesselstackctl display themes                  # list the themes (--json for the full palettes)
sudo vesselstackctl display set-theme helm daylight
sudo vesselstackctl display build helm              # then: display flash helm --serial DEVICE (or --ota HOST)
```

`set-theme` writes `ui.theme` in `displays.json`; the change reaches the screen only after you rebuild and reflash that display. Night mode uses the same red palette on every theme and the alarm banner is never themed. `set-theme ID classic` removes the key and returns to the original colours. **Before downgrading VesselStack, run `set-theme ID classic` on every display that has a theme:** older versions reject the unknown `theme` key and the displays bridge then exits. A display left on `classic` generates exactly the firmware it did before, so nothing needs reflashing. The `high-contrast` warning tile is dark amber with white text (the old bright amber made its label unreadable); rebuild and reflash a display on that theme to get it. Details and limits are in [Themes](docs/DISPLAYS.md#themes).

**Choosing what a page shows.** The Control Panel's **Displays** tab is read-only for now: it lists your displays, shows whether the latest changes are on each screen (On screen, Changes not on screen, Never flashed, Not built, Unknown), a readiness checklist with the exact fix command for anything missing, each page's layout and items, an approximate preview of every page in the display's theme (Day/Night and severity switches, live values when Signal K has them), and a searchable list of the data a page can show. Editing pages, choosing a theme, building and flashing from the tab are not available yet, so use the commands below, which the tab itself reads (all JSON output, root for the ones that change anything). Adding a display and entering Wi-Fi stay on the command line.

```bash
sudo vesselstackctl display list --json              # id, name, board and page count of every display
sudo vesselstackctl display catalog --live --json    # ~40 common values for your BOAT_UNITS, marked live or no_live_data
sudo vesselstackctl display show helm --json         # pages, layouts, preview boxes, revision, readiness (no secrets)
echo '{"pages":[{"title":"Nav","layout":"grid-2x2","items":["sog","depth"]}]}' | \
  sudo vesselstackctl display set-pages helm --if-revision "$(sudo sha256sum /opt/vesselstack/config/displays.json | cut -d' ' -f1)" --stdin
sudo vesselstackctl display history                  # backups of displays.json (newest 20)
sudo vesselstackctl display restore-config 20260101T120000Z
sudo vesselstackctl display serial-ports             # USB serial devices that `flash --serial` accepts
```

The data list (`displays/catalog.json`) offers 38 common Signal K values and 4 notifications with the right unit for `BOAT_UNITS` (`metric` or `us_customary`; the legacy `nautical` reads as metric). With `--live`, `catalog` makes one read-only request to your configured `SIGNALK_URL` (Bearer token only if `DISPLAYS_SIGNALK_TOKEN` is set, no redirects, 10 s in total, 4 MiB) to mark which values have data and to list other numeric values under "Uncatalogued"; it keeps only path, units, age and value of numeric leaves and never reads positions, destinations, MMSI, names, UUIDs or call signs, and it stores nothing. **Suggested alarm limits are shown but never applied by default**: an instrument with limits counts in the boat-wide alarm summary and can raise the alarm banner. `set-pages` refuses (exit 3) if `displays.json` changed since you read it (`--if-revision` is the sha256 of the file), refuses (exit 4) while a build or flash of that display runs, validates everything before writing, copies the previous file to `config/displays-backups/` (root-only, newest 20 kept, included in `vesselstackctl backup`) and restarts the displays bridge only when it reads something that changed. **Rollback:** `display history`, then `display restore-config STAMP` (refused with exit 2 if the backup has a different set of displays than the current `displays.json`: match them with `display add` / `display remove` first, then restore again, because restore does not touch MQTT accounts); rebuild and reflash the displays the command lists in `rebuild_needed`. Exit codes, JSON shapes and limits are in [Data catalog](docs/DISPLAYS.md#data-catalog) and [Editing pages, backups and restore](docs/DISPLAYS.md#editing-pages-backups-and-restore).

## Quick start: one command on the Raspberry Pi

**Headless is the normal case: the Pi needs no screen, keyboard or browser.** On a
Raspberry Pi running 64-bit Raspberry Pi OS (or Debian) with internet access, SSH into
it from your laptop (use `ssh -t` so the installer can ask its questions on that
session), run the pinned release installer and answer the questions in the same terminal:

```bash
ssh -t YOUR_USER@raspberrypi.local
curl -fsSL https://github.com/jclima/VesselStack/releases/download/v0.6.1/get-vesselstack.sh | sudo bash
```

The same release is also selected by `VESSELSTACK_VERSION=0.6.1` or `--version 0.6.1`:

```bash
curl -fsSL https://github.com/jclima/VesselStack/releases/download/v0.6.1/get-vesselstack.sh | sudo VESSELSTACK_VERSION=0.6.1 bash
curl -fsSL https://github.com/jclima/VesselStack/releases/download/v0.6.1/get-vesselstack.sh | sudo bash -s -- --version 0.6.1
```

> [!NOTE]
> The pinned URL names the release explicitly, so it installs exactly 0.6.1 whatever
> GitHub marks as Latest. The form `https://github.com/jclima/VesselStack/releases/latest/download/get-vesselstack.sh`
> points to whichever release GitHub marks Latest, which is not necessarily the newest
> one (every 0.5.x release was published as a prerelease, so that URL kept serving
> 0.4.4). Use it only when you want exactly that.

The installer checks the host before changing it. It may install Debian
prerequisites and, after asking, Docker Engine and Compose v2 from Docker's
official apt repository (signing-key fingerprint checked; details and undo steps are in
[the installation guide](docs/INSTALLATION.md#quick-path-one-command)). It then downloads and
checksum-verifies the release, asks for boat and service settings, and offers a final review
before starting VesselStack. `sudo` is required because installation writes system packages,
services, and `/opt/vesselstack`; the questions explain each change. Secrets are generated or
entered without echo and are not printed in the review. **32-bit Raspberry Pi OS (armhf) is
refused**: Docker Engine v28 was the last release for it. Re-flash with "Raspberry Pi OS
(64-bit)" in Raspberry Pi Imager (VesselStack supports Raspberry Pi 4 and 5);
`dpkg --print-architecture` must say `arm64`.

(Or paste the command into a terminal on the Pi itself. No address is opened and no
tunnel is needed.) The questions are grouped in short sections, each with a plain-language
explanation and a sensible default in `[brackets]`: **Enter** accepts the default, **?**
explains the question, **b** goes back one question, and a final review screen lists every
choice (never any secret) with *save*, *edit a section* and *quit*. A short example is in
[the installation guide](docs/INSTALLATION.md#quick-path-one-command). At the end the
installer prints the Grafana, Home Assistant and Boat Chat addresses, the command that shows
the Boat Chat access token, and the SSH tunnel command for the Control Panel. Everything
printed at the end is information only: nothing in the install needs a browser, and nothing is
opened for you.

For a boat with no internet, prepare the release and offline bundle in the lab and follow the
[offline deployment path](docs/DEPLOYMENT.md#simple-path-one-command).

Releases are built by the tag-triggered GitHub workflow (read-only build job, separate
write-only publish job, draft first, reproducible tarball on Linux); maintainers follow the
release checklist in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md#release-checklist). The recommended
way to sign release tags (key held in 1Password) is in
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#1-release-signing-with-an-ssh-signing-key).
Release tags must be SSH-signed annotated tags: the release workflow runs
`scripts/verify-release-tag.sh` against `.github/allowed_signers` and refuses an unsigned,
lightweight or wrongly signed tag, a tag object whose own name differs from its ref, and a tag
that does not point at the exact commit being built, before building anything. To rotate the release key, change
`.github/allowed_signers` in a reviewed pull request to `main` first, then tag.

**No prompts at all** (scripts, a Pi without a terminal, repeatable installs):

```bash
# defaults, plus the settings you name (repeat --set; names as in vesselstack.example.env)
curl -fsSL https://github.com/jclima/VesselStack/releases/download/v0.6.1/get-vesselstack.sh \
  | sudo bash -s -- --yes --set BOAT_NAME=Foo --set SOCKETCAN_ENABLE=true
# every question's own default answer, nothing asked
curl -fsSL https://github.com/jclima/VesselStack/releases/download/v0.6.1/get-vesselstack.sh | sudo bash -s -- --defaults
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

Run the guided installer above in that terminal. In **Who can reach what**, the
question **Control panel listens on** defaults to **this Pi only (SSH tunnel)**. Keep
that answer: it is the recommended setting. The installer ends by printing the tunnel
command; run it on your own computer, open `http://127.0.0.1:8780`, and paste the
panel token into **Unlock**:

```bash
ssh -L 8780:127.0.0.1:8780 YOUR_USER@YOUR_PI.local
sudo grep '^CONTROL_PANEL_TOKEN=' /opt/vesselstack/config/control-panel.env
```

Reaching the panel directly over the boat network is an opt-in choice for a trusted
lab or boat LAN: answer **trusted network (token required)** instead, or apply it to an
installed system with this command and use the address it prints:

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
interface; setup never silently opens firewall ports. Local-only (SSH tunnel) is the default and the recommendation.

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
   immediately, and offers sections for the boat (name, type, units, timezone detected
   from the system, optional and private MMSI, call sign, length and beam), Signal K
   (docker, existing or native; docker recommended), NMEA 2000 / CAN (`SOCKETCAN_ENABLE`,
   interface, bit rate and the home-lab virtual-CAN option `VESSELSTACK_CAN_CREATE_VCAN`),
   AIS, Home Assistant (URL, optional hidden token), Boat Chat provider (default `local`)
   and Telegram, the firewall (untrusted interface, enable, strict mode, allowed ports),
   raw-data retention (`INFLUXDB_RAW_RETENTION`; empty means 30 days on a new install and no
   change to an existing database) and who can reach the dashboards (`local`, `dashboards`
   or `all`; see "Reaching VesselStack from other devices"). Ports, container and bucket
   names, folders, the service account and the generated secrets sit behind "Advanced
   settings? [y/N]". Passwords and tokens default to being generated during installation, are
   read without echo when you do type one and are never printed or logged. It never turns
   hardware on by itself. The terminal wizard has no network surface. The optional browser
   wizard (`--web-wizard`) serves loopback only and cannot be combined with `--config`,
   `--set` or `--defaults`. How the wizards validate, stage and install the file is described
   in [Development: wizard internals](docs/DEVELOPMENT.md#wizard-internals);
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
curl -fsSLO https://github.com/jclima/VesselStack/releases/download/v0.6.1/get-vesselstack.sh
curl -fsSLO https://github.com/jclima/VesselStack/releases/download/v0.6.1/get-vesselstack.sh.sha256
sha256sum -c get-vesselstack.sh.sha256
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
git checkout v0.6.1
python3 vesselstack-wizard.py --cli     # terminal questions (no browser)
# python3 vesselstack-wizard.py        # or the optional browser wizard
```

The terminal wizard (`--cli`) asks in the terminal and writes `vesselstack.env`; add
`--defaults` and `--set KEY=VALUE` to configure without questions, `--base FILE` to start
from an existing file, and `--validate FILE` to check an existing env file with the wizard's
rules (exit 0 if valid, 1 otherwise, no server started). The browser wizard walks seven steps
(Preflight, Hardware, Vessel, Storage, Signal K, Network, Review); Back keeps your choices and
Continue stays blocked until the page has its address token. It prints a per-run
loopback address of the form `http://127.0.0.1:PORT/#token=...`; open that exact address, on
the Pi or through an `ssh -L` tunnel. Both wizards write a private `vesselstack.env` and
change nothing else: no boot firmware, firewall rules, marine wiring or services. The browser
wizard's options, token handling and request protections are described in
[Development: wizard internals](docs/DEVELOPMENT.md#wizard-internals). When the browser wizard
saves the file, its last page shows the commands to run next (a `--dry-run` check, then
`--start`, then `sudo vesselstackctl doctor`) and the wizard may then stop by itself, which is
expected. Run `sudo ./install.sh --config vesselstack.env --dry-run` and `--start` as described
in the sections below. The pages are shown with screenshots in [Getting started](docs/GETTING_STARTED.md#the-optional-browser-wizard).

Rollback: delete the generated `vesselstack.env`; if a browser-wizard run was killed with
SIGKILL, remove its temporary account with `sudo userdel vesselstack-wizard`.

> [!WARNING]
> VesselStack is not certified navigation, collision-avoidance, engine-control,
> alarm, or life-safety equipment. Maintain independent marine instruments and
> alarms. Never depend on this software as the sole source of safety data.

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

## Upgrading to 0.6.0

Read this before updating an installed system. Update from [Updating](#updating) with a backup
on a mounted external drive, not while underway. This section applies to 0.6.0 and 0.6.1.

**Settings that say `BOAT_UNITS=nautical` (fixed in 0.6.1).** Older setup wizards offered
"nautical" as a units choice, and installers before 0.5.7 did not check it, so such a
`vesselstack.env` may exist. 0.6.0 refused it with `BOAT_UNITS must be us_customary or metric`
and stopped the update before changing anything. From 0.6.1 the installer reads `nautical` as
`metric` (metres for depth; knots and nautical miles are kept in both systems), prints one
`Note:` line, and a real install or update rewrites `vesselstack.env` with `BOAT_UNITS=metric`
(a dry run only prints the note). The setup wizard and the Control Panel also show it as Metric
without changing it. Any other unsupported value is still refused before anything is changed,
now with a message naming the file and the fix. If you are stuck on 0.6.0, change the value
by hand and run the update again:

```bash
sudo sed -i 's/^BOAT_UNITS=.*/BOAT_UNITS=metric/' /opt/vesselstack/config/vesselstack.env
```

**Backup drive not mounted (0.6.1).** When the backup folder is under `/media` or `/mnt` and the
drive is not mounted, `get-vesselstack.sh` and (from an installed 0.6.1) `vesselstackctl update` and
`vesselstackctl backup` no longer just stop: before anything is stopped or changed they ask
whether to mount the drive from `/etc/fstab`, pick a connected USB drive, check again, or back up
to the system disk once. See [Backups](#backups). A direct `sudo vesselstackctl update <dir>` run
from an installed 0.6.0 or earlier controller does not show this menu (that controller is older
than the fix); use `get-vesselstack.sh`, or mount the drive first, or run
`sudo env VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=true vesselstackctl update <dir>` to back up to
the system disk once.

Rollback: set `BOAT_UNITS` back to `us_customary` (or `metric`) in the same file. 0.6.1
needs no other manual step.

**Fix it at the prompt (0.6.1).** When you run the installer, an update or the pinned
`get-vesselstack.sh` from a terminal (for example over `ssh -t`), an unsupported `BOAT_UNITS`
value, or a setting in `vesselstack.env` that cannot be written safely (see below), no longer
just stops the run. The installer asks what to do before changing anything else:

```text
BOAT_UNITS in /opt/vesselstack/config/vesselstack.env is "imperial", which is not supported.
  [1] US customary (feet, °F, psi, US gallons)
  [2] Metric (metres, °C, kPa, litres)
  [c] Cancel (nothing is changed)
Choice:
```

An unsafe value (a token or other setting with an apostrophe plus a backslash, double quote,
dollar sign or backtick, a line break, or a control character) is offered as
`[1] Enter a new value for <KEY>`; what you type for a token, password or key is hidden and is
never printed or logged, and a replacement that is still unsafe is refused. Your answer is saved
in the settings file, the installer prints `Changed <KEY> in <file> (previous copy: <file>.before-fix-<time>)`
and starts over with the same options. The previous copy is root-only; delete the
`*.before-fix-*` copies once the install works. `c`, an empty answer at the end of input or
five minutes without an answer prints `Cancelled. Nothing was changed.` and leaves the file as
it was. Limits: only settings written in the file you pass with `--config` are offered (a value
that comes from the environment is not), generated or preserved passwords and tokens (InfluxDB,
Grafana, MQTT, Boat Chat, Control Panel) are never offered because the running services still hold the
old value (change it in the service first, then edit the file), and a symbolic link, a file not
owned by root, a file in a folder (or under a parent folder) that other users can change, or a
file whose edit cannot be verified is left untouched. For an unsupported units value the manual
`sed` command is printed; for an unsafe value you are told to edit the setting by hand. Runs without a terminal (cron, a service, the Control Panel, `--yes`,
`VESSELSTACK_NONINTERACTIVE=true`) never ask: they stop before changing anything and print the
exact fix, as in 0.6.0. A dry run that you answer does save the fix you chose.

**What changes for an existing boat**

- **Env-file values are checked more strictly.** The installer refuses a value that holds an
  apostrophe together with a backslash, a double quote, a dollar sign or a backtick, and any
  multi-line value, in the settings it writes to `vesselstack.env`, `boat-chat.env` and
  `control-panel.env`. A boat name such as `Skipper's "Dream"` is affected; `Skipper's Dream`
  is not. The check now runs first: before anything is changed, and also under `--dry-run`, and
  it covers preserved and generated secrets too. The installer also deletes stale
  `.vesselstack.env.*`, `.boat-chat.env.*` and `.control-panel.env.*` temp files from `config/`
  (left by a killed run) before writing; no other file is touched.
- **`GET`, `HEAD` and `POST /api/settings` require the Boat Chat access code.** A bookmark,
  script or dashboard that read or saved settings without it now receives an authentication
  error. Where an admin code is configured, `POST` needs that too. Scripts that call `POST
  /api/settings` must send the access code, and, when it differs from the admin code, the new
  optional `X-Boat-Chat-Access-Token` header carries it on `GET`, `HEAD` and `POST`. The access
  code cannot be cleared through the API (blank or `null` returns 400) unless
  `BOAT_CHAT_ALLOW_OPEN=true`. The environment variable names
  (`BOAT_CHAT_ACCESS_TOKEN`, `BOAT_CHAT_SETTINGS_TOKEN`) are unchanged.
- **Unauthenticated loopback settings writes require a local `Host` name** (a defense
  against DNS rebinding). Reach Boat Chat through `127.0.0.1` or `localhost`, or send the
  token.
- **The Control Panel looks different.** It now has four tabs (Home, Settings, Updates,
  Maintenance). **Install & start** is **Reinstall & start** and **Run preflight** is **Check
  setup**, both under Maintenance → Setup & repair, and they run the same commands as before.
  Saving settings and applying them are two steps (**Save**, then **Apply**). The Updates tab
  contacts GitHub only when you press **Check for updates**. Stored settings and keys are
  unchanged. Boat Chat's credentials are labelled **Access code** and **Admin code**; the
  variable names are unchanged.
- **The Control Panel checks a few settings as you save.** The timezone must be a valid IANA
  name such as `Europe/Lisbon` (some legacy aliases such as `US/Eastern` may be rejected on
  newer systems where they moved to the `tzdata-legacy` package). Boat length must be more
  than 0 and at most 500, beam more than 0 and at most 100 (plain numbers in metres, no
  units), and CAN speed a whole number from 10000 to 1000000. The checks apply only to fields
  you edit, so an existing saved value you do not touch is never rejected or cleared; errors
  appear next to the field.
- **Docker Signal K may restart once.** If the update newly preloads the pinned
  `signalk-vesseltwin` 0.2.3 plugin into an already-running Signal K container, the
  installer restarts that container so the plugin loads. An existing preloaded copy is never
  overwritten. Plan for a short gap in Signal K data.
- **Coming from 0.4.x:** update once from the CLI with the pinned installer. Since 0.5.0
  Mosquitto always enforces an access list, so the first update recreates the broker once, and
  MQTT users you added by hand to the old password file lose access unless you add them
  through VesselStack (see [MQTT access control](docs/MQTT.md)).

**Env-file values are checked before anything changes.** `sudo ./install.sh --config FILE
--dry-run`, and therefore `vesselstackctl update` and the Control Panel updater (both run a dry
run first), refuse an unsafe value before the backup is taken, services are stopped or any file
is touched. The installer lists each offending setting by key name only (never the value), says
why, and exits with an error:

```text
  BOAT_NAME: a single quote combined with a backslash, double quote, dollar sign or backtick
Refusing to continue: the settings above cannot be written safely to the environment files.
```

Edit that value (for example `BOAT_NAME` in the Control Panel or `vesselstack.env`) so it no
longer combines an apostrophe with one of those characters or spans lines, then run the dry
run or the update again. Env files are also written atomically (to a temporary file that
replaces the old one only when complete), so a failed run leaves the previous file intact. If an
older release left an update stopped on this problem, restore the pre-update backup with the
command under Rollback below, fix the value, and update again.

**Verify after updating**

```bash
cat /opt/vesselstack/config/installed-version     # 0.6.1
sudo vesselstackctl doctor                        # RESULT healthy
sudo vesselstackctl status
```

Open Boat Chat and the Control Panel, and confirm the dashboards show live values.

**Rollback.** Downgrades are refused (`Downgrade from 0.6.1 to ... is not supported`). Restore the
verified pre-update backup instead:

```bash
sudo vesselstackctl verify-backup /path/to/vesselstack-TIMESTAMP.tar.gz
sudo vesselstackctl restore /path/to/vesselstack-TIMESTAMP.tar.gz --yes
```

The restore keeps the previous trees as `*.pre-restore-STAMP` until you delete them (see
[Backups](#backups)).

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
| Displays bridge (opt-in, `DISPLAYS_ENABLE=true`) | Publishes configured Signal K instruments to MQTT for instrument displays | systemd service `vesselstack-displays-bridge`; no listener |

The installer generates credentials, renders a pinned Docker Compose stack,
creates a confined systemd service for Boat Chat, creates configurable InfluxDB
buckets, provisions the one-minute downsample task expected by Boat Chat,
provisions Grafana datasources and a system-health dashboard, and installs the
`vesselstackctl` lifecycle command. It also installs a local-first, token-
authenticated Control Panel that can inspect and operate every managed
container and host service, edit redacted configuration, check the setup, save and apply
settings, create backups, and check for and install release updates.

All container tags are release-pinned and checked for Linux ARM64 availability;
VesselStack does not rely on floating `latest` tags.

The installer supports an existing Signal K server, an opt-in
pinned Signal K container, or an opt-in pinned native Signal K installation. It
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
- Existing Signal K server or a selected VesselStack Signal K install mode
- Dedicated non-root service account

Installation downloads container images and a Python dependency, so temporary
internet access is required.

## Before installation

1. Back up existing Home Assistant, InfluxDB, Grafana, MQTT, and Signal K data.
2. Confirm ports `80`, `443`, `1883`, `8086`, `8123`, `8765`, `8780`, `9090`,
   and `43000` are free. Port `8780` binds to loopback by default.
3. If using `SIGNALK_MODE=existing`, confirm Signal K responds:

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

To work from the 0.6.1 release, check out its tag:

```bash
git fetch --tags
git checkout v0.6.1
```

`v0.6.1` is an SSH-signed tag (Ed25519 release key, verified by the release workflow against
`.github/allowed_signers`). It is published first as a pre-release and marked Latest after the
maintainer's physical Raspberry Pi install passes; see the [CHANGELOG](CHANGELOG.md). Verify the
tag yourself (needs `ssh-keygen`, which ships with OpenSSH):

```bash
git fetch --tags
scripts/verify-release-tag.sh v0.6.1
# maintainers and owners: also bind the tag to the commit you recorded when it was built
# (the full 40-character SHA from your release notes, not one read back from the tag itself)
scripts/verify-release-tag.sh v0.6.1 .github/allowed_signers "$RECORDED_COMMIT"
# equivalent plain git command:
git -c gpg.format=ssh -c gpg.ssh.allowedSignersFile=.github/allowed_signers verify-tag v0.6.1
```

The script prints the signer and key fingerprint, and exits non-zero for an unsigned,
lightweight or wrongly signed tag. The release key fingerprint is
`SHA256:01UcXtAoMkJ7zfQ2n7gIc4fUjPa3+0BVvHJ2VHIuTIw`. Older tags (`v0.5.x`) are unsigned and
fail this check by design.

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
| `HOMEASSISTANT_BIND_ADDRESS` | IPv4 address Home Assistant (host-networked, `8123`) listens on; default `0.0.0.0`. Any other value makes the installer manage one delimited `http:` block in `${VESSELSTACK_DATA}/homeassistant/configuration.yaml` (see "Reaching VesselStack from other devices"). Signal K has no equivalent setting |
| `BOAT_CHAT_ACCESS_TOKEN` | `GENERATE` (default) creates a random token, preserved on re-install; required for chat, status and data APIs |
| `INFLUXDB_CONTAINER_NAME` | Advanced override for existing/custom Compose deployments |
| `BOAT_TIMEZONE` | IANA timezone, such as `America/Los_Angeles` |
| `BOAT_UNITS` | `us_customary` or `metric` (the legacy value `nautical` is read as `metric`; case and surrounding spaces are ignored) |
| `VESSELSTACK_USER` | Existing non-root service account |
| `VESSELSTACK_UID`, `VESSELSTACK_GID` | Derived from the service account by the installer |
| `VESSELSTACK_ROOT` | Application path; default `/opt/vesselstack` |
| `VESSELSTACK_DATA` | Persistent container-data path |
| `VESSELSTACK_BACKUP` | Backup destination; must be outside the application and data trees |
| `SIGNALK_MODE` | `existing`, `docker`, or `native` |
| `SIGNALK_VERSION` | Pinned Signal K release used by managed modes |
| `SIGNALK_URL` | Existing Signal K endpoint |
| `SIGNALK_VESSELTWIN_PRELOAD` | Managed Docker/native modes preload immutable `signalk-vesseltwin` 0.2.3 (default `true`); existing mode ignores it, and `false` skips without uninstalling |
| `HOME_ASSISTANT_TOKEN` | Optional long-lived token used by Boat Chat |
| `SOCKETCAN_ENABLE` | Opt in only after the CAN interface is present |
| `SOCKETCAN_INTERFACE`, `SOCKETCAN_BITRATE` | CAN device and NMEA 2000 bitrate (`vcan*` interfaces ignore the bitrate) |
| `VESSELSTACK_CAN_CREATE_VCAN` | `true` creates a missing `vcan*` interface at boot (home-lab replay only; default `false`) |
| `AIS_ENABLE` | Opt in to the AIS-catcher Compose profile |
| `AIS_DEVICE` | USB bus or receiver device mapped into AIS-catcher |
| `AIS_CATCHER_ARGS` | Reviewed AIS-catcher receiver/output arguments |
| `INFLUXDB_ORG` | InfluxDB organization |
| `INFLUXDB_RAW_BUCKET` | Full-resolution Signal K data; default `signalk` |
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
| `DISPLAYS_SIGNALK_TOKEN` | Optional Signal K access token for the displays bridge, sent as `Authorization: Bearer`; leave empty unless Signal K security requires it; must not contain line breaks or any other control character (the Control Panel and the installer both refuse them) |
| `DISPLAYS_HA_TOKEN` | Optional long-lived access token of a dedicated Home Assistant user (a leaked token can operate every device in that Home Assistant; see [Home Assistant items](docs/DISPLAYS.md#home-assistant-items)), used only by the displays bridge for Home Assistant items (`ha` in `displays.json`, see [Home Assistant items](docs/DISPLAYS.md#home-assistant-items)); the bridge connects to `HOME_ASSISTANT_URL` (default `http://127.0.0.1:8123`). Stored only in the root-only 0600 `config/displays-bridge.env`, never logged; same rule as the Signal K token: no line breaks or other control characters. Different from `HOME_ASSISTANT_TOKEN` (Boat Chat) |
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

For native Signal K, install Node.js 22 or newer and npm before preflight. For
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
Signal K paths. VesselStack installs reusable low-battery, bilge/high-water, and
shore-power-loss blueprints under **Settings → Automations → Blueprints**.
Create automations from them only after validating the selected entities,
thresholds, durations, and notification actions.

The installer also places
`/opt/vesselstack-data/homeassistant/vesselstack-dashboard.example.yaml` as a
starter Lovelace view. Copy it into a dashboard only after replacing every
`sensor.vesselstack_*` example with a real entity from the vessel. VesselStack
does not silently enable safety alerts or assume thresholds.

### Signal K and InfluxDB

With `SIGNALK_MODE=docker`, VesselStack starts the official pinned Signal K
image and persists its settings in `/opt/vesselstack-data/signalk` (owned by
uid/gid 1000, the image's `node` user, so the container can write it). With
`SIGNALK_MODE=native`, it installs the pinned npm package under
`/opt/vesselstack/signalk-server` and runs it as the locked VesselStack user.
`SIGNALK_MODE=existing` never modifies Signal K. The managed modes use the same
first-run admin onboarding at `http://HOST:3000`.

Managed Docker and native installs preload the pinned, zero-runtime-dependency
`signalk-vesseltwin` 0.2.3 package before Signal K starts. The archive is
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
TCP feed on port `5011`. Add a Signal K NMEA 0183 TCP client connection to
`127.0.0.1:5011`. When SocketCAN is enabled, VesselStack validates the named
interface and installs a 250-kbit/s-by-default systemd unit; configure the
matching Signal K NMEA 2000 connection in Signal K's admin UI.

Configure a Signal K InfluxDB 2-compatible output plugin with values from
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

Keeping the full-resolution bucket (`INFLUXDB_RAW_BUCKET`) forever eventually fills
the boat's disk, so new installs create it with a 30-day retention
(`INFLUXDB_RAW_RETENTION`, default `30d`); the one-minute history bucket keeps one year. **Upgrades never change an existing bucket
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
username/password changed since the last run, so re-running the installer
does not reset MQTT credentials. Update Home Assistant's MQTT integration to match.

#### Virtual CAN for home-lab replay

On a lab machine without a CAN adapter you can replay a recorded log into a
virtual CAN interface and let Signal K read it as if it were the boat bus. Set

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

Point the Signal K NMEA 2000/SocketCAN connection at `vcan0`. Recorded logs
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
the operational baseline, then add vessel dashboards for the Signal K paths
available on the boat. Provisioned dashboards are editable in Grafana; keep
reusable versions in the repository so reinstallations remain reproducible.

### Boat Chat

Open `http://HOST:8765`. The page opens on **Overview** (boat condition, observations, what
needs attention and suggested actions); **Chat** is the question box. **More** holds
**Boat Hub** (trips, position and maintenance), **Prompts** and **Settings**. When an access
code is required, choose **Connect** and enter it; it is kept in that browser tab only. The
access code is `BOAT_CHAT_ACCESS_TOKEN` in `config/boat-chat.env`, and the **Admin code**
(`BOAT_CHAT_SETTINGS_TOKEN`) authorizes changing Settings. The default `local` provider sends
nothing to an external model.

![Boat Chat Overview in a browser](docs/images/boat-chat-overview.png)

Settings, the admin code, API access rules, telemetry refresh, optional host metrics, timezone
and units are in [Boat Chat setup](docs/BOAT_CHAT.md); providers are in
[Boat Chat providers](boat-chat/README.md).

### Control Panel

The Control Panel is the browser page for looking after VesselStack. It listens on loopback by
default (`http://127.0.0.1:8780` through an SSH tunnel); `sudo vesselstackctl panel-access
lan|local` changes only that bind. Sign in with the token from
`sudo grep '^CONTROL_PANEL_TOKEN=' /opt/vesselstack/config/control-panel.env`; pasting the whole
`CONTROL_PANEL_TOKEN=...` line works too. The full tour is in the
[Control Panel guide](docs/CONTROL_PANEL.md).

![Control Panel Home tab](docs/images/control-panel-home.png)

- **Home** is status only: services running or stopped, the last backup and backup drive (read-only;
  "unknown" when they cannot be read) and whether an update is known, with one **Back up now**
  button and the components below. Component cards offer Start, Stop, Restart and, in a **⋯**
  menu, Disable.
- **Settings** has a jump list, help for every field, Advanced sections that start collapsed,
  inline field errors, and one save bar: **Save** writes the settings, **Apply** renders and installs
  them (it does not recreate containers and restarts only the displays bridge; restart a component to use the new values).
- **Updates** checks only when you press **Check for updates**, which contacts github.com.
- **Maintenance** holds Back up now, Start all, Restart all, Stop all, **Check setup**,
  **Reinstall & start**, **Provision history** and **Restart Control Panel**.

Stopping or interrupting actions ask first in an in-page dialog. If the connection drops the
header shows Disconnected with **Retry now**. Rollback: restore the previous release from a
backup as described under [Updating](#updating); stored settings keys are unchanged.

### Heimdall

Open `http://HOST` and add trusted-network links to Home Assistant, Grafana,
Signal K, Boat Chat, InfluxDB, and Prometheus.

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
USB drive cannot silently fill the SD card; pass
`vesselstackctl backup --allow-unmounted DIR` (or set
`VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=true`, which also applies to
`vesselstackctl update`) to override. A warning is printed when the destination
is on the same filesystem as the data.

When the drive is not mounted and you run the command from a terminal, the update or backup
shows a menu instead of stopping. Nothing has been changed or stopped at that point:

```text
The backup folder <DEST> is on <MOUNT_DIR>, which is not mounted.
Backing up now would put the archive on the system disk (SD card).
Nothing has been changed yet.
  [1] Mount <MOUNT_DIR> now (it is listed in /etc/fstab)
  [2] Choose a connected drive to mount at <MOUNT_DIR>
  [3] I have plugged the drive in; check again
  [4] Back up to the system disk this once (not recommended)
  [c] Cancel the update
```

- **[1]** appears only when `/etc/fstab` has an entry for that folder; it runs `mount`.
- **[2]** lists connected USB drives that are not mounted and not part of the system disk
  (the disk holding `/`, `/boot/firmware` or the VesselStack data is never offered). Only
  **ext4** (recommended) and **exFAT** are accepted; vfat/FAT32 is refused because of its 4 GiB
  file limit, and other filesystems are listed with the reason. A drive that `/etc/fstab` assigns
  to a different mount point (for example a data SSD that failed to mount) is listed with that
  reason and cannot be picked. If no unmounted drive is found, the menu says so and names a
  removable ext4 or exFAT drive that is already mounted elsewhere (for example by the desktop
  under `/media/<user>/...`); unmount it with `sudo umount <path>` and choose [3]. The mount point
  must be empty. Before anything is written, the new mount's top folder must be owned by root and
  not writable by others, and no folder from there to the backup folder may be a symbolic link or
  owned by another user; otherwise the backup is refused.
  The drive is mounted with `nodev,nosuid,noexec` (exFAT also `uid=0,gid=0,umask=0077`) and stays
  mounted until reboot; the menu prints the `/etc/fstab` line to add to mount it at boot.
  VesselStack never edits `/etc/fstab` or formats a drive.
- **[3]** checks the mount again after you plug the drive in.
- **[4]** writes this one backup to the system disk after a warning. It is not offered when
  the free space is smaller than the data (`du -skx`, 60 s limit) or when `vesselstack.env` sets
  `VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=false`. Nothing is saved: the next backup asks again.
- **[c]**, end of input or no answer within 5 minutes cancels without changing anything (in
  the pinned installer it prints `Cancelled. The update was not started; your installed VesselStack
  was not changed.`; the packages and the release folder may already be in place). An answer must
  be exactly one listed token: `1 2` is asked again.
  `vesselstackctl backup --require-mounted` (used by the Control Panel) offers only [1] to [3].

Without a terminal (cron, systemd, the Control Panel, `--yes`, `VESSELSTACK_NONINTERACTIVE=true`) it
still stops with `Refusing to back up: ... is not mounted ...` and a `To fix:` line. A direct
`sudo vesselstackctl update` from an installed 0.6.0 or earlier controller does not show the
menu; use `get-vesselstack.sh` (it asks before calling the controller) or
`sudo env VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=true vesselstackctl update <dir>`.
Rollback: unmount the drive with `umount`; remove the added `/etc/fstab` line if you do not want it.

Besides the application and data trees the
archive includes `/etc/systemd/system/vesselstack-*`,
`/usr/local/sbin/vesselstack{ctl,-firewall}` and `/etc/vesselstack/update.env`
when they exist, so a restore across versions is complete.

The whole stack, including Home Assistant, stays stopped for the copy. Home
Assistant, Grafana, InfluxDB and Boat Chat all keep SQLite or write-ahead-log
state in the data tree, and a file copy of a running database is not
crash-consistent, so the archive is taken with the whole stack stopped.

Restore only during a maintenance window. Verification rejects path traversal,
absolute or escaping links, device nodes, FIFOs and any setuid, setgid or sticky
member (a legitimate backup never contains one) before anything is extracted:

```bash
sudo vesselstackctl restore /path/to/vesselstack-TIMESTAMP.tar.gz --yes
```

Restore never extracts over the live tree. It (1) verifies the archive,
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

This is the one place in the README that describes updating and rolling back.
Updates and backups started from the Control Panel never prompt; when one stops
for something you can fix, the result banner shows a **To fix:** line with the
exact step.

1. **Back up first** to a mounted external drive (see [Backups](#backups)):

   ```bash
   sudo vesselstackctl backup
   ```

2. **Re-run the pinned release installer.** On the Pi, over `ssh -t`:

   ```bash
   curl -fsSL https://github.com/jclima/VesselStack/releases/download/v0.6.1/get-vesselstack.sh | sudo bash
   ```

   The installer finds the installed system, never overwrites it silently, and offers
   `vesselstackctl update <release dir>` for the release it unpacked under
   `/opt/vesselstack-release/<version>`. After you confirm, `vesselstackctl update` runs the
   release's `install.sh --dry-run` preflight, makes and verifies a full backup, then runs
   `install.sh --start`. Answer **n** to leave the system untouched; the installer prints
   the exact command to run later. Services restart briefly, so do not update while underway.
   If the backup drive is not mounted, the installer asks, before calling the controller and
   before anything is stopped, whether to mount it, pick a USB drive, check again, or back up
   to the system disk once (see [Backups](#backups)); `--yes` and `--defaults` never choose and
   the update stops with the reason. Plain `sudo vesselstackctl update` with no argument fails: the command needs the release
   directory, for example `sudo vesselstackctl update /opt/vesselstack-release/0.6.1`.
   Without internet, use `--offline --bundle ...` as described under
   [Offline install](#offline-install-no-internet-on-the-boat).

3. **Or use the Control Panel.** An authenticated session can check the public
   `jclima/VesselStack` releases. Stable releases are shown by default; enable
   **Include early test releases** on the Updates tab to list prereleases as well, which you need for any release
   GitHub publishes as a prerelease. The panel shows the installed version and, for a newer release, a link to its release notes. Nothing contacts GitHub until you press
   **Check for updates**. **Back up & install** asks for confirmation in a dialog, verifies the downloaded SHA-256 manifest, safely extracts the
   release, runs preflight, then makes and verifies a backup on a mounted external drive
   (`/media` or `/mnt`; the browser cannot change the destination). A failed or unmounted
   backup stops the update. A detached updater restarts the Control Panel and verifies its
   new version before the browser reconnects. The panel uses ordinary HTTP and serializes
   its own update, configuration and component operations; do not run CLI maintenance
   commands at the same time. Transient updater files under `/var/lib/vesselstack` are not
   backed up; configuration and data are.

| Installed version | Update to 0.6.1 |
|---|---|
| 0.5.7 | Control Panel or the pinned installer. 0.6.1 is published first as a pre-release (in the 0.5.x panel, enable **Include lab prereleases** on the update card to see it; the 0.6.1 panel calls it **Include early test releases**) and is marked Latest after the maintainer's physical Raspberry Pi install passes |
| 0.5.5, 0.5.6 | Same as 0.5.7: the browser updater was introduced in 0.5.5 |
| 0.5.4 or earlier, including 0.4.x | Run the pinned installer from the CLI once (these versions cannot update from the browser); read [Upgrading to 0.6.0](#upgrading-to-060) first |

A checksum shows a download is intact; it is not signed provenance. The signature on the `v0.6.1` tag (see [Download VesselStack](#3-download-vesselstack)) is the provenance check, and the installer does not perform it for you.

**Rollback.** Downgrades are refused. If an update fails, the verified pre-update backup is
kept. Check it and restore it during a maintenance window:

```bash
sudo vesselstackctl verify-backup /path/to/vesselstack-TIMESTAMP.tar.gz
sudo vesselstackctl restore /path/to/vesselstack-TIMESTAMP.tar.gz --yes
```

There is no automatic rollback. To update from a reviewed source checkout with
`install.sh --dry-run` first, see [Updating and offline install](docs/UPDATING.md).

### Offline install (no internet on the boat)

Prepare a bundle on a connected machine, carry it to the boat, and install with
`sudo bash get-vesselstack.sh --offline --bundle /path/to/offline-bundle --tarball /path/to/vesselstack-0.6.1.tar.gz`.
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

### "Refusing to back up: ... is not mounted"

The backup folder is under `/media` or `/mnt` and the drive is not mounted. From a terminal
(`ssh -t`) the update or backup now asks what to do: mount it, pick a connected ext4 or exFAT
USB drive, check again, or back up to the system disk once (see [Backups](#backups)). Without
a terminal, or from an installed 0.6.0 or earlier controller, mount the drive and run again, or
use `sudo env VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=true vesselstackctl update <dir>` for one
backup on the system disk. A FAT32 (vfat) drive is refused; use ext4 or exFAT.

### The installer stops on `BOAT_UNITS` or "cannot be written safely"

Run it from a terminal (`ssh -t`) and answer the prompt: it offers to set the units or to
enter a replacement value, keeps a root-only copy of the previous file as
`<file>.before-fix-<time>` (safe to delete once the install works), and then continues. If it
only prints the message, the run had no terminal, `--yes` or `VESSELSTACK_NONINTERACTIVE=true`
was set, or the value is a generated password or token or comes from the environment: edit the
named setting in the file as printed, then run again. See [Upgrading to 0.6.0](#upgrading-to-060).

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

Confirm Signal K and InfluxDB values in
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

### Signal K not running / EACCES on package.json

Symptom (`SIGNALK_MODE=docker`): the `vesselstack-signalk` container restarts or
exits and `sudo docker logs vesselstack-signalk` shows
`EACCES: permission denied, open '/home/node/.signalk/package.json'`. The
`signalk-server` image runs Signal K as its `node` user (uid/gid 1000, config
directory `/home/node/.signalk`), but `<data>/signalk` was created by Docker as
`root:root`. No release before 0.4.3 created it; 0.4.3 and later create it
as `1000:1000` mode 0750 before `compose up` and repair a root-owned one on
every installer run, so rerunning the installer from the release (see
[Updating](#updating)) fixes it. `sudo vesselstackctl doctor` also
detects the condition and reports it. Start with the read-only doctor output
and inspect the exact configured Signal K data directory and ownership. The
installer repairs only the known state conservatively: an empty or root-owned
directory with safe contents may be repaired, while symlinks, hard links,
special files, or another owner are left untouched with instructions. Resolve
the indicated cause, then rerun the supported installer repair. Do not
blanket-chown the Signal K tree or restart the container while ownership and
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

## Moved sections

Earlier versions of this README held these sections. Old links to them land here and point to the current location.

- <a id="alarm-summary-and-takeover"></a>[Alarm summary and takeover](docs/DISPLAYS.md#alarm-summary-and-takeover)
- <a id="automatic-telemetry-refresh"></a>[Automatic telemetry refresh](docs/BOAT_CHAT.md#automatic-telemetry-refresh)
- <a id="board-registry-schema-2"></a>[Board registry schema 2](docs/DISPLAYS.md#board-registry-schema-2)
- <a id="building-and-flashing-on-the-pi"></a>[Building and flashing on the Pi](docs/DISPLAYS.md#building-and-flashing-on-the-pi)
- <a id="choose-your-path"></a>[Choose your path](#where-to-go-next)
- <a id="configuration-and-validation"></a>[Configuration and validation](docs/DISPLAYS.md#configuration-and-validation)
- <a id="current-development-improvements"></a>[Current development improvements](docs/DEVELOPMENT.md#current-development-improvements)
- <a id="displays-bridge-service"></a>[Displays bridge service](docs/DISPLAYS.md#displays-bridge-service)
- <a id="find-your-next-answer"></a>[Find your next answer](#where-to-go-next)
- <a id="firmware-generate-build-and-flash"></a>[Firmware: generate, build and flash](docs/DISPLAYS.md#firmware-generate-build-and-flash)
- <a id="hardware-bring-up-checklist-waveshare-esp32-s3-touch-lcd-28c"></a>[Hardware bring-up checklist (Waveshare ESP32-S3-Touch-LCD-2.8C)](docs/DISPLAYS.md#hardware-bring-up-checklist-waveshare-esp32-s3-touch-lcd-28c)
- <a id="home-assistant-items"></a>[Home Assistant items](docs/DISPLAYS.md#home-assistant-items)
- <a id="icons"></a>[Icons](docs/DISPLAYS.md#icons)
- <a id="managing-displays"></a>[Managing displays](docs/DISPLAYS.md#managing-displays)
- <a id="mqtt-topics-and-freshness"></a>[MQTT topics and freshness](docs/DISPLAYS.md#mqtt-topics-and-freshness)
- <a id="notifications"></a>[Notifications](docs/DISPLAYS.md#notifications)
- <a id="one-command-application-updates"></a>[One-command application updates](docs/DEVELOPMENT.md#one-command-application-updates)
- <a id="optional-host-metrics"></a>[Optional host metrics](docs/BOAT_CHAT.md#optional-host-metrics)
- <a id="planned-limitations"></a>[Planned limitations](docs/DISPLAYS.md#planned-limitations)
- <a id="quick-start-for-an-internet-connected-pi"></a>[Quick start for an internet-connected Pi](#quick-start-one-command-on-the-raspberry-pi)
- <a id="selective-core-component-lifecycle"></a>[Selective core component lifecycle](docs/DEVELOPMENT.md#selective-core-component-lifecycle)
- <a id="settings-and-configuration-precedence"></a>[Settings and configuration precedence](docs/BOAT_CHAT.md#settings-and-configuration-precedence)
- <a id="signalk-and-influxdb"></a>[SignalK and InfluxDB](#signal-k-and-influxdb)
- <a id="signalk-not-running--eacces-on-packagejson"></a>[SignalK not running / EACCES on package.json](#signal-k-not-running--eacces-on-packagejson)
- <a id="supported-boards"></a>[Supported boards](docs/DISPLAYS.md#supported-boards)
- <a id="the-electrical-layout"></a>[The `electrical` layout](docs/DISPLAYS.md#the-electrical-layout)
- <a id="the-status-grid-layout"></a>[The `status-grid` layout](docs/DISPLAYS.md#the-status-grid-layout)
- <a id="the-tank-bars-and-gauge-arc-layouts"></a>[The `tank-bars` and `gauge-arc` layouts](docs/DISPLAYS.md#the-tank-bars-and-gauge-arc-layouts)
- <a id="thresholds-and-severity-colours"></a>[Thresholds and severity colours](docs/DISPLAYS.md#thresholds-and-severity-colours)
- <a id="touch-header-night-mode-and-dimming"></a>[Touch, header, night mode and dimming](docs/DISPLAYS.md#touch-header-night-mode-and-dimming)
- <a id="update-from-the-control-panel-057-lab-release"></a>[Update from the Control Panel (0.5.7 lab release)](#updating)
- <a id="version-10-release-scope"></a>[Version 1.0 release scope](docs/DEVELOPMENT.md#version-10-release-scope-historical)

## Development and release history

Historical release scope and review-fix notes are kept in
[Development: Historical notes](docs/DEVELOPMENT.md#historical-notes). Release notes are in `CHANGELOG.md`.

Contributors: CI runs ShellCheck 0.9.0 from the Ubuntu package at info severity; a clean run of a newer
local ShellCheck is not enough (see [Development](docs/DEVELOPMENT.md)).
