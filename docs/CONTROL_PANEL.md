# Control Panel

Purpose: how to reach, secure, and use the Control Panel, tab by tab, and what to do when it will not connect. Lab acceptance steps are in [Control Panel lab testing](CONTROL_PANEL_TESTING.md).

[Back to README](../README.md) · [Documentation index](README.md)

The Control Panel is the browser page for looking after VesselStack. It shows
whether the stack is running, makes backups, edits settings, checks for and
installs updates, and runs the occasional repair. It stays available when the
rest of the stack is stopped, so you can start it again from the same page. The
`vesselstackctl` command remains the recovery path if the panel itself is
unavailable.

![Control Panel Home tab: services, backup and software status, a Back up now button, and the component cards](images/control-panel-home.png)

*Home: three status cells, one Back up now button, and the components.*

## Open the panel

By default the panel answers only on the boat computer itself. From your laptop,
open a secure tunnel (SSH), leave that window open, and browse to the local
address:

```bash
ssh -L 8780:127.0.0.1:8780 YOUR_USER@YOUR_PI.local
```

Then open `http://127.0.0.1:8780`. If you changed the panel port in Settings,
use that number instead of 8780. The tunnel keeps the token off the boat
network and is the recommended way in.

To open the panel directly from a laptop or phone on a trusted boat network,
preview and then apply only the panel listener:

```bash
sudo vesselstackctl panel-access lan --dry-run
sudo vesselstackctl panel-access lan
```

Open the address printed by the command (normally `http://PI_ADDRESS:8780`).
This is plain HTTP, so the token crosses that network unencrypted; use it only
on a network you trust. It exposes the panel listener only, not the rest of the
stack, and a strict firewall may still block it. `sudo vesselstackctl
panel-access local` restores the loopback-only default.

## Sign in

![Control Panel unlock screen with a token field, a Show token switch and a How do I reach this panel section](images/control-panel-login.png)

*The unlock screen. It explains how to read the token and how to reach the panel.*

You can unlock with a **passphrase you choose** or with the long **token**.

### Passphrase (easiest)

Set one once, on the boat computer, or after unlocking with the token (see
**Settings, Sign-in** below):

```bash
sudo vesselstackctl passphrase set panel
```

It asks twice with hidden input and needs at least 12 characters; a few random
words work well. Then on the unlock screen type the passphrase and press
**Unlock**.

- **Remember this device (30 days)** keeps you signed in on this browser for
  30 days from sign-in (fixed, not extended by use). Without it you stay signed
  in for 12 hours or until the tab closes, whichever comes first.
- **Name for this device** defaults to something like "Safari on Mac" and is
  what you will see in the device list.
- **Lock** signs this device out on the boat and forgets it in the browser.

The browser keeps a random 256-bit device token (in local storage when you
remember the device, in session storage otherwise) and sends it in the
`X-VesselStack-Device` header. The boat stores only a SHA-256 of it, never the
token or your passphrase. No cookies are used. After 5 wrong passphrases from one
address, further tries wait longer each time (up to 15 minutes); more than 20
failures in 15 minutes from anywhere pause passphrase sign-in for 15 minutes.
The long token still works during a pause. Each attempt is counted the moment
it arrives, so a burst of simultaneous guesses cannot slip past the limit.

> **Anyone on the network can pause passphrase sign-in.** Because the pause is
> global (so a shared tunnel address cannot be used to guess slowly), anyone who
> can reach the panel can trigger a 15-minute passphrase lockout by sending wrong
> passphrases. The long token keeps working during it. This is an accepted trade-off; keep the panel on the SSH tunnel.
> A correct sign-in does not clear an address's growing wait (it only withdraws its own attempt); an hour of
> quiet does.

### Pair a phone (link or QR code)

Skip typing entirely: make a **pairing link** and scan it.

