"""Buttons under a message: a counter you can press, and links.

    export PGPCHAT_BOT_TOKEN="pgpbot_..."
    python examples/06_inline_buttons.py

Send /counter, then press the buttons. The text of buttons and their data travel
in clear even in encrypted chats: never put personal data in them.
"""

from pgpcbot import Bot, Button, InlineKeyboard, KeyboardBuilder

bot = Bot()


def counter_keyboard(value: int) -> InlineKeyboard:
    # every button of a message needs its own data: at 1, "-1" and a plain "count:0"
    # reset would clash, so the reset button says what it is instead
    return InlineKeyboard.build(
        [Button.callback("-1", f"count:{value - 1}"), Button.callback("+1", f"count:{value + 1}")],
        [Button.callback("Reset", "count:reset")],
    )


@bot.command("counter")
async def counter(message):
    await message.reply("Count: 0", markup=counter_keyboard(0))


@bot.on_callback_query(prefix="count:")
async def count(query):
    # the value travels in the button itself, so the bot keeps no state
    raw = query.data.removeprefix("count:")
    value = 0 if raw == "reset" else int(raw)
    await query.answer()
    await query.edit_message(f"Count: {value}", markup=counter_keyboard(value))


@bot.command("links")
async def links(message):
    builder = KeyboardBuilder(per_row=2)
    builder.add(
        Button.link("PGP Chat", "https://pgpchat.app"),
        Button.link("Python", "https://www.python.org"),
        Button.link("OpenPGP", "https://www.openpgp.org"),
    )
    await message.reply("Some links:", markup=builder.build())


if __name__ == "__main__":
    bot.run()
