from __future__ import annotations

import asyncio
import inspect
import logging
import types
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

import pgpcbot.engine as engine_module
from pgpcbot.engine import Engine, poll, skip_pending
from pgpcbot.errors import (
    APIError,
    AuthenticationError,
    BotPausedError,
    ConflictError,
    ForbiddenError,
    NetworkError,
    NotFoundError,
    PollingError,
    RateLimitError,
    ServerError,
    WebhookActiveError,
)
from pgpcbot.storage import MemoryStorage, SQLiteStorage

from .fakeapi import BOT_ID

# ends a script the way bot.stop() ends a real poll: the stop event is set and
# the connection under the long poll drops
STOP = object()


def message(uid: int, chat_id: int = 7) -> dict[str, Any]:
    return {
        "updateId": uid,
        "kind": "message",
        "message": {"id": 1000 + uid, "chat": {"id": chat_id, "kind": "private"}, "text": f"update {uid}"},
    }


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


class Server:
    """GET /updates as poll() sees it: one scripted answer per poll, in order.

    An answer is a batch, an exception to raise, a callable giving either at
    the moment of the poll, or STOP. Polling past the end of the script fails.
    """

    def __init__(self, stop: asyncio.Event, *answers: Any) -> None:
        self.stop = stop
        self.answers = list(answers)
        self.calls: list[tuple[int | None, int, int]] = []

    @property
    def offsets(self) -> list[int | None]:
        return [offset for offset, _, _ in self.calls]

    async def __call__(self, offset: int | None, limit: int, wait: int) -> list[dict[str, Any]]:
        self.calls.append((offset, limit, wait))
        await asyncio.sleep(0)
        if not self.answers:
            raise AssertionError(f"polled once more than scripted (offset={offset})")
        answer = self.answers.pop(0)
        if answer is STOP:
            self.stop.set()
            raise NetworkError("connection closed")
        if callable(answer):
            answer = answer()
            if inspect.isawaitable(answer):
                answer = await answer
        if isinstance(answer, BaseException):
            raise answer
        return list(answer)


class Handlers:
    """The process function: notes what ran, and can hold an update until it is let go."""

    def __init__(self) -> None:
        self.started: list[int] = []
        self.finished: list[int] = []
        self._gates: dict[int, asyncio.Event] = {}
        self._arrived: dict[int, asyncio.Event] = {}

    def hold(self, *uids: int) -> None:
        for uid in uids:
            self._gates[uid] = asyncio.Event()

    def release(self, *uids: int) -> None:
        for uid in uids:
            self._gates[uid].set()

    async def wait_started(self, *uids: int) -> None:
        for uid in uids:
            await asyncio.wait_for(self._arrived.setdefault(uid, asyncio.Event()).wait(), 2)

    async def __call__(self, update: Mapping[str, Any]) -> None:
        uid = update["updateId"]
        self.started.append(uid)
        self._arrived.setdefault(uid, asyncio.Event()).set()
        if uid in self._gates:
            await self._gates[uid].wait()
        self.finished.append(uid)


