# Updating and offline install

Purpose: update a running VesselStack with a reviewed release, roll back, and install on a boat with no internet using an offline bundle. Back up first: see [Backups](../README.md#backups).

[Back to README](../README.md) · [Documentation index](README.md)

Back up configuration and data, then use a reviewed release:

```bash
cd VesselStack
git fetch --tags
git checkout <reviewed-release-tag>

sudo ./install.sh \
  --config /opt/vesselstack/config/vesselstack.env \
  --dry-run

sudo ./install.sh \
  --config /opt/vesselstack/config/vesselstack.env
```

Review Compose and release notes before applying container changes. Prefer the
installed configuration as input. If you do re-run with a file that still says
`GENERATE` (for example a fresh copy of the example), the installer now keeps
the already-installed InfluxDB, Grafana, MQTT, Boat Chat and control-panel
secrets instead of generating new ones. A non-`GENERATE` value you set
explicitly still wins.

Once a reviewed release directory is downloaded, the lifecycle command can
perform preflight, a verified pre-update backup, installation, and startup:

```bash
sudo vesselstackctl update /path/to/VesselStack-release
sudo VESSELSTACK_OFFLINE=true VESSELSTACK_WHEELHOUSE=/path/to/offline-bundle/wheelhouse \
  vesselstackctl update /path/to/VesselStack-release   # no network
```

For Control Panel updates, stage the reviewed release beneath its restricted
root first. The panel rejects symlinks, missing release metadata, paths outside
this directory, and any entry anywhere in the release tree (`scripts/`,
`boat-chat/`, `templates/`, `systemd/`, ...) that is a symlink, owned by a user
other than root/the panel account, or group/world-writable:

```bash
sudo install -d -m 0755 /var/lib/vesselstack/releases
sudo cp -a /path/to/vesselstack-x.y.z /var/lib/vesselstack/releases/
sudo chown -R root:root /var/lib/vesselstack/releases/vesselstack-x.y.z
sudo chmod -R go-w /var/lib/vesselstack/releases/vesselstack-x.y.z
```

Then enter `/var/lib/vesselstack/releases/vesselstack-x.y.z` in the panel.
Shell-initiated `vesselstackctl update` remains available for a trusted operator
and is not limited to the panel staging directory.

The installer records the configuration schema in
`/opt/vesselstack/config/installed-version`. Any upgrade from 0.0.1 or newer to a
newer release is accepted: when a release needs no data change only the version
marker moves, and releases that do need one ship an executable
`scripts/migrations/<version>.sh` that runs once, in version order.
The first release (0.0.1, formerly 1.0.0) migrates pre-release "0.1" layouts and
retains legacy InfluxDB names when the new bucket settings are absent.

**Version renumbering (2026-10-25).** The project is pre-1.0, so the former
1.x releases were renumbered: 1.0.0 is now 0.0.1, 1.0.1 is 0.0.2, and 1.1.0,
1.2.0, 1.3.0, 1.4.0, 1.4.1 are 0.1.0, 0.2.0, 0.3.0, 0.4.0, 0.4.1 (see the
CHANGELOG for the table). Former `v1.x` tags and releases were removed. A machine
that installed a former 1.x release has `1.x.y` in `installed-version`, which
would look like a downgrade to `0.4.1`. `migrate.sh` and `get-vesselstack.sh`
therefore treat an installed `1.M.P` as `0.M.P` (1.0.0 and 1.0.1 as 0.0.1 and
0.0.2) when the target is a 0.x release, so `vesselstackctl update` and
`get-vesselstack.sh` upgrade it normally and rewrite the marker. If an older
`migrate.sh` refuses it anyway, run `sudo vesselstackctl backup`, then either
write the matching new number into `/opt/vesselstack/config/installed-version`
(for example `echo 0.4.1 | sudo tee /opt/vesselstack/config/installed-version`
for a 1.4.1 install; the configuration format is unchanged by the renumbering) or
reinstall 0.4.1 and `sudo vesselstackctl restore` the backup. To roll back the
renumbering itself, restore that backup on the previous release.

**Downgrades are not supported.** Configuration and data may already use the
newer schema, so `install.sh` (including `--dry-run`) and `migrate.sh` refuse to
install an older release over a newer one and change nothing. To go back, restore
a backup taken before the upgrade (`sudo vesselstackctl restore ...`, see
Backups) or install the newer release again. Unknown or malformed version
markers are refused instead of guessed at.

Install order is transactional. Preflight (`--dry-run` runs the same checks)
validates the upgrade path, the Compose template and, offline, the wheelhouse and
local images **before** any installed file is touched. The Python environment is
then built in a staging virtualenv next to the install; only if that succeeds are
the environment, units and code rewritten, the staged virtualenv swapped in, and
`installed-version` written last. A failed or offline-without-bundle run
therefore leaves the previous installation, `vesselstack.env`, units and version
marker exactly as they were (the installer says "Nothing was changed").
Rollback of a completed upgrade is a restore of the pre-update backup that
`vesselstackctl update` takes automatically.

## Offline install (no internet on the boat)

Prepare a bundle on the home lab or any connected machine, carry it to the
boat, then install without any network access. The guided installer does the
checks, `--verify`, `--load` and install for you:
`sudo bash get-vesselstack.sh --offline --bundle /path/to/offline-bundle --tarball /path/to/vesselstack-0.5.7.tar.gz`
(copy `get-vesselstack.sh`, the tarball with its `.sha256` and the bundle on the
carry media; the bundle must be root-owned and not group/world-writable, and
packages and Docker must already be installed). The manual steps follow:

```bash
# Connected machine (needs docker and python3 with pip). Defaults target
# Raspberry Pi OS 64-bit with Python 3.11 and linux/arm64 images.
scripts/prepare-offline-bundle.sh --output ./offline-bundle
#   other targets: --python-version 3.13  --platform manylinux_2_39_aarch64
#   same architecture as this machine:     --native
#   see the image list without downloading: --list

# Copy ./offline-bundle (wheelhouse/, images/, MANIFEST.sha256, BUNDLE_INFO) and
# a copy of this release to the boat. As root the bundle must be root-owned and
# not group/world-writable along its whole path, so make it so first:
sudo chown -R root:root /path/to/offline-bundle && sudo chmod -R go-w /path/to/offline-bundle
sudo scripts/prepare-offline-bundle.sh --load /path/to/offline-bundle   # verifies the bundle, docker load
sudo VESSELSTACK_WHEELHOUSE=/path/to/offline-bundle/wheelhouse \
  ./install.sh --config /opt/vesselstack/config/vesselstack.env --offline --dry-run
sudo VESSELSTACK_WHEELHOUSE=/path/to/offline-bundle/wheelhouse \
  ./install.sh --config /opt/vesselstack/config/vesselstack.env --offline --start
```

The bundle contains a wheelhouse (`pip download --require-hashes
--only-binary=:all:` of the release's hash-pinned `boat-chat/requirements.lock`
for the chosen platform, so only wheels whose sha256 the lock lists are fetched
and no source distribution is ever built), a `docker save` tarball for every
image in `templates/compose.yml` (all profiles; this includes the Mosquitto image that also hashes the broker passwords),
`BUNDLE_INFO` (release, platform, image list, `lock_sha256`) and
`MANIFEST.sha256`. It holds no configuration, credentials or vessel data.
The all-profile image list includes the optional pinned node-exporter image;
offline installation requires it only when `NODE_EXPORTER_ENABLE=true`.

Before installing or loading anything, `--verify DIR`, `--load DIR` and
`install.sh --offline` refuse the bundle unless: the manifest lists exactly the
files present (no extra, unlisted or missing files, no symlinks) and every
checksum matches; `BUNDLE_INFO` names this release and the sha256 of **this
release's** `requirements.lock` (the manifest is anchored to the release content,
not only to itself); every wheelhouse file is a `.whl` whose sha256 is in the
lock; and, when running as root, the bundle path chain and every file in it are
root-owned and not group/world-writable. `install.sh --offline` also requires
`VESSELSTACK_WHEELHOUSE` to be the `wheelhouse/` directory of such a bundle, then
runs `pip install --no-index --find-links $VESSELSTACK_WHEELHOUSE
--only-binary=:all: --require-hashes -r boat-chat/requirements.lock`, so even a
bundle forged consistently cannot install a wheel the signed release did not
pin. Containers start with `docker compose up -d --pull never`. If an image is
missing the installer lists it with the `docker load` hint: `--start` fails
before changing anything, while a plain render only warns. Offline mode never
contacts a registry or PyPI; SignalK/InfluxDB version bumps still need a new
bundle. Image tarballs are covered by the manifest and the root-controlled path
check but, unlike wheels, are not pinned by digest in the release (Compose images
are tag-pinned).

