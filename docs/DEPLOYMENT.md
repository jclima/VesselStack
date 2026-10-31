# Deployment runbook: VesselStack 0.5.1 to a home-lab Pi and the boat Pi

This is an operator runbook for taking release 0.5.1 through two targets:

1. **Home lab**: Raspberry Pi 4B, internet-connected, used for staging, a 48 h+
   soak and rehearsals (CAN replay through `vcan0`).
2. **Boat**: Raspberry Pi 5 on the boat's own router/LAN, **not connected to the
   internet**. A laptop or another Pi on that LAN reaches it over SSH. Updates
   are carried in as the release tarball plus an offline bundle and pushed with
   `scp`/`rsync`.

Both are arm64 (Raspberry Pi OS 64-bit). Every command below was checked against
the scripts in this repository; where a thing does not exist the text says so.

> **Nothing here should be run without the owner's review.** Steps tagged
> **[MUTATES HOST]** change the machine they run on. Steps tagged **[read-only]**
> do not. Placeholders (`YOUR_USER`, `BOAT_PI`, `LAB_PI`, `/path/to/...`) must be
> replaced; never paste real positions, MMSIs, call signs, hostnames, IP
> addresses, tokens or logs into the repository or into shared channels.
> Do not deploy to the boat while underway or while relying on the computer for an
> anchor watch or alarm.

Related: [INSTALLATION.md](INSTALLATION.md) (hardware and first install),
[README.md](../README.md) (reference for every variable and command).

Release status: `v0.5.1` is the published lab prerelease and is deliberately
not latest; `v0.4.4` remains the latest stable release. This exception uses an
annotated unsigned tag because no signing key is available. Do not retag or
promote `v0.5.1` as stable; a future stable release needs a new signed tag after
the real-Pi gates pass.

## Simple path: one command

For most people the guided installer is all that is needed, on both targets.
The detailed runbook below is the advanced path: signing, soak, rehearsals,
carrying bundles, rollback and the acceptance checklist.

**Home lab (internet-connected Pi).** On the Pi:

```bash
curl -fsSL https://github.com/jclima/VesselStack/releases/download/v0.5.1/get-vesselstack.sh | sudo bash
```

(`.../releases/latest/download/get-vesselstack.sh` works only once a release is marked latest.)
Do it over plain SSH (`ssh -t YOUR_USER@LAB_PI`): no screen, browser or tunnel is needed.
It installs prerequisites, asks before installing Docker, verifies the release
sha256 (compare it with the value recorded in section 2), asks the configuration
questions in the terminal (Enter takes the default, `?` explains, a review screen at the
end), then runs `install.sh --dry-run`, `install.sh --start` and `vesselstackctl doctor`,
and prints the service addresses, the Boat Chat token command and the control panel SSH
tunnel hint as plain text. If a VesselStack is already
installed it offers `vesselstackctl update` (with its backup) instead of
overwriting. You can read the script before running it: download with `curl -fsSLO`,
read it, then `sudo bash get-vesselstack.sh`. Without questions (scripts, no terminal):
`... | sudo bash -s -- --yes --set BOAT_NAME=Foo --set SOCKETCAN_ENABLE=true`, or
`--defaults`, or `--yes --config ./vesselstack.env`; `--web-wizard` selects the optional
browser wizard instead of the terminal questions.

**Boat (offline Pi).** The boat has no internet, so carry the files in (section 4):
`get-vesselstack.sh`, `vesselstack-0.5.1.tar.gz` with its `.sha256`, and the
offline bundle (copy the bundle root-owned, `chmod -R go-w`). Install Docker and
the apt prerequisites while the Pi still has internet (the script cannot install
packages offline and tells you what is missing), then:

```bash
sudo bash get-vesselstack.sh --offline --bundle /path/to/offline-bundle \
  --tarball /path/to/vesselstack-0.5.1.tar.gz
```

It verifies the tarball checksum and the bundle (`prepare-offline-bundle.sh
--verify`), loads the images (`--load`) and runs the offline installer or, on an
existing install, `VESSELSTACK_OFFLINE=true VESSELSTACK_WHEELHOUSE=... vesselstackctl update DIR`
(directory first, mode in the environment, so it also works through an older installed
`vesselstackctl`). Add `--sha256 HASH` with the
value you recorded on the home lab to pin the tarball to your own build.