```bash
sudo vesselstackctl panel-link                        # sign a device in to the Control Panel
sudo vesselstackctl panel-link --for chat             # sign a phone in to Boat Chat
sudo vesselstackctl panel-link --for chat --admin --label Skipper   # Boat Chat with admin rights
```

It prints the link (`http://boat.local:PORT/#pair=CODE` plus one line per boat-network IPv4 address), the
code to type as `XXXXX-XXXXX`, the expiry time and a QR code you can scan with the phone camera. When the
panel only listens on this computer (the default) it prints the tunnel form
`http://127.0.0.1:8780/#pair=CODE` with an `ssh -L` hint; pairing a phone directly needs
`sudo vesselstackctl panel-access lan` first. In an unlocked panel use **Settings, Sign-in, Pair a phone**:
**Pair a device to the Control Panel** or **Pair a phone to Boat Chat** (optionally with admin rights) shows
the QR code, link, code and a 10-minute countdown. On the unlock screen, **Have a pairing code?** accepts the
typed code (case, dashes and spaces are ignored; I and L read as 1, O as 0).

A pairing is single use, expires after 10 minutes, and at most three can be waiting at once. Opening the link
signs that browser in as a remembered device (30 days) and removes the code from the address bar. Wrong codes
count against the same throttle as passphrases. **Treat the QR code as a password for 10 minutes**: whoever
scans it first gets in. The link is plain HTTP on your boat network; the code sits in the URL fragment, which
browsers never send to the server, so it is not logged.

**Host names.** The panel answers only requests whose `Host` names this
machine (`127.0.0.1`, `localhost`, `[::1]`, and with `panel-access lan` also the
boat's hostname, `hostname.local` and its current addresses, each with the panel
port). Other names get `421 Misdirected request`; this blocks DNS-rebinding
attacks from web pages. If you reach the panel through another name (a reverse
proxy or a custom DNS name), add it to `CONTROL_PANEL_ALLOWED_HOSTS` in
`config/control-panel.env` (comma separated, `name` or `name:port`; names
without a port get the panel port), then restart the panel.

> **Plain HTTP on the boat network.** If you opened the panel with
> `panel-access lan`, the passphrase and device token cross the network
> unencrypted, exactly as the token does. Prefer the SSH tunnel (the default).

### Token

Open **Use the long token instead**. Read the generated token on the Pi:

```bash
sudo grep '^CONTROL_PANEL_TOKEN=' /opt/vesselstack/config/control-panel.env
```

Paste the result into **Control Panel token** and press **Unlock with token**.
Either form works: the whole `CONTROL_PANEL_TOKEN=…` line or just the value after
the equals sign. **Show token** reveals what you pasted. The token is kept in
this browser tab's session storage, cleared by **Lock** or when the tab closes,
and sent only in the `X-VesselStack-Token` request header. Scripts keep using it
exactly as before.

If no passphrase is set yet, the unlock screen says so and shows the command
above. If you press **Lock** with unsaved Settings edits, the panel asks first,
because locking discards them.

## The five tabs

The header shows the installed version and a connection state (Connected,
Disconnected or Locked). The tabs are **Home**, **Settings**, **Displays**, **Updates** and
**Maintenance**. The Updates tab shows a number when an update is known.

On a phone the tabs and cards stack in one column, and the page follows the device's light or
dark setting.

![Control Panel on a phone in dark mode](images/control-panel-phone-dark.png)

*The Control Panel at phone width in dark mode.*

### Home

Home is for looking, not for changing.

- **Services** says "All systems running", or how many components are stopped
  and which ones.
- **Backup** shows when the last backup was made and whether the backup drive is
  mounted. It warns when there is no backup yet, when the drive is not mounted,
  when the folder is not writable, or when backups sit on the computer's own
  disk instead of an external drive. When scheduled backups are on
  (`VESSELSTACK_BACKUP_SCHEDULE`) it also shows the schedule, whether the last scheduled run
  succeeded, failed or was skipped because another operation was running, and when the timer runs
  next. This is read-only; the schedule is changed under Settings, Storage & history (applied by a
  reinstall). A scheduled backup stops the whole stack while it runs.
