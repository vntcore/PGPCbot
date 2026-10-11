# Troubleshooting

Start with `pgpcbot whoami`: it shows whether the token works, the bot's mode, whether it is
paused, the webhook and the queue. Then look for your symptom below.

## The bot does not start

| Message | Cause | Fix |
|---|---|---|
| `ConfigurationError: no bot token` | the token is not set | set `PGPCHAT_BOT_TOKEN` (or `_FILE`), or pass `token=` |
| `ConfigurationError: ... it should look like pgpbot_<id>.<43 characters>` | a pasted token with a missing or extra character | copy it again; it is shown once in the console, so you may need a new one |
| `AuthenticationError: unauthorized (401)` | the token was revoked or replaced | make a new token in the console |
| `PollingError: a webhook is set` | polling and a webhook can't run together | `bot.run(drop_webhook=True)` or `pgpcbot webhook delete` |
| `PollingConflictError: another process (pid ...) is already running this bot` | a second copy is running with the same database | stop the other one |
| `ConfigurationError: SyncBot runs plain functions...` or `Bot runs async def handlers...` | a handler, error handler or middleware of the wrong kind; the message names it | `async def` with `Bot`, `def` with `SyncBot` |
| `PassphraseError` | wrong or missing key passphrase | check `PGPCHAT_BOT_KEY_PASSPHRASE` |
| `SSL: CERTIFICATE_VERIFY_FAILED` | no CA certificates in the system or container | install `ca-certificates`, or set `SSL_CERT_FILE` |
| `ConfigurationError: api_url uses plain http` | `PGPCHAT_API` starts with `http://` | use `https://`; for a local test server use `localhost` |

## The bot runs but gets nothing

- **In a group:** with privacy mode on (the default) the bot only gets commands, replies to its
  messages and mentions. `pgpcbot whoami` shows `privacy mode on`. The owner can turn it off.
- **In an encrypted group:** the bot needs a published key, or the phones can't seal messages
  to it.
- **Two copies are polling:** the log says "another process is probably polling this bot".
  Stop the other copy (check other machines and containers too).
- **The bot is paused:** `whoami` says `status: paused`. The owner resumes it in the app.
- **A webhook is set** and failing: `pgpcbot webhook info` shows the last error.
- **A handler does not match:** an earlier handler took the update. The first match wins, so
  put specific handlers before general ones. Turn on `DEBUG` logs for `pgpcbot.bot`: it logs
  "no handler took it" for updates that matched nothing.

## Sending fails

| Error | Cause | Fix |
|---|---|---|
| `EncryptionError: ... the bot has no private key` | the chat is encrypted, the bot has no key loaded | pass `pgp_private_key` |
| `EncryptionError: nobody in chat ... has a key` | nobody in the chat published a key | the chat cannot get encrypted messages |
| `EncryptionRequiredError` | `require_encryption=True` and the chat is readable | intended: the bot refuses to talk in clear |
| `KeyChangedError` | `pin_keys=True` and someone's key changed | check the new key with the person, then `bot.trust_key()` |
| `WrongBodyKindError` (expected `text`) | the chat became readable while the bot sent an encrypted message | send again: the next message follows the new mode |
| `BlockedByUserError` | the person blocked the bot | stop writing to them |
| `ForbiddenError` | no right to write (a restricted bot, a channel where the bot is not an admin) | ask an admin |
| `ChatNotFoundError` | the bot is not in that chat, or the ID is wrong | check the ID; a person must start a private chat first |
| `FloodWaitError`, `SlowModeError` | too fast | see [Rate limits](../guides/rate-limiting.md) |
| `ValidationError` | the request breaks a server rule; nothing was sent | `exc.field` names the problem |

## Encrypted messages

| Symptom | Cause | Fix |
|---|---|---|
| `NotARecipientError` on new messages | the bot runs with a key that is not the published one | `whoami` shows the published fingerprint; load that key, or publish the one you have |
| `NotARecipientError` after a rotation | messages sealed to the old key | add the old key to `pgp_previous_keys` for 72 hours |
| `NotARecipientError` on attachments in a group | phones seal attachments only to the members' key list | turn the bot's privacy mode off, or skip attachments |
| `verification.status` is `not_checked` | checks are off, or the chat's keys could not be fetched | `verification.detail` says which |
| `verification.status` is `unknown_key` | signed by a key that is not the one the server lists for the sender | check with the person |
| `KeyChangedError` on incoming messages | `pin_keys=True` and the sender's key changed | check the new key with the person, `bot.trust_key()`, then `bot.retry_failed()` |
| `DecryptionError: ... compressed attachment` | someone sent a compressed file; phones never do | nothing to fix on the bot's side: the file is refused on purpose |
| a warning "key ... advertises SEIPDv2" | the bot key was not made by `BotKey.generate()` | make a new key with `pgpcbot keygen`, and rotate |

## Polling and webhooks

- **Updates arrive twice.** Delivery is "at least once". The library drops repeats it can see,
  but after a crash at the wrong moment a handler can run again. Make important actions
  idempotent.
- **The webhook gets `401`s in its log.** Something without the secret calls your URL, or the
  secret in your app differs from the one the server has. With `url=` the app registers its
  secret at every start; otherwise run `pgpcbot webhook set URL --secret-env VAR` again.
- **The webhook queue does not move.** `pgpcbot webhook info` shows the last error and the next
  try. The server sends one update at a time and waits for a `2xx`: fix the error and the queue
  moves on its own.
- **A handler is slow and everything waits.** Updates of one chat run in order; other chats are
  not blocked. With a webhook and `MemoryStorage`, the whole bot runs one update at a time: use
  `SQLiteStorage`.

## Windows

- Ctrl+C stops the bot cleanly. Ctrl+Break works too.
- If you run a bot inside Jupyter, `bot.run()` can't start its own loop: use
  `await bot.start_polling()` in a cell.

## Still stuck?

Turn on debug logs for the library only:

```python
import logging
logging.basicConfig(level=logging.INFO)
logging.getLogger("pgpcbot").setLevel(logging.DEBUG)
```

The logs never contain the token, the key or message text, so you can share them when you ask
for help. Check them anyway before posting them in public.
