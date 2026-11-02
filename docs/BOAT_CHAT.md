# Boat Chat setup and operations

Purpose: first-run access, settings, telemetry refresh, host metrics, and unit and timezone behavior for Boat Chat. Provider-specific configuration lives in the [Boat Chat provider guide](../boat-chat/README.md).

[Back to README](../README.md) · [Documentation index](README.md)

Open `http://HOST:8765`. Chat requires the access token
(`BOAT_CHAT_ACCESS_TOKEN` in `config/boat-chat.env`) and refuses requests without
it unless `BOAT_CHAT_ALLOW_OPEN=true` is set. The `local` provider returns gathered context without
sending it to an external model. Configure an optional provider in Settings.
Retrieve the settings token locally when needed:

```bash
sudo grep '^BOAT_CHAT_SETTINGS_TOKEN=' \
  /opt/vesselstack/config/boat-chat.env
```

Boat Chat retains this settings credential only for the current browser tab;
closing the tab clears it.

Boat Chat rejects oversized or malformed API requests and sends defensive
browser headers while remaining embeddable in the Home Assistant dashboard.
It also provides deterministic time-window and signal planning, durable local
sessions, answer feedback, selectable response modes, chart-ready evidence,
relative AIS context, recent-trip insights, and a local maintenance tracker.
Telemetry remains in canonical API/cache units; Boat Chat converts the
model-facing context, computed answers, metric cards, and chart labels at the
presentation boundary. Set `BOAT_UNITS=metric` for L/L/h, °C, kPa, hPa, and m;
knots and nautical miles remain unchanged. Switching the preference does not
rewrite stored telemetry or cached summaries.
Fuel-balance comparisons follow the same preference, including signed rate and
volume differences. Home Assistant history uses the selected entity's reported
source unit for chart values, metric cards, computed answers, and model context;
already-metric readings are preserved. Missing or unsupported source units are
left unchanged rather than assumed to be Fahrenheit or feet. This metadata comes
from the current entity state, so historical ranges spanning a change in the
entity's reporting unit need separate review.
History threshold analysis uses the same source-unit metadata as the charts:
for example, 20 °C and 30 °C contain one sample above 25 °C. Explicit query units
are converted to the measurement's source unit; omitted query units follow
`BOAT_UNITS`. Missing, unsupported, or incompatible units omit the threshold
count rather than guessing a conversion. Raw samples and cached values are
unchanged.
Percentage thresholds keep `%` as an explicit unit, including `50%`, `50 %`,
and `50 percent`, so a mixed SOC/voltage question compares only percentage
signals. Implicit barometer thresholds use inHg in US customary mode and hPa
in metric mode; engine-pressure defaults remain psi and kPa. An explicit unit
in the question always overrides these defaults.
The interface rate-limits each client and runs at most two chat requests at a
time so slow model calls cannot exhaust the Raspberry Pi.

The installer generates `BOAT_CHAT_ACCESS_TOKEN` (random, preserved on
re-install) and writes it to `config/boat-chat.env`. Chat, feedback, session,
live status, insights, and maintenance endpoints all require it in the
`X-Boat-Chat-Token` header; the web UI asks for it through **Connect** and keeps
it only in session storage, and the Telegram bridge sends it automatically. Find
it with:

```bash
sudo grep '^BOAT_CHAT_ACCESS_TOKEN=' /opt/vesselstack/config/boat-chat.env
```

An empty token **fails closed**: all of those endpoints return 401. A reviewed,
trusted installation can opt out by adding `BOAT_CHAT_ALLOW_OPEN=true` to
`config/boat-chat.env`; this key is deliberately not editable from the Settings
page. `BOAT_CHAT_REQUIRE_ACCESS_TOKEN` is retired and no longer disables
protection.

## Settings and configuration precedence

Boat Chat reads two files; both Boat Chat and the Telegram bridge resolve them
the same way (`boat-chat/env_config.py`):

1. **Settings-page overrides** (highest): `$VESSELSTACK_DATA/boat-chat/boat-chat.env`,
   selected by `BOAT_CHAT_ENV`. This is the only settings file the service user
   can write.
