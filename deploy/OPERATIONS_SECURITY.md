# Deployment security boundaries

The installed systemd configuration uses one Unix identity per service.  The
application identities never own the checkout, `.git`, deploy receipts,
rollback pins, or the trusted rollback runner.

## Provisioning contract

Create the read-only source group and system users before installing the units.
User creation and ACL installation are root provisioning actions and are never
performed by deploy scripts.

```bash
sudo groupadd --system kanae-code
for name in bot bot-dev hermes intervention vi-signal opencode chroma mem0 oracle second-opinion x-scraper mcp-forward systemd-forward; do
  sudo useradd --system --user-group --home-dir "/var/lib/kanae-${name}" \
    --shell /usr/sbin/nologin "kanae-${name}"
  sudo usermod -a -G kanae-code "kanae-${name}"
done
sudo useradd --system --user-group --home-dir /var/lib/discord-bot-deploy \
  --shell /usr/sbin/nologin kanae-deploy
sudo usermod -a -G kanae-code kanae-deploy
sudo useradd --system --user-group --home-dir /var/lib/discord-bot-backup \
  --shell /usr/sbin/nologin kanae-backup
```

`/home/ubuntu` is mode `0750` on the host.  Do not relax it to `0755`.  Grant
only traverse permission to the source-reader group, then grant read/traverse
on the checkouts and required tool installations.  The recursive and default
ACLs include tracked source. Health never opens `.git`; it reads protected
release metadata outside the checkout. No runtime UID receives permission to
read Git metadata or write source.

```bash
sudo setfacl -m g:kanae-code:--x /home/ubuntu
install_checkout_acl() {
  root="$1"
  sudo find "${root}" -path "${root}/.git" -prune -o -type d \
    -exec setfacl -m g:kanae-code:r-x,d:g:kanae-code:r-x {} +
  git -C "${root}" ls-files -z \
    | sudo xargs -0 -r setfacl -m g:kanae-code:r-X --
  sudo setfacl -R -m g:kanae-code:--- "${root}/.git"
}
```

Do not run recursive `chown` at this point. Complete the OAuth and state
migrations below, remove every legacy `.env`, `.state`, `.claude`, and `.venv`
from both checkouts, and only then transfer the secret-free checkouts to
`kanae-deploy`.

The deploy units use `UMask=0022` so files created by Git remain readable under
the default ACL.  Deploy trust files are still explicitly `0600` or `0700`.
Runtime units combine the `kanae-code` read boundary with
`ProtectHome=read-only` and `ProtectSystem=strict`.

## OAuth credential migration

Production, staging, and Hermes are separate OAuth identities. Never copy the
production credential into either other HOME. Each identity requires a primary
and separately authenticated failover credential before its unit is enabled.
The primary paths are `/var/lib/kanae-bot/.claude/.credentials.json`,
`/var/lib/kanae-bot-dev/.claude/.credentials.json`, and
`/var/lib/kanae-hermes/.claude/.credentials.json` respectively. Their failover
files use `.credentials-failover.json` in the same private directory.
The units set explicit `OAUTH_PRIMARY_CREDENTIALS_PATH`,
`OAUTH_FAILOVER_CREDENTIALS_PATH`, and `OAUTH_FAILOVER_ENABLED=true` values.
OAuth failover state remains under that identity's private `BOT_STATE_DIR` and
is never written into a checkout.

