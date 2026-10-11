"""The exceptions pgpcbot raises, and how an error answer from the server becomes one.

Every one of them derives from `~pgpcbot.PGPCbotError` and is importable from ``pgpcbot``.
They are grouped by where the problem is, so you can catch as much or as little as you need:

- the setup and the input: `~pgpcbot.ConfigurationError`, `~pgpcbot.ValidationError`;
- the server: `~pgpcbot.APIError`, with one subclass per error code of the Bot API, and
  `~pgpcbot.PayloadError` for an answer this version cannot read;
- no answer at all: `~pgpcbot.NetworkError`;
- OpenPGP: `~pgpcbot.CryptoError` and its subclasses;
- running the bot: `~pgpcbot.PollingError`, `~pgpcbot.StorageError`, `~pgpcbot.WebhookError`.

An error answer is matched on the stable ``code`` of its envelope
(`~pgpcbot.errors.error_from_envelope` does it). A code this version does not know yet still
gets the class of its HTTP status: `~pgpcbot.NotFoundError` for a ``404``,
`~pgpcbot.ServerError` for a ``5xx``, and so on.

No exception carries the token, a passphrase or private key material. Text shaped like a bot
token in an error from the server is masked.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

__all__ = [
    "APIError",
    "AuthenticationError",
    "BadMarkupError",
    "BadRequestError",
    "BadWebhookError",
    "BlockedByUserError",
    "BotPausedError",
    "CallbackAnsweredError",
    "CallbackExpiredError",
    "CallbackNotFoundError",
    "ChatIsOpenError",
    "ChatNotFoundError",
    "ConfigurationError",
    "ConflictError",
    "CryptoError",
    "DecryptionError",
    "EncryptionError",
    "EncryptionRequiredError",
    "EndpointNotFoundError",
    "FloodWaitError",
    "ForbiddenError",
    "InvalidKeyError",
    "KeyChangedError",
    "MemberNotFoundError",
    "MessageNotFoundError",
    "NetworkError",
    "NoUserKeyError",
    "NotARecipientError",
    "NotFoundError",
    "PGPCbotError",
    "PassphraseError",
    "PayloadError",
    "PayloadTooLargeError",
    "PollingConflictError",
    "PollingError",
    "RateLimitError",
    "RequestTimeoutError",
    "SendIdConflictError",
    "ServerError",
    "SignatureError",
    "SlowModeError",
    "StorageError",
    "UnexpectedResponseError",
    "UserGoneError",
    "UserNotFoundError",
    "UsernameNotFoundError",
    "ValidationError",
    "WebhookActiveError",
    "WebhookError",
    "WrongBodyKindError",
    "error_from_envelope",
]


# what a token looks like anywhere in a string. Error text may come from a proxy
# page that echoes the request, Authorization header included
TOKEN_SHAPE = re.compile(r"pgpbot_[0-9]+\.[A-Za-z0-9_-]{20,}")


def without_tokens(text: str) -> str:
    return TOKEN_SHAPE.sub("pgpbot_****", text)


class PGPCbotError(Exception):
    """Base class of every error pgpcbot raises on purpose.

    ``except PGPCbotError`` catches what can go wrong at run time: the setup, the server, the
    network, OpenPGP, the storage. It is never raised itself. A mistake in how the library is
    called, such as an argument of the wrong type, can raise a plain `TypeError` or
    `ValueError` instead.
    """


class ConfigurationError(PGPCbotError, ValueError):
    """The bot, a client or a key is set up wrong.

    Raised before anything is sent: no token, or one that is not shaped like a token (the
    error never repeats it); an ``api_url`` that is not usable, or plain ``http`` to a host
    that is not local without ``allow_insecure_http=True``; an option out of range; a key file
    that cannot be read; a handler of the wrong kind (a plain ``def`` on `Bot`, an
    ``async def`` on `SyncBot`); a webhook app without a secret. Retrying never helps: fix the
    setup. It is also a `ValueError`.
    """


class ValidationError(PGPCbotError, ValueError):
    """A request refused before it left, because the server would refuse it too.

    Every method that calls the API first checks its input against the server's rules: text
    length, keyboards, IDs, URLs, the webhook secret, and so on. When a check fails, nothing is
    sent and this error says why, so retrying the same call fails the same way. The only check
    made after a request went out is the size limit of `AsyncClient.download_media`
    (``max_bytes``). It is also a `ValueError`.

    Attributes:
        field (str | None): The parameter at fault, as the method names it (``"text"``,
            ``"markup"``, ``"send_id"``...), or ``None`` when no single parameter is to blame.

    Example:
        A text over 4096 characters is refused before it is sent, with ``field`` set to
        ``"text"``::

            try:
                await message.reply(report)
            except ValidationError as exc:
                log.warning("not sent: %s (%s)", exc, exc.field)
    """

    def __init__(self, message: str, *, field: str | None = None) -> None:
        """Creates the error.

        Args:
            message: What is wrong, for a person.
            field: The parameter at fault, if there is one.
        """
        super().__init__(message)
        self.field = field


# --- the server said no ---


class APIError(PGPCbotError):
    """The server answered with an error.

    Every error answer of the Bot API carries a stable ``code``, an HTTP status and a message.
    The client raises the subclass that belongs to the code, so you can catch the one case you
    can do something about (`BlockedByUserError`, `ChatNotFoundError`...) or every refusal at
    once with ``except APIError``. A code this version does not know yet still gets the class
    of its status (`NotFoundError` for any ``404``, `ServerError` for any ``5xx``...).

    Branch on the class or on ``code``: both are stable. ``message`` is for a person reading a
    log and may change between server versions. ``str(error)`` reads
    ``code (status): message [METHOD path]``.

    Whether a retry helps depends on the subclass: after ``retry_after`` seconds for a
    `RateLimitError`, later for a `ServerError`. The others fail the same way until something
    changes.

    Attributes:
        status (int): The HTTP status, such as ``403``.
        code (str): The server's error code, such as ``"blocked_by_user"``.
            ``"http_<status>"`` when the answer had none.
        message (str): The server's explanation, for people; the code when there was none.
            Text shaped like a bot token is masked.
        retry_after (float | None): Seconds to wait before trying again, on a ``429``.
            ``None`` otherwise.
        expected (str | None): ``"text"`` or ``"armored"``: the body the chat takes, on
            ``wrong_body_kind`` (and on a ``bad_request`` for a send with no body).
            ``None`` otherwise.
        method (str | None): The HTTP method of the request, such as ``"POST"``.
        path (str | None): The path of the request, without the host and ``/bot/v1``, such
            as ``"/messages"``.
        payload (dict): The whole ``error`` object of the answer, for fields this version
            does not name.

    Examples:
        Catch the cases you can do something about, and log the rest::

            try:
                await bot.send_message(user_id, "Your order has shipped.")
            except (BlockedByUserError, UserGoneError):
                subscribers.discard(user_id)
            except APIError as exc:
                log.warning("not sent to %s: %s", user_id, exc)

        A test can build one by hand:

        >>> from pgpcbot import BlockedByUserError
        >>> error = BlockedByUserError(
        ...     "blocked", status=403, code="blocked_by_user", method="POST", path="/messages"
        ... )
        >>> str(error)
        'blocked_by_user (403): blocked [POST /messages]'
    """

    def __init__(
        self,
        message: str,
        *,
        status: int,
        code: str,
        retry_after: float | None = None,
        expected: str | None = None,
        method: str | None = None,
        path: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Creates the error. The client does this for you; tests can do it by hand.

        Args:
            message: The explanation. Text shaped like a bot token is masked.
            status: The HTTP status.
            code: The error code.
            retry_after: Seconds to wait, for a ``429``.
            expected: ``"text"`` or ``"armored"``, for ``wrong_body_kind``.
            method: The HTTP method of the request.
            path: The path of the request.
            payload: The ``error`` object as it came. Its top-level strings shaped like a bot
                token are masked too.
        """
        message = without_tokens(message)
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.expected = expected
        self.method = method
        self.path = path
        # the error object as it came, for fields this version doesn't know
        self.payload = {k: without_tokens(v) if isinstance(v, str) else v for k, v in (payload or {}).items()}

    def __str__(self) -> str:
        where = f" [{self.method} {self.path}]" if self.method and self.path else ""
        return f"{self.code} ({self.status}): {self.message}{where}"

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status={self.status}, code={self.code!r}, message={self.message!r})"


