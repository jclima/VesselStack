# Control Panel

Purpose: how to reach, secure, and use the loopback-first Control Panel. Lab acceptance steps are in [Control Panel lab testing](CONTROL_PANEL_TESTING.md).

[Back to README](../README.md) · [Documentation index](README.md)

The Control Panel is the browser administration surface for VesselStack. It
shows container and systemd state, starts/stops/restarts individual components,
edits settings by component, runs installer preflight, renders or installs the
saved configuration, provisions InfluxDB history, creates consistent backups,
and applies an already downloaded, reviewed, and root-controlled release.

It deliberately stays available when the managed stack is stopped. The
`vesselstackctl` command remains the recovery path if the panel is unavailable.

To open the panel directly from a laptop or phone on your trusted network:

```bash
sudo vesselstackctl panel-access lan
```

Open the address printed by the command (normally `http://PI_ADDRESS:8780`).
This enables only the panel listener; it does not expose the rest of the stack.
The default remains loopback-only, and `sudo vesselstackctl panel-access local`
restores it. Local-only users can still use a trusted VPN or an SSH tunnel
(`ssh -L 8780:127.0.0.1:8780 boat-admin@HOST`).

Retrieve the generated token on the Pi and enter it on the unlock screen:

```bash
sudo grep '^CONTROL_PANEL_TOKEN=' \
  /opt/vesselstack/config/control-panel.env
```

The token is stored in browser session storage, cleared by the **Lock** button
or when the tab closes, and sent only in the `X-VesselStack-Token` request
header. Configuration APIs never return secret values: they report only whether
a secret is configured, and a blank submitted secret preserves its current
value. Use the explicit **Clear stored value** checkbox to remove one. Every
configuration save creates mode-600 rollback copies under
`/opt/vesselstack-data/control-panel/config-backups/`. Operation commands are
fixed allowlisted argument arrays; the panel does not expose an arbitrary shell.
While one operation is running, other operation and component controls are
disabled to prevent overlapping lifecycle changes.

Clearing an optional integration token disables that credential. Clearing a
required installer-managed password or token causes the installer to generate
a replacement the next time configuration is applied; dependent clients must
then be updated with the new credential.

Saving settings writes mode-600 configuration atomically but does not restart
services. Review changes, run **Preflight**, then use **Apply configuration** or
**Install & start**. Hardware probes and the existing Signal K connectivity
check still gate installer actions. Listener changes require a shell restart:

```bash
sudo systemctl restart vesselstack-control-panel.service
```

The panel intentionally skips restarting itself while an operation request is
in flight. After applying a release that changes Control Panel code, run the
same restart command to load the new backend.

The Telegram and telemetry-indexer toggles take effect after **Install &
start** (or another installer run). Configure the Telegram bot token and allowed
chat IDs in the Telegram section before enabling its worker. Disabled workers
are shown as disabled and cannot be started accidentally from the component
controls. Operators can
inspect these workers with:

```bash
sudo vesselstackctl logs telegram
sudo vesselstackctl logs indexer
systemctl list-timers vesselstack-telemetry-indexer.timer
```

The service runs as root because Docker, systemd, installation, backup, and
hardware operations require host privileges. Keep the token private, retain
loopback binding unless a trusted authenticated access layer is in place, and
never expose port `8780` directly to the internet. To disable the panel without
affecting telemetry services:

```bash
sudo systemctl disable --now vesselstack-control-panel.service
```

Re-enable it with `sudo systemctl enable --now
vesselstack-control-panel.service`. CLI configuration and lifecycle commands
continue to work while it is disabled. Optional workers and schedules follow the
same guard: Control Panel start/restart is rejected until its enable flag is set.
Offline preflight requires the node-exporter image only when
`NODE_EXPORTER_ENABLE=true`; the default-disabled profile does not add it to the
required image set.
