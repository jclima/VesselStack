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

The quick-start URLs (`.../releases/latest/download/get-vesselstack.sh` and
`.../releases/download/vX.Y.Z/get-vesselstack.sh`) only work if the GitHub release
carries exactly these files. Nothing publishes a release for you: the tag-triggered
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
   section 1) and push the tag. The workflow refuses a tag whose commit is not on
   `main`, or that differs from `VERSION`. The explicitly approved `v0.5.2`
   lab-prerelease exception is an annotated unsigned tag because no SSH/GPG key
   is available; it is not a stable promotion and must not be treated as signed.
   The earlier `v0.5.1` tag remains untouched and was not published because its
   release pipeline caught the bridge startup-status race.
4. `.github/workflows/release.yml` then (a) reruns the whole CI workflow on the
   tagged commit, (b) builds with `VESSELSTACK_BUILD_REQUIRE_CLEAN=true` in a
   read-only job and uploads the files as workflow artifacts, and (c) in a separate
   job with write access creates a **draft** release (`--draft --latest=false`) with
   the four files and both sha256 values in the notes. It skips creation if a release
   for the tag already exists. It does not verify the tag signature; do that locally
   before pushing the tag.
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
   upgrade. The test suites use stubs and cannot cover this. Do not publish before it
   passes. For the approved `0.5.2` lab prerelease, run the pinned installer and
   component lifecycle recipe in [CONTROL_PANEL_TESTING.md](CONTROL_PANEL_TESTING.md)
   on the lab first; this exception does not waive the stable-release gates.
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
