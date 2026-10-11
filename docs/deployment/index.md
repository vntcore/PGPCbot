# Running in production

What to get right before it runs for real, then setups for systemd, Docker and
Kubernetes.

## Before it runs for real

- The token and the passphrase come from files or a secrets manager, not from the code.
  Use `PGPCHAT_BOT_TOKEN_FILE` and `PGPCHAT_BOT_KEY_PASSPHRASE_FILE`.
- `SQLiteStorage` on a local disk, in a folder only the bot's user can read.
- One process per bot (or several webhook workers on the same database file).
- Logs at `INFO`. Your handlers don't log message text from encrypted chats.
- The bot's private key is backed up, offline, with its passphrase stored separately.
- The server's clock is synchronised (NTP): signatures carry times.
- For a webhook: HTTPS in front, a secret set, `SQLiteStorage`.
- You know how to {py:meth}`revoke the token <pgpcbot.AsyncClient.revoke_token>` and
  [rotate the key](../guides/openpgp.md#8-rotating-the-key) if they leak.
- You checked the [Security](../security/index.md) page.

## systemd

`/etc/systemd/system/weatherbot.service`:

```ini
[Unit]
Description=Weather bot for PGP Chat
After=network-online.target
Wants=network-online.target

[Service]
User=weatherbot
WorkingDirectory=/opt/weatherbot
ExecStart=/opt/weatherbot/.venv/bin/python bot.py
Environment=PGPCHAT_BOT_TOKEN_FILE=%d/token
Environment=PGPCHAT_BOT_KEY_FILE=/opt/weatherbot/bot-key.asc
Environment=PGPCHAT_BOT_KEY_PASSPHRASE_FILE=%d/passphrase
LoadCredential=token:/etc/weatherbot/token
LoadCredential=passphrase:/etc/weatherbot/passphrase
StateDirectory=weatherbot
Restart=on-failure
RestartSec=5
TimeoutStopSec=45
# hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/weatherbot
LimitCORE=0

[Install]
WantedBy=multi-user.target
```

In `bot.py`, use `SQLiteStorage("/var/lib/weatherbot/bot.db")`. `LoadCredential` gives the
secrets to the service only (`%d` is the folder systemd puts them in), and `LimitCORE=0`
keeps the unlocked key out of core dumps. `TimeoutStopSec` should be longer than the bot's
`shutdown_timeout` (30 seconds), so handlers can finish when systemd stops the bot with
`SIGTERM`.

```bash
sudo systemctl enable --now weatherbot
journalctl -u weatherbot -f
```

## Docker

```dockerfile
FROM python:3.14-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 bot
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
USER bot
VOLUME /data
CMD ["python", "bot.py"]
```

`requirements.txt` pins the minor version, `PGPCbot>=0.1,<0.2`. Install `ca-certificates`: httpx2 uses the system's
certificate store.

```yaml
# compose.yaml
services:
  weatherbot:
    build: .
    restart: unless-stopped
    stop_grace_period: 45s
    environment:
      PGPCHAT_BOT_TOKEN_FILE: /run/secrets/bot_token
      PGPCHAT_BOT_KEY_FILE: /run/secrets/bot_key
      PGPCHAT_BOT_KEY_PASSPHRASE_FILE: /run/secrets/bot_key_passphrase
    secrets: [bot_token, bot_key, bot_key_passphrase]
    volumes:
      - botdata:/data
secrets:
  bot_token:
    file: ./secrets/token
  bot_key:
    file: ./secrets/bot-key.asc
  bot_key_passphrase:
    file: ./secrets/passphrase
volumes:
  botdata:
```

`docker stop` sends `SIGTERM`, which the bot handles; `stop_grace_period` gives it time to
finish. Keep the database on a named volume (`/data/bot.db`).

## Kubernetes

- Run **one replica** for a polling bot (`replicas: 1`, `strategy: Recreate`): two pods polling
  the same bot take turns and get nothing done. With `SQLiteStorage` on a volume, the second pod
  would also be refused by the lock.
- Put the token, the key and the passphrase in a `Secret`, mounted as files, and point the
  `_FILE` variables at them.
- `terminationGracePeriodSeconds: 45`.
- The database needs a `ReadWriteOnce` volume.

A webhook bot should also run in one pod. Inside it, several web workers can share one
database file. Several pods with separate databases would each see only part of the updates:
the order inside a chat and the removal of repeated deliveries would no longer hold.

## Webhook behind a reverse proxy

See [Webhooks](../guides/webhook.md#behind-a-reverse-proxy). The short version: HTTPS at the
proxy, `proxy_read_timeout` above 10 seconds, no redirects, and `uvicorn` listening on
`127.0.0.1` only.

## Logs and monitoring

- The library logs under `pgpcbot.*`. Useful lines at `INFO`: start-up (`running as @...`),
  webhook registration, published keys, stops. At `WARNING`: retries, a paused bot, members
  left out of an encrypted message, failed signature checks with a reason.
- `pgpcbot whoami` and `pgpcbot webhook info` show the queue on the server.
- `await bot.failed_updates()` lists what failed; alert on it if your bot matters.
- A `/health` route in your web app can check that the bot started: `bot.me is not None`.

## Backups

- **The private key:** a copy offline, with the passphrase stored somewhere else.
- **The database:** `sqlite3 bot.db ".backup copy.db"` while running. It holds pending and
  failed updates and the pinned keys. Losing it costs the pins and the failed list; updates
  that were pending are lost only if they were already confirmed to the server.

## Upgrading

- Read the [changelog](../project/changelog.md) and the [versioning rules](../project/versioning.md).
- Stop the bot, upgrade (`pip install -U PGPCbot`), start it. Pending updates in `SQLiteStorage`
  run after the restart.
