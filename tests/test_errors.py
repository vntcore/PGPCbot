from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pgpcbot import errors
from pgpcbot.errors import (
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
    KeyChangedError,
    MemberNotFoundError,
    MessageNotFoundError,
    NetworkError,
    NotARecipientError,
    NotFoundError,
    NoUserKeyError,
    PayloadError,
    PayloadTooLargeError,
    PGPCbotError,
    PollingConflictError,
    RateLimitError,
    RequestTimeoutError,
    SendIdConflictError,
    ServerError,
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
    error_from_envelope,
)

SPEC = Path(__file__).parents[1] / "docs" / "openapi.json"

# code -> (class, the HTTP status it comes with, the status family class)
EXPECTED: dict[str, tuple[type[APIError], int, type[APIError]]] = {
    "unauthorized": (AuthenticationError, 401, AuthenticationError),
    "bot_paused": (BotPausedError, 403, ForbiddenError),
    "rate_limited": (RateLimitError, 429, RateLimitError),
    "flood_wait": (FloodWaitError, 429, RateLimitError),
    "slow_mode": (SlowModeError, 429, RateLimitError),
    "bad_request": (BadRequestError, 400, BadRequestError),
    "wrong_body_kind": (WrongBodyKindError, 400, BadRequestError),
    "bad_markup": (BadMarkupError, 400, BadRequestError),
    "bad_webhook": (BadWebhookError, 400, BadRequestError),
    "too_large": (PayloadTooLargeError, 413, PayloadTooLargeError),
    "chat_not_found": (ChatNotFoundError, 404, NotFoundError),
    "message_not_found": (MessageNotFoundError, 404, NotFoundError),
    "member_not_found": (MemberNotFoundError, 404, NotFoundError),
    "user_not_found": (UserNotFoundError, 404, NotFoundError),
    "username_not_found": (UsernameNotFoundError, 404, NotFoundError),
    "no_user_key": (NoUserKeyError, 404, NotFoundError),
    "chat_is_open": (ChatIsOpenError, 400, BadRequestError),
    "user_gone": (UserGoneError, 403, ForbiddenError),
    "blocked_by_user": (BlockedByUserError, 403, ForbiddenError),
    "forbidden": (ForbiddenError, 403, ForbiddenError),
    "conflict": (SendIdConflictError, 409, ConflictError),
    "webhook_active": (WebhookActiveError, 409, ConflictError),
    "callback_not_found": (CallbackNotFoundError, 404, NotFoundError),
    "callback_answered": (CallbackAnsweredError, 409, ConflictError),
    "callback_expired": (CallbackExpiredError, 410, CallbackExpiredError),
    "not_found": (EndpointNotFoundError, 404, NotFoundError),
    "internal": (ServerError, 500, ServerError),
}


def spec_error_codes() -> list[str]:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    return list(spec["components"]["schemas"]["ErrorCode"]["enum"])


def envelope(code: str | None = None, status: int | None = None, **extra: Any) -> dict[str, Any]:
    error: dict[str, Any] = {"message": "said the server", **extra}
    if code is not None:
        error["code"] = code
    if status is not None:
        error["status"] = status
    return error


def test_the_table_covers_exactly_the_codes_the_spec_lists() -> None:
    assert sorted(spec_error_codes()) == sorted(EXPECTED)


@pytest.mark.parametrize("code", spec_error_codes())
def test_every_spec_code_maps_to_its_class(code: str) -> None:
    cls, status, family = EXPECTED[code]
    error = error_from_envelope(envelope(code, status), http_status=status, method="POST", path="/messages")
    assert type(error) is cls
    assert isinstance(error, family)
    assert isinstance(error, APIError)
    assert isinstance(error, PGPCbotError)
    assert (error.code, error.status, error.message, error.method, error.path) == (
        code,
        status,
        "said the server",
        "POST",
        "/messages",
    )


@pytest.mark.parametrize(
    ("status", "cls"),
    [
        (400, BadRequestError),
        (401, AuthenticationError),
        (403, ForbiddenError),
        (404, NotFoundError),
        (409, ConflictError),
        (410, CallbackExpiredError),
        (413, PayloadTooLargeError),
        (429, RateLimitError),
        (500, ServerError),
        (502, ServerError),
        (503, ServerError),
        (599, ServerError),
        (418, APIError),
        (422, APIError),
        (302, APIError),
    ],
)
def test_an_unknown_code_falls_back_on_its_status(status: int, cls: type[APIError]) -> None:
    error = error_from_envelope(envelope("brand_new_code", status), http_status=status)
    assert type(error) is cls
    assert error.code == "brand_new_code"


def test_the_status_in_the_body_wins_over_the_http_status() -> None:
    error = error_from_envelope(envelope("future_thing", 404), http_status=200)
    assert type(error) is NotFoundError
    assert error.status == 404


@pytest.mark.parametrize("status", [None, "404", True, 4.04])
def test_a_missing_or_odd_body_status_falls_back_on_the_http_status(status: Any) -> None:
    error = error_from_envelope({"code": "x", "status": status}, http_status=403)
    assert error.status == 403
    assert type(error) is ForbiddenError


@pytest.mark.parametrize("code", [None, "", 42])
def test_a_missing_code_is_named_after_the_status(code: Any) -> None:
    error = error_from_envelope({"code": code, "status": 409}, http_status=409)
    assert error.code == "http_409"
    assert type(error) is ConflictError


