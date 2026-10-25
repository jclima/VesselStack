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
carries exactly these files. Nothing publishes a release for you except the
tag-triggered workflow below, and that only creates a **draft**.

1. Bump `VERSION`, `DEFAULT_VERSION` in `scripts/get-vesselstack.sh` (must equal
   `VERSION`; `tests/test-distribution.sh` checks it), the version in the
   README/docs examples, and `CHANGELOG.md`. Update `README.md` as AGENTS.md requires.
2. Run the checks from a clean checkout: `python3 scripts/check-public-tree.py`,
   `tests/test-distribution.sh`, `python3 -m unittest discover -s tests -p 'test_*.py'`,
   and as root on Linux `tests/test-get-vesselstack.sh`, `tests/test-build-package.sh`
   and the install/update tests listed in the README.
3. Commit, create the signed tag (see [DEPLOYMENT.md](DEPLOYMENT.md) section 1) and
   run `./build-package.sh` (set `VESSELSTACK_BUILD_REQUIRE_CLEAN=true`). Record
   the printed sha256 values.
4. Upload these four files to the GitHub release for the tag, byte for byte:
   - `generated/vesselstack-<version>.tar.gz`
   - `generated/vesselstack-<version>.tar.gz.sha256`
   - `generated/get-vesselstack.sh` (default version baked in from `VERSION`)
   - `generated/get-vesselstack.sh.sha256`
5. Verify from a scratch directory after publishing (never run the script piped
   into root before you have read the downloaded copy):
   `curl -fsSLO https://github.com/jclima/VesselStack/releases/download/v<version>/get-vesselstack.sh`,
   `curl -fsSLO .../get-vesselstack.sh.sha256`, `sha256sum -c get-vesselstack.sh.sha256`,
   then run it on a spare Pi with `--help` and a full lab install.
6. Mark the release as the latest only after that works: `releases/latest/download/`
   follows the latest published, non-draft, non-prerelease release.

`.github/workflows/release.yml` automates steps 2 to 4: pushing a `vX.Y.Z` tag
(it never runs on pull requests or branch pushes) checks that the tag equals
`VERSION`, reruns the tests, builds, verifies the checksums and creates a draft
release with the four files; review and publish it by hand. It does not verify
the tag signature; do that locally before pushing the tag.

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
