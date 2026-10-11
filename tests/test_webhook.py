from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from pgpcbot import Bot
from pgpcbot.errors import ConfigurationError, StorageError, ValidationError
from pgpcbot.models import Message
from pgpcbot.storage import MemoryStorage
from pgpcbot.webhook import SECRET_HEADER, WebhookApp, WebhookResult, generate_secret

from .fakeapi import FakeAPI

SECRET = "pgp_webhook-Secret_0123456789"
URL = "https://bot.example/pgp/hook"


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    body: Any


Receive = Callable[[], Awaitable[dict[str, Any]]]


async def request(
    app: WebhookApp,
    *,
    method: str = "POST",
    path: str = "/",
    root_path: str = "",
    headers: Sequence[tuple[str, str | bytes]] = (),
    chunks: Sequence[bytes] = (b"",),
    receive: Receive | None = None,
) -> Reply:
    """One HTTP request through the app, the way an ASGI server makes it."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "root_path": root_path,
        "query_string": b"",
        "headers": [(k.encode("latin-1"), v if isinstance(v, bytes) else v.encode("latin-1")) for k, v in headers],
        "client": ("203.0.113.9", 51234),
        "server": ("bot.example", 443),
    }
    if receive is None:
        messages = [
            {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1} for i, chunk in enumerate(chunks)
        ]

        async def receive() -> dict[str, Any]:
            return messages.pop(0)

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start, body = sent
    assert start["type"] == "http.response.start"
    assert body["type"] == "http.response.body"
    headers_out = {k.decode(): v.decode() for k, v in start["headers"]}
    return Reply(start["status"], headers_out, json.loads(body["body"]))


async def post(
    app: WebhookApp, body: bytes, *, secret: str | bytes | None = SECRET, path: str = "/", root_path: str = ""
) -> Reply:
    headers: list[tuple[str, str | bytes]] = [("content-type", "application/json"), ("content-length", str(len(body)))]
    if secret is not None:
        headers.append((SECRET_HEADER, secret))
    return await request(app, path=path, root_path=root_path, headers=headers, chunks=(body,))


def delivery(fake: FakeAPI, text: str = "hello", chat_id: int = 7) -> bytes:
    """An update the way the server POSTs it."""
    if chat_id not in fake.chats:
        fake.add_chat(chat_id)
    fake.push_message(chat_id, text)
    with fake.lock:
        update = fake.queue.pop()
    return json.dumps(update).encode()


class Inbox:
    """The bot's message handler: what reached it, and a gate that can hold it inside."""

    def __init__(self, bot: Bot, *, hold: bool = False) -> None:
        self.entered: list[str | None] = []
        self.texts: list[str | None] = []
        self._gate = asyncio.Event()
        if not hold:
            self._gate.set()

        @bot.on_message()
        async def on_message(message: Message) -> None:
            self.entered.append(message.text)
            await self._gate.wait()
            self.texts.append(message.text)

    def release(self) -> None:
        self._gate.set()


class Lifespan:
    """The lifespan conversation an ASGI server like uvicorn has with the app."""

    def __init__(self, app: WebhookApp) -> None:
        self._to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}}
        self._task = asyncio.create_task(app(scope, self._to_app.get, self._from_app.put))

    async def say(self, kind: str) -> dict[str, Any]:
        await self._to_app.put({"type": kind})
        return await asyncio.wait_for(self._from_app.get(), 5)

    async def ended(self) -> None:
        await asyncio.wait_for(self._task, 5)


async def until(condition: Callable[[], bool], seconds: float = 2.0) -> None:
    async def check() -> None:
        # plain state set by the bot's own tasks: there is no event to wait on
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.005)

    await asyncio.wait_for(check(), seconds)


def records(caplog: pytest.LogCaptureFixture, logger: str, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == logger and r.levelno == level]


