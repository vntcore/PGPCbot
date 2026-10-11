"""Sends one message from a script, encrypted when the chat is encrypted.

    export PGPCHAT_BOT_TOKEN="pgpbot_..."
    export PGPCHAT_BOT_KEY_FILE=bot-key.asc          # for encrypted chats
    export PGPCHAT_BOT_KEY_PASSPHRASE="..."
    python examples/12_send_encrypted_message.py 7781 "The backup finished."

The bot can only write into chats it is in. `--by-hand` shows the same steps
without the automatic part: fetch the members' keys, seal, send armored.
"""

import argparse
import asyncio

from pgpcbot import Bot, ChatMode, PublicKey


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("chat_id", type=int)
    parser.add_argument("text")
    parser.add_argument("--by-hand", action="store_true")
    args = parser.parse_args()

    async with Bot() as bot:
        if not args.by_hand:
            sent = await bot.send_message(args.chat_id, args.text)
            print(f"sent message {sent.id}, {'encrypted' if sent.encrypted else 'in clear'}")
            return

        chat = await bot.get_chat(args.chat_id)
        if chat.mode is ChatMode.OPEN:
            plain = await bot.api.send_message(args.chat_id, args.text)
            print(f"a readable chat: sent message {plain.id} in clear")
            return
        if bot.key is None:
            raise SystemExit("the chat is encrypted and no private key is loaded")
        keys = await bot.api.get_chat_keys(args.chat_id)
        recipients = [PublicKey.from_user_key(k) for k in keys.keys]
        armored = bot.key.seal(args.text, recipients)  # the bot's own key is added, and it is signed
        sealed = await bot.api.send_message(args.chat_id, armored=armored)
        print(f"sealed for {len(recipients)} key(s), sent message {sealed.id}")


if __name__ == "__main__":
    asyncio.run(main())
