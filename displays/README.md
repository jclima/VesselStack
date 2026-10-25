# VesselStack displays (preview)

Pure-logic core for configurable live-instrument displays. The root `README.md`
("Instrument displays (preview)") is authoritative.

- `schema.py` - config and board-registry validation
- `conversions.py` - unit conversions and rounding
- `topics.py` - MQTT topics, payloads, freshness state machine, SignalK helpers
- `displayctl.py` - `validate [FILE]` and `sync-mqtt` (renders the Mosquitto ACL and hashed passwords; run by `install.sh` and `vesselstackctl`)
- `mqtt_auth.py` - MQTT account, secrets and ACL rendering used by `sync-mqtt`
- `boards/registry.json` - supported boards and layouts
- `displays.example.json` - example configuration (runtime `displays.json` is never committed)

Standard library only. `install.sh` copies this directory to `$VESSELSTACK_ROOT/displays`; the MQTT accounts and always-on ACL are active, the bridge service and firmware are not built yet.
