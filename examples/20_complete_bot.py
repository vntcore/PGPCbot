"""A complete bot: a to-do list per chat, with buttons, admin commands, durable
storage, encryption when a key is configured, and a clean way to stop.

    export PGPCHAT_BOT_TOKEN="pgpbot_..."
    export ADMIN_ID=55
    # optional, for end-to-end encrypted private chats:
    export PGPCHAT_BOT_KEY_FILE=bot-key.asc PGPCHAT_BOT_KEY_PASSPHRASE="..."
    python examples/20_complete_bot.py

Commands: /add <thing>, /list, /help; admins: /stats, /shutdown.
The lists live in a small SQLite table next to the bot's own queue.
"""

import logging
import os
import sqlite3
from contextlib import closing

from pgpcbot import BlockedByUserError, Bot, Button, InlineKeyboard, Router, SQLiteStorage
from pgpcbot import filters as f

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("todo")

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DB = os.environ.get("TODO_DB", "todo.db")

bot = Bot(storage=SQLiteStorage(os.environ.get("BOT_DB", "todo-bot.db")))
todo = Router(name="todo")
admin = Router(name="admin")


# --- the to-do list ---


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, chat INTEGER, text TEXT, done INTEGER)")
    return conn


def items_of(chat_id: int) -> list[tuple[int, str, int]]:
    with closing(db()) as conn:
        return conn.execute("SELECT id, text, done FROM items WHERE chat = ? ORDER BY id", (chat_id,)).fetchall()


def keyboard_for(chat_id: int) -> InlineKeyboard | None:
    rows = [
        [Button.callback(("✅ " if done else "⬜ ") + text[:40], f"toggle:{item_id}")]
        for item_id, text, done in items_of(chat_id)[:20]
    ]
    return InlineKeyboard.build(*rows) if rows else None


@todo.command("start", "help", description="What I can do")
async def help_(message):
    await message.reply("/add <thing> adds to the list, /list shows it. Tap an item to tick it off.")


@todo.command("add", description="Add something to the list")
async def add(message, command):
    if not command.args:
        await message.reply("Add what? /add buy bread")
        return
    with closing(db()) as conn, conn:
        conn.execute("INSERT INTO items (chat, text, done) VALUES (?, ?, 0)", (message.chat.id, command.args[:200]))
    await message.reply("Added.", markup=keyboard_for(message.chat.id))


@todo.command("list", description="Show the list")
async def show(message):
    keyboard = keyboard_for(message.chat.id)
    await message.reply("Your list:" if keyboard else "The list is empty.", markup=keyboard)


@todo.on_callback_query(pattern=r"^toggle:(\d+)$")
async def toggle(query, match):
    with closing(db()) as conn, conn:
        conn.execute("UPDATE items SET done = 1 - done WHERE id = ? AND chat = ?", (int(match[1]), query.chat.id))
    await query.answer()
    await query.edit_message(markup=keyboard_for(query.chat.id))


# --- admin ---


@admin.command("stats", filters=(f.user(ADMIN_ID),))
async def stats(message):
    with closing(db()) as conn:
        count, chats = conn.execute("SELECT COUNT(*), COUNT(DISTINCT chat) FROM items").fetchone()
    failed = await bot.failed_updates()
    await message.reply(f"{count} item(s) in {chats} chat(s); {len(failed)} failed update(s).")


@admin.command("shutdown", filters=(f.user(ADMIN_ID),))
async def shutdown(message):
    await message.reply("Stopping.")
    bot.request_stop()


# --- the rest ---


@bot.middleware
async def log_commands(handler, event, data):
    command = data.get("command")
    if command is not None:
        log.info("/%s in chat %s", command.name, event.chat.id)
    return await handler(event, data)


@bot.on_error(BlockedByUserError)
async def blocked(error, update):
    log.info("chat %s blocked the bot", update.chat.id)


@bot.on_startup
async def ready(b: Bot) -> None:
    await b.sync_commands()
    me = await b.get_me()
    mode = "end-to-end encrypted" if me.protected else "readable"
    log.info("@%s is up, private chats are %s", me.username, mode)


@bot.on_shutdown
async def bye(b: Bot) -> None:
    log.info("bye")


bot.include_routers(todo, admin)

if __name__ == "__main__":
    bot.run(configure_logging=False)
