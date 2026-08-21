# Security Policy

## Supported versions

Security fixes are provided for the latest published release and the default
branch. Pre-release builds may change without compatibility guarantees.

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
instruments, alarms, and safe operating procedures.

## Administrative surface

The VesselStack Control Panel runs as root so it can coordinate Docker,
systemd, installation, backup, and hardware operations. Its default listener is
loopback-only and every API except `/health` requires the generated control-
panel token. Do not publish it directly to the internet. Use an SSH tunnel or a
trusted encrypted VPN, protect the token as a root credential, and keep the
`vesselstackctl` CLI available as the recovery path.
