# Cryptography

This page explains what PGPCbot does with OpenPGP, what is still your job, how much you can
trust a key, and which risks remain. Read it before you put an encrypted bot in production.

> **Status.** Nobody has audited PGPCbot's cryptography independently. What has been shown,
> with real keys and real messages, in both directions, is that it works with the phones'
> library (PGPainless 1.7.6). The automated tests cover that and the behaviour described on
> this page. During the research the encryption library was also tried against OpenPGP.js
> 6.3.2. The design choices are explained in [RESEARCH.md](RESEARCH.md).

---

## 1. The two modes

A PGP Chat chat is either **readable** by the server or **end-to-end encrypted**. The server
decides, and tells you in the chat's `mode` field (`open` or `protected`):

| Chat | It is encrypted when |
|---|---|
| private chat with the bot | the bot has published a key (`PUT /key`) |
| group or channel | the history is **not** visible to new members |

In readable chats you send and receive words (`text`): the server reads them and passes them
on. In encrypted chats you send and receive ASCII-armored OpenPGP messages (`armored`): the
server cannot read them.

Publishing the key is **a one-way step** as far as the API goes: only the bot's owner can switch
the bot back to readable chats, from the console in the app. This is why PGPCbot never
publishes anything unless you ask (`publish_key=True` or `bot.publish_key()`).

---

## 2. What PGPCbot does, and what is up to you

PGPCbot does this on its own:

- finds out whether a chat is encrypted before it sends anything;
- fetches the members' keys (`GET /chats/{id}/keys`), checks them, seals the message to all of
  them and to the bot itself, and signs it with the bot's key;
- decrypts incoming messages, checks the sender's signature, reads the app's internal format
  (text, photos, albums, forwards) and gives your handler a clean `message.text`;
- always refuses to send in clear a message that should be encrypted.

This is up to you:

- **keep the bot's private key safe.** It is the bot's identity: whoever has it can read every
  message sent to the bot and sign in its name;
- decide **when** to publish it and **when** to rotate it;
- decide **how much to trust** the keys the server hands out (section 7);
- never write decrypted content where it should not go (logs, plain databases, outside
  services).

---

## 3. The bot's key

### 3.1 Making one

```python
from pgpcbot import BotKey

key = BotKey.generate("Weather", username="weatherbot")
print(key.fingerprint)          # 40 hex digits, upper case
```

Or from the command line, without writing code:

```bash
pgpcbot keygen --name Weather --username weatherbot --out bot-key.asc --passphrase-env BOT_KEY_PASSPHRASE
```

A generated key is always:

- **OpenPGP v4**, with an Ed25519 primary key (certifies and signs) and a Curve25519 subkey
  (encrypts): the same kind of key the phones make and can use;
- marked as able to use **SEIPDv1 only**. Every message the bot sends is also sealed to its own
  key, so the encryption engine has to use the format PGPainless 1.7 can open, even when every
  other key in the chat says it supports the newer one. If a message ever comes out as SEIPDv2
  anyway (a bot key made elsewhere that announces it), it is not sent;
- **without an expiry date.** An expired key could not open even old messages, and phones
  could no longer write to the bot. You change a key by rotating it (3.5).

The key lists these preferences: AES-256 and AES-128 for encryption, SHA-512 and SHA-256 for
hashes, no compression, ZLIB or ZIP.

### 3.2 Protecting and saving it

If you give a passphrase when you make the key, the private parts are protected with the
standard OpenPGP method (iterated and salted S2K with SHA-256, AES-256). Any OpenPGP tool can
open it.

```python
key = BotKey.generate("Weather", username="weatherbot", passphrase=os.environ["BOT_KEY_PASSPHRASE"])
key.save("bot-key.asc", passphrase=None)   # already protected: written as it is
```

A key without a passphrase can be saved inside a password-encrypted OpenPGP message, the same
format as `gpg -c`:

```python
key.save("bot-key.asc", passphrase=os.environ["BOT_KEY_PASSPHRASE"])
```

A key that already has a passphrase can't be sealed again under a different one: the file
would open with one and the key inside with the other, and nothing could load it back. Save it
with `passphrase=None`.

`save()` **always** wants the `passphrase` argument, even when it is `None`: writing a private
key in clear should be a choice, not a default. It writes a new file and moves it into place, so
an old file's permissions are never reused and a symbolic link at that path is replaced, not
followed. On Linux and macOS the file has mode `0600`, and PGPCbot warns when it loads a key
file that other users can read. On Windows the file gets the permissions of its folder: keep it
in a folder that only the service's user can read.

