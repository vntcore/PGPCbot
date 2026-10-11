# Technical research

This page collects the research done before writing PGPCbot: what was read, what was tested
on a real machine, which options were chosen and which were dropped, and why.

- **Date of the research:** 8 October 2026. Versions and release dates were read from the PyPI
  JSON API, the npm registry, GitHub releases and the projects' changelogs on that date.
- **Test machine:** Windows 11, CPython 3.14.0, JDK 21, Node 24.14.0.
- **Which sources win:** for how the protocol behaves, the authority is PGP Chat's own
  Bot API documentation (the OpenAPI document and its guide), checked against the behaviour
  of a server running locally and of real messages and keys made by the app, opened and
  verified with the app's own OpenPGP library. Web sources were used for implementation
  choices, never to invent features the server does not have.

When a decision depends on a fact that was tested, the fact is marked **[tested]**: it was
reproduced on this machine with the real libraries.

---

## 1. The protocol, checked against the server

The OpenAPI 3.1 spec describes 22 operations and one outgoing webhook (see
[API_COVERAGE.md](API_COVERAGE.md)). Testing the real API showed details that the spec
leaves out or describes only in part, and that the library must respect:

| Fact | What it means for the SDK |
|---|---|
| Text lengths are counted in UTF-16 code units, the way JavaScript's `.length` does | client-side checks count UTF-16 units: an emoji counts 2 |
| Text cannot hold control characters except `\n` and `\t`: even `\r` is refused | the SDK turns `\r\n` and `\r` into `\n` before sending |
| The trim the server applies to text also removes U+FEFF, as JavaScript's `trim()` does | the "empty text" check uses the same set of spaces |
| `PUT /key` carries the fingerprint as its own field, next to the key | the SDK computes the fingerprint from the key, it never takes it from the user |
| `GET /updates`: the `offset` deletes the updates below it **at the start** of the request, and the wait only starts when there is nothing to deliver | the offset is sent only for what is safe (saved, or already handled) |
| The webhook delivers one update at a time, in order, and retries the same one until it gets a 2xx | a 4xx to an authenticated delivery blocks the whole queue: avoid it |
| The per-minute limiter always says `retryAfter: 60` in the body; the real time is in the `Retry-After` header | for `rate_limited` the SDK prefers the header |
| `GET /me` returns more limits than the spec's example (for example `markupBytes`, `commandsPerScope`, `botsPerChat`) | the `Limits` model types the known ones and keeps all the others |
| In readable chats the text arrives "cleaned": spaces at the ends removed, runs of empty lines folded, cut at 4000 characters | documented; in encrypted chats the SDK gives back the exact text |
| `media[].duration` is in milliseconds (the app writes `durationMs`) | the Python field is called `duration_ms` |
| `GET /media/{id}` needs no authentication | the SDK downloads media without sending the token |
| In encrypted groups the phone seals attachments only to the members in the group's key list, not to the bots it addresses | a bot in privacy mode can read the text but cannot download that group's attachments |

These points are also in the user documentation, where they matter.

---

## 2. OpenPGP

### 2.1 The phones

The app uses **PGPainless 1.7.6 on Bouncy Castle 1.80**. From its messages and keys and from
tests with the real jars of those versions **[tested]**:

- v4 keys: `modernKeyRing` (an EdDSA primary key, an X25519 subkey to encrypt, an EdDSA subkey
  to sign) or RSA 2048/3072/4096;
- text messages are encrypted, **compressed with ZIP** and signed inline; attachments are
  binary, not compressed, and signed;
- PGPainless 1.7.6 **cannot use v6 keys** (`NoSuchElementException` when encrypting to them,
  signatures refused) and **cannot open SEIPDv2** (`NotImplementedError: Version 6 PKESKs are
  not yet supported`);
- when decrypting it does not require signatures; it checks them only for payment requests.

### 2.2 The Python libraries we looked at

