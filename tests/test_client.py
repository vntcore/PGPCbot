from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import pickle
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest

import pgpcbot
import pgpcbot.client as client_module
from pgpcbot import AsyncClient, Client, RetryPolicy, _http
from pgpcbot.errors import (
    APIError,
    AuthenticationError,
    BadRequestError,
    BadWebhookError,
    BotPausedError,
    CallbackAnsweredError,
    CallbackExpiredError,
    ChatIsOpenError,
    ChatNotFoundError,
    ConfigurationError,
    EndpointNotFoundError,
    FloodWaitError,
    MemberNotFoundError,
    MessageNotFoundError,
    NetworkError,
    PayloadError,
    PayloadTooLargeError,
    PGPCbotError,
    RateLimitError,
    RequestTimeoutError,
    ServerError,
    UnexpectedResponseError,
    UsernameNotFoundError,
    UserNotFoundError,
    ValidationError,
    WebhookActiveError,
    WrongBodyKindError,
)
from pgpcbot.keyboard import Button, InlineKeyboard
from pgpcbot.models import (
    BotStatus,
    ChatKind,
    ChatMode,
    ChatRef,
    Command,
    CommandScope,
    Media,
    SeatStatus,
    Update,
    UpdateKind,
)
from pgpcbot.ratelimit import RateLimiter

from .conftest import FAST_RETRY, FIXTURES, fixture_text
from .fakeapi import BASE, BOT_ID, MEDIA_ID, TOKEN, FakeAPI

Answer = Callable[[httpx2.Request], httpx2.Response]

SECRET = TOKEN.split(".", 1)[1]
HINT = "pgpbot_412.****AAAA"

PRIVATE = 7781
GROUP = 9100
ANNA = 55
MESSAGE = 1001
CALLBACK = "4f1c9b2e7a3d4c8e9b0a1f"
SEND_ID = "6f9619ff-8b86-4011-b42d-00c04fc964ff"
FINGERPRINT = "3f2a9c1e5d7b4a6e8c0f1b2d3e4f5a6b7c8d9e0f"
# the fake stamps everything with 1791000000000 ms
SENT_AT = datetime(2026, 10, 3, 4, 0, tzinfo=UTC)

PUBLIC_KEY = fixture_text("bot.pub.asc")
PRIVATE_KEY = fixture_text("bot.sec.asc")
ARMORED = fixture_text("msg_text_signed.asc")
ATTACHMENT = (FIXTURES / "attachment.pgp").read_bytes()
ME = {"id": BOT_ID, "username": "testbot", "name": "Test"}

KEYBOARD = InlineKeyboard.build(
    [Button.callback("Hourly", "hourly:rome"), Button.link("Map", "https://maps.example/rome")]
)
KEYBOARD_JSON = {
    "inlineKeyboard": [
        [{"text": "Hourly", "callbackData": "hourly:rome"}, {"text": "Map", "url": "https://maps.example/rome"}]
    ]
}
ANNA_KEY = {"userId": ANNA, "fingerprint": FINGERPRINT.upper(), "publicKey": fixture_text("phone_ed.pub.asc")}
MEMBERS = [
    {
        "user": {"id": ANNA, "username": "anna", "firstName": "Anna", "lastName": None, "isBot": False},
        "status": "admin",
        "adminRights": 3,
        "can": {"deleteMessages": True, "banUsers": True},
        "customTitle": "guide",
    },
    {
        "user": {"id": 56, "username": None, "firstName": "Bruno", "lastName": "Rossi", "isBot": False},
        "status": "restricted",
        "rights": 1,
        "restrictedUntil": 1791000000000,
    },
]


class Tap(httpx2.BaseTransport, httpx2.AsyncBaseTransport):
    """Sits in front of the fake and sees every request, the refused ones too."""

    def __init__(self, inner: FakeAPI) -> None:
        self.inner = inner
        self.requests: list[httpx2.Request] = []
        # the status each request got back, None where the line dropped it
        self.answers: list[int | None] = []
        self.closed = 0

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        try:
            response = self.inner.handle_request(request)
        except httpx2.TransportError:
            self.answers.append(None)
            raise
        self.answers.append(response.status_code)
        return response

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        try:
            response = await self.inner.handle_async_request(request)
        except httpx2.TransportError:
            self.answers.append(None)
            raise
        self.answers.append(response.status_code)
        return response

    def close(self) -> None:
        self.closed += 1

    async def aclose(self) -> None:
        self.closed += 1


