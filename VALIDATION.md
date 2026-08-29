# VesselStack 1.3 validation

This record describes the evidence used to release 1.3.0. It does not convert
VesselStack into certified marine safety equipment.

## Automated release gates

- Bash syntax and ShellCheck for installer, lifecycle, migration, hardware,
  firewall, packaging, and integration scripts.
- Sanitization scan for reference-vessel identifiers, network addresses, and
  generated secrets.
- Clean installer render into an isolated filesystem, including permissions,
  generated credentials, branding, systemd units, Grafana assets, Home
  Assistant assets, and installed schema version.
- Explicit 0.1/1.0/1.1/1.2-to-1.3 migration and rejection of unknown future schemas.
- Checksummed backup verification and actual restoration of changed state.
- Docker Compose rendering with default, SignalK, and AIS profiles.
- Registry-manifest verification that every pinned image publishes Linux ARM64,
  with bounded retries for transient registry transport failures.
- Disposable InfluxDB, Prometheus, and Grafana startup; health queries; bucket
  creation; idempotent Flux task creation; datasource lookup; and dashboard
  lookup.
- Boat Chat, Control Panel, query-planner, session-store, API-integration, and
  wizard tests, including installed SignalK, Home Assistant, InfluxDB
  endpoint/credential use, request limits, token handling, configuration
  rollback, and operation allowlists.
- Lifecycle tests for complete first start, per-service diagnostics, nested
  backup rejection, verified restore, preserve-data uninstall, and pre-mutation
  purge guards.
- Sanitized release archive construction and SHA-256 verification.
- Five wizard tests for complete configuration generation, input allowlisting,
  enum validation, private file permissions, and topology filtering.
- Automated browser walkthrough of all seven wizard steps, configuration save,
  content and overlay checks, browser error inspection, and two reviewed
  documentation screenshots.

## Raspberry Pi validation host

- Linux ARM64 host
- Linux `aarch64`, 64-bit
- Debian GNU/Linux 12 (bookworm)
- Clean render, migration, package, checksum, backup, and recovery tests passed
  on 2026-08-25. Wizard rendering, navigation, private configuration output,
  screenshots, and package inclusion were also verified on that host.

Hardware-dependent SocketCAN, SDR/AIS reception, and vessel-specific Home
Assistant thresholds must still be commissioned on each vessel. The installer
probes enabled devices, but CI cannot establish RF reception, CAN bus wiring,
termination, sensor calibration, or safe alarm thresholds.