```bash
sudo systemctl stop discord-bot discord-bot-dev hermes-discord-codex
for spec in \
  'kanae-bot /var/lib/kanae-bot /root/oauth-stage/prod' \
  'kanae-bot-dev /var/lib/kanae-bot-dev /root/oauth-stage/dev' \
  'kanae-hermes /var/lib/kanae-hermes /root/oauth-stage/hermes'; do
  set -- ${spec}; identity="$1"; home="$2"; source_root="$3"
  sudo test -f "${source_root}/primary.json"
  sudo test -f "${source_root}/failover.json"
  sudo test ! -L "${source_root}/primary.json"
  sudo test ! -L "${source_root}/failover.json"
  sudo install -d -o "${identity}" -g "${identity}" -m 0700 "${home}/.claude"
  sudo install -o "${identity}" -g "${identity}" -m 0600 -- \
    "${source_root}/primary.json" "${home}/.claude/.credentials.json"
  sudo install -o "${identity}" -g "${identity}" -m 0600 -- \
    "${source_root}/failover.json" "${home}/.claude/.credentials-failover.json"
  sudo -u "${identity}" python3 - \
    "${home}/.claude/.credentials.json" \
    "${home}/.claude/.credentials-failover.json" <<'PY'
import json
from pathlib import Path
import sys
import time
for raw in sys.argv[1:]:
    payload = json.loads(Path(raw).read_text(encoding="utf-8"))["claudeAiOauth"]
    assert isinstance(payload.get("accessToken"), str) and payload["accessToken"]
    assert isinstance(payload.get("refreshToken"), str) and payload["refreshToken"]
    assert payload.get("expiresAt", 0) > int(time.time() * 1000) + 300_000
PY
  sudo stat -c '%U:%G %a %n' "${home}/.claude" \
    "${home}/.claude/.credentials.json" \
    "${home}/.claude/.credentials-failover.json"
done
```

Do not print or copy the credential through a shell variable.  Retain the old
file until readiness succeeds.  After verification, archive it into a
root-owned `0700` directory or remove it according to the credential retention
policy.

## Per-service state migration

The shared checkout `.state` and `kanae-state` group are retired.  Each unit
sets `BOT_STATE_DIR` to its private `/var/lib/<service>/state`; Chroma uses
`/var/lib/kanae-chroma/mem0_db`.  Scheduler legacy `.state` log paths are bind
mounted to their own private state directory inside their mount namespace.
No MCP UID can rename or replace the bot's `sessions.json`.

Stop all writers and make a root-only offline retention copy before copying.
The inventory snapshot is enabled after the new live roots exist. Do not copy
`sessions.json`, `runtime_state.json`, history, attachments, or Invest state
into an MCP directory.

```bash
sudo systemctl stop discord-bot hermes-discord-codex \
  mcp-mem0-shared mcp-second-opinion-shared chromadb-server \
  vi-signal-scheduler intervention-scheduler
sudo install -d -o root -g root -m 0700 /var/lib/discord-bot-legacy-private
sudo rsync -a /home/ubuntu/discord-bot/.state/ \
  /var/lib/discord-bot-legacy-private/pre-migration-state/
sudo install -d -o kanae-bot -g kanae-bot -m 0700 /var/lib/kanae-bot/state
sudo rsync -a --chown=kanae-bot:kanae-bot \
  /home/ubuntu/discord-bot/.state/ /var/lib/kanae-bot/state/
sudo install -d -o kanae-hermes -g kanae-hermes -m 0700 /var/lib/kanae-hermes/state
sudo install -d -o kanae-mem0 -g kanae-mem0 -m 0700 /var/lib/kanae-mem0/state
sudo install -d -o kanae-second-opinion -g kanae-second-opinion -m 0700 \
  /var/lib/kanae-second-opinion/state
sudo install -d -o kanae-vi-signal -g kanae-vi-signal -m 0700 \
  /var/lib/kanae-vi-signal/state
sudo install -d -o kanae-intervention -g kanae-intervention -m 0700 \
  /var/lib/kanae-intervention/state
sudo install -d -o kanae-chroma -g kanae-chroma -m 0700 \
  /var/lib/kanae-chroma/mem0_db
sudo rsync -a --chown=kanae-chroma:kanae-chroma \
  /home/ubuntu/discord-bot/.state/mem0_db/ /var/lib/kanae-chroma/mem0_db/

sudo find /var/lib/kanae-bot/state /var/lib/kanae-hermes/state \
  /var/lib/kanae-mem0/state /var/lib/kanae-second-opinion/state \
  /var/lib/kanae-vi-signal/state /var/lib/kanae-intervention/state \
  /var/lib/kanae-chroma/mem0_db -xdev -exec chmod go-rwx {} +
```

