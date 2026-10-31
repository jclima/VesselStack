# Control Panel component lifecycle lab test

This is the release 0.5.1 lab recipe. It is a bounded acceptance test, not
evidence of a boat or physical Raspberry Pi test. Use a scratch lab Pi or an
isolated VM with Docker and a mounted data disk. Keep real vessel identity,
positions, MMSIs, tokens, logs, and backups out of this repository.

## Fetch and verify the pinned prerelease

Download the pinned bootstrap and the CI-generated checksum from the same
`v0.5.1` GitHub prerelease. Verify before reading or running it:

```bash
curl -fsSLO https://github.com/jclima/VesselStack/releases/download/v0.5.1/get-vesselstack.sh
curl -fsSLO https://github.com/jclima/VesselStack/releases/download/v0.5.1/get-vesselstack.sh.sha256
sha256sum -c get-vesselstack.sh.sha256
less get-vesselstack.sh
sudo bash get-vesselstack.sh --help
```

The release is an annotated unsigned lab tag, not a signed stable release. The
checksum catches a corrupt or truncated asset; it does not replace review of
the release provenance.

## Install and check the backup mount guard

Install with synthetic values and a separate backup target mounted at a path
that is expected to be a mountpoint. Before allowing a mutating install or
update, confirm the backup target is actually mounted and is not merely an
empty directory on the system disk:

```bash
mountpoint /media/vesselstack-backup
findmnt --mountpoint /media/vesselstack-backup
df -h /media/vesselstack-backup /opt
du -sh /media/vesselstack-backup
sudo vesselstackctl doctor
```

Use the real backup mount path in place of `/media/vesselstack-backup`. A
backup directory on the same system disk does not protect against disk failure;
check available capacity manually before proceeding.

If the backup media is intentionally unavailable in this disposable lab, the
one-shot exception is explicit and temporary:

```bash
sudo VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=true bash get-vesselstack.sh --yes \
  --set BOAT_NAME=Lab --set SOCKETCAN_ENABLE=false
```

This does not make an unmounted backup safe, and the exception must never be
used to guess at a real boat's backup destination. Stop if manually checked
capacity is low or if any device/data path is unclear.

## Exercise the catalog

Reach the real Control Panel through its loopback SSH tunnel (`8780`); do not
hardcode a preview port. In the Components view:

1. Confirm the component membership summary is read-only and that the CSV
   membership field cannot be edited manually.
2. Remove Grafana. Confirm the operation stops/removes its container while its
   data directory and dashboards remain present. Do not run `rm -v`, purge
   commands, or unrelated control-system cleanup.
3. Restart the stack and run `sudo vesselstackctl doctor`; the removed
   membership must remain removed.
4. Run the normal update path from the pinned release directory. Confirm the
   catalog membership remains unchanged across the update and that existing
   user data is retained.
5. Add Grafana again from the Control Panel, or with
   `sudo vesselstackctl component add grafana`. Confirm Grafana is provisioned
   and its retained data is visible again.
6. Repeat one add/remove check for another core component. If the component
   has an InfluxDB or MQTT integration, record that removal may degrade the
   integration; it must not silently auto-configure unrelated services.

The six core catalog entries are Home Assistant, InfluxDB, Grafana, Prometheus,
Mosquitto, and Heimdall. Hardware and systemd services remain
configuration-driven. A passing lab result does not claim physical Pi,
hardware, or production evidence.
