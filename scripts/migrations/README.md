# Configuration migrations

`scripts/migrate.sh` upgrades the recorded configuration schema
(`config/installed-version`) to the release being installed. Any upgrade from
`0.1.0` or newer to a newer version is accepted; when only the version marker
changes, nothing needs to be added here.

When a release needs a real data/config change, add an executable script named
`<release>.sh` (for example `0.5.0.sh`). `migrate.sh` runs every script whose
version is newer than the installed version and not newer than the target, in
version order, passing the installer env file as the only argument. A script
must be idempotent and exit non-zero on failure (the version marker is then not
updated). Downgrades are refused; restore a backup instead.
