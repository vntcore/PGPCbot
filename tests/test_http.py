from __future__ import annotations

import json
import platform
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest

from pgpcbot import _http
from pgpcbot._http import (
    NO_RETRY,
    Attempts,
    Call,
    RetryPolicy,
    clean_message,
    describe_transport_error,
    mask_token,
    normalize_api_url,
    parse_token,
    read_envelope,
    retry_after_header,
    user_agent,
)
from pgpcbot._version import __version__
from pgpcbot.errors import (
    APIError,
    BadRequestError,
    ChatNotFoundError,
    ConfigurationError,
    FloodWaitError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    RequestTimeoutError,
    ServerError,
    UnexpectedResponseError,
    ValidationError,
)

from .fakeapi import TOKEN

SECRET_PART = TOKEN.split(".", 1)[1]
CALL: Call[Any] = Call("POST", "/messages", lambda result: result)


def read(status: int, body: Any, headers: dict[str, str] | None = None) -> Any:
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return read_envelope(CALL, status, httpx2.Headers(headers or {}), content)


@pytest.fixture
def upper_bound_jitter(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, float]]:
    """random.uniform answers its upper bound, so waits are exact; the bounds asked for are recorded."""
    asked: list[tuple[float, float]] = []

    def uniform(a: float, b: float) -> float:
        asked.append((a, b))
        return b

    monkeypatch.setattr(_http.random, "uniform", uniform)
    return asked


def test_parse_token_returns_the_bot_id() -> None:
    assert parse_token(TOKEN) == 412
    assert parse_token(f"  {TOKEN}\n") == 412
    assert parse_token("pgpbot_" + "9" * 16 + "." + "a-_Z" * 10 + "x0Y") == 9_999_999_999_999_999


@pytest.mark.parametrize(
    "token",
    [
        "",
        "pgpbot_412",
        TOKEN[:-1],
        TOKEN + "A",
        TOKEN.replace("pgpbot_", "pgpbot-"),
        TOKEN.replace("pgpbot_", "PGPBOT_"),
        TOKEN.replace("_412.", "_0412."),
        TOKEN.replace("_412.", "_0."),
        TOKEN.replace("_412.", "_" + "1" * 17 + "."),
        TOKEN[:-1] + "=",
        TOKEN[:-1] + " ",
        f"{TOKEN} {TOKEN}",
    ],
)
def test_parse_token_refuses_what_is_not_a_token_without_echoing_it(token: str) -> None:
    with pytest.raises(ConfigurationError, match="that is not a bot token") as info:
        parse_token(token)
    assert SECRET_PART[:20] not in str(info.value)
    assert SECRET_PART[:20] not in repr(info.value.args)
    assert info.value.__cause__ is None


@pytest.mark.parametrize("prefix", ["Bearer ", "bearer ", "  BEARER "])
def test_parse_token_hints_at_a_pasted_authorization_header(prefix: str) -> None:
    with pytest.raises(ConfigurationError, match="without 'Bearer '") as info:
        parse_token(prefix + TOKEN)
    assert SECRET_PART not in str(info.value)


@pytest.mark.parametrize("token", [None, 412, TOKEN.encode()])
def test_parse_token_wants_a_string(token: Any) -> None:
    with pytest.raises(ConfigurationError, match="the token must be a string") as info:
        parse_token(token)
    assert SECRET_PART not in str(info.value)


def test_mask_token_shows_the_id_and_the_last_four() -> None:
    assert mask_token(TOKEN) == "pgpbot_412.****AAAA"
    token = "pgpbot_7." + "x" * 39 + "3f2a"
    assert mask_token(token) == "pgpbot_7.****3f2a"
    assert mask_token(f" {token} ") == "pgpbot_7.****3f2a"


@pytest.mark.parametrize("value", ["", "garbage", TOKEN[:-1], "pgpbot_412.short", None, 412, TOKEN.encode()])
def test_mask_token_of_anything_else_shows_nothing(value: Any) -> None:
    assert mask_token(value) == "pgpbot_****"