| Library | State on 2026-10-08 | Result |
|---|---|---|
| **pysequoia** 0.1.35 (2026-08-30), bindings for Sequoia PGP | wheels for cp310-cp314 on Windows, macOS, manylinux and musllinux; Apache-2.0, with Sequoia (LGPL-2.0+) linked in statically | **chosen** as the main engine |
| **rpgp-py** 0.20.0 (2026-07-23), bindings for rPGP | MIT, abi3 wheels from cp310 (one per platform, which also covers future Pythons); rPGP is used by Delta Chat and has been audited | **chosen** for two specific jobs (see below) |
| PGPy 0.6.0 (2022-11-24) | `import pgpy` fails on Python 3.13+ (`imghdr` was removed) **[tested]**; no longer maintained | dropped |
| PGPy13 0.6.1rc1 (2025-03-01) | a fork that calls itself temporary; cannot read OpenPGP.js keys (SHA3 in the preferences) **[tested]**; relies on deprecated `cryptography` modes | dropped |
| python-gnupg 0.5.7, gpg (GPGME) 2.0.0, pygpgme | need the `gpg` binary or `libgpgme`, and GnuPG state on disk | dropped: the SDK must work with `pip install` alone |
| johnnycanencrypt 0.18.0 | no signed encryption; no Linux aarch64/musl wheels | dropped |

### 2.3 The interoperability lab

Two separate test runs compared pysequoia, rpgp-py, PGPy13, OpenPGP.js 6.3.2 and a Java
program that uses PGPainless 1.7.6 exactly as the app does **[tested]**:

- a message encrypted and signed by pysequoia for an Ed25519 phone and an RSA-3072 phone is
  opened by PGPainless with `SIG_VERIFIED true`, and the text is identical byte for byte;
- a message from the phone (compressed, signed) is opened by pysequoia;
- **pysequoia 0.1.35 cannot check the signature inside an encrypted and compressed message**:
  with the verification callback it raises `RuntimeError: Unexpected message structure`,
  because its `verify.rs` accepts compression only as the first layer. Without verification it
  decrypts normally;
- **pysequoia switches to SEIPDv2 when every recipient says it supports it**, and the keys it
  generates do say so (Features 0x09). In a chat where every key supports SEIPDv2, the phones
  would get messages they cannot read;
- **pysequoia has no API to protect a private key with a passphrase** (it can only use a key
  that is already protected);
- rpgp-py checks the signatures of the phones' compressed messages; it makes v4 EdDSA/X25519
  keys with exactly the Features and preferences you ask for, with a passphrase, and PGPainless
  accepts them.

### 2.4 The choice: two engines, each where it is strongest

- **pysequoia** seals and opens messages. Sequoia applies the OpenPGP rules that are easy to
  get wrong by hand: which subkey may encrypt, which one is expired or revoked, the policy on
  weak algorithms.
- **rpgp-py** makes the bot's key and checks incoming signatures:
  - the bot's key says it supports **SEIPDv1 only**. Since the bot always seals to itself too,
    Sequoia can never pick SEIPDv2: every message from the bot stays readable for the phones,
    whatever keys the other members have **[tested]**;
  - the passphrase, if you choose one, protects the key with iterated and salted S2K
    (SHA-256) and AES-256, which any OpenPGP tool can read;
  - signature checks also work on the phones' compressed messages **[tested]**. As an extra
    defence, the text decrypted by rPGP must be the same as Sequoia's: two independent parsers
    must agree, or the signature is treated as invalid.
- A private key that already exists can be protected by exporting it inside a
  password-encrypted OpenPGP message (the same format as `gpg -c`), which the SDK, `gpg -d` and
  `sq decrypt` can open.

The day pysequoia fixes signature checks on compressed messages and offers passphrase
protection, rpgp-py can leave the dependencies without any change to the public API.

### 2.5 Which key to make

- **v4, Ed25519 to sign and Curve25519 to encrypt**, with the "legacy" algorithm IDs (22 and
  18). RFC 9580 recommends v6 keys, but the phones cannot use them; Sequoia's `sq` also makes v4
  keys by default, and OpenPGP.js 6 has `v6Keys = false`.
- **No expiry date.** An expired bot key opens nothing any more, not even old messages, and
  phones can no longer encrypt to the bot, without anybody noticing. Rotation is still possible
  and documented.
- **No RSA for the bot.** The `rsa` crate used by both engines is affected by
  RUSTSEC-2023-0071 (the Marvin timing attack on RSA decryption, no fixed version). It only
  concerns operations with an RSA private key: a Curve25519 bot that talks to RSA phones is not
  exposed.

A message sealed by the finished SDK to a phone's key comes out as SEIPDv1 with AES-256, a v4
signature with SHA-512, uncompressed, with a binary literal; PGPainless 1.7.6 opens it and
checks the signature **[tested]**.

