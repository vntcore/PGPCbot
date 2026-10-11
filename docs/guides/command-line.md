# Command line

The `pgpcbot` command does the chores that don't need a running bot. `python -m pgpcbot` works
the same.

```text
pgpcbot keygen --name NAME --out FILE [--username HANDLE] [--public FILE]
               [--passphrase-env VAR | --no-passphrase] [--force]
pgpcbot fingerprint FILE [--spaced] [--passphrase-env VAR]
pgpcbot whoami
pgpcbot publish-key FILE [--passphrase-env VAR] [--yes]
pgpcbot webhook info | set URL [--secret-env VAR] | delete
pgpcbot --version
```

The token comes from `PGPCHAT_BOT_TOKEN` (or `PGPCHAT_BOT_TOKEN_FILE`) and the server from
`PGPCHAT_API`, as for the library.

**Passphrases and secrets are never command-line arguments**, where they would end up in your
shell history and in the process list. They are read from an environment variable that you
name (`--passphrase-env VAR`, `--secret-env VAR`), or asked on the terminal.

Exit codes: `0` success, `1` cancelled, `2` an error (printed on standard error, without the
token), `130` interrupted with Ctrl+C.

## keygen

Makes a new key for a bot and saves the private key.

| Option | Meaning |
|---|---|
| `--name` | the bot's name, for the key's user ID (required) |
| `--username` | the bot's handle, without `@`: the user ID becomes `Name (@handle)` |
| `--out` | where to write the private key (required) |
| `--public` | also write the public key here |
| `--passphrase-env VAR` | protect the key with the passphrase in `$VAR` |
| `--no-passphrase` | leave the key unprotected (not recommended) |
| `--force` | replace existing files |

Without `--passphrase-env` or `--no-passphrase`, it asks for the passphrase twice on the
terminal (an empty answer means none).

```bash
pgpcbot keygen --name Weather --username weatherbot --out bot-key.asc --public bot-key.pub.asc \
    --passphrase-env BOT_KEY_PASSPHRASE
```

```text
private key: bot-key.asc
public key:  bot-key.pub.asc
fingerprint: 3F1A 9C0D 52E7 B8A4 6C11 0D93 E2F4 7A55 9B10 C6D2
```

The private key file is created with mode `0600` on Linux and macOS.

## fingerprint

Prints the fingerprint of a key file: a public key, a private key, or a private key sealed with
a passphrase (which needs `--passphrase-env`, or is asked on the terminal).

```bash
pgpcbot fingerprint bot-key.asc --spaced
```

## whoami

Asks the server who the token belongs to.

```text
@weatherbot (id 412) - Weather
mode:     protected, key 3F1A 9C0D 52E7 B8A4 6C11 0D93 E2F4 7A55 9B10 C6D2
status:   active, privacy mode on
webhook:  none (polling)
queued:   0 update(s)
```

## publish-key

Publishes the public part of a private key file: the bot becomes protected and its private
chats end-to-end encrypted. It loads the private key first (asking for the passphrase if
needed), so you can't publish a key you can't use.

It asks you to type `publish` to confirm, because it is one-way: only the owner can make the
bot readable again, from the console. `--yes` skips the question (for scripts).

```bash
pgpcbot publish-key bot-key.asc --passphrase-env BOT_KEY_PASSPHRASE
```

## webhook

```bash
pgpcbot webhook info
pgpcbot webhook set https://bot.example.org/pgpchat --secret-env WEBHOOK_SECRET
pgpcbot webhook delete
```

`set` without `--secret-env` makes a new secret and prints it once: give it to your webhook
app. `info` shows the address, whether a secret is set (never the secret), the updates waiting
and the last error.
