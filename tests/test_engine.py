from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from pgpcbot.engine import Engine, describe
from pgpcbot.errors import StorageError
from pgpcbot.storage import MemoryStorage, SQLiteStorage

from .fakeapi import BOT_ID


def message(uid: int, chat_id: int) -> dict[str, Any]:
    return {
        "updateId": uid,
        "kind": "message",
        "message": {"id": 1000 + uid, "chat": {"id": chat_id, "kind": "private"}, "text": f"update {uid}"},
    }


def ids(updates: Sequence[Mapping[str, Any]]) -> list[int]:
    return [u["updateId"] for u in updates]


async def settle() -> None:
    # enough turns of the loop for every task that can move to get as far as it can
    for _ in range(20):
        await asyncio.sleep(0)


class Handlers:
    """The process function: notes what ran, and can hold any update until it is let go."""

    def __init__(self, *, fail: Mapping[int, Exception] | None = None) -> None:
        self.started: list[int] = []
        self.finished: list[int] = []
        self.cancelled: list[int] = []
        self.running = 0
        self.peak = 0
        self._fail = dict(fail or {})
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
            await asyncio.wait_for(self._arrival(uid).wait(), 2)

    def _arrival(self, uid: int) -> asyncio.Event:
        return self._arrived.setdefault(uid, asyncio.Event())

    async def __call__(self, update: Mapping[str, Any]) -> None:
        uid = update["updateId"]
        self.started.append(uid)
        self._arrival(uid).set()
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            if uid in self._gates:
                await self._gates[uid].wait()
            if uid in self._fail:
                raise self._fail[uid]
        except asyncio.CancelledError:
            self.cancelled.append(uid)
            raise
        finally:
            self.running -= 1
        self.finished.append(uid)


def test_concurrency_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="concurrency"):
        Engine(MemoryStorage(), Handlers(), concurrency=0)


async def test_updates_of_one_chat_run_one_at_a_time_in_update_id_order() -> None:
    handlers = Handlers()
    handlers.hold(1, 2, 3)
    engine = Engine(MemoryStorage(), handlers)

    # handed over out of order: the storage sorts the batch before anything is scheduled
    await engine.submit([message(3, 7), message(1, 7), message(2, 7)])

    await handlers.wait_started(1)
    await settle()
    assert handlers.started == [1]
    handlers.release(1)
    await handlers.wait_started(2)
    await settle()
    assert handlers.started == [1, 2]
    handlers.release(2, 3)
    await asyncio.wait_for(engine.drained(), 2)
    assert handlers.finished == [1, 2, 3]
    assert handlers.peak == 1


async def test_updates_of_different_chats_run_side_by_side() -> None:
    both_inside = asyncio.Barrier(2)
    storage = MemoryStorage()

    async def process(update: Mapping[str, Any]) -> None:
        # each one waits for the other: it only gets through if they run at the same time
        await asyncio.wait_for(both_inside.wait(), 2)

    engine = Engine(storage, process)
    await engine.submit([message(1, 7), message(2, 8)])
    await asyncio.wait_for(engine.drained(), 3)

    assert storage.failed() == []
    assert engine.processed == 2


@pytest.mark.parametrize(
    "behind",
    [
        {"kind": "callback_query", "callback": {"id": "cb", "chat": {"id": 7, "kind": "private"}, "messageId": 1001}},
        {"kind": "my_chat_member", "chat": {"id": 7, "kind": "group"}},
    ],
    ids=["callback", "chat-level update"],
)
async def test_other_kinds_of_update_queue_behind_a_message_of_the_same_chat(behind: dict[str, Any]) -> None:
    handlers = Handlers()
    handlers.hold(1)
    engine = Engine(MemoryStorage(), handlers)

    await engine.submit([message(1, 7), {"updateId": 2, **behind}, message(3, 8)])
    # 3 is in another chat and was scheduled last: once it has run, 2 had every chance to start
    await handlers.wait_started(1, 3)
    await settle()
    assert 2 not in handlers.started

    handlers.release(1)
    await asyncio.wait_for(engine.drained(), 2)
    assert handlers.started.index(1) < handlers.started.index(2)


