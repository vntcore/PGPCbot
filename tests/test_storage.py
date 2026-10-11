from __future__ import annotations

import gc
import os
import sqlite3
import stat
import subprocess
import sys
import time
import warnings
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pgpcbot import MemoryStorage, PollingConflictError, SQLiteStorage, Storage, StorageError

from .fakeapi import BOT_ID

OTHER_BOT = 913


def upd(uid: int, chat: int = 10, text: str | None = None) -> dict[str, Any]:
    return {
        "updateId": uid,
        "kind": "message",
        "message": {"id": 1000 + uid, "chat": {"id": chat, "kind": "private"}, "text": text or f"m{uid}"},
    }


def ids(updates: Sequence[Mapping[str, Any]]) -> list[int]:
    return [u["updateId"] for u in updates]


@pytest.fixture(params=["memory", "sqlite"])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Storage]:
    store: Storage = MemoryStorage() if request.param == "memory" else SQLiteStorage(tmp_path / "bot.db")
    store.open(BOT_ID)
    yield store
    store.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "bot.db"


@pytest.fixture
def sqlite(db_path: Path) -> Iterator[SQLiteStorage]:
    store = SQLiteStorage(db_path)
    store.open(BOT_ID)
    yield store
    store.close()


# --- accepting ---


def test_accept_returns_the_new_updates_oldest_first(storage: Storage) -> None:
    fresh = storage.accept([upd(3), upd(1), upd(2)])

    assert ids(fresh) == [1, 2, 3]
    assert fresh[0] == upd(1)
    assert ids(storage.pending()) == [1, 2, 3]
    assert storage.accepted_max() == 3


def test_accept_ignores_ids_at_or_below_the_high_water_mark(storage: Storage) -> None:
    storage.accept([upd(1), upd(2), upd(3)])

    assert ids(storage.accept([upd(2), upd(3), upd(4)])) == [4]
    assert storage.accept([upd(4)]) == []
    assert storage.accepted_max() == 4
    assert ids(storage.pending()) == [1, 2, 3, 4]


def test_an_id_below_the_mark_is_ignored_even_if_it_was_never_seen(storage: Storage) -> None:
    # the server delivers in order: anything under the mark is a redelivery by definition
    storage.accept([upd(7)])

    assert storage.accept([upd(5)]) == []
    assert ids(storage.pending()) == [7]


def test_accept_keeps_one_copy_of_an_id_repeated_in_a_batch(storage: Storage) -> None:
    fresh = storage.accept([upd(1), upd(2), upd(1, text="again"), upd(2)])

    assert ids(fresh) == [1, 2]
    assert ids(storage.pending()) == [1, 2]
    assert storage.count_pending() == 2


def test_accept_sorts_out_of_order_input_and_puts_the_mark_on_the_top(storage: Storage) -> None:
    assert ids(storage.accept([upd(9), upd(4), upd(6)])) == [4, 6, 9]
    assert storage.accepted_max() == 9

    # a later batch overlapping the mark only lets through what is above it
    assert ids(storage.accept([upd(10), upd(8), upd(11)])) == [10, 11]
    assert ids(storage.pending()) == [4, 6, 9, 10, 11]


def test_accepted_max_is_none_until_something_is_accepted(storage: Storage) -> None:
    assert storage.accepted_max() is None
    assert storage.accept([]) == []
    assert storage.accepted_max() is None
    assert storage.pending() == []


@pytest.mark.parametrize(
    "bad",
    [{"kind": "message"}, {"updateId": None}, {"updateId": "7"}, {"updateId": True}, {"updateId": 7.0}],
    ids=["missing", "null", "string", "bool", "float"],
)
def test_accept_refuses_a_batch_with_an_unusable_update_id(storage: Storage, bad: dict[str, Any]) -> None:
    with pytest.raises(StorageError, match="updateId"):
        storage.accept([upd(1), bad, upd(2)])

    assert storage.pending() == []
    assert storage.accepted_max() is None


# --- the life of an update ---


def test_pending_lists_what_is_not_finished_by_update_id(storage: Storage) -> None:
    storage.accept([upd(2), upd(5)])
    storage.accept([upd(6), upd(8)])
    storage.finish(5)

    assert ids(storage.pending()) == [2, 6, 8]
    assert storage.count_pending() == 3


def test_begin_counts_the_runs_of_each_update(storage: Storage) -> None:
    storage.accept([upd(1), upd(2)])

    assert [storage.begin(1), storage.begin(1), storage.begin(1)] == [1, 2, 3]
    assert storage.begin(2) == 1
    # counting a run is not finishing it
    assert ids(storage.pending()) == [1, 2]