2. **Base configuration**: `config/boat-chat.env`, written by the installer and
   control panel and loaded by systemd into the environment.

Saving a value on the Settings page that equals the base value stores no
override. Saving a Boat Chat field in the control panel removes that key from
the override file so the new value takes effect, and the panel marks any field
that is currently overridden. Settings writes require `BOAT_CHAT_SETTINGS_TOKEN`
from any address; with no settings token they are accepted from loopback only
(private-LAN addresses are no longer trusted). `TELEGRAM_ALLOWED_CHAT_IDS` is a
secret setting and is never returned by `GET /api/settings`.

Settings updates are serialized and written atomically. Blank secret fields
keep the saved value; an explicit JSON `null` stores an empty override so an
inherited credential cannot reappear. Use the current settings token to
authenticate a save and the separate replacement field to rotate it. Tokens
remain in this browser tab's session storage. JSON write requests require
`Content-Type: application/json`; supplied browser origins must match the
request authority.

## Automatic telemetry refresh

`TELEMETRY_INDEXER_ENABLE=true` controls two schedules. The sample timer gathers
current telemetry and power observations five minutes after the previous run
finishes. The history timer refreshes retained summaries every thirty minutes.
The jobs have independent overlap locks, preserve the existing AIS and memory
index context, and use the installed service account and private state paths.
Their journal output contains counts and error classifications rather than
vessel readings or credentials.
Both installed services explicitly request quiet output, including history
refresh failures; manual indexer commands without `--quiet` still show detailed
results and should be treated as private telemetry.

With `TELEMETRY_INDEXER_ENABLE=false` in the installed vessel configuration,
`vesselstackctl start` stops both telemetry timers before stopping their running
jobs. A real stop failure makes the command fail instead of reporting a
successful configuration change; missing legacy units are tolerated. Setting
the option back to `true` and starting again starts both timers.
Threshold regressions run with `python3 -m unittest tests.test_metric_presentation`;
the mocked CLI lifecycle regressions run with `sudo tests/test-lifecycle.sh` on
Linux and do not control host services.

The installer enables the schedules for boot; **Install & start** or
`vesselstackctl start` starts them immediately. Render-only installation keeps
previously active schedules active. Backup, restore, and installation stop the
timers and running jobs before touching their state. Inspect or run them with:

```bash
systemctl list-timers 'vesselstack-telemetry-*'
sudo systemctl start vesselstack-telemetry-sample.service
sudo systemctl start vesselstack-telemetry-indexer.service
```

To disable automatic refresh, turn off the telemetry indexer in the Control
Panel and apply/start the configuration. For temporary maintenance, stop both
timers, then both services. Unknown Home Assistant distance units are excluded
from spatial power inference; configured integration outages fail the sample,
while missing optional entities remain unknown.

## Optional host metrics

Enable **Host metrics** in the Control Panel's Prometheus settings, save, and
use **Install & start**. Equivalently, set `NODE_EXPORTER_ENABLE=true` in the
installed vessel configuration and apply it with the reviewed installer.
The pinned exporter runs on the private Compose network without publishing
port 9100. Its read-only host mounts support CPU, memory, disk, filesystem,
load, and thermal collectors; host network-interface metrics are intentionally
excluded. Grafana's system-health dashboard includes optional host panels.
The CPU, memory, root-filesystem, and load panels select the provisioned
Prometheus datasource explicitly, even when InfluxDB is the Grafana default.

Set the option back to `false` and apply/start to remove the exporter and its
scrape target. Existing Prometheus data, retention, and listener settings are
preserved. Offline bundles must be rebuilt with this release so the exporter
image is available before enabling it offline.