async def test_updates_with_no_chat_get_a_lane_each() -> None:
    both_inside = asyncio.Barrier(2)
    storage = MemoryStorage()

    async def process(update: Mapping[str, Any]) -> None:
        await asyncio.wait_for(both_inside.wait(), 2)

    engine = Engine(storage, process)
    await engine.submit([{"updateId": 1, "kind": "my_chat_member"}, {"updateId": 2, "kind": "my_chat_member"}])
    await asyncio.wait_for(engine.drained(), 3)

    assert storage.failed() == []
    assert engine.processed == 2


async def test_no_more_than_concurrency_updates_run_at_once() -> None:
    handlers = Handlers()
    handlers.hold(1, 2, 3, 4, 5)
    engine = Engine(MemoryStorage(), handlers, concurrency=2)

    await engine.submit([message(uid, 100 + uid) for uid in range(1, 6)])
    await handlers.wait_started(1, 2)
    await settle()
    assert handlers.started == [1, 2]

    handlers.release(1)
    await handlers.wait_started(3)
    await settle()
    assert handlers.started == [1, 2, 3]

    handlers.release(2, 3, 4, 5)
    await asyncio.wait_for(engine.drained(), 2)
    assert handlers.peak == 2
    assert sorted(handlers.finished) == [1, 2, 3, 4, 5]


async def test_submit_hands_back_only_updates_never_seen_before() -> None:
    handlers = Handlers()
    engine = Engine(MemoryStorage(), handlers)

    assert await engine.submit([]) == []
    assert ids(await engine.submit([message(1, 7), message(2, 7)])) == [1, 2]
    assert await engine.submit([message(2, 7), message(1, 7)]) == []
    assert ids(await engine.submit([message(2, 7), message(3, 7)])) == [3]

    await asyncio.wait_for(engine.drained(), 2)
    assert handlers.finished == [1, 2, 3]


async def test_a_batch_the_storage_refuses_schedules_nothing() -> None:
    handlers = Handlers()
    storage = MemoryStorage()
    engine = Engine(storage, handlers)

    with pytest.raises(StorageError, match="updateId"):
        await engine.submit([message(1, 7), {"updateId": "2", "kind": "message"}])

    await settle()
    assert storage.accepted_max() is None
    assert engine.busy == 0
    assert handlers.started == []


async def test_a_raising_update_is_set_aside_with_its_error_and_the_rest_go_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handlers = Handlers(fail={2: ValueError("no such option")})
    storage = MemoryStorage()
    engine = Engine(storage, handlers)

    await engine.submit([message(1, 7), message(2, 7), message(3, 7), message(4, 8)])
    await asyncio.wait_for(engine.drained(), 2)

    [failed] = storage.failed()
    assert failed.update_id == 2
    assert failed.payload == message(2, 7)
    # type, message, and where it was raised
    assert failed.error.startswith("ValueError: no such option at test_engine.py:")
    # the chat goes on after the update that failed
    assert [uid for uid in handlers.finished if uid != 4] == [1, 3]
    assert 4 in handlers.finished
    assert storage.pending() == []
    assert (engine.processed, engine.failed) == (3, 1)

    [record] = [r for r in caplog.records if r.name == "pgpcbot.engine" and r.levelno == logging.ERROR]
    assert "update 2 failed and was set aside: ValueError: no such option" in record.getMessage()
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], ValueError)


