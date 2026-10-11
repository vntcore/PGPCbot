"""Repeats what people write, and says what it got when it isn't text.

    export PGPCHAT_BOT_TOKEN="pgpbot_..."
    python examples/02_echo_bot.py

In a group the bot only sees commands, replies to it and @mentions, unless its
owner turned privacy mode off.
"""

from pgpcbot import Bot

bot = Bot()


@bot.on_message()
async def echo(message):
    if message.text:
        await message.reply(message.text)
    elif message.media:
        kinds = ", ".join(item.kind for item in message.media)
        await message.reply(f"Nice {kinds}. I can only repeat words, though.")
    else:
        # stickers, call notices, payment requests: no text to repeat
        await message.reply("I can't repeat that one.")


@bot.on_edited_message()
async def edited(message):
    await message.answer(f"You changed it to: {message.text}")


if __name__ == "__main__":
    bot.run()