@pytest.mark.parametrize(("secret", "said"), [(None, "missing"), ("not-the-secret", "wrong")])
async def test_a_delivery_without_the_right_secret_is_refused_and_not_processed(
    make_bot: Callable[..., Bot],
    fake: FakeAPI,
    caplog: pytest.LogCaptureFixture,
    secret: str | None,
    said: str,
) -> None:
    bot = make_bot()
    inbox = Inbox(bot)
    app = bot.create_webhook_app(SECRET)

    async with bot.lifespan(app):
        reply = await post(app, delivery(fake), secret=secret)

    assert reply.status == 401
    assert reply.headers["www-authenticate"] == "PGPChat-Secret-Token"
    assert reply.body == {"ok": False}
    assert inbox.entered == []
    assert bot.storage.accepted_max() is None
    assert records(caplog, "pgpcbot.webhook", logging.WARNING) == [
        f"webhook delivery refused: {said} secret from 203.0.113.9:51234"
    ]


async def test_a_secret_header_that_is_not_ascii_is_refused_cleanly(
    make_bot: Callable[..., Bot], fake: FakeAPI
) -> None:
    bot = make_bot()
    inbox = Inbox(bot)
    app = bot.create_webhook_app(SECRET)

    async with bot.lifespan(app):
        over_asgi = await post(app, delivery(fake), secret=SECRET[:-1].encode() + b"\xe9")
        # a character latin-1 has no byte for
        direct = await app.handle({SECRET_HEADER: SECRET[:-1] + chr(0x20AC)}, delivery(fake))

    assert over_asgi.status == 401
    assert direct.status == 401
    assert inbox.entered == []


async def test_with_memory_storage_the_200_comes_after_the_handler(make_bot: Callable[..., Bot], fake: FakeAPI) -> None:
    bot = make_bot()
    inbox = Inbox(bot)
    app = bot.create_webhook_app(SECRET)

    async with bot.lifespan(app):
        reply = await post(app, delivery(fake, "hello"))
        assert (reply.status, reply.body) == (200, {"ok": True})
        assert reply.headers["content-type"] == "application/json"
        assert reply.headers["cache-control"] == "no-store"
        # already through when the server got its answer
        assert inbox.texts == ["hello"]
        assert bot.storage.pending() == []


async def test_with_sqlite_the_200_comes_once_stored_and_the_handler_runs_after(
    make_bot: Callable[..., Bot], fake: FakeAPI, tmp_path: Path
) -> None:
    bot = make_bot(storage=tmp_path / "bot.db")
    inbox = Inbox(bot, hold=True)
    app = bot.create_webhook_app(SECRET)

    async with bot.lifespan(app):
        reply = await post(app, delivery(fake, "hello"))
        assert (reply.status, reply.body) == (200, {"ok": True})
        assert inbox.texts == []
        assert [u["updateId"] for u in bot.storage.pending()] == [1]

        inbox.release()
        await until(lambda: inbox.texts == ["hello"] and bot.storage.count_pending() == 0)


async def test_only_post_is_accepted(make_bot: Callable[..., Bot], fake: FakeAPI) -> None:
    bot = make_bot()
    inbox = Inbox(bot)
    app = bot.create_webhook_app(SECRET)

    async with bot.lifespan(app):
        reply = await request(app, method="GET", headers=[(SECRET_HEADER, SECRET)])

    assert (reply.status, reply.body) == (405, {"ok": False})
    assert reply.headers["allow"] == "POST"
    assert inbox.entered == []


async def test_with_a_path_set_other_paths_are_404(make_bot: Callable[..., Bot], fake: FakeAPI) -> None:
    bot = make_bot()
    inbox = Inbox(bot)
    app = bot.create_webhook_app(SECRET, path="/hook/")

    async with bot.lifespan(app):
        elsewhere = await post(app, delivery(fake, "lost"), path="/other")
        exact = await post(app, delivery(fake, "exact"), path="/hook")
        slash = await post(app, delivery(fake, "slash"), path="/hook/")

    assert (elsewhere.status, elsewhere.body) == (404, {"ok": False})
    assert exact.status == slash.status == 200
    assert inbox.texts == ["exact", "slash"]
    assert repr(app) == "<WebhookApp path=/hook secret=set>"