class BadRequestError(APIError):
    """400: the body or the query of the request is wrong.

    With the code ``bad_request``, ``message`` says which field. The client checks the rules it
    knows before sending (a `ValidationError`); this comes when the server refuses anyway. Its
    subclasses cover the other ``400`` codes. Retrying the same request fails the same way.
    """


class WrongBodyKindError(BadRequestError):
    """400 ``wrong_body_kind``: the body does not fit the chat's mode.

    The request sent ``text`` into an end-to-end encrypted chat, or ``armored`` into a readable
    one, usually because the chat changed mode since the bot last looked. ``expected`` says
    which body the chat takes now.

    `Bot` deals with it in sends and edits. When a readable chat became encrypted, it seals the
    text and sends it again, a send with the same ``sendId`` (the words did reach the server in
    the refused request, so a warning is logged). When an encrypted chat became readable, it
    raises this error instead of sending in clear, and the next send uses the new mode. With
    `AsyncClient` and `Client`, send the other kind of body.

    Attributes:
        expected (str | None): ``"text"`` or ``"armored"``: the body the chat takes now.
            ``None`` when the server did not say.
    """


class BadMarkupError(BadRequestError):
    """400 ``bad_markup``: the inline keyboard breaks one of the server's rules.

    The client checks keyboards before sending (a `ValidationError` with ``field``
    ``"markup"``); this comes when the server refuses one anyway. Retrying the same keyboard
    fails the same way.
    """


