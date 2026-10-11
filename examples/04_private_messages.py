"""A tiny notebook that only works in private chats.

    export PGPCHAT_BOT_TOKEN="pgpbot_..."
    python examples/04_private_messages.py

Write anything to the bot in a private chat and it keeps it; /notes lists what
you wrote, /clear forgets it. In groups it points people to a private chat.

The notes live in memory and are gone when the bot restarts: a real bot would
use a database.
"""

from collections import defaultdict

from pgpcbot import Bot
from pgpcbot import filters as f

bot = Bot()
notes: dict[int, list[str]] = defaultdict(list)


@bot.command("notes", filters=(f.private,))
async def show(message):
    mine = notes.get(message.chat.id)
    if not mine:
        await message.reply("No notes yet. Just write me something.")
        return
    lines = [f"{n}. {text}" for n, text in enumerate(mine, start=1)]
    await message.reply("\n".join(lines))


@bot.command("clear", filters=(f.private,))
async def clear(message):
    notes.pop(message.chat.id, None)
    await message.reply("All gone.")


@bot.on_message(private=True)
async def keep(message):
    if not message.text:
        await message.reply("I only keep text.")
        return
    notes[message.chat.id].append(message.text)
    await message.reply(f"Saved. You have {len(notes[message.chat.id])} note(s).", quote=False)


@bot.on_message(private=False)
async def elsewhere(message):
    me = await bot.get_me()
    await message.reply(f"Notes are private: write to @{me.username} directly.")


if __name__ == "__main__":
    bot.run()