@pytest.mark.parametrize("path", ["/bots/hook", "/hook"], ids=["path keeps the prefix", "prefix stripped"])
async def test_the_mount_prefix_in_root_path_is_not_part_of_the_route(
    make_bot: Callable[..., Bot], fake: FakeAPI, path: str
) -> None:
    bot = make_bot()
    inbox = Inbox(bot)
    app = bot.create_webhook_app(SECRET, path="/hook")

    async with bot.lifespan(app):
        reply = await post(app, delivery(fake, "mounted"), path=path, root_path="/bots")

    assert reply.status == 200
    assert inbox.texts == ["mounted"]


async def test_a_declared_length_over_max_body_is_413_before_the_body_is_read(
    make_bot: Callable[..., Bot], fake: FakeAPI
) -> None:
    bot = make_bot()
    app = bot.create_webhook_app(SECRET, max_body=64)
    reads = 0

    async def receive() -> dict[str, Any]:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"x" * 65, "more_body": False}

    reply = await request(app, headers=[(SECRET_HEADER, SECRET), ("content-length", "65")], receive=receive)

    assert (reply.status, reply.body) == (413, {"ok": False})
    assert reads == 0
    await bot.stop()


async def test_a_body_streamed_past_max_body_is_cut_off_with_413(make_bot: Callable[..., Bot], fake: FakeAPI) -> None:
    bot = make_bot()
    inbox = Inbox(bot)
    app = bot.create_webhook_app(SECRET, max_body=64)
    reads = 0

    async def receive() -> dict[str, Any]:
        nonlocal reads
        reads += 1
        # no content-length, and a body that would go on for a long while
        return {"type": "http.request", "body": b" " * 40, "more_body": reads < 1000}

    reply = await request(app, headers=[(SECRET_HEADER, SECRET)], receive=receive)

    assert (reply.status, reply.body) == (413, {"ok": False})
    # reading stopped as soon as the limit was passed, nothing buffered past it
    assert reads == 2
    assert inbox.entered == []
    assert bot.storage.accepted_max() is None
    await bot.stop()


async def test_max_body_itself_is_still_taken(make_bot: Callable[..., Bot], fake: FakeAPI) -> None:
    bot = make_bot()
    inbox = Inbox(bot)
    body = delivery(fake, "just fits")
    app = bot.create_webhook_app(SECRET, max_body=len(body))

    async with bot.lifespan(app):
        fits = await post(app, body)
        declared_over = await post(app, body + b" ")
        streamed_over = await request(app, headers=[(SECRET_HEADER, SECRET)], chunks=[body, b" "])

    assert fits.status == 200
    assert declared_over.status == streamed_over.status == 413
    assert inbox.texts == ["just fits"]


async def test_a_body_that_stops_arriving_is_408(make_bot: Callable[..., Bot], fake: FakeAPI) -> None:
    bot = make_bot()
    inbox = Inbox(bot)
    app = bot.create_webhook_app(SECRET)
    app._read_timeout = 0.05
    first_chunk = [{"type": "http.request", "body": b'{"updateId": 1, ', "more_body": True}]

    async def receive() -> dict[str, Any]:
        if first_chunk:
            return first_chunk.pop()
        await asyncio.Event().wait()  # the sender went quiet
        return {"type": "http.disconnect"}

    reply = await request(app, headers=[(SECRET_HEADER, SECRET)], receive=receive)

    assert (reply.status, reply.body) == (408, {"ok": False})
    assert inbox.entered == []
    await bot.stop()


@pytest.mark.parametrize(
    "body",
    [
        b"<html>not json</html>",
        b"\xff\xfe\x00",
        b"[1, 2, 3]",
        b'{"kind": "message"}',
        b'{"updateId": "12", "kind": "message"}',
        b'{"updateId": true, "kind": "message"}',
        b'{"updateId": -1, "kind": "message"}',
        b'{"updateId": 1.5, "kind": "message"}',
    ],
    ids=["html", "not utf-8", "array", "no updateId", "string id", "bool id", "negative id", "float id"],
)
async def test_authenticated_garbage_is_acknowledged_logged_and_dropped(
    make_bot: Callable[..., Bot], fake: FakeAPI, caplog: pytest.LogCaptureFixture, body: bytes
) -> None:
    bot = make_bot()
    inbox = Inbox(bot)
    app = bot.create_webhook_app(SECRET)

    async with bot.lifespan(app):
        reply = await post(app, body)

    # a 4xx would have the server retry it for days, holding every later update behind it
    assert (reply.status, reply.body) == (200, {"ok": True})
    assert inbox.entered == []
    assert bot.storage.accepted_max() is None
    [error] = records(caplog, "pgpcbot.webhook", logging.ERROR)
    assert error.startswith(f"webhook delivery with a valid secret dropped, {len(body)} bytes: ")


