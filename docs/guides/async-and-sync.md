# Async and sync

PGPCbot has the same features with and without `asyncio`:

| | async | sync (blocking) |
|---|---|---|
| bot with handlers | `Bot` | `SyncBot` |
| low-level API client | `AsyncClient` | `Client` |
| a received message | `Message` | `SyncMessage` |
| a sent message | `SentMessage` | `SyncSentMessage` |
| a button press | `CallbackQuery` | `SyncCallbackQuery` |

The sync classes have the same methods, with the same parameters, without `await`. A test
checks this for every method.

## Which one to use

- **`Bot`** if your code is already async, if you call other async libraries (databases, HTTP
  APIs), or if you serve a webhook. It handles many chats with little memory.
- **`SyncBot`** if you prefer plain functions, or if you call libraries that block (`requests`,
  a classic database driver). Each handler runs in a thread.
- **`Client` / `AsyncClient`** for scripts that only call the API, with no handlers: a cron job
  that posts a report, a migration script.

## `SyncBot`

```python
from pgpcbot import SyncBot

bot = SyncBot()


@bot.command("start")
def start(message):
    message.reply("Hello!")


@bot.on_callback_query()
def pressed(query):
    query.answer("OK")


bot.run()
```

- Handlers, filters, middlewares and error handlers are plain functions. An `async def` handler
  makes the bot refuse to start, with an error that names it.
- Handlers run in a pool of `concurrency` threads (8 by default), named `pgpcbot-handler_0`, `pgpcbot-handler_1` and so on.
  As with `Bot`, one chat is handled one update at a time, in order.
- Inside a handler you can call any method of the bot (`bot.send_message(...)`,
  `message.reply(...)`): they block until done.
- Code shared between handlers must be thread-safe, because handlers of different chats run at
  the same time.

`SyncBot.run()` cannot be called from inside a running event loop (for example in a Jupyter
cell): there, use `Bot` and `await bot.start_polling()`.

`SyncBot.stop()` is a plain function too: it asks the bot to stop and returns at once, from a
handler or from any other thread; `run()` returns once the running handlers are done.

## `Bot` inside your own event loop

`bot.run()` creates and owns its event loop. When you already have one, use the async methods:

```python
async def main():
    async with Bot() as bot:          # starts the bot without polling: sends work
        await bot.send_message(7781, "The bot is up")
        ...


asyncio.run(main())
```

```python
async def main():
    bot = Bot()
    polling = asyncio.create_task(bot.start_polling())
    await other_work()
    await bot.stop()
    await polling
```

In a FastAPI app, use the webhook and `lifespan=bot.lifespan` ([Webhooks](webhook.md)).

## The low-level clients

```python
from pgpcbot import AsyncClient, Client

async with AsyncClient() as api:
    me = await api.get_me()

with Client() as api:
    me = api.get_me()
```

They map one method to one operation of the API, with validation, retries, rate limits and
typed results. They don't encrypt anything: in an encrypted chat you pass `armored=` yourself.
A bot's client is `bot.api`.

Close a client when you are done with it (`with` does it for you): it holds a pool of
connections. A client must not be shared between event loops, and cannot be pickled.

## Testing your handlers

`process_update` runs one update through the whole pipeline (decryption, filters, handlers)
without polling:

```python
await bot.process_update({
    "updateId": 1,
    "kind": "message",
    "message": {
        "id": 10, "chat": {"id": 55, "kind": "private"},
        "from": {"id": 55, "username": "anna"},
        "date": 1791462000000, "kind": "user", "text": "/start",
    },
})
```

`SyncBot.process_update` does the same without `await`. It still calls the API for the bot's
own details and for any reply, so in tests give the bot a fake transport
(`Bot(token=..., transport=...)`, an httpx2 transport) or a test server.
