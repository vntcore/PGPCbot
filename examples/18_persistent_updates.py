"""Updates that survive a restart, a crash, or a power cut.

    export PGPCHAT_BOT_TOKEN="pgpbot_..."
    python examples/18_persistent_updates.py

With SQLiteStorage every update is written to bot.db before the server is told
"got it", and handled right after. Stop the bot in the middle of /slow and start
it again: the update runs again. One process at a time can use the file; a
second copy is refused with PollingConflictError.
"""

import asyncio

from pgpcbot import Bot, SQLiteStorage

bot = Bot(
    storage=SQLiteStorage("bot.db"),
    # an update that keeps killing the process goes to the failed list after 3 tries
    max_handler_runs=3,
    # pinned key fingerprints live in the same file and survive restarts
    pin_keys=True,
)


@bot.command("slow")
async def slow(message):
    await message.reply("Working on it, 20 seconds... stop the bot now and start it again.")
    await asyncio.sleep(20)
    await message.reply("Done.")


@bot.on_message()
async def anything(message):
    await message.reply("Saved before I said a word to the server, then handled.")


@bot.on_startup
async def report(b: Bot) -> None:
    waiting = b.storage.count_pending()
    failed = await b.failed_updates()
    print(f"{waiting} update(s) picked up from the last run, {len(failed)} in the failed list")


if __name__ == "__main__":
    bot.run()
