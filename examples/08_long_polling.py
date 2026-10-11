"""Long polling by hand, with the low-level client and no Bot.

    export PGPCHAT_BOT_TOKEN="pgpbot_..."
    python examples/08_long_polling.py

You rarely need this: Bot.run() does the same with a durable queue, retries and
per-chat ordering. It shows what the server expects. Each update is confirmed
only after it was handled, by sending its id + 1 as the next offset, so a crash
means an update comes again, never that one is lost.
"""

import asyncio

from pgpcbot import AsyncClient, NetworkError, ServerError


async def main() -> None:
    async with AsyncClient() as api:
        me = await api.get_me()
        print(f"polling as @{me.username}, Ctrl+C to stop")
        offset = None
        while True:
            try:
                updates = await api.get_updates(offset, timeout=50)
            except (NetworkError, ServerError) as exc:
                print(f"poll failed ({exc}), trying again in 5 s")
                await asyncio.sleep(5)
                continue
            for update in updates:
                message = update.message
                if update.kind == "message" and message is not None and message.text:
                    if message.encrypted:
                        # the low-level client doesn't decrypt: that's Bot's job
                        print(f"chat {message.chat.id}: an encrypted message")
                    else:
                        await api.send_message(message.chat.id, f"polled: {message.text}", reply_to=message.id)
                # confirmed at the next request, after it was handled
                offset = update.update_id + 1


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