def test_finish_takes_the_update_out_of_pending_for_good(storage: Storage) -> None:
    storage.accept([upd(1), upd(2)])
    storage.begin(1)
    storage.finish(1)
    storage.finish(1)
    storage.finish(99)

    assert ids(storage.pending()) == [2]
    assert storage.failed() == []
    # finished is still seen: a redelivery stays out
    assert storage.accept([upd(1)]) == []


def test_fail_moves_the_update_to_the_failed_list_with_error_and_time(storage: Storage) -> None:
    storage.accept([upd(1), upd(2, text="è già sera 🌙")])
    storage.begin(2)
    before = datetime.now(UTC)
    storage.fail(2, "KeyError: 'x' at handlers.py:12")
    after = datetime.now(UTC)

    assert ids(storage.pending()) == [1]
    [failed] = storage.failed()
    assert failed.update_id == 2
    assert failed.payload == upd(2, text="è già sera 🌙")
    assert failed.error == "KeyError: 'x' at handlers.py:12"
    assert failed.failed_at.utcoffset() == timedelta(0)
    assert before - timedelta(seconds=1) <= failed.failed_at <= after + timedelta(seconds=1)


def test_failing_an_update_that_is_not_pending_records_nothing(storage: Storage) -> None:
    storage.accept([upd(1)])
    storage.finish(1)
    storage.fail(1, "too late")
    storage.fail(42, "never seen")

    assert storage.failed() == []


def test_failed_is_ordered_by_update_id_and_limited(storage: Storage) -> None:
    storage.accept([upd(i) for i in range(1, 7)])
    for uid in (5, 2, 4, 1):
        storage.fail(uid, f"error {uid}")

    assert [f.update_id for f in storage.failed()] == [1, 2, 4, 5]
    assert [f.update_id for f in storage.failed(limit=2)] == [1, 2]
    assert [f.error for f in storage.failed(limit=1)] == ["error 1"]


def test_requeue_failed_puts_every_failed_update_back_in_line(storage: Storage) -> None:
    storage.accept([upd(1), upd(2), upd(3), upd(4)])
    storage.fail(1, "a")
    storage.fail(3, "b")

    assert storage.requeue_failed() == 2
    assert storage.failed() == []
    assert ids(storage.pending()) == [1, 2, 3, 4]
    assert storage.pending()[0] == upd(1)


def test_requeue_failed_moves_only_the_ids_given(storage: Storage) -> None:
    storage.accept([upd(1), upd(2), upd(3)])
    for uid in (1, 2, 3):
        storage.fail(uid, "boom")

    # 7 was never failed: it is not counted
    assert storage.requeue_failed([3, 7]) == 1
    assert ids(storage.pending()) == [3]
    assert [f.update_id for f in storage.failed()] == [1, 2]


def test_requeue_failed_with_an_empty_list_moves_nothing(storage: Storage) -> None:
    storage.accept([upd(1)])
    storage.fail(1, "boom")

    assert storage.requeue_failed([]) == 0
    assert storage.pending() == []
    assert [f.update_id for f in storage.failed()] == [1]


def test_a_requeued_update_counts_its_runs_from_zero(storage: Storage) -> None:
    storage.accept([upd(1)])
    storage.begin(1)
    storage.begin(1)
    storage.fail(1, "boom")
    storage.requeue_failed([1])

    assert storage.begin(1) == 1


def test_discard_failed_forgets_all_some_or_none(storage: Storage) -> None:
    storage.accept([upd(i) for i in range(1, 5)])
    for uid in (1, 2, 3, 4):
        storage.fail(uid, "boom")

    assert storage.discard_failed([]) == 0
    assert storage.discard_failed([2, 9]) == 1
    assert [f.update_id for f in storage.failed()] == [1, 3, 4]
    assert storage.discard_failed() == 3
    assert storage.failed() == []
    assert storage.pending() == []
    # discarded is not forgotten as seen
    assert storage.accept([upd(3)]) == []


def test_pins_are_kept_uppercase_and_can_be_replaced(storage: Storage) -> None:
    assert storage.get_pin(55) is None

    storage.set_pin(55, "ab12cd34" * 5)
    assert storage.get_pin(55) == "AB12CD34" * 5

    storage.set_pin(55, "ff" * 20)
    assert storage.get_pin(55) == "FF" * 20
    assert storage.get_pin(56) is None


# --- one storage, several bots ---


