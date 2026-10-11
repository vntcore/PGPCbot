# API coverage

Bot API 1.0.0 has 22 operations and one outgoing webhook, and every one of them has a method
here, on both clients and both bots. The table says which, and where the SDK's own rules differ
from the document.

- **SDK method**: the same name on `AsyncClient`, `Client`, `Bot` and `SyncBot`. The bots add
  encryption, caches and chat objects on top (see [Bot](api/bot.rst)).
- `tests/test_parity.py::test_every_operation_of_the_spec_is_a_method_of_both_clients` fails if
  an operation of the spec has no method, and the request shape of every operation is checked
  against `openapi.json` itself. That test, not this page, is what keeps the list honest.

| Endpoint | HTTP method | operationId | SDK method | Models | Documentation |
|---|---|---|---|---|---|
| `/me` | GET | `getMe` | `get_me` | `BotInfo`, `Limits`, `WebhookSummary` | {py:meth}`client <pgpcbot.AsyncClient.get_me>` |
| `/updates` | GET | `getUpdates` | `get_updates`, `iter_updates`; `Bot.run`, `start_polling` | `Update` and the event models | {py:meth}`client <pgpcbot.AsyncClient.get_updates>`, [long polling](guides/long-polling.md) |
| `/webhook` | GET | `getWebhook` | `get_webhook` | `WebhookInfo` | {py:meth}`client <pgpcbot.AsyncClient.get_webhook>` |
| `/webhook` | POST | `setWebhook` | `set_webhook`; `create_webhook_app(url=...)` | `WebhookInfo` | {py:meth}`client <pgpcbot.AsyncClient.set_webhook>`, [webhooks](guides/webhook.md) |
| `/webhook` | DELETE | `deleteWebhook` | `delete_webhook`; `run(drop_webhook=True)` | `WebhookInfo` | {py:meth}`client <pgpcbot.AsyncClient.delete_webhook>` |
| `/key` | PUT | `setKey` | `set_key`; `Bot.publish_key`, `rotate_key`, `publish_key=True` | `PublishedKey` | {py:meth}`client <pgpcbot.AsyncClient.set_key>`, [OpenPGP](guides/openpgp.md) |
| `/commands` | GET | `getCommands` | `get_commands` | `CommandScope`, `Command` | {py:meth}`client <pgpcbot.AsyncClient.get_commands>` |
| `/commands` | PUT | `setCommands` | `set_commands`; `Bot.sync_commands` | `CommandScope` | {py:meth}`client <pgpcbot.AsyncClient.set_commands>`, [commands](guides/commands-and-buttons.md) |
| `/commands` | DELETE | `deleteCommands` | `delete_commands` | `CommandScope` | {py:meth}`client <pgpcbot.AsyncClient.delete_commands>` |
| `/messages` | POST | `sendMessage` | `send_message`; `Message.reply`, `answer`, `CallbackQuery.send` | `SentMessageBase`, `InlineKeyboard` | {py:meth}`client <pgpcbot.AsyncClient.send_message>`, [messages](guides/messages.md) |
| `/messages/{messageId}` | PATCH | `editMessage` | `edit_message`; `SentMessage.edit`, `CallbackQuery.edit_message` | `SentMessageBase` | {py:meth}`client <pgpcbot.AsyncClient.edit_message>` |
| `/messages/{messageId}` | DELETE | `deleteMessage` | `delete_message`; `Message.delete`, `SentMessage.delete` | `bool` | {py:meth}`client <pgpcbot.AsyncClient.delete_message>` |
| `/callbacks/{callbackId}/answer` | POST | `answerCallback` | `answer_callback`; `CallbackQuery.answer`, automatic answers | `bool` | {py:meth}`client <pgpcbot.AsyncClient.answer_callback>`, [buttons](guides/commands-and-buttons.md#button-presses) |
| `/chats/{chatId}` | GET | `getChat` | `get_chat` | `Chat`, `ChatPeer`, `Seat`, `AdminRights` | {py:meth}`client <pgpcbot.AsyncClient.get_chat>` |
| `/chats/{chatId}/keys` | GET | `getChatKeys` | `get_chat_keys`; `Bot.recipients`, `seal` | `ChatKeys`, `UserKey` | {py:meth}`client <pgpcbot.AsyncClient.get_chat_keys>` |
| `/chats/{chatId}/members` | GET | `getChatMembers` | `get_chat_members` | `ChatMembers`, `Member` | {py:meth}`client <pgpcbot.AsyncClient.get_chat_members>` |
| `/chats/{chatId}/members/{userId}` | GET | `getChatMember` | `get_chat_member` | `Member` | {py:meth}`client <pgpcbot.AsyncClient.get_chat_member>` |
| `/chats/{chatId}/leave` | POST | `leaveChat` | `leave_chat` | `bool` | {py:meth}`client <pgpcbot.AsyncClient.leave_chat>` |
| `/chats/{chatId}/typing` | POST | `sendTyping` | `send_typing`; `Message.typing` | `bool` | {py:meth}`client <pgpcbot.AsyncClient.send_typing>` |
| `/resolve/{username}` | GET | `resolve` | `resolve` | `Resolved`, `HandleKind`, `ChatCard`, `ChatPeer` | {py:meth}`client <pgpcbot.AsyncClient.resolve>`, {py:meth}`bot <pgpcbot.Bot.resolve>` |
| `/users/{userId}/key` | GET | `getUserKey` | `get_user_key` | `UserKey` | {py:meth}`client <pgpcbot.AsyncClient.get_user_key>` |
| `/token/revoke` | POST | `revokeToken` | `revoke_token` | `bool` | {py:meth}`client <pgpcbot.AsyncClient.revoke_token>` |
| webhook `update` (server → bot) | POST | — | `create_webhook_app`, `WebhookApp.handle` | `Update` | [webhooks](guides/webhook.md), [reference](api/webhook.rst) |
| `/media/{id}` (not in the spec) | GET | — | `download_media` | `Media` | {py:meth}`client <pgpcbot.AsyncClient.download_media>` |

The media download is not an operation of the Bot API document: the server gives `media[].url`
in messages and serves `GET /media/{id}` without authentication. It is listed because bots need
it.

## Error codes

All 26 codes of the spec's `ErrorCode` enum have their own exception class
([Exceptions](api/errors.rst)). `tests/test_errors.py` checks the table against
`openapi.json` both ways, so a code added to the spec fails the tests until it is mapped.

## Where the spec and the server differ

PGPCbot follows what the server actually does where the document is silent or different.
The list below was checked against a running server in October 2026:

| Topic | The spec says | The server does | What PGPCbot does |
|---|---|---|---|
| Text length | `maxLength: 4096` | counts UTF-16 code units, the way JavaScript's `.length` does | counts UTF-16 units |
| Control characters | not stated | refuses all but `\n` and `\t`, `\r` included | turns `\r\n` and `\r` into `\n`, refuses the rest |
| Empty text | `minLength: 1` | trims with JavaScript's `trim()`, which also drops U+FEFF | the same set of spaces |
| `PUT /key` fingerprint | must be the key's | takes it from the request body | the SDK computes it from the key itself, so the two always match |
| `rate_limited` | wait `retryAfter` seconds | the body always says 60; the `Retry-After` header has the real time | trusts the header |
| `GET /me` limits | an example with a few names | more names (`markupBytes`, `commandsPerScope`, `botsPerChat`, ...) | types the known ones, keeps all in `raw` |
| Readable text | the text | trimmed, runs of empty lines folded, cut at 4000 characters | documented; encrypted text comes exact |
| `media[].duration` | no unit | milliseconds | `duration_ms` |
| Readable chats always use `text` | yes | a body that merely contains `-----BEGIN PGP MESSAGE-----` is delivered as `armored`, even in readable chats, and in readable groups it reaches bots named by the sender's phone or with privacy mode off | an `armored` body it can't open in a readable chat reaches the handlers as text |
| Second press on a message | "the same press" | a press on a *different* button of the same message is a new callback: a press is identified by user, message and data | nothing to change |
| `DELETE /messages/{id}` | `deleted` is always true | `{deleted: false}` when the message is gone between the lookup and the delete | returns what the server says |
| `GET /chats/{id}` private `user` | `UserRef` (with `isBot`) plus `deleted`, `hasKey` | no `isBot` | `ChatPeer.is_bot` defaults to false |
| `POST /webhook` | https and a public address only | a server set up for local development can also accept http and private addresses | leaves the scheme to the server (the integration tests rely on it) |
| Command description | `maxLength: 256` | accepts up to 1000 raw characters, and applies 256 after folding whitespace | folds whitespace the same way, then checks 256 |
| `GET /media/{id}` | not in the spec | public, no token | downloads without the token |
| Attachments in encrypted groups | not stated | the phone seals them to the group's key list only, not to the bots it addresses | documented; `NotARecipientError` explains it |