def test_a_missing_message_reads_as_the_code() -> None:
    assert error_from_envelope({"code": "bot_paused", "status": 403}, http_status=403).message == "bot_paused"
    assert error_from_envelope({"code": "bot_paused", "message": 5}, http_status=403).message == "bot_paused"


def test_an_empty_error_object_still_gives_a_usable_error() -> None:
    error = error_from_envelope({}, http_status=502)
    assert type(error) is ServerError
    assert (error.status, error.code, error.message, error.retry_after, error.expected) == (
        502,
        "http_502",
        "http_502",
        None,
        None,
    )


def test_retry_after_from_the_body() -> None:
    error = error_from_envelope(envelope("flood_wait", 429, retryAfter=3), http_status=429)
    assert error.retry_after == 3.0
    assert isinstance(error.retry_after, float)


def test_retry_after_from_the_header_when_the_body_has_none() -> None:
    error = error_from_envelope(envelope("slow_mode", 429), http_status=429, retry_after_header=12.0)
    assert error.retry_after == 12.0


def test_retry_after_body_beats_the_header_for_flood_wait() -> None:
    error = error_from_envelope(envelope("flood_wait", 429, retryAfter=3), http_status=429, retry_after_header=12.0)
    assert error.retry_after == 3.0


def test_rate_limited_prefers_the_header() -> None:
    # the per-minute limiter always writes 60 in the body; the header has the real time left
    error = error_from_envelope(envelope("rate_limited", 429, retryAfter=60), http_status=429, retry_after_header=7.0)
    assert error.retry_after == 7.0
    assert error_from_envelope(envelope("rate_limited", 429, retryAfter=60), http_status=429).retry_after == 60.0


@pytest.mark.parametrize("value", [True, False, "5", -1, None, [5]])
def test_retry_after_ignores_values_that_are_not_non_negative_numbers(value: Any) -> None:
    error = error_from_envelope(envelope("flood_wait", 429, retryAfter=value), http_status=429)
    assert error.retry_after is None
    error = error_from_envelope(envelope("flood_wait", 429, retryAfter=value), http_status=429, retry_after_header=2.0)
    assert error.retry_after == 2.0


def test_retry_after_zero_and_fractions_are_kept() -> None:
    assert error_from_envelope(envelope("flood_wait", 429, retryAfter=0), http_status=429).retry_after == 0.0
    assert error_from_envelope(envelope("flood_wait", 429, retryAfter=1.5), http_status=429).retry_after == 1.5


@pytest.mark.parametrize(
    ("value", "expected"), [("text", "text"), ("armored", "armored"), ("html", None), (None, None), (1, None)]
)
def test_expected_is_only_text_or_armored(value: Any, expected: str | None) -> None:
    error = error_from_envelope(envelope("wrong_body_kind", 400, expected=value), http_status=400)
    assert error.expected == expected


def test_the_whole_error_object_is_kept_in_payload() -> None:
    given = envelope("bad_markup", 400, field="markup.inlineKeyboard[0][1]", hint={"max": 8})
    error = error_from_envelope(given, http_status=400)
    assert error.payload == given
    assert error.payload is not given


def test_str_and_repr() -> None:
    error = error_from_envelope(envelope("chat_not_found", 404), http_status=404, method="GET", path="/chats/7")
    assert str(error) == "chat_not_found (404): said the server [GET /chats/7]"
    assert repr(error) == "ChatNotFoundError(status=404, code='chat_not_found', message='said the server')"
    bare = error_from_envelope(envelope("chat_not_found", 404), http_status=404)
    assert str(bare) == "chat_not_found (404): said the server"


def test_api_error_can_be_built_by_hand() -> None:
    error = APIError("boom", status=500, code="internal")
    assert (error.retry_after, error.expected, error.method, error.path, error.payload) == (None, None, None, None, {})
    assert error.args == ("boom",)


def test_everything_exported_derives_from_pgpcbot_error() -> None:
    exported = [getattr(errors, name) for name in errors.__all__ if name != "error_from_envelope"]
    assert all(issubclass(cls, PGPCbotError) for cls in exported)
    assert issubclass(PGPCbotError, Exception)


@pytest.mark.parametrize("cls", [ConfigurationError, ValidationError, PayloadError])
def test_input_errors_are_value_errors(cls: type[Exception]) -> None:
    assert issubclass(cls, ValueError)


@pytest.mark.parametrize(
    ("child", "parent"),
    [
        (RequestTimeoutError, NetworkError),
        (UnexpectedResponseError, APIError),
        (EncryptionRequiredError, EncryptionError),
        (EncryptionError, CryptoError),
        (NotARecipientError, DecryptionError),
        (DecryptionError, CryptoError),
        (KeyChangedError, CryptoError),
        (PollingConflictError, StorageError),
    ],
)
def test_hierarchy(child: type[Exception], parent: type[Exception]) -> None:
    assert issubclass(child, parent)


def test_network_errors_are_not_api_errors() -> None:
    assert not issubclass(NetworkError, APIError)


def test_validation_error_names_the_field() -> None:
    assert ValidationError("bad", field="text").field == "text"
    assert ValidationError("bad").field is None


def test_webhook_error_carries_the_status_to_answer_with() -> None:
    assert WebhookError("bad secret").status == 400
    assert WebhookError("wrong secret", status=403).status == 403


def test_key_changed_error_carries_both_fingerprints() -> None:
    error = KeyChangedError("changed", user_id=55, pinned="AA" * 20, current="BB" * 20)
    assert (error.user_id, error.pinned, error.current) == (55, "AA" * 20, "BB" * 20)
    assert str(error) == "changed"