def test_clean_message_strips_anything_token_shaped() -> None:
    text = f"GET failed with {TOKEN}, retried with pgpbot_9.{'b' * 20} and kept pgpbot_9.short"
    assert clean_message(text) == "GET failed with pgpbot_****, retried with pgpbot_**** and kept pgpbot_9.short"


def test_describe_transport_error_names_the_error_and_scrubs_it() -> None:
    assert (
        describe_transport_error(httpx2.ConnectError(f"refused for {TOKEN}")) == "ConnectError: refused for pgpbot_****"
    )
    assert describe_transport_error(httpx2.ReadTimeout("")) == "ReadTimeout"


def test_user_agent() -> None:
    assert user_agent() == f"PGPCbot/{__version__} (Python {platform.python_version()})"


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://api.pgpchat.app", "https://api.pgpchat.app"),
        ("https://api.pgpchat.app/", "https://api.pgpchat.app"),
        ("  https://api.pgpchat.app//  ", "https://api.pgpchat.app"),
        ("https://api.pgpchat.app/bot/v1", "https://api.pgpchat.app"),
        ("https://api.pgpchat.app/bot/v1/", "https://api.pgpchat.app"),
        ("https://example.org/chat/bot/v1", "https://example.org/chat"),
        ("https://example.org:8443", "https://example.org:8443"),
        ("http://localhost:3000", "http://localhost:3000"),
        ("http://127.0.0.1:3000/bot/v1", "http://127.0.0.1:3000"),
        ("http://[::1]:3000", "http://[::1]:3000"),
    ],
)
def test_normalize_api_url(given: str, expected: str) -> None:
    assert normalize_api_url(given) == expected


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("", "must be like https://api.example.org"),
        ("api.pgpchat.app", "must be like https://api.example.org"),
        ("ftp://api.pgpchat.app", "must be like https://api.example.org"),
        ("https://", "must be like https://api.example.org"),
        ("https://user:pw@api.pgpchat.app", "must not carry credentials"),
        ("https://user@api.pgpchat.app", "must not carry credentials"),
        ("https://api.pgpchat.app/?x=1", "query or a fragment"),
        ("https://api.pgpchat.app/#top", "query or a fragment"),
        ("http://[::1", "is not a URL"),
    ],
)
def test_normalize_api_url_refusals(url: str, reason: str) -> None:
    with pytest.raises(ConfigurationError, match=reason):
        normalize_api_url(url)


@pytest.mark.parametrize("url", ["http://api.pgpchat.app", "http://192.168.1.10:3000", "http://localhost.example.org"])
def test_plain_http_is_refused_away_from_this_machine(url: str) -> None:
    with pytest.raises(ConfigurationError, match="token would travel in clear"):
        normalize_api_url(url)
    assert normalize_api_url(url, allow_insecure_http=True) == url


def test_normalize_api_url_of_none_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        normalize_api_url(None)  # type: ignore[arg-type]


@pytest.mark.parametrize("url", ["https://api.pgpchat.app:abc", "https://api.pgpchat.app:99999"])
def test_normalize_api_url_refuses_a_junk_port(url: str) -> None:
    with pytest.raises(ConfigurationError):
        normalize_api_url(url)


def test_call_url_joins_base_prefix_and_path() -> None:
    assert Call("GET", "/me", dict).url("https://api.pgpchat.app") == "https://api.pgpchat.app/bot/v1/me"


def test_call_defaults() -> None:
    call: Call[Any] = Call("GET", "/me", dict)
    assert (call.repeatable, call.done_codes, call.send, call.edit, call.chat_id, call.read_timeout) == (
        True,
        frozenset(),
        False,
        False,
        None,
        None,
    )


NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return NOW if tz is not None else NOW.replace(tzinfo=None)


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_http, "datetime", FrozenDatetime)


