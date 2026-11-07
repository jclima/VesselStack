# Control Panel component lifecycle lab test

This is the release 0.6.2 lab recipe. It is a bounded acceptance test, not
evidence of a boat or physical Raspberry Pi test. Use a scratch lab Pi or an
isolated VM with Docker and a mounted data disk. Keep real vessel identity,
positions, MMSIs, tokens, logs, and backups out of this repository.

## Fetch and verify the pinned prerelease

Download the pinned bootstrap and the CI-generated checksum from the same
`v0.6.2` GitHub release. Verify before reading or running it:

```bash
curl -fsSLO https://github.com/jclima/VesselStack/releases/download/v0.6.2/get-vesselstack.sh
curl -fsSLO https://github.com/jclima/VesselStack/releases/download/v0.6.2/get-vesselstack.sh.sha256
sha256sum -c get-vesselstack.sh.sha256
less get-vesselstack.sh
sudo bash get-vesselstack.sh --help
```

0.6.2 release status: `v0.6.2` is an SSH-signed tag, published first as a pre-release and
marked Latest after the maintainer's physical Raspberry Pi install passes (see the CHANGELOG).
Check the tag with `scripts/verify-release-tag.sh v0.6.2`. The checksum catches a corrupt or
truncated asset; it does not replace that signature check.

## Exercise the browser updater

Open the authenticated panel's **Updates** tab and press **Check for updates**. Before
you press it, confirm the tab says nothing has been contacted. Confirm the catalog
is `jclima/VesselStack`, stable releases are the default, and **Include early test
releases** is opt-in. When the installed `0.6.2` release is current, the panel
correctly reports "You're up to date"; do not downgrade, retag, or force the
same version. Exercise **Back up & install** only when a newer public release
is available. A fresh `0.6.2` install is a CLI install; an existing `0.5.5`,
`0.5.6` or `0.5.7` install may use the panel upgrade path, after enabling
**Include early test releases** (named **Include lab prereleases** in the 0.5.x panel) if
0.6.2 is published as a prerelease. CI-controlled fixtures exercise the install
orchestration, and the actual systemd restart is covered by the backend
verifier. Full physical-Pi upgrade proof remains pending physical testing.
Mount the configured external backup device first. Confirm the dialog and the Home tab
show the backup state, and that the operation log records download,
SHA-256 verification, safe extraction, preflight, backup verification, install,
detached updater restart of the Control Panel, reconnect, and final version/boot
verification.

Acceptance cases:

- With the backup path unmounted and no choice made, `--require-mounted` must refuse the update
  even if `ALLOW_UNMOUNTED=true`; no install may start. The failure keeps the
  path and recovery help visible. From the browser the same press shows the backup dialog instead
  of starting; verify each choice on a disposable Pi (an upgrade started from 0.6.2 or later; the
  upgrade that delivers 0.6.2 uses the old worker and cannot show it):
  - the dialog lists a spare ext4 USB drive by label, size and filesystem, lists a vfat drive and a
    desktop-mounted drive as refused with the reason, and never shows a device path as a choice;
  - mounting the listed drive makes `findmnt /media/<folder>` show `nodev,nosuid,noexec`, the
    backup lands there, and `/etc/fstab` and `vesselstack.env` are byte-identical afterwards;
  - **system disk this once** puts the archive in `/var/backups/vesselstack`, leaves the
    `/media/<folder>` mount point empty, and the next upgrade asks again;
  - **Install without a backup** stays disabled until the box is ticked, makes no archive, and the
    banner says no backup was made;
  - with `VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=false` in `vesselstack.env`, the system-disk and
    no-backup choices are absent and a hand-made request for them is refused (HTTP 400);
  - the same with the variable only in the panel's process environment, and with it added after the
    request was accepted but before the worker reaches the backup step (the worker refuses);
  - a hand-made `/api/action` request with a `backup` field for any action other than `update` or
    `update-release` is refused (HTTP 400); `backup_mode` / `backup_skipped` appear in the operation
    record for display only and `vesselstack.env` stays unchanged;
  - mount propagation: after the mount, `findmnt` in a normal root shell (not the panel's
    namespace) shows the drive for both **Back up & install** and **update from folder**.
- Use a checksum-mismatch fixture and an unsafe-archive fixture; both must stop
  before preflight/install. Do not weaken the live release checksum check or
  hack the production endpoint.
- Refresh during an in-progress operation and reconnect the browser. Status and
  the bounded log must be durable; the authenticated token must remain valid
  without a second login.
- Complete one successful update and confirm the Control Panel comes back on the
  new version/boot and the same token. If the listen host or port changes,
  confirm the old tab cannot reconnect and the UI opens the new URL with that
  session's token.
- Use the standalone **Restart Control Panel** button and confirm the panel
  restarts independently; unrelated service bindings remain unchanged.
- Confirm failed-backup help names `vesselstackctl verify-backup` and
  `vesselstackctl restore`; the UI makes no automatic-rollback promise.

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

To see the **To fix:** guidance in the Control Panel, unmount the backup
target (or use a path that is not a mountpoint), then choose **Back up now** on
Home and, separately, run an update from the Maintenance tab. Each stops with a
red result banner that gives the reason, then a **To fix:** line naming the mount
point (the update version also shows the one-off
`VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=true` command in monospace; a Control Panel upgrade shows the
backup dialog described above instead). Confirm
nothing was stopped or changed, that no prompt waited in the Details log, mount
the drive, and run the operation again to see it succeed. Setting an unsupported
value such as `BOAT_UNITS=imperial` in the settings file and starting an update
shows the same kind of line with the exact `sed` command.