### 2.6 Trusting keys

RFC 9580 (sections 2.1-2.2) separates confidentiality from authenticity: anyone with the
recipients' public keys, the server included, can encrypt to them. A valid signature says that
the message was made with the key the server lists for that sender, not that the key really
belongs to that person. The practices we looked at: comparing fingerprints over another
channel (RFC 9580 section 13.6, Signal's "safety numbers"), TOFU (GnuPG's
`--trust-model tofu`, with "significantly weaker security guarantees" than the Web of Trust),
Autocrypt (which only defends against passive attacks). The SDK offers TOFU as an option
(`pin_keys=True`) and documents its limits in [CRYPTOGRAPHY.md](CRYPTOGRAPHY.md).

---

## 3. HTTP, asyncio, retries

### 3.1 The HTTP client: httpx2

- **httpx 0.28.1** has not changed since 6 December 2024. "1.0" exists only as a pre-release
  (`1.0.dev6`, 2026-08-31), and it is a sync-only redesign: no `AsyncClient`, `Limits` or
  `Timeout`.
- **httpx2 2.13.1** (2026-09-23) is the fork of httpx 0.28.1 maintained by Pydantic, with the
  same API. OpenAI Python 3.x moved to httpx2. Recent versions fix
  vulnerabilities published in 2026 (CVE-2026-84380, smuggling with Content-Length and
  Transfer-Encoding together; CVE-2026-84382, decompression with no memory limit) that most
  likely affect httpx 0.28.1 too, which was never fixed.
