# Your first bot

This page takes you from nothing to a bot that answers in a chat. It takes about five minutes.

## 1. Make the bot in the app

In PGP Chat, open Settings → Account → My bots (or write to `@bot`) and create a bot. You
choose a name and a handle that ends in `bot`, for example `@weatherbot`.

The app shows the bot's **token** once, at the end. It looks like `pgpbot_412.` followed by
43 letters, digits, `-` and `_`. Copy it somewhere safe: whoever has the token can act as the
bot.

## 2. Give the token to your program

Put the token in an environment variable, not in your code:

On Linux and macOS:

```bash
export PGPCHAT_BOT_TOKEN="pgpbot_..."
```

On Windows (PowerShell):

```powershell
$env:PGPCHAT_BOT_TOKEN = "pgpbot_..."
```

If your bot lives on a server other than the public one, also set
`PGPCHAT_API` (for example `https://api.example.org`). The console in the app shows the right
address.

Check that the token works:

```bash
pgpcbot whoami
```

```text
@weatherbot (id 412) - Weather
mode:     open
status:   active, privacy mode on
webhook:  none (polling)
queued:   0 update(s)
```

## 3. Write the bot

Save this as `bot.py`:

```python
from pgpcbot import Bot

bot = Bot()


@bot.command("start", description="Say hello")
async def start(message):
    name = message.from_user.first_name if message.from_user else "there"
    await message.reply(f"Hello {name}! Send me anything and I will repeat it.")


@bot.on_message()
async def echo(message):
    if message.text:
        await message.reply(f"You said: {message.text}")
    else:
        await message.reply("I can only repeat text.")


bot.run()
```

The part that is not obvious: the first handler that matches wins, so `/start` never reaches
`echo`. The command also answers `/start@weatherbot`, which is how people call a bot in a
group. `run()` asks the server for updates (long polling) until you press Ctrl+C.

## 4. Run it

```bash
python bot.py
```

```text
2026-10-08 18:02:11,532 INFO    pgpcbot.bot: running as @weatherbot (id 412, open)
```

Open a chat with your bot in the app and write `/start`. Then write anything else.

Stop the bot with Ctrl+C. It finishes the messages it is working on, then exits.

## 5. Publish the commands

The app shows a menu of commands when someone types `/`. Commands registered with a
`description` can be published with one call:

```python
import asyncio
from pgpcbot import Bot

bot = Bot()


@bot.command("start", description="Say hello")
async def start(message): ...


asyncio.run(bot.sync_commands())
```

You only need to do this when the list changes.

## What next

- Make the bot end-to-end encrypted: [OpenPGP step by step](../guides/openpgp.md).
- Add buttons: [Commands and buttons](../guides/commands-and-buttons.md).
- Don't lose messages when the bot restarts: [Storage](../guides/storage.md).
- Every setting: [Configuration](configuration.md).
- No asyncio: [Async and sync](../guides/async-and-sync.md) shows `SyncBot`.
