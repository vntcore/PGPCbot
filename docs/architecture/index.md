# Architecture

This page is for people who want to know how the library is built, or who want to change it.

## Layers

```text
 your code
    │
    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Bot / SyncBot            bot.py, router.py, filters.py                 │
│   handlers, filters, middleware, error handlers; opens what comes in   │
│   and seals what goes out; caches, key pinning                         │
├────────────────────────────────────────────────────────────────────────┤
│ Engine + delivery        engine.py, storage.py, webhook.py             │
│   long polling loop, webhook ASGI app, durable inbox; one chat at a    │
│   time, in order; an offset is confirmed only once the update is safe  │
├────────────────────────────────────────────────────────────────────────┤
│ AsyncClient / Client     client.py                                     │
│   one method per API operation:                                        │
│   validation → rate limiter → HTTP → envelope → retries                │
├────────────────────────────────────────────────────────────────────────┤
│ Sans-I/O core            _spec.py, _http.py, _validate.py, errors.py,  │
│                          ratelimit.py, models.py                       │
│   no network here: pure functions, so the tests need none              │
├────────────────────────────────────────────────────────────────────────┤
│ OpenPGP                  crypto/keys.py, crypto/_lib.py                │
│   pysequoia seals and opens, rpgp-py makes the keys and checks the     │
│   signatures inside compressed messages                                │
└────────────────────────────────────────────────────────────────────────┘
```

Each layer only uses the ones below it. The low-level clients work without the bot; the
OpenPGP module works without either.

## Modules

| Module | What it holds |
|---|---|
| `_spec.py` | one `Call` per API operation: method, path, query, body, how to read the answer, whether it is safe to repeat, which error codes mean "already done" |
| `_http.py` | the token format, the API address, the `{ok, result}` envelope, `Retry-After`, `RetryPolicy` |
| `_validate.py` | the server's input rules, copied: UTF-16 lengths, JavaScript `trim()`, control characters, IDs, URLs, scopes |
| `errors.py` | the exception classes and the map from the server's error codes to them |
| `ratelimit.py` | the sliding-window limiter, with the same windows as the server |
| `client.py` | `AsyncClient` and `Client`: two thin shells that run a `Call` over httpx2 |
| `models.py` | the typed results: frozen dataclasses that keep the original JSON in `raw` |
| `keyboard.py` | buttons and keyboards, and the server's markup rules |
| `wire.py` | the app's internal message format (text, media, albums, forwards...) inside encrypted messages |
| `crypto/_lib.py` | the only module that talks to pysequoia and rpgp-py |
| `crypto/keys.py` | `BotKey` and `PublicKey` |
| `router.py`, `filters.py` | handlers, routers, middleware, filters |
| `engine.py` | the update engine and the polling loop |
| `storage.py` | `Storage`, `MemoryStorage`, `SQLiteStorage` |
| `webhook.py` | the ASGI webhook app |
| `bot.py` | `Bot` and `SyncBot` |
| `_secrets.py` | `Secret`, environment variables and `_FILE` variants, log redaction |
| `__main__.py` | the `pgpcbot` command |

## One description, two clients

Every operation is described once, in `_spec.py`, as data:

```python
def delete_message(chat_id: int, message_id: int) -> Call[bool]:
    message_id = v.check_id(message_id, "message_id")
    return Call(
        "DELETE",
        f"/messages/{message_id}",
        lambda r: _flag(r, "deleted"),
        params={"chatId": v.check_id(chat_id, "chat_id")},
        # a repeat whose first try got lost: "not found" means the first one worked
        done_codes=frozenset({"message_not_found"}),
        done_value=True,
    )
```

A `Call` also says whether it is safe to send again (`repeatable`), how long a long poll may
take (`read_timeout`), and what the rate limiter needs to know (`send`, `edit`, `chat_id`,
`chat_kind`).

`AsyncClient.execute()` and `Client.execute()` run a `Call` the same way: wait for the rate
limiter, send, read the envelope, map errors, decide whether to retry. Only the waiting and the
I/O differ (`await asyncio.sleep` against `time.sleep`). This is how the two clients stay the
same without one wrapping the other. Wrapping a sync client around `asyncio.run()` breaks inside
running loops, and a hidden event loop thread brings its own problems: both were rejected
([research](../RESEARCH.md#32-sync-and-async-without-writing-the-logic-twice)).

## The life of an update

### Long polling

```text
server ──GET /updates?offset=N──► poll loop ──accept()──► storage ──► engine
                                     ▲                                  │
                                     └── next offset = highest safe + 1 │
                                                                        ▼
                               one queue per chat, up to `concurrency` chats at once
                                                                        │
                         decrypt → check signature → read wire format → filters → handler
                                                                        │
                                                    finish() or fail(error) in the storage
```

- With durable storage, "safe" means "written": the offset moves on at the next poll.
- With memory storage, "safe" means "handled": the loop waits for the batch to drain first.
- The engine runs the updates of one chat in order, one at a time, and different chats in
  parallel.

### Webhook

```text
server ──POST──► WebhookApp: secret? size? JSON? ──► bot._accept_delivery()
                                                          │
                       durable storage: accept() and answer 200 at once
                       memory storage:  wait for the handlers (ack_timeout), 200 or 503
```

The server sends the next update only after a `2xx`, so the answer must come quickly, and must
be a `2xx` for anything authenticated, even a broken body: anything else blocks the bot's queue
on the server.

### Sending into an encrypted chat

```text
send_message(chat, text)
   │
   ├─ mode of the chat: cache → updates seen → GET /chats/{id}
   ├─ open:      POST /messages {text}
   └─ protected: GET /chats/{id}/keys → check each key (fingerprint, subkey, v4)
                 → seal to members + bot, sign (pysequoia) → POST /messages {armored}
                    │
                    └─ 400 wrong_body_kind, expected=armored: seal and resend, same sendId
                       400 wrong_body_kind, expected=text: raise, never resend in clear
```

## Two OpenPGP engines

`crypto/_lib.py` uses two libraries, each for what it does best:

- **pysequoia** (Sequoia PGP) encrypts and decrypts. It knows which subkeys may be used, and
  refuses weak algorithms.
- **rpgp-py** (rPGP) makes the bot's key, so it can say "SEIPDv1 only" and use a passphrase,
  and checks signatures, including inside compressed messages, which pysequoia 0.1.35 cannot
  do.

When a message is opened, rPGP decrypts it a second time and the two texts must match, or the
signature is reported as `invalid`. The [research page](../RESEARCH.md#2-openpgp) has the tests
behind these choices.

## Design rules

- **Nothing is sent in clear by accident.** Every path that could fall back to plain text
  raises instead.
- **Nothing is confirmed before it is safe.** Offsets and webhook answers follow the storage.
- **The server is the authority.** Client-side checks copy the server's rules and limits
  (read from `GET /me`); they never invent stricter ones, and a request the server would accept
  must not be refused.
- **Secrets don't leak.** Tokens and keys never appear in logs, exceptions or `repr`, and the
  objects that hold them refuse to be pickled.
- **Unknown is not an error.** New fields from a newer server stay in `raw`; new enum values
  become readable pseudo-members instead of crashing.
