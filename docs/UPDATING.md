# Updating and offline install

Purpose: update a running VesselStack with a reviewed release, roll back, and install on a boat with no internet using an offline bundle. Back up first: see [Backups](../README.md#backups).

[Back to README](../README.md) · [Documentation index](README.md)

Back up first (see [Backups](../README.md#backups)); updating and rolling back are summarised
in the README under [Updating](../README.md#updating) and
[Upgrading to 0.6.0](../README.md#upgrading-to-060).

## From the pinned installer

Re-run the pinned release installer over `ssh -t`. It finds the installed system and offers
`vesselstackctl update <release dir>`, which runs preflight, makes a verified backup and then
installs and starts the release:

```bash
curl -fsSL https://github.com/jclima/VesselStack/releases/download/v0.6.1/get-vesselstack.sh | sudo bash
```

The command itself needs the unpacked release directory, so plain `vesselstackctl update`
with no argument fails. To run it by hand once a release is unpacked (the installer unpacks to
`/opt/vesselstack-release/<version>`):

```bash
sudo vesselstackctl update /opt/vesselstack-release/0.6.1
```

From a terminal (`ssh -t`) the installer now offers to fix an unsupported `BOAT_UNITS` value or
a setting that cannot be written safely at a prompt instead of stopping (choose, or `c` to
cancel with nothing changed). A root-only copy of the previous settings file is kept as
`vesselstack.env.before-fix-<time>` next to it; delete these copies once the update works. Runs
without a terminal (the Control Panel, `--yes`, `VESSELSTACK_NONINTERACTIVE=true`) still stop
before changing anything and print the exact fix. Details: [Upgrading to 0.6.0](../README.md#upgrading-to-060).

## From the Control Panel

Open the **Updates** tab and press **Check for updates**. This is the only thing that
contacts `github.com`; nothing about your boat is sent. A newer release is listed with a
link to its notes and a **Back up & install** button. Releases that GitHub publishes as
prereleases appear only after you turn on **Include early test releases** (0.5.x panels
call the same switch "Include lab prereleases"). **Back up & install** downloads the
archive, checks its size and SHA-256 checksum, runs the installer's `--dry-run` check,
takes a backup with `--require-mounted` (so the update stops if the external backup drive
under `/media` or `/mnt` is not mounted), installs, restarts the Control Panel and waits
for it to report the new version. The page reconnects by itself. See
[Control Panel](CONTROL_PANEL.md#updates).

If the Control Panel is unavailable, use the pinned installer above.

## When the backup drive is not mounted

The pinned installer and (from an installed 0.6.1) `vesselstackctl update` and
`vesselstackctl backup` ask, before anything is stopped, whether to mount the drive from
`/etc/fstab`, pick a connected USB drive (ext4 recommended, exFAT allowed, vfat refused), check
again, or back up to the system disk once. Without a terminal, with `--yes`, or in the Control
Panel they stop and print a `To fix:` line instead. A direct `sudo vesselstackctl update <dir>` from
an installed 0.6.0 or earlier controller has no menu: use the pinned installer, mount the drive
first, or run `sudo env VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=true vesselstackctl update <dir>`.
See [Backups](../README.md#backups).

## From a source checkout

Use a reviewed tag from a git clone, with the dry run first:

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
`GENERATE` (for example a fresh copy of the example), the installer keeps
the already-installed InfluxDB, Grafana, MQTT, Boat Chat and control-panel
secrets instead of generating new ones. A non-`GENERATE` value you set
explicitly still wins.

Once a reviewed release directory is downloaded, the lifecycle command performs
preflight, a verified pre-update backup, installation, and startup:

```bash
sudo vesselstackctl update /path/to/reviewed-release
```

Before it rewrites the installation the installer checks every value it will write to
`vesselstack.env`, `boat-chat.env` and `control-panel.env`, then builds the Python
environment in a staging directory and checks the upgrade path. A failed or
offline-without-bundle run stops with "Nothing was changed" and leaves the previous
installation, `vesselstack.env`, units and version marker as they were. A value the
installer refuses to write (an apostrophe combined with a backslash, double quote, dollar
sign or backtick, or a multi-line value) is caught by `--dry-run` itself, so `vesselstackctl
update` and the Control Panel updater stop before the backup, naming only the setting's key;
see [Upgrading to 0.6.0](../README.md#upgrading-to-060). The env files are then written
atomically (temporary file, then rename), so a failed run never leaves one truncated. When
the code, units and `compose.yml` have been replaced, the staged virtualenv is swapped
in and `installed-version` is written last.

## Rollback

Downgrades are refused. Rolling back a completed or half-finished upgrade is a restore of
the pre-update backup that `vesselstackctl update` takes automatically:

```bash
sudo vesselstackctl verify-backup /path/to/vesselstack-TIMESTAMP.tar.gz
sudo vesselstackctl restore /path/to/vesselstack-TIMESTAMP.tar.gz --yes
```

## Offline install (no internet on the boat)

Prepare a bundle on the home lab or any connected machine, carry it to the
boat, then install without any network access. The guided installer does the
checks, `--verify`, `--load` and install for you:
`sudo bash get-vesselstack.sh --offline --bundle /path/to/offline-bundle --tarball /path/to/vesselstack-0.6.1.tar.gz`
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
bundle forged consistently cannot install a wheel the pinned release did not
pin. Containers start with `docker compose up -d --pull never`. If an image is
missing the installer lists it with the `docker load` hint: `--start` fails
before changing anything, while a plain render only warns. Offline mode never
contacts a registry or PyPI; Signal K/InfluxDB version bumps still need a new
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
variable the install runs online, with the hash-pinned lock. Rollback: restore the
pre-update backup (the lock file is inert for older releases).

## Verify a release tag

A checksum shows a download is intact; it is not signed provenance. Release tags are
SSH-signed annotated tags, and `scripts/verify-release-tag.sh` checks one against the
release key listed in `.github/allowed_signers`. It needs a clone that contains the tag
and `ssh-keygen` (part of OpenSSH):

```bash
git fetch --tags
scripts/verify-release-tag.sh v0.6.1
```

The script takes `TAG [ALLOWED_SIGNERS_FILE [EXPECTED_COMMIT]]`. It checks that the tag is
annotated, that its own name equals `TAG`, that it points directly at a commit, that the
signature is Good from an allowed key and that the signer is the release principal. Pass a
full 40 or 64 character commit SHA as the third argument to also bind the tag to the commit
you recorded when the release was built (use `""` as the second argument to keep the default
signers file). It exits non-zero, with an error on stderr, for an unsigned, lightweight or
wrongly signed tag. Older `v0.5.x` tags are unsigned and fail by design. The Control Panel
updater and `vesselstackctl update` check the archive checksum only; they do not run this
script. How maintainers sign a release with a key held in 1Password is in
[Deployment: release signing](DEPLOYMENT.md#1-release-signing-with-an-ssh-signing-key).