Start Chroma, MCP daemons, schedulers, and bots in that order.  Treat empty MCP
state as a new service-local cache/projection and rebuild it through its normal
application path.  Do not blanket-copy the bot state into an MCP state root.
Keep the stopped checkout `.state` read-only until the post-migration snapshot
and readiness checks pass, then archive it outside the checkout.

Before either checkout is recursively assigned to the deploy identity, move
the remaining legacy private paths into root-only retention and remove the
retired shared virtual environment. Staging follows the same clean-checkout,
owner, and ACL contract as production.

```bash
sudo install -d -o root -g root -m 0700 /var/lib/discord-bot-legacy-private
for root in /home/ubuntu/discord-bot /home/ubuntu/discord-bot-dev; do
  for name in .env .state .claude; do
    if sudo test -e "${root}/${name}" || sudo test -L "${root}/${name}"; then
      sudo mv -- "${root}/${name}" \
        "/var/lib/discord-bot-legacy-private/$(basename "${root}")-${name#.}"
    fi
  done
  sudo rm -rf -- "${root}/.venv"
  sudo -u ubuntu git -C "${root}" status --porcelain=v1 --untracked-files=all \
    | grep -q . && { echo "checkout is not clean: ${root}" >&2; exit 1; }
  sudo chown -R kanae-deploy:kanae-deploy "${root}"
  install_checkout_acl "${root}"
done
install_checkout_acl /home/ubuntu/free-code/x-scraper
sudo setfacl -R -m g:kanae-code:r-X \
  /home/ubuntu/.volta /home/ubuntu/.opencode/bin

for root in /home/ubuntu/discord-bot /home/ubuntu/discord-bot-dev; do
  sudo test ! -e "${root}/.env"
  sudo test ! -e "${root}/.state"
  sudo test ! -e "${root}/.claude"
  sudo test ! -e "${root}/.venv"
done
```

## Credential and release trust roots

Repository dotenv files are not trusted inputs.  Install credentials and
release roots outside the checkout.

```bash
sudo install -d -o root -g root -m 0700 /etc/discord-bot/credentials
sudo install -d -o root -g root -m 0755 /etc/discord-bot/release /usr/local/libexec
sudo install -d -o root -g root -m 0755 \
  /var/lib/discord-bot-releases \
  /var/lib/discord-bot-releases/prod \
  /var/lib/discord-bot-releases/dev \
  /var/lib/discord-bot-releases/prod/generations \
  /var/lib/discord-bot-releases/dev/generations
sudo install -d -o kanae-deploy -g kanae-deploy -m 0700 \
  /var/lib/discord-bot-deploy/prod/release-build \
  /var/lib/discord-bot-deploy/dev/release-build
sudo install -o root -g root -m 0755 scripts/bot-rollback.sh \
  /usr/local/libexec/discord-bot-rollback
sudo install -o root -g root -m 0755 scripts/release_artifact_lib.sh \
  /usr/local/libexec/discord-bot-release-lib
sudo install -o root -g root -m 0755 scripts/release_publisher.sh \
  /usr/local/libexec/discord-bot-release-publisher
sudo install -o root -g root -m 0644 \
  docs/2026-07-14-refactor-protected-manifest.sha256 \
  /etc/discord-bot/release/protected-manifest.sha256
sudo tee /etc/sudoers.d/discord-bot-deploy >/dev/null <<'EOF'
Cmnd_Alias DISCORD_RELEASE_RESTART = /usr/bin/systemctl restart chromadb-server, /usr/bin/systemctl restart mcp-mem0-shared, /usr/bin/systemctl restart mcp-second-opinion-shared, /usr/bin/systemctl restart mcp-oracle-shared, /usr/bin/systemctl restart vi-signal-scheduler, /usr/bin/systemctl restart intervention-scheduler, /usr/bin/systemctl restart hermes-discord-codex, /usr/bin/systemctl restart discord-bot, /usr/bin/systemctl restart kanae-mcp-forward, /usr/bin/systemctl restart kanae-systemd-forward, /usr/bin/systemctl restart discord-bot-dev
Cmnd_Alias DISCORD_RELEASE_PUBLISH = /usr/local/libexec/discord-bot-release-publisher *
kanae-deploy ALL=(root) NOPASSWD: DISCORD_RELEASE_RESTART, DISCORD_RELEASE_PUBLISH
EOF
sudo chmod 0440 /etc/sudoers.d/discord-bot-deploy
sudo visudo -cf /etc/sudoers.d/discord-bot-deploy
```

