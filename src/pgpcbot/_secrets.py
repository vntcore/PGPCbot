"""Secrets: where they come from, and keeping them out of logs.

A secret (the token, a passphrase) can be given as a plain string, a `~pgpcbot.Secret`, or a
function that returns one of them (a vault lookup, say). From the environment, ``NAME_FILE``
wins over ``NAME``: it holds the path of a file with the value, the way Docker and Kubernetes
mount secrets. A file is the safer place, because the environment leaks into child processes
and crash dumps.

When a bot is created, the ``pgpcbot`` loggers also get a filter that removes anything shaped
like a bot token or a private key block. It is a safety net: the library never logs secrets
on purpose.

Internal: only `~pgpcbot.Secret` is public, importable from ``pgpcbot``.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import ConfigurationError, without_tokens

__all__ = ["RedactingFilter", "Secret", "install_redaction", "read_env", "resolve", "scrub"]

_PRIVATE_BLOCK = re.compile(
    r"-----BEGIN PGP PRIVATE KEY BLOCK-----.*?(?:-----END PGP PRIVATE KEY BLOCK-----|\Z)", re.DOTALL
)


class Secret:
    """A string that does not show itself, for a token or a passphrase.

    ``repr()``, ``str()`` and f-strings give ``Secret('***')``, so the value stays out of logs,
    error reports and debugger views that print variables. It refuses to be pickled, so it does
    not end up in a cache or a queue either. ``bool(secret)`` is ``False`` for an empty string.
    Call `reveal` where the value is needed.

    The token (``token=`` of `Bot`, `SyncBot`, `AsyncClient` and `Client`) and the passphrase of
    a key being loaded (``pgp_passphrase=``, `BotKey.from_armored`, `BotKey.from_file`,
    `BotKey.from_env`) can be given as a `Secret`, or as a function that returns the value as
    a string or a `Secret`.

    It guards against accidents; it is not encryption. The value is an ordinary string in the
    process's memory, so a memory dump, a debugger or any code in the process can still read
    it. The plain string that `reveal` returns has none of this protection.

    Example:
        >>> from pgpcbot import Secret
        >>> token = Secret("pgpbot_...")
        >>> token
        Secret('***')
        >>> print(f"using {token}")
        using Secret('***')
        >>> token.reveal()
        'pgpbot_...'
        >>> import pickle
        >>> pickle.dumps(token)
        Traceback (most recent call last):
            ...
        TypeError: a Secret cannot be pickled
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        """Wraps ``value``.

        Args:
            value: The secret, as a string.

        Raises:
            TypeError: ``value`` is not a ``str``: bytes, ``None``, or another `Secret`.
        """
        if not isinstance(value, str):
            raise TypeError("a Secret holds a string")
        self._value = value

    def reveal(self) -> str:
        """Returns the value.

        Call it where the value is used, and don't keep the result: the plain string can end up
        anywhere.

        Returns:
            The string this `Secret` holds.
        """
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__

    def __bool__(self) -> bool:
        return bool(self._value)

    def __reduce__(self) -> Any:
        # pickling would write the value to wherever the pickle goes
        raise TypeError("a Secret cannot be pickled")


def resolve(value: str | Secret | Callable[[], str | Secret | None] | None, what: str) -> str | None:
    """The plain value of a secret source, or None."""
    if value is None:
        return None
    if isinstance(value, Secret):
        return value.reveal()
    if isinstance(value, str):
        return value
    if callable(value):
        out = value()
        if out is None:
            return None
        if isinstance(out, Secret):
            return out.reveal()
        if not isinstance(out, str):
            raise ConfigurationError(f"the {what} provider returned {type(out).__name__}, not a string")
        return out
    raise ConfigurationError(f"{what} must be a string, a Secret or a callable, got {type(value).__name__}")


def read_env(name: str) -> str | None:
    """`NAME` from the environment, or the contents of the file named by `NAME_FILE`."""
    path = os.environ.get(f"{name}_FILE")
    if path:
        try:
            # editors and `echo` leave a newline at the end, Notepad a byte order mark at
            # the start: neither is part of a token or a passphrase
            return Path(path).read_text(encoding="utf-8-sig").rstrip("\r\n")
        except OSError as exc:
            raise ConfigurationError(f"{name}_FILE points to {path!r}, which cannot be read: {exc.strerror}") from None
    value = os.environ.get(name)
    return value if value else None


def scrub(text: str) -> str:
    text = without_tokens(text)
    return _PRIVATE_BLOCK.sub("[private key removed]", text)


class RedactingFilter(logging.Filter):
    """Removes bot tokens and private key blocks from log records, wherever they hide.

    `install_redaction` puts it on every ``pgpcbot`` logger, and creating a bot calls that.
    It is a safety net, not a licence to log secrets: nothing in pgpcbot logs them on purpose.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format string is not ours to fix here
            return True
        clean = scrub(message)
        if clean != message:
            record.msg = clean
            record.args = None
        # the exception text is the other place a token can ride in: format it now,
        # as the handler would, so there is a string to clean
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = scrub(record.stack_info)
        return True


_LOGGERS = ("pgpcbot", "pgpcbot.bot", "pgpcbot.client", "pgpcbot.crypto", "pgpcbot.engine", "pgpcbot.webhook")
_installed = False


def install_redaction(logger: logging.Logger | None = None) -> None:
    """Puts `RedactingFilter` on `logger`, or on every pgpcbot logger.

    One per logger, not one on the parent: a logger's filters only see the
    records logged on that very logger, never its children's.
    """
    global _installed
    if logger is not None:
        if not any(isinstance(f, RedactingFilter) for f in logger.filters):
            logger.addFilter(RedactingFilter())
        return
    if _installed:
        return
    _installed = True
    shared = RedactingFilter()
    for name in _LOGGERS:
        logging.getLogger(name).addFilter(shared)
