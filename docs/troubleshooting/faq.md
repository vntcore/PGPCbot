# Questions and answers

## General

**Is this the official library?**
It is a library for the PGP Chat Bot API, written against the server's own OpenAPI document and
code. It is not part of the server.

**Which Python versions work?**
3.11, 3.12, 3.13 and 3.14, on Linux, macOS and Windows.

**Do I need GnuPG installed?**
No. The OpenPGP engines (Sequoia PGP and rPGP) come inside the Python wheels.

**Async or sync?**
Both, with the same features: `Bot` and `SyncBot`, `AsyncClient` and `Client`. See
[Async and sync](../guides/async-and-sync.md).

**Can I use it from Django, Flask or a script?**
Yes. For one-off sends use `Client`. For a bot inside a web app, use the webhook; with a
non-ASGI framework, `WebhookApp.handle()` does the work.

## Encryption

**Do I have to use encryption?**
No. A bot without a published key works in readable chats. Groups with visible history are
readable anyway.

**If I publish a key, can I go back?**
Not through the API. Only the bot's owner can switch it back, from the console in the app.

**Can the server read what my bot sends in an encrypted chat?**
No, as long as the keys it hands out are the real ones. It sees the metadata (who, when, how
long), and the buttons, which are never encrypted. See [Trusting keys](../CRYPTOGRAPHY.md#7-trusting-keys).

**Why can't my bot read attachments in an encrypted group?**
Phones seal attachments only to the group's key list, which includes the bot only when its
privacy mode is off.

**Is the cryptography audited?**
The library itself has not been audited. It uses two OpenPGP implementations (Sequoia PGP and
rPGP) instead of writing any cryptography, and it is tested against the phones' library with
real keys and messages. See [Cryptography](../CRYPTOGRAPHY.md).

**Can a message ever go out in clear by mistake?**
The library never falls back to plain text when encryption fails, and never resends an
encrypted message in clear. With `require_encryption=True` it refuses to send anything in clear
at all.

**What does a valid signature prove?**
That the message was made with the key the server lists for the sender. Not that the key
belongs to that person, and not that the message is new. See [section 4.2](../CRYPTOGRAPHY.md#42-what-a-valid-signature-proves).

## Delivery

**Can an update be handled twice?**
Yes, rarely: delivery is "at least once". If the process dies after a handler ran but before
the server got the confirmation, the update comes again. The library drops repeats it can see;
make important actions idempotent.

**Can an update be lost?**
Not by the library: an update is confirmed only once it is safe (saved or handled). The server
keeps updates for 72 hours, so a bot that is off for longer loses the oldest.

**Can I run two copies of my bot for availability?**
Not polling the same bot: they would starve each other. Run one, and let systemd, Docker or
Kubernetes restart it. A webhook bot can have several workers on one database file.

**How do I send a message to everyone who uses my bot?**
Keep a list of chat IDs in your own database (from the updates you get), and send in a loop.
The rate limiter paces the sends. Remove people who blocked the bot (`BlockedByUserError`).

## Errors

**Do I need to retry failed calls myself?**
Usually not: network errors, `5xx` and `429` are retried for you when it is safe. See
[Errors](../guides/errors.md#what-the-library-retries-for-you).

**My handler raised an exception. Is the update lost?**
No. It is in the failed list: `await bot.failed_updates()`, then `retry_failed()` after you fix
the bug.

**Where do I see why something failed?**
In the logs (`pgpcbot.*` loggers), in `bot.failed_updates()`, and for a webhook in
`pgpcbot webhook info`.
