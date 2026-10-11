"""The parts of an API call that do not care whether the client is sync or async.

A call is described once (``Call``): method, path, query, body, how to read the result, and
whether it is safe to send twice. The two clients only differ in how they put it on the wire
and how they wait. The rest is shared the same way: reading the token and the API address,
turning an answer's envelope into a result or the right `~pgpcbot.APIError`,
`~pgpcbot.RetryPolicy`, and the bookkeeping of one call's retries (``Attempts``).

Internal: only `~pgpcbot.RetryPolicy` is public, importable from ``pgpcbot``. Everything else
here can change in any version.
"""

from __future__ import annotations

import json
import platform
import random
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Generic, TypeVar
from urllib.parse import urlsplit

from ._version import __version__
from .errors import (
    APIError,
    ConfigurationError,
    NetworkError,
    RateLimitError,
    ServerError,
    UnexpectedResponseError,
    error_from_envelope,
    without_tokens,
)
from .models import ChatKind

T = TypeVar("T")

DEFAULT_API_URL = "https://api.pgpchat.app"
API_PREFIX = "/bot/v1"
ENV_API_URL = "PGPCHAT_API"
ENV_TOKEN = "PGPCHAT_BOT_TOKEN"  # noqa: S105 - the name of the variable, not a token

TOKEN_RE = re.compile(r"pgpbot_([1-9][0-9]{0,15})\.([A-Za-z0-9_-]{43})")

# the server takes 256 KiB of JSON; this is how much answer we are willing to read
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def user_agent() -> str:
    return f"PGPCbot/{__version__} (Python {platform.python_version()})"


def parse_token(token: str) -> int:
    """The bot id a token belongs to. Raises without ever echoing the token back."""
    if not isinstance(token, str):
        raise ConfigurationError("the token must be a string")
    m = TOKEN_RE.fullmatch(token.strip())
    if m is None:
        hint = "it should look like pgpbot_<id>.<43 characters>"
        if token.strip().lower().startswith("bearer "):
            hint = "pass the token alone, without 'Bearer '"
        raise ConfigurationError(f"that is not a bot token: {hint}")
    return int(m.group(1))


def mask_token(token: object) -> str:
    """`pgpbot_412.****3f2a`: the id and the last four, like the console's hint."""
    m = TOKEN_RE.fullmatch(token.strip()) if isinstance(token, str) else None
    if m is None:
        return "pgpbot_****"
    return f"pgpbot_{m.group(1)}.****{m.group(2)[-4:]}"


def normalize_api_url(url: str, *, allow_insecure_http: bool = False) -> str:
    """Scheme and host of the API, without the `/bot/v1` part and the trailing slash."""
    raw = (url or "").strip().rstrip("/")
    if raw.endswith(API_PREFIX):
        raw = raw[: -len(API_PREFIX)].rstrip("/")
    try:
        parts = urlsplit(raw)
        port = parts.port  # a junk port raises here rather than on the first request
    except ValueError as exc:
        raise ConfigurationError(f"api_url is not a URL: {exc}") from None
    del port
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ConfigurationError(f"api_url must be like https://api.example.org, got {url!r}")
    if parts.username or parts.password:
        raise ConfigurationError("api_url must not carry credentials")
    if parts.query or parts.fragment:
        raise ConfigurationError("api_url must not carry a query or a fragment")
    if parts.scheme == "http" and parts.hostname not in _LOCAL_HOSTS and not allow_insecure_http:
        # the token rides in a header: over plain http anybody on the path can lift it
        raise ConfigurationError(
            "api_url uses plain http: the token would travel in clear. Use https, "
            "or pass allow_insecure_http=True for a test server on your own network"
        )
    return raw