class BadWebhookError(BadRequestError):
    """400 ``bad_webhook``: the server refused the webhook URL.

    For example it is not ``https``, or it resolves to a private address. The client checks the
    shape of the URL before sending (a `ValidationError`); the rest only the server can judge.
    Use another URL.
    """


class ChatIsOpenError(BadRequestError):
    """400 ``chat_is_open``: keys were asked for a readable chat.

    A readable chat has no keys: send ``text`` there. `Bot` gets it when a chat stopped being
    encrypted while it was sealing a message; it raises instead of sending in clear, and the
    next send uses the new mode.
    """


class PayloadTooLargeError(APIError):
    """413 ``too_large``: the request body was over 256 KiB.

    Retrying the same body fails the same way.
    """


class AuthenticationError(APIError):
    """401 ``unauthorized``: the token is missing, revoked, replaced or invalid.

    Any call can raise it, and a bot stops with it. Retrying never helps: the owner has to make
    a new token in the console. A token that is not even shaped like one fails earlier, with a
    `ConfigurationError`, before anything is sent.
    """


class ForbiddenError(APIError):
    """403: the bot has no right to do that here.

    With the code ``forbidden``: for example a send where the bot may not write, or an edit or
    delete of a message that is not the bot's own. Its subclasses cover the other ``403``
    codes. Retrying fails the same way until the bot's rights change.
    """


class BotPausedError(ForbiddenError):
    """403 ``bot_paused``: the owner paused the bot from the console.

    Nothing goes in or out until the owner resumes it. Meanwhile only ``get_me``,
    ``get_webhook``, ``delete_webhook`` and ``revoke_token`` work. A polling bot does not stop:
    it logs it once and checks again every 30 seconds. Elsewhere, retrying helps only once the
    bot is resumed.
    """