def test_opening_for_another_bot_shows_none_of_the_first_bots_data(storage: Storage) -> None:
    storage.accept([upd(1), upd(2)])
    storage.fail(2, "boom")
    storage.set_pin(55, "AA" * 20)

    storage.open(OTHER_BOT)

    assert storage.accepted_max() is None
    assert storage.pending() == []
    assert storage.failed() == []
    assert storage.get_pin(55) is None
    assert ids(storage.accept([upd(1)])) == [1]


def test_memory_storage_reopened_for_the_same_bot_keeps_its_queue() -> None:
    store = MemoryStorage()
    store.open(BOT_ID)
    store.accept([upd(1), upd(2)])
    store.finish(1)

    store.open(BOT_ID)

    assert ids(store.pending()) == [2]
    assert store.accepted_max() == 2


def test_memory_storage_lock_is_held_by_one_bot_at_a_time() -> None:
    store = MemoryStorage()

    assert store.try_lock() is True
    assert store.try_lock() is False
    store.unlock()
    assert store.try_lock() is True


def test_two_bots_in_one_sqlite_file_do_not_see_each_other(db_path: Path) -> None:
    first, second = SQLiteStorage(db_path), SQLiteStorage(db_path)
    first.open(BOT_ID)
    second.open(OTHER_BOT)
    try:
        first.accept([upd(1), upd(2), upd(3)])
        first.set_pin(55, "AA" * 20)
        second.accept([upd(1)])
        second.fail(1, "boom")

        assert first.accepted_max() == 3
        assert second.accepted_max() == 1
        assert ids(first.pending()) == [1, 2, 3]
        assert second.pending() == []
        assert first.failed() == []
        assert [f.update_id for f in second.failed()] == [1]
        assert second.get_pin(55) is None
        assert second.requeue_failed() == 1
        assert ids(first.pending()) == [1, 2, 3]
        assert first.discard_failed() == 0
    finally:
        first.close()
        second.close()


# --- SQLite: durability ---


def test_sqlite_keeps_everything_across_a_restart(db_path: Path) -> None:
    before = SQLiteStorage(db_path)
    before.open(BOT_ID)
    before.accept([upd(1), upd(2, text="è già sera 🌙"), upd(3)])
    before.begin(2)
    before.begin(2)
    before.finish(1)
    before.fail(3, "ValueError: nope")
    before.set_pin(55, "ab" * 20)
    before.close()

    after = SQLiteStorage(db_path)
    after.open(BOT_ID)
    try:
        assert after.accepted_max() == 3
        assert after.pending() == [upd(2, text="è già sera 🌙")]
        # the run counter is what catches a crash loop, so it has to survive one
        assert after.begin(2) == 3
        assert [(f.update_id, f.error) for f in after.failed()] == [(3, "ValueError: nope")]
        assert after.get_pin(55) == "AB" * 20
        assert after.accept([upd(3)]) == []
    finally:
        after.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_sqlite_file_is_created_readable_by_its_owner_only(db_path: Path) -> None:
    store = SQLiteStorage(db_path)
    old_umask = os.umask(0o022)
    try:
        store.open(BOT_ID)
        store.accept([upd(1)])
        assert store.try_lock()
    finally:
        os.umask(old_umask)
    try:
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(Path(f"{db_path}.lock").stat().st_mode) == 0o600
    finally:
        store.close()


def test_sqlite_runs_in_wal_mode_with_full_sync(sqlite: SQLiteStorage, db_path: Path) -> None:
    assert sqlite._db is not None
    assert sqlite._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    # 2 is FULL: an update confirmed to the server must survive a power cut
    assert sqlite._db.execute("PRAGMA synchronous").fetchone()[0] == 2

    # WAL is a property of the file, not only of this connection
    other = sqlite3.connect(db_path)
    try:
        assert other.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        other.close()


def test_sqlite_synchronous_can_be_lowered_on_purpose(tmp_path: Path) -> None:
    store = SQLiteStorage(tmp_path / "fast.db", synchronous="normal")
    store.open(BOT_ID)
    try:
        assert store._db is not None
        assert store._db.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        store.close()

    with pytest.raises(ValueError, match="synchronous"):
        SQLiteStorage(tmp_path / "x.db", synchronous="OFF")


class _FailingInsert:
    """A connection that breaks on the n-th row written into the inbox."""

    def __init__(self, real: sqlite3.Connection, on: int) -> None:
        self._real = real
        self._on = on
        self.inserts = 0

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        if sql.startswith("INSERT OR IGNORE INTO inbox"):
            self.inserts += 1
            if self.inserts == self._on:
                raise sqlite3.OperationalError("disk I/O error")
        return self._real.execute(sql, params)

    def close(self) -> None:
        self._real.close()


