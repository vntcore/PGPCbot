# Events, filters and middleware

A bot reacts to **updates**: things that happened, sent by the server. You register
**handlers** for the updates you care about, and **filters** decide which handler gets which
update.

## Kinds of updates and their handlers

| Decorator | The update | The handler gets |
|---|---|---|
| `@bot.on_message()` | a new message | `Message` |
| `@bot.command("name")` | a new message that is the command `/name` | `Message` |
| `@bot.on_edited_message()` | someone edited a message the bot had received | `Message` |
| `@bot.on_callback_query()` | someone pressed a button with callback data | `CallbackQuery` |
| `@bot.on_deleted_messages()` | messages were deleted for everyone | `DeletedMessages` |
| `@bot.on_chat_member()` | the bot's own place in a chat changed: added, promoted, removed, blocked | `ChatMemberUpdate` |
| `@bot.on_update()` | any update at all | `Update` |

```python
@bot.on_chat_member()
async def membership(change):
    if change.joined:
        await bot.send_message(change.chat, "Thanks for adding me!")
    elif change.blocked:
        print(f"user {change.chat.id} blocked the bot")
```

## Which handler runs

1. The handlers are tried **in the order you registered them**.
2. The **first one whose filters all match** runs. The others are not tried.
3. If no handler matches, nothing happens (the update is still confirmed as handled).

So put specific handlers before general ones:

```python
@bot.command("help")
async def help_(message): ...         # tried first


@bot.on_message()
async def anything_else(message): ... # gets what /help did not take
```

### Priority

`priority` changes the order inside one router: a higher number is tried first. Handlers with
the same priority keep their registration order.

```python
@bot.on_message(priority=10)
async def spam_guard(message):
    if looks_like_spam(message.text):
        await message.delete()
        return
    raise SkipHandler            # not spam: let the next handler have it
```

### Passing an update on

Raise `SkipHandler` inside a handler to say "not mine after all": the next matching handler
is tried, as if this one had not matched.

```python
from pgpcbot import SkipHandler
```

## Filters

Filters go in the decorator. All of them must match.

```python
from pgpcbot import filters as f


@bot.on_message(f.private, f.has_text)
async def private_text(message): ...


@bot.on_message(private=True)          # the same as f.private, as a keyword
async def private_only(message): ...
```

### Built-in filters

