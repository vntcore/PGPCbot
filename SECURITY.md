# Security

## Reporting a vulnerability

Please don't open a public issue. Write to the PGP Chat team through the contacts on
<https://pgpchat.app>. Say which version of PGPCbot you use, how to reproduce the problem, and
what it lets an attacker do. If the problem is about cryptography, attach a test message or a
test key if you can (never real material).

## Supported versions

Security fixes go to the newest 0.1.x. Nothing older gets them.

The minimum versions of the dependencies are also picked for security: `httpx2>=2.13.1`
includes the fixes for CVE-2026-84380 and CVE-2026-84382.

## What PGPCbot protects

### The bot's token

The token is the key to the bot's house: whoever has it can do everything the bot can do.

- it travels only in the `Authorization` header, never in a URL;
- it never shows up in `repr`, in exceptions or in logs: you only see `pgpbot_<id>.****abcd`,
  as in the app's console. A filter on the `pgpcbot.*` loggers also removes anything that looks
  like a token;
- clients and bots refuse to be pickled;
- it is never sent when downloading media: those addresses are fetched without it;
- with `http://` the client refuses to start, except towards `localhost` or with an explicit
  `allow_insecure_http=True`.

If the token leaks (a published log, a commit, a screenshot): `await bot.revoke_token()` or
`POST /token/revoke` burns it at once and tells the owner, who can make a new one from the
console.

### The private key and the passphrase

- made with settings the phones can use (v4, Ed25519/Curve25519, SEIPDv1 only), without an
  expiry date (details in the Cryptography page, `docs/CRYPTOGRAPHY.md`);
- written to disk only when you ask, with `save()`, which always makes you say whether to use a
  passphrase. On Linux and macOS the file is created with mode `0600`, and loading a key file
  that others can read logs a warning;
- unlocked once, at start-up. It cannot be pickled and does not show up in `repr` or in logs;
- never sent to the server: `PUT /key` gets only the public key, and the client refuses a
  private key block passed by mistake.

### Messages

- in an encrypted chat the text never leaves the process in clear: if encryption fails, the
  message is not sent. There is no fallback to plain text, not even when the chat changes mode
  half way;
- the members' keys are checked (fingerprint, a valid encryption subkey, version) before
  sealing;
- incoming signatures are checked and the result is in `message.verification`. With
  `verify_signatures="require"`, messages that are unsigned or badly signed do not reach the
  handlers;
- members' keys that are revoked or expired are not used, and a message that opens to more than
  1 MiB, or an attachment that is compressed, is refused before it can fill the memory.

### The webhook

- a secret is required by default, compared in constant time, and checked before the body is read;
- `401` with no explanation for whoever doesn't present it, `405` for methods other than POST,
  `413` above 1 MiB, `408` for clients that send the body too slowly;
- `200` only once the update is saved (or handled, without durable storage): a crash does not
  lose it, the server delivers it again;
- deliveries that come twice are recognised by their `updateId` and handled once.

### Storage

- `SQLiteStorage` writes with `synchronous=FULL`: an update confirmed to the server is on disk;
- only one process at a time handles a bot's updates (an operating system lock);
- updates from encrypted chats stay encrypted on disk, and nothing decrypted is written: when a
  handler fails on one of them, only the error's type and place are kept, not its message.
  **Updates from readable chats are plain text**: treat the file like the messages it holds.

## What PGPCbot cannot protect

- **The server sees the metadata**: who writes to the bot, when, in which chats, how long the
  messages are, who presses which buttons.
- **Buttons are not encrypted**, in any mode: the button text and its `callback_data` travel in
  clear, because the server must know which one was pressed. Don't put personal data there. The
  same goes for the command menu and for button answers (`answer_callback`).
- **The server hands out the keys.** A compromised server can give the bot a fake key instead
  of a person's real one. Pinning keys the first time they are seen (`pin_keys=True`) notices
  when a key changes, not a key that was fake from the start. For the contacts that matter,
  compare fingerprints over another channel.
- **A valid signature** says who signed, not who the message was meant for, and not that it is
  not a replay of an old one.
- **The process memory** holds the unlocked key and the decrypted messages: Python cannot wipe
  them reliably. Whoever can read the process memory (core dumps, a debugger, another user with
  enough rights) can read everything.
- **Your code**: what your handlers do with the decrypted text (logs, databases, third-party
  APIs) is outside the library's control.

## Advice for production

- read the token and the passphrase from a mounted file (`PGPCHAT_BOT_TOKEN_FILE`,
  `PGPCHAT_BOT_KEY_PASSPHRASE_FILE`) or from a secrets manager, not from environment variables,
  which child processes inherit and dumps include;
- run the bot as a dedicated system user, with a data folder only that user can read;
- keep `SQLiteStorage` on a local disk (not on a network share);
- use `require_encryption=True` if the bot must never talk in clear;
- use `pin_keys=True` with durable storage if the bot handles sensitive information;
- log at `INFO` level in production: at `DEBUG` the HTTP libraries may log request details
  (never the token, which is in a header, but URLs and timings yes);
- keep PGPCbot and its dependencies up to date: `pip install -U PGPCbot`.

## What has been checked

Nobody has audited this library independently. Automated tests check the behaviour described
on this page and the interoperability with PGPainless 1.7.6 (the phones' library) using real
keys and messages. The library was also run against the real PGP Chat server. During the
research the encryption library was also tried against OpenPGP.js 6.3.2. The design choices
and their limits are in the research notes, `docs/RESEARCH.md`.