def test_sqlite_accept_stores_all_or_nothing(sqlite: SQLiteStorage) -> None:
    sqlite.accept([upd(1)])
    assert sqlite._db is not None
    flaky = _FailingInsert(sqlite._db, on=2)
    sqlite._db = flaky  # type: ignore[assignment]

    with pytest.raises(StorageError, match="disk I/O error"):
        sqlite.accept([upd(2), upd(3), upd(4)])

    assert flaky.inserts == 2
    assert sqlite.accepted_max() == 1
    assert ids(sqlite.pending()) == [1]
    # the transaction was rolled back, not left open: the next batch goes through
    assert ids(sqlite.accept([upd(2), upd(3)])) == [2, 3]


def test_sqlite_accept_rolls_back_on_an_error_that_is_not_sqlites(sqlite: SQLiteStorage) -> None:
    unwritable = {"updateId": 3, "kind": "message", "extra": object()}

    with pytest.raises(TypeError):
        sqlite.accept([upd(2), unwritable, upd(4)])

    assert sqlite.accepted_max() is None
    assert sqlite.pending() == []
    assert ids(sqlite.accept([upd(2)])) == [2]


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("accepted_max", ()),
        ("accept", ([upd(1)],)),
        ("pending", ()),
        ("count_pending", ()),
        ("begin", (1,)),
        ("finish", (1,)),
        ("fail", (1, "x")),
        ("failed", ()),
        ("requeue_failed", ()),
        ("discard_failed", ()),
        ("get_pin", (55,)),
        ("set_pin", (55, "AA" * 20)),
    ],
)
def test_sqlite_refuses_to_work_before_open(db_path: Path, method: str, args: tuple[Any, ...]) -> None:
    store = SQLiteStorage(db_path)

    with pytest.raises(StorageError, match="not open"):
        getattr(store, method)(*args)
    assert not db_path.exists()


def test_sqlite_refuses_to_work_after_close(db_path: Path) -> None:
    store = SQLiteStorage(db_path)
    store.open(BOT_ID)
    store.accept([upd(1)])
    store.close()

    with pytest.raises(StorageError, match="not open"):
        store.pending()

    # and it can be opened again
    store.open(BOT_ID)
    try:
        assert ids(store.pending()) == [1]
    finally:
        store.close()


def test_sqlite_open_on_a_path_it_cannot_use_raises_storage_error(tmp_path: Path) -> None:
    (tmp_path / "taken.db").mkdir()
    store = SQLiteStorage(tmp_path / "taken.db")

    with pytest.raises(StorageError, match="cannot open"):
        store.open(BOT_ID)


def test_sqlite_open_on_a_file_that_is_not_a_database_raises_storage_error(tmp_path: Path) -> None:
    path = tmp_path / "junk.db"
    path.write_bytes(b"definitely not sqlite " * 64)
    store = SQLiteStorage(path)

    with warnings.catch_warnings():
        # the leak is reported on its own below
        warnings.simplefilter("ignore", ResourceWarning)
        with pytest.raises(StorageError, match="not a database"):
            store.open(BOT_ID)
        gc.collect()


def test_sqlite_open_that_fails_leaves_no_connection_behind(tmp_path: Path) -> None:
    path = tmp_path / "junk.db"
    path.write_bytes(b"definitely not sqlite " * 64)
    store = SQLiteStorage(path)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            store.open(BOT_ID)
        except StorageError:
            pass
        gc.collect()

    assert [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)] == []


# --- SQLite: one process per bot ---

_TRY_LOCK = """
import sys
from pgpcbot import PollingConflictError, SQLiteStorage
store = SQLiteStorage(sys.argv[1])
store.open(int(sys.argv[2]))
print(store.try_lock())
try:
    store.require_lock()
except PollingConflictError as exc:
    print("conflict:", exc)
else:
    print("locked")
"""

_HOLD_LOCK = """
import os, sys
from pgpcbot import SQLiteStorage
store = SQLiteStorage(sys.argv[1])
store.open(int(sys.argv[2]))
print("locked" if store.try_lock() else "refused", os.getpid(), flush=True)
sys.stdin.read()
"""