| Filter | Matches |
|---|---|
| `f.private`, `f.group`, `f.channel` | the kind of chat |
| `f.has_text` | the message has text |
| `f.has_media` | the message has pictures, clips or files |
| `f.encrypted` | the message came end-to-end encrypted |
| `f.signed` | encrypted, with a valid signature from the sender's key |
| `f.reply` | the message answers another one |
| `f.mentioned` | the text mentions the bot's `@handle` |
| `f.text("yes", "no", ignore_case=True)` | the whole text is one of these (spaces at the ends are ignored) |
| `f.regex(r"^order (\d+)$")` | the text (or a button's data) matches; the handler can ask for `match` |
| `f.command("start", "help")` | a command; the handler can ask for `command`. With no names, any command |
| `f.user(42, "@anna")` | from one of these people, by ID or handle |
| `f.chat(7781, "@news")` | in one of these chats, by ID or public handle |
| `f.callback_data("yes", "no", prefix="vote:")` | a button press with this data |

The keyword shortcuts `private=True`, `group=True` and `channel=True` work on `on_message` and
`on_edited_message`. `private=False` means "anything but private chats".

### Combining filters

Use `&` (and), `|` (or) and `~` (not):

```python
@bot.on_message(f.group & (f.mentioned | f.reply) & ~f.has_media)
async def addressed_in_group(message): ...
```

### Your own filters

Any function that takes the event and returns `True` or `False` is a filter. It can be
`async`:

```python
def long_text(message):
    return len(message.text or "") > 500


async def is_subscriber(message):
    return await db.is_subscriber(message.from_user.id)


@bot.on_message(long_text, is_subscriber)
async def summarize(message): ...
```

A filter can also return a `dict` instead of `True`: the dict's items are passed to the
handler as keyword arguments. This is how `f.regex` gives you `match`:

```python
def order_number(message):
    m = re.fullmatch(r"#(\d+)", message.text or "")
    return {"order_id": int(m[1])} if m else False


@bot.on_message(order_number)
async def show_order(message, order_id):
    await message.reply(f"Order {order_id}: shipped")
```

In a `SyncBot`, filters must be plain functions, not `async`.

## What a handler can ask for

The first parameter of a handler is always the event. After it, a handler can ask for extra
values **by name**:

| Name | Value |
|---|---|
| `bot` | the bot |
| `update` | the whole `Update` |
| `command` | a `ParsedCommand`, for commands: `name`, `args`, `argv`, `mention` |
| `match` | the `re.Match`, for `f.regex` |
| anything a filter or a middleware added | its value |

```python
@bot.command("remind")
async def remind(message, command, bot):
    minutes, _, what = command.args.partition(" ")
    ...
```

A handler with `**kwargs` gets all of them.

## IDs: who wrote, and where

Every event carries the two IDs a bot works with, so most of the time there is nothing to look
up:

```python
@bot.on_message()
async def seen(message):
    print(message.chat.id, message.chat.kind)       # the chat: private, group or channel
    if message.from_user:                           # who wrote it
        print(message.from_user.id, message.from_user.username)
```

A message in a group gives you the group's ID and the writer's ID together; a channel post
gives you the channel's ID. `callback_query.chat` / `.from_user` and
`chat_member.chat` / `.from_user` (whoever added or promoted the bot) are the same pair.

`from_user` is `None` for one kind of message: a channel post that speaks in the channel's own
name. Nobody is told who wrote that - a subscriber's app does not know either - and a signed
post carries `message.signature`, the name readers see, instead.

For an ID no event handed over, because somebody typed a handle at the bot, there is
`bot.resolve`:

```python
@bot.command("paga")
async def pay(message, command):
    found = await bot.resolve(command.args)          # "@anna", "anna", "ANNA" all work
    if not found.is_user:
        await message.reply("quello e un gruppo, non una persona")
        return
    await message.reply(f"{found.mention} ha id {found.id}")
```

It raises `UsernameNotFoundError` when nothing answers to the handle, and `ValidationError`
before sending anything if the handle cannot be one. People, groups and channels share one
pool of names, so check `found.kind` (or `is_user` / `is_chat`) before you use the ID: a
`Resolved` for a chat carries a `ChatCard` you can pass straight to `bot.get_chat` once an
administrator has added the bot.

Knowing an ID is not permission to use it. The bot can only write in chats it is in, so
`send_message` to a resolved group raises `ChatNotFoundError` until somebody adds the bot, and
`get_user_key` wants a chat in common. The lookup also has a window of its own, 60 a minute,
where a handle nobody holds counts as five: resolve what people ask about, not a dictionary.

## Routers: splitting a bot into modules

A `Router` holds handlers like a bot does. Include it into the bot:

```python
# admin.py
from pgpcbot import Router, filters as f

router = Router(name="admin")


@router.command("stats", filters=(f.user(*ADMINS),))
async def stats(message): ...
```

```python
# main.py
from pgpcbot import Bot
import admin, shop

bot = Bot()
bot.include_routers(admin.router, shop.router)
bot.run()
```

The bot's own handlers are tried first, then each included router in the order you included
it, then the routers included into those, and so on. A router can be included only once.

## Middleware

A middleware runs around every handler of its router (and of the routers below it). It can
look at the event, add data for the handler, time it, or stop it.

```python
import time


@bot.middleware
async def timing(handler, event, data):
    start = time.perf_counter()
    try:
        return await handler(event, data)
    finally:
        log.info("handled in %.0f ms", (time.perf_counter() - start) * 1000)


@bot.middleware
async def load_user(handler, event, data):
    data["profile"] = await db.load(event.from_user.id) if event.from_user else None
    return await handler(event, data)


@bot.on_message()
async def hello(message, profile):         # gets what load_user added
    ...
```

To stop an update, don't call `handler`. Middlewares run from the outside in: the bot's own
first, then the router's, in the order they were added. A middleware runs only when a
handler's filters matched.

In a `SyncBot` a middleware is a plain function that returns `handler(event, data)`.

## Errors

When a handler raises, the error goes to the **error handlers**:

```python
@bot.on_error(RateLimitError)
async def slow_down(error, update):
    log.warning("rate limited, update %s will be retried later", update.update_id)
    raise error             # not handled: the update goes to the failed list


@bot.on_error()             # no types: any Exception
async def everything_else(error, update, bot):
    log.exception("update %s failed", update.update_id, exc_info=error)
```

- Error handlers are tried in order (the bot's own first, then the routers'), and the first
  whose type matches runs. Raise `SkipHandler` to try the next one.
- An error handler that returns normally means "handled": the update is done.
- An error that no error handler takes (or that an error handler raises again) marks the update
  as **failed**. It is kept with the error, and you can retry it later
  ([Storage](storage.md#failed-updates)). The bot goes on with the next updates.
- Errors that happen while opening an encrypted message (`DecryptionError`, `SignatureError`)
  go to the error handlers too, before any handler runs.

## Startup and shutdown hooks

```python
@bot.on_startup
async def ready(bot):
    await bot.sync_commands()          # the bot is connected: API calls work here
    await database.connect()


@bot.on_shutdown
async def closing(bot):
    await database.disconnect()        # after the last handler, before the connections close
```

A startup hook runs once the bot is connected and before the first update. If it raises, the
bot does not start. A shutdown hook runs after the last handler finished; its errors are logged
and the next hook still runs. On a `SyncBot` hooks are plain functions.

## Ordering and concurrency

- Updates of **one chat** are handled one at a time, in the order the server sent them.
- **Different chats** are handled at the same time, up to `concurrency` (8 by default).
- A slow handler holds up its own chat only. For long jobs (minutes), start a background task
  and answer when it is done, instead of keeping the handler busy.
