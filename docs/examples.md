# Examples

The scripts in the `examples/` folder, smallest bot first. Each starts with a comment that
says how to run it, and they read the token from `PGPCHAT_BOT_TOKEN` (and the server from
`PGPCHAT_API`): none has a secret written in it. All of them were run against a real server,
with a person writing to the bot from the app.

| File | What it shows |
|---|---|
| `01_hello_world.py` | the smallest bot: one handler, `run()` |
| `02_echo_bot.py` | text, media and messages without text; edited messages |
| `03_commands.py` | commands with arguments, the command menu published at start-up, a catch-all for unknown commands |
| `04_private_messages.py` | a bot that works in private chats only, and says so in groups |
| `05_group_bot.py` | being added to a group, mentions, the member list, an admin-only command, deleted messages |
| `06_inline_buttons.py` | a counter with buttons, links, `KeyboardBuilder` |
| `07_callbacks.py` | a poll and a menu that changes in place; regex patterns on button data; late answers |
| `08_long_polling.py` | polling by hand with `AsyncClient`, confirming each update after handling it |
| `09_fastapi_webhook.py` | a webhook inside FastAPI, with `SQLiteStorage` and a health route |
| `10_encrypted_bot.py` | an end-to-end encrypted bot: publishing the key, signatures required |
| `11_generate_pgp_keys.py` | making and saving the bot's key with a passphrase |
| `12_send_encrypted_message.py` | one message from a script, encrypted when the chat is; the same by hand |
| `13_custom_handlers.py` | your own filters, a filter that hands values to the handler, `SkipHandler`, middleware, routers |
| `14_error_handling.py` | error handlers by type, `ValidationError`, the failed list and `/retry` |
| `15_rate_limiting.py` | a broadcast to many chats in the background, people who blocked the bot, a custom `RetryPolicy` |
| `16_async_bot.py` | the bot inside your own asyncio program, next to a periodic task |
| `17_sync_client.py` | no asyncio: the blocking `Client` in a cron-style script, and a `SyncBot` |
| `18_persistent_updates.py` | `SQLiteStorage`: an update interrupted by a crash runs again after the restart |
| `19_advanced_configuration.py` | most options, with a word on each, and secrets from files |
| `20_complete_bot.py` | a to-do list per chat: routers, buttons, middleware, admin commands, hooks, a clean `/shutdown` |

## Running them

```bash
pip install PGPCbot fastapi uvicorn     # fastapi and uvicorn only for 09
export PGPCHAT_BOT_TOKEN="pgpbot_..."
python examples/01_hello_world.py
```

Then write to your bot from the app. Ctrl+C stops it.

A few need more:

- `10_encrypted_bot.py` needs a key: run `11_generate_pgp_keys.py` first. Publishing the key is
  one-way: try it on a test bot.
- `09_fastapi_webhook.py` needs a public HTTPS address that reaches the app, and a secret.
- `14`, `15` and `20` have admin commands: set `ADMIN_ID` to your own user ID.
