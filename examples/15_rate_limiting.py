"""Sending to many chats without tripping the server's limits.

    export PGPCHAT_BOT_TOKEN="pgpbot_..."
    export ADMIN_ID=55
    python examples/15_rate_limiting.py

Everybody who writes /subscribe gets on the list; the admin's /broadcast <text>
sends to all of them in the background. The client paces itself like the server
(1 message a second per private chat, 20 a minute per group, 30 a second in all)
and waits out a 429 when one comes anyway.
"""

import asyncio
import os

from pgpcbot import BlockedByUserError, Bot, RateLimitError, RetryPolicy, UserGoneError
from pgpcbot import filters as f

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
# wait out longer slow modes than the default 90 s, and more of them
bot = Bot(retry=RetryPolicy(rate_limit_retries=8, max_rate_limit_wait=300))
subscribers: set[int] = set()
background: set[asyncio.Task[None]] = set()


@bot.command("subscribe")
async def subscribe(message):
    subscribers.add(message.chat.id)
    await message.reply("Subscribed. /unsubscribe to stop.")


@bot.command("unsubscribe")
async def unsubscribe(message):
    subscribers.discard(message.chat.id)
    await message.reply("Unsubscribed.")


async def send_to_everyone(text: str, report_to: int) -> None:
    sent = gone = 0
    for chat_id in list(subscribers):
        try:
            await bot.send_message(chat_id, text)
            sent += 1
        except (BlockedByUserError, UserGoneError):
            subscribers.discard(chat_id)
            gone += 1
        except RateLimitError as exc:
            # longer than the policy waits: try that chat again later
            print(f"chat {chat_id}: still rate limited ({exc.retry_after}s)")
    await bot.send_message(report_to, f"Broadcast done: {sent} sent, {gone} gone.")


@bot.command("broadcast", filters=(f.user(ADMIN_ID),))
async def broadcast(message, command):
    if not command.args:
        await message.reply("Usage: /broadcast <text>")
        return
    # a broadcast takes a while: don't hold up this chat for it
    task = asyncio.create_task(send_to_everyone(command.args, message.chat.id))
    background.add(task)
    task.add_done_callback(background.discard)
    await message.reply(f"Sending to {len(subscribers)} chat(s) in the background.")


if __name__ == "__main__":
    bot.run()
