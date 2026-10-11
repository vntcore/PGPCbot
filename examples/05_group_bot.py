"""A bot for groups: it greets the group when it is added, answers when it is
mentioned, and gives admins a couple of commands.

    export PGPCHAT_BOT_TOKEN="pgpbot_..."
    python examples/05_group_bot.py

Add the bot to a group from the app, then try /members, /who @somebody and
/leave. With privacy mode on (the default) the bot
only sees commands, replies to its own messages and messages that mention it.
The owner can turn privacy mode off in the app to let it see everything.
"""

from pgpcbot import Bot, ForbiddenError, NotFoundError, UsernameNotFoundError, ValidationError
from pgpcbot import filters as f

bot = Bot()


@bot.on_chat_member()
async def membership(change):
    if change.joined and not change.chat.is_private:
        await bot.send_message(change.chat, "Hello everyone! Mention me or use /help.")
    elif change.left:
        print(f"no longer in chat {change.chat.id}")


@bot.command("help", filters=(f.group,))
async def help_(message):
    await message.reply("/members - who is here\n/who @name - look a handle up\n/leave - make me leave (admins only)")


@bot.command("members", filters=(f.group,))
async def members(message):
    try:
        roster = await bot.get_chat_members(message.chat)
    except ForbiddenError:
        await message.reply("I am not allowed to see the member list here.")
        return
    names = [m.user.mention for m in roster.members]
    await message.reply(f"{roster.member_count} member(s): " + ", ".join(names[:50]))


@bot.command("who", filters=(f.group,))
async def who(message, command):
    # the one id no update ever hands over: the handle somebody typed
    if not command.args:
        await message.reply("Give me a handle: /who @anna")
        return
    try:
        found = await bot.resolve(command.args.split()[0])
    except ValidationError:
        await message.reply("That is not a handle.")
        return
    except UsernameNotFoundError:
        await message.reply("Nobody and no chat answers to that.")
        return

    if not found.is_user:
        card = found.chat
        await message.reply(f"{found.mention} is a {found.kind} - id {card.id}, {card.member_count} member(s).")
        return
    # a handle is not membership, so ask the roster before saying anything about it
    try:
        seat = await bot.get_chat_member(message.chat, found.id)
        where = f"here as {seat.status}"
    except NotFoundError:
        where = "not in this group"
    await message.reply(f"{found.mention} is user {found.id}, {where}.")


@bot.command("leave", filters=(f.group,))
async def leave(message):
    asker = await bot.get_chat_member(message.chat, message.from_user.id)
    if not asker.is_admin:
        await message.reply("Only an admin can send me away.")
        return
    await message.reply("Bye!")
    await bot.leave_chat(message.chat)


@bot.on_message(f.group & (f.mentioned | f.reply))
async def addressed(message):
    await message.reply(f"You called, {message.from_user.mention}?")


@bot.on_deleted_messages()
async def deleted(event):
    print(f"{len(event.message_ids)} message(s) deleted in chat {event.chat.id}")


if __name__ == "__main__":
    bot.run()
