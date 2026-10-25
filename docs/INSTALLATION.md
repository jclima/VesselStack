# End-to-end installation

This runbook takes a new Raspberry Pi from unopened hardware to a verified,
recoverable VesselStack installation. Budget a quiet half-day for the first
build; do not commission while underway.

## Quick path: one command

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
- an older `docker.list` (the manual steps of v1.4.0) or any other apt source that
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

Once the hardware is assembled, the operating system is installed (section 3) and
`can0` works if you use NMEA 2000 (section 4), paste this on the Raspberry Pi:

```bash
curl -fsSL https://github.com/jclima/VesselStack/releases/latest/download/get-vesselstack.sh | sudo bash
```

The `latest` URL works only once a release carrying `get-vesselstack.sh` is marked latest.
Pinned to one release (works for any published release, including a pre-release): `.../releases/download/v1.4.1/get-vesselstack.sh`, or add
`--version 1.4.1` (`| sudo bash -s -- --version 1.4.1`). It installs the Debian
prerequisites, asks before installing Docker Engine and Compose v2 from Docker's official apt
repository (see the box above), downloads and checksum-verifies the
release, opens the wizard (it prints the address to open in a browser on the Pi or
the exact `ssh -L` command for your laptop), then runs the installer dry run, the
installation, `vesselstackctl doctor` and `vesselstackctl urls`. It shows a list of
everything it changed at the end and is safe to re-run. Sections 5 and 6 below are
the same steps done by hand ("advanced / manual").

**What this trusts.** `curl | sudo bash` runs a script from GitHub as root, so you
trust GitHub, TLS and the VesselStack release. The tarball's `.sha256` comes from
the same release: it catches corrupt or truncated downloads, not a compromised
release. Compare the sha256 the script prints with the one on the GitHub release
page, or pass `--sha256 HASH`. To read the script first (recommended for the first
install):

```bash
curl -fsSLO https://github.com/jclima/VesselStack/releases/latest/download/get-vesselstack.sh
less get-vesselstack.sh
sudo bash get-vesselstack.sh
```

Useful options: `--yes` (no questions), `--no-wizard --config FILE`, `--dir DIR`,
`--wizard-port N`, `--offline --bundle DIR --tarball FILE` (no internet; see
[DEPLOYMENT.md](DEPLOYMENT.md)), `--help`. If VesselStack is already installed the
script reports an upgrade and offers `vesselstackctl update`; it never overwrites an
installation silently. Rollback: `sudo vesselstackctl stop`, and see the README
restore and uninstall sections; unpacked releases stay under
`/opt/vesselstack-release/` until you remove them.

## 1. Plan and buy

Use the [bill of materials](BOM.md). Before ordering the CAN interface, verify:

1. Raspberry Pi 5 support and a current 64-bit Raspberry Pi OS driver/overlay.
2. Galvanic isolation between CAN and the Pi.
3. Mechanical compatibility with the cooler, power hardware, and enclosure.
4. The vessel connector family and the correct drop cable/T-piece.
5. Whether the interface powers the Pi; design exactly one Pi power source.

Record the power circuit, fuse, device model, overlay lines, and rollback in the
boat's electrical documentation.

## 2. Assemble on the bench

Leave the NMEA backbone disconnected. Fit the Active Cooler, CAN interface, and
SSD according to their manufacturers. Check jumper voltage, oscillator, CAN
channel, and termination settings. The reference build uses the isolated NMEA
channel, 3.3 V logic where selectable, and termination **off** for a normal drop.

Use the official Pi supply during bench setup. Do not connect marine power at
the same time.

## 3. Install the operating system

