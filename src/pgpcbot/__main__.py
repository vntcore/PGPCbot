"""The ``pgpcbot`` command: the chores that don't need a running bot.

Installing PGPCbot adds a ``pgpcbot`` command; ``python -m pgpcbot`` runs the same thing::

    pgpcbot keygen --name NAME --out FILE [--username HANDLE] [--public FILE]
                   [--passphrase-env VAR | --no-passphrase] [--force]
    pgpcbot fingerprint FILE [--spaced] [--passphrase-env VAR]
    pgpcbot whoami
    pgpcbot publish-key FILE [--passphrase-env VAR] [--yes]
    pgpcbot webhook info | set URL [--secret-env VAR] | delete
    pgpcbot --version

- ``keygen`` makes a new key for a bot and writes the private key to ``--out``, and the public
  key to ``--public`` if given. ``--force`` replaces files that exist. Without
  ``--passphrase-env`` or ``--no-passphrase`` it asks for the passphrase twice on the
  terminal; an empty answer leaves the key unprotected.
- ``fingerprint`` prints the fingerprint of a key file: a public key, a private key, or a key
  file sealed with a passphrase, which it has to unlock. ``--spaced`` prints it in groups of
  four.
- ``whoami`` asks the server who the token belongs to: the handle, the mode and the key, the
  status, the webhook and the updates waiting.
- ``publish-key`` loads a private key, asking for its passphrase if needed, and publishes its
  public part: the bot becomes protected, and its private chats end-to-end encrypted. Only
  the owner can undo that, from the console, so it asks you to type ``publish`` first;
  ``--yes`` skips the question.
- ``webhook info`` shows the webhook, ``webhook set URL`` sets it, ``webhook delete`` removes
  it. ``set`` without ``--secret-env`` makes a new secret and prints it once.

The token comes from ``PGPCHAT_BOT_TOKEN`` (or ``PGPCHAT_BOT_TOKEN_FILE``) and the server from
``PGPCHAT_API``, as for the library. Passphrases and webhook secrets are never arguments, where
they would end up in the shell history and the process list: they are read from an
environment variable you name (``--passphrase-env VAR``, ``--secret-env VAR``), or asked on
the terminal.

Exit status:

- ``0``: done.
- ``1``: nothing done. Either ``publish-key`` was not confirmed, or the command refused its
  input before starting (an empty variable, no terminal to ask the passphrase on, two
  passphrases that differ, ``webhook set`` without a URL), with the reason on standard error.
- ``2``: an error from the library or the server, printed on standard error as
  ``error: ...``, without the token. Also a command line that cannot be parsed.
- ``130``: interrupted with Ctrl+C.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Sequence

from . import __version__
from .client import Client
from .crypto import BotKey, PublicKey, _lib, format_fingerprint
from .errors import ConfigurationError, PassphraseError, PGPCbotError
from .webhook import generate_secret


def _passphrase(args: argparse.Namespace, *, confirm: bool) -> str | None:
    if getattr(args, "no_passphrase", False):
        return None
    if args.passphrase_env:
        value = os.environ.get(args.passphrase_env)
        if not value:
            raise SystemExit(f"{args.passphrase_env} is empty or not set")
        return value
    if not sys.stdin.isatty():
        raise SystemExit("no terminal to ask the passphrase on: use --passphrase-env VAR or --no-passphrase")
    first = getpass.getpass("Passphrase (empty for none): ")
    if not first:
        return None
    if confirm and getpass.getpass("Again: ") != first:
        raise SystemExit("the two passphrases differ")
    return first


def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        raise ConfigurationError(f"cannot read the key file {path}: {exc.strerror}") from None


def _load_key(args: argparse.Namespace) -> BotKey:
    """The private key in `args.file`, asking for its passphrase on the terminal if it needs one."""
    data = _read(args.file)
    try:
        return BotKey.from_armored(data, _passphrase(args, confirm=False) if args.passphrase_env else None)
    except PassphraseError:
        if args.passphrase_env or not sys.stdin.isatty():
            raise
    passphrase = getpass.getpass("Passphrase of the key: ")
    return BotKey.from_armored(data, passphrase or None)


def cmd_keygen(args: argparse.Namespace) -> int:
    """Runs ``pgpcbot keygen``: makes a key, saves it and prints its fingerprint.

    With a passphrase, the private key file is protected by it.

    Args:
        args: The parsed command line.

    Returns:
        The exit status, ``0``.

    Raises:
        ConfigurationError: ``--public`` or ``--out`` names a file that exists, and ``--force``
            is not given.
    """
    if args.public and os.path.exists(args.public) and not args.force:
        raise ConfigurationError(f"{args.public} exists; pass --force to replace it")
    passphrase = _passphrase(args, confirm=True)
    key = BotKey.generate(args.name, username=args.username, passphrase=passphrase)
    # protected at generation: the file is the S2K-protected key, readable by any OpenPGP tool
    path = key.save(args.out, passphrase=None, overwrite=args.force)
    print(f"private key: {path}{'' if passphrase else '  (NOT protected by a passphrase)'}")
    if args.public:
        with open(args.public, "w", encoding="ascii", newline="\n") as fh:
            fh.write(key.export_public())
        print(f"public key:  {args.public}")
    print(f"fingerprint: {format_fingerprint(key.fingerprint)}")
    return 0


def cmd_fingerprint(args: argparse.Namespace) -> int:
    """Runs ``pgpcbot fingerprint``: prints the fingerprint of a key file.

    A public or a private key needs no passphrase, because the fingerprint is public. A key file
    sealed whole with a passphrase (what ``BotKey.save()`` writes with one) is unlocked first.

    Args:
        args: The parsed command line.

    Returns:
        The exit status, ``0``.
    """
    data = _read(args.file)
    if b"BEGIN PGP MESSAGE" in data:
        # a key sealed whole with a passphrase: there is no way in without it
        fingerprint = _load_key(args).fingerprint
    elif b"PRIVATE KEY BLOCK" in data:
        # the fingerprint is public: no need to unlock the key to print it
        fingerprint = _lib.secret_fingerprint(data)
    else:
        fingerprint = PublicKey.from_armored(data).fingerprint
    print(format_fingerprint(fingerprint) if args.spaced else fingerprint)
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    """Runs ``pgpcbot whoami``: asks the server who the token belongs to, and prints it.

    Args:
        args: The parsed command line; ``whoami`` has no options.

    Returns:
        The exit status, ``0``.
    """
    with Client() as api:
        me = api.get_me()
    print(f"@{me.username} (id {me.id}) - {me.name}")
    print(f"mode:     {me.mode.value}" + (f", key {format_fingerprint(me.fingerprint)}" if me.fingerprint else ""))
    print(f"status:   {me.status.value}, privacy mode {'on' if me.privacy_mode else 'off'}")
    print(
        f"webhook:  {me.webhook.url or 'none (polling)'}"
        + (f", {me.webhook.failures} failures" if me.webhook.failures else "")
    )
    print(f"queued:   {me.queued} update(s)")
    return 0


def cmd_publish_key(args: argparse.Namespace) -> int:
    """Runs ``pgpcbot publish-key``: publishes the public part of a private key file.

    The key is loaded first, so a key whose passphrase is lost cannot be published. Unless
    ``--yes`` is given, it asks you to type ``publish``: the bot becomes protected, and only
    the owner can undo that, from the console.

    Args:
        args: The parsed command line.

    Returns:
        The exit status, ``0`` when published or ``1`` when not confirmed.

    Raises:
        ConfigurationError: The key is a v6 key, which phones on PGPainless 1.7 cannot use.
    """
    # loaded, not just read: publishing a key whose passphrase is lost would lock the bot out
    key = _load_key(args)
    if key.public_key.version != 4:
        raise ConfigurationError("only v4 keys can be published: phones on PGPainless 1.7 can't use v6 keys")
    if not args.yes:
        print(
            f"Publishing {format_fingerprint(key.fingerprint)} makes the bot protected: its private chats\n"
            "become end-to-end encrypted. Only the owner can undo it, from the console."
        )
        if input("Type 'publish' to go on: ").strip() != "publish":
            print("nothing done")
            return 1
    with Client() as api:
        result = api.set_key(key.export_public(), key.fingerprint)
    print(f"published {format_fingerprint(result.fingerprint)}, mode {result.mode.value}")
    return 0


def cmd_webhook(args: argparse.Namespace) -> int:
    """Runs ``pgpcbot webhook``: shows, sets or removes the webhook.

    ``set`` reads the secret from the variable named by ``--secret-env``. Without it, a new
    secret is made and printed once: give it to your webhook app.

    Args:
        args: The parsed command line.

    Returns:
        The exit status, ``0``.

    Raises:
        SystemExit: ``set`` without a URL, or a ``--secret-env`` variable that is empty or not
            set. The exit status is ``1``.
    """
    with Client() as api:
        if args.action == "info":
            info = api.get_webhook()
            print(f"url:      {info.url or 'none'}")
            print(f"secret:   {'set' if info.has_secret else 'none'}")
            print(f"pending:  {info.pending}, failures in a row: {info.failures}")
            if info.last_error:
                print(f"last error: {info.last_error}")
        elif args.action == "set":
            if not args.url:
                raise SystemExit("webhook set needs a URL")
            secret = os.environ.get(args.secret_env) if args.secret_env else None
            if args.secret_env and not secret:
                raise SystemExit(f"{args.secret_env} is empty or not set")
            if secret is None:
                secret = generate_secret()
                print(f"generated secret (give it to your webhook app, it won't be shown again): {secret}")
            info = api.set_webhook(args.url, secret)
            print(f"webhook set: {info.url}, {info.pending} update(s) pending")
        else:
            info = api.delete_webhook()
            print(f"webhook removed, {info.pending} update(s) pending")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Builds the argument parser of the ``pgpcbot`` command.

    Returns:
        The parser. Each subcommand sets ``func`` to the function that runs it.
    """
    parser = argparse.ArgumentParser(prog="pgpcbot", description="Chores for PGP Chat bots.")
    parser.add_argument("--version", action="version", version=f"pgpcbot {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    keygen = sub.add_parser("keygen", help="make a new key for a bot")
    keygen.add_argument("--name", required=True, help="the bot's name, for the user ID")
    keygen.add_argument("--username", help="the bot's handle, without @")
    keygen.add_argument("--out", required=True, help="where to write the private key")
    keygen.add_argument("--public", help="also write the public key here")
    keygen.add_argument("--passphrase-env", metavar="VAR", help="read the passphrase from this variable")
    keygen.add_argument("--no-passphrase", action="store_true", help="leave the private key unprotected")
    keygen.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    keygen.set_defaults(func=cmd_keygen)

    fpr = sub.add_parser("fingerprint", help="print the fingerprint of a key file")
    fpr.add_argument("file")
    fpr.add_argument("--spaced", action="store_true", help="in groups of four")
    fpr.add_argument("--passphrase-env", metavar="VAR")
    fpr.set_defaults(func=cmd_fingerprint)

    who = sub.add_parser("whoami", help="ask the server who this token belongs to")
    who.set_defaults(func=cmd_whoami)

    pub = sub.add_parser("publish-key", help="publish a key: the bot turns protected (one-way)")
    pub.add_argument("file", help="the private key file")
    pub.add_argument("--passphrase-env", metavar="VAR")
    pub.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    pub.set_defaults(func=cmd_publish_key)

    hook = sub.add_parser("webhook", help="look at, set or remove the webhook")
    hook.add_argument("action", choices=("info", "set", "delete"))
    hook.add_argument("url", nargs="?")
    hook.add_argument("--secret-env", metavar="VAR", help="read the secret from this variable")
    hook.set_defaults(func=cmd_webhook)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the ``pgpcbot`` command, the entry point of the console script.

    An error from the library is printed on standard error as ``error: ...``. A command line
    that cannot be parsed, ``--help``, ``--version`` and the input a command refuses leave
    through ``SystemExit`` instead of a return value.

    Args:
        argv: The arguments, without the program name. ``None`` reads ``sys.argv``.

    Returns:
        The exit status, ``0`` when done, ``1`` when not confirmed, ``2`` after an error, or
        ``130`` after Ctrl+C.
    """
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except PGPCbotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
