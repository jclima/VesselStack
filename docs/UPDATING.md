# Updating and offline install

Purpose: update a running VesselStack with a reviewed release, roll back, and install on a boat with no internet using an offline bundle. Back up first: see [Backups](../README.md#backups).

[Back to README](../README.md) · [Documentation index](README.md)

Back up first (see [Backups](../README.md#backups)); updating and rolling back are summarised
in the README under [Updating](../README.md#updating). If you are on 0.6.x or older, read
[Upgrading from 0.6.x or older](#upgrading-from-06x-or-older) first.

## Upgrading to 0.9.0

0.9.1 is a pre-release. Physical Raspberry Pi and display acceptance is still pending, so it is not the
release marked Latest. A boat should stay on 0.8.0 until 0.9.1 is promoted.

0.9.1 is the release to install and update to. 0.9.0 was published as a pre-release on 2026-11-16
and cannot be installed with Docker Compose older than 2.35.

- 0.9.0 itself fails the installer's Compose check on Docker Compose older than 2.35, including
  Debian's packaged 2.26.1, with no reason shown. Nothing is changed. Use 0.9.1. Run
  `docker compose version` to see what you have. 0.9.1 is checked with real Compose from 2.10.2
  through the current release; with an older one it prints a warning and continues, and the template
  check prints the reason if that Compose cannot read the template. From 0.9.0 there is nothing
  extra to do.
- Run the full installer (the pinned installer below) or `sudo vesselstackctl update <release dir>`.
  An application-only update is not enough, because 0.9.0 and 0.9.1 add settings and units.
- On a Raspberry Pi the hardware watchdog is armed by default (`VESSELSTACK_WATCHDOG=auto`). Set
  `VESSELSTACK_WATCHDOG="false"` and rerun the installer to opt out.
- While a restore is unfinished (a pending restore journal), commands that change the system and
  the installer refuse to run. Run `sudo /var/lib/vesselstack/restore/current/recover status`, then
  `recover rollback` if you need to go back. See
  [If a restore is interrupted](../README.md#if-a-restore-is-interrupted).
- `vesselstackctl status` exits non-zero while an installation or restore is unfinished. The Control
  Panel, a browser-started update and the boat profile command also report that VesselStack is busy
  then.
- A restore now runs in the background unit `run-vesselstack-restore`: a closed terminal does not
  interrupt it, and `sudo vesselstackctl restore --status --follow` re-attaches. After a power loss
  during a restore, the next boot rolls it back (`boot-vesselstack-restore.service`, installed and
  enabled by the installer). Set `VESSELSTACK_RESTORE_AT_BOOT="hold"` to opt out, or use
  `--foreground`. Services still start before that recovery stops them. See
  [Recovery at boot](../README.md#recovery-at-boot).
- Signal K and AIS-catcher may move to this release's versions at the first update. An older
  installer wrote their versions into your settings file. The first update to 0.9.0 clears a
  `SIGNALK_VERSION` or `AIS_IMAGE` that it wrote, so both follow the release from now on. A value
  you chose is kept. To keep a specific version, set it in the settings file and rerun the
  installer. See [Component versions](../README.md#component-versions).
- Displays need a rebuild and reflash (`sudo vesselstackctl display build ID`, then `display flash
  ID`) to get the new alarm banner. The new firmware needs this release's bridge. To go back, reflash
  with `sudo vesselstackctl display flash ID --previous`. See
  [Instrument displays (preview)](../README.md#instrument-displays-preview).

All upgrade notes, the list of settings to turn each new feature off, and rollback are in
[Upgrading to 0.9.0](../README.md#upgrading-to-090) and in the `0.9.0` section of
[CHANGELOG.md](../CHANGELOG.md).

## From the pinned installer

Re-run the pinned release installer over `ssh -t`. It finds the installed system and offers
`vesselstackctl update <release dir>`, which runs preflight, makes a verified backup and then
installs and starts the release:

```bash
curl -fsSL https://github.com/jclima/VesselStack/releases/download/v0.9.1/get-vesselstack.sh | sudo bash
```

Running it again for the version that is already installed reports "nothing to do". If the
previous run did not finish (for example the network dropped while images were downloading),
the same command continues it with the saved settings instead: the questions are not asked
again and no new backup is made. After a failed update the message names the backup to restore
(`sudo vesselstackctl restore FILE --yes`).

The command itself needs the unpacked release directory, so plain `vesselstackctl update`
with no argument fails. To run it by hand once a release is unpacked (the installer unpacks to
`/opt/vesselstack-release/<version>`):

```bash
sudo vesselstackctl update /opt/vesselstack-release/0.9.1
```

If the installer fails part-way, `vesselstackctl update` stops with three lines: that the update
to the new version did not finish and your data is kept, the command to try again (for example
once the network is back), and the exact `sudo vesselstackctl restore ARCHIVE --yes` command for
the backup it just made. If no backup was made (a skipped backup), it says to restore the newest
`vesselstack-*.tar.gz` in your backup folder instead.

From a terminal (`ssh -t`) the installer now offers to fix an unsupported `BOAT_UNITS` value or
a setting that cannot be written safely at a prompt instead of stopping (choose, or `c` to
cancel with nothing changed). A root-only copy of the previous settings file is kept as
`vesselstack.env.before-fix-<time>` next to it; delete these copies once the update works. Runs
without a terminal (the Control Panel, `--yes`, `VESSELSTACK_NONINTERACTIVE=true`) still stop
before changing anything and print the exact fix. Details: [Upgrading from 0.6.x or older](#upgrading-from-06x-or-older).

Updating to 0.6.3 from an older version can end with a shell error after the install has
completed. See [The 0.6.3 shell error](#updating-to-063-can-end-with-a-shell-error-known-issue).

## From the Control Panel

Open the **Updates** tab and press **Check for updates**. This is the only thing that
contacts `github.com`; nothing about your boat is sent. A newer release is listed with a
link to its notes and a **Back up & install** button. Releases that GitHub publishes as
pre-releases appear only after you turn on **Include early test releases** (0.5.x panels
call the same switch "Include lab prereleases"). **Back up & install** downloads the
archive, checks its size and SHA-256 checksum, runs the installer's `--dry-run` check,
takes a backup with `--require-mounted` (so the update stops if the external backup drive
under `/media` or `/mnt` is not mounted, unless you choose in the dialog to mount it, back up to the
system disk once or install without a backup), installs, restarts the Control Panel and waits
for it to report the new version. The page reconnects by itself. See
[Control Panel](CONTROL_PANEL.md#updates).

If the Control Panel is unavailable, use the pinned installer above.

## When the backup drive is not mounted

The pinned installer and (from an installed 0.6.1) `vesselstackctl update` and
`vesselstackctl backup` ask, before anything is stopped, whether to mount the drive from
`/etc/fstab`, pick a connected USB drive (ext4 recommended, exFAT allowed, vfat refused), check
again, or back up to the system disk once. Cancelling in the pinned installer prints
`Cancelled. The update was not started; your installed VesselStack was not changed.` (the packages
and the release folder may already be in place). A drive mounted from the menu must have a
root-owned top folder that others cannot write to, with no symbolic link or foreign-owned
folder on the way to the backup folder; otherwise the backup is refused. Without a terminal, with `--yes`, or in the Control
Panel they stop and print a `To fix:` line instead. A direct `sudo vesselstackctl update <dir>` from
an installed 0.6.0 or earlier controller has no menu: use the pinned installer, mount the drive
first, or run `sudo env VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=true vesselstackctl update <dir>`.
See [Backups](../README.md#backups).

From 0.6.2 the Control Panel's **Back up & install** and **update from folder** show a dialog for an
unmounted backup drive (mount from `/etc/fstab`, mount a listed USB drive, back up to the system disk
once, use a different folder, or install without a backup). This cannot help the upgrade that delivers it: the Control Panel worker uses the controller and panel that are already installed, so for the upgrade to 0.6.2 itself mount the drive by hand or run the update from a terminal. Setting
`VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=false` (in `vesselstack.env` or the environment) turns off the system-disk and no-backup choices; the choice is never saved. In the terminal
menu, "back up to the system disk this once" also now writes to `/var/backups/vesselstack`, leaving the
unmounted mount folder empty.

## Upgrading from 0.6.x or older

Use this section if you installed 0.6.4 or any older release, and you are updating to 0.7.0 or
later. Find the line that matches your case. Update with a backup on a mounted external drive, not
while underway. The pinned installer and `vesselstackctl update` make a verified backup first
(see [From the pinned installer](#from-the-pinned-installer)).

- Your `vesselstack.env` says `BOAT_UNITS=nautical`: see [Settings that say `BOAT_UNITS=nautical`](#settings-that-say-boat_unitsnautical).
- The installer stopped on a setting it will not write: see [A value the installer refuses to write](#a-value-the-installer-refuses-to-write).
- The backup drive is not mounted: see [When the backup drive is not mounted](#when-the-backup-drive-is-not-mounted), and [Backup drive notes from 0.6.1 and 0.6.2](#backup-drive-notes-from-061-and-062).
- You use instrument displays and may go below 0.6.2: see [Instrument displays (0.6.2)](#instrument-displays-062).
- You are on 0.6.2 or older and have Heimdall: see [Heimdall to Homepage (0.6.3)](#heimdall-to-homepage-063).
- An update to 0.6.3 ended with a shell error: see [The 0.6.3 shell error](#updating-to-063-can-end-with-a-shell-error-known-issue).
- You want the list of changes for an existing boat: see [What changes for an existing boat](#what-changes-for-an-existing-boat).
- You are on 0.4.x: see [Coming from 0.4.x](#coming-from-04x).

### Settings that say `BOAT_UNITS=nautical`

Older setup wizards offered "nautical" as a units choice, and installers before 0.5.7 did not check
it, so such a `vesselstack.env` may exist. 0.6.0 refused it with
`BOAT_UNITS must be us_customary or metric` and stopped the update before changing anything.

From 0.6.1 the installer reads `nautical` as `metric` (metres for depth; knots and nautical miles
are kept in both systems) and prints one `Note:` line. A real install or update rewrites
`vesselstack.env` with `BOAT_UNITS=metric`; a dry run only prints the note. The setup wizard and the
Control Panel also show it as Metric without changing it. Any other unsupported value is still
refused before anything is changed, now with a message that names the file and the fix.

If you are stuck on 0.6.0, change the value by hand and run the update again:

```bash
sudo sed -i 's/^BOAT_UNITS=.*/BOAT_UNITS=metric/' /opt/vesselstack/config/vesselstack.env
```

Rollback: set `BOAT_UNITS` back to `us_customary` (or `metric`) in the same file. 0.6.1 needs no
other manual step.

### A value the installer refuses to write

**Fix it at the prompt (0.6.1).** When you run the installer, an update or the pinned
`get-vesselstack.sh` from a terminal (for example over `ssh -t`), an unsupported `BOAT_UNITS` value,
or a setting in `vesselstack.env` that cannot be written safely, no longer just stops the run. The
installer asks what to do before changing anything else:

```text
BOAT_UNITS in /opt/vesselstack/config/vesselstack.env is "imperial", which is not supported.
  [1] US customary (feet, °F, psi, US gallons)
  [2] Metric (metres, °C, kPa, litres)
  [c] Cancel (nothing is changed)
Choice:
```

An unsafe value (a token or other setting with an apostrophe plus a backslash, double quote, dollar
sign or backtick, a line break, or a control character) is offered as `[1] Enter a new value for <KEY>`.
What you type for a token, password or key is hidden and is never printed or logged, and a
replacement that is still unsafe is refused. Your answer is saved in the settings file, the installer
prints `Changed <KEY> in <file> (previous copy: <file>.before-fix-<time>)` and starts over with the
same options. The previous copy is root-only; delete the `*.before-fix-*` copies once the install
works. `c`, an empty answer at the end of input, or five minutes without an answer prints
`Cancelled. Nothing was changed.` and leaves the file as it was.

Limits:

- Only settings written in the file you pass with `--config` are offered. A value that comes from
  the environment is not.
- Generated or preserved passwords and tokens (InfluxDB, Grafana, MQTT, Boat Chat, Control Panel)
  are never offered, because the running services still hold the old value. Change it in the
  service first, then edit the file.
- A symbolic link, a file not owned by root, a file in a folder (or under a parent folder) that
  other users can change, or a file whose edit cannot be verified is left untouched.
- For an unsupported units value the manual `sed` command above is printed. For an unsafe value you
  are told to edit the setting by hand.
- Runs without a terminal (cron, a service, the Control Panel, `--yes`,
  `VESSELSTACK_NONINTERACTIVE=true`) never ask. They stop before changing anything and print the
  exact fix, as in 0.6.0.
- A dry run that you answer does save the fix you chose.

**Env-file values are checked before anything changes.** `sudo ./install.sh --config FILE --dry-run`,
and therefore `vesselstackctl update` and the Control Panel updater (both run a dry run first),
refuse an unsafe value before the backup is taken, services are stopped or any file is touched. The
installer lists each offending setting by key name only (never the value), says why, and exits with
an error:

```text
  BOAT_NAME: a single quote combined with a backslash, double quote, dollar sign or backtick
Refusing to continue: the settings above cannot be written safely to the environment files.
```

Edit that value (for example `BOAT_NAME` in the Control Panel or `vesselstack.env`) so it no longer
combines an apostrophe with one of those characters or spans lines, then run the dry run or the
update again. A boat name such as `Skipper's "Dream"` is refused; `Skipper's Dream` is not. The installer
also deletes stale `.vesselstack.env.*`, `.boat-chat.env.*` and `.control-panel.env.*` temp files from
`config/` (left by a killed run) before writing; no other file is touched. If an older release left an update stopped on this problem, restore the pre-update
backup with the commands under [Rollback](#rollback), fix the value, and update again. The env files
are written atomically (see [From a source checkout](#from-a-source-checkout)).

### Backup drive notes from 0.6.1 and 0.6.2

The menu for an unmounted backup drive and its limits are described in
[When the backup drive is not mounted](#when-the-backup-drive-is-not-mounted), including the
`VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=true` workaround for a direct `vesselstackctl update <dir>` run
from an installed 0.6.0 or earlier controller. Two points are only recorded here:

- The Control Panel dialog (0.6.2) applies to that one request only. Rollback: unmount with
  `umount`. Nothing is written to `/etc/fstab` or `vesselstack.env`.
- The dialog cannot help the upgrade that delivers it, because the worker uses the controller that
  is already installed. For the upgrade to 0.6.2, mount the drive by hand or run the update from a
  terminal.

### Instrument displays (0.6.2)

The Control Panel **Displays** tab can edit pages and themes and apply them to a screen over USB or
Wi-Fi (see [Displays](DISPLAYS.md)). Displays are a preview and optional; an install that does not
enable them is unaffected. Wi-Fi (OTA) updates are not yet verified on hardware: flash each display
over USB first.

**Before downgrading below 0.6.2**, run this for every themed display, otherwise the displays bridge
exits with status 2 on the older version (it rejects the `theme` key):

```bash
sudo vesselstackctl display set-theme ID classic
```

Rollback of display settings: `sudo vesselstackctl display restore-config STAMP`, then
`sudo vesselstackctl display flash ID --previous`.

### Heimdall to Homepage (0.6.3)

Homepage replaces Heimdall on port 80 (`HOMEPAGE_PORT`; `HEIMDALL_PORT` is migrated when
`HOMEPAGE_PORT` is unset). An update (`install.sh` or `vesselstackctl update`) stops and removes an
existing `vesselstack-heimdall` container (only if it carries this stack's Compose project label)
and its image, and prints a notice. Port 443 is no longer published.

- A leftover `heimdall` entry in `VESSELSTACK_DISABLED_COMPONENTS` is dropped (it is not turned into
  `homepage`). `HEIMDALL_PORT` and `HEIMDALL_HTTPS_PORT` are no longer kept.
- `/opt/vesselstack-data/heimdall` (`$VESSELSTACK_DATA/heimdall`) is left on disk, with a one-line
  notice. Delete it yourself when you no longer need it. Homepage uses no data directory.
- Heimdall tiles are not migrated. Tiles follow the enabled components; add your own in
  `config/homepage-custom/`.
- If you restrict the firewall by hand, allow port 80 from trusted interfaces only.

Easier sign-in arrived in the same release. The Control Panel and Boat Chat can be unlocked with a
passphrase, optionally remembered on a device for 30 days, or by a pairing link or QR code
(`sudo vesselstackctl panel-link [--for panel|chat]`). Existing long tokens and access/admin codes
keep working. Passphrase sign-in is off until a passphrase is set. Rotating a token signs out the
devices issued under the old value. The Control Panel and Boat Chat use plain HTTP on the LAN, so a
passphrase or pairing link can be seen by anyone who can watch that network: use a trusted LAN only.
Older versions ignore the sign-in store files.

Rollback: reinstall 0.6.2 with its pinned installer (Homepage is replaced by Heimdall only on that
reinstall), or restore the backup of the previous release. Before downgrading below 0.6.2, run
`sudo vesselstackctl display set-theme ID classic` for every themed display.

### Updating to 0.6.3 can end with a shell error (known issue)

Releases up to 0.6.3 rewrote `/usr/local/sbin/vesselstackctl` in place while the older
`vesselstackctl update` (or the pinned installer, which starts it) was still running, and that older
script is read from disk as it runs. When the installer finished, the old command carried on reading
the new file from its old position and could print
`/usr/local/sbin/vesselstackctl: line N: syntax error near unexpected token ...` after the
installer's final `Control panel:` line.

**The install has completed.** The message comes from the old command after the work is done. Do not
restore a backup because of it. To verify:

```bash
cat /opt/vesselstack/config/installed-version     # 0.6.3
sudo vesselstackctl doctor
sudo vesselstackctl status
```

Restore only if one of these fails. The 0.6.4 installer writes the new file beside the old one and
renames it into place, so a running older command keeps its own copy and the message cannot happen on
later updates. Operational impact: none at runtime; only the final message of the update is affected,
and the installed command is the correct 0.6.3 one. Rollback: this note changes nothing to roll back.
If you need to leave 0.6.3, downgrades are refused, so restore the verified pre-update backup as
described under [Rollback](#rollback).

### What changes for an existing boat

The 0.6.0 section of [CHANGELOG.md](../CHANGELOG.md) records the changes for an existing boat: the
stricter env-file checks, the Boat Chat settings API codes and the `X-Boat-Chat-Access-Token`
header, the local `Host` requirement, the four-tab Control Panel, the **Reinstall & start** and
**Check setup** labels, and the one-time Signal K restart for the VesselTwin preload. One detail is
recorded only here.

- **Limits the Control Panel checks when you save.** The timezone must be a valid IANA name such as
  `Europe/Lisbon` (some legacy aliases such as `US/Eastern` may be rejected on newer systems where
  they moved to the `tzdata-legacy` package). Boat length must be more than 0 and at most 500, beam
  more than 0 and at most 100 (plain numbers in metres, no units), and CAN speed a whole number from
  10000 to 1000000. The checks apply only to fields you edit, so an existing saved value you do not
  touch is never rejected or cleared. Errors appear next to the field.

### Coming from 0.4.x

Update once from the command line with the pinned installer. Since 0.5.0 Mosquitto always enforces an
access list, so the first update recreates the broker once, and MQTT users you added by hand to the
old password file lose access unless you add them through VesselStack (see
[MQTT access control](MQTT.md)).

### Verify after updating

```bash
cat /opt/vesselstack/config/installed-version     # the version you installed
sudo vesselstackctl doctor                        # RESULT healthy
sudo vesselstackctl status
```

Open Boat Chat and the Control Panel, and confirm the dashboards show live values. To go back,
restore the verified pre-update backup (see [Rollback](#rollback)); downgrades are refused.

## From a source checkout

Use a reviewed tag from a git clone, with the dry run first:

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

Review Compose and release notes before applying container changes. Prefer the
installed configuration as input. If you do re-run with a file that still says
`GENERATE` (for example a fresh copy of the example), the installer keeps
the already-installed InfluxDB, Grafana, MQTT, Boat Chat and control-panel
secrets instead of generating new ones. A non-`GENERATE` value you set
explicitly still wins.

Once a reviewed release directory is downloaded, the lifecycle command performs
preflight, a verified pre-update backup, installation, and startup:

```bash
sudo vesselstackctl update /path/to/reviewed-release
```

Before it rewrites the installation the installer checks every value it will write to
`vesselstack.env`, `boat-chat.env` and `control-panel.env`, then builds the Python
environment in a staging directory and checks the upgrade path. A failed or
offline-without-bundle run stops with "Nothing was changed" and leaves the previous
installation, `vesselstack.env`, units and version marker as they were. A value the
installer refuses to write (an apostrophe combined with a backslash, double quote, dollar
sign or backtick, or a multi-line value) is caught by `--dry-run` itself, so `vesselstackctl
update` and the Control Panel updater stop before the backup, naming only the setting's key;
see [Upgrading from 0.6.x or older](#upgrading-from-06x-or-older). The env files are then written
atomically (temporary file, then rename), so a failed run never leaves one truncated. When
the code, units and `compose.yml` have been replaced, the staged virtualenv is swapped
in and `installed-version` records the configuration version once everything is rendered.
Starting the services comes after that. `config/install-in-progress` exists from the first
change until the start phase has succeeded; its presence means an installation or update did
not finish.
`sudo vesselstackctl doctor` reports it as `FAIL installation of VERSION did not finish`, with the
command to continue and, when a backup was recorded, the command to go back; `sudo vesselstackctl
status` shows an `Installation INCOMPLETE` line (and exits non-zero) and `vesselstackctl version`
adds a note.

## Component versions and updates

A stack update moves every component that follows the release default to the new release's
version. A component whose version you set yourself (`GRAFANA_VERSION`, `SIGNALK_VERSION` and the
other component settings in `config/vesselstack.env`) stays at that version, which is not tested
with the new release. Check with `sudo vesselstackctl component versions`. To follow the release
again, empty the setting and run the installer (a backup first, because a component that holds data
may have upgraded it one way). The first update to a release that has these settings clears a
Signal K or AIS-catcher version that an older installer wrote down, so they follow the release from
then on; a value you chose yourself is kept. The old forms still work: a `SIGNALK_VERSION` such as
`2.27.0` or `v2.28.0-beta.1`, and any plain `AIS_IMAGE` reference (a moving tag or no tag too, which
`doctor` warns about). The update is refused before anything changes only for a value that
cannot work: a `SIGNALK_VERSION` that is not a 2.x version, an `AIS_IMAGE` or component version
with spaces, quotes, `$` or other unusual characters, an image name with uppercase letters or one
that starts with `-`, and any of the eight newer settings set to a floating name such as `latest`.
Each message names the setting; fix or empty it and run the installer again.
The Control Panel and `vesselstackctl` read these settings from the file only, so a value left over
in the environment of a running Control Panel cannot put a container back on an older image.
See "Component versions" in the README.

## Rollback

Downgrades are refused. Rolling back a completed or half-finished upgrade is a restore of
the pre-update backup that `vesselstackctl update` takes automatically:

```bash
sudo vesselstackctl verify-backup /path/to/vesselstack-TIMESTAMP.tar.gz
sudo vesselstackctl restore /path/to/vesselstack-TIMESTAMP.tar.gz --yes
```

## Offline install (no internet on the boat)

Prepare a bundle on the home lab or any connected machine, carry it to the
boat, then install without any network access. The guided installer does the
checks, `--verify`, `--load` and install for you:
`sudo bash get-vesselstack.sh --offline --bundle /path/to/offline-bundle --tarball /path/to/vesselstack-0.9.1.tar.gz`
(copy `get-vesselstack.sh`, the tarball with its `.sha256` and the bundle on the
carry media; the bundle must be root-owned and not group/world-writable, and
packages and Docker must already be installed). The manual steps follow:

```bash
# Connected machine (needs docker and python3 with pip). Defaults target
# Raspberry Pi OS 64-bit with Python 3.11 and linux/arm64 images.
scripts/prepare-offline-bundle.sh --output ./offline-bundle
#   other targets: --python-version 3.13  --platform manylinux_2_39_aarch64
#   same architecture as this machine:     --native
#   see the image list without downloading: --list

# Copy ./offline-bundle (wheelhouse/, images/, MANIFEST.sha256, BUNDLE_INFO) and
# a copy of this release to the boat. As root the bundle must be root-owned and
# not group/world-writable along its whole path, so make it so first:
sudo chown -R root:root /path/to/offline-bundle && sudo chmod -R go-w /path/to/offline-bundle
sudo scripts/prepare-offline-bundle.sh --load /path/to/offline-bundle   # verifies the bundle, docker load
sudo VESSELSTACK_WHEELHOUSE=/path/to/offline-bundle/wheelhouse \
  ./install.sh --config /opt/vesselstack/config/vesselstack.env --offline --dry-run
sudo VESSELSTACK_WHEELHOUSE=/path/to/offline-bundle/wheelhouse \
  ./install.sh --config /opt/vesselstack/config/vesselstack.env --offline --start
```

The bundle contains a wheelhouse (`pip download --require-hashes
--only-binary=:all:` of the release's hash-pinned `boat-chat/requirements.lock`
for the chosen platform, so only wheels whose sha256 the lock lists are fetched
and no source distribution is ever built), a `docker save` tarball for every
image in `templates/compose.yml` (all profiles; this includes the Mosquitto image that also hashes the broker passwords),
`BUNDLE_INFO` (release, platform, image list, `lock_sha256`) and
`MANIFEST.sha256`. It holds no configuration, credentials or vessel data.
The all-profile image list includes the optional pinned node-exporter image;
offline installation requires it only when `NODE_EXPORTER_ENABLE=true`.

Before installing or loading anything, `--verify DIR`, `--load DIR` and
`install.sh --offline` refuse the bundle unless: the manifest lists exactly the
files present (no extra, unlisted or missing files, no symlinks) and every
checksum matches; `BUNDLE_INFO` names this release and the sha256 of **this
release's** `requirements.lock` (the manifest is anchored to the release content,
not only to itself); every wheelhouse file is a `.whl` whose sha256 is in the
lock; and, when running as root, the bundle path chain and every file in it are
root-owned and not group/world-writable. `install.sh --offline` also requires
`VESSELSTACK_WHEELHOUSE` to be the `wheelhouse/` directory of such a bundle, then
runs `pip install --no-index --find-links $VESSELSTACK_WHEELHOUSE
--only-binary=:all: --require-hashes -r boat-chat/requirements.lock`, so even a
bundle forged consistently cannot install a wheel the pinned release did not
pin. Containers start with `docker compose up -d --pull never`. If an image is
missing the installer lists it with the `docker load` hint: `--start` fails
before changing anything, while a plain render only warns. Offline mode never
contacts a registry or PyPI; Signal K/InfluxDB version bumps still need a new
bundle. Image tarballs are covered by the manifest and the root-controlled path
check but, unlike wheels, are not pinned by digest in the release (Compose images
are tag-pinned).

Online installs use the same lock (`--only-binary=:all: --require-hashes`), so
the Python environment is the same hash-verified set either way. The lock lists
Linux aarch64 and x86_64 wheels for Python 3.11-3.13; on another platform or
architecture the install fails with a clear message instead of building from
source. A release without `boat-chat/requirements.lock`, or a lock missing for
an offline bundle build, is refused before anything changes.

Maintainers: when `boat-chat/requirements.txt` changes, run
`scripts/update-requirements-lock.sh` (needs network; `--python-version` and
`--platform` select targets) and commit the regenerated lock. It is generated
trust-on-first-use from PyPI: review the diff and compare the hashes with the
digests PyPI publishes. `tests/test_requirements_lock.py` fails when the lock is
stale. `scripts/deploy-chat.py` treats a changed `requirements.lock` like a
changed `requirements.txt` and refuses the code-only deploy.

`sudo VESSELSTACK_OFFLINE=true VESSELSTACK_WHEELHOUSE=/path/to/bundle/wheelhouse vesselstackctl update /path/to/release`
(the directory is always the first argument; the `--offline` flag form is also
accepted by this release's `vesselstackctl`, but older installed `vesselstackctl`
versions would read it as the directory, so the guided installer and the docs use the
variable) passes
`--offline` to both the preflight and the install of that release; any value other
than `true`/`false` in `VESSELSTACK_OFFLINE` is rejected. Without the flag or
variable the install runs online, with the hash-pinned lock. Rollback: restore the
pre-update backup (the lock file is inert for older releases).

## Verify a release tag

A checksum shows a download is intact; it is not signed provenance. Release tags are
SSH-signed annotated tags, and `scripts/verify-release-tag.sh` checks one against the
release key listed in `.github/allowed_signers`. It needs a clone that contains the tag
and `ssh-keygen` (part of OpenSSH):

```bash
git fetch --tags
scripts/verify-release-tag.sh v0.9.1
```

The script takes `TAG [ALLOWED_SIGNERS_FILE [EXPECTED_COMMIT]]`. It checks that the tag is
annotated, that its own name equals `TAG`, that it points directly at a commit, that the
signature is Good from an allowed key and that the signer is the release principal. Pass a
full 40 or 64 character commit SHA as the third argument to also bind the tag to the commit
you recorded when the release was built (use `""` as the second argument to keep the default
signers file). It exits non-zero, with an error on stderr, for an unsigned, lightweight or
wrongly signed tag. Older `v0.5.x` tags are unsigned and fail by design. The Control Panel
updater and `vesselstackctl update` check the archive checksum only; they do not run this
script. How maintainers sign a release with a key held in 1Password is in
[Deployment: release signing](DEPLOYMENT.md#1-release-signing-with-an-ssh-signing-key).