- Dropped: aiohttp (async only: we would need a second, sync stack), niquests (its dependency
  urllib3-future replaces `urllib3` in the user's whole environment).

A practical note: httpx2 uses the operating system's certificate store (truststore) instead of
certifi. In minimal containers without CA certificates, or behind company proxies, you need to
set `SSL_CERT_FILE`; the deployment guide says so.

### 3.2 Sync and async without writing the logic twice

The design is the one of the official OpenAI SDK and of other clients generated the same way: a shared core without I/O
(the description of each call, reading the envelope, the error map, the retry rules, the rate
limiter's numbers) and two thin shells, `AsyncClient` on `httpx2.AsyncClient` and `Client` on
`httpx2.Client`.

Dropped: a sync shell that calls `asyncio.run()` on every call (it does not work inside a loop
that is already running, for example in Jupyter, and it throws the connection pool away every
time). Also dropped as the default: a thread with its own event loop
(`run_coroutine_threadsafe` or an anyio portal). Weaviate adopted it in 2024 and removed it
nine months later.

In `SyncBot` the engine is still asyncio (polling, the queue, the per-chat order) and runs with
`asyncio.Runner` in the main thread. Only the handlers, which are plain functions, run in a
thread pool with the sync client. This way Ctrl+C works on Windows too (the Runner cancels the
main task; a long poll blocked on a socket would not stop).

### 3.3 asyncio from 3.11 to 3.15

Used: `asyncio.Runner`, `asyncio.timeout`, `TaskGroup` where it helps. Avoided: the "policy"
system (deprecated in 3.14, removed in 3.16), `asyncio.get_event_loop()` (in 3.14 it raises
`RuntimeError` when there is no current loop), `asyncio.iscoroutinefunction` (deprecated in
3.14: use `inspect.iscoroutinefunction`). On Windows `loop.add_signal_handler` does not exist:
SIGTERM and SIGBREAK are handled with `signal.signal` and `call_soon_threadsafe`.

### 3.4 Retries, Retry-After, idempotency

- Exponential backoff with **full jitter** (the AWS article "Exponential Backoff And Jitter"):
  a random wait between 0 and `min(cap, base * 2^n)`.
- `Retry-After` as in RFC 9110 section 10.2.3 (whole seconds or an HTTP date).
- Only what can be repeated safely is retried automatically (RFC 9110 section 9.2.2): reads,
  idempotent operations, and sends with a `sendId`. The SDK always makes a `sendId` (the same
  one for every attempt): it is the body version of the IETF `Idempotency-Key` proposal
  (draft-ietf-httpapi-idempotency-key-header-07, expired).
- A 429 can always be retried after the wait: the server refuses it before doing anything.
  Other 4xx errors never.
- Codes that mean "done" after an attempt whose result is unknown: `message_not_found` when
  repeating a delete, `callback_answered` when repeating an answer, `chat_not_found` when
  repeating a leave, `unauthorized` when repeating the token revocation.
- Not copied from those SDKs: retrying on 409. Here a 409 is final.

### 3.5 Client-side rate limiting

The server counts sends with a **sliding window log**. A simulation with the
server's algorithm: 60 sends in a group paced by a leaky bucket (the one in `aiolimiter`, used
by python-telegram-bot) get **19 refusals**; paced by a sliding window log, **none**. So the SDK
copies the same algorithm with the same numbers, read from `GET /me`.

---

## 4. Webhooks, the durable queue, frameworks

### 4.1 Webhook security

Main sources: the OWASP Webhook Security Cheat Sheet (published on 2026-09-28), the REST
Security and Logging Cheat Sheets, RFC 9110.

- the secret is compared **in constant time** (`hmac.compare_digest` on bytes);
- `401` (with `WWW-Authenticate`, as RFC 9110 asks) without saying why; `405` for methods other
  than POST; `413` above the size limit; `408` for clients that send the body too slowly;
- `200` only after the update has been saved durably, or after it has been handled when there
  is no durable storage;
- **adapted to the PGP Chat server:** OWASP suggests `400` for a malformed payload, but here
  the server would retry the same delivery for days, with waits of up to one hour, and block
  every later update. An **authenticated** delivery that cannot be read is logged as an error
  and confirmed with `200`;
- never in the logs: request bodies, secrets, the values of authentication headers.

What existing frameworks do: python-telegram-bot compares the secret with `!=` (not in constant
time) and logs the received value and the whole body at DEBUG level; it confirms the update
before handling it, keeping it in an in-memory queue. aiogram uses `secrets.compare_digest`
but by default answers before handling. PGPCbot avoids both.

### 4.2 FastAPI, Starlette, ASGI

- Starlette 1.7.0 (1.0 came out in March 2026) removed `on_startup`/`on_shutdown`: use
  `lifespan`.
- **An app mounted with `Mount` does not get its own lifespan** **[tested]**, and FastAPI
  documents this for sub-applications. This is why the SDK offers `bot.lifespan` to pass to the
  main app; without it, the bot starts at the first delivery, with a warning.
- FastAPI 0.142 does not expose Starlette's `max_body_size`: the size limit is applied inside
  the SDK's endpoint.
- The endpoint is a plain ASGI application with no dependencies: it works with uvicorn,
  Hypercorn and Granian, and mounted in FastAPI or Starlette.

### 4.3 The durable queue

- SQLite in WAL mode with `synchronous=FULL`: in WAL mode `NORMAL` can lose the last
  transaction after a power cut, and here a lost write would be a lost update, because it is
  confirmed to the server right after.
- Every write opens its transaction with `BEGIN IMMEDIATE` **[tested]**: a `DEFERRED`
  transaction that tries to write after another process committed fails with
  `database is locked`, even with `busy_timeout`.
- De-duplication by the highest `updateId` accepted: the server promises growing IDs and
  delivers in order.
- Only one process at a time handles a bot's updates: an operating system lock on a file next
  to the database (`msvcrt.locking` on Windows, `fcntl.flock` elsewhere), which the system
  releases even if the process dies. Dropped: `filelock` (twelve releases in eighteen days in
  September 2026) and `portalocker`: about thirty lines do the job, with no dependency.
- Dropped: aiosqlite: one thread per connection and one more dependency, when
  `asyncio.to_thread` with a lock is enough.

### 4.4 Bot frameworks compared

Taken from aiogram 3.31: a tree of routers, the first matching handler wins with
`SkipHandler` to pass the update on, middleware with the signature `(handler, event, data)`,
injecting the parameters the handler declares, `/command@botname` compared with the bot's own
name. Taken from python-telegram-bot 22.8: immutable models that keep unknown fields. Avoided:
the "magic filter" DSL as the main API (it fights with type checkers, as aiogram's issues
show), confirming updates before handling them, tight version pins on the main dependencies.

### 4.5 Data models: dataclasses instead of Pydantic

Pydantic 2.13 would have worked (camelCase aliases, extra fields kept, discriminated unions),
but it is heavy: a 2.4 MB wheel with its compiled part, and about 135 ms to import against 6 ms
**[tested]**; and a user's FastAPI application often has its own version pins on Pydantic.
msgspec drops unknown fields and cannot keep them. The choice: hand-written immutable
dataclasses, each with the original JSON in `raw`; unknown enum values become pseudo-members
instead of errors.

---

## 5. Packaging, tests, quality

| Tool | Version checked | Choice |
|---|---|---|
| hatchling | 1.32.4 | build backend; metadata pinned to version 2.4 (2.5, the default, is accepted by PyPI but not by twine < 7 and some private mirrors) |
| license | PEP 639 | `license = "MIT"` + `license-files`, without the `License ::` classifier (deprecated, and three backends out of six fail when the SPDX expression is there too) |
| name | PyPI | `PGPCbot` is free: no collision in the full index (908,274 projects), neither with PEP 503 normalisation nor with PyPI's anti-typosquatting normalisation |
| pytest | 9.1.1 | native `[tool.pytest]` configuration |
| pytest-asyncio | 1.4.0 | `asyncio_mode = "auto"`; the `event_loop` fixture is gone since 1.0 |
| coverage | 7.16.2 | branch coverage |
| ruff | 0.16.10 | lint and formatting; since 0.16 the default set has 413 rules |
| mypy | 2.4.0 | `strict` mode |
| GitHub Actions | checkout v7, setup-python v7, setup-uv v10.2.0, gh-action-pypi-publish `release/v1` (v1.14.2) | a 3.11-3.14 matrix on Linux, Windows and macOS; publishing with Trusted Publishing, never automatic |

HTTP tests don't use respx: it intercepts `httpx`, not `httpx2`. httpx2's fake transport
(`MockTransport`, or a class that implements both transport interfaces) serves both the sync
and the async client.

**Python versions.** 3.10 reached its end of life on 2026-10-01; 3.11 gets security fixes until
October 2027 and is still 17-20% of the downloads of similar packages. The final 3.15.0 was
expected on 9 October 2026, the day after this research: pysequoia has no cp315 wheels for
Windows and macOS yet, so the classifiers list 3.11-3.14.

---

## 6. Documentation

- **Sphinx** builds the site. The API reference is generated by **autodoc** from the docstrings,
  which are written in Google style and read by the **napoleon** extension; the guides are
  Markdown pages read by **MyST-Parser 5.1.0**. The theme is **Furo 2025.12.19**, and
  **sphinx-copybutton 0.5.2** adds a copy button to the code blocks.
- **Sphinx 9.1.0** needs Python 3.12 or newer. On 3.11 pip settles on **8.2.3**, which builds
  the same site: the `docs` group allows `>=8.2,<10`.
- The build runs with `-n` (every reference between objects must resolve) and `-W` (a warning
  is an error). `tests/test_docs.py` checks that every public object has a docstring, that every
  parameter is in `Args:` and every field in `Attributes:`, and runs the `>>>` examples. Ruff's
  pydocstyle rules (`D`, Google convention) run with the rest of the lint.
- The same docstrings are what `help()` and `python -m pydoc` show, so the reference can be read
  without a browser.
- The pages load nothing from third parties: Furo uses the system fonts and the copy button's
  script is bundled **[tested]** on the built site. The only network access is at build time,
  for the inventory of the Python documentation (intersphinx).
- The OpenAPI 3.1 spec is not shown with Swagger UI any more: the page of operations is
  generated from the file (`scripts/openapi_pages.py`) and the file itself can be downloaded.
- The documentation is written in simple English, for readers whose first language is not
  English.
- History: the first version used MkDocs 1.6.1 with Material for MkDocs and mkdocstrings, with
  Markdown docstrings and hand-written reference pages; Sphinx had been dropped because it needs
  Python 3.12 and reStructuredText docstrings. It moved to the standard Python method on
  2026-10-09: docstrings in the code, read by autodoc, and the same text in `pydoc`. Two concerns noted for MkDocs went away with it: MkDocs 2.0 is a
  rewrite without plugins, and Material for MkDocs is in maintenance mode until 5 May 2027.

---

## 7. Summary of what was dropped

| Dropped | Why |
|---|---|
| PGPy / PGPy13 | cannot be imported on 3.13+ / a temporary fork that cannot read OpenPGP.js keys |
| GnuPG via subprocess or GPGME | needs system binaries and state on disk |
| v6 keys | the phones (PGPainless 1.7.6) cannot use them |
| keys that expire by default | the bot would stop decrypting without warning |
| httpx 0.28.1 / httpx 1.0.dev | no longer updated, likely unfixed vulnerabilities / a different API, still a pre-release |
| aiohttp, niquests | async only / changes `urllib3` for the whole environment |
| a sync wrapper around `asyncio.run` | broken inside a running loop, loses the connection pool |
| a leaky bucket for the limits | refusals that the server's algorithm avoids |
| Pydantic, msgspec for the models | weight and version conflicts / unknown fields lost |
| confirming an update before saving it | updates lost on a crash |
| 400 for authenticated but unreadable webhook deliveries | blocks the server's queue for days |
| filelock, portalocker, aiosqlite | extra dependencies for thirty lines of code |
| MkDocs, Material, mkdocstrings | replaced by Sphinx (2026-10-09): docstrings as the only source of the reference |
| pdoc | API pages only, no guides |
| respx | does not intercept httpx2 |

---

## 8. What was tested on this machine

- installing the pysequoia 0.1.35 and rpgp-py 0.20.0 wheels on CPython 3.14.0 on Windows;
- full interoperability of the engine libraries with PGPainless 1.7.6 (both directions,
  Ed25519 and RSA-3072 keys, signed, unsigned and compressed messages, binary attachments) and
  with OpenPGP.js 6.3.2; the SDK's own tests repeat the PGPainless part (see
  `tests/interop/`);
