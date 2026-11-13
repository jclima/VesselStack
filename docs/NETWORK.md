# Network access and security

Purpose: who can reach which VesselStack service from other devices, what the firewall and bind settings enforce, and the minimum network hardening to apply.

[Back to README](../README.md) · [Documentation index](README.md)

## Reaching VesselStack from other devices

The installer asks "Who should be able to reach the dashboards?" (terminal wizard
question in the "Who can reach what" section; `--set LAN_ACCESS=local|dashboards|all`
for unattended installs) and `sudo vesselstackctl lan` changes it later. The answer is
a preset that sets the real bind settings; `LAN_ACCESS` itself is never written to a file.

| Preset | Grafana, Homepage (`VESSELSTACK_WEB_BIND_ADDRESS`) | Boat Chat (`BOAT_CHAT_HOST`) | InfluxDB, Prometheus, MQTT (`VESSELSTACK_DATA_BIND_ADDRESS`) | Home Assistant (`HOMEASSISTANT_BIND_ADDRESS`) |
| --- | --- | --- | --- | --- |
| `local` | `127.0.0.1` | `127.0.0.1` | `127.0.0.1` | `127.0.0.1` |
| `dashboards` (default, recommended) | `0.0.0.0` | `0.0.0.0` | `127.0.0.1` | `0.0.0.0` |
| `all` | `0.0.0.0` | `0.0.0.0` | `0.0.0.0` | `0.0.0.0` |

The Control Panel (`CONTROL_PANEL_HOST`) defaults to `127.0.0.1` and is independent
of every preset. Choose network access during setup or run
`sudo vesselstackctl panel-access lan` to share only its administration listener;
`panel-access local` returns it to loopback. The dashboard presets leave this choice
unchanged. `all` also opens
Prometheus, which has **no login**, plus InfluxDB (token) and MQTT (password) to every
device on the network; turn the firewall on and prefer Ethernet or a VPN as the way in.

**What is enforced and what is not (host-networked containers).** Signal K (3000) and Home
Assistant (8123) run with `network_mode: host`, so Compose port bindings cannot restrict them.

- **Home Assistant: enforced by `local`.** Home Assistant binds the address in
  `http: server_host`. With `HOMEASSISTANT_BIND_ADDRESS=127.0.0.1` the installer (and
  `vesselstackctl lan`/`ha-http sync`) adds one block to
  `${VESSELSTACK_DATA}/homeassistant/configuration.yaml`, delimited by the lines
  `# BEGIN VESSELSTACK MANAGED http bind ...` and `# END VESSELSTACK MANAGED http bind`,
  containing `http:` / `server_host: 127.0.0.1`. Nothing outside those markers is ever
  changed. On a fresh data directory the file is created with Home Assistant's stock content
  (plus empty `automations.yaml`, `scripts.yaml`, `scenes.yaml`). Home Assistant rejects a
  second top-level `http:` key, so if your `configuration.yaml` already has its **own**
  `http:` section VesselStack changes nothing in it, still applies the other settings, and
  prints the exact line to add inside your section (`server_host: 127.0.0.1`) and the restart
  command; `vesselstackctl doctor` then warns until you do. An already-running container gets
  the new file checked with Home Assistant's `check_config` (the previous file is restored if
  it fails) and is the only container restarted. `dashboards` and `all` remove the block again.
  *Security note:* the Home Assistant container runs as root and can write that directory
  while VesselStack edits the file as root, so every read, write, restore and removal goes
  through one small Python helper that opens the directory with `O_NOFOLLOW` (refusing
  symlinked or group/world-writable directories), opens the file with `O_NOFOLLOW` and
  requires a plain single-link regular file, and replaces it with a temporary file in the same
  directory descriptor plus `rename(2)`, never following a link at the destination. A symlink,
  hard link, unusual file or symlinked directory makes `sync` change nothing and print the
  manual instructions (`ha-http status` shows `unsafe`). The pre-change backup is kept in the
  root-only `/var/lib/vesselstack/ha-http-backup` (mode 0700, outside the container-writable
  tree) and restored through the same helper; if the file or directory was swapped while
  `check_config` ran, the restore is refused, the backup stays there and the message says so.
  Rollback of this hardening: reinstall 0.4.3 (it has no helper; the managed block itself is
  unchanged).
  Only the web/API port follows the setting; other Home Assistant listeners (for example
  discovery or HomeKit integrations you add) are outside it. Boat Chat, the telemetry indexer
  and the installer already reach it on `http://127.0.0.1:8123`, and Home Assistant reaches
  MQTT and InfluxDB on loopback, so nothing else depends on the LAN address.
