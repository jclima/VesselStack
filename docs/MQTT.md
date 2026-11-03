# MQTT access control

Purpose: how Mosquitto accounts, the access list, and display credentials are generated, where the files live, and how to rotate them.

[Back to README](../README.md) · [Documentation index](README.md)

Mosquitto always enforces an access list (`acl_file /mosquitto/auth/acl`,
`password_file /mosquitto/auth/passwords`). Every `install.sh` run that is not
`--dry-run` (including render-only runs such as Control Panel "Apply
configuration"), and `vesselstackctl start` when the files are missing, render
the list and hashed passwords through
`python3 displays/displayctl.py sync-mqtt`. Passwords are hashed inside the
pinned Mosquitto image (`mosquitto_passwd -U`) and are never placed on a
command line. The plaintext list exists only briefly as `root:root` `0600`
before hashing and is never readable by the broker; the hashing container then
hands only the hashed file to `1883:1883`.

Where the files live and who owns them:

| Path | Owner | Mode | Notes |
| --- | --- | --- | --- |
| `$VESSELSTACK_ROOT/config/mosquitto/` | `root:root` | `0755` | mounted **read-only** as a directory at `/mosquitto/config`; holds `mosquitto.conf` (`root:root` `0644`) |
| `$VESSELSTACK_ROOT/config/mosquitto-auth/` | `root:1883` | `0750` | mounted **read-only** into the broker at `/mosquitto/auth` |
| `acl`, `passwords`, `acl.prev`, `passwords.prev` inside it | `1883:1883` | `0600` | the broker's user owns them, as Mosquitto expects |
| `$VESSELSTACK_ROOT/config/mosquitto.conf` (legacy) | unchanged | unchanged | no longer written or mounted by this release; left in place so a container created by an older release (and a downgrade) keeps working |

The Mosquitto configuration moved from the single-file mount
`config/mosquitto.conf` to the directory `config/mosquitto/` on purpose: a
single-file bind mount keeps pointing at the inode it was created with, and
`restart: unless-stopped` re-resolves the bind source on every restart, so
replacing that file under a still-running old container would send it into a
crash loop (it would load the new configuration without the `/mosquitto/auth`
mount). The installer refuses a symlinked `config/mosquitto` (or
`mosquitto.conf` inside it) and creates the directory `root:root` `0755`.

The directory is root-owned and not writable by the broker (uid/gid 1883), so a
compromised or misbehaving broker cannot create, remove, rename or replace
entries there, in particular it cannot plant symlinks for the root-run sync
code to follow. The files are owned by the broker user only because Mosquitto
warns about files it does not own and states that future versions will refuse
to load them; the broker cannot swap them out because the directory and mount
are read-only. The sync code is hardened accordingly: it refuses a symlinked
`config/` or `mosquitto-auth` directory, refuses (as root) a directory that is
not owned by root or is group/world writable, opens everything relative to a
directory file descriptor with `O_NOFOLLOW`, creates temporary files with
`O_CREAT|O_EXCL|O_NOFOLLOW`, sets owner and mode on the open file descriptor,
refuses symlinks or non-regular files at `acl`, `passwords` and their `.new`,
`.prev` and `.prev.tmp` names, and never touches
`$VESSELSTACK_DATA/mosquitto`. The hashing container runs with
`--network none --cap-drop ALL --cap-add CHOWN --security-opt
no-new-privileges` and `umask 077`; the plaintext file and any
`passwords.new.*` file `mosquitto_passwd` derives from it (such as its
`.backup.XXXXXX` copy) are removed on every outcome. The Mosquitto image must
be present to hash passwords; if it is missing and cannot be pulled (for
example offline), the installer stops with a clear message before changing any
file. `sync-mqtt`, `bridge-env` and the lifecycle commands all hold the same
exclusive lock (`config/.displays.lock`, 10 s wait), so a Control Panel Apply
cannot interleave with a concurrent `displayctl add`.

With `DISPLAYS_ENABLE=false` (the default) only the administrator account
(`MQTT_USERNAME`) exists: it has `readwrite #` and `read $SYS/#`, so Home
Assistant, Signal K and other existing clients that use that account keep
working. With `DISPLAYS_ENABLE=true` these accounts are added:

| Account | Read | Write |
| --- | --- | --- |
| `MQTT_USERNAME` (admin) | `#`, `$SYS/#` | `#` |
| `DISPLAYS_BRIDGE_MQTT_USERNAME` (default `vesselstack-bridge`) | `vesselstack/v1/displays/+/status` | `vesselstack/v1/instruments/#`, `vesselstack/v1/notifications/#`, `vesselstack/v1/alarms/#`, `vesselstack/v1/ha/#`, `vesselstack/v1/bridge/#` |
| `vsdisp-<display id>`, one per display in `config/displays.json` | `vesselstack/v1/instruments/#`, `vesselstack/v1/notifications/#`, `vesselstack/v1/alarms/#`, `vesselstack/v1/ha/#`, `vesselstack/v1/bridge/#` | `vesselstack/v1/displays/vsdisp-<display id>/status` only |

Display accounts are read-only for instrument, notification, alarm and Home Assistant
item data and cannot read other MQTT topics such as `homeassistant/#`. Any other MQTT
account is granted nothing. The `vesselstack/v1/ha/#` rule is new with Home Assistant
items: the next installer run or `sudo vesselstackctl display sync-mqtt` rewrites the ACL
on an existing install and signals the broker; until then the bridge cannot publish
Home Assistant items and displays cannot read them (the other topics keep working).

**Existing installations:** the `vesselstack/v1/notifications/#` rules were added
with the display notifications feature. The ACL file is rewritten by the next
installer run (`install.sh`, `vesselstackctl update`) or by `sudo vesselstackctl
display sync-mqtt`; until then the bridge cannot publish notifications and
status-grid displays show `--` for them. The change only adds two rules; roll
back by restoring the previous release and running `display sync-mqtt` again.

The `vesselstack/v1/alarms/#` rules (bridge write, displays read) were added with the
alarm summary. They are picked up the same way: the next installer run
(`install.sh`, `vesselstackctl update`) or `sudo vesselstackctl display
sync-mqtt` rewrites the ACL and signals the broker. Until then the bridge
cannot publish `alarms/summary` (its publishes are refused, nothing else
breaks) and a display with `ui.alarm_takeover` never shows the banner. The
change only adds two rules; roll back by restoring the previous release and
running `display sync-mqtt` again.

Operational notes:

- Bridge and display passwords are generated once (`secrets.token_urlsafe(32)`)
  into `$VESSELSTACK_ROOT/config/displays.secrets.json` (mode 0600) and are
  reused on every later run; this file is inside the backed-up `config/`
  directory, so treat backups as secrets. `vesselstackctl display remove`
  deletes the display's entry here as well. Credentials of a display removed
  from `displays.json` by hand stop working immediately but remain in that file
  (so a temporarily broken or missing `displays.json` cannot destroy them);
  delete the entry by hand, or re-add and then `remove` the display, to retire
  it permanently. With displays enabled and no
  `displays.json`, only the admin and bridge accounts exist.
- The sync tool sets the ownership and modes above (it must run as root),
  keeps `acl.prev` and `passwords.prev` copies, and signals the running broker
  to reload (`SIGHUP`), which also disconnects removed accounts. Because the
  auth directory is mounted as a directory, replaced files reach the running
  broker without a restart.
- `install.sh --dry-run` (the Control Panel "Preflight", and the check that
  `vesselstackctl update` runs before it takes a backup) never writes files,
  pulls images, takes the lock or signals containers, but it runs
  `displayctl.py preflight`, which rejects up front everything the sync would
  reject: an administrator `MQTT_PASSWORD` containing `:` or leading/trailing
  whitespace (a generated one is always fine), invalid account names, a bridge
  name that collides with `MQTT_USERNAME` or a `vsdisp-<id>` account, an
  invalid `displays.json` (when `DISPLAYS_ENABLE=true`), and a missing
  Mosquitto image. A missing image is a failure only when the registry cannot
  be reached either (`docker manifest inspect`); the message names the
  `docker pull` to run once while online. With a reachable registry it is only a
  warning ("will be pulled"), so the documented first-install flow (dry run,
  then `--start`) still works on a boat that has not pulled anything yet. A real
  run repeats the same checks and pulls a missing image *before* it changes any
  file, then syncs the MQTT access files **before** it installs the new
  `compose.yml` and `config/mosquitto/mosquitto.conf`, so a failure leaves the
  previous broker definition in place. **Apply** and **Reinstall & start** may
  pull the pinned Mosquitto image on first use and send `SIGHUP` to a running
  broker.
- Upgrade impact: the Mosquitto container is recreated (a brief MQTT
  interruption) by the first `install.sh --start`, `vesselstackctl start` or
  `vesselstackctl update` after the upgrade, because its compose definition
  changed (new configuration directory and read-only auth mount). **Until then
  the old container keeps its old configuration and mounts, also across reboots
  and `docker restart`:** it still bind-mounts the untouched legacy
  `config/mosquitto.conf` and reads the legacy `passwords` file in
  `$VESSELSTACK_DATA/mosquitto`, so existing clients keep working, but the new
  ACL, the bridge account and the display accounts do **not** exist on that
  broker. A render-only run (Control Panel **Apply**, or
  `install.sh --config ...` without `--start`) writes the new files but never
  recreates containers; it prints a `NOTICE` when the running broker predates
  the new definition (and when the displays bridge is enabled but not running)
  telling you to run `sudo vesselstackctl start`. In particular, enabling
  displays through Apply needs that start before the bridge can connect.
  Further configuration-only changes are applied by the same commands: the
  installer records the hash of the last applied `mosquitto.conf` in
  `config/.mosquitto-conf.sha256` and force-recreates the broker when a later
  `--start` (or `vesselstackctl start`) sees a different file, even if the file
  was rendered by an earlier render-only run.
- Legacy files: releases that kept `passwords`, `acl` and their `.prev`/`.new`
  copies in `$VESSELSTACK_DATA/mosquitto/` leave them there untouched and
  unused (the broker's persistence database stays in that directory). They are
  safe to delete by hand once the new configuration works; VesselStack never
  deletes or follows anything in that broker-writable directory.
- Manually added MQTT users are not preserved: every non-dry-run `install.sh`
  and `vesselstackctl update` rebuild the password file from `MQTT_USERNAME` and the
  displays accounts, and any account not in the ACL has no access. Add other
  clients by sharing the administrator account or extend VesselStack.
- Accounts are rebuilt when you change `DISPLAYS_ENABLE`, the bridge name or
  `displays.json` and run `sudo ./install.sh --config ...` (render-only runs sync too; add `--start`
  to recreate the broker). Names must
  match `[A-Za-z0-9_.-]{1,64}` and cannot collide; invalid input or a hashing
  failure aborts with an error and leaves the previous live files in place.
  An existing `MQTT_PASSWORD` containing `:` or leading/trailing spaces is
  rejected (by the preflight, before anything changes) because
  `mosquitto_passwd` would store a different password; change it in the
  Control Panel first.
- Rollback: set `DISPLAYS_ENABLE=false` and run `sudo ./install.sh --config
  ... --start`; the ACL returns to administrator-only. Downgrading to a release
  without this feature: this release never modifies the legacy
  `config/mosquitto.conf`, so the older release finds it as it left it; its
  Mosquitto configuration points at the legacy
  `$VESSELSTACK_DATA/mosquitto/passwords` path and its installer recreates the
  files there, so the broker works again after the downgrade; the
  `config/mosquitto/`, `config/mosquitto-auth/` directories and
  `displays.secrets.json` are left in place. `vesselstackctl restore` extracts with `--numeric-owner` so the
  `root:1883` and `1883:1883` ownership survives a restore.
- Troubleshooting: if the broker log contains "owner is not mosquitto",
  "world readable" or "permission denied" for `/mosquitto/auth/...`, re-run
  `sudo ./install.sh --config /opt/vesselstack/config/vesselstack.env` to
  rewrite the files with the correct ownership. If sync reports that the image
  "could not be pulled", connect the boat to the internet once (or load the
  image with `docker load`) and run it again; nothing is changed before then.
  "refusing to use ... symbolic link" or "group or world writable" means the
  auth directory was tampered with: inspect and remove it, then re-run.
- Verify the upgrade path with `sudo tests/test-mosquitto-upgrade.sh`: a
  release-0.3.0 broker (single-file conf, legacy passwords, no ACL) must
  survive the new render plus `docker restart`, and only the container
  recreated from the real `compose.yml` mounts enforces the ACL.
- `tests/test-displays-envfile-systemd.sh` boots systemd in a privileged
  Debian container and checks that a Signal K token containing dollar, backtick, quote, backslash,
  hash, semicolon and percent characters and spaces reaches the service environment unchanged through
  `EnvironmentFile=` (systemd does not expand `$` or `%` there), and that
  `systemd-analyze verify` accepts the rendered unit. If the container cannot
  boot systemd it skips loudly; `python3 -m unittest` also covers a
  parser-equivalent check. `sudo tests/test-displays-bridge.sh` builds the
  environment with the installer's helper under `umask 077` and runs the bridge
  as uid 1000, so an unreadable environment is caught.
- Verify with `sudo tests/test-displays-mosquitto.sh` (real broker in Docker;
  as root it verifies the real ownership, otherwise a one-shot container copies
  the files into a Docker volume with that ownership first)
  and `python3 -m unittest discover -s tests -p 'test_displays*.py'`.