def _run_python(script: str, *args: str) -> list[str]:
    done = subprocess.run(
        [sys.executable, "-c", script, *args], capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines()


def _lock_within(store: SQLiteStorage, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not store.try_lock():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def test_only_one_storage_on_a_file_holds_the_lock(db_path: Path) -> None:
    first, second = SQLiteStorage(db_path), SQLiteStorage(db_path)
    first.open(BOT_ID)
    second.open(BOT_ID)
    try:
        assert first.try_lock() is True
        assert first.try_lock() is True  # holding it already is not a conflict
        assert first.lock_holder() == str(os.getpid())

        assert second.try_lock() is False
        with pytest.raises(PollingConflictError, match=rf"pid {os.getpid()}\b") as caught:
            second.require_lock()
        assert str(db_path) in str(caught.value)
    finally:
        first.close()
        second.close()


def test_another_process_cannot_take_the_lock(db_path: Path) -> None:
    holder = SQLiteStorage(db_path)
    holder.open(BOT_ID)
    assert holder.try_lock()
    try:
        lines = _run_python(_TRY_LOCK, str(db_path), str(BOT_ID))
    finally:
        holder.close()

    assert lines[0] == "False"
    assert lines[1].startswith("conflict: another process")
    assert f"(pid {os.getpid()})" in lines[1]


def test_close_releases_the_lock(db_path: Path) -> None:
    first, second = SQLiteStorage(db_path), SQLiteStorage(db_path)
    first.open(BOT_ID)
    assert first.try_lock()
    first.close()
    second.open(BOT_ID)
    try:
        assert second.try_lock() is True
        assert _run_python(_TRY_LOCK, str(db_path), str(BOT_ID))[0] == "False"
    finally:
        second.close()


def test_unlock_releases_the_lock_and_keeps_the_storage_open(db_path: Path) -> None:
    first, second = SQLiteStorage(db_path), SQLiteStorage(db_path)
    first.open(BOT_ID)
    second.open(BOT_ID)
    try:
        assert first.try_lock()
        first.unlock()
        first.unlock()

        assert second.try_lock() is True
        assert first.try_lock() is False
        assert ids(first.accept([upd(1)])) == [1]
    finally:
        first.close()
        second.close()


def test_the_lock_goes_away_with_the_process_that_held_it(db_path: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(db_path), str(BOT_ID)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    store = SQLiteStorage(db_path)
    store.open(BOT_ID)
    try:
        assert proc.stdout is not None
        state, pid = proc.stdout.readline().split()
        assert state == "locked"
        assert store.try_lock() is False
        # not proc.pid: on windows a venv's python.exe is a launcher that runs the
        # real interpreter as a child, and the child is the one holding the lock
        assert store.lock_holder() == pid

        # no unlock, no close: the process just dies
        proc.kill()
        proc.wait(timeout=30)

        assert _lock_within(store, 5.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        store.close()


def test_two_bots_on_one_file_each_take_their_own_lock(db_path: Path) -> None:
    first, second = SQLiteStorage(db_path), SQLiteStorage(db_path)
    first.open(BOT_ID)
    second.open(OTHER_BOT)
    try:
        assert first.try_lock() is True
        assert second.try_lock() is True
        assert _run_python(_TRY_LOCK, str(db_path), str(BOT_ID))[0] == "False"
        assert _run_python(_TRY_LOCK, str(db_path), str(OTHER_BOT))[0] == "False"
    finally:
        first.close()
        second.close()


def test_the_lock_needs_the_storage_open_first(db_path: Path) -> None:
    store = SQLiteStorage(db_path)
    with pytest.raises(StorageError, match="not open"):
        store.try_lock()
    assert store.lock_holder() is None


def test_begin_refuses_an_update_that_is_no_longer_pending(storage: Storage) -> None:
    storage.accept([upd(1), upd(2)])
    storage.finish(1)
    storage.begin(2)
    storage.fail(2, "boom")

    # read from the inbox before it finished: it must not run a second time
    assert storage.begin(1) == 0
    assert storage.begin(2) == 0
    assert storage.begin(99) == 0


def test_pending_ids_and_load_skip_the_payloads_they_are_not_asked_for(storage: Storage) -> None:
    storage.accept([upd(3), upd(1), upd(2)])
    storage.finish(2)

    assert storage.pending_ids() == [1, 3]
    assert ids(storage.load([3, 2, 1])) == [1, 3]
    assert storage.load([]) == []


def test_the_failed_list_keeps_only_the_newest(tmp_path: Path) -> None:
    for store in (MemoryStorage(max_failed=2), SQLiteStorage(tmp_path / "f.db", max_failed=2)):
        store.open(BOT_ID)
        store.accept([upd(1), upd(2), upd(3)])
        for uid in (1, 2, 3):
            store.fail(uid, "boom")
        assert [f.update_id for f in store.failed()] == [2, 3]
        store.close()
