# Storage and failed updates

The storage is where updates wait between "received from the server" and "handled". It also
keeps the failed updates and, with `pin_keys=True`, the pinned key fingerprints.

| | `MemoryStorage` | `SQLiteStorage` |
|---|---|---|
| default | yes | no |
| survives a restart | no | yes |
| when an update is confirmed to the server | after its handler finished | as soon as it is written to disk |
| a crash during a handler | the server sends the update again | the update runs again at the next start |
| failed updates | kept until the process ends | kept until you retry or discard them |
| pinned keys (`pin_keys=True`) | forgotten at every restart | kept |
| a second process with the same bot | not detected | refused with `PollingConflictError` |
| webhook | handles one update at a time | handles many chats at once |

Use `SQLiteStorage` for anything that runs for real:

```python
from pgpcbot import Bot, SQLiteStorage

bot = Bot(storage=SQLiteStorage("/var/lib/weatherbot/bot.db"))
bot = Bot(storage="bot.db")                    # a path is the same as SQLiteStorage(path)
```

## What `SQLiteStorage` does

- It writes with `synchronous=FULL` in WAL mode: when the library tells the server "got it",
  the update is on disk, even if the power goes off right after.
- It takes an operating system lock on a file next to the database (`bot.db.<bot id>.lock`), so
  only one process handles a bot's updates. The system releases the lock when the process dies,
  even in a crash.
- One file can hold several bots: each bot's rows are kept apart by its ID, and each bot has its
  own lock.
- On Linux and macOS the database and the lock file are created readable by their owner only
  (`0600`), and opening an existing database that others can read logs a warning.

Keep the file on a local disk. SQLite locks don't work reliably on network shares (NFS, SMB).

:::{admonition} What is in the file
:class: warning

Updates are stored as the server sent them. From encrypted chats they are encrypted
OpenPGP messages, and nothing decrypted is ever written. **From readable chats they are
plain text.** Protect the file and its backups like the messages it holds.
:::

### Backups

The safest way to copy the database while the bot runs is SQLite's own backup:

```bash
sqlite3 /var/lib/weatherbot/bot.db ".backup /backups/bot-$(date +%F).db"
```

Copying the file with `cp` while the bot writes can give you a broken copy. If you copy it that
way, stop the bot first, and copy `bot.db-wal` too if it exists.

## Failed updates

An update **fails** when a handler raises an error and no error handler takes it. A failed
update:

- is confirmed to the server like any other, so it does not block the queue;
- is kept, with the error message, in the failed list;
- does not stop the bot: the next updates are handled normally.

Anyone who can write to the bot can make updates fail, so the list keeps the newest ones only:
1000 with `MemoryStorage`, 10 000 per bot with `SQLiteStorage` (the `max_failed` option of both).
For a message from an encrypted chat, the error kept is the exception's type and where it was
raised, without its message: a handler's own exception could quote the decrypted text.

You decide what to do with it:

```python
for item in await bot.failed_updates():
    print(item.update_id, item.failed_at, item.error)
    # item.payload is the update, as the server sent it

await bot.retry_failed()                 # all of them back in the queue
await bot.retry_failed([1201, 1202])     # only these
await bot.discard_failed()               # forget them
```

Retried updates run through the handlers again, as if they had just arrived. A common pattern
is to fix the bug, restart the bot, and call `retry_failed()`.

### Updates that crash the process

If the process dies while a handler is running (out of memory, a crash in a library, a power
cut), the update is still pending at the next start, and runs again. To stop an update that
crashes the process every time from blocking the bot forever, the library counts the runs: after
`max_handler_runs` (5) runs that never finished, the update goes to the failed list without
running.

## Pinned keys

With `pin_keys=True`, the first key fingerprint seen for each person is saved. See
[Trusting keys](../CRYPTOGRAPHY.md#7-trusting-keys). To accept a new key after checking it:

```python
await bot.trust_key(user_id, "3F1A9C0D52E7B8A46C110D93E2F47A559B10C6D2")
```

## Your own storage

To keep updates somewhere else (PostgreSQL, Redis), subclass `Storage` and implement its
methods. The [API reference](../api/storage.rst) lists them and what each one must
promise. The two that matter most:

- `durable`: set it to `True` on your class only if `accept(updates)` has saved the updates
  for good when it returns, because the bot then confirms them to the server right away. With
  the default, `False`, the bot waits for the handlers before confirming, as with
  `MemoryStorage`.
- `try_lock()` must make sure only one process handles a bot's updates.