class BlockedByUserError(ForbiddenError):
    """403 ``blocked_by_user``: the person blocked the bot.

    Raised by calls into the private chat with them, such as sends and edits. Stop writing to
    them: retrying fails until they unblock the bot.
    """


class UserGoneError(ForbiddenError):
    """403 ``user_gone``: the person closed their account.

    Raised by calls into the private chat with them. Retrying never helps: remove them from
    your lists.
    """


class NotFoundError(APIError):
    """404: something named in the request does not exist, or is not the bot's to see.

    Its subclasses say what was not found. A ``404`` with a code this version does not know is
    a plain `NotFoundError`, and so is a media file that is gone (``code`` is
    ``"media_not_found"``, from `AsyncClient.download_media`). Retrying fails the same way.
    """


class EndpointNotFoundError(NotFoundError):
    """404 ``not_found``: the server has no such path.

    Usually ``api_url`` (or ``PGPCHAT_API``) points to the wrong server: use the API address the
    console shows. Retrying does not help.
    """


class ChatNotFoundError(NotFoundError):
    """404 ``chat_not_found``: no such chat, or the bot is not in it.

    The server gives the same answer for both, so a bot that was removed from a group gets
    this too.
    """


class MessageNotFoundError(NotFoundError):
    """404 ``message_not_found``: no such message in that chat.

    On a delete it means the message is already gone. When a delete timed out and the retry
    gets this answer, the first try did delete it, and the client returns ``True`` instead of
    raising.
    """


class MemberNotFoundError(NotFoundError):
    """404 ``member_not_found``: not a member of the chat, or hidden from the bot.

    Where the member list is hidden, a bot that is not an admin sees only the admins, and gets
    this error for everybody else.
    """


class UserNotFoundError(NotFoundError):
    """404 ``user_not_found``: no such person, or nobody the bot shares a chat with.

    The bot can only look up people it shares a chat with. For anybody else the server answers
    this, whether or not they exist.
    """


class UsernameNotFoundError(NotFoundError):
    """404 ``username_not_found``: nobody and no chat answers to that handle.

    From `AsyncClient.resolve`. It means the handle is free, held by a chat with no public
    name, or held by an account that has been closed - the answer does not say which.
    """


class NoUserKeyError(NotFoundError):
    """404 ``no_user_key``: the person never published a public key.

    From `AsyncClient.get_user_key`, and from `AsyncClient.get_chat_keys` for a private chat
    whose person has no key. Nothing can be sealed to them until they publish one.
    """


class CallbackNotFoundError(NotFoundError):
    """404 ``callback_not_found``: no such button press for this bot."""


class ConflictError(APIError):
    """409: the request clashes with the state the server is in.

    Its subclasses say what the clash is. A ``409`` with a code this version does not know is a
    plain `ConflictError`.
    """


class SendIdConflictError(ConflictError):
    """409 ``conflict``: this ``send_id`` was already used for a message in another chat.

    The same ``send_id`` again in the same chat is not an error: the server returns the
    message it already wrote. Leave ``send_id`` out and the library makes a new one for each
    message.
    """


class WebhookActiveError(ConflictError):
    """409 ``webhook_active``: polling while a webhook is set.

    The server refuses ``get_updates`` while the bot has a webhook. Delete the webhook first
    (``bot.run(drop_webhook=True)`` does it at start), or take the updates on the webhook. A
    polling bot turns this into a `PollingError`.
    """


class CallbackAnsweredError(ConflictError):
    """409 ``callback_answered``: that press was answered already.

    Each press can be answered once, so this is safe to ignore. When an answer timed out and
    the retry gets this, the first try went through, and the client returns ``True`` instead of
    raising.
    """


class CallbackExpiredError(APIError):
    """410 ``callback_expired``: more than 60 seconds went by since the press.

    A press can only be answered within 60 seconds: this one is too late, and retrying fails
    the same way. A ``410`` with a code this version does not know also gets this class.
    """