class Scripted(httpx2.BaseTransport, httpx2.AsyncBaseTransport):
    """A server that plays its answers in order and then keeps repeating the last one."""

    def __init__(self, *answers: Answer) -> None:
        self.answers = list(answers)
        self.requests: list[httpx2.Request] = []

    def _play(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return answer(request)

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        request.read()
        return self._play(request)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await request.aread()
        return self._play(request)


class Chunks(httpx2.SyncByteStream, httpx2.AsyncByteStream):
    """A body that arrives in pieces, without Content-Length, counting the pieces actually read."""

    def __init__(self, *pieces: bytes | Exception) -> None:
        self.pieces = pieces
        self.served = 0

    def __iter__(self) -> Iterator[bytes]:
        for piece in self.pieces:
            if isinstance(piece, Exception):
                raise piece
            self.served += 1
            yield piece

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for piece in self.pieces:
            if isinstance(piece, Exception):
                raise piece
            self.served += 1
            yield piece


def ok(result: Any) -> Answer:
    return lambda request: httpx2.Response(200, json={"ok": True, "result": result})


def refusal(
    status: int, code: str, message: str = "", *, headers: dict[str, str] | None = None, **extra: Any
) -> Answer:
    error = {"status": status, "code": code, "message": message or code, **extra}
    return lambda request: httpx2.Response(status, json={"ok": False, "error": error}, headers=headers)


def page(status: int, html: str) -> Answer:
    """What a proxy in front of the API answers when it is the one failing."""
    return lambda request: httpx2.Response(status, text=html, headers={"Content-Type": "text/html"})


def broken(failure: type[httpx2.TransportError], message: str = "") -> Answer:
    def answer(request: httpx2.Request) -> httpx2.Response:
        raise failure(message or failure.__name__, request=request)

    return answer


class Either:
    """Both clients behind one face: every method is awaited, whichever flavour is underneath."""

    def __init__(self, inner: AsyncClient | Client) -> None:
        self.inner = inner
        self.flavour = "async" if isinstance(inner, AsyncClient) else "sync"

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        async def call(*args: Any, **kwargs: Any) -> Any:
            result = attr(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result

        return call


class SpyLimiter(RateLimiter):
    """Lets every call through at once and keeps what the client told it."""

    def __init__(self) -> None:
        super().__init__()
        self.acquired: list[dict[str, Any]] = []
        self.penalties: list[dict[str, Any]] = []

    def acquire(self, **call: Any) -> float:
        self.acquired.append(call)
        return 0.0

    async def acquire_async(self, **call: Any) -> float:
        self.acquired.append(call)
        return 0.0

    def penalize(self, seconds: float, **where: Any) -> None:
        self.penalties.append({"seconds": seconds, **where})


def client_class(flavour: str) -> type[AsyncClient | Client]:
    return AsyncClient if flavour == "async" else Client


async def shut(client: AsyncClient | Client) -> None:
    if isinstance(client, AsyncClient):
        await client.aclose()
    else:
        client.close()


async def take(client: Either, count: int, **options: Any) -> list[Update]:
    """The first ``count`` updates iter_updates hands over, whichever flavour the client is."""
    taken: list[Update] = []
    if client.flavour == "async":
        async with contextlib.aclosing(client.inner.iter_updates(**options)) as updates:
            async for update in updates:
                taken.append(update)
                if len(taken) == count:
                    break
    else:
        with contextlib.closing(client.inner.iter_updates(**options)) as blocking:
            for update in blocking:
                taken.append(update)
                if len(taken) == count:
                    break
    return taken


@pytest.fixture(autouse=True)
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every wait the clients asked for, none of them actually slept."""
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)

    async def async_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(client_module, "time", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(client_module, "asyncio", SimpleNamespace(sleep=async_sleep))
    return waits


@pytest.fixture
def wire(fake: FakeAPI) -> Tap:
    return Tap(fake)


@pytest.fixture(params=["async", "sync"])
def flavour(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
async def connect(flavour: str) -> AsyncIterator[Callable[..., Either]]:
    """Makes clients of the flavour under test and closes them when the test is over."""
    made: list[AsyncClient | Client] = []

    def factory(transport: Any, *, token: Any = TOKEN, **options: Any) -> Either:
        options.setdefault("api_url", BASE)
        options.setdefault("retry", FAST_RETRY)
        options.setdefault("rate_limiter", False)
        inner = client_class(flavour)(token, transport=transport, **options)
        made.append(inner)
        return Either(inner)

    yield factory
    for inner in made:
        await shut(inner)


@pytest.fixture
def client(connect: Callable[..., Either], wire: Tap) -> Either:
    return connect(wire)


@pytest.fixture
def world(fake: FakeAPI) -> FakeAPI:
    """A chat with Anna, an encrypted group, one of the bot's messages with buttons, a pending press."""
    fake.add_chat(PRIVATE)
    fake.add_chat(GROUP, "group", title="Hikers", username="hikers", members=MEMBERS, keys=[ANNA_KEY])
    fake.user_keys[ANNA] = ANNA_KEY
    fake.add_handle("anna", user={"id": ANNA, "username": "anna", "firstName": "Anna", "isBot": False, "hasKey": True})
    fake.add_handle("anna_too", user={"id": ANNA, "username": "anna", "firstName": "Anna", "hasKey": True})
    fake.add_handle(
        "hikers",
        chat={
            "id": GROUP,
            "kind": "group",
            "title": "Hikers",
            "username": "hikers",
            "memberCount": 3,
            "historyVisible": True,
            "description": "Boots on",
        },
    )
    fake.commands["default"] = [{"command": "start", "description": "Say hello"}]
    with Client(TOKEN, api_url=BASE, transport=fake, rate_limiter=False) as setup:
        assert setup.send_message(PRIVATE, "Rome: sun", markup=KEYBOARD).id == MESSAGE
    fake.callbacks[CALLBACK] = {"answered": False, "expiresAt": int((time.time() + 60) * 1000), "answer": None}
    fake.requests.clear()
    return fake


@pytest.fixture
def no_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bot installs a scrubbing filter on the pgpcbot loggers once per process; the
    # client has to stay clean on its own, whatever ran before this test
    monkeypatch.setattr(logging.getLogger("pgpcbot.client"), "filters", [])


WIRE = [
    pytest.param("get_me", (), {}, "GET", "/bot/v1/me", {}, None, id="get_me"),
    pytest.param("get_updates", (), {}, "GET", "/bot/v1/updates", {}, None, id="get_updates"),
    pytest.param(
        "get_updates",
        (5, 20, 0),
        {},
        "GET",
        "/bot/v1/updates",
        {"offset": "5", "limit": "20", "timeout": "0"},
        None,
        id="get_updates-every-option",
    ),
    pytest.param("get_webhook", (), {}, "GET", "/bot/v1/webhook", {}, None, id="get_webhook"),
    pytest.param(
        "set_webhook",
        ("https://bot.example/hook",),
        {},
        "POST",
        "/bot/v1/webhook",
        {},
        {"url": "https://bot.example/hook"},
        id="set_webhook",
    ),
    pytest.param(
        "set_webhook",
        ("https://bot.example/hook", "s3cr3t_-"),
        {},
        "POST",
        "/bot/v1/webhook",
        {},
        {"url": "https://bot.example/hook", "secret": "s3cr3t_-"},
        id="set_webhook-with-secret",
    ),
    pytest.param("delete_webhook", (), {}, "DELETE", "/bot/v1/webhook", {}, None, id="delete_webhook"),
    pytest.param(
        "set_key",
        (PUBLIC_KEY, f" {FINGERPRINT} "),
        {},
        "PUT",
        "/bot/v1/key",
        {},
        {"publicKey": PUBLIC_KEY, "fingerprint": FINGERPRINT.upper()},
        id="set_key",
    ),
    pytest.param("get_commands", (), {}, "GET", "/bot/v1/commands", {}, None, id="get_commands"),
    pytest.param(
        "get_commands", ("private",), {}, "GET", "/bot/v1/commands", {"scope": "private"}, None, id="get_commands-scope"
    ),
    pytest.param(
        "set_commands",
        ([("start", "Say hello")],),
        {},
        "PUT",
        "/bot/v1/commands",
        {},
        {"commands": [{"command": "start", "description": "Say hello"}]},
        id="set_commands",
    ),
    pytest.param(
        "set_commands",
        ([Command(command="help", description="What I do")], "groups"),
        {},
        "PUT",
        "/bot/v1/commands",
        {},
        {"commands": [{"command": "help", "description": "What I do"}], "scope": "groups"},
        id="set_commands-scope",
    ),
    pytest.param("delete_commands", (), {}, "DELETE", "/bot/v1/commands", {}, None, id="delete_commands"),
    pytest.param(
        "delete_commands",
        (f"chat:{GROUP}",),
        {},
        "DELETE",
        "/bot/v1/commands",
        {"scope": f"chat:{GROUP}"},
        None,
        id="delete_commands-scope",
    ),
    pytest.param(
        "send_message",
        (PRIVATE, "hi"),
        {"send_id": SEND_ID},
        "POST",
        "/bot/v1/messages",
        {},
        {"chatId": PRIVATE, "text": "hi", "sendId": SEND_ID},
        id="send_message",
    ),
    pytest.param(
        "send_message",
        (PRIVATE, "hi"),
        {"reply_to": MESSAGE, "markup": KEYBOARD, "silent": True, "send_id": SEND_ID},
        "POST",
        "/bot/v1/messages",
        {},
        {
            "chatId": PRIVATE,
            "text": "hi",
            "replyTo": MESSAGE,
            "markup": KEYBOARD_JSON,
            "silent": True,
            "sendId": SEND_ID,
        },
        id="send_message-every-option",
    ),
    pytest.param(
        "send_message",
        (GROUP,),
        {"armored": ARMORED, "send_id": SEND_ID},
        "POST",
        "/bot/v1/messages",
        {},
        {"chatId": GROUP, "armored": ARMORED, "sendId": SEND_ID},
        id="send_message-armored",
    ),
    pytest.param(
        "edit_message",
        (PRIVATE, MESSAGE, "Rome: rain"),
        {},
        "PATCH",
        f"/bot/v1/messages/{MESSAGE}",
        {},
        {"chatId": PRIVATE, "text": "Rome: rain"},
        id="edit_message",
    ),
    pytest.param(
        "edit_message",
        (PRIVATE, MESSAGE),
        {"markup": None},
        "PATCH",
        f"/bot/v1/messages/{MESSAGE}",
        {},
        {"chatId": PRIVATE, "markup": None},
        id="edit_message-drop-buttons",
    ),
    pytest.param(
        "edit_message",
        (PRIVATE, MESSAGE, "Rome: rain"),
        {"markup": KEYBOARD},
        "PATCH",
        f"/bot/v1/messages/{MESSAGE}",
        {},
        {"chatId": PRIVATE, "text": "Rome: rain", "markup": KEYBOARD_JSON},
        id="edit_message-new-buttons",
    ),
    pytest.param(
        "delete_message",
        (PRIVATE, MESSAGE),
        {},
        "DELETE",
        f"/bot/v1/messages/{MESSAGE}",
        {"chatId": str(PRIVATE)},
        None,
        id="delete_message",
    ),
    pytest.param(
        "answer_callback", (CALLBACK,), {}, "POST", f"/bot/v1/callbacks/{CALLBACK}/answer", {}, {}, id="answer_callback"
    ),
    pytest.param(
        "answer_callback",
        (CALLBACK, "  On its way  "),
        {"alert": True, "url": "https://bot.example/more"},
        "POST",
        f"/bot/v1/callbacks/{CALLBACK}/answer",
        {},
        {"text": "On its way", "alert": True, "url": "https://bot.example/more"},
        id="answer_callback-every-option",
    ),
    pytest.param("get_chat", (PRIVATE,), {}, "GET", f"/bot/v1/chats/{PRIVATE}", {}, None, id="get_chat"),
    pytest.param("get_chat_keys", (GROUP,), {}, "GET", f"/bot/v1/chats/{GROUP}/keys", {}, None, id="get_chat_keys"),
    pytest.param(
        "get_chat_members", (GROUP,), {}, "GET", f"/bot/v1/chats/{GROUP}/members", {}, None, id="get_chat_members"
    ),
    pytest.param(
        "get_chat_member",
        (GROUP, ANNA),
        {},
        "GET",
        f"/bot/v1/chats/{GROUP}/members/{ANNA}",
        {},
        None,
        id="get_chat_member",
    ),
    pytest.param("leave_chat", (GROUP,), {}, "POST", f"/bot/v1/chats/{GROUP}/leave", {}, None, id="leave_chat"),
    pytest.param(
        "send_typing", (PRIVATE,), {}, "POST", f"/bot/v1/chats/{PRIVATE}/typing", {}, {"typing": True}, id="send_typing"
    ),
    pytest.param(
        "send_typing",
        (PRIVATE, False),
        {},
        "POST",
        f"/bot/v1/chats/{PRIVATE}/typing",
        {},
        {"typing": False},
        id="send_typing-stop",
    ),
    pytest.param("resolve", ("anna",), {}, "GET", "/bot/v1/resolve/anna", {}, None, id="resolve"),
    pytest.param("resolve", ("@Anna",), {}, "GET", "/bot/v1/resolve/Anna", {}, None, id="resolve-at-and-case"),
    pytest.param("get_user_key", (ANNA,), {}, "GET", f"/bot/v1/users/{ANNA}/key", {}, None, id="get_user_key"),
    pytest.param("revoke_token", (), {}, "POST", "/bot/v1/token/revoke", {}, None, id="revoke_token"),
]

NOT_REPEATABLE = {"get_updates", "send_typing"}


def one_per_operation(*, repeatable: bool) -> list[Any]:
    picked = []
    for row in WIRE:
        op, args, kwargs, method, path, _query, _body = row.values
        if row.id == op and (op not in NOT_REPEATABLE) == repeatable:
            picked.append(pytest.param(op, args, kwargs, method, path, id=op))
    return picked


@pytest.mark.parametrize(("op", "args", "kwargs", "method", "path", "query", "body"), WIRE)
async def test_every_operation_goes_out_as_the_api_describes_it(
    client: Either,
    world: FakeAPI,
    op: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    method: str,
    path: str,
    query: dict[str, str],
    body: Any,
) -> None:
    await getattr(client, op)(*args, **kwargs)
    [sent] = world.calls()
    assert (sent["method"], sent["path"], sent["query"], sent["json"]) == (method, path, query, body)
    headers = sent["headers"]
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["user-agent"].startswith(f"PGPCbot/{pgpcbot.__version__} (Python ")
    assert headers["accept"] == "application/json"
    assert headers.get("content-type") == (None if body is None else "application/json")


async def test_get_me_reads_the_bot_its_webhook_and_the_limits(client: Either, fake: FakeAPI) -> None:
    fake.webhook.update(url="https://bot.example/hook", secret="s3cret")
    me = await client.get_me()
    assert (me.id, me.username, me.name, me.mode, me.status) == (
        BOT_ID,
        "testbot",
        "Test",
        ChatMode.OPEN,
        BotStatus.ACTIVE,
    )
    assert not me.protected
    assert not me.paused
    assert (me.webhook.url, me.webhook.has_secret, me.queued) == ("https://bot.example/hook", True, 0)
    limits = me.limits
    assert (limits.text_max, limits.poll_timeout_max_s, limits.sends_per_second, limits.api_calls_per_minute) == (
        4096,
        50,
        30,
        600,
    )
    # left out by this server: the documented default stands in
    assert limits.armored_max == 131072


async def test_a_paused_bot_can_still_ask_who_it_is(client: Either, fake: FakeAPI) -> None:
    fake.add_chat(PRIVATE)
    fake.me["status"] = "paused"
    assert (await client.get_me()).paused
    with pytest.raises(BotPausedError):
        await client.send_message(PRIVATE, "anyone there?")


async def test_get_updates_hands_over_updates_and_an_offset_confirms_them(client: Either, fake: FakeAPI) -> None:
    fake.add_chat(PRIVATE)
    fake.push_message(PRIVATE, "one")
    fake.push_message(PRIVATE, "one, corrected", edited=True)
    first, second = await client.get_updates()
    assert (first.update_id, first.kind, first.message.text) == (1, UpdateKind.MESSAGE, "one")
    assert (first.chat.id, first.from_user.id, first.message.date) == (PRIVATE, ANNA, SENT_AT)
    assert (second.update_id, second.kind) == (2, UpdateKind.EDITED_MESSAGE)
    assert [u.update_id for u in await client.get_updates(offset=2)] == [2]
    assert [u.update_id for u in await client.get_updates()] == [2]


async def test_a_long_poll_outlives_its_own_timeout(client: Either, fake: FakeAPI, wire: Tap) -> None:
    fake.add_chat(PRIVATE)
    fake.push_message(PRIVATE, "already waiting")
    await client.get_updates(timeout=50)
    await client.get_me()
    poll, plain = (request.extensions["timeout"] for request in wire.requests)
    assert (poll["read"], plain["read"]) == (65.0, 30.0)
    assert poll["connect"] == plain["connect"] == 10.0


async def test_iter_updates_confirms_a_batch_only_when_it_asks_for_the_next(client: Either, fake: FakeAPI) -> None:
    fake.add_chat(PRIVATE)
    for text in ("one", "two", "three"):
        fake.push_message(PRIVATE, text)
    seen = await take(client, 3, limit=2, timeout=0)
    assert [u.message.text for u in seen] == ["one", "two", "three"]
    polls = [call["query"] for call in fake.calls("GET", "/bot/v1/updates")]
    assert polls == [{"limit": "2", "timeout": "0"}, {"offset": "3", "limit": "2", "timeout": "0"}]
    # "three" was handed over, but no later poll confirmed it
    assert [u.update_id for u in await client.get_updates()] == [3]


async def test_iter_updates_leaves_the_rest_of_a_batch_for_next_time(client: Either, fake: FakeAPI) -> None:
    fake.add_chat(PRIVATE)
    fake.push_message(PRIVATE, "one")
    fake.push_message(PRIVATE, "two")
    [first] = await take(client, 1, timeout=0)
    assert first.update_id == 1
    assert [u.update_id for u in await client.get_updates()] == [1, 2]


async def test_iter_updates_starts_from_the_offset_it_is_given(client: Either, fake: FakeAPI) -> None:
    fake.add_chat(PRIVATE)
    for text in ("old", "new"):
        fake.push_message(PRIVATE, text)
    [update] = await take(client, 1, offset=2, timeout=0)
    assert update.message.text == "new"
    assert fake.calls("GET", "/bot/v1/updates")[0]["query"] == {"offset": "2", "limit": "100", "timeout": "0"}


async def test_a_webhook_is_set_read_and_deleted(client: Either, fake: FakeAPI) -> None:
    fake.add_chat(PRIVATE)
    fake.push_message(PRIVATE, "queued while nobody listened")
    info = await client.set_webhook("https://bot.example/hook", "s3cret")
    assert (info.url, info.has_secret, info.pending) == ("https://bot.example/hook", True, 1)
    info = await client.get_webhook()
    assert (info.url, info.has_secret, info.pending, info.failures, info.last_error, info.last_ok_at) == (
        "https://bot.example/hook",
        True,
        1,
        0,
        None,
        None,
    )
    with pytest.raises(WebhookActiveError):
        await client.get_updates()
    info = await client.delete_webhook()
    assert (info.url, info.has_secret, info.pending) == (None, False, 1)


async def test_the_server_not_the_client_refuses_a_plain_http_webhook(client: Either, wire: Tap) -> None:
    with pytest.raises(BadWebhookError):
        await client.set_webhook("http://bot.example/hook")
    assert len(wire.requests) == 1


async def test_set_key_publishes_the_key_and_the_bot_turns_protected(client: Either) -> None:
    published = await client.set_key(PUBLIC_KEY, FINGERPRINT)
    assert (published.fingerprint, published.mode) == (FINGERPRINT.upper(), ChatMode.PROTECTED)
    assert (await client.get_me()).protected


async def test_commands_are_cleaned_up_the_way_the_server_would(client: Either, fake: FakeAPI) -> None:
    scope = await client.set_commands(
        [
            ("/Start ", "Say   hello"),
            {"command": "weather", "description": "Three days ahead"},
            Command(command="help", description="What I can do"),
            ("start", "a second /start, folded into the first"),
        ],
        "private",
    )
    expected = (
        Command(command="start", description="Say hello"),
        Command(command="weather", description="Three days ahead"),
        Command(command="help", description="What I can do"),
    )
    assert (scope.scope, scope.commands) == ("private", expected)
    [sent] = fake.calls("PUT", "/bot/v1/commands")
    assert sent["json"] == {"commands": [c.to_json() for c in expected], "scope": "private"}


async def test_get_commands_lists_every_scope_or_just_one(client: Either, fake: FakeAPI) -> None:
    fake.commands["default"] = [{"command": "start", "description": "Say hello"}]
    fake.commands["private"] = [{"command": "help", "description": "What I can do"}]
    start = Command(command="start", description="Say hello")
    help_ = Command(command="help", description="What I can do")
    assert await client.get_commands() == [
        CommandScope(scope="default", commands=(start,)),
        CommandScope(scope="private", commands=(help_,)),
    ]
    assert await client.get_commands("private") == [help_]
    assert await client.get_commands("groups") == []
    assert await client.delete_commands("private") == CommandScope(scope="private", commands=())
    assert [s.scope for s in await client.get_commands()] == ["default"]


async def test_send_message_returns_the_message_as_the_server_stored_it(
    client: Either, fake: FakeAPI, world: FakeAPI
) -> None:
    sent = await client.send_message(PRIVATE, "Rome: sun\r\nRain tomorrow", reply_to=MESSAGE, markup=KEYBOARD)
    assert sent.id == MESSAGE + 1
    assert sent.chat == ChatRef(id=PRIVATE, kind=ChatKind.PRIVATE)
    assert (sent.text, sent.armored, sent.encrypted) == ("Rome: sun\nRain tomorrow", None, False)
    assert (sent.reply_to, sent.markup, sent.date) == (MESSAGE, KEYBOARD, SENT_AT)
    assert fake.calls("POST", "/bot/v1/messages")[0]["json"]["text"] == "Rome: sun\nRain tomorrow"


async def test_an_armored_message_goes_into_an_encrypted_chat_untouched(client: Either, world: FakeAPI) -> None:
    sent = await client.send_message(GROUP, armored=ARMORED)
    assert (sent.armored, sent.text, sent.encrypted) == (ARMORED, None, True)
    assert sent.chat == ChatRef(id=GROUP, kind=ChatKind.GROUP, title="Hikers", username="hikers")


async def test_text_into_an_encrypted_chat_comes_back_with_the_body_it_expects(
    client: Either, world: FakeAPI, wire: Tap
) -> None:
    with pytest.raises(WrongBodyKindError) as caught:
        await client.send_message(GROUP, "in clear, by mistake")
    assert caught.value.expected == "armored"
    assert len(wire.requests) == 1


async def test_edit_message_keeps_the_buttons_unless_told_otherwise(client: Either, world: FakeAPI) -> None:
    kept = await client.edit_message(PRIVATE, MESSAGE, "Rome: rain")
    dropped = await client.edit_message(PRIVATE, MESSAGE, markup=None)
    first, second = world.calls("PATCH", f"/bot/v1/messages/{MESSAGE}")
    assert "markup" not in first["json"]
    assert second["json"] == {"chatId": PRIVATE, "markup": None}
    assert (kept.text, kept.markup) == ("Rome: rain", KEYBOARD)
    assert (dropped.text, dropped.markup) == ("Rome: rain", None)


async def test_an_empty_keyboard_on_an_edit_takes_the_buttons_away(client: Either, world: FakeAPI) -> None:
    edited = await client.edit_message(PRIVATE, MESSAGE, markup=InlineKeyboard())
    assert edited.markup is None
    assert world.calls()[0]["json"] == {"chatId": PRIVATE, "markup": None}


async def test_delete_message_deletes_once(client: Either, world: FakeAPI) -> None:
    assert await client.delete_message(PRIVATE, MESSAGE) is True
    with pytest.raises(MessageNotFoundError):
        await client.delete_message(PRIVATE, MESSAGE)


async def test_answer_callback_stops_the_spinner_once(client: Either, world: FakeAPI) -> None:
    assert await client.answer_callback(CALLBACK, "On its way", alert=True) is True
    assert world.callbacks[CALLBACK]["answer"] == {"text": "On its way", "alert": True}
    with pytest.raises(CallbackAnsweredError):
        await client.answer_callback(CALLBACK)


async def test_a_press_answered_too_late_is_expired(client: Either, world: FakeAPI, wire: Tap) -> None:
    world.callbacks[CALLBACK]["expiresAt"] = int((time.time() - 1) * 1000)
    with pytest.raises(CallbackExpiredError):
        await client.answer_callback(CALLBACK)
    assert len(wire.requests) == 1


@pytest.mark.parametrize(
    "callback_id",
    [
        "a/b",
        "../../token/revoke",
        "?scope=all",
        "%2e%2e",
        "..",
        ".",
    ],
)
async def test_a_callback_id_stays_a_single_path_segment(client: Either, wire: Tap, callback_id: str) -> None:
    with contextlib.suppress(PGPCbotError):
        await client.answer_callback(callback_id)
    paths = [request.url.raw_path.decode() for request in wire.requests]
    assert all(re.fullmatch(r"/bot/v1/callbacks/[^/?#]+/answer", path) for path in paths), paths


async def test_get_chat_describes_a_private_chat(client: Either, world: FakeAPI) -> None:
    chat = await client.get_chat(PRIVATE)
    assert (chat.id, chat.kind, chat.mode, chat.protected, chat.blocked) == (
        PRIVATE,
        ChatKind.PRIVATE,
        ChatMode.OPEN,
        False,
        False,
    )
    assert chat.user is not None
    assert (chat.user.id, chat.user.username, chat.user.first_name, chat.user.has_key, chat.user.deleted) == (
        ANNA,
        "anna",
        "Anna",
        True,
        False,
    )
    assert (chat.me, chat.member_count) == (None, None)


async def test_get_chat_describes_a_group_and_the_bots_own_seat(client: Either, world: FakeAPI) -> None:
    chat = await client.get_chat(GROUP)
    assert (chat.kind, chat.title, chat.username, chat.mode, chat.protected) == (
        ChatKind.GROUP,
        "Hikers",
        "hikers",
        ChatMode.PROTECTED,
        True,
    )
    assert (chat.history_visible, chat.member_count, chat.slow_mode_seconds, chat.user) == (False, 2, 0, None)
    assert chat.me is not None
    assert (chat.me.status, chat.me.rights, chat.me.restricted_until) == (SeatStatus.MEMBER, 255, None)


async def test_get_chat_keys_lists_the_keys_a_message_is_sealed_to(client: Either, world: FakeAPI) -> None:
    keys = await client.get_chat_keys(GROUP)
    [anna] = keys.keys
    assert (anna.user_id, anna.fingerprint, anna.public_key) == (ANNA, FINGERPRINT.upper(), ANNA_KEY["publicKey"])
    assert keys.member_count == 3
    with pytest.raises(ChatIsOpenError):
        await client.get_chat_keys(PRIVATE)


async def test_the_members_of_a_group_one_by_one_or_all_together(client: Either, world: FakeAPI) -> None:
    roster = await client.get_chat_members(GROUP)
    assert roster.member_count == 2
    anna, bruno = roster.members
    assert (anna.user.id, anna.status, anna.is_admin, anna.admin_rights, anna.custom_title) == (
        ANNA,
        SeatStatus.ADMIN,
        True,
        3,
        "guide",
    )
    assert anna.can is not None
    assert (anna.can.delete_messages, anna.can.pin_messages) == (True, False)
    assert (bruno.user.full_name, bruno.status, bruno.rights, bruno.restricted_until, bruno.is_inside) == (
        "Bruno Rossi",
        SeatStatus.RESTRICTED,
        1,
        SENT_AT,
        True,
    )
    assert await client.get_chat_member(GROUP, ANNA) == anna
    with pytest.raises(MemberNotFoundError):
        await client.get_chat_member(GROUP, 4242)


async def test_leave_chat_leaves_for_good(client: Either, world: FakeAPI) -> None:
    assert await client.leave_chat(GROUP) is True
    with pytest.raises(ChatNotFoundError):
        await client.get_chat(GROUP)


async def test_send_typing_says_whether_the_indicator_went_out(
    connect: Callable[..., Either], wire: Tap, world: FakeAPI
) -> None:
    assert await connect(wire).send_typing(PRIVATE) is True
    # a typing sent less than 3 s ago is held back and reported as not sent
    assert await connect(Scripted(ok({"sent": False}))).send_typing(PRIVATE) is False


async def test_get_user_key_hands_out_the_key_the_server_holds(client: Either, world: FakeAPI) -> None:
    key = await client.get_user_key(ANNA)
    assert (key.user_id, key.fingerprint, key.public_key) == (ANNA, FINGERPRINT.upper(), ANNA_KEY["publicKey"])
    with pytest.raises(UserNotFoundError):
        await client.get_user_key(4242)


async def test_resolve_turns_a_handle_into_an_id(client: Either, world: FakeAPI) -> None:
    found = await client.resolve("@Anna")
    assert (found.kind, found.id, found.username, found.mention) == ("user", ANNA, "anna", "@anna")
    assert (found.is_user, found.is_chat) == (True, False)
    assert found.user is not None
    assert (found.user.has_key, found.user.full_name) == (True, "Anna")
    assert found.chat is None

    # a spare handle answers with the one they actually go by
    spare = await client.resolve("anna_too")
    assert (spare.id, spare.username) == (ANNA, "anna")

    # a group comes back as a card that get_chat will take straight away
    room = await client.resolve("hikers")
    assert (room.kind, room.id) == ("group", GROUP)
    assert (room.is_chat, room.is_user) == (True, False)
    assert room.user is None
    assert room.chat is not None
    assert (room.chat.is_group, room.chat.member_count) == (True, 3)
    assert (await client.get_chat(room.chat.id)).id == GROUP

    with pytest.raises(UsernameNotFoundError):
        await client.resolve("nessuno")


async def test_revoke_token_burns_the_token(client: Either) -> None:
    assert await client.revoke_token() is True
    with pytest.raises(AuthenticationError):
        await client.get_me()


INVALID = [
    pytest.param("get_updates", (), {"offset": -1}, "offset", id="get_updates-negative-offset"),
    pytest.param("get_updates", (), {"offset": True}, "offset", id="get_updates-bool-offset"),
    pytest.param("get_updates", (), {"limit": 0}, "limit", id="get_updates-no-limit"),
    pytest.param("get_updates", (), {"limit": 101}, "limit", id="get_updates-limit-too-high"),
    pytest.param("get_updates", (), {"timeout": 51}, "timeout", id="get_updates-timeout-too-long"),
    pytest.param("get_updates", (), {"timeout": 2.5}, "timeout", id="get_updates-fractional-timeout"),
    pytest.param("set_webhook", ("ftp://bot.example/hook",), {}, "url", id="set_webhook-not-http"),
    pytest.param("set_webhook", ("https://bot.example/hook#top",), {}, "url", id="set_webhook-fragment"),
    pytest.param("set_webhook", ("https://bot:pw@bot.example/hook",), {}, "url", id="set_webhook-credentials"),
    pytest.param("set_webhook", ("https://bot.example/hook", "not secret!"), {}, "secret", id="set_webhook-bad-secret"),
    pytest.param("set_key", ("hello", FINGERPRINT), {}, "public_key", id="set_key-not-a-key"),
    pytest.param("set_key", (PRIVATE_KEY, FINGERPRINT), {}, "public_key", id="set_key-private-key"),
    pytest.param(
        "set_key", (PUBLIC_KEY + PRIVATE_KEY, FINGERPRINT), {}, "public_key", id="set_key-private-key-tagging-along"
    ),
    pytest.param("set_key", (PUBLIC_KEY, FINGERPRINT[:-1]), {}, "fingerprint", id="set_key-short-fingerprint"),
    pytest.param("get_commands", ("everyone",), {}, "scope", id="get_commands-unknown-scope"),
    pytest.param("delete_commands", ("chat:",), {}, "scope", id="delete_commands-chat-without-id"),
    pytest.param("set_commands", ([("Not Valid", "x")],), {}, "commands", id="set_commands-bad-name"),
    pytest.param("set_commands", ([("start", "   ")],), {}, "commands", id="set_commands-blank-description"),
    pytest.param("set_commands", ([["start", "Say hello"]],), {}, "commands", id="set_commands-list-not-pair"),
    pytest.param("set_commands", ([(f"c{i}", "x") for i in range(101)],), {}, "commands", id="set_commands-too-many"),
    pytest.param("set_commands", ([("start", "hi")], "everyone"), {}, "scope", id="set_commands-unknown-scope"),
    pytest.param("send_message", (0, "hi"), {}, "chat_id", id="send_message-chat-zero"),
    pytest.param("send_message", (True, "hi"), {}, "chat_id", id="send_message-chat-bool"),
    pytest.param("send_message", (str(PRIVATE), "hi"), {}, "chat_id", id="send_message-chat-str"),
    pytest.param("send_message", (PRIVATE,), {}, "text", id="send_message-no-body"),
    pytest.param("send_message", (PRIVATE, "hi"), {"armored": ARMORED}, None, id="send_message-text-and-armored"),
    pytest.param("send_message", (PRIVATE, " \n\t "), {}, "text", id="send_message-blank"),
    pytest.param("send_message", (PRIVATE, "x" * 4097), {}, "text", id="send_message-too-long"),
    pytest.param("send_message", (PRIVATE, "ring\x07"), {}, "text", id="send_message-control-character"),
    pytest.param("send_message", (PRIVATE,), {"armored": "just text"}, "armored", id="send_message-armor-missing"),
    pytest.param("send_message", (PRIVATE, "hi"), {"reply_to": 0}, "reply_to", id="send_message-reply-zero"),
    pytest.param("send_message", (PRIVATE, "hi"), {"send_id": "not-a-uuid"}, "send_id", id="send_message-bad-send-id"),
    pytest.param("send_message", (PRIVATE, "hi"), {"markup": {"keyboard": []}}, "markup", id="send_message-bad-markup"),
    pytest.param(
        "send_message",
        (PRIVATE, "hi"),
        {"markup": {"inlineKeyboard": [[{"text": "does nothing"}]]}},
        "markup",
        id="send_message-button-without-action",
    ),
    pytest.param("edit_message", (PRIVATE, MESSAGE), {}, None, id="edit_message-nothing-to-change"),
    pytest.param("edit_message", (PRIVATE, 0, "x"), {}, "message_id", id="edit_message-message-zero"),
    pytest.param("edit_message", (-1, MESSAGE, "x"), {}, "chat_id", id="edit_message-negative-chat"),
    pytest.param("delete_message", (PRIVATE, 0), {}, "message_id", id="delete_message-message-zero"),
    pytest.param("delete_message", (0, MESSAGE), {}, "chat_id", id="delete_message-chat-zero"),
    pytest.param("answer_callback", ("",), {}, "callback_id", id="answer_callback-empty-id"),
    pytest.param("answer_callback", ("x" * 65,), {}, "callback_id", id="answer_callback-id-too-long"),
    pytest.param("answer_callback", (CALLBACK, "x" * 201), {}, "text", id="answer_callback-text-too-long"),
    pytest.param("answer_callback", (CALLBACK, "two\nlines"), {}, "text", id="answer_callback-two-lines"),
    pytest.param(
        "answer_callback", (CALLBACK,), {"url": "javascript:alert(1)"}, "url", id="answer_callback-script-url"
    ),
    pytest.param("get_chat", (0,), {}, "chat_id", id="get_chat-zero"),
    pytest.param("get_chat_keys", (-7,), {}, "chat_id", id="get_chat_keys-negative"),
    pytest.param("get_chat_members", (1.5,), {}, "chat_id", id="get_chat_members-float"),
    pytest.param("get_chat_member", (GROUP, 0), {}, "user_id", id="get_chat_member-user-zero"),
    pytest.param("leave_chat", (None,), {}, "chat_id", id="leave_chat-none"),
    pytest.param("send_typing", (0,), {}, "chat_id", id="send_typing-zero"),
    pytest.param("get_user_key", (-ANNA,), {}, "user_id", id="get_user_key-negative"),
    pytest.param("resolve", ("ab",), {}, "username", id="resolve-too-short"),
    pytest.param("resolve", ("a" * 33,), {}, "username", id="resolve-too-long"),
    pytest.param("resolve", ("con-trattino",), {}, "username", id="resolve-not-a-handle"),
    pytest.param("resolve", ("@",), {}, "username", id="resolve-nothing-but-an-at"),
    pytest.param("resolve", (55,), {}, "username", id="resolve-an-id-is-not-a-handle"),
    pytest.param("download_media", (42,), {}, "media", id="download_media-not-a-reference"),
]


@pytest.mark.parametrize(("op", "args", "kwargs", "field"), INVALID)
async def test_bad_arguments_are_refused_before_anything_is_sent(
    client: Either, wire: Tap, op: str, args: tuple[Any, ...], kwargs: dict[str, Any], field: str | None
) -> None:
    with pytest.raises(ValidationError) as caught:
        await getattr(client, op)(*args, **kwargs)
    assert caught.value.field == field
    assert wire.requests == []


@pytest.mark.parametrize(("op", "args", "kwargs", "method", "path"), one_per_operation(repeatable=True))
async def test_every_repeatable_operation_gets_through_a_refused_connection(
    client: Either,
    fake: FakeAPI,
    wire: Tap,
    world: FakeAPI,
    op: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    method: str,
    path: str,
) -> None:
    fake.fail(method, f"^{re.escape(path)}$", network=True)
    await getattr(client, op)(*args, **kwargs)
    assert [request.url.path for request in wire.requests] == [path, path]
    assert wire.answers[0] is None


@pytest.mark.parametrize(("op", "args", "kwargs", "method", "path"), one_per_operation(repeatable=True))
async def test_every_repeatable_operation_gets_through_a_lost_answer(
    client: Either,
    fake: FakeAPI,
    wire: Tap,
    world: FakeAPI,
    op: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    method: str,
    path: str,
) -> None:
    fake.fail(method, f"^{re.escape(path)}$", after=True)
    await getattr(client, op)(*args, **kwargs)
    assert [request.url.path for request in wire.requests] == [path, path]
    assert wire.answers[0] is None


@pytest.mark.parametrize(("op", "args", "kwargs", "method", "path"), one_per_operation(repeatable=False))
@pytest.mark.parametrize(
    ("fault", "error"),
    [
        pytest.param({"network": True}, NetworkError, id="refused"),
        pytest.param({"after": True}, RequestTimeoutError, id="answer-lost"),
        pytest.param({"status": 503, "code": "internal"}, ServerError, id="server-error"),
    ],
)
async def test_polls_and_typing_are_never_repeated(
    client: Either,
    fake: FakeAPI,
    wire: Tap,
    world: FakeAPI,
    slept: list[float],
    op: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    method: str,
    path: str,
    fault: dict[str, Any],
    error: type[PGPCbotError],
) -> None:
    fake.fail(method, f"^{re.escape(path)}$", **fault)
    with pytest.raises(error):
        await getattr(client, op)(*args, **kwargs)
    assert len(wire.requests) == 1
    assert slept == []


async def test_network_retries_stop_at_the_policys_attempts(
    client: Either, fake: FakeAPI, wire: Tap, slept: list[float]
) -> None:
    fake.fail("GET", "/me$", network=True, times=10)
    with pytest.raises(NetworkError) as caught:
        await client.get_me()
    assert type(caught.value) is NetworkError
    assert str(caught.value) == "GET /me: ConnectError: connection refused"
    assert len(wire.requests) == FAST_RETRY.attempts
    assert slept == [0.0] * (FAST_RETRY.attempts - 1)


@pytest.mark.parametrize("attempts", [1, 2])
async def test_a_stingier_policy_tries_fewer_times(
    connect: Callable[..., Either], fake: FakeAPI, wire: Tap, attempts: int
) -> None:
    client = connect(wire, retry=RetryPolicy(attempts=attempts, backoff_base=0, backoff_max=0))
    fake.fail("GET", "/me$", network=True, times=10)
    with pytest.raises(NetworkError):
        await client.get_me()
    assert len(wire.requests) == attempts


async def test_network_retries_back_off_as_the_policy_says(
    connect: Callable[..., Either], fake: FakeAPI, wire: Tap, slept: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    # the top of every jitter range, so the waits come out exact
    monkeypatch.setattr(_http, "random", SimpleNamespace(uniform=lambda low, high: high))
    client = connect(wire, retry=RetryPolicy(attempts=4, backoff_base=0.5, backoff_max=1.5))
    fake.fail("GET", "/me$", network=True, times=3)
    assert (await client.get_me()).id == BOT_ID
    assert slept == [0.5, 1.0, 1.5]


async def test_server_errors_are_retried_until_one_goes_through(client: Either, fake: FakeAPI, wire: Tap) -> None:
    fake.fail("GET", "/me$", status=500, code="internal", times=2)
    assert (await client.get_me()).id == BOT_ID
    assert wire.answers == [500, 500, 200]


async def test_server_errors_are_raised_once_the_attempts_run_out(client: Either, fake: FakeAPI, wire: Tap) -> None:
    fake.fail("GET", "/me$", status=503, code="unavailable", times=10)
    with pytest.raises(ServerError) as caught:
        await client.get_me()
    assert (caught.value.status, caught.value.code, caught.value.method, caught.value.path) == (
        503,
        "unavailable",
        "GET",
        "/me",
    )
    assert len(wire.requests) == FAST_RETRY.attempts


@pytest.mark.parametrize(
    ("status", "code", "error"),
    [
        (400, "bad_request", BadRequestError),
        (401, "unauthorized", AuthenticationError),
        (403, "bot_paused", BotPausedError),
        (404, "not_found", EndpointNotFoundError),
        (409, "webhook_active", WebhookActiveError),
        (410, "callback_expired", CallbackExpiredError),
        (413, "too_large", PayloadTooLargeError),
        (418, "teapot", APIError),
    ],
)
async def test_client_errors_are_never_retried(
    client: Either,
    fake: FakeAPI,
    wire: Tap,
    slept: list[float],
    status: int,
    code: str,
    error: type[APIError],
) -> None:
    fake.fail("GET", "/webhook$", status=status, code=code, times=5)
    with pytest.raises(error) as caught:
        await client.get_webhook()
    assert type(caught.value) is error
    assert (caught.value.status, caught.value.code, caught.value.method, caught.value.path) == (
        status,
        code,
        "GET",
        "/webhook",
    )
    assert len(wire.requests) == 1
    assert slept == []


async def test_every_send_carries_a_fresh_send_id(client: Either, fake: FakeAPI) -> None:
    fake.add_chat(PRIVATE)
    await client.send_message(PRIVATE, "one")
    await client.send_message(PRIVATE, "two")
    send_ids = [call["json"]["sendId"] for call in fake.calls("POST", "/bot/v1/messages")]
    assert len(set(send_ids)) == 2
    assert all(uuid.UUID(send_id).version == 4 for send_id in send_ids)


@pytest.mark.parametrize(
    "given", [uuid.UUID(SEND_ID), SEND_ID.upper(), "{" + SEND_ID + "}"], ids=["uuid", "upper", "braces"]
)
async def test_a_send_id_of_your_own_goes_out_in_canonical_form(client: Either, fake: FakeAPI, given: Any) -> None:
    fake.add_chat(PRIVATE)
    await client.send_message(PRIVATE, "hi", send_id=given)
    assert fake.calls("POST", "/bot/v1/messages")[0]["json"]["sendId"] == SEND_ID


async def test_a_send_whose_answer_was_lost_is_repeated_with_its_send_id_and_stored_once(
    client: Either, fake: FakeAPI
) -> None:
    fake.add_chat(PRIVATE)
    fake.fail("POST", "/messages$", after=True)
    sent = await client.send_message(PRIVATE, "only once")
    first, second = fake.calls("POST", "/bot/v1/messages")
    assert first["json"] == second["json"]
    assert list(fake.messages) == [sent.id]
    assert sent.text == "only once"


async def test_a_send_that_hit_a_server_error_is_repeated_with_the_same_send_id(
    client: Either, fake: FakeAPI, wire: Tap
) -> None:
    fake.add_chat(PRIVATE)
    fake.fail("POST", "/messages$", status=502, code="internal")
    sent = await client.send_message(PRIVATE, "hi")
    first, second = (json.loads(request.content) for request in wire.requests)
    assert first == second
    assert uuid.UUID(first["sendId"]).version == 4
    assert list(fake.messages) == [sent.id]


DONE_CODES = [
    pytest.param(
        "delete_message", (PRIVATE, MESSAGE), "DELETE", f"/bot/v1/messages/{MESSAGE}", 404, id="delete_message"
    ),
    pytest.param(
        "answer_callback", (CALLBACK,), "POST", f"/bot/v1/callbacks/{CALLBACK}/answer", 409, id="answer_callback"
    ),
    pytest.param("leave_chat", (GROUP,), "POST", f"/bot/v1/chats/{GROUP}/leave", 404, id="leave_chat"),
    pytest.param("revoke_token", (), "POST", "/bot/v1/token/revoke", 401, id="revoke_token"),
]


@pytest.mark.parametrize(("op", "args", "method", "path", "status"), DONE_CODES)
async def test_a_done_code_after_a_lost_answer_means_the_first_try_worked(
    client: Either,
    fake: FakeAPI,
    wire: Tap,
    world: FakeAPI,
    op: str,
    args: tuple[Any, ...],
    method: str,
    path: str,
    status: int,
) -> None:
    fake.fail(method, f"^{re.escape(path)}$", after=True)
    assert await getattr(client, op)(*args) is True
    assert wire.answers == [None, status]


@pytest.mark.parametrize(
    ("op", "args", "prepare", "error"),
    [
        pytest.param("delete_message", (PRIVATE, 4242), lambda fake: None, MessageNotFoundError, id="delete_message"),
        pytest.param(
            "answer_callback",
            (CALLBACK,),
            lambda fake: fake.callbacks[CALLBACK].update(answered=True),
            CallbackAnsweredError,
            id="answer_callback",
        ),
        pytest.param("leave_chat", (4242,), lambda fake: None, ChatNotFoundError, id="leave_chat"),
        pytest.param(
            "revoke_token", (), lambda fake: setattr(fake, "revoked", True), AuthenticationError, id="revoke_token"
        ),
    ],
)
async def test_the_same_code_on_a_clean_first_try_is_raised(
    client: Either,
    wire: Tap,
    world: FakeAPI,
    op: str,
    args: tuple[Any, ...],
    prepare: Callable[[FakeAPI], None],
    error: type[APIError],
) -> None:
    prepare(world)
    with pytest.raises(error):
        await getattr(client, op)(*args)
    assert len(wire.requests) == 1


async def test_a_rate_limit_is_no_reason_to_believe_a_done_code(
    client: Either, fake: FakeAPI, wire: Tap, world: FakeAPI, slept: list[float]
) -> None:
    fake.fail("DELETE", "/messages/4242$", status=429, code="flood_wait", retryAfter=1)
    with pytest.raises(MessageNotFoundError):
        await client.delete_message(PRIVATE, 4242)
    assert wire.answers == [429, 404]
    assert len(slept) == 1


async def test_a_flood_wait_is_waited_out_and_the_send_goes_through(
    client: Either, fake: FakeAPI, wire: Tap, slept: list[float]
) -> None:
    fake.add_chat(PRIVATE)
    fake.fail("POST", "/messages$", status=429, code="flood_wait", retryAfter=3)
    sent = await client.send_message(PRIVATE, "patience")
    [wait] = slept
    assert 3.05 <= wait <= 3.25
    assert wire.answers == [429, 201]
    assert list(fake.messages) == [sent.id]


async def test_a_wait_longer_than_the_policy_allows_is_raised_at_once(
    client: Either, fake: FakeAPI, wire: Tap, slept: list[float]
) -> None:
    fake.add_chat(PRIVATE)
    fake.fail("POST", "/messages$", status=429, code="flood_wait", retryAfter=6)
    with pytest.raises(FloodWaitError) as caught:
        await client.send_message(PRIVATE, "hurry")
    assert caught.value.retry_after == 6.0
    assert len(wire.requests) == 1
    assert slept == []


async def test_only_so_many_rate_limits_are_waited_out(
    client: Either, fake: FakeAPI, wire: Tap, slept: list[float]
) -> None:
    fake.fail("GET", "/me$", status=429, code="rate_limited", retryAfter=1, times=10)
    with pytest.raises(RateLimitError):
        await client.get_me()
    assert len(wire.requests) == FAST_RETRY.rate_limit_retries + 1
    assert len(slept) == FAST_RETRY.rate_limit_retries


async def test_the_per_minute_limit_waits_what_the_header_says_not_the_body(
    connect: Callable[..., Either], slept: list[float]
) -> None:
    # the per-minute limiter always writes 60 in the body; the header has the real time left
    transport = Scripted(refusal(429, "rate_limited", retryAfter=60, headers={"Retry-After": "2"}), ok(ME))
    assert (await connect(transport).get_me()).id == BOT_ID
    [wait] = slept
    assert 2.05 <= wait <= 2.25


async def test_a_rate_limit_that_names_no_time_waits_a_second(
    connect: Callable[..., Either], slept: list[float]
) -> None:
    transport = Scripted(refusal(429, "rate_limited"), ok(ME))
    assert (await connect(transport).get_me()).id == BOT_ID
    [wait] = slept
    assert 1.05 <= wait <= 1.25


async def test_a_retry_after_given_as_a_date_is_waited_out_until_then(
    connect: Callable[..., Either], slept: list[float]
) -> None:
    then = format_datetime(datetime.now(UTC) + timedelta(seconds=3), usegmt=True)
    transport = Scripted(refusal(429, "flood_wait", headers={"Retry-After": then}), ok(ME))
    assert (await connect(transport).get_me()).id == BOT_ID
    [wait] = slept
    # an HTTP date has whole seconds only: up to one of the three may be gone already
    assert 1.5 <= wait <= 3.25


async def test_even_a_call_that_never_repeats_waits_out_a_rate_limit(
    client: Either, fake: FakeAPI, wire: Tap, slept: list[float]
) -> None:
    fake.add_chat(PRIVATE)
    fake.fail("POST", "/typing$", status=429, code="flood_wait", retryAfter=2)
    assert await client.send_typing(PRIVATE) is True
    assert wire.answers == [429, 200]
    assert len(slept) == 1


@pytest.mark.parametrize(
    ("op", "args", "method", "path", "code", "penalties"),
    [
        pytest.param(
            "send_message",
            (PRIVATE, "hi"),
            "POST",
            "/messages$",
            "flood_wait",
            [{"seconds": 2.0, "chat_id": PRIVATE, "edit": False}],
            id="flood-wait-on-a-send",
        ),
        pytest.param(
            "edit_message",
            (PRIVATE, MESSAGE, "hi"),
            "PATCH",
            "/messages/[0-9]+$",
            "flood_wait",
            [{"seconds": 2.0, "chat_id": PRIVATE, "edit": True}],
            id="flood-wait-on-an-edit",
        ),
        pytest.param("get_me", (), "GET", "/me$", "rate_limited", [{"seconds": 2.0, "calls": True}], id="rate-limited"),
        pytest.param("send_message", (PRIVATE, "hi"), "POST", "/messages$", "slow_mode", [], id="slow-mode"),
    ],
)
async def test_the_limiter_learns_from_a_429_the_server_sent_anyway(
    connect: Callable[..., Either],
    fake: FakeAPI,
    wire: Tap,
    world: FakeAPI,
    op: str,
    args: tuple[Any, ...],
    method: str,
    path: str,
    code: str,
    penalties: list[dict[str, Any]],
) -> None:
    limiter = SpyLimiter()
    client = connect(wire, rate_limiter=limiter)
    fake.fail(method, path, status=429, code=code, retryAfter=2)
    await getattr(client, op)(*args)
    assert limiter.penalties == penalties


async def test_the_limiter_hears_of_a_flood_wait_even_when_the_client_gives_up(
    connect: Callable[..., Either], fake: FakeAPI, wire: Tap
) -> None:
    limiter = SpyLimiter()
    client = connect(wire, rate_limiter=limiter)
    fake.add_chat(PRIVATE)
    fake.fail("POST", "/messages$", status=429, code="flood_wait", retryAfter=600)
    with pytest.raises(FloodWaitError):
        await client.send_message(PRIVATE, "hi")
    assert limiter.penalties == [{"seconds": 600.0, "chat_id": PRIVATE, "edit": False}]


async def test_the_limiter_is_asked_before_every_attempt(
    connect: Callable[..., Either], fake: FakeAPI, wire: Tap
) -> None:
    limiter = SpyLimiter()
    client = connect(wire, rate_limiter=limiter)
    fake.add_chat(PRIVATE)
    fake.fail("POST", "/messages$", network=True)
    await client.send_message(PRIVATE, "hi", chat_kind=ChatKind.PRIVATE)
    await client.get_me()
    send = {"send": True, "edit": False, "chat_id": PRIVATE, "chat_kind": ChatKind.PRIVATE}
    assert limiter.acquired == [send, send, {"send": False, "edit": False, "chat_id": None, "chat_kind": None}]


async def test_the_rate_limiter_option(connect: Callable[..., Either]) -> None:
    assert isinstance(connect(None, rate_limiter=True).rate_limiter, RateLimiter)
    assert connect(None, rate_limiter=False).rate_limiter is None
    own = RateLimiter()
    assert connect(None, rate_limiter=own).rate_limiter is own


async def test_retries_and_rate_limit_waits_are_logged(
    client: Either, fake: FakeAPI, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="pgpcbot.client")
    fake.fail("GET", "/me$", network=True)
    fake.fail("GET", "/webhook$", status=429, code="rate_limited", retryAfter=2)
    await client.get_me()
    await client.get_webhook()
    retry, wait = (record for record in caplog.records if record.name == "pgpcbot.client")
    assert retry.levelno == logging.WARNING
    assert retry.getMessage() == "GET /me failed (GET /me: ConnectError: connection refused), retry 1 in 0.0s"
    assert wait.levelno == logging.INFO
    assert wait.getMessage().startswith("GET /webhook: rate_limited, waiting 2.")


PROXY_PAGE = (
    "<html><head><title>502 Bad Gateway</title></head>"
    "<body><center><h1>502 Bad Gateway</h1></center><hr><center>nginx</center></body></html>"
)


async def test_a_proxy_error_page_is_a_server_error_and_is_retried(connect: Callable[..., Either]) -> None:
    transport = Scripted(page(502, PROXY_PAGE))
    with pytest.raises(ServerError) as caught:
        await connect(transport).get_me()
    error = caught.value
    assert (error.status, error.code, error.method, error.path) == (502, "http_502", "GET", "/me")
    assert "502 Bad Gateway" in error.message
    assert len(transport.requests) == FAST_RETRY.attempts


async def test_a_proxy_error_page_then_a_real_answer_goes_through(connect: Callable[..., Either]) -> None:
    transport = Scripted(page(502, PROXY_PAGE), ok(ME))
    assert (await connect(transport).get_me()).id == BOT_ID
    assert len(transport.requests) == 2


@pytest.mark.parametrize(
    ("status", "body"),
    [
        pytest.param(200, b"Welcome to nginx!", id="plain-text"),
        pytest.param(200, b"", id="empty"),
        pytest.param(200, b"\xff\xfe\x00\x81", id="binary"),
        pytest.param(200, b"[1, 2, 3]", id="json-list"),
        pytest.param(200, b'{"result": {"id": 412}}', id="no-ok"),
        pytest.param(200, b'{"ok": "true", "result": {"id": 412}}', id="ok-not-a-boolean"),
        pytest.param(404, b"<html>Not Found</html>", id="a-404-page-from-the-wrong-host"),
    ],
)
async def test_an_answer_without_the_envelope_is_unexpected_and_not_retried(
    connect: Callable[..., Either], status: int, body: bytes
) -> None:
    transport = Scripted(lambda request: httpx2.Response(status, content=body))
    with pytest.raises(UnexpectedResponseError) as caught:
        await connect(transport).get_me()
    assert (caught.value.status, caught.value.code) == (status, f"http_{status}")
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    ("op", "args", "result"),
    [
        pytest.param("get_me", (), {"username": "testbot"}, id="bot-without-id"),
        pytest.param("get_updates", (), {"updates": []}, id="updates-not-a-list"),
        pytest.param("get_commands", (), {"default": []}, id="scopes-not-a-list"),
        pytest.param("get_commands", ("private",), "start", id="commands-not-a-list"),
        pytest.param("send_message", (PRIVATE, "hi"), {"id": 1002, "chat": {"id": PRIVATE}}, id="message-without-date"),
        pytest.param("revoke_token", (), True, id="yes-or-no-not-an-object"),
    ],
)
async def test_a_result_of_the_wrong_shape_is_a_payload_error_and_not_retried(
    connect: Callable[..., Either], op: str, args: tuple[Any, ...], result: Any
) -> None:
    transport = Scripted(ok(result))
    with pytest.raises(PayloadError):
        await getattr(connect(transport), op)(*args)
    assert len(transport.requests) == 1


async def test_an_answer_over_the_size_cap_is_dropped(client: Either, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", 64)
    with pytest.raises(NetworkError, match="larger than 64 bytes") as caught:
        await client.get_me()
    assert not isinstance(caught.value, RequestTimeoutError)


async def test_an_answer_exactly_at_the_size_cap_is_read(
    connect: Callable[..., Either], monkeypatch: pytest.MonkeyPatch
) -> None:
    body = json.dumps({"ok": True, "result": ME}).encode()
    monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", len(body))
    transport = Scripted(lambda request: httpx2.Response(200, content=body))
    assert (await connect(transport).get_me()).id == BOT_ID


async def test_an_endless_answer_is_cut_off_while_it_streams(
    connect: Callable[..., Either], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", 10)
    streams: list[Chunks] = []

    def endless(request: httpx2.Request) -> httpx2.Response:
        streams.append(Chunks(*[b"abcd"] * 1000))
        return httpx2.Response(200, stream=streams[-1])

    with pytest.raises(NetworkError, match="larger than 10 bytes"):
        await connect(Scripted(endless)).get_me()
    assert [stream.served for stream in streams] == [3] * len(streams)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_redirects_are_not_followed(connect: Callable[..., Either], status: int) -> None:
    transport = Scripted(
        lambda request: httpx2.Response(status, headers={"Location": "https://collector.test/bot/v1/me"})
    )
    with pytest.raises(UnexpectedResponseError) as caught:
        await connect(transport).get_me()
    assert caught.value.code == f"http_{status}"
    assert [request.url.host for request in transport.requests] == ["api.test"]


@pytest.mark.parametrize(
    "failure", [httpx2.ConnectTimeout, httpx2.ReadTimeout, httpx2.WriteTimeout, httpx2.PoolTimeout]
)
async def test_timeouts_are_request_timeout_errors(
    connect: Callable[..., Either], failure: type[httpx2.TimeoutException]
) -> None:
    transport = Scripted(broken(failure, "timed out"))
    with pytest.raises(RequestTimeoutError) as caught:
        await connect(transport).get_me()
    assert str(caught.value) == f"GET /me: no answer in time ({failure.__name__}: timed out)"
    assert len(transport.requests) == FAST_RETRY.attempts


@pytest.mark.parametrize("failure", [httpx2.ConnectError, httpx2.ReadError, httpx2.RemoteProtocolError])
async def test_other_line_failures_are_plain_network_errors(
    connect: Callable[..., Either], failure: type[httpx2.TransportError]
) -> None:
    with pytest.raises(NetworkError) as caught:
        await connect(Scripted(broken(failure, "gone"))).get_me()
    assert type(caught.value) is NetworkError
    assert str(caught.value) == f"GET /me: {failure.__name__}: gone"


async def test_a_timeout_while_the_body_streams_in_is_a_request_timeout(connect: Callable[..., Either]) -> None:
    def stalls(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, stream=Chunks(b'{"ok": tr', httpx2.ReadTimeout("stalled")))

    with pytest.raises(RequestTimeoutError):
        await connect(Scripted(stalls)).get_me()


@pytest.mark.parametrize(
    "reference",
    [
        pytest.param(Media(id=MEDIA_ID, kind="photo"), id="media-without-url"),
        pytest.param(Media(id=MEDIA_ID, kind="photo", url=f"/media/{MEDIA_ID}"), id="media-with-path"),
        pytest.param(Media(id=MEDIA_ID, kind="photo", url=f"{BASE}/media/{MEDIA_ID}"), id="media-with-url"),
        pytest.param(MEDIA_ID, id="bare-id"),
        pytest.param(f"/media/{MEDIA_ID}", id="path"),
        pytest.param(f"{BASE}/media/{MEDIA_ID}", id="url"),
    ],
)
async def test_download_media_fetches_the_stored_bytes_without_the_token(
    client: Either, fake: FakeAPI, reference: Media | str
) -> None:
    fake.media[MEDIA_ID] = ATTACHMENT
    assert await client.download_media(reference) == ATTACHMENT
    [fetched] = fake.calls("GET", f"/media/{MEDIA_ID}")
    assert "authorization" not in fetched["headers"]
    assert fetched["headers"]["user-agent"].startswith("PGPCbot/")


@pytest.mark.parametrize(
    "reference",
    [
        pytest.param(f"https://evil.test/media/{MEDIA_ID}", id="another-host"),
        pytest.param(f"http://api.test/media/{MEDIA_ID}", id="another-scheme"),
        pytest.param(f"https://api.test:8443/media/{MEDIA_ID}", id="another-port"),
        pytest.param(f"https://api.test@evil.test/media/{MEDIA_ID}", id="userinfo-trick"),
        pytest.param(f"//evil.test/media/{MEDIA_ID}", id="scheme-relative"),
        pytest.param("/bot/v1/me", id="api-path"),
        pytest.param(f"{BASE}/bot/v1/token/revoke", id="api-url"),
        pytest.param(f"/media/{MEDIA_ID}/../../bot/v1/me", id="climbing-out"),
        pytest.param(Media(id="../bot/v1/me", kind="photo"), id="forged-id"),
        pytest.param(Media(id=MEDIA_ID, kind="photo", url="https://evil.test/steal"), id="forged-url"),
        pytest.param(f"/media/{MEDIA_ID}\n", id="trailing-newline"),
    ],
)
async def test_download_media_refuses_anything_but_a_media_path_on_the_api_host(
    client: Either, wire: Tap, reference: Media | str
) -> None:
    with pytest.raises(ValidationError) as caught:
        await client.download_media(reference)
    assert caught.value.field == "media"
    assert wire.requests == []


async def test_download_media_takes_a_file_exactly_at_the_cap(client: Either, fake: FakeAPI) -> None:
    fake.media[MEDIA_ID] = b"x" * 16
    assert await client.download_media(MEDIA_ID, max_bytes=16) == b"x" * 16


async def test_download_media_refuses_a_declared_size_over_the_cap_without_reading_it(
    connect: Callable[..., Either],
) -> None:
    body = Chunks(b"x" * 16)
    transport = Scripted(lambda request: httpx2.Response(200, headers={"Content-Length": "16"}, stream=body))
    with pytest.raises(ValidationError, match="16 bytes"):
        await connect(transport).download_media(MEDIA_ID, max_bytes=15)
    assert body.served == 0


async def test_download_media_stops_reading_once_the_cap_is_crossed(connect: Callable[..., Either]) -> None:
    body = Chunks(*[b"abcd"] * 1000)
    transport = Scripted(lambda request: httpx2.Response(200, stream=body))
    with pytest.raises(ValidationError, match="larger than max_bytes=10"):
        await connect(transport).download_media(MEDIA_ID, max_bytes=10)
    assert body.served == 3


async def test_a_missing_file_is_media_not_found(client: Either, wire: Tap) -> None:
    with pytest.raises(APIError) as caught:
        await client.download_media(MEDIA_ID)
    error = caught.value
    assert (error.status, error.code, error.method, error.path) == (404, "media_not_found", "GET", f"/media/{MEDIA_ID}")
    assert len(wire.requests) == 1


async def test_other_media_failures_carry_their_http_status(connect: Callable[..., Either]) -> None:
    transport = Scripted(page(500, "<html>oops</html>"))
    with pytest.raises(APIError) as caught:
        await connect(transport).download_media(MEDIA_ID)
    assert (caught.value.status, caught.value.code) == (500, "http_500")
    assert len(transport.requests) == 1


async def test_a_media_download_that_cannot_connect_is_a_network_error(connect: Callable[..., Either]) -> None:
    with pytest.raises(NetworkError) as caught:
        await connect(Scripted(broken(httpx2.ConnectError, "connection refused"))).download_media(MEDIA_ID)
    assert str(caught.value) == f"GET /media/{MEDIA_ID}: ConnectError: connection refused"


async def test_a_media_download_that_times_out_is_a_request_timeout(connect: Callable[..., Either]) -> None:
    with pytest.raises(RequestTimeoutError):
        await connect(Scripted(broken(httpx2.ReadTimeout, "no bytes for 120 s"))).download_media(MEDIA_ID)


async def test_repr_and_token_hint_show_the_bot_id_and_the_last_four_only(client: Either) -> None:
    assert client.token_hint == HINT
    for shown in (repr(client.inner), str(client.inner)):
        assert HINT in shown
        assert "bot_id=412" in shown
        assert SECRET not in shown


def refused_quoting_the_token(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError(f"proxy refused the tunnel, Authorization: Bearer {TOKEN}", request=request)


def timed_out_quoting_the_token(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ReadTimeout(f"no answer to {request.method} with Bearer {TOKEN}", request=request)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(refused_quoting_the_token, id="line-error-quoting-the-token"),
        pytest.param(timed_out_quoting_the_token, id="timeout-quoting-the-token"),
        pytest.param(refusal(401, "unauthorized", "missing, revoked or invalid bot token"), id="unauthorized"),
        pytest.param(refusal(500, "internal"), id="server-error"),
        pytest.param(refusal(429, "rate_limited", retryAfter=600), id="rate-limited-for-too-long"),
    ],
)
@pytest.mark.usefixtures("no_redaction")
async def test_a_failing_call_never_shows_the_token(
    connect: Callable[..., Either], caplog: pytest.LogCaptureFixture, failure: Answer
) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(PGPCbotError) as caught:
        await connect(Scripted(failure)).get_me()
    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value)
    assert [record.getMessage() for record in caplog.records if SECRET in record.getMessage()] == []


def proxy_page_echoing_the_request(request: httpx2.Request) -> httpx2.Response:
    html = f"<html><h1>502 Bad Gateway</h1><pre>Authorization: {request.headers['authorization']}</pre></html>"
    return httpx2.Response(502, text=html, headers={"Content-Type": "text/html"})


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(proxy_page_echoing_the_request, id="proxy-page"),
        pytest.param(refusal(401, "unauthorized", f"token {TOKEN} was revoked"), id="error-message"),
    ],
)
@pytest.mark.usefixtures("no_redaction")
async def test_an_answer_echoing_the_token_does_not_carry_it_into_errors_or_logs(
    connect: Callable[..., Either], caplog: pytest.LogCaptureFixture, failure: Answer
) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(PGPCbotError) as caught:
        await connect(Scripted(failure)).get_me()
    assert SECRET not in str(caught.value)
    assert [record.getMessage() for record in caplog.records if SECRET in record.getMessage()] == []


async def test_token_and_server_come_from_the_environment(
    connect: Callable[..., Either], fake: FakeAPI, wire: Tap, monkeypatch: pytest.MonkeyPatch
) -> None:
    # as read from a secrets file, trailing newline and all
    monkeypatch.setenv("PGPCHAT_BOT_TOKEN", f"  {TOKEN}\n")
    monkeypatch.setenv("PGPCHAT_API", f"{BASE}/bot/v1/")
    client = connect(wire, token=None, api_url=None)
    assert (client.bot_id, client.api_url, client.token_hint) == (BOT_ID, BASE, HINT)
    assert (await client.get_me()).id == BOT_ID
    assert fake.calls()[0]["headers"]["authorization"] == f"Bearer {TOKEN}"


async def test_arguments_win_over_the_environment(
    connect: Callable[..., Either], fake: FakeAPI, wire: Tap, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PGPCHAT_BOT_TOKEN", "pgpbot_999." + "B" * 43)
    monkeypatch.setenv("PGPCHAT_API", "https://elsewhere.test")
    client = connect(wire)
    await client.get_me()
    assert (client.bot_id, client.api_url) == (BOT_ID, BASE)
    assert fake.calls()[0]["headers"]["authorization"] == f"Bearer {TOKEN}"


async def test_the_server_defaults_to_pgpchat_app(connect: Callable[..., Either]) -> None:
    assert connect(None, api_url=None).api_url == "https://api.pgpchat.app"


@pytest.mark.parametrize("token", [None, ""], ids=["absent", "empty"])
def test_no_token_anywhere_is_a_configuration_error(
    flavour: str, monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    if token is not None:
        monkeypatch.setenv("PGPCHAT_BOT_TOKEN", token)
    with pytest.raises(ConfigurationError, match="PGPCHAT_BOT_TOKEN"):
        client_class(flavour)(api_url=BASE)


LEAKABLE = "Zx9" + "q" * 40


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(f"pgpbot_412.{LEAKABLE[:20]}", id="too-short"),
        pytest.param(f"pgpbot_412.{LEAKABLE}q", id="too-long"),
        pytest.param(f"pgpbot_0.{LEAKABLE}", id="bot-zero"),
        pytest.param(f"pgpbot_412.{LEAKABLE[:-1]}!", id="bad-character"),
        pytest.param(f"pgpbot_412.{LEAKABLE}".encode(), id="bytes"),
        pytest.param(f"Bearer pgpbot_412.{LEAKABLE}", id="with-bearer"),
    ],
)
def test_a_malformed_token_is_refused_without_being_repeated(flavour: str, token: Any) -> None:
    with pytest.raises(ConfigurationError) as caught:
        client_class(flavour)(token, api_url=BASE)
    assert "Zx9" not in str(caught.value)


def test_a_token_pasted_with_bearer_gets_a_hint(flavour: str) -> None:
    with pytest.raises(ConfigurationError, match="without 'Bearer '"):
        client_class(flavour)(f"Bearer {TOKEN}", api_url=BASE)


@pytest.mark.parametrize("url", ["http://api.test", "http://localhost.evil.test", "http://10.0.0.5:8080"])
def test_plain_http_is_refused_away_from_this_machine(flavour: str, url: str) -> None:
    with pytest.raises(ConfigurationError, match="plain http"):
        client_class(flavour)(TOKEN, api_url=url)


def test_the_environment_server_gets_the_same_checks(flavour: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGPCHAT_API", "http://api.test")
    with pytest.raises(ConfigurationError, match="plain http"):
        client_class(flavour)(TOKEN)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost:8080", "http://localhost:8080"),
        ("http://127.0.0.1:3000/bot/v1", "http://127.0.0.1:3000"),
        ("http://[::1]:3000/", "http://[::1]:3000"),
    ],
)
async def test_plain_http_is_fine_on_this_machine(connect: Callable[..., Either], url: str, expected: str) -> None:
    assert connect(None, api_url=url).api_url == expected


