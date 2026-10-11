"""Button presses in depth: a small poll and a menu that changes in place.

    export PGPCHAT_BOT_TOKEN="pgpbot_..."
    python examples/07_callbacks.py

/poll starts a vote, /menu opens a menu. A press has to be answered within 60
seconds; if a handler forgets, the library answers with nothing so the button
stops spinning.
"""

from collections import Counter

from pgpcbot import Bot, Button, CallbackExpiredError, InlineKeyboard

bot = Bot()
votes: dict[int, Counter[str]] = {}
voted: dict[int, set[int]] = {}

CHOICES = {"pizza": "Pizza", "pasta": "Pasta", "salad": "Salad"}


def poll_keyboard(poll_id: int) -> InlineKeyboard:
    tally = votes[poll_id]
    row = [Button.callback(f"{label} ({tally[key]})", f"vote:{poll_id}:{key}") for key, label in CHOICES.items()]
    return InlineKeyboard.build(row)


@bot.command("poll")
async def poll(message):
    sent = await message.answer("What's for lunch?")
    votes[sent.id], voted[sent.id] = Counter(), set()
    await sent.edit(markup=poll_keyboard(sent.id))


@bot.on_callback_query(pattern=r"^vote:(\d+):(\w+)$")
async def vote(query, match):
    poll_id, choice = int(match[1]), match[2]
    if poll_id not in votes:
        await query.answer("This poll is closed.", alert=True)
        return
    if query.from_user.id in voted[poll_id]:
        await query.answer("You already voted.")
        return
    voted[poll_id].add(query.from_user.id)
    votes[poll_id][choice] += 1
    await query.answer(f"You voted {CHOICES[choice]}")
    await query.edit_message(markup=poll_keyboard(poll_id))


MENUS = {
    "main": ("Main menu", [[("Settings", "menu:settings"), ("About", "menu:about")]]),
    "settings": ("Settings", [[("Language", "menu:lang")], [("Back", "menu:main")]]),
    "lang": ("Language: English only, sorry.", [[("Back", "menu:settings")]]),
    "about": ("A demo bot for PGPCbot.", [[("Back", "menu:main")]]),
}


def menu(name: str) -> tuple[str, InlineKeyboard]:
    text, rows = MENUS[name]
    return text, InlineKeyboard.build(*([Button.callback(label, data) for label, data in row] for row in rows))


@bot.command("menu")
async def open_menu(message):
    text, keyboard = menu("main")
    await message.reply(text, markup=keyboard)


@bot.on_callback_query(prefix="menu:")
async def navigate(query):
    text, keyboard = menu(query.data.removeprefix("menu:"))
    try:
        await query.answer()
    except CallbackExpiredError:
        pass  # pressed more than a minute ago: still worth updating the menu
    await query.edit_message(text, markup=keyboard)


if __name__ == "__main__":
    bot.run()
