"""The smallest bot there is: it answers every message with a greeting.

    export PGPCHAT_BOT_TOKEN="pgpbot_..."
    python examples/01_hello_world.py

Then write to the bot from the app. Ctrl+C stops it.
"""

from pgpcbot import Bot

bot = Bot()


@bot.on_message()
async def hello(message):
    await message.reply("Hello, world!")


if __name__ == "__main__":
    bot.run()