With Raspberry Pi Imager, write the current 64-bit Raspberry Pi OS Lite to the
microSD. In Imager customization, set a unique hostname, non-default user,
strong password, timezone, SSH public key, and Wi-Fi only if needed. Boot with
Ethernet attached, then update once:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo reboot
```

After reconnecting, the one-line installer installs the prerequisites itself
(`ca-certificates curl jq openssl python3 python3-venv rsync tar gzip`, and Docker
Engine/Compose v2 after asking) and creates the service account. For the manual
path, install them yourself, with Docker Engine/Compose from Docker's official
Debian instructions:

```bash
sudo apt install -y ca-certificates curl git jq openssl python3 python3-venv rsync
docker compose version
```

Create the dedicated account before installer preflight (the one-line installer
does this for you after confirmation):

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin boat
id boat
```

## 4. Enable the CAN interface

Follow the exact overlay and jumper instructions for the selected interface;
VesselStack intentionally does not edit boot firmware. Reboot, then verify:

```bash
ip -details link show can0
```

If `can0` is absent, stop here. Inspect `dmesg`, `/boot/firmware/config.txt`, the
overlay name, oscillator frequency, SPI assignment, and HAT vendor guide. Do
not enable `SOCKETCAN_ENABLE` until this succeeds.

## 5. Download and run the wizard (advanced / manual)

Skip sections 5 and 6 if you used the one-line installer.

```bash
git clone https://github.com/jclima/VesselStack.git
cd VesselStack
git checkout v1.4.1
python3 vesselstack-wizard.py
```

On the Pi itself open the address the wizard prints (`http://127.0.0.1:8088/#token=...`; the token after `#` is required). For a headless Pi, keep the
wizard loopback-only and tunnel it from your workstation:

```bash
ssh -L 8088:127.0.0.1:8088 YOUR_USER@YOUR_PI
```

Then open the same printed address (with its `#token=...`) locally. The assistant inventories the host,
walks through the BOM and deployment choices, and writes `vesselstack.env` with
mode 600. Stop it with Ctrl-C when finished.

![Wizard system preflight](images/wizard-preflight.png)

![Wizard NMEA configuration](images/wizard-nmea.png)

## 6. Preflight, install, and start

Review the generated file without sharing it publicly:

```bash
sudo ./install.sh --config vesselstack.env --dry-run
sudo ./install.sh --config vesselstack.env
sudo ./install.sh --config vesselstack.env --start
sudo vesselstackctl doctor
sudo vesselstackctl urls
```

The dry run must pass before installation. If Docker access is denied, follow
the Docker post-install guidance or run the documented commands with `sudo`.

## 7. Connect and commission NMEA 2000

Power down the Pi and the NMEA backbone. Connect one drop cable to a T-piece on
the existing backbone. Confirm the backbone already has two end terminators and
the new interface's terminator is off. Restore backbone and Pi power, then:

```bash
ip -statistics link show can0
sudo vesselstackctl status
```

In Signal K, add an NMEA 2000/SocketCAN connection using `can0`. Confirm RX
packets increase and expected navigation values appear. A rising error or
dropped counter is a wiring, bitrate, termination, power, or interface problem;
disconnect the new drop and correct it before proceeding.

## 8. Configure integrations and acceptance tests

Follow the root README for Home Assistant, Signal K, AIS, Grafana, and Boat Chat.
Before relying on the system:

- Reboot and confirm `sudo vesselstackctl doctor` passes.
- Turn off upstream internet and confirm local dashboards remain available.
- Simulate only safe sensor conditions and verify alarms on every intended
  device. Never create a real flooding, over-temperature, or low-voltage event.
- Run and verify a backup to separate media:

```bash
sudo vesselstackctl backup
sudo vesselstackctl verify-backup /path/to/archive.tar.gz
```

- Record the Pi hostname/IP, fuse location, power isolation point, CAN adapter,
  connector, installed version, backup location, and recovery owner.

## Rollback and recovery

Before NMEA commissioning, rollback is simply: power down, disconnect the new
drop at its T-piece, and restore backbone power. For software rollback, stop the
stack with `sudo vesselstackctl stop`; preserve `/opt/vesselstack-data` and the
latest verified backup. See the README's update, restore, and uninstall sections
before changing or removing persistent data.
