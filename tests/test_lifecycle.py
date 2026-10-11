"""Starting, stopping, hooks, and what a running bot believes about its chats."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pgpcbot import (
    Bot,
    BotKey,
    ConfigurationError,
    DecryptionError,
    Limits,
    Media,
    PublicKey,
    RateLimiter,
    SQLiteStorage,
    SyncBot,
)
from pgpcbot.bot import _TTLCache
from pgpcbot.engine import Engine, poll
from pgpcbot.storage import MemoryStorage
from pgpcbot.webhook import SECRET_HEADER

from .conftest import FIXTURES, fixture_text
from .fakeapi import BOT_ID, MEDIA_ID, FakeAPI

SECRET = "pgp_webhook-Secret_0123456789"


def posts(fake: FakeAPI) -> list[dict[str, Any]]:
    return [call["json"] for call in fake.calls("POST", "/bot/v1/messages")]


async def eventually(probe: Callable[[], Any], within: float = 5.0) -> None:
    deadline = time.monotonic() + within
    while True:
        result = probe()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        if time.monotonic() > deadline:
            raise AssertionError("condition still false after waiting")
        await asyncio.sleep(0.01)


def anna(phone_key: PublicKey) -> dict[str, Any]:
    return {"userId": 55, "fingerprint": phone_key.fingerprint, "publicKey": fixture_text("phone_ed.pub.asc")}


# --- hooks ---


async def test_startup_and_shutdown_hooks_run_with_the_bot_connected(fake: FakeAPI, make_bot: Callable[..., Bot]):
    fake.add_chat(7)
    bot = make_bot()
    seen: list[tuple[str, str | None]] = []

    @bot.on_startup
    async def started(b: Bot) -> None:
        seen.append(("start", b.username))
        await b.send_message(7, "up")

    @bot.on_shutdown
    async def stopping(b: Bot) -> None:
        seen.append(("stop", b.username))
        await b.send_message(7, "going down")

    task = asyncio.create_task(bot.start_polling(poll_timeout=1))
    await eventually(lambda: seen)
    await bot.stop()
    await asyncio.wait_for(task, 5)

    assert seen == [("start", "testbot"), ("stop", "testbot")]
    # the shutdown hook still had a connection to send with
    assert [p["text"] for p in posts(fake)] == ["up", "going down"]


async def test_a_failing_startup_hook_keeps_the_bot_from_starting(fake: FakeAPI, make_bot: Callable[..., Bot]):
    bot = make_bot()
    stopped: list[bool] = []

    @bot.on_startup
    async def broken(b: Bot) -> None:
        raise RuntimeError("the database is not there")

    @bot.on_shutdown
    async def never(b: Bot) -> None:
        stopped.append(True)

    with pytest.raises(RuntimeError, match="database"):
        await bot.start_polling(poll_timeout=1)
    assert fake.calls("GET", "/bot/v1/updates") == []
    # it never started, so there is nothing to shut down
    assert stopped == []


async def test_a_failing_shutdown_hook_is_logged_and_the_bot_still_stops(
    make_bot: Callable[..., Bot], caplog: pytest.LogCaptureFixture
):
    bot = make_bot()

    @bot.on_shutdown
    async def broken(b: Bot) -> None:
        raise RuntimeError("could not say goodbye")

    @bot.on_shutdown
    async def second(b: Bot) -> None:
        second.ran = True  # type: ignore[attr-defined]

    await bot.start()
    with caplog.at_level(logging.ERROR, logger="pgpcbot.bot"):
        await bot.stop()
    assert "shutdown hook" in caplog.text
    assert second.ran  # type: ignore[attr-defined]


async def test_a_bot_refuses_a_plain_hook(make_bot: Callable[..., Bot]) -> None:
    bot = make_bot()

    @bot.on_startup
    def plain(b: Any) -> None: ...

    with pytest.raises(ConfigurationError, match="hook"):
        await bot.start_polling(poll_timeout=1)


def test_a_sync_bot_refuses_an_async_hook(make_sync_bot: Callable[..., SyncBot]) -> None:
    bot = make_sync_bot()

    @bot.on_shutdown
    async def coroutine(b: Any) -> None: ...

    with pytest.raises(ConfigurationError, match="hook"):
        bot.run(poll_timeout=1, configure_logging=False)


def test_a_hook_has_to_be_callable(make_bot: Callable[..., Bot]) -> None:
    bot = make_bot()
    with pytest.raises(TypeError):
        bot.on_startup("not a function")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        bot.on_shutdown(None)  # type: ignore[arg-type]


def test_sync_bot_hooks_run_on_handler_threads(fake: FakeAPI, make_sync_bot) -> None:
    bot = make_sync_bot()
    threads: list[str] = []

    @bot.on_startup
    def started(b: SyncBot) -> None:
        threads.append(threading.current_thread().name)
        # a startup hook can stop the bot too: the loop is up by now
        b.stop()

    bot.run(poll_timeout=1, configure_logging=False)
    assert len(threads) == 1
    assert threads[0].startswith("pgpcbot-handler")


# --- stopping ---


async def test_request_stop_from_a_handler_ends_polling(fake: FakeAPI, make_bot: Callable[..., Bot]):
    fake.add_chat(7)
    bot = make_bot()

    @bot.on_message()
    async def shutdown(message: Any) -> None:
        await message.reply("bye")
        bot.request_stop()

    task = asyncio.create_task(bot.start_polling(poll_timeout=1))
    fake.push_message(7, "/shutdown")
    await asyncio.wait_for(task, 5)
    assert [p["text"] for p in posts(fake)] == ["bye"]


async def test_stop_awaited_in_a_handler_does_not_wait_for_that_handler(fake: FakeAPI, make_bot: Callable[..., Bot]):
    fake.add_chat(7)
    bot = make_bot(shutdown_timeout=30.0)

    @bot.on_message()
    async def shutdown(message: Any) -> None:
        await bot.stop()

    task = asyncio.create_task(bot.start_polling(poll_timeout=1))
    fake.push_message(7, "/shutdown")
    started = time.monotonic()
    await asyncio.wait_for(task, 10)
    assert time.monotonic() - started < 10


def test_request_stop_before_the_bot_runs_does_nothing(make_sync_bot) -> None:
    bot = make_sync_bot()
    bot.request_stop()
    bot.stop()


async def test_a_stop_does_not_wait_for_a_long_poll_to_come_back() -> None:
    engine = Engine(MemoryStorage(), lambda update: asyncio.sleep(0))
    stop = asyncio.Event()

    async def fetch(offset: int | None, limit: int, wait: int) -> list[dict[str, Any]]:
        await asyncio.sleep(30)  # a poll the server holds for its whole timeout
        return []

    asyncio.get_running_loop().call_later(0.05, stop.set)
    started = time.monotonic()
    await asyncio.wait_for(poll(fetch, engine, stop, poll_timeout=50), 5)
    assert time.monotonic() - started < 2


# --- webhook workers ---


async def test_a_worker_without_the_lock_only_stores_what_it_is_given(
    fake: FakeAPI, make_bot: Callable[..., Bot], tmp_path: Path
):
    holder = SQLiteStorage(tmp_path / "bot.db")
    holder.open(BOT_ID)
    assert holder.try_lock()
    fake.add_chat(7)
    bot = make_bot(storage=SQLiteStorage(tmp_path / "bot.db"))
    ran: list[int] = []

    @bot.on_message()
    async def handler(message: Any) -> None:
        ran.append(message.id)

    app = bot.create_webhook_app(SECRET)
    await bot.start()
    try:
        uid = fake.push_message(7, "hi")
        result = await app.handle({SECRET_HEADER: SECRET}, json.dumps(fake.queue[-1]).encode())
        assert result.status == 200
        await asyncio.sleep(0.1)
        # stored, so safe; run by the worker that holds the lock, never by both
        assert ran == []
        assert holder.pending_ids() == [uid]
    finally:
        await bot.stop()
        holder.close()


async def test_a_full_engine_turns_deliveries_away_or_stores_them(
    fake: FakeAPI, make_bot: Callable[..., Bot], tmp_path: Path
):
    fake.add_chat(7)
    in_memory = make_bot()
    app = in_memory.create_webhook_app(SECRET)
    await in_memory.start()
    assert in_memory._engine is not None
    in_memory._engine.max_pending = 0
    fake.push_message(7, "one")
    try:
        result = await app.handle({SECRET_HEADER: SECRET}, json.dumps(fake.queue[-1]).encode())
        # nothing on disk and no room: the server keeps it and comes back
        assert result.status == 503
    finally:
        await in_memory.stop()

    on_disk = make_bot(storage=SQLiteStorage(tmp_path / "bot.db"))
    app = on_disk.create_webhook_app(SECRET)
    await on_disk.start()
    assert on_disk._engine is not None
    on_disk._engine.max_pending = 0
    uid = fake.push_message(7, "two")
    try:
        result = await app.handle({SECRET_HEADER: SECRET}, json.dumps(fake.queue[-1]).encode())
        assert result.status == 200
        assert uid in on_disk.storage.pending_ids()
    finally:
        await on_disk.stop()


# --- what the bot believes about its chats ---


async def test_an_encrypted_message_makes_an_open_bot_look_again_before_replying(
    fake: FakeAPI, make_bot: Callable[..., Bot], bot_key: BotKey, phone_key: PublicKey
):
    phone = BotKey.from_file(FIXTURES / "phone_ed.sec.asc")
    fake.add_chat(7, keys=[anna(phone_key)])
    bot = make_bot(pgp_private_key=bot_key)
    await bot.get_me()
    # the key was published from somewhere else (the console, the command line)
    fake.me["mode"] = "protected"
    fake.me["fingerprint"] = bot_key.fingerprint

    @bot.on_message()
    async def reply(message: Any) -> None:
        await message.reply("got it")

    fake.push_message(7, armored=fixture_text("msg_text_signed.asc"))
    try:
        await bot.process_update(fake.queue[-1])
    finally:
        await bot.stop()

    # sealed at the first try: the words never went to the server in clear
    [post] = posts(fake)
    assert "text" not in post
    assert phone.decrypt(post["armored"]).text == "got it"


async def test_a_group_body_that_contradicts_the_cache_makes_the_bot_ask_again(
    fake: FakeAPI, make_bot: Callable[..., Bot], bot_key: BotKey, phone_key: PublicKey
):
    fake.add_chat(900, "group", title="Team", history_visible=False, keys=[anna(phone_key)])
    bot = make_bot(pgp_private_key=bot_key)
    try:
        await bot.send_message(900, "sealed")
        fake.push_message(900, "plain words")
        await bot.process_update(fake.queue[-1])
        await bot.send_message(900, "again")
    finally:
        await bot.stop()
    assert len(fake.calls("GET", "/bot/v1/chats/900")) == 2


def test_an_open_mode_is_trusted_for_a_short_while_only() -> None:
    cache = _TTLCache(60.0)
    cache.put(900, "open")
    assert cache.fresh(900, 10.0)
    time.sleep(0.02)
    assert not cache.fresh(900, 0.01)
    assert not cache.fresh(901, 10.0)


async def test_an_armored_body_in_a_readable_chat_reaches_the_handler_as_words(
    fake: FakeAPI, make_bot: Callable[..., Bot]
):
    fake.add_chat(7)
    bot = make_bot()
    got: list[Any] = []

    @bot.on_message()
    async def handler(message: Any) -> None:
        got.append(message)

    quoted = "my old key:\n-----BEGIN PGP MESSAGE-----\n\nnot really\n-----END PGP MESSAGE-----"
    fake.push_message(7, armored=quoted)
    try:
        await bot.process_update(fake.queue[-1])
    finally:
        await bot.stop()
    [message] = got
    assert message.text == quoted
    assert not message.encrypted


async def test_a_rate_limiter_passed_in_keeps_its_own_numbers(fake: FakeAPI, make_bot: Callable[..., Bot]):
    mine = RateLimiter(Limits(sends_per_second=3))
    own = make_bot(rate_limit=True)
    given = make_bot(rate_limit=mine)
    try:
        await own.start()
        await given.start()
    finally:
        await own.stop()
        await given.stop()
    assert mine._overall.limit == 3
    assert own._limiter is not None
    assert own._limiter._overall.limit == fake.me["limits"]["sendsPerSecond"]


async def test_a_compressed_attachment_is_refused_before_it_is_inflated(
    fake: FakeAPI, make_bot: Callable[..., Bot], bot_key: BotKey
):
    # a phone's text message is compressed: as a file it is exactly what must not be inflated blindly
    fake.media[MEDIA_ID] = fixture_text("msg_text_signed.asc").encode()
    bot = make_bot(pgp_private_key=bot_key)
    try:
        with pytest.raises(DecryptionError, match="compressed"):
            await bot.download_media(Media(id=MEDIA_ID, kind="file", encrypted=True))
    finally:
        await bot.stop()


def test_a_message_that_opens_too_big_is_refused(bot_key: BotKey) -> None:
    with pytest.raises(DecryptionError, match="more than the 10 allowed"):
        bot_key.decrypt(fixture_text("msg_text_signed.asc"), max_size=10)
