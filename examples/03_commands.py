"""Commands with arguments, and the command menu the app shows on "/".

    export PGPCHAT_BOT_TOKEN="pgpbot_..."
    python examples/03_commands.py

Try /start, /help, /roll, /roll 20, /pick tea coffee water.
"""

import random

from pgpcbot import Bot
from pgpcbot import filters as f

bot = Bot()


@bot.on_startup
async def publish_menu(bot):
    # every command below with a description ends up in the app's menu
    await bot.sync_commands()


@bot.command("start", description="Say hello")
async def start(message):
    name = message.from_user.first_name if message.from_user else None
    await message.reply(f"Hi {name or 'there'}! Type /help to see what I can do.")


@bot.command("help", description="What I can do")
async def help_(message):
    lines = [f"/{name} - {text}" for name, text in bot.declared_commands()]
    await message.reply("\n".join(lines))


@bot.command("roll", description="Roll a die: /roll or /roll 20")
async def roll(message, command):
    sides = 6
    if command.args:
        if not command.args.isdigit() or not 2 <= int(command.args) <= 1000:
            await message.reply("Give me a number of sides between 2 and 1000.")
            return
        sides = int(command.args)
    await message.reply(f"🎲 {random.randint(1, sides)} (d{sides})")


@bot.command("pick", description="Pick one: /pick tea coffee water")
async def pick(message, command):
    if len(command.argv) < 2:
        await message.reply("Give me at least two things to pick from.")
        return
    await message.reply(f"I pick {random.choice(command.argv)}.")


# registered last, so the commands above get their turn first
@bot.on_message(f.command())
async def unknown(message, command):
    await message.reply(f"I don't know /{command.name}. Try /help.")


if __name__ == "__main__":
    bot.run()