### 3.3 Loading it

```python
bot = Bot(pgp_private_key="bot-key.asc", pgp_passphrase=os.environ["BOT_KEY_PASSPHRASE"])
```

`pgp_private_key` takes a path, the armored text of the key, or a `BotKey`. With no argument,
PGPCbot reads `PGPCHAT_BOT_KEY` (the text or a path) or `PGPCHAT_BOT_KEY_FILE`, and the
passphrase from `PGPCHAT_BOT_KEY_PASSPHRASE` or `PGPCHAT_BOT_KEY_PASSPHRASE_FILE`. The
passphrase can also be a function that returns it (for example, one that asks a secrets
manager).

The key is unlocked **once, when it is loaded**: a wrong passphrase stops the bot at start-up
with `PassphraseError`, not at the first message.

### 3.4 Publishing it

```python
await bot.publish_key()            # PUT /key with the public key and its fingerprint
```

or `Bot(..., publish_key=True)`, which publishes it at start-up only when the server has a
different key or none. PGPCbot computes the fingerprint it sends from the key itself, so the
two always match: a wrong fingerprint would make phones seal messages to a key the bot does
not have. A v6 key is refused before anything is sent.

### 3.5 Rotating it

```python
new_key = BotKey.generate("Weather", username="weatherbot", passphrase=...)
await bot.rotate_key(new_key)
```

From then on, phones seal to the new key. But some messages may already be on their way, or
waiting in the queue, sealed to the old one (the server keeps updates for up to 72 hours). The
running bot keeps the old key to decrypt them. When you restart the bot, pass the old key in
`pgp_previous_keys` for at least 72 hours:

```python
bot = Bot(pgp_private_key="new-key.asc", pgp_previous_keys=[BotKey.from_file("old-key.asc", ...)])
```

### 3.6 If the key is stolen

Rotate it at once (3.5) and tell the people who use the bot. Whoever has the old private key
can read the messages that were sealed to it, and no rotation can stop that. If the token
leaked too, `bot.revoke_token()` burns it and stops the bot until the owner makes a new one.

---

## 4. Incoming messages

When a message with `armored` arrives, PGPCbot does this before any handler sees it:

1. it **decrypts** it with the bot's key (and, if the message was not sealed to that key, with
   the `pgp_previous_keys`);
2. it gets the chat's keys from the server (cached for `cache_ttl` seconds) and **checks the
   signature** against the sender's key;
3. it **reads the app's internal format**: plain text, a photo or video with a caption,
   albums, forwards, link previews. Stickers, calls and payment requests have `text=None`;
4. it fills in the message: `message.text`, `message.media`, `message.plaintext` (the exact
   decrypted body), `message.verification`, and keeps the original in `message.armored`.

A message that cannot be opened does not reach the handlers. The error (`NotARecipientError`,
`DecryptionError`) goes to the error handlers. If none of them handles it, the update ends up
in the list of failed updates, with the reason.

Two exceptions to that:

- In a **readable** chat, the server labels a message `armored` whenever its text contains an
  OpenPGP armor line, for example when someone pastes a PGP block. If the bot can't open it and
  the chat is readable, the handlers get it as plain text (`message.text`, with
  `message.encrypted` false): it is words anyone in the chat can read.
- A message that opens to more than 1 MiB is refused with `DecryptionError`. The app never
  writes that much (4096 characters and small envelopes), and compressed data can be made to
  open to hundreds of megabytes. See the risks table.

### 4.1 The signature status

`message.verification.status` is one of:

| Status | Meaning |
|---|---|
| `valid` | signed with the key the server lists for the sender, and the signature is correct |
| `unknown_key` | signed, but by a key that is not the sender's |
| `invalid` | signed by the sender's key but the signature is wrong, or the two OpenPGP engines do not agree on the content |
| `unsigned` | no signature. Channel posts sent in the channel's name come like this on purpose |
| `not_checked` | checking is off, the sender is unknown (an anonymous post), or the chat's keys could not be fetched; `detail` says why |

With `verify_signatures="require"`, a message that is not `valid` becomes a `SignatureError` and
does not reach the handlers. With `"off"`, the keys are not even fetched. The default, `"auto"`,
checks and reports but does not drop anything: the app itself does not drop unsigned messages.

