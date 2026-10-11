"""An end-to-end encrypted bot.

    python examples/11_generate_pgp_keys.py          # once, makes bot-key.asc
    export PGPCHAT_BOT_TOKEN="pgpbot_..."
    export PGPCHAT_BOT_KEY_FILE=bot-key.asc
    export PGPCHAT_BOT_KEY_PASSPHRASE="..."
    export PUBLISH_KEY=1                             # only the first time
    python examples/10_encrypted_bot.py

Publishing the key makes the bot's private chats end-to-end encrypted, and only
the bot's owner can undo that, from the console in the app. Try it on a test bot
first. The handlers are the same as for a readable bot: messages arrive
decrypted, and replies are encrypted and signed for you.
"""

import os

from pgpcbot import Bot, SignatureStatus

bot = Bot(
    publish_key=os.environ.get("PUBLISH_KEY") == "1",
    # a message whose signature is wrong never reaches the handlers
    verify_signatures="require",
)


@bot.on_message()
async def echo(message):
    if not message.encrypted:
        await message.reply("This chat is readable by the server, nothing I can do about it here.")
        return
    status = message.verification.status
    who = "you" if status is SignatureStatus.VALID else "someone I can't verify"
    await message.reply(f"Got it, privately, from {who}: {message.text}")


@bot.on_startup
async def say_how(b: Bot) -> None:
    me = await b.get_me()
    if b.key is None:
        print("no private key loaded: set PGPCHAT_BOT_KEY_FILE")
    elif me.fingerprint == b.key.fingerprint:
        print(f"protected with key {b.key.fingerprint}")
    else:
        print("the key is loaded but not published: set PUBLISH_KEY=1 once")


if __name__ == "__main__":
    bot.run()