## 0. Prerequisites and read-only pre-flight

Run on each Pi before anything else. All **[read-only]**.

```bash
cat /etc/os-release                 # expect Raspberry Pi OS / Debian 12+ (64-bit)
uname -m                            # must print aarch64
dpkg --print-architecture           # must print arm64
python3 --version                   # 3.11+ (the lock covers 3.11-3.13)
docker --version
docker compose version              # must be Compose v2
systemctl is-active docker
id boat                             # service account (VESSELSTACK_USER); create per INSTALLATION.md if absent
df -h / /opt /var/backups           # >= 1 GiB free for data is the doctor FAIL threshold; plan far more
findmnt -no SOURCE,FSTYPE /opt      # SD card (mmcblk) vs SSD (sda/nvme)
ss -ltn | grep -E ':(80|443|1883|3000|8086|8123|8765|8780|9090|43000)\b' || echo "ports free"
cat /opt/vesselstack/config/installed-version 2>/dev/null || echo "not installed"
```

Storage: put `/opt/vesselstack-data` (InfluxDB, Grafana, Home Assistant) on an
SSD/NVMe on the Pi 5; an SD card wears out under time-series writes. Keep
backups on a **different** device (see section 5). Docker's data root also grows
with images; check `docker system df` if the root filesystem is small.

Pi 5 only, CAN HAT (**[read-only]** until the vendor overlay is in place):

```bash
ip -details link show can0          # must exist before SOCKETCAN_ENABLE=true
dmesg | grep -iE 'can|mcp25|spi'    # overlay/oscillator/SPI problems show here
```

VesselStack does **not** edit boot firmware. Add the HAT vendor's overlay to
`/boot/firmware/config.txt` yourself (see [INSTALLATION.md](INSTALLATION.md)
section 4) and reboot; if `can0` is absent, stop. Physical `can*` interfaces get
`type can bitrate 250000 restart-ms 100` (`SOCKETCAN_BITRATE`) and are never
auto-created.

## 1. Release signing with an SSH signing key

**How verification actually works in this repository.** Signature checking is
implemented by `scripts/deploy-chat.py` (run through `vesselstack-update`). It
fetches the pinned `origin` into a root-owned mirror and requires the upstream
branch tip to have a valid commit signature **or** a validly signed tag pointing
at it. It needs network access to `origin`, so it is a home-lab/connected-machine
flow. **`install.sh` and `vesselstackctl update` do not verify signatures** of the
tarball or directory they are given. On the boat the integrity gate for the
carried tarball is the sha256 you recorded at build time (section 2) plus the
signed tag you verified on the maintainer machine.

On the **maintainer machine** (private key never leaves it):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/vesselstack_signing -C "vesselstack release signing"
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/vesselstack_signing.pub
git config --global tag.gpgSign true      # optional: sign every tag
```

Build the allowed-signers file. The principal must equal the e-mail in your git
`user.email` (the tagger identity); `namespaces="git"` restricts the key to git
signatures:

```bash
printf '%s namespaces="git" %s\n' "$(git config user.email)" \
  "$(cat ~/.ssh/vesselstack_signing.pub)" > allowed_signers
