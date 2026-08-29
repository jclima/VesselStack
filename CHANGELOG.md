# Changelog

All notable changes to VesselStack are documented here. The project follows
Semantic Versioning.

## Unreleased

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
