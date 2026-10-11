"""PGPCbot: bots for PGP Chat, in readable and in end-to-end encrypted chats.

PGPCbot is a Python library for the PGP Chat Bot API. It covers every operation of the API,
async and blocking, and it handles the OpenPGP encryption: when a bot has a published key,
messages reach its handlers already decrypted, and its replies are encrypted and signed for
the people who should read them. The handler code is the same either way.

Install it with ``pip install PGPCbot`` (Python 3.11 or newer). Two extras are optional:
``PGPCbot[webhook]`` adds uvicorn to serve a webhook, ``PGPCbot[http2]`` adds HTTP/2.

A bot that answers every message, with its token in ``PGPCHAT_BOT_TOKEN``::

    from pgpcbot import Bot

    bot = Bot()  # reads the token from PGPCHAT_BOT_TOKEN


    @bot.on_message()
    async def echo(message):
        await message.reply(message.text or "...")


    bot.run()  # polls until Ctrl+C

The main names, all importable from ``pgpcbot``:

Bots

- `~pgpcbot.Bot`: a bot whose handlers are ``async def`` functions.
- `~pgpcbot.SyncBot`: the same bot for plain functions, run in a pool of threads.
- `~pgpcbot.Router`: a group of handlers, to split a bot into modules.
- `~pgpcbot.filters`: which updates a handler takes (commands, private chats, text...).

The Bot API

- `~pgpcbot.AsyncClient` and `~pgpcbot.Client`: one method per operation, async or blocking.
- `~pgpcbot.RetryPolicy`: how hard a call tries again after a failure.
- `~pgpcbot.RateLimiter`: paces the calls the way the server counts them.

What handlers get, and what they send

- `~pgpcbot.Message`: a message, already decrypted; ``message.reply(...)`` answers it.
- `~pgpcbot.CallbackQuery`: a press on one of the bot's buttons.
- `~pgpcbot.Update`: anything that happened in a chat, as the server delivered it.
- `~pgpcbot.SentMessage`: a message the bot sent, to edit or delete later.
- `~pgpcbot.InlineKeyboard`, `~pgpcbot.Button`: the buttons under a message.

Keys, storage, webhooks and errors

- `~pgpcbot.BotKey`: the bot's private key; `~pgpcbot.PublicKey`: somebody's public key.
- `~pgpcbot.SQLiteStorage`: a queue of updates on disk that survives restarts.
- `~pgpcbot.WebhookApp`: the ASGI app the server posts updates to.
- `~pgpcbot.Secret`: a string that stays out of logs and ``repr``.
- `~pgpcbot.PGPCbotError`: the base of every error; `~pgpcbot.APIError` is the server's
  refusal, with one subclass per error code.

The ``pgpcbot`` command does the chores that need no running bot: ``pgpcbot keygen``,
``pgpcbot whoami``, ``pgpcbot publish-key`` and more (``pgpcbot --help``).

The full documentation, guides and API reference, is in the ``docs/`` folder of the source,
built with Sphinx: ``pip install -e . --group docs``, then
``sphinx-build -b html docs docs/_build/html``. The API reference is made from these
docstrings, so ``help()`` shows the same text: ``help(pgpcbot.Bot)``.

Security: the token travels only in the ``Authorization`` header, and never shows in
``repr``, exceptions or logs. Keep it and the bot's private key out of your code: read them
from ``PGPCHAT_BOT_TOKEN`` and ``PGPCHAT_BOT_KEY``, or their ``_FILE`` forms. In an encrypted
chat, a message that cannot be encrypted is not sent, and never sent in clear instead. Buttons
are never encrypted. If the token leaks, ``await bot.revoke_token()`` burns it at once.
``SECURITY.md`` explains what the library protects and how to report a problem.
"""

import logging as _logging

