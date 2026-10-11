# Errors

Every error the library raises is a `PGPCbotError`. Below it, errors are grouped by **where**
the problem is, so you can catch as much or as little as you need.

```text
PGPCbotError
├── ConfigurationError        the bot or client is set up wrong (no token, bad URL...)
├── ValidationError           a request that the server would refuse; it was not sent
├── PayloadError              the server sent JSON this version cannot read
├── APIError                  the server answered with an error
│   ├── BadRequestError           400 (WrongBodyKindError, BadMarkupError, BadWebhookError, ChatIsOpenError)
│   ├── AuthenticationError       401
│   ├── ForbiddenError            403 (BotPausedError, BlockedByUserError, UserGoneError)
│   ├── NotFoundError             404 (ChatNotFoundError, MessageNotFoundError, MemberNotFoundError,
│   │                                  UserNotFoundError, NoUserKeyError, CallbackNotFoundError,
│   │                                  EndpointNotFoundError)
│   ├── ConflictError             409 (SendIdConflictError, WebhookActiveError, CallbackAnsweredError)
│   ├── CallbackExpiredError      410
│   ├── PayloadTooLargeError      413
│   ├── RateLimitError            429 (FloodWaitError, SlowModeError)
│   ├── ServerError               5xx
│   └── UnexpectedResponseError   not the API's JSON at all (a proxy page, a cut answer)
├── NetworkError              no answer: DNS, refused connection, TLS...
│   └── RequestTimeoutError
├── CryptoError               OpenPGP
│   ├── InvalidKeyError
│   ├── PassphraseError
│   ├── EncryptionError           the message was NOT sent
│   │   └── EncryptionRequiredError
│   ├── DecryptionError
│   │   └── NotARecipientError
│   ├── SignatureError
│   └── KeyChangedError
├── WebhookError
├── PollingError
└── StorageError
    └── PollingConflictError
```

The full list, with the server's error codes, is in the [API reference](../api/errors.rst).

## What an `APIError` tells you

```python
from pgpcbot import APIError

try:
    await bot.send_message(chat_id, "hi")
except APIError as exc:
    print(exc.status)        # 403
    print(exc.code)          # "blocked_by_user"
    print(exc.message)       # the server's explanation
    print(exc.retry_after)   # seconds to wait, for 429 (else None)
    print(exc.expected)      # "text" or "armored", for wrong_body_kind
    print(exc.method, exc.path)   # "POST", "/messages"
```

`str(exc)` is readable and never contains the token.

## What the library retries for you

You don't need to wrap every call in a retry loop. The client already:

- retries on network errors and `5xx` answers, with a growing random wait, **when the call is
  safe to repeat**: reads, idempotent calls, and sends (which always carry a `sendId`). By
  default it tries 4 times in all;
- waits out `429` answers for as long as the server asks (up to 90 seconds, 5 times per call);
- treats some errors after an uncertain attempt as success. For example, if a delete timed out
  and the retry says `message_not_found`, the first attempt did delete it.

It never retries other `4xx` errors: they would fail the same way again.

`send_typing` and `get_updates` are not retried after a network error: a late "typing" is
useless, and polling has its own loop.

When the retries run out, you get the last error. See [Configuration](../getting-started/configuration.md#retries)
to change the numbers.

## Handling errors in a bot

Inside handlers, let errors go up to an error handler, instead of catching them everywhere:

```python
from pgpcbot import BlockedByUserError, ForbiddenError


@bot.on_error(BlockedByUserError)
async def blocked(error, update):
    await db.mark_inactive(update.chat.id)          # stop writing to them


@bot.on_error(ForbiddenError)
async def no_rights(error, update):
    log.info("no rights in chat %s: %s", update.chat.id, error.code)


@bot.on_error()
async def anything_else(error, update):
    log.exception("update %s failed", update.update_id, exc_info=error)
    raise error                                     # keep it in the failed list
```

An error handler that returns normally marks the update as done. An error that nobody handles
marks the update as failed and keeps it for later ([Storage](storage.md#failed-updates)). Either
way the bot goes on with the next updates.

Catch an error inside the handler only when you have something useful to do there:

```python
@bot.command("notify")
async def notify(message, command):
    for user_id in subscribers:
        try:
            await bot.send_message(user_id, command.args)
        except (BlockedByUserError, UserGoneError):
            subscribers.discard(user_id)
```

## Errors that need your attention

| Error | What it means | What to do |
|---|---|---|
| `AuthenticationError` | the token is wrong, revoked or replaced | get a new token from the console |
| `BotPausedError` | the owner paused the bot | nothing until it is resumed; polling waits for it |
| `WebhookActiveError` | you poll while a webhook is set | `run(drop_webhook=True)` or delete the webhook |
| `EndpointNotFoundError` | the server has no such path | `api_url` points to the wrong server: use the API address the console shows |
| `EncryptionError` | the message could not be encrypted, and was not sent | see [OpenPGP](openpgp.md#common-problems) |
| `PollingConflictError` | another process runs this bot with the same database | run one process |
| `PayloadError` | the server sent something this version cannot read | update PGPCbot |

## Errors you can usually ignore

| Error | Why |
|---|---|
| `MessageNotFoundError` on delete | the message is already gone |
| `CallbackAnsweredError`, `CallbackExpiredError` | the press was already answered, or it is too late |
| `BlockedByUserError`, `UserGoneError` | the person does not want messages, or left PGP Chat |

## `ValidationError`: refused before sending

The library checks your request against the same rules as the server (text length, button
rules, IDs, URLs...). A `ValidationError` means nothing was sent; `exc.field` names the bad
field.

```python
try:
    await message.reply("x" * 5000)
except ValidationError as exc:
    print(exc.field, exc)        # text  text is 5000 characters long (UTF-16), the limit is 4096
```
