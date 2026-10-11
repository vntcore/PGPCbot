# OpenPGP step by step

This guide turns a normal bot into an end-to-end encrypted one. The [Cryptography](../CRYPTOGRAPHY.md)
page explains what happens underneath and which risks remain; read it before production.

## Before you start: one-way step

When a bot publishes a key, its private chats become end-to-end encrypted **for good**, as far
as the API goes. Only the bot's owner can turn it back, from the console in the app. Try
everything on a test bot first.

## 1. Make the key

```bash
export BOT_KEY_PASSPHRASE="a long passphrase only you know"
pgpcbot keygen --name Weather --username weatherbot --out bot-key.asc --public bot-key.pub.asc \
    --passphrase-env BOT_KEY_PASSPHRASE
```

```text
private key: bot-key.asc
public key:  bot-key.pub.asc
fingerprint: 3F1A 9C0D 52E7 B8A4 6C11 0D93 E2F4 7A55 9B10 C6D2
```

Or in Python:

```python
from pgpcbot import BotKey

key = BotKey.generate("Weather", username="weatherbot", passphrase=os.environ["BOT_KEY_PASSPHRASE"])
key.save("bot-key.asc", passphrase=None)       # already protected by the passphrase above
print(key.fingerprint)
```

The key is the kind the phones use (v4, Ed25519 and Curve25519), set up so that every message
the bot sends can be opened by every phone. It does not expire.

:::{admonition} Back up the private key
:class: warning

If you lose `bot-key.asc` or its passphrase, the bot can no longer read anything sealed to
it. Keep a copy somewhere safe and offline. Never commit it, and never send it to anyone.
:::

## 2. Load it in the bot

```python
import os
from pgpcbot import Bot

bot = Bot(
    pgp_private_key="bot-key.asc",
    pgp_passphrase=os.environ["BOT_KEY_PASSPHRASE"],
)
```

Or with no code at all, from the environment:

```bash
export PGPCHAT_BOT_KEY_FILE=/etc/weatherbot/bot-key.asc
export PGPCHAT_BOT_KEY_PASSPHRASE_FILE=/run/secrets/bot_key_passphrase
```

A wrong passphrase stops the bot at start-up with `PassphraseError`.

## 3. Publish it

Once, from the command line (it asks you to confirm):

```bash
pgpcbot publish-key bot-key.asc --passphrase-env BOT_KEY_PASSPHRASE
```

Or let the bot do it at start-up, only when the server does not have this key yet:

```python
bot = Bot(pgp_private_key="bot-key.asc", pgp_passphrase=..., publish_key=True)
```

Or from code: `await bot.publish_key()`.

After this, `pgpcbot whoami` shows `mode: protected` and the key's fingerprint.

:::{admonition} If the bot has a key but did not publish it
:class: note

The bot logs a warning at start-up: its chats are still readable by the server, so the key
is not used yet.
:::

## 4. Nothing else changes

Your handlers stay the same:

```python
@bot.on_message()
async def echo(message):
    await message.reply(f"Got it: {message.text}")
```

- `message.text` is already decrypted;
- `message.reply()` encrypts the answer for the person and for the bot, and signs it;
- in an encrypted group, the answer is encrypted for every member who has a key.

## 5. Check who signed

Every encrypted message has a `verification`:

```python
from pgpcbot import SignatureStatus, filters as f


@bot.on_message(f.encrypted)
async def check(message):
    v = message.verification
    if v.status is SignatureStatus.VALID:
        await message.reply("Signed by the sender's key.")
    else:
        await message.reply(f"Signature: {v.status} ({v.detail})")
```

| Status | Meaning |
|---|---|
| `valid` | signed with the key the server lists for the sender |
| `unsigned` | no signature (channel posts in the channel's name are like this) |
| `unknown_key` | signed with a key that is not the one the server lists for the sender |
| `invalid` | the signature is wrong |
| `not_checked` | checks are off, or the sender's key could not be fetched; `detail` says why |

To drop everything that is not `valid` before it reaches a handler:

```python
bot = Bot(..., verify_signatures="require")
```

## 6. Decide how strict to be

| Option | Effect |
|---|---|
| `require_encryption=True` | the bot never sends anything in clear, anywhere. Sends into readable chats raise `EncryptionRequiredError` |
| `verify_signatures="require"` | only messages with a valid signature reach the handlers |
| `pin_keys=True` | remember each person's key fingerprint; if the server later gives a different key, it is refused both ways (`KeyChangedError` on sends, and on their incoming messages, which go to the error handlers) until you accept it with `bot.trust_key(user_id, fingerprint)` |

`pin_keys` needs `SQLiteStorage`, or the pins are forgotten at every restart.

## 7. Attachments

```python
@bot.on_message(f.has_media)          # from pgpcbot import filters as f
async def photo(message):
    data = await bot.download_media(message.media[0])     # downloaded and decrypted
```

In a private chat with the bot this always works. In an encrypted group the phones seal
attachments only to the members, so it works only if the bot's privacy mode is off. Otherwise
it fails with `NotARecipientError`.

## 8. Rotating the key

Change the key from time to time, and at once if you think it leaked:

```python
new_key = BotKey.generate("Weather", username="weatherbot", passphrase=...)
new_key.save("bot-key-2026-11.asc", passphrase=None)
await bot.rotate_key(new_key)
```

Messages sealed to the old key may still be on their way for up to 72 hours. Keep loading the
old key for that long:

```python
bot = Bot(
    pgp_private_key="bot-key-2026-11.asc",
    pgp_passphrase=...,
    pgp_previous_keys=[BotKey.from_file("bot-key.asc", os.environ["OLD_KEY_PASSPHRASE"])],
)
```

## 9. Tell people the fingerprint

Phones get the bot's key from the server. To be sure nobody swapped it, people can compare the
fingerprint they see in the app with one you publish somewhere else: your website, the bot's
description, a printed card. `pgpcbot fingerprint bot-key.asc --spaced` prints it in groups of
four.

## Common problems

| Error | Cause | Fix |
|---|---|---|
| `EncryptionError: nobody in chat ... has a key` | nobody in the chat has published a key | nothing to do: the chat cannot receive encrypted messages |
| `EncryptionError` in a protected chat, at the first send | the bot has no private key loaded | pass `pgp_private_key` |
| `NotARecipientError` | the message was not sealed to this key: an old key after a rotation, or a group message the phone did not address to the bot | add the old key to `pgp_previous_keys`; in groups, see privacy mode |
| `PassphraseError` | wrong or missing passphrase | check `PGPCHAT_BOT_KEY_PASSPHRASE` |
| `WrongBodyKindError` | the chat became readable while the bot was sending an encrypted message | nothing: the next message follows the new mode. The library never resends in clear |
