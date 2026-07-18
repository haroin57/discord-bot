# Deploy assets

## Deployment checkouts

CD uses dedicated, secret-free checkouts owned by `kanae-deploy`:

- production: `/var/lib/discord-bot-deploy/checkouts/prod`
- staging: `/var/lib/discord-bot-deploy/checkouts/dev`

Developer worktrees under `/home/ubuntu` are not deployment inputs. This keeps
uncommitted development work, local virtual environments, and dotenv files out
of the pull-based deployment transaction.

Production supports two fail-closed consumer profiles. The installed unit uses
`AUTO_DEPLOY_PROFILE=core` while only `discord-bot.service` consumes the atomic
release tree. After every production consumer has been migrated to
`/var/lib/discord-bot-releases/prod/current`, switch the unit to `full`; the
full profile requires the complete consumer set, ports 3004 and 3002, and the
Chroma liveness endpoint. Arbitrary consumer subsets are rejected.

## Initial install
1. Provision the service identities and filesystem boundaries in
   [OPERATIONS_SECURITY.md](OPERATIONS_SECURITY.md).
2. Copy the units to `/etc/systemd/system/`.
3. Install the root-owned rollback runner and protected manifest trust root.
4. Create the service-specific credential files under
   `/etc/discord-bot/credentials` with owner `root:root` and mode `0600`.
5. Run `systemctl daemon-reload && systemctl enable --now discord-bot`.

## Verification
- `systemd-analyze security discord-bot.service` should report Exposure level: MEDIUM or better
- `stat -c '%U %G %a' /etc/discord-bot/credentials/discord-bot.env` must
  output `root root 600`.
- `stat -c '%U %a' /usr/local/libexec/discord-bot-rollback` must output
  `root 755` or stricter.
- `systemctl show discord-bot -p User` must output `User=kanae-bot`.
- `journalctl -fu discord-bot` shows no startup errors

## C1 — token rotation reminder
If you found a leaked token in `scripts/*.bak.*`:
1. Discord Developer Portal → Reset Bot Token
2. Update `DISCORD_TOKEN=` in
   `/etc/discord-bot/credentials/discord-bot.env`.
3. `systemctl restart discord-bot`

## Service-specific secrets

The units set `PYTHON_DOTENV_DISABLED=1`; they do not fall back to repository
dotenv files. PID 1 reads the root-owned `EnvironmentFile` before switching to
the service UID. Each service receives only its own variables and cannot open
another service's credential file.

| Unit | EnvironmentFile |
|---|---|
| `discord-bot` | `discord-bot.env` |
| `discord-bot-dev` | `discord-bot-dev.env` |
| `hermes-discord-codex` | `hermes-discord.env` |
| `vi-signal-scheduler` | `vi-signal.env` |
| `intervention-scheduler` | `intervention.env` |
| `remote-opencode` | `remote-opencode.env` |
| `mcp-mem0-shared` | `mcp-mem0.env` |
| `mcp-oracle-shared` | `mcp-oracle.env` |
| `mcp-second-opinion-shared` | `mcp-second-opinion.env` |
| `mcp-x-scraper-shared` | `mcp-x-scraper.env` |

The Oracle file requires `OPENAI_API_KEY`; `GOOGLE_AI_API_KEY` is optional. The
Oracle launcher no longer reads the shared `.env`. The patched supergateway
binds `127.0.0.1:8015` and refuses startup when the loopback patch is absent.
The production, staging, and Hermes bot identities also require distinct
primary and failover OAuth files in their private HOME directories as specified
in `OPERATIONS_SECURITY.md`; these tokens are not stored in EnvironmentFiles.
