# VesselStack 1.1 validation

This record describes the evidence used to release 1.1.0. It does not convert
VesselStack into certified marine safety equipment.

## Automated release gates

- Bash syntax and ShellCheck for installer, lifecycle, migration, hardware,
  firewall, packaging, and integration scripts.
- Sanitization scan for reference-vessel identifiers, network addresses, and
  generated secrets.
- Clean installer render into an isolated filesystem, including permissions,
  generated credentials, branding, systemd units, Grafana assets, Home
  Assistant assets, and installed schema version.
- Explicit 0.1/1.0-to-1.1 migration and rejection of unknown future schemas.
- Checksummed backup verification and actual restoration of changed state.
- Docker Compose rendering with default, SignalK, and AIS profiles.
- Registry-manifest verification that every pinned image publishes Linux ARM64,
  with bounded retries for transient registry transport failures.
- Disposable InfluxDB, Prometheus, and Grafana startup; health queries; bucket
  creation; idempotent Flux task creation; datasource lookup; and dashboard
  lookup.
- Forty-six Boat Chat and Control Panel tests, including installed SignalK,
  Home Assistant, InfluxDB endpoint/credential use, request limits, token
  handling, configuration rollback, and operation allowlists.
- Lifecycle tests for complete first start, per-service diagnostics, nested
  backup rejection, verified restore, preserve-data uninstall, and pre-mutation
  purge guards.
- Sanitized release archive construction and SHA-256 verification.

## Raspberry Pi validation host

- Linux ARM64 host
- Linux `aarch64`, 64-bit
- Debian GNU/Linux 12 (bookworm)
- Clean render, migration, package, checksum, backup, and recovery tests passed
  on 2026-08-25. The local Boat Chat and Control Panel hardening was then loaded
  with targeted service restarts and both health endpoints were verified.

Hardware-dependent SocketCAN, SDR/AIS reception, and vessel-specific Home
Assistant thresholds must still be commissioned on each vessel. The installer
probes enabled devices, but CI cannot establish RF reception, CAN bus wiring,
termination, sensor calibration, or safe alarm thresholds.