- building the documentation with MkDocs, Material and mkdocstrings in `--strict` mode on
  3.14.0 on Windows, and with Zensical;
- SQLite 3.50.4 with `BEGIN IMMEDIATE` between two processes, `RETURNING`, WAL;
- Starlette 1.7.0 with `max_body_size` and with the lifespan of mounted apps;
- the SDK itself against the real PGP Chat server, started locally on a temporary database
  (see the integration tests).

---

## 9. Sources

The references worth keeping, grouped by area: the specifications the library follows, the
pages of the libraries it depends on, and the advisories behind a version floor. Package
versions and release dates were read from PyPI and the npm registry on the date above.

### OpenPGP

- <https://www.rfc-editor.org/rfc/rfc9580.html> - the standard; v6 keys and SEIPDv2 come from here
- <https://github.com/wiktor-k/pysequoia>
- <https://book.sequoia-pgp.org/sq_key_generation.html>
- <https://github.com/rpgp/rpgp>
- <https://github.com/pgpainless/pgpainless> - the library the phones use
- <https://github.com/openpgpjs/openpgpjs/releases/tag/v6.0.0>
- <https://rustsec.org/advisories/RUSTSEC-2023-0071.html> - the RSA timing advisory behind the version floor
- <https://www.gnupg.org/documentation/manuals/gnupg/GPG-Configuration-Options.html>
- <https://docs.autocrypt.org/level1.html> - key pinning, by people who argued it out first
- <https://signal.org/blog/safety-number-updates/>

