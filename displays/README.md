# VesselStack displays (preview)

Pure-logic core for configurable live-instrument displays. The root `README.md`
("Instrument displays (preview)") is authoritative.

- `schema.py` - config and board-registry validation
- `conversions.py` - unit conversions and rounding
- `topics.py` - MQTT topics, payloads, freshness state machine, SignalK helpers
- `displayctl.py` - `python3 displays/displayctl.py validate [FILE]`
- `boards/registry.json` - supported boards and layouts
- `displays.example.json` - example configuration (runtime `displays.json` is never committed)

Standard library only. Nothing here is installed or started yet.