@pytest.mark.parametrize(("value", "seconds"), [("120", 120.0), (" 7 ", 7.0), ("0", 0.0)])
def test_retry_after_in_seconds(value: str, seconds: float) -> None:
    assert retry_after_header(httpx2.Headers({"Retry-After": value})) == seconds


def test_retry_after_header_name_is_case_insensitive_with_real_headers() -> None:
    assert retry_after_header(httpx2.Headers({"RETRY-AFTER": "3"})) == 3.0


@pytest.mark.parametrize(
    "headers", [{}, {"retry-after": ""}, {"retry-after": "soon"}, {"retry-after": "1.5"}, {"retry-after": "-3"}]
)
def test_retry_after_that_cannot_be_read_is_none(headers: dict[str, str]) -> None:
    assert retry_after_header(headers) is None


@pytest.mark.usefixtures("frozen_now")
def test_retry_after_as_an_http_date() -> None:
    assert retry_after_header({"retry-after": "Thu, 08 Oct 2026 12:02:00 GMT"}) == 120.0


@pytest.mark.usefixtures("frozen_now")
def test_retry_after_date_without_a_zone_is_utc() -> None:
    assert retry_after_header({"retry-after": "Thu, 08 Oct 2026 12:00:30 -0000"}) == 30.0


@pytest.mark.usefixtures("frozen_now")
def test_retry_after_date_in_the_past_is_zero() -> None:
    assert retry_after_header({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}) == 0.0


def test_retry_after_with_odd_digits_is_none_not_a_crash() -> None:
    # one stray 0xB2 byte from a proxy decodes (latin-1) to this
    assert retry_after_header({"retry-after": "\u00b2"}) is None


def test_retry_policy_defaults_and_no_retry() -> None:
    policy = RetryPolicy()
    assert (
        policy.attempts,
        policy.backoff_base,
        policy.backoff_max,
        policy.rate_limit_retries,
        policy.max_rate_limit_wait,
    ) == (4, 0.5, 20.0, 5, 90.0)
    assert (NO_RETRY.attempts, NO_RETRY.rate_limit_retries) == (1, 0)


@pytest.mark.parametrize(
    "options",
    [{"attempts": 0}, {"attempts": -1}, {"rate_limit_retries": -1}, {"backoff_base": -0.1}, {"backoff_max": -1}],
)
def test_retry_policy_refuses_nonsense(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**options)


def test_retry_policy_is_frozen() -> None:
    with pytest.raises(AttributeError):
        RetryPolicy().attempts = 9  # type: ignore[misc]


def test_backoff_is_full_jitter_under_an_exponential_ceiling(upper_bound_jitter: list[tuple[float, float]]) -> None:
    policy = RetryPolicy(backoff_base=0.5, backoff_max=20.0)
    assert [policy.backoff(retry) for retry in range(8)] == [0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 20.0, 20.0]
    assert all(low == 0 for low, _ in upper_bound_jitter)


def test_backoff_stays_within_its_bounds() -> None:
    policy = RetryPolicy(backoff_base=1.0, backoff_max=3.0)
    waits = [policy.backoff(4) for _ in range(200)]
    assert all(0 <= w <= 3.0 for w in waits)
    assert len(set(waits)) > 1


def test_a_success_hands_back_the_result() -> None:
    assert read(200, {"ok": True, "result": {"id": 1}}) == {"id": 1}
    assert read(201, {"ok": True, "result": [1, 2]}) == [1, 2]
    assert read(200, {"ok": True}) is None


def test_an_error_envelope_becomes_its_api_error() -> None:
    with pytest.raises(ChatNotFoundError) as info:
        read(404, {"ok": False, "error": {"status": 404, "code": "chat_not_found", "message": "no such chat"}})
    assert (info.value.method, info.value.path, info.value.message) == ("POST", "/messages", "no such chat")