- **Software** shows the installed version, or the newer version found by the
  last update check, with a link to the Updates tab. It never checks by itself.
- **Back up now** is the one action on this page. It runs `vesselstackctl backup`.

Below the status row, every component has a card with its plain name, a status
word (Running, Stopped, Disabled or Unknown) and what it does. Running
components offer **Restart** and **Stop**; stopped ones offer **Start**.
**Configure** jumps to that component's section in Settings. A **⋯** menu on a
core component holds **Disable**, which stops it, removes its container and
keeps it off, including after a restart or **Start all**. **Enable** brings it
back. Optional workers that are not core show **Turn on in Settings** instead.
The search box and the All, Running, Stopped and Disabled filters narrow the list.
Stopping, restarting and disabling a component ask in a dialog first.

### Settings

![Control Panel Settings tab with a jump list, sections, and the Save and Apply bar](images/control-panel-settings.png)

*Settings: a jump list, one section per topic, Advanced sections collapsed, and one save bar.*

Nothing changes on the boat until you press **Save** and then **Apply**.

- A jump list at the top goes to Vessel, Network & access, Boat Chat, Signal K &
  NMEA 2000, and Displays. Under **Advanced** it lists Storage & history,
  Services & ports, AIS receiver, Home Assistant and Installation. Advanced
  sections start collapsed; Installation is read-only here.
- Every field has plain help text. Mistakes show inline, next to the field. The
  server checks the timezone against the system's timezone list (a valid IANA
  name such as `Europe/Lisbon`; some legacy aliases like `US/Eastern` may be
  rejected on systems that moved them to `tzdata-legacy`), boat length (above 0,
  up to 500 m), beam (above 0, up to 100 m) and CAN speed (whole number, 10000
  to 1000000). Only fields you edit are checked, and a save with any problem
  is rejected with a message per field, so nothing is half-saved.
- Secrets are write-only. The panel reports only whether one is set; leave a
  secret blank to keep its value, or use the explicit **Clear stored value**
  checkbox. Clearing an optional integration token disables that credential;
  clearing a required installer-managed password or token makes the installer
  generate a replacement the next time settings are applied, and dependent
  clients must then be updated.
