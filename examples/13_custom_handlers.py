"""Your own filters, middleware, priorities and routers.

    export PGPCHAT_BOT_TOKEN="pgpbot_..."
    python examples/13_custom_handlers.py

Write "#42" to look up an order, a long text to have its words counted, or
anything with "spam" in it to see the guard drop it.
"""

import logging
import re
import time

from pgpcbot import Bot, Router, SkipHandler
from pgpcbot import filters as f

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("example")

bot = Bot()
orders = Router(name="orders")
everything_else = Router(name="fallback")

ORDERS = {42: "shipped", 43: "being packed"}


# a filter can hand a value to the handler by returning a dict
def order_number(message):
    m = re.fullmatch(r"#(\d+)", (message.text or "").strip())
    return {"order_id": int(m[1])} if m else False


def long_text(message):
    return len(message.text or "") > 200


@orders.on_message(order_number)
async def show_order(message, order_id):
    status = ORDERS.get(order_id)
    await message.reply(f"Order {order_id}: {status}" if status else f"No order {order_id}.")


@orders.on_message(f.has_text & long_text)
async def count_words(message):
    await message.reply(f"That's {len(message.text.split())} words.")


@everything_else.on_message()
async def fallback(message):
    await message.reply("Send #<number> to look up an order.")


# the bot's own handlers are tried before any router's
@bot.on_message(f.has_text)
async def spam_guard(message):
    if "spam" in message.text.lower():
        log.info("dropped a message from %s", message.from_user.mention if message.from_user else "someone")
        return
    raise SkipHandler  # not spam: let the next handler have it


@bot.middleware
async def timing(handler, event, data):
    started = time.perf_counter()
    try:
        return await handler(event, data)
    finally:
        log.info("handled in %.1f ms", (time.perf_counter() - started) * 1000)


# routers are tried in the order they are included: the catch-all goes last
bot.include_routers(orders, everything_else)


if __name__ == "__main__":
    bot.run(configure_logging=False)
