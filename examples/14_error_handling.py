"""Handling errors in one place, and the failed updates the bot keeps.

    export PGPCHAT_BOT_TOKEN="pgpbot_..."
    export ADMIN_ID=55                    # your own user id, for /failed and /retry
    python examples/14_error_handling.py

Send /divide 10 0 to make a handler fail. The update is kept in the failed list;
the admin can look at it with /failed and run it again with /retry.
"""

import logging
import os

from pgpcbot import BlockedByUserError, Bot, ForbiddenError, RateLimitError, ValidationError
from pgpcbot import filters as f

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("example")

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
bot = Bot()


@bot.command("divide")
async def divide(message, command):
    a, b = (int(x) for x in command.argv[:2])
    await message.reply(f"{a} / {b} = {a / b}")  # b = 0 raises ZeroDivisionError


@bot.command("long")
async def too_long(message):
    try:
        await message.reply("x" * 5000)
    except ValidationError as exc:
        # nothing was sent: the server would have refused it too
        await message.reply(f"Not sent: {exc}")


@bot.on_error(ValueError)
async def bad_input(error, update):
    await bot.send_message(update.chat, "Use it like this: /divide 10 2")


@bot.on_error(BlockedByUserError)
async def blocked(error, update):
    log.info("chat %s blocked the bot: stop writing there", update.chat.id)


@bot.on_error(ForbiddenError, RateLimitError)
async def not_now(error, update):
    log.warning("update %s: %s", update.update_id, error)
    raise error  # keep it in the failed list, to retry later


@bot.on_error()
async def everything_else(error, update):
    log.error("update %s failed: %r", update.update_id, error)
    raise error  # not handled: kept with its error in the failed list


@bot.command("failed", filters=(f.user(ADMIN_ID),))
async def failed(message):
    items = await bot.failed_updates(limit=10)
    if not items:
        await message.reply("Nothing failed.")
        return
    lines = [f"#{item.update_id} {item.failed_at:%H:%M:%S}: {item.error}" for item in items]
    await message.reply("\n".join(lines))


@bot.command("retry", filters=(f.user(ADMIN_ID),))
async def retry(message):
    count = await bot.retry_failed()
    await message.reply(f"{count} update(s) back in the queue.")


if __name__ == "__main__":
    bot.run(configure_logging=False)
