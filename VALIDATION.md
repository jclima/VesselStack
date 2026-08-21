# VesselStack 1.0 validation

This record describes the evidence used to release 1.0.1. It does not convert
VesselStack into certified marine safety equipment.

## Automated release gates

- Bash syntax and ShellCheck for installer, lifecycle, migration, hardware,
  firewall, packaging, and integration scripts.
- Sanitization scan for reference-vessel identifiers, network addresses, and
  generated secrets.
- Clean installer render into an isolated filesystem, including permissions,
  generated credentials, branding, systemd units, Grafana assets, Home
  Assistant assets, and installed schema version.
- 0.1-to-1.0 migration and rejection of unknown future schemas.
- Checksummed backup verification and actual restoration of changed state.
- Docker Compose rendering with default, SignalK, and AIS profiles.
- Registry-manifest verification that every pinned image publishes Linux ARM64.
- Disposable InfluxDB, Prometheus, and Grafana startup; health queries; bucket
  creation; idempotent Flux task creation; datasource lookup; and dashboard
  lookup.
- Thirty-one Boat Chat tests, including installed SignalK, Home Assistant, and
  InfluxDB endpoint and credential use.
- Sanitized release archive construction and SHA-256 verification.

## Raspberry Pi validation host

- Linux ARM64 host
- Linux `aarch64`, 64-bit
- Debian GNU/Linux 12 (bookworm)
- Clean render, migration, package, checksum, backup, and recovery tests passed
  on 2026-08-21 without modifying the running boat services.

Hardware-dependent SocketCAN, SDR/AIS reception, and vessel-specific Home
Assistant thresholds must still be commissioned on each vessel. The installer
probes enabled devices, but CI cannot establish RF reception, CAN bus wiring,
termination, sensor calibration, or safe alarm thresholds.
