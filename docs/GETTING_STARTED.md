# Getting started

This is the owner-friendly starting point for an existing Raspberry Pi or a
new build. For a complete hardware-to-commissioning run, use
[INSTALLATION.md](INSTALLATION.md).

## Before you begin

- A Raspberry Pi 4 or 5 running 64-bit Raspberry Pi OS (4 GB RAM minimum,
  8 GB recommended), or a supported Debian/Ubuntu host. On Raspberry Pi the
  installer expects `arm64`; a supported non-Pi host uses its native supported
  architecture. 32-bit Raspberry Pi OS is refused.
- A stable network connection for the first online install, preferably
  Ethernet, and a laptop or desktop with an SSH client.
- Storage with room for the operating system, containers, history, backups, and
  updates. An SSD is strongly preferred for time-series data.
- If using NMEA 2000, an isolated, compatible SocketCAN interface, a powered
  and terminated backbone, and the vendor's Pi overlay configured before
  enabling CAN. VesselStack does not edit boot firmware or turn hardware on.
- A quiet commissioning window. Do not install or validate while underway, and
  do not use VesselStack as your only alarm or navigation system.

For a new Pi, start with the [BOM](BOM.md). For an existing Pi, check the
operating system, architecture, free space, Docker, and Compose before making
changes:

```bash
cat /etc/os-release
uname -m
dpkg --print-architecture
df -h /
docker --version
docker compose version
```

## First boot: online install

On an internet-connected Pi, the normal path is one SSH session and one guided
installer. `-t` gives the session a terminal so the installer can ask questions:

```bash
ssh -t YOUR_USER@raspberrypi.local
curl -fsSL https://github.com/jclima/VesselStack/releases/latest/download/get-vesselstack.sh | sudo bash
```

The script checks the host first. It asks before installing missing Debian
packages or Docker Engine and Compose v2 from Docker's official apt repository,
then downloads and checksum-verifies VesselStack, asks configuration questions,
and shows a review before starting. It needs `sudo` because it changes system
packages, services, and protected directories. Re-running an existing install
offers the supported update flow; it does not silently overwrite it.

The terminal wizard explains defaults and keeps secrets out of the review. Boat
name, units, timezone, Signal K, CAN, firewall, retention, network reachability,
and the optional Boat Chat provider are configured there. Boat Chat defaults to
a local provider. A remote provider is optional and may require internet and
provider credentials.

![Example VesselStack preflight review](images/wizard-preflight.png)

*Example of a preflight review; your host checks and values may differ.*

## How to access it

At the end, the installer prints the LAN addresses for Grafana, Home Assistant,
and Boat Chat. Open those addresses from a computer on the same trusted boat
network. It also prints a command to read the Boat Chat access token locally;
keep that token private and never commit `config/*.env`.

The control panel is administration-only and binds to loopback by default. On
the Pi, retrieve its token locally:

```bash
sudo grep '^CONTROL_PANEL_TOKEN=' /opt/vesselstack/config/control-panel.env
```

Keep the token private; paste it only into the unlock screen reached through an
SSH tunnel from your own computer:

```bash
ssh -L 8780:127.0.0.1:8780 YOUR_USER@PI
```

Then open `http://127.0.0.1:8780` and enter the token. Do not expose port 8780 directly to the
internet. Exact ports, LAN presets, firewall behavior, token storage, and
rollback are documented in the [main README reference](../README.md#quick-start-one-command-on-the-raspberry-pi).

For a deliberate trusted-LAN setup, preview and apply only the panel listener:
`sudo vesselstackctl panel-access lan --dry-run`, then
`sudo vesselstackctl panel-access lan`. This is unencrypted HTTP on all
interfaces; a strict firewall may still block it. Roll back with
`sudo vesselstackctl panel-access local`. The command prints the token retrieval
command but never prints the token itself.

## Lab Pi versus boat Pi

Use an internet-connected home-lab Pi to stage and soak a release, test backup
and restore, and rehearse an update. A boat Pi can run without internet after
you carry in the release tarball, checksum, and matching offline bundle. Docker
and required Debian packages must be installed while the Pi is still online;
the offline installer cannot fetch them. Before leaving the lab, verify that
the matching offline bundle contains every required Python dependency and
container image.

Follow [DEPLOYMENT.md](DEPLOYMENT.md) for the signed release workflow, offline
bundle verification, dock-side update, and rollback. Internet is not required
for the local Signal K/history/dashboard stack once its images and packages
are present. It may still be required by an explicitly selected remote Boat
Chat provider or another separately configured integration.

## Keep data private

VesselStack is local-first but not automatically isolated from every network:
choose LAN access deliberately, use the firewall guidance, protect SSH, and
keep generated tokens per host. Do not copy lab secrets to the boat. Do not
publish vessel positions, MMSIs, call signs, device identifiers, telemetry
databases, backups, logs, or environment files.

An optional VesselTwin connection uses the separately installed
`signalk-vesseltwin` plugin, pairing, and explicit reading opt-ins. VesselStack
does not enable it for you. Consult the [VesselTwin integration guide](VESSELTWIN.md)
and [VesselTwin](https://www.vesseltwin.io/) for maintenance manuals and service
records.

## If something goes wrong

Start with the read-only checks in [Troubleshooting](README.md#troubleshooting):
`sudo vesselstackctl status`, `sudo vesselstackctl doctor`, and `sudo vesselstackctl urls`,
then inspect the relevant service logs. Stop before changing CAN or boot settings if `can0` is
missing. For release failures, use the deployment backup and rollback path;
do not delete `/opt/vesselstack-data` while investigating.

For deeper manual setup, every variable, port, service, security note, and
rollback command remains in the [full README reference](../README.md#quick-start-one-command-on-the-raspberry-pi).