Two different engines open every message. Sequoia (pysequoia) decrypts and rPGP (rpgp-py)
checks the signature, because pysequoia 0.1.35 cannot check signatures inside the compressed
messages that phones send. If the two decrypted texts are not the same, the status is
`invalid`.

### 4.2 What a valid signature proves

It proves that the message was made by whoever holds the private key that the server lists for
the sender, and that nobody changed it.

It does **not** prove:

- that the key really belongs to that person (the server could hand out a different one:
  section 7);
- that the message was meant for this chat: whoever receives a signed message can seal it again
  to other people, signature included. Neither the app nor PGPCbot uses the "Intended Recipient
  Fingerprint" subpacket from RFC 9580;
- that the message is new: only the de-duplication by `updateId` protects against replays, not
  the cryptography;
- that the server delivered every message.

---

## 5. Outgoing messages

`await message.reply("...")` or `await bot.send_message(chat, "...")`:

1. **The chat's mode.** In a private chat it is the bot's mode (from `GET /me`). In a group,
   PGPCbot takes it from the latest updates or asks `GET /chats/{id}`, and caches it for
   `cache_ttl` seconds.
2. **Recipients.** `GET /chats/{id}/keys` gives the members' keys (without the bot, and without
   other bots in privacy mode). Each key is read and checked: its fingerprint must match the
   one the server gives, it must not be revoked or expired, it must have an encryption subkey
   that is neither, and it must not be v6 (phones could not open the result). In a private chat
   an unusable key stops the send with `EncryptionError`. In a group, that member is left out
   with a warning in the logs, as the app does.
3. **Sealing.** The text is encrypted to every recipient and to the bot's own key, signed with
   the bot's key (SHA-512), and armored. The result is SEIPDv1, with AES-256 for the phones'
   keys (see 3.1). If it ever came out as SEIPDv2, it would not be sent.
4. **Sending**, with a `sendId` made once and reused for every attempt, so a retry after a
   timeout never writes the message twice.

If nobody in the chat has a key, the send fails with `EncryptionError`: **nothing is sent,
neither encrypted nor in clear**. The same happens if the chat is encrypted and the bot has no
private key.

### 5.1 When the mode changes

The library does what it can to know the mode before it sends:

- in a private chat it is the bot's own mode, read again from `GET /me` once it is older than
  `cache_ttl`, and at once after an encrypted message arrives while the bot thought it was
  readable;
- in a group, "readable" is trusted for 10 seconds at most, then `GET /chats/{id}` is asked
  again. The body of a message in the group is only a hint: when it disagrees with what the
  bot knows, the bot asks the server before its next send, and never takes the hint itself
  (anyone in the group can shape a message).

Still, the mode can change between the check and the send:

- **the chat became encrypted** and the bot sent text: the server refuses it with
  `wrong_body_kind`, and PGPCbot seals the message and sends it again with the same `sendId`.
  This is a move towards more protection, so it happens on its own. But the text did reach the
  server in the refused request, so a warning is logged;
- **the chat became readable** and the bot sent a sealed message: the server refuses it, and
  PGPCbot does **not** send it again in clear. It raises `WrongBodyKindError`; the next message
  follows the new mode.

With `require_encryption=True` the bot refuses to send anything in clear, anywhere
(`EncryptionRequiredError`). The `encrypt=` argument of a single send does the same for that
message: `True` requires encryption, `False` requires plain text.

---

## 6. Encrypted groups and attachments

In an encrypted group, **the phone of the person who writes** decides which bots to seal the
message to. It does that only for commands, mentions and replies to the bot, unless the owner
turned the bot's privacy mode off. A bot without a published key gets nothing in an encrypted
group.

The attachments of encrypted chats are binary OpenPGP files on the server. `message.media`
describes them (the description comes from inside the encrypted message), and

```python
data = await bot.download_media(message.media[0])
```

downloads the file and decrypts it. A phone seals an attachment only to the chat's recipients,
not to the bots it addresses. In a private chat with the bot this always works. In a group it
works only if the bot is in the group's list of keys (that is, its privacy mode is off).
Otherwise decryption fails with `NotARecipientError`. Downloads have a size limit
(`max_bytes`, 64 MiB by default), which also applies to the decrypted file, and they do not
send the token: that address does not use it.