Each deploy builds a complete candidate under
`/var/lib/discord-bot-deploy/<env>/release-build`. The installed root-owned
publisher is the only process allowed to move it into
`/var/lib/discord-bot-releases/<env>/generations/<sha>`. Every regular file
under `src` and `venv` is recorded by path, mode, and SHA-256 in
`release-content.jsonl`; `release.json` pins that manifest digest. The
publisher changes the generation to `root:root` and removes write permission
before publication. It validates source identity and `pip check` before a
directory rename publishes the immutable generation.
Existing generations are never deleted or repaired in place. The only release
switch is one atomic replacement of the relative `current` symlink. Every
affected unit uses `current/src` and `current/venv`, so source, interpreter, and
metadata always resolve through one generation. Deploy and rollback restart all
affected consumers in dependency order and require systemd active state,
Chroma heartbeat, and exact-SHA readiness on ports 3004 and 3002.
The deploy identity records each switch in
`/var/lib/discord-bot-deploy/<env>/activation.json` before activation. This
atomic, fsynced journal survives SIGKILL and records every restarted consumer.
The next deploy resumes it before any HEAD-equal or rollback-pin early exit.
Application health rehashes the complete content manifest at process startup.

```bash
sudo -u kanae-bot test -x \
  /var/lib/discord-bot-releases/prod/current/venv/bin/python
sudo -u kanae-bot test -r \
  /var/lib/discord-bot-releases/prod/current/release.json
! sudo -u kanae-bot test -r /home/ubuntu/discord-bot/.git/HEAD
```

## Read-only backup inventory

Install a root-owned inventory that enumerates every live service state root.
There is no checkout-relative snapshot default.

```json
{
  "format": "discord-bot-state-inventory-v1",
  "services": [
    {"name": "discord-bot", "path": "/var/lib/kanae-bot/state"},
    {"name": "discord-bot-dev", "path": "/var/lib/kanae-bot-dev/state"},
    {"name": "hermes", "path": "/var/lib/kanae-hermes/state"},
    {"name": "mem0", "path": "/var/lib/kanae-mem0/state"},
    {"name": "second-opinion", "path": "/var/lib/kanae-second-opinion/state"},
    {"name": "vi-signal", "path": "/var/lib/kanae-vi-signal/state"},
    {"name": "intervention", "path": "/var/lib/kanae-intervention/state"},
    {"name": "chroma", "path": "/var/lib/kanae-chroma/mem0_db"},
    {"name": "oracle", "path": "/var/lib/kanae-oracle"},
    {"name": "x-scraper", "path": "/var/lib/kanae-x-scraper"},
    {"name": "remote-opencode", "path": "/var/lib/kanae-opencode"},
    {"name": "mcp-forward", "path": "/var/lib/kanae-mcp-forward"},
    {"name": "systemd-forward", "path": "/var/lib/kanae-systemd-forward"}
  ]
}
```