git config gpg.ssh.allowedSignersFile "$PWD/allowed_signers"   # local verification only
```

Only the **public** key line travels. Install it on each Pi, root-owned, in a
root-owned directory chain (the helper refuses an allowed-signers path that, or
whose parents, is not root-owned and free of group/world write):

```bash
# [MUTATES HOST] on each Pi that will run vesselstack-update
sudo install -d -m 0755 /etc/vesselstack
sudo install -m 0644 -o root -g root allowed_signers /etc/vesselstack/allowed_signers
```

Configure `update.env` (variable names are from `templates/update.env.example`):

```bash
# [MUTATES HOST]
sudo install -m 0600 templates/update.env.example /etc/vesselstack/update.env
sudoedit /etc/vesselstack/update.env
#   SOURCE_DIRECTORY=...        separate checkout, NOT inside /etc/vesselstack, owned by an unprivileged user
#   TARGET_DIRECTORY="/opt/vesselstack"
#   VERIFY_ALLOWED_SIGNERS="/etc/vesselstack/allowed_signers"
#   VERIFY_GNUPGHOME=""         leave empty when using SSH signatures
#   ALLOW_UNSIGNED_SOURCE="false"   keep false on the boat
#   EXPECTED_REMOTE_URL / UPDATE_MIRROR_DIR: defaults are fine
```

`vesselstack-update` only runs as root after `install.sh` has installed the
root-owned helper once. `ALLOW_UNSIGNED_SOURCE="true"` is a home-lab-only escape
hatch; never set it on the boat. The boat has no route to `origin`, so
`vesselstack-update` is not the boat's update path; leave it unconfigured there
unless you later add a connection you trust.

Cut and verify the tag (on the maintainer machine, from merged `main`):

```bash
git checkout main && git pull --ff-only
git tag -a v0.5.1 -m "VesselStack 0.5.1 lab prerelease"
git push origin v0.5.1
```

For this explicitly authorized lab prerelease, `v0.5.1` is an annotated,
unsigned tag: no SSH/GPG signing key is available. Do not claim
`git verify-tag` success, and do not use this exception for a stable release.
Future stable versions still require a cryptographically signed tag.

## 2. Cut and build

The release workflow builds the files on GitHub when the tag is pushed and attaches them
to a **draft** release; that CI-built draft is authoritative (see
[DEVELOPMENT.md](DEVELOPMENT.md)). Take the recorded sha256 values from the draft. A
local build below is a cross-check: on Linux (GNU tar) it is byte-identical to CI for
the same commit; on macOS the archive is valid but may differ.

From a clean clone, so no stray files can matter (`build-package.sh` packages
`git archive HEAD` only and refuses tracked private artifacts):

```bash
git clone https://github.com/jclima/VesselStack.git vs-release && cd vs-release
git checkout v0.5.1
cat VERSION                                  # 0.5.1
VESSELSTACK_BUILD_REQUIRE_CLEAN=true ./build-package.sh
```

It writes `generated/vesselstack-0.5.1.tar.gz` and
`generated/vesselstack-0.5.1.tar.gz.sha256`, and prints the sha256 and
`commit <sha>`. **Record both values** in your release notes (out of band from
the tarball): they are the boat's acceptance check.

```bash
git rev-parse v0.5.1^{commit}                # must equal the printed commit
tar -tzf generated/vesselstack-0.5.1.tar.gz | head -20
tar -xzOf generated/vesselstack-0.5.1.tar.gz vesselstack-0.5.1/COMMIT
tar -xzOf generated/vesselstack-0.5.1.tar.gz vesselstack-0.5.1/BUILD_INFO
tar -tzf generated/vesselstack-0.5.1.tar.gz | grep -E '\.env$|boat_facts|\.venv|memory/|data/' \
  || echo "no private artifacts"
python3 scripts/check-public-tree.py
```

(`vesselstack.example.env` is expected in the listing; a real `*.env` is not.)
`scripts/update-requirements-lock.sh` is for maintainers when
`boat-chat/requirements.txt` changes; for an unchanged 0.5.1 the lock ships in
the tarball and you do not run it.

## 3. Home lab (Pi 4B, connected)

### 3.1 Install

Copy the tarball to the lab Pi, check it, unpack it root-owned:

```bash
scp generated/vesselstack-0.5.1.tar.gz* YOUR_USER@LAB_PI:~/
# on LAB_PI:
sha256sum -c vesselstack-0.5.1.tar.gz.sha256        # [read-only]
sudo install -d -m 0755 /var/lib/vesselstack/releases       # [MUTATES HOST]
sudo tar -xzf vesselstack-0.5.1.tar.gz -C /var/lib/vesselstack/releases --no-same-owner
sudo chown -R root:root /var/lib/vesselstack/releases/vesselstack-0.5.1
sudo chmod -R go-w /var/lib/vesselstack/releases/vesselstack-0.5.1
cd /var/lib/vesselstack/releases/vesselstack-0.5.1
```

Configuration: either the terminal wizard (`python3 vesselstack-wizard.py --cli`, over plain
SSH; add `--defaults` and `--set KEY=VALUE` for no questions), the optional browser wizard
(loopback only; tunnel it with
`ssh -L 8088:127.0.0.1:8088 YOUR_USER@LAB_PI`, run `python3 vesselstack-wizard.py` as a normal user and open the printed `http://127.0.0.1:8088/#token=...` address)
or edit a copy of the example. Keep it out of git and mode 600:

