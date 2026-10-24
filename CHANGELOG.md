# Changelog

All notable changes to VesselStack are documented here. The project follows
Semantic Versioning.

## 1.4.0 - 2026-10-24

- Security: the data directory is no longer service-writable. `VESSELSTACK_DATA` is `root:<service group>` 0750 and only leaf mount roots (`influxdb` 1000, `grafana` 472, `boat-chat`/`signalk` service user) are owned by their writers, so the service user cannot swap a path component for a symlink between the installer's check and its `install -d`/`chown` (TOCTOU). The installer refuses symlinks at managed entries and non-root-owned managed intermediate directories (`homeassistant/...`), and migrates legacy runtime state through a root-only staging directory published with `rename(2)`. Upgrades take ownership of the data root on the first run (not recursive).
- Security: the legacy quarantine moved out of the data tree to `/var/lib/vesselstack/quarantine` (root 0700, lstat-verified not a symlink); quarantine directories from earlier 1.4.0 builds are relocated there.
- Security: root no longer executes `scripts/deploy-chat.py` from the unprivileged-owned checkout. `install.sh` installs `vesselstack-update` and the helper root-owned (0755) under `/usr/local/libexec/vesselstack`; as root the launcher runs only that copy and refuses an `update.env` or helper whose path chain is not root-owned and free of group/world write access (and an `update.env` inside `SOURCE_DIRECTORY`). The helper also refuses to run as root from an unprivileged-controlled location. The first copy requires running the installer once; non-root home-lab runs still use the checkout helper.
- Added: `vesselstackctl update --offline` (or `VESSELSTACK_OFFLINE=true`) passes offline mode to the release's preflight and install. New `tests/test-update-launcher.sh` and CI step.
- Security: `scripts/deploy-chat.py` no longer runs `git archive` or `git verify-*` inside the unprivileged-owned checkout. Root fetches the pinned remote (or imports the checkout's refs over file transport) into its own root-owned bare mirror (`UPDATE_MIRROR_DIR`, default `/var/lib/vesselstack/update-mirror`, mode 0700, fsck on fetch), verifies the signature there with the fixed git configuration (attributes file, hooks, fsmonitor disabled; system/global config and inherited `GIT_*` variables ignored) and exports the tree as raw blobs (`git ls-tree -r -z` + `git cat-file blob`), refusing symlinks, submodules, unsafe paths and modes other than 100644/100755. Tests still run on the exported tree and the HEAD/clean/tree re-check before apply is kept. New `vesselstack-update` setting `UPDATE_MIRROR_DIR`.
- Changed: `install.sh` is transactional. The Python environment is built first in a staging virtualenv; env files, units, code and `installed-version` are only rewritten after it succeeds, and the version marker is written last. A failed or offline run no longer leaves a half-applied install. The venv is now readable (not writable) by the service user.
- Security: offline and online Python installs are supply-chain checked. `install.sh` installs from the release's hash-pinned `boat-chat/requirements.lock` with `pip --only-binary=:all: --require-hashes` (no sdists, no `setup.py` as root; online and offline). `prepare-offline-bundle.sh` builds the wheelhouse from the lock (`pip download -r lock --require-hashes`) and records `lock_sha256` in `BUNDLE_INFO`. Before installing or loading, the bundle must match its manifest exactly (no unlisted files or symlinks), name this release and this release's lock hash, contain only wheels whose sha256 the lock lists, and, as root, be root-owned and not group/world-writable (wheelhouse and image tarballs). A missing lock is refused before anything changes. New `scripts/update-requirements-lock.sh` (Linux aarch64/x86_64, Python 3.11-3.13), `tests/test_requirements_lock.py`, and `tests/test-offline-install.sh` cases for tampered/extra wheels, sdists, missing lock, manifest mismatch and non-root-owned bundles. `scripts/deploy-chat.py` treats a changed lock as a dependency change.
- Added: offline installs. `install.sh --offline` / `VESSELSTACK_OFFLINE=true` uses `pip install --no-index --find-links $VESSELSTACK_WHEELHOUSE`, requires images to be loaded already (clear error listing missing images and the `docker load` hint) and starts with `docker compose up -d --pull never`. `scripts/prepare-offline-bundle.sh` builds (wheelhouse with configurable platform/Python, `docker save` of every Compose and Mosquitto image, `BUNDLE_INFO`, `MANIFEST.sha256`), verifies (`--verify`) and loads (`--load`) the bundle.
- Changed: the migration-path check runs in preflight (`install.sh --dry-run` reports it) before any file is overwritten. `scripts/migrate.sh` is data-driven: any upgrade from 0.1.0 or newer is accepted, optional `scripts/migrations/<version>.sh` steps run in version order, and the marker is only written on success. Downgrades are refused with an explicit "not supported" message (restore a backup instead).
- Changed: `build-package.sh` packages `git archive HEAD` only (untracked/ignored files such as `.venv` or `boat-chat/boat_facts.json` cannot leak), embeds `COMMIT` and `BUILD_INFO`, prints the sha256 and commit, warns about uncommitted tracked edits (`VESSELSTACK_BUILD_REQUIRE_CLEAN=true` fails) and refuses to run outside a git checkout. Covered by `tests/test-build-package.sh`.
- Fixed: SocketCAN on virtual CAN. `vcan*` interfaces get `ip link set <if> up` only (no `type can`, bitrate or restart-ms), and with `VESSELSTACK_CAN_CREATE_VCAN=true` the unit loads the `vcan` module and creates the interface. Home-lab `canplayer` replay flow documented. Physical `can*` behaviour is unchanged. Covered by `tests/test-socketcan.sh`.

- Security: the control panel no longer follows symlinks or predictable temp names when clearing Settings overrides in the service-writable state directory (`O_NOFOLLOW`, `mkstemp`, fd-based chmod/chown); the installer refuses symlinked managed entries under the data directory before root creates, chowns or writes there.
- Security: `vesselstackctl restore` stages and verifies the archive, rejects setuid/setgid/sticky members, swaps trees by rename and keeps `*.pre-restore-STAMP` copies instead of extracting over the live tree, then runs the restored bundled installer. Backups also include unit files, `vesselstackctl`, `vesselstack-firewall` and `update.env`, refuse unmounted `/media` and `/mnt` destinations (`--allow-unmounted`), and warn on same-filesystem destinations.
- Security: firewall rules load atomically (`iptables-restore --noflush`), the service waits for/creates `DOCKER-USER`, a missing interface no longer skips the policy, `vesselstack-firewall print` renders rules, and opt-in `VESSELSTACK_FIREWALL_STRICT` with `VESSELSTACK_FIREWALL_ALLOW_TCP/UDP` blocks everything else on the untrusted interface.
- Security: the setup wizard requires `application/json`, a loopback `Host`/`Origin` and a per-run `X-Wizard-Token` (CSRF and DNS-rebinding defence).
- Fixed: Boat Chat settings reject newline/CR/NUL values; README SQLite paths corrected; `vesselstackctl urls` shows effective bind addresses; CI no longer runs the control-panel and wizard suites twice.

- Security: install.sh quarantines legacy Boat Chat state it cannot migrate into a root-only `boat-chat-legacy-<timestamp>` directory instead of leaving it in the code tree (never deletes data).
- Security: `scripts/deploy-chat.py` verifies signatures with a fixed git config (hooks off, no repo-local overrides, system/global config ignored) against a root-controlled `VERIFY_GNUPGHOME` / `VERIFY_ALLOWED_SIGNERS`; checker, tests and deployed bytes come from the verified commit object exported to a private tree, with HEAD/clean-tree/tree re-checks after tests and before apply.
- Fixed: every generated secret (InfluxDB, Grafana, MQTT, tokens) is preserved from the installed config when the input says GENERATE; Mosquitto credentials are updated in place only when changed instead of `-c` resetting the file; init-only secrets are read-only in the control panel.
- Fixed: InfluxDB bootstrap waits (bounded) for `/health`, treats `bucket list` errors as errors (retry, then fail) instead of creating buckets, passes the token via `INFLUX_TOKEN` not argv, and the downsample task averages numeric fields only (existing task updated).
- Added: `INFLUXDB_RAW_RETENTION` (default 30d on new installs; existing buckets only change when set explicitly) and a `vesselstackctl doctor` data-disk usage warning above 80%.
- Changed: scoped read-only Influx token deferred (see README).

- Security: update helper verifies source signatures, pins origin and runs tests/checker as the checkout owner instead of root; home-lab opt-in `ALLOW_UNSIGNED_SOURCE`.
- Security: control-panel release apply validates ownership/permissions/symlinks over the whole release tree.
- Security: application code, virtualenv and facts are root-owned; runtime state moves to `$VESSELSTACK_DATA/boat-chat` (`BOAT_CHAT_STATE_DIR`); chat/Telegram/indexer units gain `ProtectSystem=strict`, `ProtectHome=true` and `ReadWritePaths`; CLI binary paths are no longer web-settable.

- Security: Boat Chat fails closed without `BOAT_CHAT_ACCESS_TOKEN` (installer now generates and preserves it; `BOAT_CHAT_ALLOW_OPEN=true` is the explicit opt-out; `BOAT_CHAT_REQUIRE_ACCESS_TOKEN` retired); settings writes no longer trust private-LAN addresses; `TELEGRAM_ALLOWED_CHAT_IDS` is a secret setting.
- Security: Compose publishes InfluxDB, Prometheus and MQTT on `127.0.0.1` by default (`VESSELSTACK_DATA_BIND_ADDRESS`); Grafana/Heimdall bind is configurable (`VESSELSTACK_WEB_BIND_ADDRESS`, default unchanged).
- Changed: one `BOAT_CHAT_ENV` settings-override file shared by Boat Chat and Telegram, with documented precedence over `config/boat-chat.env`; control-panel saves clear stale overrides.
- Add a configured one-command application update/preview/rollback launcher.
- Restart an active Telegram worker when its code changes and retain the restart plan for rollback.

### Added

- Trustworthy owner overview, observation timestamps and connection recovery.
- Per-answer evidence, safe text rendering, conversation restoration and Telegram follow-ups.
- Speed-band fuel/RPM report with matched sample support and configurable history bucket.
- Public-tree privacy checks, ignored runtime facts and a separate-checkout development runbook.
- Optional application-only deployment preview, backup, readiness verification and guarded rollback.

### Fixed

- Match fuel, RPM and SOG minutes; reject invalid inputs, disclose coverage and bypass old cached calculations.
- Enforce both-engine filtering without an RPM band and preserve integration settings during provider-settings saves.
- Honor lifecycle readiness flags while retaining configured service URLs, schemas, timezone and default access protection.
- Keep computed evidence for complex explanations and distinguish missing/stale status from normal state.

## 1.3.0 - 2026-08-28

### Added

- Added deterministic query planning for exact time windows, signal selection,
  extrema, event counts, and port/starboard RPM-band comparisons.
- Added durable per-client chat sessions, request audit metadata, answer
  feedback, and a local maintenance-task tracker in the existing private Boat
  Chat SQLite database.
- Added authenticated status and insights surfaces with vessel cards, relative
  AIS bearing, overdue maintenance, recent-trip context, chart-ready evidence,
  follow-up actions, and selectable concise/explain/diagnose/checklist modes.
- Added optional OpenAI Responses web search and regression coverage for the
  query planner, session store, and HTTP API flow.

### Improved

- Redesigned Boat Chat for responsive dashboard use with navigation tabs,
  proactive browser alerts, trip-report export, richer evidence display, and
  session-aware Telegram follow-ups.
- Added per-client request throttling and a two-request concurrency bound.
- Made Boat Chat time-window planning honor the configured vessel timezone.

## 1.2.0 - 2026-08-25

### Added

- Added a loopback-only, dependency-free first-run wizard that inventories the
  host and writes a complete mode-600 installer configuration.
- Added an end-to-end Raspberry Pi 5 installation and commissioning runbook,
  illustrated wizard walkthrough, and marine power/NMEA 2000 bill of materials.

### Safety

- Documented galvanic isolation, single-source Pi power, GPIO HAT stacking,
  backbone termination, powered-down connection, and CAN error-counter checks.

## 1.1.0 - 2026-08-25

### Added

- Added a loopback-first, token-authenticated Control Panel for component
  status and lifecycle, redacted configuration, installer preflight/apply,
  history provisioning, backups, and reviewed release updates.
- Added a bundled installer snapshot so panel operations do not depend on a
  persistent Git checkout.

### Security

- Control Panel commands use fixed argument allowlists and serialized
  operations; arbitrary shell execution is not exposed.
- Secret settings are write-only through the API, configuration writes are
  atomic and mode 600, and privileged installation paths remain read-only in
  the browser UI.

### Improved

- Added intuitive `doctor`, `urls`, `version`, and scoped `logs` commands.
- Made status checks honor configured service URLs and ports and report a
  concise component summary.
- Made every Docker-published port configurable and isolated service tests from
  existing VesselStack installations.
- Hardened backup verification against traversal, escaping links, device nodes,
  and FIFOs before restore.
- Made Control Panel tokens tab-scoped with an explicit Lock action, added
  rollback copies and all published-port settings to its configuration UI, and
  restricted web-triggered updates to root-controlled staged releases.
- Added packaged, hardened Telegram and telemetry-indexer units with explicit
  enable settings and complete lifecycle/status/log handling.
- Made Control Panel component state/actions honor the optional-worker toggles
  and corrected installed Boat Chat service guidance.
- Added installer preflight validation for Boat Chat/Control Panel listeners
  and duplicate host ports across enabled services.
- Limited the Boat Chat settings credential to browser session storage so it is
  cleared when the tab closes.
- Bounded Boat Chat request bodies, returned client errors for malformed JSON,
  and added no-store/nosniff/CSP response headers without breaking dashboard embedding.
- Rejected nested or symlink-aliased backup destinations that could make an
  archive recursively include itself.
- Replaced the aggregate container status with per-service state for every
  required and enabled Compose component.
- Made `vesselstackctl start` perform all first-start prerequisites, including
  hardware/firewall setup, MQTT credentials, and InfluxDB history provisioning.
- Updated CI checkout to the Node.js 24-based v6 action and its safer external
  credential storage.
- Made uninstall validate purge confirmation and safe paths before any mutation,
  reject surplus lifecycle arguments, and added preserve-data regression coverage.
- Added explicit migration edges from legacy 0.1 and both 1.0 patch releases to
  the 1.1 configuration schema.
- Added bounded retries to external registry manifest checks so transient resets
  do not mask actual Linux ARM64 compatibility results.

## 1.0.1 - 2026-08-21

### Privacy

- Replaced reference-vessel names, identifiers, network addresses, paths,
  hardware facts, engine details, AIS examples, and local entity prefixes with
  neutral placeholders.
- Added distribution checks and publishing guidance to reduce the chance of
  committing vessel identity, credentials, or private key material.

## 1.0.0 - 2026-08-21

### Added

- Reproducible Raspberry Pi OS and Debian-family installer with generated
  credentials, pinned multi-architecture images, and systemd confinement.
- Existing, Docker, and native SignalK modes.
- Opt-in, hardware-probed SocketCAN and AIS-catcher modules.
- Configurable InfluxDB schema with idempotent bucket and downsample-task
  provisioning.
- Grafana datasource and system-health dashboard provisioning.
- Generic Home Assistant battery, bilge/high-water, and shore-power alert
  blueprints plus a Lovelace starter view.
- `vesselstackctl` status, start, stop, restart, backup, verified restore,
  guarded update, and uninstall workflows.
- Optional untrusted-interface firewall and explicit 0.1-to-1.0 configuration
  migration.
- Clean-install, migration, recovery, ARM64 manifest, application, packaging,
  and disposable service-integration test gates.

### Safety

- Hardware and firewall modules remain disabled by default.
- Safety automations require explicit entity, threshold, duration, and action
  selection.
- VesselStack remains advisory software and is not certified navigation,
  collision-avoidance, alarm, engine-control, or life-safety equipment.
