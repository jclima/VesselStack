# Security policy

## Supported versions

Security fixes are provided for the newest published release (the highest version
number, whether or not GitHub marks it Latest or as a pre-release) and for the default
branch. Older releases, including 0.4.x, receive no fixes: update to the newest release.
Pre-release builds may change without compatibility guarantees.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting feature for this repository. If
that feature is unavailable, contact the repository owner privately through
their GitHub profile before disclosing details.

Do not open a public issue containing credentials, API tokens, vessel
positions, remote-access details, or instructions that expose a running boat.
Include the affected version, component, reproduction steps, impact, and any
suggested mitigation. You should receive an acknowledgement within seven days.

## Operational scope

VesselStack is not certified navigation, collision-avoidance, engine-control,
alarm, or life-safety equipment. Operators must retain independent marine
instruments, alarms, and safe operating procedures. Never depend on this
software as the sole source of safety data.

## Administrative surface

The VesselStack Control Panel runs as root so it can coordinate Docker,
systemd, installation, backup, and hardware operations. Its default listener is
loopback-only and every API except `/health` requires the generated control-
panel token. Do not publish it directly to the internet. Use an SSH tunnel or a
trusted encrypted VPN, protect the token as a root credential, and keep the
`vesselstackctl` CLI available as the recovery path.

Remote access with Tailscale is off by default. When you enable it, the Control Panel is
published to your tailnet only and the panel token is still required. Tailscale gives every
device in your tailnet a route to the boat computer, so services bound to all interfaces are
reachable too: limit who can reach the boat with Tailscale ACLs. The Node-RED editor (an
optional feature, off by default) requires sign-in and can run code, so keep it on a trusted
LAN or reach it through Tailscale or an SSH tunnel. The Telegram bot token used by Boat Chat
and alerts is a credential stored on the Pi. The `*.pre-restore-*` and
`*.failed-restore-*` folders left by a restore hold a copy of your data including secrets and are
not removed automatically: delete them yourself once the restored system is verified. The restore
journal under `/var/lib/vesselstack/restore/current` is readable by root only and holds a snapshot
of system files while a restore is unfinished: do not delete it by hand, finish it with its
`recover` command (see [If a restore is interrupted](README.md#if-a-restore-is-interrupted)). At
boot, the root unit `boot-vesselstack-restore.service` runs that journal's own `recover` command
after an interrupted restore; a restore runs as the root unit `run-vesselstack-restore`.
`/etc/vesselstack-recovery.conf` is read as text and never run.