```bash
install -m 0600 vesselstack.example.env ~/vesselstack-lab.env
${EDITOR:-nano} ~/vesselstack-lab.env
```

Lab settings worth checking (all defined in `vesselstack.example.env`):

```bash
SIGNALK_MODE="docker"                      # self-contained; "existing" needs a running SignalK
SOCKETCAN_ENABLE="true"
SOCKETCAN_INTERFACE="vcan0"                # bitrate is ignored for vcan*
VESSELSTACK_CAN_CREATE_VCAN="true"         # modprobe vcan + ip link add at boot
VESSELSTACK_BACKUP="/media/vesselstack-backup/backups"   # must be a mounted device or backup refuses
INFLUXDB_RAW_RETENTION="30d"               # see 3.6
BOAT_CHAT_ACCESS_TOKEN="GENERATE"          # default; generated per host
```

Leave `BOAT_MMSI`/`BOAT_CALLSIGN` empty in the lab if you can. Then:

```bash
sudo ./install.sh --config ~/vesselstack-lab.env --dry-run   # [read-only] must say "Preflight passed"
sudo ./install.sh --config ~/vesselstack-lab.env             # [MUTATES HOST] render/install, no start
sudo ./install.sh --config ~/vesselstack-lab.env --start     # [MUTATES HOST] pulls images, starts stack
sudo vesselstackctl doctor
sudo vesselstackctl urls
cat /opt/vesselstack/config/installed-version                # 0.5.1
```

### 3.2 Replay a private CAN log

Record your own log on the boat with `candump -l can0` (can-utils). Logs contain
vessel identifiers and positions: keep them **outside the repository** (the
public-tree check rejects `.log`), never commit or share them, and delete lab
copies when finished.

```bash
sudo apt install -y can-utils                              # [MUTATES HOST]
ip -details link show vcan0                                # [read-only] exists and is UP
canplayer -I /path/to/private-candump.log vcan0=can0       # add -l i to loop forever
ip -statistics link show vcan0                             # RX packets should climb
```

Point SignalK's NMEA 2000/SocketCAN connection at `vcan0`. The rendered
`vesselstack-socketcan.service` runs `ip link set vcan0 up` only. Reboot once and
confirm the unit recreates `vcan0` (`systemctl status vesselstack-socketcan`).

### 3.3 Firewall decision

The firewall is opt-in (`VESSELSTACK_FIREWALL_ENABLE`, default false) and applies
to one named untrusted interface (`VESSELSTACK_UNTRUSTED_INTERFACE`, example
`wlan0`). Preview without changing anything:

```bash
sudo vesselstack-firewall print           # [read-only] renders the iptables-restore payloads
```

Decide per host: on the lab Pi, leave it off or enable the default policy
(blocks MQTT/InfluxDB/Prometheus/Grafana on that interface). Strict mode
(`VESSELSTACK_FIREWALL_STRICT`, allow-list `VESSELSTACK_FIREWALL_ALLOW_TCP`,
default `22`) drops everything else on that interface: keep Ethernet or console
access before enabling it. After enabling, check the live kernel
(`sudo iptables -S VESSELSTACK-INPUT`). See the **known gaps** in section 8.

### 3.4 Soak (48 h or more)

Daily, and after any reboot, **[read-only]**:

```bash
sudo vesselstackctl doctor
sudo vesselstackctl logs all | tail -n 200
df -h /opt/vesselstack-data
```

Keep `canplayer -l i` running, confirm Grafana/Home Assistant keep receiving
values, and record the `doctor` result and data-directory size each day.

### 3.5 Rehearsals (do all of these before the boat)

1. **Update from the previous version.** On a spare lab install (or before the
   soak), install the prior tag the normal way (`git checkout v0.3.0` in a clean
   clone, `sudo ./install.sh --config ... --start`), then update with 0.5.1 from
   the unpacked release directory:

   ```bash
   sudo vesselstackctl update /var/lib/vesselstack/releases/vesselstack-0.5.1   # [MUTATES HOST]
   ```

   It runs `install.sh --dry-run`, takes a verified backup into
   `VESSELSTACK_BACKUP` (stopping the stack briefly), then installs with
   `--start`. `scripts/migrate.sh` runs any `scripts/migrations/<version>.sh`
   steps in order; `installed-version` is written last.