async def test_a_redelivered_or_older_update_is_answered_200_and_not_run_again(
    make_bot: Callable[..., Bot], fake: FakeAPI
) -> None:
    bot = make_bot()
    inbox = Inbox(bot)
    app = bot.create_webhook_app(SECRET)
    older = delivery(fake, "older")
    newer = delivery(fake, "newer")

    async with bot.lifespan(app):
        replies = [await post(app, body) for body in (newer, newer, older)]

    assert [r.status for r in replies] == [200, 200, 200]
    assert inbox.entered == ["newer"]


async def test_handlers_slower_than_ack_timeout_get_503_and_the_retry_does_not_run_them_again(
    make_bot: Callable[..., Bot], fake: FakeAPI
) -> None:
    bot = make_bot()
    inbox = Inbox(bot, hold=True)
    app = bot.create_webhook_app(SECRET, ack_timeout=0.05)
    body = delivery(fake, "slow")

    async with bot.lifespan(app):
        first = await post(app, body)
        assert (first.status, first.body) == (503, {"ok": False})
        # the server tries again while the first run is still going
        assert (await post(app, body)).status == 503

        inbox.release()
        await until(lambda: inbox.texts == ["slow"])
        assert (await post(app, body)).status == 200

    assert inbox.entered == ["slow"]


async def test_a_retry_that_arrives_while_the_first_run_is_going_shares_its_outcome(
    make_bot: Callable[..., Bot], fake: FakeAPI
) -> None:
    bot = make_bot()
    inbox = Inbox(bot, hold=True)
    app = bot.create_webhook_app(SECRET, ack_timeout=2.0)
    body = delivery(fake, "slow")

    async with bot.lifespan(app):
        first = asyncio.create_task(post(app, body))
        await until(lambda: inbox.entered == ["slow"])
        # the server gave up on the first request on its side and tried again
        retry = asyncio.create_task(post(app, body))
        for _ in range(20):
            await asyncio.sleep(0)
        assert not retry.done()

        inbox.release()
        replies = await asyncio.wait_for(asyncio.gather(first, retry), 2)

    assert [r.status for r in replies] == [200, 200]
    assert inbox.entered == ["slow"]


async def test_a_delivery_before_the_lifespan_ran_starts_the_bot_with_a_warning(
    make_bot: Callable[..., Bot], fake: FakeAPI, caplog: pytest.LogCaptureFixture
) -> None:
    bot = make_bot()
    inbox = Inbox(bot)
    app = bot.create_webhook_app(SECRET)

    try:
        reply = await post(app, delivery(fake, "early"))
    finally:
        await bot.stop()

    assert reply.status == 200
    assert inbox.texts == ["early"]
    assert len(fake.calls("GET", "/bot/v1/me")) == 1
    assert records(caplog, "pgpcbot.bot", logging.WARNING) == [
        "webhook delivery before the bot started; starting now (wire bot.lifespan into your app)"
    ]