def test_the_retry_after_header_reaches_the_error() -> None:
    body = {"ok": False, "error": {"status": 429, "code": "rate_limited", "message": "slow", "retryAfter": 60}}
    with pytest.raises(RateLimitError) as info:
        read(429, body, {"Retry-After": "7"})
    assert info.value.retry_after == 7.0
    body["error"]["code"] = "flood_wait"
    with pytest.raises(FloodWaitError) as info:
        read(429, body, {"Retry-After": "7"})
    assert info.value.retry_after == 60.0


def test_ok_true_with_a_4xx_status_is_still_an_error() -> None:
    with pytest.raises(NotFoundError) as info:
        read(404, {"ok": True, "result": {"id": 1}})
    assert info.value.code == "http_404"


def test_ok_false_with_a_2xx_status_is_still_an_error() -> None:
    with pytest.raises(BadRequestError):
        read(200, {"ok": False, "error": {"status": 400, "code": "bad_request", "message": "no"}})


def test_an_error_that_is_not_an_object_falls_back_on_the_status() -> None:
    with pytest.raises(ServerError) as info:
        read(503, {"ok": False, "error": "maintenance"})
    assert info.value.code == "http_503"


@pytest.mark.parametrize(
    ("status", "body", "cls"),
    [
        (404, b"<html><body>404 Not Found</body></html>", UnexpectedResponseError),
        (502, b"<html><body>502 Bad Gateway</body></html>", ServerError),
        (200, b"OK", UnexpectedResponseError),
        (200, b"[1, 2]", UnexpectedResponseError),
        (200, b'{"result": 1}', UnexpectedResponseError),
        (200, b'{"ok": "true", "result": 1}', UnexpectedResponseError),
        (500, b'{"ok": 1}', ServerError),
        (403, b"", UnexpectedResponseError),
    ],
)
def test_an_answer_without_the_envelope(status: int, body: bytes, cls: type[APIError]) -> None:
    with pytest.raises(cls) as info:
        read(status, body)
    error = info.value
    assert type(error) is cls
    assert (error.status, error.code, error.method, error.path) == (status, f"http_{status}", "POST", "/messages")
    assert error.message.startswith(f"HTTP {status} without the API envelope: ")
    assert error.message.endswith(body.decode().strip() or "empty body")


def test_the_snippet_is_short_and_on_one_line() -> None:
    body = b"line one\nline two\n" + b"Z" * 300
    with pytest.raises(UnexpectedResponseError) as info:
        read(400, body)
    message = info.value.message
    assert "line one line two " in message
    assert "\n" not in message
    assert message.count("Z") == 200 - len(b"line one\nline two\n")


def test_the_snippet_survives_bytes_that_are_not_utf8() -> None:
    with pytest.raises(UnexpectedResponseError, match="caf\ufffd"):
        read(400, b"caf\xe9")


def test_a_proxy_page_that_echoes_the_token_does_not_put_it_in_the_error() -> None:
    page = f"<html><pre>Authorization: Bearer {TOKEN}</pre></html>".encode()
    with pytest.raises(ServerError) as info:
        read(502, page)
    assert SECRET_PART not in str(info.value)


def flood(retry_after: float | None) -> RateLimitError:
    return FloodWaitError("slow down", status=429, code="flood_wait", retry_after=retry_after)


def test_a_429_is_waited_out_a_hair_over_its_retry_after(upper_bound_jitter: list[tuple[float, float]]) -> None:
    attempts = Attempts(RetryPolicy(rate_limit_retries=2))
    assert attempts.wait_for(CALL, flood(3.0)) == 3.25
    assert upper_bound_jitter == [(0.05, 0.25)]
    assert attempts.rate_limited == 1
    assert not attempts.uncertain


@pytest.mark.usefixtures("upper_bound_jitter")
def test_a_429_without_retry_after_waits_a_second() -> None:
    assert Attempts(RetryPolicy()).wait_for(CALL, flood(None)) == 1.25


