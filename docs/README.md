# VesselStack documentation

Use this page as the short map. VesselStack is self-hosted: your boat data and
credentials stay on the Pi unless you separately configure an integration or a
Boat Chat provider that sends data elsewhere.

## Start here

- [Getting started](GETTING_STARTED.md) — prerequisites, first boot, SSH access,
  local addresses, tokens, and the difference between a lab Pi and a boat Pi.
- [Hardware bill of materials](BOM.md) — the reference Pi, storage, power,
  enclosure, network, and isolated CAN choices.
- [End-to-end installation](INSTALLATION.md) — assemble, configure, verify, and
  recover a new installation.

## Choose a task

| I want to… | Read |
|---|---|
| Install on a new Pi | [BOM](BOM.md) → [Installation](INSTALLATION.md) |
| Install on an existing Pi | [Getting started](GETTING_STARTED.md) → [Installation](INSTALLATION.md) |
| Stage releases in a home lab | [Deployment](DEPLOYMENT.md) |
| Carry an update to an offline boat | [Deployment: offline path](DEPLOYMENT.md#simple-path-one-command) |
| Understand every setting and command | [README reference](../README.md#quick-start-one-command-on-the-raspberry-pi) |
| Connect selected readings to VesselTwin | [Optional VesselTwin connection](VESSELTWIN.md) |
| Develop, test, or cut a release | [Development](DEVELOPMENT.md) |
| Fix an unhealthy service | [README troubleshooting](../README.md#troubleshooting) and [Installation troubleshooting](INSTALLATION.md#troubleshooting-boat-chat-not-ready-memory-index-empty) |

## Access and privacy in one minute

From another computer on the boat LAN, use the addresses printed by the
installer for Grafana, Home Assistant, and Boat Chat. Retrieve the Boat Chat
token on the Pi when needed; do not paste it into chat, tickets, or the
repository. The administration control panel listens on loopback by default and
should be reached through an SSH tunnel:

```bash
ssh -L 8780:127.0.0.1:8780 YOUR_USER@PI
```

To deliberately allow panel access from a trusted LAN, preview then run
`sudo vesselstackctl panel-access lan`; this changes only the panel bind and
uses unencrypted HTTP on all interfaces. A strict firewall may still block it.
Restore the tunnel-only default with `sudo vesselstackctl panel-access local`.

An optional VesselTwin connection uses the separately installed
`signalk-vesseltwin` plugin, pairing, and explicit reading opt-ins. VesselStack
does not enable it for you. See the [VesselTwin integration guide](VESSELTWIN.md)
and [VesselTwin](https://www.vesseltwin.io/) for maintenance manuals and service
records. Review any external Boat Chat provider's network and retention terms
before enabling it.

## Troubleshooting

Start with read-only checks: `sudo vesselstackctl status`,
`sudo vesselstackctl doctor`, and `sudo vesselstackctl urls`, followed by the
service logs described in the [main reference](../README.md#troubleshooting).
If the Pi is offline, do not expect a new Docker image, Debian package, or
remote AI provider to be available; use the prepared offline bundle and the
deployment rollback instructions. Never troubleshoot by sharing tokens,
MMSIs, call signs, vessel positions, or complete logs publicly.

## Contributing

Read [Development](DEVELOPMENT.md) before changing scripts, services, or
documentation. Keep examples fictional, preserve the README reference, and
update the relevant documentation with every repository change. VesselStack is
licensed under the [Apache License 2.0](../LICENSE).