class RateLimitError(APIError):
    """429: too many requests, after the client waited as much as its `RetryPolicy` allows.

    The client waits out a ``429`` on its own, as long as the server asks, up to
    ``rate_limit_retries`` times per call. It raises at once when the server asks for more
    than ``max_rate_limit_wait`` seconds. Wait ``retry_after`` seconds before trying again: the
    server's counter does not move while you wait, so waiting exactly that long is enough.

    With the code ``rate_limited`` it means too many API calls of any kind (600 a minute by
    default). The subclasses are about messages: `FloodWaitError` and `SlowModeError`. A
    ``429`` with a code this version does not know is a plain `RateLimitError`.

    Attributes:
        retry_after (float | None): Seconds to wait. For ``rate_limited`` it is taken from the
            ``Retry-After`` header, which has the exact time left; for the other codes from the
            body, or from the header when the body has none. ``None`` if the server sent
            neither.
    """


class FloodWaitError(RateLimitError):
    """429 ``flood_wait``: too many messages, into one chat or overall.

    By default the server takes one message a second into a private chat, twenty a minute into
    a group or channel, and thirty a second in all; edits are counted apart, with the same
    numbers. The client paces itself the same way (`RateLimiter`), so this comes mostly when
    another process uses the same token. Wait ``retry_after`` seconds.
    """


class SlowModeError(RateLimitError):
    """429 ``slow_mode``: the group has slow mode on.

    Its admins set a time everybody, bots included, must wait between two messages.
    ``retry_after`` says how long until the bot may write again. The client's own pacing does
    not know about slow mode.
    """


class ServerError(APIError):
    """5xx: the server failed.

    It has the code ``internal``, or comes from any other ``5xx`` answer, with or without the
    API's envelope. The client already tried again for the calls that are safe to repeat
    (`RetryPolicy.attempts` tries in all), and a polling bot keeps trying on its own. Worth
    retrying later.
    """


class UnexpectedResponseError(APIError):
    """The answer was not the API's JSON envelope.

    For example an HTML page from a proxy, a body cut short, plain text or nothing at all,
    with a status below 500 (with a ``5xx`` status it is a `ServerError`). ``code`` is
    ``"http_<status>"``, and ``message`` quotes the start of the body. The client does not
    retry it. Check that ``api_url`` points to the Bot API and that no proxy in between
    answers in its place.
    """


class PayloadError(PGPCbotError, ValueError):
    """A JSON object from the server is missing something this version cannot do without.

    Raised while an answer or an update is read into a model, for example a message without a
    date. Retrying gets the same answer: update PGPCbot. It is also a `ValueError`.
    """


_BY_CODE: dict[str, type[APIError]] = {
    "bad_request": BadRequestError,
    "wrong_body_kind": WrongBodyKindError,
    "bad_markup": BadMarkupError,
    "bad_webhook": BadWebhookError,
    "chat_is_open": ChatIsOpenError,
    "too_large": PayloadTooLargeError,
    "unauthorized": AuthenticationError,
    "bot_paused": BotPausedError,
    "forbidden": ForbiddenError,
    "blocked_by_user": BlockedByUserError,
    "user_gone": UserGoneError,
    "not_found": EndpointNotFoundError,
    "chat_not_found": ChatNotFoundError,
    "message_not_found": MessageNotFoundError,
    "member_not_found": MemberNotFoundError,
    "user_not_found": UserNotFoundError,
    "username_not_found": UsernameNotFoundError,
    "no_user_key": NoUserKeyError,
    "callback_not_found": CallbackNotFoundError,
    "conflict": SendIdConflictError,
    "webhook_active": WebhookActiveError,
    "callback_answered": CallbackAnsweredError,
    "callback_expired": CallbackExpiredError,
    "rate_limited": RateLimitError,
    "flood_wait": FloodWaitError,
    "slow_mode": SlowModeError,
    "internal": ServerError,
}