- **Signal K: NOT restricted by any preset (documented limitation).** Verified against the
  pinned image (`signalk-server` v2.27.0): the server starts its HTTP/WebSocket listener with
  `server.listen(port)` and logs `signalk-server running at 0.0.0.0:3000`; there is no
  environment variable, `settings.json` key or command-line option for the bind address (only
  the optional TCP stream has `TCPSTREAMADDRESS`). Running it in a bridge network would
  restrict the port but breaks SocketCAN and local NMEA feeds, so it is not done. The only
  VesselStack control is the firewall's strict mode (`VESSELSTACK_FIREWALL_STRICT=true`),
  which drops port 3000 on the untrusted interface only; it is not enabled by a preset. On
  a trusted network, or with the firewall off, Signal K stays reachable by other devices
  even under `local`. `sudo vesselstackctl urls` shows how every service really listens.

```bash
sudo vesselstackctl lan                      # show the current preset and its values
sudo vesselstackctl ha-http status           # Home Assistant bind setting and configuration.yaml state
sudo vesselstackctl ha-http sync             # re-apply HOMEASSISTANT_BIND_ADDRESS (repair drift)
sudo vesselstackctl lan local --dry-run      # print what would change, change nothing
sudo vesselstackctl lan dashboards           # apply (root); rewrites the keys atomically
sudo vesselstackctl lan all --yes            # 'all' needs --yes
```

`lan` rewrites `VESSELSTACK_WEB_BIND_ADDRESS`, `VESSELSTACK_DATA_BIND_ADDRESS` and
`HOMEASSISTANT_BIND_ADDRESS` in `config/vesselstack.env` and `BOAT_CHAT_HOST` in `config/boat-chat.env` (temporary file in
the same directory, owner and mode kept, symlinks refused; a failure restores both files),
then runs `compose up -d` (only containers whose published ports changed are recreated) and
restarts `vesselstack-chat.service`; `--dry-run` prints the planned Home Assistant file action too.
A `BOAT_CHAT_HOST` saved from Boat Chat's Settings page
wins over `boat-chat.env`; `lan` warns when it differs. Rollback: run `lan` with the previous
preset (`dashboards` removes the managed Home Assistant block). Manual rollback: delete the
lines between the BEGIN/END markers in `configuration.yaml`, set
`HOMEASSISTANT_BIND_ADDRESS=0.0.0.0` in `config/vesselstack.env`, and `docker restart
vesselstack-homeassistant`. The installer re-applies the setting on every run and
upgrade: with the default `0.0.0.0` it only removes a block it previously added.

**Firewall caveat (`wlan0`).** With `VESSELSTACK_FIREWALL_ENABLE=true` the interface
`VESSELSTACK_UNTRUSTED_INTERFACE` (default `wlan0`) is untrusted: other devices that reach the
Pi over it cannot open Grafana, InfluxDB, Prometheus or MQTT (in strict mode, anything except
the allow-listed TCP ports), whatever the preset says. If the Pi is on your network over
`wlan0`, that locks every other device out. `lan` and the wizard say so; `lan` refuses to open
the network in that state unless you pass `--allow-firewall-conflict`. Pick another untrusted
interface (for example a hotspot or modem) or leave the firewall off on a trusted network.

Test from a laptop on the same network (replace `PI` with the address that
`sudo vesselstackctl urls` prints):

```bash
curl -sS -o /dev/null -w 'grafana %{http_code}\n' http://PI:43000/login   # 200 when reachable
curl -sS -m 5 -o /dev/null -w 'influx %{http_code}\n' http://PI:8086/health || echo 'influx: not reachable (expected unless preset all)'
```

## Home page host names

Homepage answers only to the host names and addresses in `HOMEPAGE_ALLOWED_HOSTS`
(managed; the Pi's `.local` name, host name, current IPv4 addresses and loopback, plus
`HOMEPAGE_ALLOWED_HOSTS_EXTRA`). The list is refreshed at every start and, because Docker
restarts the container with the value it was created with, again at every boot by
`vesselstack-homepage-refresh.service` (`vesselstackctl homepage refresh`), which
recreates the container only when the list changed (for example after a new DHCP lease).
IPv6 addresses are not added: use the `.local` name, an IPv4 address or
`HOMEPAGE_ALLOWED_HOSTS_EXTRA`. A request with any other Host header gets `400`.

## Network security

