# Versions and upgrades

PGPCbot uses [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

## What counts as the public API

- every name exported by `pgpcbot` (and by `pgpcbot.crypto`, `pgpcbot.filters`,
  `pgpcbot.webhook`), with its documented parameters and behaviour;
- the `pgpcbot` command and its options;
- the environment variables listed in [Configuration](../getting-started/configuration.md);
- the SQLite database format of `SQLiteStorage` (a new version opens the old file).

Not public: names that start with `_`, the exact text of error and log messages (use the
exception types and `code`, not the message text), and the `raw` contents of models.

## Before 1.0

While the version is `0.x`:

- a **minor** release (`0.2.0`) may change the public API. Changes are listed in the
  [changelog](changelog.md) with what to do;
- a **patch** release (`0.1.1`) only fixes bugs and never breaks working code.

Pin the minor version: `PGPCbot>=0.1,<0.2`.

## From 1.0

- **patch**: bug fixes only;
- **minor**: new features; nothing that worked breaks. Deprecated things keep working and warn
  (`DeprecationWarning`) for at least one minor release;
- **major**: may remove deprecated things and change behaviour, with an upgrade guide.

## Following the server

The library follows the Bot API version in the server's OpenAPI document (1.0.0 today).

- New fields from the server don't break old versions of the library: they stay in `raw`, and
  unknown enum values become pseudo-members.
- New operations of the API come in a **minor** release.
- Limits are read from `GET /me` at start-up, so changed numbers on the server need no upgrade.

## Supported Python versions

A Python version is supported until its end of life. Dropping one comes in a minor release
and is announced in the changelog. Today: 3.11 to 3.14.

## Security fixes

Security fixes go to the latest minor release. See [Security](../security/index.md).
