# Webhooks

With a webhook, the server sends each update to your web address as soon as it happens. You
need a public HTTPS address. In exchange there is no polling, and the bot can run inside a web
application you already have.

## How the server delivers

- One update per request, a `POST` with a JSON body, **in order**.
- If you set a secret, every request carries it in the `X-PGPChat-Bot-Api-Secret-Token` header.
- Any `2xx` answer within 10 seconds means "delivered". Anything else (an error, a timeout, a
  redirect) means "try again": the server waits 1 second, then 2, 4, and so on up to one hour,
  and sends **the same update** again. Later updates wait behind it.
- After ten failures in a row, the owner is told in the app. Updates keep queuing for up to 72
  hours.
- The address must be `https`, must not contain a user name, password or `#fragment`, and must
  point to a public IP address.

Because the server retries until it gets a `2xx`, an update can arrive twice. The library
recognises repeats by their `updateId` and handles each update once.

## The webhook app

`bot.create_webhook_app()` returns a standard ASGI application that does everything above
correctly:

```python
import os
from pgpcbot import Bot, SQLiteStorage

bot = Bot(storage=SQLiteStorage("bot.db"))


@bot.on_message()
async def echo(message):
    await message.reply(message.text or "...")


app = bot.create_webhook_app(
    secret=os.environ["WEBHOOK_SECRET"],
    url="https://bot.example.org/pgpchat",      # registered with the server at start-up
)
```

```bash
uvicorn bot:app --host 127.0.0.1 --port 8080
```

What it does with each request:

| Request | Answer |
|---|---|
| secret missing or wrong | `401`, nothing else is read |
| not a `POST` | `405` |
| body bigger than `max_body` (1 MiB) | `413` |
| body sent too slowly (over 10 seconds) | `408` |
| a different path, when you set `path=` | `404` |
| a valid update | `200` once the update is safe (see below) |
| a request with the right secret but a body that is not an update | `200`, and an error in the logs |

The last row is on purpose. If the app answered `400`, the server would retry the same broken
delivery for days and every later update would wait behind it. The secret proves the request
came from the server, so the problem is logged for you to look at, and the queue moves on.

### Options

| Option | Default | Meaning |
|---|---|---|
| `secret` | required | the secret the server sends with each delivery. Make one with `generate_secret()` |
| `url` | `None` | your public address. If given, the app registers it (and the secret) with the server every time it starts |
| `path` | `None` | answer only on this path, `404` elsewhere. Not needed inside another app's route |
| `max_body` | 1 MiB | the largest body accepted |
| `ack_timeout` | `8.0` | with `MemoryStorage`: how long to wait for the handlers before answering `503`. Must be under 10 seconds |
| `allow_no_secret` | `False` | allow `secret=None`. Anyone who finds the address could then send fake updates: don't |

Make a secret once and keep it with your other secrets:

```bash
python -c "from pgpcbot import generate_secret; print(generate_secret())"
```

## When is an update "safe"?

The answer to the server depends on the storage:

- **`SQLiteStorage` (recommended)**: the app answers `200` as soon as the update is written to
  the database. The handlers run right after. A crash loses nothing: the update is on disk and
  runs at the next start. The server can send the next update at once, so many chats are
  handled at the same time.
- **`MemoryStorage`**: there is nothing on disk, so the app answers `200` only when the handlers
  are done. If they take longer than `ack_timeout`, it answers `503` and the server sends the
  update again a little later; by then it is usually done, and the app answers `200` without
  running it again. The server sends one update at a time and waits for each answer, so with
  `MemoryStorage` the bot effectively handles **one update at a time**.

Use `SQLiteStorage` for webhooks.

## With FastAPI

```python
from fastapi import FastAPI

webhook = bot.create_webhook_app(secret=os.environ["WEBHOOK_SECRET"], url="https://example.org/pgpchat")
app = FastAPI(lifespan=bot.lifespan)
app.add_route("/pgpchat", webhook, methods=["POST"])


@app.get("/health")
async def health():
    return {"ok": True}
```

`lifespan=bot.lifespan` starts the bot when the application starts and stops it cleanly at
shutdown. It is needed because the webhook app, inside another application, does not get start
and stop events of its own. If you forget it, the bot starts at the first delivery and logs a
warning.

:::{admonition} A route, not a mount
:class: warning

`app.mount("/pgpchat", webhook)` answers a POST to `/pgpchat` with a redirect to
`/pgpchat/`, and the server counts a redirect as a failed delivery: the queue stops. Use
`add_route` as above, or register the address with the trailing slash
(`url="https://example.org/pgpchat/"`) if you do mount it.
:::

If your application already has a lifespan, start the bot inside it:

```python
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    async with bot.lifespan(app):
        await database.connect()
        yield
        await database.disconnect()


app = FastAPI(lifespan=lifespan)
```

## With Starlette

```python
from starlette.applications import Starlette
from starlette.routing import Route

app = Starlette(
    routes=[Route("/pgpchat", bot.create_webhook_app(secret=os.environ["WEBHOOK_SECRET"]), methods=["POST"])],
    lifespan=bot.lifespan,
)
```

## With another framework

`WebhookApp.handle()` does the checks and the processing without ASGI. Give it the headers and
the raw body, and send back what it returns:

```python
webhook = bot.create_webhook_app(secret=os.environ["WEBHOOK_SECRET"])

result = await webhook.handle(request_headers, raw_body)
# result.status, result.body (a dict to send as JSON), result.headers
```

Start the bot yourself first (`await bot.start()`) and stop it at the end (`await bot.stop()`).

## Behind a reverse proxy

The usual setup is nginx or Caddy for HTTPS, in front of uvicorn on `127.0.0.1`:

```nginx
location /pgpchat {
    proxy_pass http://127.0.0.1:8080;
    proxy_read_timeout 15s;
    client_max_body_size 1m;
}
```

- Pass the body untouched and keep the `X-PGPChat-Bot-Api-Secret-Token` header.
- Don't answer with a redirect (for example from `/pgpchat` to `/pgpchat/`): for the server a
  redirect is a failed delivery. Register the exact address the app answers on.

## Several worker processes

With `SQLiteStorage` you can run several web workers (for example `uvicorn --workers 4`)
against the same database file:

- every worker accepts deliveries and writes them to the database;
- only one worker runs the handlers, the one that holds the database lock;
- if that worker dies, another one takes over and runs what is waiting.

The database must be on a local disk shared by the workers, not on a network share.

## Switching between webhook and polling

- A webhook stops polling: `GET /updates` is refused while a webhook is set. `bot.run()`
  refuses to start, unless you pass `drop_webhook=True`.
- `await bot.delete_webhook()` (or `pgpcbot webhook delete`) goes back to polling. Updates that
  queued meanwhile are not lost.
- `pgpcbot webhook info` shows the address, how many updates are waiting, and the last error.