If the backup media is intentionally unavailable in this disposable lab, the
one-shot exception is explicit and temporary:

```bash
sudo VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=true bash get-vesselstack.sh --yes \
  --set BOAT_NAME=Lab --set SOCKETCAN_ENABLE=false
```

This does not make an unmounted backup safe, and the exception must never be
used to guess at a real boat's backup destination. Stop if manually checked
capacity is low or if any device/data path is unclear.

## Exercise the interface

With the stack running, walk each tab once and record anything that differs:

1. Unlock with the pasted `CONTROL_PANEL_TOKEN=...` line, then lock and unlock with the
   bare value. A wrong token shows "That token wasn't accepted".
2. **Home** shows Services, Backup and Software cells and one **Back up now** button.
   Press it: a banner reports the result and the saved path, and **Details** shows the
   log. Unmount the backup drive and repeat: the banner fails with a mount message and the
   Backup cell says the drive is not mounted.
3. **Stop all** (Maintenance) opens a dialog listing what goes dark; **Cancel** changes
   nothing. Confirm it, check the panel stays up, then **Start all**.
4. **Settings**: enter an unknown timezone and an out-of-range number. **Save** is
   rejected with an inline message per field and nothing is written. Fix them, **Save**
   (nothing restarts), then **Apply**.
5. **Maintenance → Restart Control Panel**: the header shows Disconnected, then reconnects
   by itself, and boat services keep running. Close the SSH tunnel to see Disconnected
   and **Retry now**.
6. Resize to phone width and switch to dark mode; every tab stays usable.

## Exercise the catalog

Run `sudo vesselstackctl panel-access lan` on the Pi and open its printed address
from your laptop on the same trusted network. Retrieve the unlock token with the
printed command. No SSH tunnel is needed. Confirm `panel-access` shows the chosen
host/port and that the other service bindings are unchanged. Use
`sudo vesselstackctl panel-access local` to undo this after testing if desired.
Do not hardcode a preview port. On the Home tab's component grid:

1. Confirm core components show a **⋯** menu with **Disable**, and that optional
   components show **Turn on in Settings** instead.
2. Disable Grafana from its **⋯** menu. Confirm the operation stops/removes its container while its
   data directory and dashboards remain present. Do not run `rm -v`, purge
   commands, or unrelated control-system cleanup.
3. Use **Restart all** and run `sudo vesselstackctl doctor`; the disabled
   component must remain disabled.
4. Run the normal update path from the pinned release directory. Confirm the
   catalog membership remains unchanged across the update and that existing
   user data is retained.
5. Press **Enable** on the Grafana card, or run
   `sudo vesselstackctl component add grafana`. Confirm Grafana is provisioned
   and its retained data is visible again.
6. Repeat one add/remove check for another core component. If the component
   has an InfluxDB or MQTT integration, record that removal may degrade the
   integration; it must not silently auto-configure unrelated services.

The six core catalog entries are Home Assistant, InfluxDB, Grafana, Prometheus,
Mosquitto, and Homepage. Hardware and systemd services remain
configuration-driven. A passing lab result does not claim physical Pi,
hardware, or production evidence.

## Exercise display editing and Apply (lab, with hardware)

Needs a display added with `display add`, Wi-Fi stored (`display wifi`), Docker and about 3 GB free
disk. Wi-Fi (OTA) updates are not yet verified on hardware: record what actually happens.

1. Displays tab: **Edit pages and theme**, pick the `daylight` swatch, add a value with **Add to page**
   (try a full page and an incompatible layout to see the disabled reasons), move and remove an item.
   **Save**; check `sudo vesselstackctl display history` lists a new backup and `displays.json` has the edit.
   Edit `displays.json` in a shell while the draft is open and **Save**: expect the conflict offer.
2. **Apply to screen → By USB cable** with the display plugged in: the stepper walks Preparing, Building,
   Sending, Done; during the run Maintenance buttons and Save are refused (409); the display restarts
   with the new theme and page. `build/displays/ID/last-flash.json` exists with mode 0600.
3. Repeat Apply with no change: Building is skipped ("already up to date").
4. **Apply to screen → Over Wi-Fi** with the display's IP (for example `192.0.2.50`). Also try a wrong
   address and a powered-off display: the **To fix:** banner appears. Record whether OTA works.
5. History → **Restore** the older backup (the settings that were current before the last Apply). Only now
   does **Put previous firmware back on screen** appear on the card (before the restore it is hidden and
   a hint explains why). Press it; confirm the display shows the old pages and no Wi-Fi password appears
   in the Details output (`[redacted]`).
6. `display deploy` on the command line, USB first, then Wi-Fi:
   `sudo vesselstackctl display deploy helm --serial /dev/serial/by-id/usb-... --progress`, then change the
   theme and run `sudo vesselstackctl display deploy helm --ota 192.0.2.50 --progress`. Check
   `last-flash.json` (mode 0600) after each and that a failed upload (unplug the display) leaves it unchanged.
7. Wi-Fi address confirmation: in Apply → Over Wi-Fi the last address is prefilled and Start is enabled; type a
   different address (for example `192.0.2.51`): a checkbox "Send firmware, which contains this boat's Wi-Fi
   password, to 192.0.2.51" appears and Start stays disabled until ticked. A direct `POST /api/displays/deploy`
   with a new address and no `confirm_new_target` returns 400.
8. Lifecycle slot: while an Apply runs, start a backup or an update from Maintenance and Updates and expect
   them to be refused as busy; try Save and Restore too (409).
9. Downgrade note: before installing an older VesselStack run `sudo vesselstackctl display set-theme helm classic`
   for every themed display (older versions reject the `theme` key and the bridge exits).