### HTTP, retries, rate limits

- <https://www.rfc-editor.org/rfc/rfc9110> - status codes and `Retry-After`
- <https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/> - the idea behind `sendId`
- <https://httpx2.pydantic.dev/>
- <https://www.cve.org/CVERecord?id=CVE-2026-84380>
- <https://www.cve.org/CVERecord?id=CVE-2026-84382>
- <https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/>
- <https://github.com/mjpieters/aiolimiter> - the leaky bucket measured in section 4

### Webhook, storage, ASGI

- <https://cheatsheetseries.owasp.org/cheatsheets/Webhook_Security_Cheat_Sheet.html>
- <https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html>
- <https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html>
- <https://asgi.readthedocs.io/en/latest/specs/lifespan.html>
- <https://starlette.dev>
- <https://www.sqlite.org/wal.html>
- <https://www.sqlite.org/lang_transaction.html>
- <https://www.sqlite.org/pragma.html#pragma_synchronous>

### Packaging, and how other bot libraries do it

- <https://typing.python.org/en/latest/spec/distributing.html> - what `py.typed` promises
- <https://peps.python.org/pep-0639/> - the licence fields in `pyproject.toml`
- <https://docs.aiogram.dev/en/latest/>
- <https://docs.python-telegram-bot.org/en/stable/>