Phones never compress attachments. A compressed one is refused with `DecryptionError` before
anything is inflated: a 64 MiB file of compressed data could open to many gigabytes.

---

## 7. Trusting keys

The server hands out the public keys. A compromised server, or an attacker who controls it,
could give the bot their own key instead of a person's. The bot would then seal messages for
the attacker, and messages signed by the attacker would show as `valid`. Encryption alone cannot
notice this.

What you can do:

- **Pin keys the first time you see them** (TOFU): `Bot(..., pin_keys=True)`. The first
  fingerprint seen for each person is saved in the storage. If the server later lists a
  different one, the key is refused both ways: sends to that person fail with
  `KeyChangedError`, and their incoming messages go to the error handlers as `KeyChangedError`
  instead of reaching your handlers (they wait in the failed list, so you can retry them). After
  you have checked the new key:

  ```python
  await bot.trust_key(user_id, new_fingerprint)
  ```

  With `MemoryStorage` the fingerprints are lost at every restart: use `SQLiteStorage` if you
  really want to pin them. TOFU catches a key that changes, not a server that lies from the
  very first moment.
- **Compare fingerprints over another channel** for the people who matter.
  `format_fingerprint()` shows them in groups of four, the way people read them.
- **Publish the bot's fingerprint** where users can check it (a website, the bot's profile, the
  docs). That is how people can make sure they are sealing to the real bot.

---

## 8. Risks and how they are handled

| Risk | How it is handled |
|---|---|
| Token in logs, exceptions, `repr` | the token is only in the `Authorization` header; `repr` shows `pgpbot_<id>.****abcd`; a filter removes tokens and private key blocks from every message of the `pgpcbot.*` loggers; clients cannot be pickled |
| Private key or passphrase in logs | never logged; `BotKey` and `Secret` refuse pickle and do not show up in `repr` |
| Private key in memory | it stays in the process as long as the object lives: Python cannot wipe memory. Treat the process like the key (no readable core dumps, no exposed debuggers) |
| Silent switch to plain text | never automatic (section 5.1); `require_encryption` if you want no risk at all |
| Tampered member keys | the fingerprint is checked against the key; optional TOFU (section 7) |
| Messages the phones cannot read | the bot's key is SEIPDv1 only; v6 keys are left out; a message that comes out as SEIPDv2 is not sent |
| Revoked or expired member keys | left out of a group, refused in a private chat; Sequoia alone would fall back to an expired subkey, so the library checks first |
| Decrypted updates on disk | the SQLite storage keeps updates as they arrive: encrypted in encrypted chats, **in clear in readable chats**. Nothing decrypted is ever written: when a handler fails on a message from an encrypted chat, the failed list and the log keep the error's type and where it was raised, not its message (which could quote the text) |
| Compression bombs | a message is refused if it opens to more than 1 MiB, an attachment if it is compressed at all or opens to more than `max_bytes`. Neither engine can stop half way, so one message is decrypted at a time: a crafted message costs at most a few hundred MiB for a moment, not that much per worker |
| Clocks out of sync | signatures carry the signer's time; keep the server's clock in sync (NTP), like the phones' |
| Marvin attack on RSA (RUSTSEC-2023-0071) | it affects RSA private keys: the bot uses Curve25519. Don't load an RSA key as the bot's key |
| BZip2 | the engine does not decompress BZip2; the app does not use it (it compresses with ZIP) |
| Text from OpenPGP.js | OpenPGP.js text messages use CRLF; `message.text` turns them back into LF, `message.plaintext` stays exact |

---

## 9. Using OpenPGP by hand

The `pgpcbot.crypto` module works on its own, if you want full control:

```python
from pgpcbot.crypto import BotKey, PublicKey

key = BotKey.from_file("bot-key.asc", passphrase)
anna = PublicKey.from_file("anna.asc")

armored = key.seal("hi Anna", [anna])                  # encrypted for Anna and the bot, signed
result = key.decrypt(armored_from_anna, verify_with=[anna])
print(result.text, result.verification.status)
```

And with the low-level client, which never encrypts anything on its own:

```python
keys = await bot.api.get_chat_keys(chat_id)
recipients = [PublicKey.from_user_key(k) for k in keys.keys]
await bot.api.send_message(chat_id, armored=key.seal("text", recipients))
```

`bot.recipients(chat)` and `bot.seal(chat, text)` do the steps in between with the same checks
as an automatic send, without sending anything.
