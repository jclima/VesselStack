# Boat profile

`config/boat-profile.json` is the shared, validated description of engines,
battery banks, and fuel tanks. It contains no credentials, positions, or
telemetry. If the file is absent, consumers use the legacy diagnostic default:
port/starboard combustion engines, a house bank, and fuel tanks `0` and `1`.
Malformed or unsafe files are errors and never silently fall back.

Use the standalone CLI (from the repository root):

```bash
python3 common/boat_profile_cli.py show --root /opt/vesselstack
python3 common/boat_profile_cli.py validate templates/boat-profile.example.json
python3 common/boat_profile_cli.py set FILE.json --root /opt/vesselstack
python3 common/boat_profile_cli.py init --root /opt/vesselstack
```

On an installed boat, the equivalent root-managed commands are:

```bash
sudo vesselstackctl profile show
sudo vesselstackctl profile validate FILE.json
sudo vesselstackctl profile set FILE.json
```

`set` validates the complete source before an atomic replacement, retaining the
previous file in `config/boat-profile-backups/`. `init` refuses to overwrite an
existing profile. Files are limited to 64 KiB, must be regular non-symlinks,
and are written mode `0640`; when run as root, ownership is `root:vesselstack`
when that group exists. A successful change is picked up on the next consumer
refresh; this module does not restart or mutate services.

Schema version 1 requires exact fields and safe Signal K paths. IDs and paths
must be unique. Empty arrays mean that capability is absent. Engine kinds are
`combustion` or `electric`; battery roles are `house`, `starter`, or `other`.
`fuel_rate_supported` defaults to `true` for combustion and `false` for
electric engines when omitted.
When `primary_battery_bank` is set, it must identify a battery bank whose role
is `house`; solar and battery summaries use that bank consistently.

Boat Chat and the telemetry indexer read this profile at request/refresh
boundaries. It does not automatically generate Home Assistant, Grafana, or
display mappings; those integrations retain their own configuration. Profile
extensions should preserve the schema and capability semantics above, with
consumers explicitly updated and tested before relying on new fields.
