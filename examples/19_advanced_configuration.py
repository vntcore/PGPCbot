"""Most of the knobs, with a word on each.

    export PGPCHAT_BOT_TOKEN_FILE=/run/secrets/bot_token
    export PGPCHAT_BOT_KEY_FILE=/etc/weatherbot/bot-key.asc
    export PGPCHAT_BOT_KEY_PASSPHRASE_FILE=/run/secrets/bot_key_passphrase
    python examples/19_advanced_configuration.py

Every value here can also come from the environment; nothing secret is written
in the code.
"""

import logging
import os
from pathlib import Path

import httpx2

from pgpcbot import Bot, RetryPolicy, Secret, SQLiteStorage


def passphrase_from_vault() -> str:
    # stands in for a call to a secrets manager; read once, when the key is loaded
    path = os.environ.get("PGPCHAT_BOT_KEY_PASSPHRASE_FILE", "")
    return Path(path).read_text(encoding="utf-8").strip() if path else ""


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("pgpcbot.engine").setLevel(logging.DEBUG)

bot = Bot(
    # the token: None reads PGPCHAT_BOT_TOKEN or PGPCHAT_BOT_TOKEN_FILE; a Secret
    # or a function work too and keep it out of repr()
    token=Secret(os.environ["BOT_TOKEN"]) if "BOT_TOKEN" in os.environ else None,
    api_url=os.environ.get("PGPCHAT_API"),  # None: the public server
    pgp_passphrase=passphrase_from_vault if "PGPCHAT_BOT_KEY_PASSPHRASE_FILE" in os.environ else None,
    storage=SQLiteStorage(os.environ.get("BOT_DB", "bot.db"), timeout=30.0),
    concurrency=16,  # chats handled at once; one update at a time per chat
    max_handler_runs=5,  # crashes before an update goes to the failed list
    shutdown_timeout=20.0,  # seconds running handlers get at stop
    cache_ttl=30.0,  # chat details, modes and keys are kept this long
    require_encryption=False,  # True: never send anything in clear
    verify_signatures="auto",  # "require": drop messages without a valid signature
    pin_keys=False,  # True: refuse a person's key when it changes
    auto_answer_callbacks=True,  # stop a pressed button's spinner if no handler answered
    retry=RetryPolicy(attempts=5, backoff_base=0.5, backoff_max=30.0, rate_limit_retries=5, max_rate_limit_wait=90.0),
    rate_limit=True,  # the client-side limiter
    timeout=httpx2.Timeout(30.0, connect=5.0),
)


@bot.on_message()
async def echo(message):
    await message.reply(message.text or "...")


if __name__ == "__main__":
    print(bot)  # the token shows as pgpbot_<id>.****abcd
    bot.run(poll_timeout=50, limit=100, configure_logging=False)