Online installs use the same lock (`--only-binary=:all: --require-hashes`), so
the Python environment is the same hash-verified set either way. The lock lists
Linux aarch64 and x86_64 wheels for Python 3.11-3.13; on another platform or
architecture the install fails with a clear message instead of building from
source. A release without `boat-chat/requirements.lock`, or a lock missing for
an offline bundle build, is refused before anything changes.

Maintainers: when `boat-chat/requirements.txt` changes, run
`scripts/update-requirements-lock.sh` (needs network; `--python-version` and
`--platform` select targets) and commit the regenerated lock. It is generated
trust-on-first-use from PyPI: review the diff and compare the hashes with the
digests PyPI publishes. `tests/test_requirements_lock.py` fails when the lock is
stale. `scripts/deploy-chat.py` treats a changed `requirements.lock` like a
changed `requirements.txt` and refuses the code-only deploy.

`sudo VESSELSTACK_OFFLINE=true VESSELSTACK_WHEELHOUSE=/path/to/bundle/wheelhouse vesselstackctl update /path/to/release`
(the directory is always the first argument; the `--offline` flag form is also
accepted by this release's `vesselstackctl`, but older installed `vesselstackctl`
versions would read it as the directory, so the guided installer and the docs use the
variable) passes
`--offline` to both the preflight and the install of that release; any value other
than `true`/`false` in `VESSELSTACK_OFFLINE` is rejected. Without the flag or
variable the behaviour is unchanged apart from the hash-pinned lock. Rollback:
restore the pre-update backup; to return to the previous behaviour revert this
change (the lock file is inert for older releases).
