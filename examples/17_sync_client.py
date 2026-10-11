"""No asyncio at all: the blocking client in a script, and SyncBot.

    export PGPCHAT_BOT_TOKEN="pgpbot_..."
    python examples/17_sync_client.py report 7781 "Nightly backup: OK"
    python examples/17_sync_client.py bot

`report` sends one message and exits, the way a cron job would. `bot` runs a
SyncBot whose handlers are plain functions, run in a pool of threads.
"""

import sys

from pgpcbot import APIError, Client, NetworkError, SyncBot


def report(chat_id: int, text: str) -> int:
    with Client() as api:
        try:
            me = api.get_me()
            sent = api.send_message(chat_id, text)
        except (APIError, NetworkError) as exc:
            print(f"not sent: {exc}", file=sys.stderr)
            return 1
    print(f"@{me.username} sent message {sent.id}")
    return 0


def run_bot() -> None:
    bot = SyncBot()

    @bot.command("start")
    def start(message):
        message.reply("Hello from a bot without asyncio.")

    @bot.on_message()
    def echo(message):
        message.typing()
        message.reply(f"(sync) {message.text}")

    bot.run()


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "report":
        raise SystemExit(report(int(sys.argv[2]), sys.argv[3]))
    if len(sys.argv) == 2 and sys.argv[1] == "bot":
        run_bot()
    else:
        raise SystemExit(__doc__)