2. **Backup, verify, restore.**

   ```bash
   sudo vesselstackctl backup /media/vesselstack-backup/backups      # [MUTATES HOST] stops stack briefly
   sudo vesselstackctl verify-backup /media/vesselstack-backup/backups/vesselstack-STAMP.tar.gz
   sudo vesselstackctl restore /media/vesselstack-backup/backups/vesselstack-STAMP.tar.gz --yes   # [MUTATES HOST]
   ```

   Restore stages a second copy (needs room for old + new) and re-runs the
   restored bundled installer, which needs network for `pip` unless you use
   `--skip-installer` and run the installer yourself (offline: see 3.5.3).

3. **Offline install with the internet blocked.** Build the bundle on a
   connected machine (docker + python3 + pip; defaults target aarch64, Python 3.11,
   `linux/arm64` images; add `--python-version 3.13` if the Pi's Python differs):

   ```bash
   scripts/prepare-offline-bundle.sh --list                       # [read-only] image list
   scripts/prepare-offline-bundle.sh --output ./offline-bundle    # downloads wheels + images
   scripts/prepare-offline-bundle.sh --verify ./offline-bundle
   ```

   Copy it into the release directory, make it root-owned, block the network on
   the lab Pi (for example unplug the router's WAN uplink; the repository provides
   no network-block command), then:

   ```bash
   sudo chown -R root:root offline-bundle && sudo chmod -R go-w offline-bundle
   sudo scripts/prepare-offline-bundle.sh --load /path/to/offline-bundle   # docker load
   sudo VESSELSTACK_OFFLINE=true VESSELSTACK_WHEELHOUSE=/path/to/offline-bundle/wheelhouse \
     vesselstackctl update /var/lib/vesselstack/releases/vesselstack-0.5.1
   ```

   The bundle path and **every parent** must be root-owned and not
   group/world-writable, or root refuses it.

4. **Disk and retention.** `doctor` prints data-filesystem usage and warns above
   80 % (`VESSELSTACK_DISK_WARN_PERCENT`).

### 3.6 InfluxDB retention note

New installs create the raw bucket with 30 d retention. Setting
`INFLUXDB_RAW_RETENTION` on an **existing** install permanently deletes older raw
data (empty = leave as is, `0` = keep forever). The 1-minute history bucket keeps
one year. Back up first; apply with `sudo vesselstackctl restart`.

## 4. The carry bundle

Assemble one directory on the connected machine; transfer it as a unit:

| File | Source |
| --- | --- |
| `vesselstack-0.5.1.tar.gz` | `./build-package.sh` |
| `vesselstack-0.5.1.tar.gz.sha256` | same run |
| `offline-bundle/` (`wheelhouse/`, `images/`, `BUNDLE_INFO`, `MANIFEST.sha256`) | `scripts/prepare-offline-bundle.sh --output ./offline-bundle` |
| Release notes with the recorded sha256 + commit | your notes, **kept separately** |

The bundle holds no configuration, credentials or vessel data, and
`BUNDLE_INFO` pins the release and the lock hash. Rebuild it whenever the release
(or `requirements.lock`) changes. SignalK `native` mode installs from npm and is
**not** covered by the bundle; use `SIGNALK_MODE="docker"` or `existing`.

Transfer over the boat LAN from the laptop (SSH keys, no passwords; use the
boat's own hostname or address, never write it into the repo):

```bash
rsync -av --progress ./carry/ YOUR_USER@BOAT_PI:~/carry/
# or: scp -r ./carry YOUR_USER@BOAT_PI:~/
```

The upload lands in an unprivileged home directory, so on the Pi copy it to a
root-controlled location before use (section 5).

## 5. Boat install/update, at the dock

Only at the dock, not underway and not during an anchor watch. Have the previous
backup, the recorded sha256 and a console/Ethernet path ready.

1. **Pre-flight and verified backup to separate media (before anything changes).**

   ```bash
   cat /opt/vesselstack/config/installed-version              # [read-only]
   sudo vesselstackctl doctor                                 # [read-only] must be healthy first
   mountpoint /media/vesselstack-backup                       # [read-only] USB/SSD must be mounted
   sudo vesselstackctl backup /media/vesselstack-backup/backups     # [MUTATES HOST] stack stops briefly
   sudo vesselstackctl verify-backup /media/vesselstack-backup/backups/vesselstack-STAMP.tar.gz
   rsync -av YOUR_USER@BOAT_PI:/media/vesselstack-backup/backups/ ./boat-backup/   # from the laptop
   ```

   Keep one copy off the Pi. `vesselstackctl update` also takes its own backup
   into `VESSELSTACK_BACKUP`, but that is not a substitute for this one.

2. **Verify the tarball against the value recorded at build time** (compare with
   your notes, not with the `.sha256` file that travelled with it):

   ```bash
   sha256sum ~/carry/vesselstack-0.5.1.tar.gz     # must equal the recorded value
   ```

3. **Stage root-owned [MUTATES HOST].**

   ```bash
   sudo install -d -m 0755 /var/lib/vesselstack/releases
   sudo tar -xzf ~/carry/vesselstack-0.5.1.tar.gz -C /var/lib/vesselstack/releases --no-same-owner
   sudo cp -a ~/carry/offline-bundle /var/lib/vesselstack/releases/
   sudo chown -R root:root /var/lib/vesselstack/releases/vesselstack-0.5.1 /var/lib/vesselstack/releases/offline-bundle
   sudo chmod -R go-w /var/lib/vesselstack/releases/vesselstack-0.5.1 /var/lib/vesselstack/releases/offline-bundle
   cat /var/lib/vesselstack/releases/vesselstack-0.5.1/COMMIT       # matches recorded commit
   sudo /var/lib/vesselstack/releases/vesselstack-0.5.1/scripts/prepare-offline-bundle.sh \
     --verify /var/lib/vesselstack/releases/offline-bundle           # [read-only]
   ```

4. **Update [MUTATES HOST].** First install on a fresh Pi uses `install.sh --config`
   (section 3.1 form, plus `--offline` and `VESSELSTACK_WHEELHOUSE`); an existing
   boat uses:

   ```bash
   sudo /var/lib/vesselstack/releases/vesselstack-0.5.1/scripts/prepare-offline-bundle.sh \
     --load /var/lib/vesselstack/releases/offline-bundle      # verifies, then docker load
   sudo VESSELSTACK_OFFLINE=true VESSELSTACK_WHEELHOUSE=/var/lib/vesselstack/releases/offline-bundle/wheelhouse \
     vesselstackctl update /var/lib/vesselstack/releases/vesselstack-0.5.1
   ```

5. **Verify.**

   ```bash
   cat /opt/vesselstack/config/installed-version     # 0.5.1
   sudo vesselstackctl doctor                        # RESULT healthy
   sudo reboot
   # after reconnecting:
   sudo vesselstackctl doctor
   ip -statistics link show can0                     # RX packets increasing; errors/dropped not rising
   ```

   Run the `can0` check twice a minute apart. Rising errors/dropped counters mean
   wiring, bitrate, termination or power: stop and correct before relying on it.

6. **Access tokens.** `BOAT_CHAT_ACCESS_TOKEN` (and the Control Panel, MQTT,
   Grafana and InfluxDB secrets) are `GENERATE`d **per host** and preserved on
   re-install. Never copy `config/*.env` or tokens from the lab Pi to the boat.
   Read the token on the boat when you need it:

   ```bash
   sudo grep '^BOAT_CHAT_ACCESS_TOKEN=' /opt/vesselstack/config/boat-chat.env
   ```

7. **Control panel** (loopback only, default `8780`) through an SSH tunnel from
   the laptop; unlock with `CONTROL_PANEL_TOKEN` from
   `/opt/vesselstack/config/control-panel.env`:

   ```bash
   ssh -L 8780:127.0.0.1:8780 YOUR_USER@BOAT_PI      # then open http://127.0.0.1:8780
   ```

## 6. Rollback and failed updates

**Failed or interrupted update.** `install.sh` is transactional: preflight
(`migrate.sh --check`, Compose validation, offline wheelhouse/image checks) runs
before any file is touched, the Python environment is built in a staging
virtualenv first, and `installed-version` is written last. A failure before the
swap prints "Nothing was changed" and leaves the old install running config. If
the process dies during the later swap, run `vesselstackctl doctor` and treat the
host as suspect: restore the backup below rather than guessing.

**Rollback = restore the pre-update backup.** There is no downgrade support:
`install.sh` and `migrate.sh` refuse an older release over a newer one.

```bash
sudo vesselstackctl verify-backup /path/to/vesselstack-STAMP.tar.gz
sudo vesselstackctl restore /path/to/vesselstack-STAMP.tar.gz --yes --skip-installer   # [MUTATES HOST]
sudo VESSELSTACK_WHEELHOUSE=/path/to/previous-offline-bundle/wheelhouse \
  /opt/vesselstack/installer/install.sh --offline --config /opt/vesselstack/config/vesselstack.env --start
```

Use `--skip-installer` on the boat: without it restore runs the bundled installer
online (it needs PyPI for the virtualenv). Running the restored installer by hand
with `--offline` needs the **matching old release's** wheelhouse and loaded
images (keep the previous version's bundle on the boat). Restore swaps the live
trees to `/opt/vesselstack.pre-restore-STAMP` and
`/opt/vesselstack-data.pre-restore-STAMP`. **These directories hold the
replaced configuration and secrets**; verify the restored system, then delete
them (`sudo rm -rf -- ...`, the command is printed) to free space and remove old
secrets. `vesselstack-update --rollback BACKUP_DIRECTORY` is only for code-only
`vesselstack-update --apply` runs, not for a release install.

## 7. Acceptance checklist

- [ ] Lab prerelease `v0.5.1` annotated tag is intentionally unsigned; CI asset sha256 and commit recorded.
- [ ] Lab: 48 h+ soak with daily `doctor` healthy; replay data visible in Grafana.
- [ ] Lab: update from 0.3.0, backup/verify/restore, and offline install rehearsed.
- [ ] Boat: sha256 of the carried tarball equals the recorded value.
- [ ] Boat: verified backup exists on separate media and a copy is off the Pi.
- [ ] Boat: `installed-version` is `0.5.1`; `doctor` healthy before and after reboot.
- [ ] Boat: `can0` RX counter increases, error/dropped counters flat.
- [ ] Boat: tokens are per-host; no lab secrets copied; control panel reached by tunnel only.
- [ ] Rollback path (backup + previous release + previous offline bundle) is on board.

## 8. Remote access, secrets, and known gaps

**Remote access (later, optional).** Nothing here assumes it. If you add a
Tailscale or other VPN, bind/allow services on that interface deliberately and
update the firewall policy; keep the control panel on loopback plus tunnel. The
repository ships no Tailscale integration.

**Secrets.** Secrets live in `/opt/vesselstack/config/*.env` (mode 600) and in
backups (`chmod 0600` archives, which therefore contain them: store them
encrypted/off-boat). Never commit env files, logs, backups, CAN logs, positions,
MMSIs, call signs or device identifiers. `*.pre-restore-*` directories keep old
secrets until you delete them. The signing private key stays on the maintainer
machine only.

**Known gaps (not verified in this repository's development environment).**

- Firewall rules were rendered and unit-tested but **not loaded into a live
  kernel**; confirm with `sudo iptables -S VESSELSTACK-INPUT`.
- The `vcan` systemd unit (`vesselstack-socketcan.service` with
  `VESSELSTACK_CAN_CREATE_VCAN=true`) is rendered/tested but has not been
  exercised across a real boot.
- **Real SSH signature verification** against a signed tag has only been tested
  with fixtures; do the first real `git verify-tag` and `vesselstack-update`
  against the allowed-signers file on the lab Pi.
- The **arm64 offline bundle** (wheels for `manylinux*_aarch64`, `linux/arm64`
  images) has not been built and installed on a real Pi; section 3.5.3 is that
  rehearsal. Image tarballs are manifest-checked but not digest-pinned.
- Scoped read-only InfluxDB tokens for Boat Chat and Grafana are **deferred**
  (see README and CHANGELOG); they are not part of 0.4.3.
- The tarball is integrity-checked by sha256 only on the boat (no signature
  check in `install.sh`/`vesselstackctl update`).
