# Commands and buttons

## Commands

A command is a message that starts with `/` and a name: `/start`, `/weather rome`.

```python
@bot.command("weather", description="Forecast for a town")
async def weather(message, command):
    town = command.args or "Rome"           # what follows the command
    await message.reply(f"{town}: sun.")
```

- A name has 1 to 32 lowercase letters, digits and `_`. Matching ignores case: `/Weather`
  works too.
- One handler can take several names: `@bot.command("help", "start")`.
- `command` is a `ParsedCommand`: `command.name` (`"weather"`), `command.args` (everything after
  the name, as one string), `command.argv` (the same, split on spaces), `command.mention` (the
  bot handle after `@`, if any).

### Commands in groups

In a group, people may write `/start@weatherbot` to choose which bot they mean. The library
compares the handle with the bot's own and **ignores commands meant for another bot**. Pass
`ignore_mention=True` to accept them anyway.

In groups the bot does not see every message. With **privacy mode** on (the default, changed by
the owner in the app), the server gives a bot only:

- commands (`/cmd` reaches every bot in the group, `/cmd@name` only that bot);
- replies to the bot's messages;
- messages that mention the bot's `@handle`.

With privacy mode off, the bot gets everything, and the group is told on its member list.

### The command menu

When someone types `/` in the app, a menu shows the bot's commands. Every command registered
with a `description` can be published with one call:

```python
await bot.sync_commands()                    # the "default" menu
await bot.sync_commands(scope="groups")      # the menu shown in groups
```

Or set a menu by hand:

```python
await bot.set_commands([("start", "Say hello"), ("help", "What I can do")], scope="private")
print(await bot.get_commands())              # every scope with its list
await bot.delete_commands(scope="private")
```

| Scope | Shown |
|---|---|
| `default` | everywhere, unless a narrower scope has a list |
| `private` | in private chats |
| `groups` | in groups |
| `groupAdmins` | to group administrators |
| `chat:<id>` | in one chat |

The app picks the narrowest scope that has commands: in a private chat `private`, then
`default`; in a group `chat:<id>`, then `groupAdmins` (for admins), then `groups`, then
`default`. Up to 100 commands per scope; a description is 1 to 256 characters. The owner can
edit the same lists in the app: whoever writes last wins.

## Buttons

Buttons sit under a message. There are two kinds:

- **callback buttons** send a short piece of data back to the bot when pressed;
- **link buttons** offer to open a web address. The phone shows the address before opening it.

```python
from pgpcbot import Button, InlineKeyboard

keyboard = InlineKeyboard.build(
    [Button.callback("Yes", "vote:yes"), Button.callback("No", "vote:no")],   # first row
    [Button.link("Read the rules", "https://example.org/rules")],             # second row
)
await message.reply("Do you agree?", markup=keyboard)
```

Each argument of `InlineKeyboard.build` is one row. To fill rows automatically, use the
builder:

```python
from pgpcbot import KeyboardBuilder

builder = KeyboardBuilder(per_row=3)
for town in ["Rome", "Milan", "Turin", "Naples", "Bari"]:
    builder.add(Button.callback(town, f"town:{town}"))
builder.row(Button.link("All towns", "https://example.org/towns"))   # a row of its own
await message.reply("Pick a town", markup=builder.build())
```

You can also pass the JSON form the API uses:
`markup={"inlineKeyboard": [[{"text": "Yes", "callbackData": "yes"}]]}`.

### The rules

The library checks the same rules as the server before sending, and raises `ValidationError`
if one is broken:

| Rule | Limit |
|---|---|
| rows | up to 20 |
| buttons in a row | up to 8 |
| buttons in all | up to 100 |
| button text | 1 to 64 characters (UTF-16 units), no control characters |
| each button | exactly one of `callback_data` and `url` |
| `callback_data` | up to 64 bytes in UTF-8, and different on every button of the message |
| `url` | `http` or `https`, no user name or password in it, up to 1024 characters |
| the whole keyboard | up to 8 KiB as JSON |

Empty rows are dropped. A keyboard with no buttons is the same as no keyboard.

:::{admonition} Buttons are never encrypted
:class: warning

The text of the buttons and their `callback_data` travel in clear, even in an encrypted
chat: the server has to know which button was pressed. Don't put personal data in them.
Use an ID that means something only to your bot (`order:8812`), not the content
(`order:Anna Rossi, 3 pizzas`).
:::

## Button presses

When someone presses a callback button, the bot gets a callback query:

```python
@bot.on_callback_query(prefix="vote:")
async def vote(query):
    choice = query.data.removeprefix("vote:")
    await save_vote(query.from_user.id, choice)
    await query.answer(f"You voted {choice}")
```

| Field | Meaning |
|---|---|
| `query.id` | the ID you answer with |
| `query.data` | the `callback_data` of the pressed button |
| `query.from_user` | who pressed |
| `query.chat` | the chat of the message |
| `query.message_id` | the message the button is under |
| `query.expires_at`, `query.expired` | the deadline to answer (60 seconds) |

`on_callback_query` takes `data="exact"`, `prefix="start:"` or `pattern=r"regex"` to choose
the presses it handles. With `pattern`, the handler can ask for `match`.

### Answering

The pressed button spins on the person's phone until the bot answers. Answer within 60
seconds:

```python
await query.answer()                                  # just stop the spinner
await query.answer("Saved")                           # a short line over the chat
await query.answer("Out of stock", alert=True)        # a box they have to close
await query.answer(url="https://example.org/cart")    # offer to open a page
```

The text can be up to 200 characters. Each press can be answered once.

If your handler does not answer, the library answers with nothing after the handler ends, so
the button stops spinning. Turn this off with `Bot(auto_answer_callbacks=False)`. An answer that
comes too late raises `CallbackExpiredError`.

### Changing the message

Answering does not change the message. To change it:

```python
await query.edit_message("Thanks for voting!", markup=None)        # new text, no buttons
await query.edit_message(markup=new_keyboard)                      # same text, new buttons
await query.send("Here is your receipt")                           # a new message
await query.delete_message()
```

A common pattern is a menu that changes in place:

```python
@bot.on_callback_query(data="menu:settings")
async def settings(query):
    await query.answer()
    await query.edit_message("Settings", markup=InlineKeyboard.build(
        [Button.callback("Language", "menu:lang"), Button.callback("Back", "menu:main")],
    ))
```

In an encrypted chat a new text is encrypted like any other message. The buttons are not (see
the warning above).