def retry_after_header(headers: Mapping[str, str]) -> float | None:
    value = headers.get("retry-after")
    if not value:
        return None
    value = value.strip()
    # isdigit() alone takes things like a stray superscript two, which float() then refuses
    if value.isascii() and value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How hard a client tries again before it gives up.

    Pass one as ``retry=`` to `Bot`, `SyncBot`, `AsyncClient` or `Client`; without it they use
    ``RetryPolicy()``. The fields below are the constructor's keyword arguments. A policy is
    frozen, so one can be shared. ``RetryPolicy(attempts=1, rate_limit_retries=0)`` turns
    retries off.

    Two kinds of failure are tried again, each with its own budget:

    - No answer, or a ``5xx`` (a `NetworkError` or a `ServerError`), for the calls that are
      safe to repeat: every operation except ``get_updates`` and ``send_typing``. Sends are safe
      because each carries a ``sendId``. The wait before retry ``n`` (``0`` for the first) is
      random, between 0 and ``min(backoff_max, backoff_base * 2**n)`` seconds ("full jitter"),
      so clients that failed together don't all come back at the same moment.
    - A ``429``, for any call, since the server refused it before doing anything. The client
      waits as long as the server asks (``retry_after``, or one second when it gives none),
      plus a fraction of a second.

    Other ``4xx`` answers are never retried, and media downloads are not retried at all. When a
    budget runs out, the last error is raised.

    Attributes:
        attempts: Tries in total, the first one included, for a call that gets no answer or a
            ``5xx``. ``1`` means no retry. Tries that ended in a ``429`` do not count.
        backoff_base: The longest wait before the first retry, in seconds. It doubles at each
            retry.
        backoff_max: The longest wait between two tries, in seconds.
        rate_limit_retries: How many ``429`` answers one call waits out. The next one is
            raised as a `RateLimitError`; ``0`` raises the first.
        max_rate_limit_wait: A ``429`` that asks for a longer wait than this, in seconds, is
            raised at once as a `RateLimitError` instead of waited out.

    Raises:
        ValueError: ``attempts`` is below ``1``, or ``rate_limit_retries``, ``backoff_base`` or
            ``backoff_max`` is negative.

    Example:
        >>> from pgpcbot import RetryPolicy
        >>> policy = RetryPolicy(attempts=6, backoff_max=30.0)
        >>> policy.attempts, policy.backoff_base, policy.rate_limit_retries
        (6, 0.5, 5)
        >>> 0.0 <= policy.backoff(3) <= 4.0
        True
        >>> RetryPolicy(attempts=0)
        Traceback (most recent call last):
            ...
        ValueError: attempts must be at least 1
    """

    attempts: int = 4
    backoff_base: float = 0.5
    backoff_max: float = 20.0
    rate_limit_retries: int = 5
    max_rate_limit_wait: float = 90.0

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")
        if self.rate_limit_retries < 0 or self.backoff_base < 0 or self.backoff_max < 0:
            raise ValueError("retry settings must not be negative")

    def backoff(self, retry: int) -> float:
        """Picks the wait before a retry after no answer or a ``5xx``.

        The wait is random, between 0 and ``min(backoff_max, backoff_base * 2**retry)``
        seconds ("full jitter").

        Args:
            retry: Which retry this is, counting from ``0`` for the first.

        Returns:
            A number of seconds, never more than ``backoff_max``.
        """
        ceiling = min(self.backoff_max, self.backoff_base * (2**retry))
        return random.uniform(0, ceiling)


NO_RETRY = RetryPolicy(attempts=1, rate_limit_retries=0)


@dataclass(frozen=True, slots=True)
class Call(Generic[T]):
    """One API operation, ready to be sent by either client."""

    method: str
    path: str
    parse: Callable[[Any], T]
    params: dict[str, Any] | None = None
    body: Any = None
    has_body: bool = False
    # safe to send again after the first answer got lost (idempotent, or carries a sendId)
    repeatable: bool = True
    # codes that mean "already done" when they answer a repeat whose first try got lost
    done_codes: frozenset[str] = frozenset()
    done_value: Any = None
    # long polls need a read timeout longer than the poll itself
    read_timeout: float | None = None
    # what the rate limiter needs to know
    send: bool = False
    edit: bool = False
    chat_id: int | None = None
    chat_kind: ChatKind | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def url(self, base: str) -> str:
        return f"{base}{API_PREFIX}{self.path}"


def read_envelope(call: Call[Any], status: int, headers: Mapping[str, str], content: bytes) -> Any:
    """The `result` of a success, or the right `APIError` raised."""
    try:
        data = json.loads(content) if content else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        kind = ServerError if status >= 500 else UnexpectedResponseError
        snippet = content[:200].decode("utf-8", "replace").strip().replace("\n", " ")
        raise kind(
            f"HTTP {status} without the API envelope: {snippet or 'empty body'}",
            status=status,
            code=f"http_{status}",
            method=call.method,
            path=call.path,
        )
    if data["ok"] and 200 <= status < 300:
        return data.get("result")
    raw_error = data.get("error")
    error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
    raise error_from_envelope(
        error,
        http_status=status,
        method=call.method,
        path=call.path,
        retry_after_header=retry_after_header(headers),
    )


@dataclass(slots=True)
class Attempts:
    """Bookkeeping for one call across its retries."""

    policy: RetryPolicy
    tries: int = 0
    rate_limited: int = 0
    # the first answer of a previous try never arrived: done_codes now mean success
    uncertain: bool = False

    def wait_for(self, call: Call[Any], error: BaseException) -> float | None:
        """Seconds to wait before trying again, or None to give up and raise `error`."""
        if isinstance(error, RateLimitError):
            # a 429 is refused before anything happens, so any call may repeat it
            if self.rate_limited >= self.policy.rate_limit_retries:
                return None
            wait = error.retry_after if error.retry_after is not None else 1.0
            if wait > self.policy.max_rate_limit_wait:
                return None
            self.rate_limited += 1
            # a hair over, and not all at once: several sends waiting on the
            # same chat would otherwise land on the same millisecond again
            return wait + random.uniform(0.05, 0.25)
        lost_answer = isinstance(error, (NetworkError, ServerError))
        if not lost_answer or not call.repeatable or isinstance(error, AnswerTooLargeError):
            return None
        self.uncertain = True
        # tries spent waiting out a 429 don't count against the network budget
        failed = self.tries - self.rate_limited
        if failed >= self.policy.attempts:
            return None
        return self.policy.backoff(failed - 1)

    def settles(self, call: Call[Any], error: APIError) -> bool:
        """A repeat answered with a code that means the first try did go through."""
        return self.uncertain and error.code in call.done_codes


class AnswerTooLargeError(NetworkError):
    """An answer bigger than any the API sends. Asking again would only download it again."""


def clean_message(text: str) -> str:
    """Strips anything token shaped out of a string that may end up in a log."""
    return without_tokens(text)


def describe_transport_error(exc: BaseException) -> str:
    name = type(exc).__name__
    detail = clean_message(str(exc)) if str(exc) else ""
    return f"{name}: {detail}" if detail else name