async def test_an_update_already_started_max_runs_times_is_failed_without_running(
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = MemoryStorage()
    storage.accept([message(1, 7)])
    for _ in range(3):
        storage.begin(1)  # three runs that never finished: the process died each time
    handlers = Handlers()
    engine = Engine(storage, handlers, max_runs=3)

    await engine.recover()
    await asyncio.wait_for(engine.drained(), 2)

    assert handlers.started == []
    [failed] = storage.failed()
    assert failed.update_id == 1
    assert failed.error == "gave up: started 3 times and never finished (the process kept stopping)"
    assert storage.pending() == []
    assert (engine.processed, engine.failed) == (0, 1)
    assert "update 1: gave up: started 3 times" in caplog.text


async def test_an_update_on_its_last_allowed_run_still_runs() -> None:
    storage = MemoryStorage()
    storage.accept([message(1, 7)])
    for _ in range(2):
        storage.begin(1)
    handlers = Handlers()
    engine = Engine(storage, handlers, max_runs=3)

    await engine.recover()
    await asyncio.wait_for(engine.drained(), 2)

    assert handlers.finished == [1]
    assert storage.failed() == []
    assert storage.pending() == []


async def test_recover_schedules_what_was_left_pending_in_order_and_only_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="pgpcbot.engine")
    storage = MemoryStorage()
    storage.accept([message(1, 7), message(2, 7), message(3, 8)])
    handlers = Handlers()
    handlers.hold(1)
    engine = Engine(storage, handlers)

    assert await engine.recover() == 3
    await handlers.wait_started(1)
    # again while they are in flight, and a redelivery on top: nothing gets a second run
    await engine.recover()
    assert await engine.submit([message(2, 7)]) == []
    handlers.release(1)
    await asyncio.wait_for(engine.drained(), 2)

    assert sorted(handlers.started) == [1, 2, 3]
    assert handlers.started.index(1) < handlers.started.index(2)
    assert storage.pending() == []
    assert "resuming 3 update(s) left pending from the last run" in caplog.text


async def test_recover_with_nothing_pending_does_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="pgpcbot.engine")
    engine = Engine(MemoryStorage(), Handlers())

    assert await engine.recover() == 0
    assert engine.busy == 0
    assert "resuming" not in caplog.text


