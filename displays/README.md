# VesselStack displays (preview)

Pure-logic core for configurable live-instrument displays. The root `README.md`
("Instrument displays (preview)") is authoritative.

- `schema.py` - config and board-registry validation
- `conversions.py` - unit conversions and rounding
- `topics.py` - MQTT topics, payloads, freshness state machine, SignalK helpers
- `bridge.py` - the `vesselstack-displays-bridge` service: SignalK websocket to MQTT, driving `topics.Engine` (runtime adapter only; the only module that needs `requirements.txt`)
- `requirements.txt` - exact, sha256-hash-pinned dependencies of the bridge (`paho-mqtt`, `websockets`), installed with `pip --require-hashes` into `displays/.venv` when `DISPLAYS_ENABLE=true` (regenerate with `scripts/pin-displays-requirements.py`)
- `displayctl.py` - `validate [FILE]`, the lifecycle commands (`list`, `add`, `remove`, `rotate`, `wifi`, `status`), `sync-mqtt` (renders the Mosquitto ACL and hashed passwords) and `bridge-env --output FILE` (writes the bridge's 0600 systemd environment file); run by `install.sh` and `vesselstackctl`
- `lifecycle.py` - `list`, `add`, `remove`, `rotate`, `wifi` and `status` behind `displayctl.py` (locked, no-follow, atomic edits of `displays.json` and `displays.secrets.json`; also reachable as `vesselstackctl display ...`)
- `mqtt_auth.py` - MQTT account, secrets and ACL rendering used by `sync-mqtt`
- `boards/registry.json` - supported boards and layouts
- `displays.example.json` - example configuration (runtime `displays.json` is never committed)

Everything except `bridge.py` is standard library only, and `bridge.py` imports `paho-mqtt` and `websockets` lazily, so `python3 -m unittest discover -s tests -p 'test_displays*.py'` needs no extra packages. `install.sh` copies this directory to `$VESSELSTACK_ROOT/displays`; the MQTT accounts, always-on ACL and (when `DISPLAYS_ENABLE=true`) the bridge service are active, the display firmware is not built yet.
