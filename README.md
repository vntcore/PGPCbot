# PGPCbot

[![ci](https://github.com/vntcore/PGPCbot/actions/workflows/ci.yml/badge.svg)](https://github.com/vntcore/PGPCbot/actions/workflows/ci.yml)

A Python library for writing bots for [PGP Chat](https://pgpchat.app), a messenger whose
private chats are end-to-end encrypted with OpenPGP. It covers the whole Bot API, and it
handles the encryption for you.

**Documentation: <https://vntcore.github.io/PGPCbot/>** - the guides and the whole API reference.

```python
from pgpcbot import Bot

bot = Bot()  # reads the token from PGPCHAT_BOT_TOKEN


@bot.command("start", description="Say hello")
async def start(message):
    await message.reply("Hello! Send me anything and I will repeat it.")


@bot.on_message()
async def echo(message):
    await message.reply(f"You said: {message.text}")


bot.run()
```

If the bot has a published OpenPGP key, its private chats are end-to-end encrypted. The same
code still works: messages reach your handlers already decrypted, and replies are encrypted
for the people who should read them. You don't change anything in the handlers.

## What it does

- **Every operation of the Bot API**, with typed models, as async calls (`AsyncClient`)
  and as blocking calls (`Client`).
- **A high-level bot** with handlers for messages, commands (`/start` and `/start@yourbot`),
  button presses, edited and deleted messages, and changes to the bot's membership. Filters you
  can combine, routers to split a bot into modules, middleware, and one place to handle errors.
- **Automatic OpenPGP**: incoming messages are decrypted and their signatures checked;
  replies are encrypted for every member of the chat and signed. A message that should be
  encrypted is never sent in clear. Tested with real keys and messages from the phones'
  library (PGPainless 1.7.6).
- **Long polling and webhooks** with "at least once" delivery done right: no update is lost
  when a handler fails, no update is confirmed to the server before it is safe, and a SQLite
  queue survives restarts.
- **Safe retries**: every send has a `sendId`, so a retry after a timeout never writes the
  message twice. After a `429` the client waits as long as the server asks. A client-side rate
  limiter uses the same algorithm as the server.
- **Security**: the token never shows up in logs, exceptions or `repr`. Secrets can come from
  environment variables or from files (Docker and Kubernetes secrets too). The webhook checks
  its secret in constant time.

## Install

You need Python 3.11 or newer (tested up to 3.14). The OpenPGP engines come as prebuilt
wheels for Linux and macOS (x86-64 and arm64) and for Windows (x86-64), so there is nothing
to compile; anywhere else pip builds them and you need a Rust toolchain.

```bash
pip install PGPCbot
```

From a clone of the repository, `pip install .`, or `pip install -e .` to work on the code.

Optional extras: `PGPCbot[webhook]` adds uvicorn to serve the webhook, `PGPCbot[http2]` adds
HTTP/2 support. Check that it works:

```bash
python -c "import pgpcbot; print(pgpcbot.__version__)"
pgpcbot --version
```

## Your first bot

1. Create the bot in the app: Settings → Account → My bots, or write to `@bot`. Copy the
   token when you see it: it is shown only once.
2. Put it in an environment variable, not in your code:

   ```bash
   export PGPCHAT_BOT_TOKEN="pgpbot_..."          # PowerShell: $env:PGPCHAT_BOT_TOKEN="pgpbot_..."
   export PGPCHAT_API="https://api.pgpchat.app"   # only if your server is a different one
   ```

3. Check the token: `pgpcbot whoami`.
4. Save the example at the top of this page as `bot.py`, run `python bot.py`, and write to your
   bot from the app. Stop it with Ctrl+C: it finishes the messages it is working on first.

## Commands and buttons

```python
from pgpcbot import Bot, Button, InlineKeyboard

bot = Bot()


@bot.command("weather", description="Forecast for a town")
async def weather(message, command):
    town = command.args or "Rome"
    keyboard = InlineKeyboard.build(
        [Button.callback("Hourly", f"hourly:{town}"), Button.callback("Another town", "ask")],
        [Button.link("On the web", "https://example.org/weather")],
    )
    await message.reply(f"{town}: sun, sun, rain.", markup=keyboard)


@bot.on_callback_query(prefix="hourly:")
async def hourly(query):
    await query.answer("Here is the hourly view")
    await query.edit_message(f"{query.data[7:]}: 14° at 10, 18° at 14.", markup=None)


bot.run()
```

Commands registered with a `description` go into the app's command menu with
`await bot.sync_commands()`.

## An end-to-end encrypted bot

```bash
pgpcbot keygen --name Weather --username weatherbot --out bot-key.asc --passphrase-env BOT_KEY_PASSPHRASE
```

```python
import os
from pgpcbot import Bot

bot = Bot(
    pgp_private_key="bot-key.asc",
    pgp_passphrase=os.environ["BOT_KEY_PASSPHRASE"],
    publish_key=True,   # on the first run, publish the key: private chats become encrypted
)


@bot.on_message()
async def echo(message):
    # already decrypted; message.verification says if the sender's signature is valid
    await message.reply(f"Got it, privately: {message.text}")


bot.run()
```

Publishing the key cannot be undone through the API: only the bot's owner can switch the bot
back to readable chats, from the console in the app. Read [the cryptography page][crypto]
before you go to production.

## Webhook with FastAPI

```python
import os
from fastapi import FastAPI
from pgpcbot import Bot, SQLiteStorage

bot = Bot(storage=SQLiteStorage("bot.db"))


@bot.on_message()
async def echo(message):
    await message.reply(message.text or "...")


webhook = bot.create_webhook_app(
    secret=os.environ["WEBHOOK_SECRET"],
    url="https://example.org/pgpchat",   # registered with the server at start-up
)
app = FastAPI(lifespan=bot.lifespan)
app.add_route("/pgpchat", webhook, methods=["POST"])
```

Run it with `uvicorn app:app`. With `SQLiteStorage` every update is saved before the server
gets its answer, and handled right after: a crash loses nothing.

## Without asyncio

```python
from pgpcbot import SyncBot

bot = SyncBot()


@bot.command("start")
def start(message):
    message.reply("Hello!")


bot.run()
```

`SyncBot` has the same methods as `Bot`, without `await`. Handlers run in a pool of threads.
For a script that only needs to send one message:

```python
from pgpcbot import Client

with Client() as api:
    api.send_message(7781, "Last night's backup finished fine.")
```

## Documentation

The site is at <https://vntcore.github.io/PGPCbot/>: the guides, and the API reference built from the
docstrings. It follows `main`.

The same reference without a browser: `help(pgpcbot.Bot)` in Python, or
`python -m pydoc pgpcbot.Bot` in a terminal. The pages are Markdown in [`docs/`][docs], and
from a clone you can build the site yourself:

```bash
pip install -e . --group docs
sphinx-build -b html docs docs/_build/html
```

Good places to start: [quick start][quickstart], [cryptography][crypto],
[API coverage][coverage], [research and decisions][research]. Ready-to-run examples are in
[`examples/`][examples].

## Security

See [SECURITY.md][security] for the threat model and for how to report a vulnerability. In
short: keep the token and the private key out of your code and out of repositories. If the
token leaks, `await bot.revoke_token()` burns it at once.

## License

MIT. The dependencies have their own licenses: httpx2 (BSD-3-Clause), pysequoia (Apache-2.0,
with Sequoia PGP under LGPL-2.0-or-later inside its wheels), rpgp-py (MIT).

[docs]: https://github.com/vntcore/PGPCbot/tree/main/docs
[examples]: https://github.com/vntcore/PGPCbot/tree/main/examples
[quickstart]: https://github.com/vntcore/PGPCbot/blob/main/docs/getting-started/quickstart.md
[crypto]: https://github.com/vntcore/PGPCbot/blob/main/docs/CRYPTOGRAPHY.md
[coverage]: https://github.com/vntcore/PGPCbot/blob/main/docs/API_COVERAGE.md
[research]: https://github.com/vntcore/PGPCbot/blob/main/docs/RESEARCH.md
[security]: https://github.com/vntcore/PGPCbot/blob/main/SECURITY.md