async def test_deliveries_racing_in_before_the_start_boot_the_bot_once(
    make_bot: Callable[..., Bot], fake: FakeAPI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    answer = fake.handle_async_request

    async def slow_me(request: Any) -> Any:
        if request.url.path.endswith("/me"):
            await asyncio.sleep(0.02)  # long enough for the second delivery to find the bot still starting
        return await answer(request)

    monkeypatch.setattr(fake, "handle_async_request", slow_me)
    bot = make_bot()
    inbox = Inbox(bot)
    app = bot.create_webhook_app(SECRET)

    try:
        first, second = await asyncio.gather(post(app, delivery(fake, "one")), post(app, delivery(fake, "two")))
    finally:
        await bot.stop()

    assert first.status == second.status == 200
    assert sorted(t or "" for t in inbox.texts) == ["one", "two"]
    # both found it not started, and still only one of them booted it
    assert len(records(caplog, "pgpcbot.bot", logging.WARNING)) == 2
    assert len(fake.calls("GET", "/bot/v1/me")) == 1


async def test_a_delivery_that_cannot_start_the_bot_is_answered_500(
    make_bot: Callable[..., Bot], fake: FakeAPI, caplog: pytest.LogCaptureFixture
) -> None:
    fake.fail("GET", r"/me$", status=401, code="unauthorized")
    bot = make_bot()
    inbox = Inbox(bot)
    app = bot.create_webhook_app(SECRET)

    try:
        reply = await post(app, delivery(fake))
    finally:
        await bot.stop()

    assert (reply.status, reply.body) == (500, {"ok": False})
    assert inbox.entered == []
    assert "webhook delivery failed" in records(caplog, "pgpcbot.webhook", logging.ERROR)


async def test_a_delivery_while_the_bot_is_stopping_is_turned_away_with_503(
    make_bot: Callable[..., Bot], fake: FakeAPI, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="pgpcbot.engine")
    bot = make_bot()
    gate = asyncio.Event()
    handled: list[str | None] = []

    @bot.on_message()
    async def on_message(message: Message) -> None:
        handled.append(message.text)
        if message.text == "slow":
            await gate.wait()

    app = bot.create_webhook_app(SECRET, ack_timeout=0.05)
    lifespan = Lifespan(app)
    assert (await lifespan.say("lifespan.startup"))["type"] == "lifespan.startup.complete"
    # still running, so the shutdown has to wait for it
    assert (await post(app, delivery(fake, "slow"))).status == 503

    shutdown = asyncio.create_task(lifespan.say("lifespan.shutdown"))
    try:
        await until(lambda: "still running" in caplog.text)
        during = await post(app, delivery(fake, "during the shutdown"))
    finally:
        gate.set()
        await asyncio.wait_for(shutdown, 5)
        await lifespan.ended()
        await bot.stop()

    assert (during.status, during.body) == (503, {"ok": False})
    assert handled == ["slow"]
    assert len(fake.calls("GET", "/bot/v1/me")) == 1


async def test_a_delivery_after_shutdown_is_turned_away_with_503(make_bot: Callable[..., Bot], fake: FakeAPI) -> None:
    bot = make_bot()
    inbox = Inbox(bot)
    app = bot.create_webhook_app(SECRET)
    lifespan = Lifespan(app)
    assert (await lifespan.say("lifespan.startup"))["type"] == "lifespan.startup.complete"
    assert (await lifespan.say("lifespan.shutdown"))["type"] == "lifespan.shutdown.complete"
    await lifespan.ended()

    try:
        reply = await asyncio.wait_for(post(app, delivery(fake, "too late")), 5)
    finally:
        await bot.stop()

    assert (reply.status, reply.body) == (503, {"ok": False})
    assert inbox.entered == []
    assert len(fake.calls("GET", "/bot/v1/me")) == 1


async def test_the_app_runs_the_asgi_lifespan_on_its_own(
    make_bot: Callable[..., Bot], fake: FakeAPI, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="pgpcbot")
    bot = make_bot()
    inbox = Inbox(bot)
    app = bot.create_webhook_app(SECRET)
    lifespan = Lifespan(app)

    assert await lifespan.say("lifespan.startup") == {"type": "lifespan.startup.complete"}
    assert len(fake.calls("GET", "/bot/v1/me")) == 1
    assert not bot.storage.try_lock()  # the running bot holds it
    assert (await post(app, delivery(fake, "hi"))).status == 200

    assert await lifespan.say("lifespan.shutdown") == {"type": "lifespan.shutdown.complete"}
    await lifespan.ended()
    assert inbox.texts == ["hi"]
    assert "stopped" in records(caplog, "pgpcbot.bot", logging.INFO)
    # the storage was handed back on the way out
    assert bot.storage.try_lock()


async def test_the_lifespan_reports_a_start_that_failed(
    make_bot: Callable[..., Bot], fake: FakeAPI, caplog: pytest.LogCaptureFixture
) -> None:
    fake.fail("GET", r"/me$", status=401, code="unauthorized")
    bot = make_bot()
    app = bot.create_webhook_app(SECRET)
    lifespan = Lifespan(app)

    try:
        reply = await lifespan.say("lifespan.startup")
        await lifespan.ended()
    finally:
        await bot.stop()

    assert reply["type"] == "lifespan.startup.failed"
    assert "unauthorized" in reply["message"]
    assert "the bot could not start" in records(caplog, "pgpcbot.webhook", logging.ERROR)


async def test_a_websocket_is_closed_and_other_scopes_are_left_alone(make_bot: Callable[..., Bot]) -> None:
    bot = make_bot()
    app = bot.create_webhook_app(SECRET)
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app({"type": "websocket", "path": "/", "headers": []}, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1003}]

    sent.clear()
    await app({"type": "something.else"}, receive, send)
    assert sent == []
    await bot.stop()