from . import filters
from ._http import RetryPolicy
from ._secrets import Secret
from ._version import __version__
from .bot import Bot, SyncBot
from .client import AsyncClient, Client
from .crypto import BotKey, Decrypted, PublicKey, format_fingerprint
from .errors import (
    APIError,
    AuthenticationError,
    BadMarkupError,
    BadRequestError,
    BadWebhookError,
    BlockedByUserError,
    BotPausedError,
    CallbackAnsweredError,
    CallbackExpiredError,
    CallbackNotFoundError,
    ChatIsOpenError,
    ChatNotFoundError,
    ConfigurationError,
    ConflictError,
    CryptoError,
    DecryptionError,
    EncryptionError,
    EncryptionRequiredError,
    EndpointNotFoundError,
    FloodWaitError,
    ForbiddenError,
    InvalidKeyError,
    KeyChangedError,
    MemberNotFoundError,
    MessageNotFoundError,
    NetworkError,
    NotARecipientError,
    NotFoundError,
    NoUserKeyError,
    PassphraseError,
    PayloadError,
    PayloadTooLargeError,
    PGPCbotError,
    PollingConflictError,
    PollingError,
    RateLimitError,
    RequestTimeoutError,
    SendIdConflictError,
    ServerError,
    SignatureError,
    SlowModeError,
    StorageError,
    UnexpectedResponseError,
    UserGoneError,
    UsernameNotFoundError,
    UserNotFoundError,
    ValidationError,
    WebhookActiveError,
    WebhookError,
    WrongBodyKindError,
)
from .filters import ParsedCommand
from .keyboard import Button, InlineKeyboard, KeyboardBuilder
from .models import (
    KEEP,
    AdminRights,
    BotInfo,
    BotStatus,
    CallbackQuery,
    CallbackQueryBase,
    Chat,
    ChatCard,
    ChatKeys,
    ChatKind,
    ChatMembers,
    ChatMemberUpdate,
    ChatMode,
    ChatPeer,
    ChatRef,
    Command,
    CommandScope,
    DeletedMessages,
    HandleKind,
    Limits,
    Media,
    Member,
    Message,
    MessageBase,
    PublishedKey,
    Resolved,
    Seat,
    SeatStatus,
    SentMessage,
    SentMessageBase,
    SignatureStatus,
    SyncCallbackQuery,
    SyncMessage,
    SyncSentMessage,
    Update,
    UpdateKind,
    User,
    UserKey,
    Verification,
    WebhookInfo,
    WebhookSummary,
)
from .ratelimit import RateLimiter
from .router import Router, SkipHandler
from .storage import FailedUpdate, MemoryStorage, SQLiteStorage, Storage
from .webhook import WebhookApp, generate_secret

__all__ = [
    "KEEP",
    "APIError",
    "AdminRights",
    "AsyncClient",
    "AuthenticationError",
    "BadMarkupError",
    "BadRequestError",
    "BadWebhookError",
    "BlockedByUserError",
    "Bot",
    "BotInfo",
    "BotKey",
    "BotPausedError",
    "BotStatus",
    "Button",
    "CallbackAnsweredError",
    "CallbackExpiredError",
    "CallbackNotFoundError",
    "CallbackQuery",
    "CallbackQueryBase",
    "Chat",
    "ChatCard",
    "ChatIsOpenError",
    "ChatKeys",
    "ChatKind",
    "ChatMemberUpdate",
    "ChatMembers",
    "ChatMode",
    "ChatNotFoundError",
    "ChatPeer",
    "ChatRef",
    "Client",
    "Command",
    "CommandScope",
    "ConfigurationError",
    "ConflictError",
    "CryptoError",
    "Decrypted",
    "DecryptionError",
    "DeletedMessages",
    "HandleKind",
    "EncryptionError",
    "EncryptionRequiredError",
    "EndpointNotFoundError",
    "FailedUpdate",
    "FloodWaitError",
    "ForbiddenError",
    "InlineKeyboard",
    "InvalidKeyError",
    "KeyChangedError",
    "KeyboardBuilder",
    "Limits",
    "Media",
    "Member",
    "MemberNotFoundError",
    "MemoryStorage",
    "Message",
    "MessageBase",
    "MessageNotFoundError",
    "NetworkError",
    "NoUserKeyError",
    "NotARecipientError",
    "NotFoundError",
    "PGPCbotError",
    "ParsedCommand",
    "PassphraseError",
    "PayloadError",
    "PayloadTooLargeError",
    "PollingConflictError",
    "PollingError",
    "PublicKey",
    "PublishedKey",
    "Resolved",
    "RateLimitError",
    "RateLimiter",
    "RequestTimeoutError",
    "RetryPolicy",
    "Router",
    "SQLiteStorage",
    "Seat",
    "SeatStatus",
    "Secret",
    "SendIdConflictError",
    "SentMessage",
    "SentMessageBase",
    "ServerError",
    "SignatureError",
    "SignatureStatus",
    "SkipHandler",
    "SlowModeError",
    "Storage",
    "StorageError",
    "SyncBot",
    "SyncCallbackQuery",
    "SyncMessage",
    "SyncSentMessage",
    "UnexpectedResponseError",
    "Update",
    "UpdateKind",
    "User",
    "UserGoneError",
    "UserKey",
    "UserNotFoundError",
    "UsernameNotFoundError",
    "ValidationError",
    "Verification",
    "WebhookActiveError",
    "WebhookApp",
    "WebhookError",
    "WebhookInfo",
    "WebhookSummary",
    "WrongBodyKindError",
    "__version__",
    "filters",
    "format_fingerprint",
    "generate_secret",
]

# a library never prints on its own: the application decides where logs go
_logging.getLogger("pgpcbot").addHandler(_logging.NullHandler())