async def test_a_restart_on_sqlite_runs_what_the_last_process_left_pending(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    before = SQLiteStorage(path)
    before.open(BOT_ID)
    before.accept([message(1, 7), message(2, 7), message(3, 7)])
    before.finish(1)
    before.close()

    storage = SQLiteStorage(path)
    storage.open(BOT_ID)
    try:
        handlers = Handlers()
        engine = Engine(storage, handlers)
        assert await engine.recover() == 2
        await asyncio.wait_for(engine.drained(), 5)
        assert handlers.finished == [2, 3]
        assert storage.pending() == []
    finally:
        storage.close()


class DiskLikeStorage(MemoryStorage):
    """A memory storage that says it is durable, and notes which thread calls it."""

    durable = True

    def __init__(self) -> None:
        super().__init__()
        self.threads: set[int] = set()

    def accept(self, updates: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        self.threads.add(threading.get_ident())
        return super().accept(updates)

    def begin(self, update_id: int) -> int:
        self.threads.add(threading.get_ident())
        return super().begin(update_id)

    def finish(self, update_id: int) -> None:
        self.threads.add(threading.get_ident())
        super().finish(update_id)


async def test_a_durable_storage_is_called_off_the_event_loop() -> None:
    storage = DiskLikeStorage()
    handlers = Handlers()
    engine = Engine(storage, handlers)

    await engine.submit([message(1, 7)])
    await asyncio.wait_for(engine.drained(), 2)

    assert handlers.finished == [1]
    assert storage.threads
    assert threading.get_ident() not in storage.threads


async def test_drained_returns_only_once_nothing_is_scheduled_or_running() -> None:
    handlers = Handlers()
    handlers.hold(1)
    engine = Engine(MemoryStorage(), handlers)
    await asyncio.wait_for(engine.drained(), 1)  # idle from the start

    await engine.submit([message(1, 7), message(2, 7)])
    waiting = asyncio.create_task(engine.drained())
    await settle()
    assert not waiting.done()
    assert engine.busy == 2

    handlers.release(1)
    await asyncio.wait_for(waiting, 2)
    assert engine.busy == 0
    assert handlers.finished == [1, 2]


async def test_room_closes_at_max_pending_and_opens_again_at_half() -> None:
    handlers = Handlers()
    handlers.hold(1, 2, 3, 4)
    engine = Engine(MemoryStorage(), handlers, max_pending=4)

    await engine.submit([message(1, 7), message(2, 8), message(3, 9)])
    await asyncio.wait_for(engine.room(), 1)

    await engine.submit([message(4, 10)])
    waiting = asyncio.create_task(engine.room())
    await settle()
    assert not waiting.done()

    handlers.release(1)
    await settle()
    assert handlers.finished == [1]
    assert not waiting.done()  # three still in flight is more than half

    handlers.release(2)
    await asyncio.wait_for(waiting, 1)

    handlers.release(3, 4)
    await asyncio.wait_for(engine.drained(), 2)


async def test_wait_for_says_whether_the_update_got_through_in_time() -> None:
    handlers = Handlers()
    handlers.hold(1)
    engine = Engine(MemoryStorage(), handlers)
    assert await engine.wait_for(99, 1) is True  # nothing by that id to wait for

    await engine.submit([message(1, 7), message(2, 7)])
    assert engine.is_running(2)  # queued behind 1 counts too
    assert await engine.wait_for(2, 0.05) is False
    # running out of time cancels nothing
    await settle()
    assert handlers.cancelled == []

    handlers.release(1)
    assert await engine.wait_for(2, 2) is True
    assert handlers.finished == [1, 2]
    assert not engine.is_running(2)


async def test_stop_lets_what_finishes_within_the_grace_period_finish(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="pgpcbot.engine")
    handlers = Handlers()
    handlers.hold(1)
    storage = MemoryStorage()
    engine = Engine(storage, handlers)

    await engine.submit([message(1, 7)])
    await handlers.wait_started(1)
    asyncio.get_running_loop().call_later(0.02, handlers.release, 1)
    await asyncio.wait_for(engine.stop(grace=2), 3)

    assert handlers.finished == [1]
    assert handlers.cancelled == []
    assert storage.pending() == []
    assert "waiting up to 2s for 1 update(s) still running" in caplog.text
    assert "cut short" not in caplog.text


async def test_stop_with_nothing_running_returns_at_once(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="pgpcbot.engine")
    engine = Engine(MemoryStorage(), Handlers())

    await asyncio.wait_for(engine.stop(grace=30), 1)

    assert caplog.records == []


async def test_stop_cancels_what_outlasts_the_grace_period_and_leaves_it_pending(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handlers = Handlers()
    handlers.hold(1)
    storage = MemoryStorage()
    engine = Engine(storage, handlers)

    await engine.submit([message(1, 7), message(2, 8)])
    await handlers.wait_started(1, 2)
    await asyncio.wait_for(engine.stop(grace=0.05), 2)

    assert handlers.cancelled == [1]
    assert handlers.finished == [2]
    assert ids(storage.pending()) == [1]
    assert storage.failed() == []
    assert engine.busy == 0
    assert "1 update(s) cut short; they stay pending and run again on the next start" in caplog.text

    # and the next start picks it up again
    again = Handlers()
    restarted = Engine(storage, again)
    assert await restarted.recover() == 1
    await asyncio.wait_for(restarted.drained(), 2)
    assert again.finished == [1]
    assert storage.pending() == []


async def test_stop_also_cuts_short_an_update_queued_behind_a_late_one() -> None:
    handlers = Handlers()
    handlers.hold(1)
    storage = MemoryStorage()
    engine = Engine(storage, handlers)

    await engine.submit([message(1, 7), message(2, 7)])
    await handlers.wait_started(1)
    await asyncio.wait_for(engine.stop(grace=0.05), 2)

    assert handlers.started == [1]
    assert ids(storage.pending()) == [1, 2]


def broken_handler() -> None:
    raise RuntimeError("handler broke")


def test_describe_names_the_type_the_message_and_where_it_was_raised() -> None:
    # never raised: no traceback, so no place
    assert describe(KeyError("chat")) == "KeyError: 'chat'"

    with pytest.raises(RuntimeError) as caught:
        broken_handler()

    raised_at = broken_handler.__code__.co_firstlineno + 1
    assert describe(caught.value) == f"RuntimeError: handler broke at test_engine.py:{raised_at}"


# --- what a failure leaves behind, and storage that fails ---


def sealed(uid: int, chat_id: int) -> dict[str, Any]:
    update = message(uid, chat_id)
    update["message"] = {**update["message"], "armored": "-----BEGIN PGP MESSAGE-----\n...", "text": None}
    return update


async def test_a_handler_error_from_an_encrypted_chat_keeps_its_words_out_of_disk_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = MemoryStorage()
    storage.open(BOT_ID)
    secret_text = "the vault code is 4471"
    engine = Engine(storage, Handlers(fail={1: ValueError(f"bad order: {secret_text}")}))

    with caplog.at_level(logging.ERROR, logger="pgpcbot.engine"):
        await engine.submit([sealed(1, 7)])
        await engine.drained()

    [failed] = storage.failed()
    assert failed.error.startswith("ValueError (message withheld")
    assert secret_text not in failed.error
    assert secret_text not in caplog.text
    assert "test_engine.py" in caplog.text  # the frames are still there to debug with


async def test_pgpcbot_errors_keep_their_message_even_from_an_encrypted_chat() -> None:
    from pgpcbot.errors import NotARecipientError

    storage = MemoryStorage()
    storage.open(BOT_ID)
    engine = Engine(storage, Handlers(fail={1: NotARecipientError("sealed to none of the bot's keys")}))
    await engine.submit([sealed(1, 7)])
    await engine.drained()
    [failed] = storage.failed()
    assert failed.error.startswith("NotARecipientError: sealed to none of the bot's keys")


async def test_recover_schedules_no_more_than_there_is_room_for() -> None:
    storage = MemoryStorage()
    storage.open(BOT_ID)
    storage.accept([message(uid, 7 + uid) for uid in (1, 2, 3, 4, 5)])
    handlers = Handlers()
    handlers.hold(1, 2, 3, 4, 5)
    engine = Engine(storage, handlers, max_pending=2)

    assert await engine.recover() == 2
    assert await engine.recover() == 0  # full
    handlers.release(1, 2)
    await settle()
    assert await engine.recover(quiet=True) == 2
    handlers.release(3, 4, 5)
    await settle()
    assert await engine.recover(quiet=True) == 1
    await engine.drained()
    assert sorted(handlers.finished) == [1, 2, 3, 4, 5]


class FlakyBegin(MemoryStorage):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def begin(self, update_id: int) -> int:
        if self.failures:
            self.failures -= 1
            raise StorageError("database is locked")
        return super().begin(update_id)


async def test_a_storage_that_cannot_count_a_run_gets_a_few_tries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    storage = FlakyBegin(failures=2)
    storage.open(BOT_ID)
    handlers = Handlers()
    engine = Engine(storage, handlers)
    await engine.submit([message(1, 7)])
    await engine.drained()
    assert handlers.finished == [1]


async def test_an_update_stays_pending_when_the_storage_keeps_failing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    storage = FlakyBegin(failures=99)
    storage.open(BOT_ID)
    handlers = Handlers()
    engine = Engine(storage, handlers)
    with caplog.at_level(logging.ERROR, logger="pgpcbot.engine"):
        await engine.submit([message(1, 7)])
        await engine.drained()
    assert handlers.started == []
    assert ids(storage.pending()) == [1]
    assert "stays pending" in caplog.text


_real_sleep = asyncio.sleep


async def _no_sleep(seconds: float) -> None:
    await _real_sleep(0)


async def test_an_update_already_finished_is_not_run_again() -> None:
    storage = MemoryStorage()
    storage.open(BOT_ID)
    storage.accept([message(1, 7)])
    storage.finish(1)
    handlers = Handlers()
    engine = Engine(storage, handlers)
    # as if read from the inbox just before it finished
    engine._schedule(message(1, 7))
    await engine.drained()
    assert handlers.started == []


async def test_store_keeps_updates_without_running_them() -> None:
    storage = MemoryStorage()
    storage.open(BOT_ID)
    handlers = Handlers()
    engine = Engine(storage, handlers)
    assert ids(await engine.store([message(1, 7), message(2, 8)])) == [1, 2]
    assert await engine.store([]) == []
    await settle()
    assert handlers.started == []
    assert ids(storage.pending()) == [1, 2]