async def test_plain_http_elsewhere_takes_an_explicit_opt_in(connect: Callable[..., Either]) -> None:
    client = connect(None, api_url="http://10.0.0.5:8080", allow_insecure_http=True)
    assert client.api_url == "http://10.0.0.5:8080"


@pytest.mark.parametrize(
    "url",
    [
        "api.test",
        "ftp://api.test",
        "https://bot:pw@api.test",
        "https://api.test/?debug=1",
        "https://api.test/#top",
        "https://[::1",
    ],
)
def test_an_api_url_that_is_not_a_plain_origin_is_refused(flavour: str, url: str) -> None:
    with pytest.raises(ConfigurationError):
        client_class(flavour)(TOKEN, api_url=url)


async def test_a_custom_user_agent_replaces_the_default(
    connect: Callable[..., Either], fake: FakeAPI, wire: Tap
) -> None:
    await connect(wire, user_agent="weather-bot/2.1").get_me()
    assert fake.calls()[0]["headers"]["user-agent"] == "weather-bot/2.1"


@pytest.mark.parametrize(
    ("option", "expected"),
    [
        pytest.param(5, {"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0}, id="seconds"),
        pytest.param(60, {"connect": 10.0, "read": 60.0, "write": 60.0, "pool": 60.0}, id="seconds-connect-capped"),
        pytest.param(
            httpx2.Timeout(12.0, connect=3.0),
            {"connect": 3.0, "read": 12.0, "write": 12.0, "pool": 12.0},
            id="httpx-timeout",
        ),
    ],
)
async def test_the_timeout_option_reaches_every_request(
    connect: Callable[..., Either], wire: Tap, option: float | httpx2.Timeout, expected: dict[str, float]
) -> None:
    await connect(wire, timeout=option).get_me()
    assert wire.requests[0].extensions["timeout"] == expected


@pytest.mark.parametrize(
    ("attempts", "rate_limit_retries", "backoff"),
    [
        pytest.param(0, 5, 0.5, id="no-attempt-at-all"),
        pytest.param(4, -1, 0.5, id="negative-rate-limit-retries"),
        pytest.param(4, 5, -0.5, id="negative-backoff"),
    ],
)
def test_a_retry_policy_that_cannot_work_is_refused(attempts: int, rate_limit_retries: int, backoff: float) -> None:
    with pytest.raises(ValueError, match=r"at least 1|must not be negative"):
        RetryPolicy(attempts=attempts, rate_limit_retries=rate_limit_retries, backoff_base=backoff)


async def test_a_client_refuses_to_be_pickled(client: Either) -> None:
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        with pytest.raises(TypeError, match="cannot be pickled"):
            pickle.dumps(client.inner, protocol=protocol)


async def test_async_client_closes_the_http_client_it_made_on_exit(fake: FakeAPI) -> None:
    tap = Tap(fake)
    async with AsyncClient(TOKEN, api_url=BASE, transport=tap, rate_limiter=False) as api:
        await api.get_me()
        assert tap.closed == 0
    assert tap.closed == 1


def test_async_client_serves_one_short_lived_loop_after_another(fake: FakeAPI) -> None:
    # what SyncBot.process_update does: a fresh loop each time, the client closed at its end
    tap = Tap(fake)
    api = AsyncClient(TOKEN, api_url=BASE, transport=tap, rate_limiter=False)

    async def once() -> int:
        try:
            return (await api.get_me()).id
        finally:
            await api.aclose()

    assert [asyncio.run(once()), asyncio.run(once())] == [BOT_ID, BOT_ID]
    assert (tap.closed, len(tap.requests)) == (2, 2)


async def test_async_client_leaves_an_http_client_it_was_given_open(fake: FakeAPI) -> None:
    tap = Tap(fake)
    async with httpx2.AsyncClient(transport=tap) as http:
        api = AsyncClient(TOKEN, api_url=BASE, http_client=http, rate_limiter=False)
        async with api:
            await api.get_me()
        await api.aclose()
        assert not http.is_closed
        assert tap.closed == 0
        await api.get_me()
        assert [request.headers["authorization"] for request in tap.requests] == [f"Bearer {TOKEN}"] * 2
    with pytest.raises(ConfigurationError, match="was closed"):
        await api.get_me()
    assert len(tap.requests) == 2


def test_sync_client_closes_on_exit_and_stays_closed(fake: FakeAPI) -> None:
    tap = Tap(fake)
    with Client(TOKEN, api_url=BASE, transport=tap, rate_limiter=False) as api:
        api.get_me()
        assert tap.closed == 0
    assert tap.closed == 1
    with pytest.raises(ConfigurationError, match="was closed"):
        api.get_me()
    assert len(tap.requests) == 1


def test_sync_client_leaves_an_http_client_it_was_given_open(fake: FakeAPI) -> None:
    tap = Tap(fake)
    with httpx2.Client(transport=tap) as http:
        api = Client(TOKEN, api_url=BASE, http_client=http, rate_limiter=False)
        with api:
            api.get_me()
        api.close()
        assert not http.is_closed
        assert tap.closed == 0
        api.get_me()
        assert [request.headers["authorization"] for request in tap.requests] == [f"Bearer {TOKEN}"] * 2
    with pytest.raises(ConfigurationError, match="was closed"):
        api.get_me()


def test_a_closed_sync_client_refuses_downloads_like_every_other_call(fake: FakeAPI) -> None:
    api = Client(TOKEN, api_url=BASE, transport=fake, rate_limiter=False)
    api.close()
    with pytest.raises(ConfigurationError):
        api.download_media(MEDIA_ID)


# --- where the token comes from, and clients that outlive an event loop ---


def test_the_token_can_come_from_a_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, fake: FakeAPI) -> None:
    secret_file = tmp_path / "token"
    # a byte order mark and a newline, the way Notepad and echo leave a file
    secret_file.write_text("\ufeff" + TOKEN + "\n", encoding="utf-8")
    monkeypatch.setenv("PGPCHAT_BOT_TOKEN_FILE", str(secret_file))
    with Client(api_url=BASE, transport=fake, rate_limiter=False) as api:
        assert api.get_me().id == fake.me["id"]


def test_the_token_can_be_a_secret_or_a_function() -> None:
    from pgpcbot import Secret

    assert Client(Secret(TOKEN), api_url=BASE).bot_id == Client(lambda: TOKEN, api_url=BASE).bot_id
    with pytest.raises(ConfigurationError, match="_FILE"):
        Client(api_url=BASE)


def test_an_async_client_works_across_two_asyncio_runs(fake: FakeAPI) -> None:
    api = AsyncClient(TOKEN, api_url=BASE, transport=fake, rate_limiter=False)
    # the second run used to reuse a connection pool tied to the first, closed loop
    assert asyncio.run(api.get_me()).id == asyncio.run(api.get_me()).id
    asyncio.run(api.aclose())


def test_a_missing_media_file_is_a_not_found_error(fake: FakeAPI) -> None:
    from pgpcbot.errors import NotFoundError

    with Client(TOKEN, api_url=BASE, transport=fake, rate_limiter=False) as api, pytest.raises(NotFoundError) as caught:
        api.download_media(MEDIA_ID)
    assert caught.value.code == "media_not_found"


def test_an_answer_too_large_to_read_is_not_asked_for_again(fake: FakeAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", 10)
    api = Client(TOKEN, api_url=BASE, transport=fake, rate_limiter=False, retry=RetryPolicy(attempts=4))
    with api, pytest.raises(NetworkError, match="larger than"):
        api.get_me()
    assert len(fake.calls("GET", "/bot/v1/me")) == 1