Compose publishes InfluxDB `8086`, Prometheus `9090`, and MQTT `1883` on
`127.0.0.1` by default (`VESSELSTACK_DATA_BIND_ADDRESS`). Grafana `43000` and
Homepage `80` are published on every interface by default
(`VESSELSTACK_WEB_BIND_ADDRESS=0.0.0.0`) because the documented dashboards are
used over the boat LAN; set it to `127.0.0.1` to keep them local. With the
firewall enabled (see below), Grafana and Homepage are dropped on the untrusted
interface only; every other interface still reaches them. Grafana still
reaches InfluxDB and Prometheus by service name, and host-networked Home
Assistant reaches MQTT on loopback. AIS ports are unchanged. At minimum:

- restrict Grafana `43000` and Homepage to trusted management networks, and set
  `VESSELSTACK_DATA_BIND_ADDRESS` to a trusted LAN/VPN IPv4 address only if
  external devices must publish to MQTT or InfluxDB;
- keep Home Assistant, Signal K, and Boat Chat off the public internet unless
  protected by an authenticated reverse proxy;
- keep the root-privileged Control Panel loopback-only or behind a trusted VPN
  and authenticated proxy;
- use ZeroTier, Tailscale, or WireGuard for remote access (VesselStack has optional Tailscale support,
  off by default: `sudo vesselstackctl remote enable`, see
  [Remote access with Tailscale](../README.md#remote-access-with-tailscale)). Tailscale reaches every service
  bound to `0.0.0.0` on the boat, not only the Control Panel, so restrict it with Tailscale ACLs. The firewall
  rules match only the untrusted interface and never mention `tailscale0`; under strict mode UDP `41641` may be
  added to `VESSELSTACK_FIREWALL_ALLOW_UDP` for direct connections; and
- keep `/opt/vesselstack/config/*.env` readable only by root or the required
  service account.

VesselStack does not install firewall rules because interface names and trusted
network ranges differ between boats by default. To enable its conservative
untrusted-interface policy, set:

```bash
VESSELSTACK_FIREWALL_ENABLE="true"
VESSELSTACK_UNTRUSTED_INTERFACE="wlan0"
```

Preflight requires that interface to exist. The policy blocks MQTT, InfluxDB,
Prometheus, and Grafana administrative/data ports arriving on that interface,
including Docker-forwarded traffic, while leaving Home Assistant, Signal K,
Boat Chat, AIS viewer, HTTP/HTTPS, loopback, VPN, and outbound traffic intact.
Review the policy without root or any change using
`vesselstack-firewall print` (renders the exact `iptables-restore` payloads).
Chain contents are replaced atomically (`iptables-restore --noflush`), so an
existing chain is never briefly empty. The host (INPUT) rules are applied first;
`DOCKER-USER` only exists once Docker is running, so the service waits up to 60 s
for it (`TimeoutStartSec=180`) and, if Docker is still absent, creates the chain
itself. Until Docker starts no container ports are published, so that window
exposes nothing. A missing untrusted interface no longer aborts: rules are
installed anyway (with a warning) and take effect when the interface appears.

Strict mode (opt-in, default off) is for boats where nothing should be reachable
over Wi-Fi except what you list:

```bash
VESSELSTACK_FIREWALL_STRICT="true"
VESSELSTACK_FIREWALL_ALLOW_TCP="22"        # comma-separated ports or LOW:HIGH ranges
VESSELSTACK_FIREWALL_ALLOW_UDP=""
```

On the untrusted interface strict mode drops everything except replies to
connections the boat started, ICMP/ICMPv6, DHCP client replies and the
allow-lists. That covers host-networked Signal K `3000`, AIS, Boat Chat `8765`
(INPUT) and published container ports such as Homepage `80` and Grafana
(`DOCKER-USER`; Homepage is matched by its container port `3000`, whatever
`HOMEPAGE_PORT` is, so replies to the Pi's own outbound HTTP on port 80 are never
dropped). The allow-list therefore opens
ports on both paths; the default `22` only lets SSH in. Keep Ethernet, a VPN or
console access before enabling it: a wrong interface name or allow-list can lock
you out of Wi-Fi access, and remote `ssh` on that interface needs `22` listed.
Rollback: set `VESSELSTACK_FIREWALL_STRICT="false"` and
`sudo systemctl restart vesselstack-firewall.service` (or `vesselstack-firewall
remove`). IPv6 is restricted only for INPUT; Docker's IPv6 forwarding is not
managed. The rules were rendered and tested but not loaded into a live kernel
in the development environment; confirm with `sudo iptables -S
VESSELSTACK-INPUT` on the Pi.

Disable and remove its chains with:

```bash
sudo systemctl disable --now vesselstack-firewall.service
sudo vesselstack-firewall remove
```