**Apply configuration** followed by **Start** also applies exporter scrape
changes to a running Prometheus instance. The controller records the last
successfully applied configuration hash in `config/.prometheus-conf.sha256`;
render-only Apply does not advance it. Missing or changed hashes cause only
Prometheus to be recreated, and a failed recreation leaves the previous stamp
intact for retry. Unchanged starts do not force another recreation. The first
start after upgrading an installation without this stamp recreates Prometheus
once. A disabled Prometheus component is neither started nor marked applied.
This preserves its data volume, retention, and listener configuration and adds
no reload endpoint or published port.
The render-only Apply → installed Start regressions run with
`sudo tests/test-offline-install.sh` on Linux using fake external commands.

After updating an existing installation, rerun the reviewed installer to apply
the service and dashboard templates, then start the telemetry services. These
changes do not remove existing journal entries or rewrite retained telemetry.
To roll back, install the previous reviewed release; it restores the earlier
presentation, CLI behavior, and templates without a telemetry database migration.
When rolling back to a release without applied-configuration tracking, remove
`/opt/vesselstack/config/.prometheus-conf.sha256` (adjust for a custom install
root) so a later upgrade checks application again instead of reusing that stamp.
Focused regressions can be run from the repository root with:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_display_units tests.test_metric_presentation tests.test_node_exporter tests.test_telemetry_indexer
```

`BOAT_CHAT_WEB_SEARCH=true` enables the OpenAI Responses API web-search tool
when the OpenAI provider is selected. This can send the question and grounded
context to an external service and may incur provider charges; it is disabled
by default. Boat Chat sessions, audits, feedback, and maintenance tasks stay in
the private SQLite memory database
(`$VESSELSTACK_DATA/boat-chat/memory/boat_memory.sqlite`, power observations in
`$VESSELSTACK_DATA/boat-chat/data/power_tracking.sqlite`) and are excluded from
release packages. Settings values containing a newline, carriage return or NUL
byte are rejected (HTTP 400, nothing written), matching the control panel.

`vesselstackctl urls` reports the **real** bind state of each service from the
listening sockets (`ss -ltn`), so the host-networked SignalK (always) and Home Assistant
(unless limited by `HOMEASSISTANT_BIND_ADDRESS`) show up as listening on every interface
although their client URLs say `127.0.0.1`. It prints this machine's reachable addresses (`HOSTNAME.local`
and every non-loopback IPv4 address, ignoring Docker bridges and veth links) as
copy-pasteable URLs for the services that other devices can reach, marks
loopback-only services "this device only" with a one-line hint on how to change
that, and falls back to the configuration (read from `config/boat-chat.env`,
`config/control-panel.env` and the Compose bind variables, never by sourcing
service-writable files) when `ss` is unavailable or a published port is not
visible to it, saying so on the line.

**Memory index.** Boat Chat answers from a private SQLite index of its bundled
knowledge files (`boat-chat/knowledge/*.md`, the facts file and the boat-chat
documentation). Until that index has content `/health` reports
`readiness.layers.memory = {ready: false, chunks: 0}` and Boat Chat is "not
ready". The installer builds it (`scripts/memory-index.sh`, `memory_index.py
ensure`) as the **service user** with the unit's minimal environment
(`BOAT_CHAT_STATE_DIR`, `BOAT_CHAT_ENV`, plus `BOAT_CHAT_EMBEDDING_MODEL` and
`BOAT_CHAT_MEMORY_DB` only if set in `config/boat-chat.env`, validated and never
sourced), so the database is service-user-owned and root never creates SQLite
side files in the state directory. `ensure` is idempotent: it rebuilds only when
the index is missing, empty or the hash of the source documents changed. It runs
on every installer run (including upgrades), on `vesselstackctl start`, and at
the start of every telemetry-indexer run (so a release that changes the
knowledge files refreshes the index on the next history run). A failed build prints
a `WARN` with the exact manual command, never aborts the install, and
`vesselstackctl doctor` fails until it is fixed. `sudo vesselstackctl reindex`
rebuilds it on demand.

Hardening of service-writable paths: the control panel clears stale Settings
overrides without following symlinks (it refuses a symlinked override file,
writes through a random `O_EXCL` temporary file and sets mode/owner on the open
descriptor).

**Data-directory ownership (no check-then-use race).** `VESSELSTACK_DATA` is
`root:<service group>` mode 0750: the service user can traverse it but cannot
create, rename, replace or symlink anything directly in it. Only the leaf mount
roots belong to the accounts that must write them: `influxdb` (uid/gid 1000),
`grafana` (uid 472, gid 0) and `boat-chat` (service user; native SignalK state
`signalk` likewise; with `SIGNALK_MODE=docker` `signalk` is uid/gid 1000, the
`node` user of the signalk-server image, created before `compose up`; a
root-owned one left by Docker is chowned to 1000:1000 on rerun, non-recursively
when empty, otherwise recursively but never through symlinks, and the change is
printed; a symlink inside it makes the installer refuse). Intermediate directories the installer writes through
(`homeassistant`, `homeassistant/blueprints/...`) must be root-owned and not
group/world-writable. Before touching anything the installer refuses, with a
message naming the path, a symlink at any managed entry (`influxdb`, `grafana`,
`boat-chat`, `signalk`, `mosquitto`, `homeassistant/...`) or a managed
intermediate directory that is not root-owned; fix it as the message says and
rerun. Because the service user cannot alter the parent, a symlink cannot be
swapped in between the check and the `install -d`/`chown`. Legacy runtime
state is copied and chowned in a root-only staging directory and published with
`rename(2)` (which never follows a symlink at the destination); a destination
that already exists or is a symlink sends the legacy copy to quarantine instead.
Upgrade: installs from earlier 0.4.0 builds had a service-owned data root; the
first run of this installer takes ownership of the data root (not recursive:
existing database files keep their owners) and moves old quarantine directories
out of it. Rollback: `chown -R` is not needed; reinstalling the previous release
makes the data root service-owned again, and restoring a backup keeps recorded
owners.

Security layout (0.3.x hardening): the installed application code, its Python
virtualenv and the generated `boat_facts.json` are owned by `root` and are not
writable by the service account (`boat_facts.json` is `root:SERVICE 0640`).
Everything Boat Chat writes at runtime (memory/session database, power database,
Telegram state and the Settings-page `boat-chat.env`) lives under
`$VESSELSTACK_DATA/boat-chat` (default `/opt/vesselstack-data/boat-chat`,
selected by `BOAT_CHAT_STATE_DIR`). The chat, Telegram and telemetry-indexer
units run with `ProtectSystem=strict`, `ProtectHome=true`, `NoNewPrivileges=true`,
`PrivateTmp=true` and `ReadWritePaths=$VESSELSTACK_DATA/boat-chat`. Consequences:
`VESSELSTACK_ROOT`/`VESSELSTACK_DATA` may not be under `/home` or `/root`, and the
`codex_cli`/`claude_cli` providers cannot use binaries or credentials kept in a
home directory (install them system-wide and set `BOAT_CHAT_CODEX_BIN` /
`BOAT_CHAT_CLAUDE_BIN` in the root-owned `config/boat-chat.env`; these two
settings are no longer accepted from the web Settings page or the Settings
`boat-chat.env`). On the first install/upgrade the installer stops the chat
workers, moves any legacy `boat-chat/memory`, `boat-chat/data` and
`boat-chat/boat-chat.env` into the new state directory (refusing symlinks),
recreates the virtualenv as root and restarts the services only with `--start`.
Rollback: restore the pre-update backup (`vesselstackctl restore`) or reinstall the
previous release; the legacy files are moved, not copied, so keep the backup
taken by `vesselstackctl update`. `systemd-analyze verify` was not run in the
development environment; run it on the Pi after upgrading
(`systemd-analyze verify /etc/systemd/system/vesselstack-chat*.service`).


## Timezone and units

By default, Boat Chat uses the configured `BOAT_TIMEZONE` for query windows and
displayed timestamps and `BOAT_UNITS` for presentation. A user may request a
grounded display timezone or unit conversion; that changes presentation only,
not the application's resolved query window. Source instants, authoritative
unit labels, and canonical cache values are preserved. Apply configuration and
restart the service after changing the timezone. Updated local knowledge is
picked up by the existing telemetry index refresh path.
