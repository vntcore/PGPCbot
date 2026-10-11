"""A bot inside your own asyncio program, next to other work.

    export PGPCHAT_BOT_TOKEN="pgpbot_..."
    export REPORT_CHAT=7781               # a chat the bot is in
    python examples/16_async_bot.py

The bot polls while a second task posts a short report every few minutes. Ctrl+C
stops both: the bot finishes the handlers that are running first.
"""

import asyncio
import os
import time

from pgpcbot import Bot

REPORT_CHAT = int(os.environ.get("REPORT_CHAT", "0"))
EVERY = float(os.environ.get("REPORT_EVERY", "300"))
started = time.monotonic()
handled = 0

bot = Bot()


@bot.on_message()
async def count(message):
    global handled
    handled += 1
    await message.reply(f"Message number {handled} since start.")


async def reporter() -> None:
    while True:
        await asyncio.sleep(EVERY)
        if REPORT_CHAT:
            minutes = (time.monotonic() - started) / 60
            await bot.send_message(REPORT_CHAT, f"Up {minutes:.0f} min, {handled} message(s) handled.")


async def main() -> None:
    async with asyncio.TaskGroup() as group:
        polling = group.create_task(bot.start_polling())
        reporting = group.create_task(reporter())
        # start_polling returns once the bot stops; then the reporter goes too
        polling.add_done_callback(lambda _: reporting.cancel())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