async def test_handle_works_without_asgi_and_matches_the_header_without_case(
    make_bot: Callable[..., Bot], fake: FakeAPI
) -> None:
    bot = make_bot()
    inbox = Inbox(bot)
    app = bot.create_webhook_app(SECRET)

    async with bot.lifespan(app):
        result = await app.handle({"X-PGPChat-Bot-API-Secret-Token": SECRET}, delivery(fake, "direct"))

    assert result == WebhookResult(200, {"ok": True})
    assert inbox.texts == ["direct"]


async def test_handle_checks_the_size_itself_for_frameworks_that_read_the_body(
    make_bot: Callable[..., Bot], fake: FakeAPI
) -> None:
    bot = make_bot()
    inbox = Inbox(bot)
    app = bot.create_webhook_app(SECRET, max_body=64)

    async with bot.lifespan(app):
        result = await app.handle({SECRET_HEADER: SECRET}, delivery(fake, "x" * 100))

    assert result == WebhookResult(413, {"ok": False})
    assert inbox.entered == []
    assert bot.storage.accepted_max() is None


async def test_a_sender_that_hangs_up_mid_body_gets_nothing_processed(
    make_bot: Callable[..., Bot], fake: FakeAPI
) -> None:
    bot = make_bot()
    inbox = Inbox(bot)
    app = bot.create_webhook_app(SECRET)
    body = delivery(fake, "cut")
    messages: list[dict[str, Any]] = [
        {"type": "http.request", "body": body[:20], "more_body": True},
        {"type": "http.disconnect"},
    ]

    async def receive() -> dict[str, Any]:
        return messages.pop(0)

    async with bot.lifespan(app):
        await request(app, headers=[(SECRET_HEADER, SECRET)], receive=receive)

    assert inbox.entered == []
    assert bot.storage.accepted_max() is None


