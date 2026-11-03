# Validation

Current automated gates come first. The 0.3.0 record that follows is kept for history.

## 0.6.0 automated gates

GitHub Actions (`.github/workflows/ci.yml`) runs these on every pull request; the release
workflow reruns the whole CI workflow on the tagged commit. None of them replaces
commissioning on the boat.

- README update required: a pull request that does not change `README.md` fails.
- Privacy check (`scripts/check-public-tree.py`) and static distribution checks
  (`tests/test-distribution.sh`, including ShellCheck and that `DEFAULT_VERSION` equals `VERSION`).
- Lifecycle backup, verify and restore; versioned migrations; clean installation; offline and
  transactional installation; the update launcher's root-trust checks; the guided
  `get-vesselstack.sh` bootstrap; SocketCAN unit rendering; release package contents.
- A real systemd browser-update acceptance run, and the Signal K VesselTwin preload security tests as root.
- Docker Compose validation with the default and optional profiles, and a check that every
  pinned image publishes Linux ARM64.
- All Python regressions (`python3 -m unittest discover -s tests -p 'test_*.py'`) and the
  Control Panel UI regression checks (`node tests/test_control_panel_ui.cjs`).
- A release archive build with a SHA-256 check of the archive and of `get-vesselstack.sh`.
- Disposable InfluxDB, Prometheus and Grafana startup, the Mosquitto access list and display
  accounts, the Mosquitto upgrade path, the Signal K to MQTT displays bridge, and its
  EnvironmentFile under real systemd.
- The generated display projects are validated with the pinned ESPHome image.

Not covered by CI: SocketCAN and NMEA 2000 wiring, SDR/AIS reception, real Raspberry Pi
hardware and upgrades, and vessel-specific alarm thresholds. 0.6.0 release status: recorded
in the CHANGELOG at release time.

## Historical 0.3.0 validation (schema 1.3)

This record describes the evidence used for the historical 0.3.0 release. It is
not a current-release gate and does not convert VesselStack into certified
marine safety equipment.

### Automated release gates (0.3.0)

- Bash syntax and ShellCheck for installer, lifecycle, migration, hardware,
  firewall, packaging, and integration scripts.
- Sanitization scan for reference-vessel identifiers, network addresses, and
  generated secrets.
- Clean installer render into an isolated filesystem, including permissions,
  generated credentials, branding, systemd units, Grafana assets, Home
  Assistant assets, and installed schema version.
- Explicit 0.1/1.0/1.1/1.2-to-1.3 migration (schema labels of that era) and rejection of unknown future schemas.
- Checksummed backup verification and actual restoration of changed state.
- Docker Compose rendering with default, Signal K, and AIS profiles.
- Registry-manifest verification that every pinned image publishes Linux ARM64,
  with bounded retries for transient registry transport failures.
- Disposable InfluxDB, Prometheus, and Grafana startup; health queries; bucket
  creation; idempotent Flux task creation; datasource lookup; and dashboard
  lookup.
- Boat Chat, Control Panel, query-planner, session-store, API-integration, and
  wizard tests, including installed Signal K, Home Assistant, InfluxDB
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

### Raspberry Pi validation host (0.3.0)

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