- The save bar at the bottom shows how many changes are waiting. **Discard**
  resets the form to the saved values. **Save** writes the configuration
  atomically with mode 600 and keeps a rollback copy under
  `/opt/vesselstack-data/control-panel/config-backups/`; it does not restart
  anything. **Apply** then renders and installs the saved settings (see
  [Backup, update, apply and reinstall](#backup-update-apply-and-reinstall)).
- The Telegram and telemetry-indexer switches take effect when settings are
  applied. Configure the Telegram bot token and allowed chat IDs before enabling
  the worker. Disabled workers cannot be started from the component cards.
- Changing the panel's own listener or port takes effect when the panel
  restarts: use **Maintenance → Restart Control Panel**.

### Sign-in

At the bottom of Settings: set or change the passphrase (the current one is
asked for unless you unlocked with the token), and see **Signed-in devices** with
name, when it signed in, last seen, when it expires and **this device**.
**Revoke** signs one device out immediately; **Revoke all devices** signs out
every device including this one. Changing the passphrase signs out every other
device. These are not part of Save and Apply and take effect at once.

On the boat computer: `sudo vesselstackctl devices list`, `devices revoke ID`,
`devices revoke-all`, `passphrase status` and `passphrase clear panel`.

**Rotating a token signs out every device.** A new `CONTROL_PANEL_TOKEN` (re-running the installer with a
different value) revokes all Control Panel devices and waiting pairing links; a new Boat Chat access code
saved under Settings revokes all Boat Chat devices and links (the save reports how many were signed out, or
the command to run if that failed). To do it by hand: `sudo vesselstackctl devices revoke-all` (panel) or
`sudo vesselstackctl devices revoke-all chat`. Admin pairing for Boat Chat uses the admin code Boat Chat
actually enforces (its Settings-page value first, then `config/boat-chat.env`).

In **Settings → Homepage**, select a VesselStack palette, heading, tile layout, and clock. **Save**
and **Apply**, then reload Homepage to see the changes.

### Displays

The Displays tab shows your ESP32 instrument displays and the data they can show, and lets you change
a display's colour theme and pages and send the result to the screen. Looking is read-only; changes
happen when you press **Save**, **Start** or **Save and upload**. The tab opens when
you pick it (it does not poll), and **Refresh** reads it again. The Settings tab's Displays section
links to it.

- **One card per display** with its name, id, board, colour theme and a firmware chip:
  **Firmware sent** (the last successful upload matches your saved settings), **Changes not on screen** (the settings changed after the firmware was built, or
  a newer build exists than the one flashed), **Never flashed**, **Not built**, **Last flash failed** or
  **Unknown**.
- **A preview of every page**, drawn from the same geometry the firmware generator uses (the panel
  does not repeat layout maths) with the display's theme colours: the round screen, the safe area, the
  header and page dots, tiles, status cells, tank bars and gauges. **Day / Night** switches the
  palette and **Normal / OK / Warning / Alarm / No data** shows how each severity looks. Values are
  live numbers from Signal K when the data list has them, otherwise a dash. It is labelled
  **Approximate preview**; the display is the final word on spacing and fonts.
- **Colour theme** swatches for `classic`, `high-contrast`, `daylight` and `ocean` with the current one
  marked. Press **Edit pages and theme**, pick a swatch and the preview changes at once; nothing is
  stored until **Save**. (Going back to an older VesselStack: run `set-theme ID classic` first.)
- **Editing**: **Edit theme and pages** opens a private draft. Choose a page by name or select
  **Add page**. Set its name and layout, then use **Add values** to search within that page. You can
  reorder pages or values, remove them, and preview layout and item changes before saving. The editor
  shows capacity, required items, and fixed slot roles. Values from a detected or configured source can be added directly. When no source
  is detected for a catalog value, select the suggested source from the dropdown. Gauges and tank
  bars show the range defined for the value. **Count in alarm banner** is optional. **Done** closes an
  unchanged draft. **Cancel** asks before discarding edits. **Save** keeps the settings on the boat
  computer. If the saved settings changed elsewhere, the panel keeps your draft and offers a reload.
- **Apply to screen** opens the upload dialog. Choose **By USB cable** (the first upload) or
  **Over Wi-Fi**. The USB picker prefers stable device names and also checks for USB serial ports
  without one. **Refresh** rescans after you plug in the screen. Wi-Fi uses the last successful
  address or `vs-ID.local`; an IP address is more reliable. A new Wi-Fi address requires explicit
  confirmation. For an edited draft, **Save and upload** saves before starting the upload. Cancelling
  the dialog keeps the draft. Wi-Fi updates remain unverified on hardware. The first build can take
  10–15 minutes and needs about 3 GB free. Backups, updates and settings changes wait while the
  upload runs. The
  progress panel shows a stepper from the `STAGE:` lines (Preparing, Building, Sending, Done), a
  **Details** tail of the output and, on failure, a **To fix:** banner. Apply is disabled, with the
  reason and the command to run, when readiness blocks it (no Wi-Fi stored, no data password, no
  Docker). Disk space is checked when compilation is needed.
- **History** lists the settings backups (`display history`) with **Restore**. When the previous
  firmware is still what the stored `firmware.prev` is, **Put previous firmware back on screen** runs
  `display flash ID --previous` (never `--force`) after you restore the matching settings.
- **Pages** with their layout and the items on each (labels from your instruments, notifications and
  Home Assistant items). Clicking a page previews it.
- **Upload and live data checks** show whether the display bridge is enabled, the required credentials
  are stored, and MQTT is configured to listen on the network. The checks also show missing build tools
  and low disk space. After upload, check the physical screen for live readings.
- **Available data**: a searchable, grouped list of the Signal K values a page can show, with badges
  **Live**, **No live data**, **Uncatalogued** and **Used on** a display, and the current value when it
  is live. Opening the tab asks your Signal K server once (at most every 30 s) which values have data;
  if it cannot be reached the list still shows, without live values.

Endpoints (all POSTs need the unlock token and the panel's Origin, JSON only, at most 1 MiB; payloads
go to the tool on stdin, never on a command line): `POST /api/displays/save` (`set-pages ID --if-revision
REV --stdin`; 409 when an operation is running or the revision changed, 400 for invalid pages),
`POST /api/displays/deploy` `{id, method: "ota"|"serial", target, confirm_new_target?}` (`display deploy ID --ota|--serial
TARGET --progress`, an operation holding the lifecycle slot; the OTA target must be an IP address or a
plain host name without a `%` scope id and not a name ESPHome reserves (`bootsel`, `mqtt`, `mqttip`, `ota`, `serial`, `COM*`), the USB target one of the listed serial ports), `GET /api/displays/history`,
`POST /api/displays/restore` `{stamp, revision}` and `POST /api/displays/rollback` `{id, method, target, confirm_new_target?}`
(`display flash ID --previous ...`; 409 while another operation runs).

**What was last sent (`last-flash.json`).** After every successful upload the tool records, in the root-only
`build/displays/ID/last-flash.json`, when and how (USB or Wi-Fi), the target, the image and VesselStack version
(hashes only, no password). The panel uses it to prefill the Wi-Fi address and the method of the Apply dialog, to say
**Credentials changed since this was sent** when the Wi-Fi or data password was rotated afterwards, and to decide whether
the previous firmware can be put back.

**Wi-Fi address confirmation.** The firmware image contains this boat's Wi-Fi password. The Apply and Put-previous-firmware
dialogs prefill the address that worked last time; entering any other address (or any address when none worked yet) shows a
checkbox, "Send firmware, which contains this boat's Wi-Fi password, to ADDRESS", and the button stays disabled until it is
ticked. The server enforces the same rule: a Wi-Fi request whose target differs from the last successful one is refused with
400 unless it carries `confirm_new_target: true`. USB uploads need no confirmation.

**Putting the previous firmware back.** The button appears only when the previous image was built from exactly the current
settings and credentials (otherwise the upload would be refused). The usual route is **History → Restore** of the saved copy
that was current when that older firmware was built; once the settings match, **Put previous firmware back** appears on the card.

**Builder image.** A missing ESPHome builder image does not block Apply: the checklist shows it as a to-do (about 250 MB, needs internet),
and when the computer is offline the deploy fails with a `To fix:` hint. This is accepted behaviour.

Adding a display (`sudo vesselstackctl display add ID --board BOARD --name NAME`) and entering Wi-Fi
(`sudo vesselstackctl display wifi`) stay on the command line, so passwords never pass through this
page. Nothing from the secrets file reaches the browser: the panel shows only yes/no for stored
passwords. Rollback: nothing is changed by this tab, so there is nothing to roll back.

### Updates

![Control Panel Updates tab showing the installed version and a Check for updates button](images/control-panel-updates.png)

*Updates: nothing is contacted until you press Check for updates.*

The tab shows the installed version and, if the running panel differs from it,
a note to restart the Control Panel. **Check for updates** is the only thing
that contacts `github.com`; nothing about your boat is sent. The result is
either "You're up to date" or the newer release with a link to its release
notes on GitHub and a **Back up & install** button.

**Include early test releases** adds GitHub prereleases to the check. They are
for testing; leave it off on a boat in regular use. The 0.5.x panels called the
same switch "Include lab prereleases"; an owner updating from 0.5.x uses that older name.

**Back up & install** asks in a dialog (when the backup drive is not mounted, a longer one, see
[When the backup drive is not mounted](#when-the-backup-drive-is-not-mounted)), then runs the update in a detached
worker so it survives the panel restarting. It:

1. downloads the release archive and its checksum file from GitHub and checks
   the size, the SHA-256 checksum and the digest GitHub reports;
2. unpacks it and runs the installer's checks (`--dry-run`);
3. takes a backup with `vesselstackctl backup --require-mounted`, so the update
   stops here if the backup drive is not mounted (unless you chose another way in the dialog
   described below, in which case it mounts the drive or backs up to the system disk first;
   with **Install without a backup** it makes no backup at all);
4. runs the new installer with `--config … --start`;
5. restarts the Control Panel and waits for it to report the new version.

The panel and the services may be interrupted for a few minutes. The page
reconnects by itself, and the banner confirms the new version. The panel checks
the archive against its published checksum; verifying the signed release tag is
a separate step you can run yourself (see
[Updating](UPDATING.md#verify-a-release-tag)).

**Advanced: install a reviewed release folder** is for a release you already
downloaded, checked and unpacked on the Pi under `/var/lib/vesselstack/releases`.
**Back up & update from folder** runs `vesselstackctl update FOLDER`, which
takes its own backup first. It shows the same dialog when the backup drive is not mounted.

### When the backup drive is not mounted

Before **Back up & install** or **Back up & update from folder** the panel checks the backup folder.
If it is on a mounted drive, the usual confirmation appears. If the folder is under `/media` or
`/mnt` and that drive is not mounted (or the folder is not on an external drive at all), a dialog
lets you choose, for this one request only. Nothing you pick here is remembered, and
`vesselstack.env` and `/etc/fstab` are not changed:

| Choice | What happens |
|---|---|
| Mount the backup drive now | Only when `/etc/fstab` has an entry for the folder. |
| Mount a listed drive | Connected USB drives (ext4 recommended, exFAT allowed) that are not part of the system disk; refused and already-mounted drives are listed with the reason. The panel only sends the drive's UUID; the root side lists again and mounts nothing else. Mounted with `nodev,nosuid,noexec`; the mount folder must be empty. |
| I plugged the drive in; check again | Reads the list again. |
| Back up to the system disk this once | Saves to `/var/backups/vesselstack` on the SD card, not into the unmounted folder. Refused when the data would not fit. Not recommended. |
| Use a different backup folder | Saves the folder as the **Backup folder** setting, then checks again. Unavailable while Settings has unsaved edits. |
| Install without a backup | Needs the box **I understand there is no backup to restore if this update fails**. No backup is made; the result banner says so. |

The system-disk and no-backup choices are not offered, and the server refuses them, when
`vesselstack.env` or the panel's own environment contains `VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=false`;
the update worker checks again right before it skips or redirects the backup. The `backup` field is
accepted only for the `update` and `update-release` actions (other actions get a 400). The choice is
recorded in the operation record as `backup_mode` / `backup_skipped` for display only; it is never
written to `vesselstack.env` or reused. A drive mounted by UUID is checked after the mount
(its filesystem UUID must match) and unmounted again if not. The mount runs as root
outside the panel's own sandbox (in the update worker, or a transient `systemd-run` unit limited to 300 seconds
for the folder update). This cannot help the upgrade that delivers it: the Control Panel worker uses the controller and panel that are already installed, so for the upgrade to 0.6.2 itself mount the drive by hand or run the update from a terminal. Rollback: `sudo umount` the drive.

![Control Panel Updates tab with a newer release listed and a Back up and install button](images/control-panel-update-available.png)

*After a check, a newer release appears with its notes and a Back up & install button.*

### Maintenance

![Control Panel Maintenance tab grouped into Backup, Start and stop, Setup and repair, History storage and Panel](images/control-panel-maintenance.png)

*Maintenance: occasional jobs, each described before you run it.*

| Group | Button | What it does | Command behind it |
|---|---|---|---|
| Backup | **Back up now** | Saves settings and data to the backup folder. Services pause while the copy is made, which can take a few minutes. | `vesselstackctl backup` |
| Start & stop | **Start all** | Starts every enabled service that is stopped. | `vesselstackctl start` |
| | **Restart all** | Stops and starts every service; live data pauses for under a minute. | `vesselstackctl restart` |
| | **Stop all** | Turns off every service. The Control Panel stays available. | `vesselstackctl stop` |
| Setup & repair | **Check setup** | Checks saved settings and this Pi; changes nothing. | `install.sh --config … --dry-run` |
| | **Reinstall & start** | Re-applies saved settings and restarts every service; live data pauses for about a minute. Unsaved Settings edits are not included. | `install.sh --config … --start` |
| | **Go to Settings** | Applying a change is done with **Apply** on the Settings tab. | none |
| History storage | **Provision history** | Creates the places where history is kept and the small job that builds one-minute averages. Safe to run again. | `installer/scripts/bootstrap-influx.sh` |
| Panel | **Restart Control Panel** | Restarts only the panel. Boat services keep running and the page reconnects in a few seconds. | detached `systemctl restart` of the panel service |

Restoring from a backup is deliberately not on this page. Restore replaces
current data, so do it from the boat computer:

```bash
sudo vesselstackctl verify-backup BACKUP_FILE
sudo vesselstackctl restore BACKUP_FILE --yes
```

Operation commands are fixed allowlisted argument arrays; the panel does not
expose an arbitrary shell. While one operation runs, the other buttons are
disabled so lifecycle changes cannot overlap.

## Results and confirmations

The result of the latest operation stays in a banner under the tabs until you
dismiss it. Success shows the time and, for a backup, where the file was saved;
failure shows the reason and what to try next. When an update or backup stops
for something you can fix (for example an unmounted backup drive, an unsupported
`BOAT_UNITS` value, or a saved value that cannot be written safely), the banner
adds a **To fix:** line with the exact step, and any command to run over SSH is
shown in monospace. The Control Panel never asks questions or changes your
settings itself; run the fix, then start the operation again. **Details** opens
the command output.

![Control Panel Home with a green banner reporting that a backup finished and where it was saved](images/control-panel-backup-result.png)

*The result banner after Back up now.*

Anything that stops or interrupts something asks first, in a dialog that lists
what is affected and has a plain **Cancel**.

![Control Panel confirmation dialog for Stop all services listing what will go dark](images/control-panel-stop-dialog.png)

*The Stop all dialog lists what will go dark and says the panel stays available.*

## Backup, update, apply and reinstall

| Action | Takes a backup | Changes software | Applies saved settings | Interrupts |
|---|---|---|---|---|
| **Back up now** | It is the backup | No | No | Services pause during the copy |
| **Apply** (Settings) | No | No | Renders and installs them; containers are not recreated | The displays bridge, if running, is restarted; other services keep running until restarted |
| **Reinstall & start** | No | No | Yes | Every service restarts, about a minute |
| **Back up & install** (Updates) | Yes, first, drive must be mounted (or you choose otherwise in the dialog) | Yes, to the chosen release | Yes | Panel and services, a few minutes |

**Apply** runs `install.sh --config …` against your saved settings without
`--start`. That is the installer's render-only mode: it writes the rendered
configuration but does not recreate containers or restart Boat Chat, Telegram or
the Control Panel; it restarts only the displays bridge, if it is running. To make
a running service use the new values, restart that component from its Home card,
or use **Restart all** or **Reinstall & start**. The Apply dialog says the same:
saved settings are written into the service files, running services keep running
and take the changes at their next restart, and the displays bridge restarts now.
Hardware probes and the Signal K connectivity
check still gate installer actions.

## When the panel cannot connect

- **Disconnected.** The header says Disconnected and the page shows "Can't reach
  the boat computer". Controls are paused. The panel retries five times and then
  waits for **Retry now**. Check that the SSH tunnel window is still open and
  that the Pi is powered, then press **Retry now**. If the Pi was updating or
  restarting, it reconnects by itself and reloads when the panel has restarted.
  Unsaved Settings edits are kept and you are asked to save or discard them
  first.
- **Your session ended.** After a restart or a token change the panel returns
  to the unlock screen and says so. Paste the token again; unsaved edits are
  kept.
- **Locked out of passphrase sign-in.** "Too many attempts" means several wrong
  passphrases were entered; wait the time shown (at most 15 minutes) or unlock
  with the long token. To start over from the boat computer run
  `sudo vesselstackctl passphrase set panel` (this signs every device out) or
  `sudo vesselstackctl passphrase clear panel` to turn passphrase sign-in off.
  Forgot the passphrase? Use the token or the commands above. The pause is
  global, so anyone on the network can trigger it by guessing; the token still
  works while it lasts.
- **421 Misdirected request.** The address in the browser is not one the panel
  recognises. Use `localhost` through the SSH tunnel, or add the name to
  `CONTROL_PANEL_ALLOWED_HOSTS` (see Host names above).
- **This device is no longer signed in.** The 12-hour or 30-day period ended, or
  the device was revoked. Enter the passphrase again.
- **That token wasn't accepted.** Re-read the token on the Pi. A leftover space
  or an old token from before a reinstall is the usual cause.
- **Couldn't reach the Control Panel.** At the unlock screen this means the
  tunnel is closed or the panel is not running. Restart it from the Pi:

  ```bash
  sudo systemctl restart vesselstack-control-panel.service
  sudo vesselstackctl logs control
  ```
- **"A newer Control Panel is installed than the one running."** Press
  **Restart Control Panel** on the Maintenance tab.

The panel does not restart itself while an operation is running. After applying
a release that changes Control Panel code, restart it the same way.

## Telegram, indexer and host service

Inspect the optional workers with:

```bash
sudo vesselstackctl logs telegram
sudo vesselstackctl logs indexer
systemctl list-timers vesselstack-telemetry-indexer.timer
```

The service runs as root because Docker, systemd, installation, backup and
hardware operations require host privileges. Keep the token private, keep
loopback binding unless a trusted authenticated access layer is in place, and
never expose port `8780` to the internet. To disable the panel without
affecting telemetry services:

```bash
sudo systemctl disable --now vesselstack-control-panel.service
```

Re-enable it with `sudo systemctl enable --now vesselstack-control-panel.service`.
CLI configuration and lifecycle commands continue to work while it is disabled.
Optional workers and schedules follow the same guard: a start or restart from the
panel is rejected until the worker's enable flag is set. Offline setup checks
require the node-exporter image only when `NODE_EXPORTER_ENABLE=true`.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Back up now fails with a mount message | The backup drive is not mounted. Mount it, then try again; the Home tab shows the drive state. (**Back up & install** offers to mount it for you; see [above](#when-the-backup-drive-is-not-mounted).) |
| Home says backups are on the computer's own disk | The backup folder is not on an external drive. Set **Backup folder** under Settings → Advanced → Storage & history to a path under `/media` or `/mnt`. |
| Save is rejected with field messages | Fix the marked fields (unknown timezone, number out of range) and save again. |
| Check for updates fails | The Pi has no route to `github.com`. Check its internet connection; offline boats use [the offline path](UPDATING.md). |
| A component shows Stopped with extra detail | The detail is the raw state, for example restarting. Open its log with `sudo vesselstackctl logs COMPONENT`. |
| A disabled component says it is still running | Press **Retry disabling**. |
