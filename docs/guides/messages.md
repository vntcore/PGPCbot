# Messages

This guide shows how a bot reads the messages it gets, and how it sends, edits and deletes its
own.

## What a message looks like

A handler for messages gets a `Message`:

```python
@bot.on_message()
async def show(message):
    print(message.id)                    # 1834, unique inside the chat
    print(message.chat.id)               # 7781
    print(message.chat.kind)             # ChatKind.PRIVATE, GROUP or CHANNEL
    print(message.from_user.username)    # "anna", or None if she has no handle
    print(message.text)                  # what she wrote, or None
    print(message.date)                  # a datetime in UTC
```

| Field | Type | Meaning |
|---|---|---|
| `id` | `int` | the message ID, unique inside its chat |
| `chat` | `ChatRef` | `id`, `kind`, and `title` / `username` for groups and channels |
| `from_user` | `User` or `None` | who wrote it. `None` for a channel post written in the channel's name |
| `date` | `datetime` | when it was sent, in UTC |
| `reply_to` | `int` or `None` | the ID of the message it answers |
| `text` | `str` or `None` | the text, already decrypted in an encrypted chat |
| `media` | tuple of `Media` | pictures, clips and files attached |
| `signature` | `str` or `None` | for a signed channel post with no `from_user`: the name it shows |
| `encrypted` | `bool` | `True` if it came end-to-end encrypted |
| `armored` | `str` or `None` | the original OpenPGP message, in an encrypted chat |
| `plaintext` | `str` or `None` | the exact decrypted body (the app's internal format), in an encrypted chat |
| `verification` | `Verification` or `None` | the result of the signature check, in an encrypted chat |
| `raw` | `dict` | the JSON the server sent, untouched |

Some fields are shortcuts: `message.chat_id` is `message.chat.id`, and `message.is_command`
is true when the text starts with `/`.

### Text in readable chats

In a readable chat the server hands the text over slightly cleaned: spaces at the start and at
the end are removed, runs of empty lines become one, and the text is cut at 4000 characters.
In an encrypted chat you get the exact text the person wrote.

### Stickers, calls, payment requests

For a sticker, a call notice or a payment request, `message.text` is `None`. In an encrypted
chat, `message.plaintext` still holds the app's internal format if you need it.

### Media

`message.media` lists what came with the message:

```python
from pathlib import Path
from pgpcbot import filters as f


@bot.on_message(f.has_media)
async def save_photo(message):
    for item in message.media:
        print(item.kind, item.mime, item.width, item.height, item.duration_ms)
        data = await bot.download_media(item)       # bytes, decrypted if needed
        Path(f"{item.id}.bin").write_bytes(data)
```

`download_media` refuses files bigger than `max_bytes` (64 MiB by default). In an encrypted
chat the file is decrypted for you. In an encrypted **group**, phones seal attachments only to
the chat's members, so a bot in privacy mode can read the text but not the attachments: the
download then fails with `NotARecipientError`.

## Sending

### Replying

```python
await message.reply("Got it")                    # quotes the message
await message.reply("Got it", quote=False)       # same chat, no quote
await message.answer("Got it")                   # the same as quote=False
```

### Sending into any chat

```python
await bot.send_message(7781, "Last night's backup finished fine.")
await bot.send_message(message.chat, "Hello")    # a chat object works too
await bot.send_message(7781, "Done", reply_to=1834)
```

The bot can only write into chats it is in. In a private chat, the person must have started
the chat with the bot first.

### Encryption is automatic

You never choose between "text" and "encrypted" yourself. Before sending, the library checks
the chat's mode:

- **readable chat**: the text is sent as it is;
- **encrypted chat**: the text is encrypted for every member and for the bot, signed with the
  bot's key, and sent as an OpenPGP message.

If the chat is encrypted and the message cannot be encrypted (no keys, no private key, a bad
key in a private chat), nothing is sent and you get an `EncryptionError`. The library never
falls back to plain text. The [OpenPGP guide](openpgp.md) explains the details.

You can state what you expect for one message:

```python
await message.reply("Your code is 4471", encrypt=True)    # EncryptionRequiredError in a readable chat
await message.reply("Public notice", encrypt=False)       # ValidationError in an encrypted chat
```

### Limits

| What | Limit |
|---|---|
| text | 1 to 4096 characters, counted in UTF-16 units like JavaScript does: most emoji count 2 |
| characters | no control characters except new lines and tabs. `\r\n` is turned into `\n` for you |
| encrypted message | 131072 characters of armored text |

The library checks these before sending and raises `ValidationError` with the field name.
The numbers come from the server (`GET /me`), so they follow the server you are on.

### Silent posts

```python
await bot.send_message(channel_id, "Maintenance tonight", silent=True)
```

`silent` only matters in channels: the post goes out without a notification.

### Retries never write a message twice

Every send gets a `sendId`, a UUID made once and reused for every attempt. If an answer gets
lost on the network and the library tries again, the server recognises the `sendId` and gives
back the message it already wrote. You can pass your own:

```python
import uuid

send_id = uuid.uuid4()          # save it with your job, before sending
await bot.send_message(7781, "Invoice 2026-118 is ready", send_id=send_id)
```

If your own program crashes and runs the job again with the same `send_id`, the person still
gets one message. A `sendId` belongs to one chat: using it in another chat raises
`SendIdConflictError`.

## Showing "typing..."

```python
await message.typing()              # or: await bot.send_typing(chat_id)
result = await slow_search(message.text)
await message.reply(result)
```

The indicator goes away after a few seconds or when the message arrives. The server sends at
most one every 3 seconds per chat; extra calls return `False` and do nothing.

## Editing

`reply` and `send_message` return a `SentMessage`, which you can edit or delete later:

```python
sent = await message.reply("Working on it...")
await asyncio.sleep(2)
await sent.edit("Done: 14 files checked.")
```

Or by ID:

```python
await bot.edit_message(chat_id, message_id, "New text")
```

- The bot can edit only its own ordinary messages, at any age.
- In an encrypted chat the new text is encrypted like a send.
- Leave `markup` out to keep the buttons; `markup=None` removes them.
- Edits have their own rate limits: one per second in a private chat, twenty per minute in a
  group.

## Deleting

```python
await message.delete()                  # a message someone else sent
await sent.delete()                     # one of the bot's own
await bot.delete_message(chat_id, message_id)
```

The bot can delete its own messages anywhere, and other people's messages in a group or
channel where it has the "delete messages" right. A message that is already gone raises
`MessageNotFoundError`. After a timeout the library treats that error as "done", so a retry is
safe.

## Edited and deleted messages

```python
@bot.on_edited_message()
async def edited(message):
    print("now it says:", message.text)


@bot.on_deleted_messages()
async def deleted(event):
    print("gone from", event.chat.id, ":", event.message_ids)
```

## Sending without handlers

For a script that only sends, use the client directly. It does not encrypt anything, so it
fits readable chats:

```python
from pgpcbot import Client

with Client() as api:
    api.send_message(7781, "Backup finished.")
```

For encrypted chats outside a handler, use the bot's methods, which encrypt:

```python
import asyncio
from pgpcbot import Bot

async def main():
    async with Bot(pgp_private_key="bot-key.asc", pgp_passphrase=...) as bot:
        await bot.send_message(7781, "Backup finished.")

asyncio.run(main())
```
