# Development and safe updates

## Use a separate source checkout

Clone `https://github.com/jclima/VesselStack.git` on each development machine.
On the boat, keep the Git checkout separate from `/opt/vesselstack` (or another
live application directory). Never `git pull`, `git reset --hard`, `git clean`
or `rsync --delete` over an existing operational workspace.

```bash
git clone https://github.com/jclima/VesselStack.git
cd VesselStack
python3 -m venv .venv
.venv/bin/pip install -r boat-chat/requirements.txt
cp boat-chat/boat_facts.example.json boat-chat/boat_facts.json
python3 -m unittest discover -s tests -p 'test_*.py'
tests/test-distribution.sh
```

Use synthetic local facts for development. Runtime env/MCP files, vessel facts,
memory/power databases, audit logs and backups are ignored. The installer still
generates actual vessel facts in the installed application; the source example
contains no real identity. Never force-add private files.

Before committing, inspect your staged diff and run:

```bash
python3 scripts/check-public-tree.py --staged
```

The privacy checker prints filenames/reasons, not matched values. It is a
heuristic guard and does not replace reviewing documentation, fixtures or history.
Every change must update README.md as required by AGENTS.md.

## Release checklist

The quick-start URLs (the pinned `.../releases/download/vX.Y.Z/get-vesselstack.sh` and the
secondary `.../releases/latest/download/get-vesselstack.sh`, which follows whichever release
GitHub marks Latest) only work if the GitHub release carries exactly these files. Nothing publishes a release for you: the tag-triggered
workflow only creates a **draft**, and the CI-built draft is the authoritative release.
Do not build and upload files by hand.

1. Bump `VERSION`, `DEFAULT_VERSION` in `scripts/get-vesselstack.sh` (must equal
   `VERSION`; `tests/test-distribution.sh` checks it), the version in the
   README/docs examples, and `CHANGELOG.md`. Update `README.md` as AGENTS.md requires.
2. Open a pull request; CI must pass (privacy check, distribution checks with
   shellcheck, lifecycle, migrations, clean/offline install, update launcher,
   bootstrap, build-package, Compose config, image manifests, Python, service
   integration). Merge it to `main`.
3. Tag the merged `main` commit (signed tag, see [DEPLOYMENT.md](DEPLOYMENT.md)
   section 1; the recommended 1Password-held signing key is the first subsection there)
   and push the tag. The workflow refuses a tag whose commit is not on
   `main`, that differs from `VERSION`, or that is not an annotated tag SSH-signed by a key in
   `.github/allowed_signers` (checked with `scripts/verify-release-tag.sh`). Verify locally
   first: `scripts/verify-release-tag.sh vX.Y.Z`; the workflow also passes the built commit
   (`scripts/verify-release-tag.sh TAG .github/allowed_signers "$GITHUB_SHA"`), requires the signed
   tag object's own name to equal `TAG`, and refuses a tag that does not point directly at that
   commit. Locally, pass the commit you expect as the third argument. To rotate or add the release key, change
   `.github/allowed_signers` in a pull request to `main` and merge it before creating the tag;
   the workflow reads the copy at the tagged commit. The `v0.5.7` lab prerelease was an
   annotated unsigned tag because no SSH/GPG key was available; it is not signed and would now
   be refused. The `v0.5.1` tag is untouched and was not published because its release pipeline
   caught the bridge startup-status race. `v0.6.2` is an SSH-signed tag, published first as a
   pre-release and marked Latest after the maintainer's physical Raspberry Pi install passes
   (see the CHANGELOG).
4. `.github/workflows/release.yml` then (a) reruns the whole CI workflow on the
   tagged commit, (b) builds with `VESSELSTACK_BUILD_REQUIRE_CLEAN=true` in a
   read-only job and uploads the files as workflow artifacts, and (c) in a separate
   job with write access creates a **draft** release (`--draft --latest=false`) with
   the four files and both sha256 values in the notes. It skips creation if a release
   for the tag already exists. The build job verifies the tag signature (step 3) before building,
   so a bad tag fails there and no draft is created.
5. Take the sha256 values from the draft's notes (or the workflow log). Download the
   draft's assets and test **those exact files** on the home-lab Pi (see
   [DEPLOYMENT.md](DEPLOYMENT.md)): `sha256sum -c`, then the install and update
   rehearsals. The build is reproducible on Linux (same commit, same bytes).