@pytest.mark.usefixtures("upper_bound_jitter")
def test_429s_are_waited_out_only_rate_limit_retries_times() -> None:
    attempts = Attempts(RetryPolicy(rate_limit_retries=2))
    assert attempts.wait_for(CALL, flood(1.0)) is not None
    assert attempts.wait_for(CALL, flood(1.0)) is not None
    assert attempts.wait_for(CALL, flood(1.0)) is None
    assert Attempts(NO_RETRY).wait_for(CALL, flood(1.0)) is None


@pytest.mark.usefixtures("upper_bound_jitter")
def test_a_429_asking_for_too_long_is_raised() -> None:
    attempts = Attempts(RetryPolicy(max_rate_limit_wait=30.0))
    assert attempts.wait_for(CALL, flood(30.0)) == 30.25
    assert attempts.wait_for(CALL, flood(30.5)) is None
    assert attempts.rate_limited == 1


@pytest.mark.usefixtures("upper_bound_jitter")
def test_a_429_is_waited_out_even_for_a_call_that_is_not_repeatable() -> None:
    once: Call[Any] = Call("POST", "/messages", dict, repeatable=False)
    assert Attempts(RetryPolicy()).wait_for(once, flood(2.0)) == 2.25


@pytest.mark.usefixtures("upper_bound_jitter")
@pytest.mark.parametrize(
    "error",
    [NetworkError("reset"), RequestTimeoutError("slow"), ServerError("boom", status=500, code="internal")],
)
def test_a_lost_answer_is_retried_with_backoff(error: Exception) -> None:
    policy = RetryPolicy(attempts=4, backoff_base=1.0, backoff_max=100.0)
    attempts = Attempts(policy, tries=1)
    assert attempts.wait_for(CALL, error) == 1.0
    assert attempts.uncertain
    attempts.tries = 3
    assert attempts.wait_for(CALL, error) == 4.0


@pytest.mark.usefixtures("upper_bound_jitter")
def test_a_lost_answer_is_given_up_after_the_last_attempt() -> None:
    attempts = Attempts(RetryPolicy(attempts=3), tries=3)
    assert attempts.wait_for(CALL, NetworkError("reset")) is None
    # whatever comes next, the first try may have gone through
    assert attempts.uncertain


def test_a_call_that_is_not_repeatable_is_never_retried_after_a_lost_answer() -> None:
    once: Call[Any] = Call("POST", "/token/revoke", dict, repeatable=False)
    attempts = Attempts(RetryPolicy(), tries=1)
    assert attempts.wait_for(once, NetworkError("reset")) is None
    assert attempts.wait_for(once, ServerError("boom", status=500, code="internal")) is None
    assert not attempts.uncertain


@pytest.mark.parametrize(
    "error",
    [
        BadRequestError("no", status=400, code="bad_request"),
        ValidationError("no"),
        ValueError("no"),
        UnexpectedResponseError("html", status=404, code="http_404"),
    ],
)
def test_other_errors_are_not_retried(error: Exception) -> None:
    attempts = Attempts(RetryPolicy(), tries=1)
    assert attempts.wait_for(CALL, error) is None
    assert not attempts.uncertain


def test_settles_only_after_an_uncertain_try_and_only_for_done_codes() -> None:
    delete: Call[Any] = Call("DELETE", "/messages/5", dict, done_codes=frozenset({"message_not_found"}))
    gone = APIError("gone", status=404, code="message_not_found")
    other = APIError("no chat", status=404, code="chat_not_found")
    attempts = Attempts(RetryPolicy(), tries=1)
    assert not attempts.settles(delete, gone)
    attempts.wait_for(delete, NetworkError("reset"))
    assert attempts.settles(delete, gone)
    assert not attempts.settles(delete, other)
    assert not attempts.settles(CALL, gone)