Install this JSON as `/etc/discord-bot/backup-state.inventory`, `root:root
0644`. Grant `kanae-backup` and `kanae-deploy` read/traverse ACLs, including
default ACLs on each live directory, but never write. The deploy identity needs
the same read-only view for the production release-gate rehearsal. Grant the
backup identity write permission only on
`/var/lib/discord-bot-backup/snapshots`. Verify `sudo -u kanae-backup test -w
<live-root>` and `sudo -u kanae-deploy test -w <live-root>` fail for every
inventory entry. Run:

Snapshot stability fingerprints cover non-database files. SQLite database,
WAL, SHM, and rollback-journal bytes are intentionally excluded because a WAL
append or checkpoint is not a logical generation change. Every database is
captured through SQLite `.backup`; the candidate must pass `integrity_check`,
and its logical dump digest is included in `SNAPSHOT_GENERATION`. The relative
path set of SQLite main databases must match before backup, after backup, in
the candidate, and at final validation. Snapshot files, directories, manifest,
and both rename parents are fsynced before publication returns success.

```bash
sudo install -d -o root -g root -m 0755 /var/lib/discord-bot-backup
sudo install -d -o kanae-backup -g kanae-backup -m 0700 \
  /var/lib/discord-bot-backup/snapshots
sudo install -d -o kanae-deploy -g kanae-deploy -m 0700 \
  /var/lib/discord-bot-backup/release-gate

sudo -u kanae-backup env \
  SNAPSHOT_STATE_INVENTORY=/etc/discord-bot/backup-state.inventory \
  SNAPSHOT_RELEASE_METADATA_PATH=/var/lib/discord-bot-releases/prod/current/release.json \
  SNAPSHOT_ROOT=/var/lib/discord-bot-backup/snapshots \
  /home/ubuntu/discord-bot/scripts/snapshot_state.sh
```

Apply the pinned supergateway patch during provisioning before starting
`mcp-oracle-shared`.

```bash
sudo env HOME=/home/ubuntu \
  /home/ubuntu/discord-bot/scripts/supergateway_apply_patches.sh
```

The Oracle runtime UID cannot repair installed Node packages; startup
intentionally fails if the loopback patch is absent.

Each `/etc/discord-bot/credentials/*.env` file is `root:root 0600` and contains
only the variables required by its matching unit.  `deploy.env` contains the
read-only GitHub credential used by `gh` and the deploy-only
`DEPLOY_NOTIFY_WEBHOOK_URL`; it does not contain bot or MCP credentials.  PID 1
can read these files, while the service UIDs cannot open them.

The protected manifest is updated only through a separate operator-reviewed
installation step.  Changing the repository copy in the release commit cannot
approve protected-path changes.  A legitimate protected-path change requires
installing the reviewed external manifest before the release gate runs.

## Deploy and rollback state

`StateDirectory=discord-bot-deploy` creates `/var/lib/discord-bot-deploy` for
`kanae-deploy`.  Production and staging use separate subdirectories.  The bot
UIDs cannot write receipts, locks, or pins.  The rollback runner validates all
three identities before switching the checkout:

- the installed tag resolves to the supplied expected old SHA;
- current HEAD equals the supplied blocked release SHA;
- an existing pin, when present, contains the same tag and SHAs.

Auto and manual deploy create the pin before releasing their flock, after which
the external runner acquires the same lock.  No environment variable bypasses
the rollback lock.  If smoke or a build hook modifies tracked or
non-ignored files after merge, deploy saves patches and untracked files under
`/var/lib/discord-bot-deploy/<env>/quarantine/`, restores the blocked checkout
to a clean target, and switches to the verified old SHA.  The rollback pin
prevents a timer or `Restart=on-failure` from re-entering the blocked release.
Both deploy paths merge the exact commit object read from `FETCH_HEAD`, never a
mutable remote-tracking ref. A post-merge mismatch is quarantined, reset to the
old SHA, cleaned, release-pointer-restored, and pinned before any service start.

Grant `kanae-deploy` passwordless sudo only for the exact release-consumer
restart commands listed above. Do not grant a wildcard `systemctl` rule.