_BY_STATUS: dict[int, type[APIError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: ForbiddenError,
    404: NotFoundError,
    409: ConflictError,
    410: CallbackExpiredError,
    413: PayloadTooLargeError,
    429: RateLimitError,
}


def _number(value: Any) -> float | None:
    # bool is an int, and a "retryAfter": true would otherwise read as one second
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 else None


def error_from_envelope(
    error: Mapping[str, Any],
    *,
    http_status: int,
    method: str | None = None,
    path: str | None = None,
    retry_after_header: float | None = None,
) -> APIError:
    """Builds the right `~pgpcbot.APIError` subclass from the ``error`` object of an answer.

    The clients call it for every error answer. It is public for code that reads the Bot API's
    answers some other way, such as a test double. The class comes from ``code``; a code this
    version does not know falls back on the status: `~pgpcbot.ServerError` for any ``5xx``,
    the class of the status for ``400``, ``401``, ``403``, ``404``, ``409``, ``410``, ``413``
    and ``429``, and a plain `~pgpcbot.APIError` for anything else.

    Missing or odd values never make it fail: no status means ``http_status``, no code means
    ``"http_<status>"``, no message means the code. Text shaped like a bot token is masked.

    Args:
        error: The ``error`` object of the answer, as decoded from JSON.
        http_status: The status on the HTTP line, used when ``error`` has none.
        method: The HTTP method of the request, kept in the error.
        path: The path of the request, kept in the error.
        retry_after_header: The ``Retry-After`` header in seconds, if the answer had one. It is
            used when the body has no ``retryAfter``, and always for ``rate_limited``, where the
            header has the exact time left.

    Returns:
        The error, ready to raise. Nothing is raised here.

    Examples:
        >>> from pgpcbot.errors import error_from_envelope
        >>> error = error_from_envelope(
        ...     {"code": "flood_wait", "message": "too many messages", "retryAfter": 3},
        ...     http_status=429,
        ...     method="POST",
        ...     path="/messages",
        ... )
        >>> type(error).__name__, error.retry_after
        ('FloodWaitError', 3.0)
        >>> str(error)
        'flood_wait (429): too many messages [POST /messages]'

        A code this version does not know gets the class of its status:

        >>> error_from_envelope({"code": "brand_new", "message": "?"}, http_status=404)
        NotFoundError(status=404, code='brand_new', message='?')
    """
    status = error.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        status = http_status
    code = error.get("code")
    if not isinstance(code, str) or not code:
        code = f"http_{status}"
    message = error.get("message")
    if not isinstance(message, str):
        message = code

    cls = _BY_CODE.get(code)
    if cls is None:
        cls = ServerError if status >= 500 else _BY_STATUS.get(status, APIError)

    retry_after = _number(error.get("retryAfter"))
    # the per-minute limiter always writes 60 in the body; its Retry-After
    # header carries the real time left in the window
    if retry_after is None or (code == "rate_limited" and retry_after_header is not None):
        retry_after = retry_after_header if retry_after_header is not None else retry_after
    expected = error.get("expected") if error.get("expected") in ("text", "armored") else None
    return cls(
        message,
        status=status,
        code=code,
        retry_after=retry_after,
        expected=expected,
        method=method,
        path=path,
        payload=dict(error),
    )


# --- the line, not the server ---


class NetworkError(PGPCbotError):
    """The request got no answer: DNS, a refused or reset connection, TLS, a timeout.

    The client already tried again, up to `RetryPolicy.attempts` tries in all, for the calls
    that are safe to repeat: every operation except ``get_updates`` and ``send_typing``. Media
    downloads are not retried. A polling bot keeps trying on its own. An answer larger than
    8 MiB is a `NetworkError` too, and is not retried: asking again would only download it
    again. The message names the request and the cause, never the token.
    """


class RequestTimeoutError(NetworkError):
    """No answer came in time.

    The limit is the ``timeout`` of the bot or the client: 30 seconds by default, 10 to
    connect; long polls get extra time. Like any `NetworkError` it was already retried when the
    call is safe to repeat. A send that timed out may still have been written. To be able to
    try it again yourself, give it a ``send_id`` and pass the same one again: the server then
    returns the message it already wrote instead of writing it twice.
    """


# --- webhooks ---


class WebhookError(PGPCbotError):
    """A delivery that reached the webhook could not be read as an update.

    Raised while the body of a delivery is read: it is not JSON, not a JSON object, or has no
    ``updateId`` that is a non-negative integer. `WebhookApp` catches it itself: a delivery with
    the right secret comes from the server, so it is logged and answered ``200``, because any
    other answer would make the server send it again for days and hold back every later
    update. It does not reach your code. Retrying the same body fails the same way.

    Attributes:
        status (int): The HTTP status that fits the problem, ``400`` unless said otherwise.
    """

    def __init__(self, message: str, *, status: int = 400) -> None:
        """Creates the error.

        Args:
            message: What is wrong with the delivery.
            status: The HTTP status that fits the problem.
        """
        super().__init__(message)
        # what the endpoint answers the server with
        self.status = status


# --- OpenPGP ---


class CryptoError(PGPCbotError):
    """Something in the OpenPGP layer went wrong.

    The base class of the OpenPGP errors: catch it to handle all of them at once. It is raised
    as itself only in a rare case: a private key loaded from text that is not UTF-8, which
    then cannot be armored again.
    """


class InvalidKeyError(CryptoError):
    """A key that cannot be read, or cannot do what it is asked to.

    Raised when a key is loaded or used: text that is not an OpenPGP key, a public key where
    the bot needs its private key (or the other way round), a private key the bot cannot use
    (it needs a signing key and an encryption subkey, neither expired nor revoked), a key whose
    fingerprint is not the one the server lists for it (`PublicKey.from_user_key`), or a v6 key
    to publish, which phones on PGPainless 1.7 cannot use. Retrying with the same key fails the
    same way.
    """


class PassphraseError(CryptoError):
    """The private key is locked, and the passphrase is missing or wrong.

    Raised when a key is loaded (`BotKey.from_file` and the other loaders), so a bot with a
    wrong passphrase fails when it is created, not at the first message. Check
    ``pgp_passphrase`` or ``PGPCHAT_BOT_KEY_PASSPHRASE``; retrying with the same passphrase
    fails the same way.
    """


class EncryptionError(CryptoError):
    """A message could not be encrypted. It was **not** sent, in clear or otherwise.

    `Bot` and `SyncBot` raise it when they have to seal a message for an encrypted chat and
    cannot: the bot has no private key loaded, nobody in the chat has a key to seal to, the key
    of the person in a private chat cannot be used (revoked, expired, v6, or with no
    encryption subkey), or the sealing failed. `BotKey.seal` raises it for a recipient key
    that cannot be used, and `BotKey.export_secret` and `BotKey.save` when they cannot seal the
    key with a passphrase. Retrying fails the same way until the cause is fixed.
    """


class EncryptionRequiredError(EncryptionError):
    """The chat is readable, and this message had to be encrypted. It was not sent.

    Raised by the sends of `Bot` and `SyncBot` when the chat is not end-to-end encrypted, and
    either the bot was made with ``require_encryption=True`` or the call passed
    ``encrypt=True``. It is the bot keeping its promise, not a fault: retrying sends nothing
    until the chat becomes encrypted.
    """


class DecryptionError(CryptoError):
    """An encrypted message or attachment could not be opened.

    For example: the bot has no private key loaded, the message was altered on the way (the
    integrity check failed), it is not a well-formed OpenPGP message, it opens to more than the
    size allowed (1 MiB for a message, ``max_bytes`` for an attachment), or it is a compressed
    attachment, which phones never send. Loading a key file sealed with a passphrase raises it
    too when the file cannot be opened; a wrong passphrase is a `PassphraseError`.

    In an encrypted chat, an incoming message that fails this way does not reach your
    handlers: the error goes to the error handlers, and the update to the failed list if none
    of them takes it. Retrying opens the same bytes the same way.
    """


class NotARecipientError(DecryptionError):
    """The message was not sealed to any key the bot has loaded.

    Usually it was written to an old key after a rotation (keep the old key in
    ``pgp_previous_keys`` for 72 hours), the bot runs with a key that is not the published one
    (``pgpcbot whoami`` shows the published fingerprint), or it is an attachment in an
    encrypted group: phones seal those only to the group's key list, which leaves out bots in
    privacy mode. Retrying does not help: load the right key.
    """


class SignatureError(CryptoError):
    """A message without a valid signature, refused because of ``verify_signatures="require"``.

    The signature is missing, wrong, or made by a key that is not the sender's. The message
    does not reach your handlers: the error goes to the error handlers, and the update to the
    failed list if none of them takes it. With the default, ``"auto"``, this is never raised:
    the result is in ``message.verification`` instead. Retrying gives the same result.
    """


class KeyChangedError(CryptoError):
    """A person's key changed while key pinning is on (``pin_keys=True``).

    With pinning on, the bot remembers the first key fingerprint it sees for each person. When
    the server later gives a different key for them, the bot refuses it both ways: sealing a
    message for a chat they are in raises this error, and their incoming messages go to the
    error handlers with it instead of reaching your handlers (they wait in the failed list).

    Encryption alone cannot tell a key the person changed from a key someone swapped on the
    server. Check the new fingerprint with the person over another channel, then call
    `Bot.trust_key`, and `Bot.retry_failed` for the messages that waited. Until then, retrying
    fails the same way.

    Attributes:
        user_id (int): The person whose key changed.
        pinned (str): The fingerprint the bot remembered, in hex, upper case.
        current (str): The fingerprint the server gives now, in hex, upper case.

    Example:
        Log it and keep the update in the failed list until the key is checked::

            @bot.on_error(KeyChangedError)
            async def key_changed(error, update):
                log.warning(
                    "user %s has a new key %s, was %s", error.user_id, error.current, error.pinned
                )
                raise error
    """

    def __init__(self, message: str, *, user_id: int, pinned: str, current: str) -> None:
        """Creates the error.

        Args:
            message: What happened, for a person.
            user_id: The person whose key changed.
            pinned: The fingerprint the bot remembered.
            current: The fingerprint the server gives now.
        """
        super().__init__(message)
        self.user_id = user_id
        self.pinned = pinned
        self.current = current


# --- local state ---


class PollingError(PGPCbotError):
    """Polling cannot start or go on, because a webhook is set.

    The server refuses to be polled while the bot has a webhook. `Bot.run`,
    `Bot.start_polling` and `SyncBot.run` check it at start, and raise this error too when a
    webhook is set while they poll (the server then answers ``409 webhook_active``). Retrying
    does not help: start with ``run(drop_webhook=True)``, delete the webhook
    (``pgpcbot webhook delete``), or take the updates on the webhook instead.
    """


class StorageError(PGPCbotError):
    """The bot's storage failed: queued updates, failed updates or pinned keys.

    `SQLiteStorage` raises it when its file cannot be opened as a database, when SQLite reports
    an error, or when it is used before the bot opened it; both storages for an update without
    a valid ``updateId``. A polling bot does not confirm a batch it could not store: it waits,
    and the server sends the batch again.
    """


class PollingConflictError(StorageError):
    """Another bot is already polling with the same storage.

    Raised when polling starts. With `SQLiteStorage` the bot holds an operating-system lock on
    the file, so a second process with the same file fails here, and the message gives the
    process ID of the first one when it can. With `MemoryStorage` only a second bot in the same
    process, with the same storage object, is caught. Two pollers would take each other's
    updates: stop one. To handle more work, raise ``concurrency``, or use a webhook.
    """