6. **Manual gate (not automated): one real interactive run** of `get-vesselstack.sh`
   on a real Raspberry Pi: fresh install over plain SSH (`ssh -t`) answering the terminal wizard (and one with `--web-wizard`), a no-prompt install (`--yes --set ...`), the Docker
   step on stock 64-bit Raspberry Pi OS bookworm without Docker (expect: the signing key
   verified, `/etc/apt/sources.list.d/docker.sources` written, `docker-ce` and
   `docker-compose-plugin` installed, `docker info` answering; check `dpkg --print-architecture`
   says `arm64`), an upgrade from the previous release, and an offline
   upgrade. The test suites use stubs and cannot cover this. For a prerelease,
   publication may precede physical-Pi acceptance; record that acceptance is pending
   (it was for `0.5.7`). Complete the pinned installer
   and component lifecycle recipe in [CONTROL_PANEL_TESTING.md](CONTROL_PANEL_TESTING.md)
   before any stable promotion.
7. Publish the draft as a **pre-release** (or otherwise not latest) first, so
   `releases/latest/download/` still points at the previous release. Install-test
   through the pinned URL from a scratch directory (never run it piped into root
   before reading it):
   `curl -fsSLO https://github.com/jclima/VesselStack/releases/download/v<version>/get-vesselstack.sh`,
   `curl -fsSLO .../get-vesselstack.sh.sha256`, `sha256sum -c get-vesselstack.sh.sha256`,
   then `sudo bash get-vesselstack.sh --help` and a full lab install.
8. Only after that works make it the latest release:
   `gh release edit v<version> --prerelease=false --latest`.

## Pull without deploying

From the clean source checkout:

```bash
git status --short
git pull --ff-only
python3 -m unittest discover -s tests -p 'test_*.py'
tests/test-distribution.sh
```

A source pull does not modify installed configuration, services, telemetry,
credentials or container volumes. Dirty/diverged branches must be resolved
explicitly; never discard local changes to make a pull succeed.

## Normal release updates

Use the existing installer and lifecycle workflow in the README. Keep the
installed private `config/vesselstack.env`, including generated credentials;
the installer preserves installed secrets even when the input still says GENERATE, but prefer the installed config as input during an update.

`sudo vesselstackctl update /path/to/reviewed/source` runs preflight, a verified
full backup and installation/startup. Plan a maintenance window: this can affect
infrastructure services. Preserve the backup and follow README restore guidance
if recovery is required. Review schema/dependency changes separately.

## Optional application-only development updates

For reviewed Boat Chat Python/static changes that do not require dependencies,
migrations, configuration, service-unit or infrastructure changes, preview with:

```bash
python3 /path/to/source/scripts/deploy-chat.py \
  --source /path/to/source --target /path/to/installed/app --pull
```

Objects are fetched, signature-verified and exported (raw blobs, no `git archive`)
through a root-owned mirror (`--mirror-dir`, default
`/var/lib/vesselstack/update-mirror` as root), never from inside the checkout.
This requires a clean source checkout, pulls fast-forward only, checks public
source and runs all unit tests. It lists changed tracked Python/static files
without applying them. An advisory lock file is created in the live directory.

Apply a reviewed preview explicitly:

```bash
sudo python3 /usr/local/libexec/vesselstack/deploy-chat.py \
  --source /path/to/source --target /path/to/installed/app --apply --restart-chat
```

The default service is `vesselstack-chat.service`; an existing legacy deployment
can explicitly select `--service boat-chat.service`. For a configured local
listener port, pass `--health-url http://127.0.0.1:PORT/health`.

The helper refuses mismatched dependency files and source/target directory
confusion. It never installs packages, runs migrations, updates version metadata,
changes containers, rewrites services, replaces facts/env/MCP/knowledge/database
files, or removes extra live files. It preserves ownership/modes, records a source
commit and per-file hashes in a restricted backup, and atomically replaces only
allowlisted application files. Only Boat Chat restarts.

A failed restart/readiness check restores previous code and restarts it. If that
recovery fails, inspect logs and use the printed receipt. Manual code rollback is:

```bash
sudo python3 /usr/local/libexec/vesselstack/deploy-chat.py \
  --target /path/to/installed/app --rollback /path/to/backup-directory \
  --apply --restart-chat
```

Backup hashes are verified; later file edits block rollback. Retain receipts and
backups privately. This development helper is not a replacement for release
updates and cannot make database migrations reversible or prove navigation safety.

## Daily update shortcut

The installer places `sudo vesselstack-update` and its root-owned helper
(`/usr/local/libexec/vesselstack/deploy-chat.py`); as root the launcher never runs
the checkout's copy. Non-root development runs fall back to the checkout helper.
Its private `/etc/vesselstack/update.env` sets source/live paths, unit names and
health URL. It contains no API credentials and must remain root-owned/mode 600.
Use preview after pushing from the development machine; add `--apply` when ready.
The command fetches, verifies the commit/tag signature against the pinned
`EXPECTED_REMOTE_URL`, fast-forwards, and runs the helper's complete tests as the
checkout owner (never root), then performs the
backup and readiness workflow. Unsigned source is accepted only with the
home-lab opt-in `ALLOW_UNSIGNED_SOURCE=true`. A rollback takes the printed backup directory
and never pulls new source. The active Telegram worker restarts only if its
Python file changed, and the same restart plan is recorded for rollback.