class RefusingStorage(MemoryStorage):
    """A storage whose disk is full."""

    def accept(self, updates: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        raise StorageError("database or disk is full")


async def test_a_delivery_the_storage_cannot_take_is_answered_503(
    make_bot: Callable[..., Bot], fake: FakeAPI, caplog: pytest.LogCaptureFixture
) -> None:
    bot = make_bot(storage=RefusingStorage())
    inbox = Inbox(bot)
    app = bot.create_webhook_app(SECRET)

    async with bot.lifespan(app):
        reply = await post(app, delivery(fake))

    # the server keeps it and tries again later
    assert (reply.status, reply.body) == (503, {"ok": False})
    assert inbox.entered == []
    assert records(caplog, "pgpcbot.bot", logging.ERROR) == ["webhook update 1 could not be stored"]


async def test_a_handler_that_raises_is_acknowledged_and_its_update_set_aside(
    make_bot: Callable[..., Bot], fake: FakeAPI
) -> None:
    bot = make_bot()

    @bot.on_message()
    async def broken(message: Message) -> None:
        raise LookupError("no such order")

    app = bot.create_webhook_app(SECRET)

    async with bot.lifespan(app):
        reply = await post(app, delivery(fake, "order 7"))
        failed = bot.storage.failed()

    # a retry would fail the same way: the update waits in the failed list instead
    assert reply.status == 200
    assert [f.update_id for f in failed] == [1]
    assert failed[0].error.startswith("LookupError: no such order")


async def test_starting_a_paused_bot_says_so(
    make_bot: Callable[..., Bot], fake: FakeAPI, caplog: pytest.LogCaptureFixture
) -> None:
    fake.me["status"] = "paused"
    bot = make_bot()

    async with bot.lifespan(bot.create_webhook_app(SECRET)):
        pass

    assert "the owner paused this bot: nothing goes in or out until it is resumed" in records(
        caplog, "pgpcbot.bot", logging.WARNING
    )


def test_a_webhook_without_a_secret_has_to_be_asked_for(make_bot: Callable[..., Bot]) -> None:
    bot = make_bot()

    with pytest.raises(ConfigurationError, match="allow_no_secret"):
        bot.create_webhook_app(None)

    app = bot.create_webhook_app(None, allow_no_secret=True)
    assert app.secret is None
    assert repr(app) == "<WebhookApp path=* secret=none>"


async def test_without_a_secret_any_delivery_is_taken(make_bot: Callable[..., Bot], fake: FakeAPI) -> None:
    bot = make_bot()
    inbox = Inbox(bot)
    app = bot.create_webhook_app(None, allow_no_secret=True)

    async with bot.lifespan(app):
        reply = await post(app, delivery(fake, "anyone"), secret=None)

    assert reply.status == 200
    assert inbox.texts == ["anyone"]


@pytest.mark.parametrize("secret", ["", "has space", "slash/inside", "accent\xe9", "x" * 257])
def test_a_secret_the_server_would_refuse_is_refused_up_front(make_bot: Callable[..., Bot], secret: str) -> None:
    with pytest.raises(ValidationError) as caught:
        make_bot().create_webhook_app(secret)

    assert caught.value.field == "secret"


@pytest.mark.parametrize("ack_timeout", [10, 12.5, 0, -1])
def test_ack_timeout_has_to_stay_under_the_servers_ten_seconds(
    make_bot: Callable[..., Bot], ack_timeout: float
) -> None:
    with pytest.raises(ConfigurationError, match="ack_timeout"):
        make_bot().create_webhook_app(SECRET, ack_timeout=ack_timeout)


def test_a_webhook_url_with_a_fragment_is_refused(make_bot: Callable[..., Bot]) -> None:
    with pytest.raises(ValidationError) as caught:
        make_bot().create_webhook_app(SECRET, url=URL + "#top")

    assert caught.value.field == "url"


async def test_url_registers_the_webhook_with_its_secret_at_every_start(
    make_bot: Callable[..., Bot], fake: FakeAPI
) -> None:
    def registrations() -> list[dict[str, Any]]:
        return [call["json"] for call in fake.calls("POST", "/bot/v1/webhook")]

    first = make_bot()
    async with first.lifespan(first.create_webhook_app(SECRET, url=URL)):
        pass
    assert registrations() == [{"url": URL, "secret": SECRET}]
    assert fake.webhook["url"] == URL

    # the server never shows the secret it holds, so the same settings are sent again:
    # a secret changed on this side would otherwise never reach it
    same = make_bot()
    async with same.lifespan(same.create_webhook_app(SECRET, url=URL)):
        pass
    assert registrations() == [{"url": URL, "secret": SECRET}] * 2

    moved = make_bot()
    async with moved.lifespan(moved.create_webhook_app(SECRET, url="https://bot.example/elsewhere")):
        pass
    assert registrations()[2:] == [{"url": "https://bot.example/elsewhere", "secret": SECRET}]

    open_app = make_bot()
    async with open_app.lifespan(
        open_app.create_webhook_app(None, url="https://bot.example/elsewhere", allow_no_secret=True)
    ):
        pass
    assert len(registrations()) == 4
    assert fake.webhook["secret"] is None


async def test_without_a_url_the_webhook_is_left_as_the_server_has_it(
    make_bot: Callable[..., Bot], fake: FakeAPI
) -> None:
    fake.webhook.update(url=URL, secret=SECRET)
    bot = make_bot()

    async with bot.lifespan(bot.create_webhook_app(SECRET)):
        pass

    assert fake.calls("POST", "/bot/v1/webhook") == []
    assert fake.calls("DELETE", "/bot/v1/webhook") == []


async def test_a_rotated_secret_on_the_same_url_is_registered_again(
    make_bot: Callable[..., Bot], fake: FakeAPI
) -> None:
    fake.webhook.update(url=URL, secret="the_old_secret")
    bot = make_bot()

    async with bot.lifespan(bot.create_webhook_app(SECRET, url=URL)):
        pass

    assert fake.webhook["secret"] == SECRET


def test_generate_secret_makes_43_url_safe_characters_the_app_accepts(make_bot: Callable[..., Bot]) -> None:
    secrets = {generate_secret() for _ in range(50)}

    assert len(secrets) == 50
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{43}", s) for s in secrets)
    assert make_bot().create_webhook_app(secrets.pop()).secret is not None


def test_mounted_in_starlette_with_the_bot_lifespan(make_bot: Callable[..., Bot], fake: FakeAPI) -> None:
    bot = make_bot()
    inbox = Inbox(bot)
    webhook = bot.create_webhook_app(SECRET, path="/hook")
    # WebhookApp types the scope as dict where Starlette's ASGIApp wants MutableMapping
    app = Starlette(routes=[Mount("/bots", app=webhook)], lifespan=bot.lifespan)  # type: ignore[arg-type]

    with TestClient(app) as client:
        assert len(fake.calls("GET", "/bot/v1/me")) == 1
        refused = client.post("/bots/hook", content=delivery(fake, "no secret"))
        taken = client.post("/bots/hook", content=delivery(fake, "hello"), headers={SECRET_HEADER: SECRET})
        wrong_method = client.get("/bots/hook")
        wrong_path = client.post("/bots/other", content=delivery(fake, "lost"), headers={SECRET_HEADER: SECRET})

    assert refused.status_code == 401
    assert refused.headers["www-authenticate"] == "PGPChat-Secret-Token"
    assert (taken.status_code, taken.json()) == (200, {"ok": True})
    assert wrong_method.status_code == 405
    assert wrong_method.headers["allow"] == "POST"
    assert (wrong_path.status_code, wrong_path.json()) == (404, {"ok": False})
    assert inbox.texts == ["hello"]


def test_mounted_in_fastapi_with_the_bot_lifespan(make_bot: Callable[..., Bot], fake: FakeAPI) -> None:
    bot = make_bot()
    inbox = Inbox(bot)
    app = FastAPI(lifespan=bot.lifespan)
    # WebhookApp types the scope as dict where Starlette's ASGIApp wants MutableMapping
    app.mount("/pgp", bot.create_webhook_app(SECRET, path="/hook"))  # type: ignore[arg-type]

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        refused = client.post("/pgp/hook", content=delivery(fake, "wrong"), headers={SECRET_HEADER: "wrong"})
        taken = client.post("/pgp/hook", content=delivery(fake, "hello"), headers={SECRET_HEADER: SECRET})

    assert refused.status_code == 401
    assert (taken.status_code, taken.json()) == (200, {"ok": True})
    assert inbox.texts == ["hello"]
    assert len(fake.calls("GET", "/bot/v1/me")) == 1


async def test_a_wrong_secret_is_refused_before_the_body_is_read(make_bot: Callable[..., Bot]) -> None:
    app = make_bot().create_webhook_app(SECRET)
    reads: list[bool] = []

    async def receive() -> dict[str, Any]:
        reads.append(True)
        return {"type": "http.request", "body": b"{}", "more_body": False}

    reply = await request(app, headers=[(SECRET_HEADER, "wrong")], receive=receive)
    assert reply.status == 401
    # no worker held and no buffer filled for somebody without the secret
    assert reads == []
