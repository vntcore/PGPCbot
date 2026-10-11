# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the version
numbers follow [Semantic Versioning](https://semver.org/), with the rules in
`docs/project/versioning.md`.

## [Unreleased]

### Added

- `resolve()` on both clients and both bots, for the 22nd operation of the Bot API
  (`GET /resolve/{username}`): the handle somebody writes at a bot, turned into an id. One
  pool of names covers people, groups and channels, so the answer says which it was -
  `Resolved`, with `HandleKind`, a `ChatPeer` for a person or the new `ChatCard` for a public
  group or channel. A leading `@` and the case are taken off before the request goes out.
- `UsernameNotFoundError` (404 `username_not_found`), raised when nothing answers to a handle:
  also what a group with no public name and a closed account answer.
- A guide to where ids come from, in `docs/guides/events.md`: which field of which event
  carries the chat and the writer, when `from_user` is `None`, and when to look a handle up
  instead.

### Changed

- `ChatPeer.from_json` takes the name of the place it is reading, since the shape now arrives
  from two operations rather than one. Reading a private chat is unchanged.

## [0.1.0] - 2026-10-08

First release.

### Added

- `AsyncClient` and `Client`: all 21 operations of Bot API 1.0.0 (`openapi.json`), plus media
  downloads, with typed models, client-side checks that match the server's, retries with
  backoff and a stable `sendId`, waiting on `429`, and a client-side rate limiter.
- `Bot` and `SyncBot`: handlers for messages, commands, buttons, edited and deleted messages,
  and membership changes; filters, routers, middleware, error handlers, automatic answers to
  button presses; startup and shutdown hooks; `request_stop()` from a handler or another thread.
- OpenPGP: making, loading, protecting and rotating the bot's key; automatic encryption and
  signing in protected chats; decryption and signature checks on incoming messages, including
  the compressed messages phones send; reading the app's internal message format; downloading
  and decrypting attachments; pinning keys the first time they are seen.
- Long polling and an ASGI webhook (FastAPI, Starlette, uvicorn) with "at least once"
  delivery, a durable `SQLiteStorage` queue, and failed updates kept so they can be retried.
- The `pgpcbot` command line: `keygen`, `fingerprint`, `whoami`, `publish-key`, `webhook`.