class _SkippedWaits:
    """asyncio as the engine module sees it, except that timed waits are noted and skipped."""

    def __init__(self, noted: list[float]) -> None:
        self._noted = noted

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def wait_for(self, awaitable: Any, seconds: float) -> Any:
        if not inspect.iscoroutine(awaitable):
            return await asyncio.wait_for(awaitable, seconds)
        self._noted.append(seconds)
        awaitable.close()  # the stop.wait() it would have waited on
        await asyncio.sleep(0)
        raise TimeoutError


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every pause poll() takes between polls, in seconds, without taking it."""
    noted: list[float] = []
    monkeypatch.setattr(engine_module, "asyncio", _SkippedWaits(noted))
    return noted


@pytest.fixture
def jitter(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, float]]:
    """The ranges the backoff draws its wait from. The draw is always a quarter of the top."""
    ranges: list[tuple[float, float]] = []

    def uniform(low: float, high: float) -> float:
        ranges.append((low, high))
        return high / 4

    monkeypatch.setattr(engine_module, "random", types.SimpleNamespace(uniform=uniform))
    return ranges


@pytest.fixture
def sqlite(tmp_path: Path) -> Iterator[SQLiteStorage]:
    storage = SQLiteStorage(tmp_path / "bot.db")
    storage.open(BOT_ID)
    yield storage
    storage.close()


def polling_failures(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "pgpcbot.engine" and r.getMessage().startswith("polling failed")]


async def test_memory_storage_starts_without_an_offset_even_after_updates_were_seen() -> None:
    storage = MemoryStorage()
    storage.accept([message(5)])
    storage.finish(5)
    stop = asyncio.Event()
    server = Server(stop, STOP)

    await asyncio.wait_for(poll(server, Engine(storage, Handlers()), stop, poll_timeout=5, limit=25), 2)

    assert server.calls == [(None, 25, 5)]


@pytest.mark.parametrize(("accepted", "first_offset"), [([], None), ([3, 7], 8)], ids=["fresh", "seen before"])
async def test_sqlite_storage_starts_right_after_the_last_update_it_accepted(
    sqlite: SQLiteStorage, accepted: list[int], first_offset: int | None
) -> None:
    sqlite.accept([message(uid) for uid in accepted])
    stop = asyncio.Event()
    server = Server(stop, STOP)

    await asyncio.wait_for(poll(server, Engine(sqlite, Handlers()), stop, poll_timeout=5), 2)

    assert server.offsets == [first_offset]


async def test_sqlite_confirms_a_batch_once_it_is_stored_without_waiting_for_handlers(
    sqlite: SQLiteStorage,
) -> None:
    handlers = Handlers()
    handlers.hold(1, 2)
    engine = Engine(sqlite, handlers)
    stop = asyncio.Event()
    finished_at_second_poll: list[list[int]] = []

    def second_poll() -> list[dict[str, Any]]:
        finished_at_second_poll.append(list(handlers.finished))
        return []

    server = Server(stop, [message(1, 7), message(2, 8)], second_poll, STOP)
    await asyncio.wait_for(poll(server, engine, stop, poll_timeout=5), 2)

    assert server.offsets == [None, 3, 3]
    assert finished_at_second_poll == [[]]

    handlers.release(1, 2)
    await asyncio.wait_for(engine.drained(), 2)
    assert sorted(handlers.finished) == [1, 2]
    assert sqlite.pending() == []


async def test_memory_storage_confirms_a_batch_only_once_its_handlers_are_through() -> None:
    handlers = Handlers()
    handlers.hold(1, 2)
    engine = Engine(MemoryStorage(), handlers)
    stop = asyncio.Event()
    finished_at_second_poll: list[list[int]] = []

    def second_poll() -> list[dict[str, Any]]:
        finished_at_second_poll.append(sorted(handlers.finished))
        return []

    server = Server(stop, [message(1, 7), message(2, 8)], second_poll, STOP)
    polling = asyncio.create_task(poll(server, engine, stop, poll_timeout=5))
    await handlers.wait_started(1, 2)
    await settle()
    assert len(server.calls) == 1

    handlers.release(1, 2)
    await asyncio.wait_for(polling, 2)
    assert server.offsets == [None, 3, 3]
    assert finished_at_second_poll == [[1, 2]]


async def test_updates_delivered_again_are_not_run_again() -> None:
    handlers = Handlers()
    engine = Engine(MemoryStorage(), handlers)
    stop = asyncio.Event()
    server = Server(stop, [message(1), message(2)], [message(2), message(3)], [message(3)], STOP)

    await asyncio.wait_for(poll(server, engine, stop, poll_timeout=5), 2)

    assert handlers.finished == [1, 2, 3]
    assert server.offsets == [None, 3, 4, 4]


async def test_a_full_engine_holds_the_next_poll_back(sqlite: SQLiteStorage) -> None:
    handlers = Handlers()
    handlers.hold(1, 2)
    # durable, so the poll doesn't wait for the batch to drain: only room() holds it
    engine = Engine(sqlite, handlers, max_pending=2)
    stop = asyncio.Event()
    server = Server(stop, [message(1, 7), message(2, 8)], STOP)
    polling = asyncio.create_task(poll(server, engine, stop, poll_timeout=5))

    await handlers.wait_started(1, 2)
    await settle()
    assert len(server.calls) == 1

    handlers.release(1)
    await asyncio.wait_for(polling, 2)
    assert server.offsets == [None, 3]

    handlers.release(2)
    await asyncio.wait_for(engine.drained(), 2)


async def test_a_rejected_token_ends_polling_with_the_error() -> None:
    refused = AuthenticationError("missing, revoked or invalid bot token", status=401, code="unauthorized")
    stop = asyncio.Event()
    server = Server(stop, refused)

    with pytest.raises(AuthenticationError) as caught:
        await asyncio.wait_for(poll(server, Engine(MemoryStorage(), Handlers()), stop, poll_timeout=5), 2)

    assert caught.value is refused
    assert len(server.calls) == 1


async def test_a_webhook_in_the_way_ends_polling_with_a_polling_error() -> None:
    active = WebhookActiveError("a webhook is set; delete it before polling", status=409, code="webhook_active")
    stop = asyncio.Event()
    server = Server(stop, active)

    with pytest.raises(PollingError, match="webhook") as caught:
        await asyncio.wait_for(poll(server, Engine(MemoryStorage(), Handlers()), stop, poll_timeout=5), 2)

    assert caught.value.__cause__ is active
    assert len(server.calls) == 1


@pytest.mark.parametrize(
    "refusal",
    [
        ForbiddenError("this bot may not poll", status=403, code="forbidden"),
        NotFoundError("no such method", status=404, code="not_found"),
        ConflictError("conflict", status=409, code="conflict"),
        APIError("this bot is gone", status=410, code="gone"),
    ],
    ids=lambda e: str(e.status),
)
async def test_refusals_no_retry_gets_past_end_polling(refusal: APIError, waits: list[float]) -> None:
    stop = asyncio.Event()
    server = Server(stop, refusal)

    with pytest.raises(APIError) as caught:
        await asyncio.wait_for(poll(server, Engine(MemoryStorage(), Handlers()), stop, poll_timeout=5), 2)

    assert caught.value is refusal
    assert len(server.calls) == 1
    assert waits == []


async def test_a_paused_bot_is_waited_for_and_said_once_per_pause(
    waits: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="pgpcbot.engine")

    def paused() -> BotPausedError:
        return BotPausedError("this bot is paused by its owner", status=403, code="bot_paused")

    handlers = Handlers()
    stop = asyncio.Event()
    server = Server(stop, paused(), paused(), paused(), [message(1)], paused(), [], STOP)

    await asyncio.wait_for(poll(server, Engine(MemoryStorage(), handlers), stop, poll_timeout=5, paused_wait=12.0), 2)

    said = [
        (r.levelno, r.getMessage())
        for r in caplog.records
        if r.name == "pgpcbot.engine" and ("paused" in r.getMessage() or "active again" in r.getMessage())
    ]
    pause = (logging.WARNING, "the bot is paused by its owner; checking again every 12s")
    back = (logging.INFO, "the bot is active again")
    assert said == [pause, back, pause, back]
    assert waits == [12.0] * 4
    assert handlers.finished == [1]


@pytest.mark.parametrize(("retry_after", "expected"), [(7.5, 7.5), (None, 5.0)], ids=["as asked", "no hint"])
async def test_a_rate_limited_poll_waits_as_long_as_the_server_asks(
    retry_after: float | None, expected: float, waits: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    limited = RateLimitError("too many requests", status=429, code="rate_limited", retry_after=retry_after)
    handlers = Handlers()
    stop = asyncio.Event()
    server = Server(stop, limited, [message(1)], STOP)

    await asyncio.wait_for(poll(server, Engine(MemoryStorage(), handlers), stop, poll_timeout=5), 2)

    assert waits == [expected]
    assert f"polling rate limited, waiting {expected:.1f}s" in caplog.text
    assert handlers.finished == [1]
    assert server.offsets == [None, None, 2]


async def test_network_and_server_failures_back_off_and_polling_recovers(
    waits: list[float], jitter: list[tuple[float, float]], caplog: pytest.LogCaptureFixture
) -> None:
    handlers = Handlers()
    stop = asyncio.Event()
    server = Server(
        stop,
        NetworkError("connection reset"),
        ServerError("bad gateway", status=502, code="bad_gateway"),
        NetworkError("connection reset"),
        [message(1)],
        NetworkError("connection reset"),
        STOP,
    )

    await asyncio.wait_for(poll(server, Engine(MemoryStorage(), handlers), stop, poll_timeout=5), 2)

    assert handlers.finished == [1]
    assert server.offsets == [None, None, None, None, 2, 2]
    # a random wait under a doubling ceiling, which starts over once a poll gets through
    assert jitter == [(0, 2.0), (0, 4.0), (0, 8.0), (0, 2.0)]
    assert waits == [0.5, 1.0, 2.0, 0.5]
    assert [r.levelno for r in polling_failures(caplog)] == [logging.WARNING] * 4


@pytest.mark.usefixtures("waits")
async def test_the_backoff_tops_out_at_thirty_seconds_and_turns_to_errors_from_the_fifth_failure(
    jitter: list[tuple[float, float]], caplog: pytest.LogCaptureFixture
) -> None:
    stop = asyncio.Event()
    server = Server(stop, *[NetworkError("network unreachable") for _ in range(7)], STOP)

    await asyncio.wait_for(poll(server, Engine(MemoryStorage(), Handlers()), stop, poll_timeout=5), 2)

    assert [top for _, top in jitter] == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]
    assert [r.levelno for r in polling_failures(caplog)] == [logging.WARNING] * 4 + [logging.ERROR] * 3


async def test_polls_that_keep_coming_back_empty_at_once_point_at_a_second_poller(
    waits: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    stop = asyncio.Event()
    server = Server(stop, [], [], [], [], STOP)

    await asyncio.wait_for(poll(server, Engine(MemoryStorage(), Handlers()), stop, poll_timeout=50), 2)

    warnings = [r for r in caplog.records if "another process is probably polling" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    # and each one in a row waits a little longer before the next poll
    assert waits == [0.5, 1.0, 1.5, 2.0]


async def test_a_poll_that_brings_something_starts_the_empty_count_over(
    waits: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    stop = asyncio.Event()
    server = Server(stop, [], [], [message(1)], [], [], STOP)

    await asyncio.wait_for(poll(server, Engine(MemoryStorage(), Handlers()), stop, poll_timeout=50), 2)

    assert "another process" not in caplog.text
    assert waits == [0.5, 1.0, 0.5, 1.0]


async def test_quick_empty_polls_are_normal_with_a_short_poll_timeout(
    waits: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    stop = asyncio.Event()
    server = Server(stop, [], [], [], [], STOP)

    await asyncio.wait_for(poll(server, Engine(MemoryStorage(), Handlers()), stop, poll_timeout=9), 2)

    assert "another process" not in caplog.text
    assert waits == []


async def test_empty_polls_that_used_up_their_timeout_are_normal(
    waits: list[float], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = types.SimpleNamespace(now=1000.0)
    monkeypatch.setattr(engine_module, "time", types.SimpleNamespace(monotonic=lambda: clock.now))

    def quiet() -> list[dict[str, Any]]:
        clock.now += 50  # the server held the poll for the whole timeout
        return []

    stop = asyncio.Event()
    server = Server(stop, quiet, quiet, quiet, quiet, STOP)

    await asyncio.wait_for(poll(server, Engine(MemoryStorage(), Handlers()), stop, poll_timeout=50), 2)

    assert "another process" not in caplog.text
    assert waits == []


async def test_a_stop_set_before_polling_makes_no_poll_at_all() -> None:
    stop = asyncio.Event()
    stop.set()
    server = Server(stop)

    await asyncio.wait_for(poll(server, Engine(MemoryStorage(), Handlers()), stop), 1)

    assert server.calls == []


async def test_stop_cuts_a_wait_short() -> None:
    stop = asyncio.Event()

    def limited_for_a_long_time() -> RateLimitError:
        asyncio.get_running_loop().call_later(0.05, stop.set)
        return RateLimitError("too many requests", status=429, code="rate_limited", retry_after=30)

    server = Server(stop, limited_for_a_long_time)

    # the 30 second wait ends as soon as stop is set
    await asyncio.wait_for(poll(server, Engine(MemoryStorage(), Handlers()), stop, poll_timeout=5), 2)

    assert len(server.calls) == 1


@pytest.mark.parametrize(
    "error",
    [NetworkError("connection closed"), ServerError("bad gateway", status=502, code="bad_gateway")],
    ids=["network", "server"],
)
async def test_a_failure_after_stop_ends_polling_quietly(
    error: Exception, waits: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    stop = asyncio.Event()

    def closed_under_the_poll() -> Exception:
        stop.set()
        return error

    server = Server(stop, closed_under_the_poll)

    await asyncio.wait_for(poll(server, Engine(MemoryStorage(), Handlers()), stop, poll_timeout=5), 2)

    assert len(server.calls) == 1
    assert polling_failures(caplog) == []
    assert waits == []


async def test_skip_pending_confirms_everything_queued_without_running_a_handler() -> None:
    handlers = Handlers()
    storage = MemoryStorage()
    engine = Engine(storage, handlers)
    stop = asyncio.Event()
    server = Server(stop, [message(1), message(2), message(3)], [message(4), message(5)], [], [])

    assert await asyncio.wait_for(skip_pending(server, engine), 2) == 5

    # the last call only carries the offset that makes the server drop the last batch
    assert server.calls == [(None, 100, 0), (4, 100, 0), (6, 100, 0), (6, 1, 0)]
    await settle()
    assert handlers.started == []
    assert storage.accepted_max() == 5
    assert storage.pending() == []
    assert storage.failed() == []
    # a stray redelivery of a skipped one is not run either
    assert await engine.submit([message(3)]) == []


async def test_skip_pending_with_nothing_queued_makes_one_call() -> None:
    stop = asyncio.Event()
    server = Server(stop, [])

    assert await asyncio.wait_for(skip_pending(server, Engine(MemoryStorage(), Handlers())), 2) == 0

    assert server.calls == [(None, 100, 0)]


async def test_polling_on_sqlite_picks_up_after_the_skipped_updates(sqlite: SQLiteStorage) -> None:
    handlers = Handlers()
    engine = Engine(sqlite, handlers)
    stop = asyncio.Event()
    skipper = Server(stop, [message(1), message(2)], [], [])
    assert await asyncio.wait_for(skip_pending(skipper, engine), 2) == 2

    server = Server(stop, [message(3)], STOP)
    await asyncio.wait_for(poll(server, engine, stop, poll_timeout=5), 2)
    await asyncio.wait_for(engine.drained(), 2)

    assert server.offsets == [3, 4]
    assert handlers.finished == [3]


async def test_an_update_batch_that_cannot_be_stored_is_fetched_again_not_confirmed(
    waits: list[float],
) -> None:
    from pgpcbot.errors import StorageError as _StorageError

    class Refusing(MemoryStorage):
        def __init__(self) -> None:
            super().__init__()
            self.refusals = 1

        def accept(self, updates: Any) -> Any:
            if self.refusals:
                self.refusals -= 1
                raise _StorageError("disk full")
            return super().accept(updates)

    storage = Refusing()
    storage.open(BOT_ID)
    ran: list[int] = []

    async def process(update: Any) -> None:
        ran.append(update["updateId"])

    engine = Engine(storage, process)
    stop = asyncio.Event()
    offsets: list[int | None] = []

    async def fetch(offset: int | None, limit: int, wait: int) -> list[dict[str, Any]]:
        offsets.append(offset)
        if len(offsets) <= 2:
            return [{"updateId": 5, "kind": "message", "message": {"chat": {"id": 7}}}]
        stop.set()
        return []

    await poll(fetch, engine, stop, poll_timeout=1)
    # the batch came twice: the first time it could not be stored, so it was not confirmed
    assert offsets[:2] == [None, None]
    assert ran == [5]
