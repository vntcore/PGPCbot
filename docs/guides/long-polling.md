# Long polling

With long polling, the bot asks the server "anything new?" and the server answers as soon as
something arrives, or after up to 50 seconds with an empty list. It works from any computer,
even behind a home router: you don't need a public address. It is the default.

```python
bot.run()                                  # blocks until Ctrl+C or SIGTERM
```

Inside your own asyncio program:

```python
async def main():
    task = asyncio.create_task(bot.start_polling())
    ...
    await bot.stop()                       # finishes running handlers, then returns
    await task
```

## Options

| Option | Default | Meaning |
|---|---|---|
| `poll_timeout` | `50` | how long each request may wait on the server, 0 to 50 seconds |
| `limit` | `100` | how many updates one request may bring, 1 to 100 |
| `drop_webhook` | `False` | if a webhook is set, remove it and poll. Without this, the bot refuses to start (`PollingError`) |
| `skip_pending` | `False` | throw away the updates that queued while the bot was off, without handling them |
| `configure_logging` | `True` | `run()` only: set up basic logging if your program has not |

## How updates are confirmed

The server keeps every update until the bot **confirms** it. A bot confirms by sending, in its
next request, the number of the last update it is done with, plus one (the `offset`). The
server then deletes everything below that number, for good.

The library never confirms an update before it is safe:

- with **`SQLiteStorage`**, an update is safe once it is written to the database. The bot
  confirms it at the next request, even if its handler has not run yet. If the process
  crashes, the update is still in the database and runs at the next start;
- with **`MemoryStorage`** (the default), an update is safe only once its handler has
  finished. The bot waits for the whole batch to be handled before it asks for more. If the
  process crashes, the server still has the updates and sends them again.

Either way, an update can be handled **twice** if the process dies at the wrong moment (after
the handler ran, before the confirmation reached the server). The library removes duplicates
it can see, by `updateId`. For actions that must never happen twice (a payment, an order),
make them idempotent in your own code too: check whether you already did it.

An update that fails (its handler raised and no error handler took the error) is confirmed
too, so it does not block the others, and it is kept in the failed list. See
[Storage](storage.md#failed-updates).

## Only one poller per bot

The server allows **one** long poll per bot at a time. A second request ends the first one at
once with an empty answer, so two copies of the same bot that poll will take turns and get
nothing done.

- With `SQLiteStorage`, the library takes a lock on the database (an operating system lock,
  freed even if the process dies). A second process with the same file fails at start with
  `PollingConflictError`, which tells you the process ID of the first one.
- With `MemoryStorage` the library cannot see the other process. If several polls in a row come
  back empty right away, it logs a warning: "another process is probably polling this bot".

To handle more work, raise `concurrency`, or switch to a [webhook](webhook.md).

## When things go wrong

| What happens | What the bot does |
|---|---|
| the network fails, the server answers `5xx` | waits a little (longer each time) and tries again, without end |
| `429` too many requests | waits as long as the server says |
| the owner paused the bot (`403 bot_paused`) | logs it once, checks again every 30 seconds, and goes on when the bot is resumed |
| the token is wrong or revoked (`401`) | stops with `AuthenticationError` |
| a webhook was set meanwhile (`409`) | stops with `PollingError` |

At start-up the bot also waits for the server if it is down, instead of failing.

## Stopping

`Ctrl+C`, `SIGTERM` (what `systemd`, Docker and Kubernetes send) and, on Windows,
`Ctrl+Break` all stop the bot cleanly:

1. it stops asking for updates, at once: a long poll in progress is dropped, and what it
   brought was not confirmed, so the server sends it again next time;
2. it waits for the handlers that are running, up to `shutdown_timeout` (30 seconds);
3. handlers still running after that are cancelled. Their updates stay in the queue (with
   `SQLiteStorage`) or on the server (with `MemoryStorage`), and run again at the next start.

To stop from your own code, call `bot.request_stop()`: it returns at once, from a handler or
from another thread, and `run()` then stops as above. `await bot.stop()` does the same and
waits until the bot has stopped (called from inside a handler, it only asks, so that it does
not wait for itself).

## Updates that wait for too long

The server keeps updates for 72 hours. A bot that is off for longer loses the oldest ones. When
the bot comes back after a long pause and the old messages no longer matter, start it with
`skip_pending=True`.