No scheduler or webhook automatically deploys changes. Git pulls alone cannot
activate arbitrary infrastructure edits. Routine application updates are one
command; release/schema/dependency changes still need separate review.

## Wizard internals

Reference for the installer's terminal and browser wizards. Owners normally need only the
[quick start](../README.md#quick-start-one-command-on-the-raspberry-pi).

### Terminal wizard in the guided installer

The guided installer asks the configuration questions **in the terminal** (the terminal wizard,
`vesselstack-wizard.py --cli`, the default). It reads your answers from the terminal
(`/dev/tty`, so the piped `curl | sudo bash` form works), validates every answer
immediately with the same rules as the browser wizard (`DEFAULTS`, `validate()`,
`render()`; stricter checks only where `install.sh` enforces the same limits, such as
the boat name characters), and offers sections for the boat (name, type, units, timezone
detected from the system, optional and private MMSI, call sign, length and beam),
Signal K (docker, existing or native; docker recommended), NMEA 2000 / CAN
(`SOCKETCAN_ENABLE`, interface, bit rate and the home-lab virtual-CAN option
`VESSELSTACK_CAN_CREATE_VCAN`), AIS, Home Assistant (URL, optional hidden token), Boat
Chat provider (default `local`) and Telegram, the firewall (untrusted interface, enable,
strict mode, allowed ports), raw-data retention (`INFLUXDB_RAW_RETENTION`; empty means
30 days on a new install and no change to an existing database) and who can reach the
dashboards (`local`, `dashboards` or `all`; see "Reaching VesselStack from other devices").
Ports, container and bucket names, folders, the service account and the generated
secrets sit behind "Advanced settings? [y/N]". Passwords and tokens default to being
generated during installation, are read without echo when you do type one and are
never printed or logged. It detects the architecture, Docker, `can0` and the network
interface names to inform defaults and hints; it never turns hardware on by itself.
The browser wizard asks the same "Who should be able to reach the dashboards?" question
(`local`, `dashboards` default, `all`) on its network step. The page sends only the
preset name (`LAN_ACCESS`); the server validates it (an unknown name is refused with HTTP 400,
nothing is written) and expands it with the terminal wizard's `LAN_PRESETS` mapping
into `VESSELSTACK_WEB_BIND_ADDRESS`, `VESSELSTACK_DATA_BIND_ADDRESS`, `BOAT_CHAT_HOST` and `HOMEASSISTANT_BIND_ADDRESS`;
the preset wins over explicit bind values, `LAN_ACCESS` is never written to the env file
and the independent Control Panel access choice is preserved. Choosing `all` shows the warning that Prometheus
has no login and recommends the firewall. The loopback, Host/Origin, URL-fragment token
and content-type protections are unchanged.
The terminal wizard has no network surface (no web server, no token, no URL), so it
runs as root directly, in a private root-only `0700` directory; its output is then
treated exactly like the browser wizard's: copied once into the root-only staging
directory, linted, re-validated as root with `vesselstack-wizard.py --validate FILE`
(known settings only, allowed characters, enum and boolean values) and installed
root-only, mode 600. `--config FILE` gets the same treatment (opened without following
symlinks, a regular file of at most 64 KiB, copied once, only the staged copy linted,
validated and used); with `--set` the staged copy is the base the values are applied
to. Optional browser wizard: add `--web-wizard`; it is unchanged (loopback only on a
random free port, `--wizard-port N`, run as an unprivileged user or the temporary
`vesselstack-wizard` system account, a `#token=` address printed once, open on the Pi
or through the printed `ssh -L` tunnel) and cannot be combined with `--yes`-style
configuration options (`--config`, `--set`, `--defaults`). Limitation: the Docker line
in a wizard's preflight reflects what that wizard sees; `install.sh` performs the
authoritative checks.

### Running the wizards by hand

The terminal wizard (`--cli`) asks in the terminal and writes `vesselstack.env`; add
`--defaults` and `--set KEY=VALUE` to configure without questions, `--base FILE` to start from an
existing file. For the browser wizard open the address it prints (`http://127.0.0.1:8088/#token=...`), or use the
SSH tunnel documented in the guide and open the same address on your workstation.
The wizard accepts `--port N`, `--output FILE` (where to write the env file) and
`--exit-after-save` (stop after the first successful save; the one-line installer
uses it) and `--validate FILE` (check an existing env file with the wizard's rules,
exit 0 if valid, 1 otherwise, no server started; `VESSELSTACK_CAN_CREATE_VCAN` is a
known wizard setting, default `false`). Nothing is persisted except the temporary account, which is removed on exit (`sudo userdel
vesselstack-wizard` if a run was killed with SIGKILL). It prints a per-run address of the form `http://127.0.0.1:PORT/#token=...`; open
that exact address. The token is carried in the URL fragment (never sent to the
server, never present in any served file); `wizard/app.js` reads it, removes it
from the address bar with `history.replaceState` and sends it as `X-Wizard-Token`
with every API call (`/api/system` and `/api/save` refuse requests without it).
The wizard needs no root privileges; run it as a normal user. Requests are refused unless the `Host`
header is `127.0.0.1`/`localhost`/`[::1]` on the bound port, any `Origin` is the
same loopback origin, the token matches, and saves use `Content-Type:
application/json` (this blocks cross-site form posts and DNS rebinding). The env
file is created mode 600 from the start and never through a symlink. Open the
printed URL itself (including the `#token=` part); a bookmarked older address stops working when the wizard restarts. The dependency-free wizard binds only to loopback, shows relevant host
prerequisites, and writes a complete private `vesselstack.env`. It does not
change boot firmware, firewall rules, marine wiring, or start services. Then run
`sudo ./install.sh --config vesselstack.env --dry-run` and `--start` as described
in the [README](../README.md#5-run-preflight).

## Developer validation

On Linux, keep temporary test roots protected and remove them after the run:

```bash
trusted_test_tmp=$(mktemp -d "$HOME/vesselstack-python-tests.XXXXXX")
TMPDIR="$trusted_test_tmp" python3 -m unittest discover -s tests -p 'test_*.py'
rm -rf -- "$trusted_test_tmp"
```

The `displays-esphome-config` CI job pulls the image pinned in
`displays/esphome/IMAGE` and runs `tests/test-displays-esphome-config.sh` on every
push and pull request. That script runs the containers with the same hardening
flags `displays/firmware.py` uses (`--cap-drop ALL`, `no-new-privileges`), once
with the flash flags (project mounted read-only and, as in production, owned by root, which the test sets with a one-shot unhardened `chown` helper container; tmpfs scratch, `--network none`), and fails if
the pinned image's `esphome upload --help` no longer lists `--file`, which
`display flash` depends on, so an image bump that drops it cannot go unnoticed.
`tests/test_displays_render.py` also checks that the topics the generated YAML
publishes and subscribes to are permitted by the display ACL and that nothing
else can publish under its MQTT prefix. GitHub Actions uses `actions/checkout@v6`, which runs on Node.js 24 and keeps
persisted checkout credentials outside the repository Git configuration.
External registry manifest checks retry short transport failures three times;
missing Linux ARM64 support still fails the release gate.

`tests/test-distribution.sh` runs `shellcheck` at full severity (including
info-level findings) on the shell scripts it lists, and they currently pass
with no findings. The style `A && B || handler` is avoided (SC2015); use
`if ! { A && B; }; then ... fi` instead.
Run local checks with `shellcheck -S info` (and default severity) over
`install.sh`, `build-package.sh`, `scripts/*` and `tests/*.sh`, since CI does.
Check with ShellCheck 0.9.0 (the Ubuntu and Debian bookworm package, which CI
installs): ShellCheck 0.11.0 does not report SC2015 for `A && B || continue`, so
a clean local 0.11 run can still fail CI.

Root-controlled test fixtures (`tests/test-offline-install.sh`,
`tests/test-update-launcher.sh`) are created under `/usr/local/share/vs-test`,
not `/opt`: the product refuses root-used paths whose chain is not root-owned
and group/world-writable-free, and GitHub runners ship `/opt` as mode 0777. The
tests verify the whole base chain is root-controlled and fail clearly otherwise;
the product check is unchanged. The directory is left empty after a run and can
be removed with `sudo rmdir /usr/local/share/vs-test`.

The lifecycle and installer regression tests cover persisted component
exclusions, filtered LAN re-apply, retained data, optional-service selection,
and offline-safe startup behavior.

`tests/test-get-vesselstack.sh` simulates "Docker absent" by running the
bootstrap under an isolated `PATH` made of its stubs and symlinks to every host
tool except `docker*`, so the result no longer depends on whether the host (for
example a GitHub runner) ships a real `docker` and Compose plugin. To keep the
tests away from the runner's real apt configuration, the bootstrap reads the
locations it touches from `VESSELSTACK_OS_RELEASE`, `VESSELSTACK_APT_KEYRING_DIR`,
`VESSELSTACK_APT_SOURCES_DIR`, `VESSELSTACK_APT_SOURCES_FILE` and
`VESSELSTACK_DOCKER_DATA_DIR` (defaults: `/etc/os-release`, `/etc/apt/keyrings`,
`/etc/apt/sources.list.d`, `/etc/apt/sources.list`, `/var/lib/docker`); they are
test hooks and are not needed in normal use. Docker's key fingerprint is not
overridable.

```bash
tests/test-distribution.sh
sudo tests/test-lifecycle.sh
tests/test-migrations.sh
sudo tests/test-clean-install.sh
tests/test-image-manifests.sh
tests/test-service-integration.sh
sudo tests/test-displays-mosquitto.sh
sudo tests/test-displays-bridge.sh
tests/test-displays-esphome-config.sh   # renders projects and runs the pinned ESPHome "config" (Docker, ~1.2 GB image)
tests/compile-display-firmware.sh       # optional, not in CI: full "esphome compile" (~2 GB cache, 2-5 min)
tests/test-build-package.sh
tests/test-socketcan.sh
sudo tests/test-offline-install.sh

This root-controlled offline regression also verifies failed staging cleanup while
telemetry scheduler recovery remains active.
sudo tests/test-update-launcher.sh
sudo tests/test-get-vesselstack.sh
docker compose --env-file vesselstack.example.env \
  -f templates/compose.yml config --quiet
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 displays/displayctl.py validate
python3 -m unittest discover -s tests -p 'test_control_panel.py' -v
node tests/test_control_panel_ui.cjs   # Control Panel interface (tabs, dialogs, banner, validation)
```

Build a sanitized release:

```bash
./build-package.sh
(cd generated && sha256sum -c "vesselstack-$(cat ../VERSION).tar.gz.sha256")
```

The package is produced by `git archive` of the committed `HEAD`, so untracked
or ignored files (`.venv`, `boat-chat/boat_facts.json`, env files, memory/data,
logs) can never enter it, and uncommitted edits to tracked files are excluded
(with a warning; `VESSELSTACK_BUILD_REQUIRE_CLEAN=true` makes that an error). The
tarball contains `COMMIT` and `BUILD_INFO` naming the exact source commit, and
the build prints the sha256 and that commit. `tests/test-build-package.sh`
proves private and untracked files are not packaged. Building outside a git
checkout is refused.

The checksum records only the archive basename, so the archive and `.sha256`
file can be downloaded and verified together from any directory.

`./build-package.sh` also writes `generated/get-vesselstack.sh` (the committed
`scripts/get-vesselstack.sh` with this release's version baked in as the default)
and `generated/get-vesselstack.sh.sha256`. A GitHub release must carry exactly
four files: `vesselstack-<version>.tar.gz`, its `.sha256`, `get-vesselstack.sh`
and `get-vesselstack.sh.sha256`; the quick-start URLs depend on those names. The
checklist is in [docs/DEVELOPMENT.md](DEVELOPMENT.md#release-checklist).
`.github/workflows/release.yml` runs only when a `vX.Y.Z` tag is pushed (never on
pull requests), re-runs the checks and creates a **draft** release with those
files for the maintainer to review and publish.
`sudo tests/test-get-vesselstack.sh` covers the bootstrap (checksum mismatch,
truncated downloads and scripts, unsafe archives, existing-install detection,
non-root refusal, `--help`, offline and `--no-wizard --config` flows, and the Docker
installation cases listed in the 0.4.1 changelog entry, and, since 0.4.2, the terminal
wizard driven through `VESSELSTACK_BOOTSTRAP_ANSWERS` (an answers file read like the
terminal: a full default walk, edits, invalid input and re-prompts, `?`, `b`, the review,
secrets never printed or logged, quit and early end of input), `--yes`/`--defaults`/`--set`
(valid, unknown key, invalid value, on top of `--config` and a saved configuration), the
no-terminal failure message, `--web-wizard` and the staged validation of the terminal
wizard's output) with fake `curl`/`apt-get`/`apt-cache`/`dpkg`/`gpg`/`docker`/`systemctl`
stubs. `tests/test_wizard.py` tests the prompt logic as functions driven by an input
iterator, the unbuffered descriptor reader and the real wizard on a pseudo-terminal.

## Historical notes

Kept for reference; these sections describe past scope and unreleased review fixes, not the current lab prerelease. See `CHANGELOG.md` for release notes.

### Version 1.0 release scope (historical)

This is the historical 1.0 scope checklist, kept for reference; it is not a
statement about the current lab prerelease.

1. **Complete:** InfluxDB schema names and runtime Boat Chat branding are
   configurable, with legacy defaults retained only for 0.x upgrades.
2. **Complete:** Existing, pinned native, and pinned Docker Signal K modes are
   included.
3. **Complete:** Opt-in SocketCAN and AIS-catcher modules include hardware
   probes and stay disabled by default.
4. **Complete:** Generic battery, bilge, and shore-power alert blueprints plus
   a Lovelace vessel dashboard template are included.
5. **Complete:** Grafana datasource/dashboard and InfluxDB downsample
   provisioning are included.
6. **Complete:** backup/restore, guarded upgrades, firewall, versioned
   migrations, and uninstall are included.
7. **Complete:** CI exercises clean rendering, service startup/provisioning,
   migration, packaging, and recovery. The same non-invasive install and
   recovery gates pass on a Linux ARM64 host running 64-bit Debian.
8. **Complete:** A loopback-first authenticated Control Panel manages component
   status, configuration, installation, updates, backups, and lifecycle while
   preserving CLI recovery access.

See `CHANGELOG.md` for release notes. `VALIDATION.md` is the historical 0.3.0
validation record, including its hardware-dependent commissioning limits; it is
not a current-release gate.

### Current development improvements

**Review fixes (unreleased).** `install.sh` now writes `vesselstack.env`,
`boat-chat.env` and `control-panel.env` values that bash, systemd, Python and
Docker Compose read identically: bare when plain, otherwise single-quoted
(double-quoted if the value has an apostrophe). Values combining an apostrophe
with a backslash, double quote, `$` or backtick, or containing a newline, are
refused. This fixes values with spaces (such as `AIS_CATCHER_ARGS`) being passed
to Compose as one argument. The installed `vesselstackctl` defaults to the
installation's `VESSELSTACK_ROOT`, so a custom root works without exporting it.
Boat Chat and Control Panel token checks no longer fail on non-ASCII header
text, `GET`/`HEAD`/`POST /api/settings` now need the access code (the Settings page
sends the code saved by **Connect**; see [Boat Chat setup](BOAT_CHAT.md)), unauthenticated loopback settings writes
need a `localhost`/loopback or configured `BOAT_CHAT_HOST` `Host` name (DNS
rebinding defense; configure `BOAT_CHAT_SETTINGS_TOKEN` to change settings from
other hosts), and power tracking splits intervals at the zero crossing so
inferred charge and observed discharge are no longer overstated. Rollback:
reinstall the previous release; no data migration is involved. Re-run the
installer after upgrading to rewrite the env files in the new format.

**Upgrade notes (secure defaults).** After upgrading, re-run the installer so it
generates `BOAT_CHAT_ACCESS_TOKEN` and renders the new bind variables. Until it
does, Boat Chat answers 401 to chat, status and data requests, and Compose
publishes InfluxDB, Prometheus and MQTT on loopback. Reconnect each browser with
**Connect** using the new token. External devices that publish to MQTT or
InfluxDB over the LAN stop working until `VESSELSTACK_DATA_BIND_ADDRESS` is set
to a trusted address. Settings writes lose the old private-LAN trust (use the
settings token). Scoped read-only Influx tokens for Boat Chat and Grafana are
not implemented (deferred: InfluxDB cannot be given a chosen token value, so a
scoped token must be created after start and written back into the config and
two services, which is fragile); both still use the admin token.

**Verification.** `python3 -m unittest discover -s tests -p 'test_*.py'`,
`tests/test-distribution.sh`, `curl -i -X POST http://127.0.0.1:8765/api/chat`
(expect 401 without the token) and
`docker compose --env-file /opt/vesselstack/config/vesselstack.env -f /opt/vesselstack/compose.yml config`
(ports show `127.0.0.1:` for InfluxDB, Prometheus and MQTT).

**Rollback.** Restore the previous release with the `vesselstack-update`
rollback; to temporarily restore the old network exposure set
`VESSELSTACK_DATA_BIND_ADDRESS="0.0.0.0"` and re-run the installer. The generated
`BOAT_CHAT_ACCESS_TOKEN` line is harmless to older releases.

The owner overview now distinguishes missing/stale readings and contradictory
health indicators, displays observation age, refreshes periodically and retains
explicitly outdated information during connection loss. Chat preserves per-answer
evidence and feedback, renders responses as text, and handles cancellation and
conversation restoration. Complex explanations keep computed evidence intact.
VesselStack's configured URLs, buckets, timezone and branding remain supported.

Live status remains protected by a dedicated access token by default. Configure
`BOAT_CHAT_ACCESS_TOKEN` in private Boat Chat settings before connecting the owner
UI. `BOAT_CHAT_ALLOW_OPEN=true` is an explicit opt-out for a reviewed, trusted
installation; it is not the public default. Settings writes retain their
separate protection. Integration settings survive provider-settings saves.

Fuel economy now uses complete, finite, nonnegative fuel/RPM/speed observations
from the same minute and states matched coverage. A reusable comparison is:

```bash
python3 scripts/cruise-efficiency.py --days 90 --minimum 15 --maximum 40 --step 2.5
```

The command requires the deployment's private Influx settings and configured
history bucket. It reports combined US gal/h, nm/gal, gal/100 nm, mean RPM and
sample support; SOG/current effects and transitions remain. `--json` is supported.
`vesselstackctl status`/`doctor` now require Boat Chat's `ok` and `ready` flags,
rather than accepting any successful HTTP response.

For development from another machine, use a clean source checkout and follow
[Development and safe updates](DEVELOPMENT.md). Git pulls update source only.
The existing lifecycle updater remains the release-update path; an optional
application-only helper offers preview, tests, per-file backup and guarded rollback
without touching infrastructure. Dependencies and database migrations remain
separate reviewed work. Source facts now live in `boat_facts.example.json`; actual
`boat_facts.json` and other operational data are ignored, not published.

**Upgrade notes (secrets, readiness, retention, update trust).**

- Re-running the installer no longer regenerates InfluxDB/Grafana/MQTT secrets;
  the control panel shows the init-only ones as read-only (see "Rotating
  secrets"). The Mosquitto password file is no longer truncated on every start.
- `bootstrap-influx.sh` waits for InfluxDB health and treats list errors as
  errors. The downsample task is updated once to the numeric-only version.
- New installs keep 30 days of raw data. Existing buckets are unchanged unless
  you set `INFLUXDB_RAW_RETENTION` (deletes older raw data; back up first).
- `vesselstack-update` now verifies signatures with a root-controlled keyring
  (`VERIFY_GNUPGHOME` / `VERIFY_ALLOWED_SIGNERS`). **Before the next update,
  import the maintainer key there**, or verification fails (the checkout
  owner's keyring is no longer consulted).
- Legacy `boat-chat/{memory,data,boat-chat.env}` left under the code tree
  because the new location already existed are moved to a root-only
  `/var/lib/vesselstack/quarantine/boat-chat-legacy-<timestamp>.XXXXXX` directory
  (see below) with a warning; nothing is deleted. Review, merge what you need and remove it.

**Verification.** `python3 -m unittest discover -s tests -p 'test_*.py'`,
`tests/test-distribution.sh`, `tests/test-clean-install.sh` and
`tests/test-update-launcher.sh` (as root, on Linux),
`vesselstackctl doctor` (disk line), and
`docker exec vesselstack-influxdb influx bucket list` after `restart`.

- The legacy quarantine moved out of the data tree: it is now
  `/var/lib/vesselstack/quarantine/boat-chat-legacy-<timestamp>.XXXXXX` (root
  0700, verified not to be a symlink), outside every service-writable path.
  Quarantine directories that earlier 0.4.0 builds created in
  `$VESSELSTACK_DATA` are moved there on the next install.
- The installer now installs `vesselstack-update` (`/usr/local/sbin`) and its
  helper (`/usr/local/libexec/vesselstack/deploy-chat.py`) root-owned; see
  "One-command application updates" for the new root-only rules.

**Rollback.** Install the previous release's scripts and re-run the installer;
secrets are preserved either way. Unset `INFLUXDB_RAW_RETENTION` (deleted raw data
is not recoverable; restore from backup if needed). A pre-existing downsample task
can be left as is. For the update trust change set `ALLOW_UNSIGNED_SOURCE="true"`
temporarily (home lab only) or import the key. The quarantine directory can be
deleted once reviewed.

#### One-command application updates

`install.sh` installs the launcher (`/usr/local/sbin/vesselstack-update`) and the
helper it runs (`/usr/local/libexec/vesselstack/deploy-chat.py`), both
`root:root` mode 0755 in root-owned directories. **Bootstrap:** the first copy of
the helper comes from running the installer once (`sudo ./install.sh --config ...`);
until then the launcher refuses to run as root. New helper versions only arrive
the same way, i.e. through a reviewed release installed by `install.sh` /
`vesselstackctl update`, never from the update flow itself. Then create the
private per-machine configuration once:

```bash
sudo install -d -m 0755 /etc/vesselstack
sudo install -m 0600 templates/update.env.example /etc/vesselstack/update.env
sudoedit /etc/vesselstack/update.env
```

As root the launcher enforces: (1) `update.env` and every parent directory up to
`/` must be root-owned and not group/world-writable (the file is sourced as
root), and it must not live inside `SOURCE_DIRECTORY`; (2) it executes **only**
the installed helper, never `scripts/deploy-chat.py` from the checkout, and
only if that file and its whole path chain are root-owned and not
group/world-writable; (3) the helper itself also refuses to run as root from a
location an unprivileged user controls (this includes calling a source-checkout
copy directly with `sudo python3 .../scripts/deploy-chat.py`; use the installed
copy). Home-lab development as a normal user (non-root) still works: the
launcher then prefers `scripts/deploy-chat.py` from the checkout (falling back to
the installed copy) and applies none of the root-ownership checks; the helper's
own pull/verify/test logic is unchanged. Upgrade: run the installer once; the
old manually installed launcher keeps working only after the helper exists.
Rollback: reinstall the previous release (its installer places the previous
launcher/helper); nothing else changes.

Set the separate source/live paths and actual chat/Telegram unit names. After
pushing changes to the source checkout's upstream branch, run on the boat:

```bash
sudo vesselstack-update          # fetch, verify, test, preview; no service restart
sudo vesselstack-update --apply  # same, then back up, apply and verify
```

Trust model: pulled source is untrusted until verified. The launcher refuses to
fetch unless the checkout's `origin` equals `EXPECTED_REMOTE_URL` (default
`https://github.com/jclima/VesselStack.git`). **Root never runs `git archive` or
`git verify-*` inside the checkout**, whose `.git/config` filters,
`.git/info/attributes` and hooks the checkout owner controls. Instead root
fetches the pinned remote's branches and tags into its **own root-owned bare
mirror** (`UPDATE_MIRROR_DIR`, default `/var/lib/vesselstack/update-mirror`,
mode 0700, fsck-checked fetch; without `--pull` the checkout's refs are imported
into the mirror over file transport), requires the fetched commit there to have
a valid signature (`git verify-commit`) or a validly signed tag
(`git verify-tag`), and only then fast-forwards the checkout to that exact
commit. Verification runs
with a fixed configuration (`git -c core.hooksPath=/dev/null -c gpg.program=gpg`,
system and global git config ignored, no repository-local overrides) against a
**root-controlled** trust anchor, not the checkout owner's keyring:
`VERIFY_GNUPGHOME` (a GNUPGHOME holding only the maintainer public key) or
`VERIFY_ALLOWED_SIGNERS` (an SSH allowed-signers file) in `update.env`. When
running as root, that path and its parents must be root-owned and not
group/world-writable. After verification the tests and the deployed bytes both
come from the verified commit object, exported **as raw blobs** from the mirror
(`git ls-tree -r -z` + `git cat-file blob`; no `git archive`, so attributes,
`export-subst`, filters and textconv cannot change bytes; symlinks, submodules,
unsafe paths and modes other than 100644/100755 are refused) into a private tree
the test user cannot write, not the working tree; HEAD, the clean state and the tree
are re-checked after the tests and again just before apply, and any change aborts. `scripts/check-public-tree.py` and
the unit tests run as the checkout owner via `runuser`, never as root; a
root-owned checkout is refused. Import the maintainer signing key into that root-controlled keyring before first
use, e.g. `sudo install -d -m 700 /etc/vesselstack/gnupg` then
`sudo GNUPGHOME=/etc/vesselstack/gnupg gpg --import maintainer.asc`. Home-lab only: set `ALLOW_UNSIGNED_SOURCE="true"` in
`/etc/vesselstack/update.env` to accept unsigned source (a warning is printed);
do not enable it on a vessel others can push code to. The checkout owner can no longer
change what root deploys after verification, because root deploys the verified
commit's content. Upgrade note: 0.4.0 adds the mirror;
no action is required (it is created on first run) but a root-owned,
non-writable `/var/lib/vesselstack` path is enforced. Roll back
by restoring a printed backup (below) or by setting `ALLOW_UNSIGNED_SOURCE`
back to `false`.

If Telegram code changes, an already-active configured worker also restarts;
a stopped worker remains stopped. Restart units are saved in the backup receipt
and used during rollback. Restore a printed backup directory with
`sudo vesselstack-update --rollback /path/to/backup-directory`.

These commands update Boat Chat Python/static assets. Dependencies, migrations,
service definitions, dashboards and container/infrastructure changes require the
separate reviewed release workflow. The launcher, helper and config stay installed
locally across Git pulls; they change only when you run a reviewed release's
`install.sh` (the update flow never replaces its own helper).
See [development/update details](DEVELOPMENT.md).

#### Selective core component lifecycle

The installed `VESSELSTACK_DISABLED_COMPONENTS` setting is a comma-separated
list of core services to exclude: `homeassistant`, `influxdb`, `grafana`,
`prometheus`, `mosquitto`, and `heimdall`. Empty (the default) starts all six;
unknown or duplicate IDs are rejected. `install.sh --start` and
`vesselstackctl start` pass only selected services to Compose, preserve offline
`--pull never`, and skip InfluxDB provisioning and Mosquitto force-recreation
when those services are disabled.

Use `sudo vesselstackctl component remove ID` to persist a removal and run
`docker compose rm --stop --force ID` without deleting volumes, or
`sudo vesselstackctl component add ID` to persist inclusion and start it. Adding
Mosquitto refreshes its managed access files; adding InfluxDB runs its bootstrap
when available. Do not edit the membership field in the Control Panel directly;
use the Disable (⋯ menu) and Enable controls on the Home tab's component cards. CLI and Control Panel lifecycle operations should
be run one at a time because external CLI requests are not cross-process
serialized.
