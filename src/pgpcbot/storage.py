"""Where a bot keeps its updates between the server and the handlers.

The server delivers each update at least once and in order, and deletes it for good once the bot
confirms it. The storage is the bot's side of that exchange. It holds:

- the highest ``updateId`` accepted, so that a repeated delivery is recognised and skipped;
- the **pending** updates: accepted, and not yet through the handlers;
- the **failed** updates: their handlers raised and no error handler took the error. They stay
  here, with the error, until you retry or discard them, but the list keeps only the newest
  ones;
- the **pins**: the first key fingerprint seen for each person when the bot has
  ``pin_keys=True``, or the one you accepted with `~pgpcbot.Bot.trust_key`.

`~pgpcbot.MemoryStorage` keeps all of it in memory and is the default.
`~pgpcbot.SQLiteStorage` keeps it in a file, so it survives restarts and crashes: use it for a bot
that runs for real. The default writes nothing to disk on purpose: updates from readable chats
are plain text.

To keep updates somewhere else, subclass `~pgpcbot.Storage`.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from .errors import PollingConflictError, StorageError

__all__ = ["FailedUpdate", "MemoryStorage", "SQLiteStorage", "Storage"]

log = logging.getLogger("pgpcbot.storage")


@dataclass(frozen=True, slots=True)
class FailedUpdate:
    """An update whose handlers failed, kept with the error.

    `Bot.failed_updates` returns them, oldest first. A storage of your own builds them in its
    `Storage.failed`: the fields are also the constructor's arguments, in this order.

    Attributes:
        update_id: The update's ``updateId``.
        payload: The update as the server sent it. From an encrypted chat it holds the armored
            message: nothing decrypted is kept.
        error: What went wrong: the exception's type and message, and the file and line where it
            was raised, such as ``"KeyError: 'city' at bot.py:31"``. For an update from an
            encrypted chat the message is left out, because a handler's exception could quote
            the decrypted text; the messages of pgpcbot's own exceptions are kept. An update set
            aside because the process kept stopping while it ran says ``"gave up: ..."``.
        failed_at: When it failed, in UTC.
    """

    update_id: int
    payload: Mapping[str, Any]
    error: str
    failed_at: datetime


def _update_id(update: Mapping[str, Any]) -> int:
    value = update.get("updateId")
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError(f"an update without a valid updateId: {value!r}")
    return value


class Storage(ABC):
    """The interface every storage implements: subclass it to keep updates somewhere else.

    The bot hands each update it receives to its storage before a handler sees it, and asks the
    storage what is left to do at every start. `MemoryStorage` and `SQLiteStorage` implement
    it. To keep updates in PostgreSQL or Redis, set `durable` and write the abstract methods
    below: each one says what it must do and when the bot calls it. `count_pending`,
    `pending_ids` and `load` have defaults built on `pending`; override them when loading every
    pending update at once is slow.

    Updates are plain mappings of the server's JSON. The key that matters here is ``updateId``,
    a number that grows with each new update of the bot.

    Rules for every method:

    - It is synchronous and thread safe. The bot calls it from its event loop and from worker
      threads, sometimes at the same time. With `durable` ``False`` the calls for each update
      run on the event loop itself, so they must be quick and must not block.
    - It works on the bot given to `open` only. One store may hold several bots, kept apart by
      their ID.
    - It raises `StorageError` when the store fails. The bot recovers where it can: a batch that
      `accept` could not store is fetched again by long polling, or answered ``503`` on a
      webhook so that the server sends it again, and an update whose run could not be counted
      or recorded stays pending and runs again later.

    When the bot starts, it calls `open`, `try_lock` and, for long polling, `accepted_max`.
    While it runs, it calls `accept`, `pending_ids`, `load`, `begin`, and `finish` or `fail`.
    When it stops, it calls `unlock` and `close`. The other methods serve `Bot` methods such as
    `Bot.failed_updates`, and key pinning.

    Attributes:
        durable: ``True`` only if `accept` has saved the updates for good when it returns. The bot
            then confirms them to the server right away, before their handlers run, and makes
            the calls for each update from worker threads. With ``False``, the default, it
            confirms an update only after its handlers finished, as with `MemoryStorage`. Set it
            on your class.

    Example:
        A storage of your own, given to the bot like the built-in ones::

            from pgpcbot import Bot, Storage


            class PostgresStorage(Storage):
                durable = True  # accept() commits before it returns

                def open(self, bot_id):
                    ...

                # ... and every other abstract method


            bot = Bot(storage=PostgresStorage())
    """

    durable: bool = False

    @abstractmethod
    def open(self, bot_id: int) -> None:
        """Prepares the storage for one bot.

        Called at every start, before the bot uses the storage for anything else. Everything
        after it (the high-water mark, the pending and failed updates, the pins, the lock)
        belongs to this bot only: a store shared by several bots keeps them apart by this ID.
        After `close`, the bot may call it again when it starts again.

        `Bot.failed_updates`, `Bot.retry_failed`, `Bot.discard_failed` and `Bot.trust_key`
        call the storage directly, so on a bot that is not running they reach a storage that is
        not open. `SQLiteStorage` then raises `StorageError`.

        Args:
            bot_id: The bot's user ID, read from its token.

        Raises:
            StorageError: The store can't be opened. The bot does not start.
        """

    @abstractmethod
    def close(self) -> None:
        """Releases what the storage holds: connections, open files, the lock.

        Called when the bot stops, after `unlock`. Its errors are ignored.
        """
        ...

    @abstractmethod
    def try_lock(self) -> bool:
        """Takes the right to run this bot's handlers, if nobody else holds it.

        Only one process at a time may run a bot's handlers, and this is how the bot makes sure.
        Called at start, after `open`. Long polling does not start without the right: the bot
        raises `PollingConflictError`. A webhook process that does not get it keeps accepting
        deliveries (with a durable storage it only stores them, for the process that holds the
        right) and asks again every 5 seconds.

        The right must end by itself when the process that holds it dies, even in a crash, or
        the bot could never start again after one. An operating system file lock does that.

        Returns:
            ``True`` if the caller holds the right now, ``False`` if someone else has it.
        """

    @abstractmethod
    def unlock(self) -> None:
        """Gives back the right taken by `try_lock`.

        Called when the bot stops, before `close`, even if the right was never taken: then it
        must do nothing. Its errors are ignored.
        """
        ...

    @abstractmethod
    def accepted_max(self) -> int | None:
        """Reads the high-water mark: the highest ``updateId`` ever accepted for this bot.

        The mark never goes down, not even when the updates are finished, failed or discarded.
        Long polling reads it at start: with a durable storage, its first request confirms to
        the server every update up to the mark.

        Returns:
            The highest ``updateId`` that `accept` stored, or ``None`` before the first one.
        """

    @abstractmethod
    def accept(self, updates: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """Stores the updates it has not seen before and returns them, oldest first.

        Called with every batch of a long poll and every webhook delivery. An update is new
        when its ``updateId`` is above `accepted_max`; anything at or below the mark is a repeat
        and is skipped, as is a second copy of an ID within the batch. The batch can come in any
        order.

        It must be atomic: either every new update is stored and the mark moves to the highest
        of them, or nothing changes and it raises. With `Storage.durable` ``True``, the updates
        must be saved for good when it returns, because the bot tells the server it has them
        right after.

        Only ``updateId`` is sure to be there. With ``skip_pending=True`` the bot stores
        ``{"updateId": n, "kind": "skipped"}`` for each update it throws away, and finishes
        them at once, so that a late repeat is still recognised.

        Args:
            updates: The updates, as the server sent them.

        Returns:
            The new updates, sorted by ``updateId``. These are the ones the handlers will see.

        Raises:
            StorageError: The updates could not be stored, or one has no integer ``updateId``.
                A long poll then does not confirm the batch and fetches it again; a webhook
                answers ``503``. Any other exception stops a polling bot.
        """

    @abstractmethod
    def pending(self) -> list[Mapping[str, Any]]:
        """Lists the pending updates: accepted, and neither finished nor failed.

        The bot does not call it directly: the defaults of `pending_ids`, `load` and
        `count_pending` build on it.

        Returns:
            The pending updates, oldest first, as `accept` stored them.
        """

    @abstractmethod
    def begin(self, update_id: int) -> int:
        """Counts one more handler run of a pending update.

        Called right before the handlers of an update run. The count is how the bot spots an
        update that kills the process every time: after ``max_handler_runs`` runs that never
        finished, it moves the update to the failed list instead of running it again. A durable
        storage must keep the count with the update, so that it survives a crash. After
        `requeue_failed` the count starts again from zero.

        Args:
            update_id: The update about to run.

        Returns:
            The number of runs, this one included, so ``1`` the first time. ``0`` when the
            update is not pending any more, because it finished or failed in the meantime; the
            bot then does not run it.
        """

    @abstractmethod
    def finish(self, update_id: int) -> None:
        """Forgets a pending update: its handlers are done.

        Called when the handlers return, or when an error handler took their error. An ID that
        is not pending must be ignored. If it raises, the update stays pending and runs again
        later: the bot handles updates at least once.

        Args:
            update_id: The update that is done.
        """
        ...

    @abstractmethod
    def fail(self, update_id: int, error: str) -> None:
        """Moves a pending update to the failed list, with the error.

        Called when the handlers raised and no error handler took the error, and when an update
        ran ``max_handler_runs`` times without ever finishing (see `begin`). The update leaves
        the pending list, and `failed` returns it with the error and the time. An ID that is
        not pending must be ignored. Anyone who can write to the bot can make updates fail, so
        keep the list bounded: the built-in storages keep the newest ``max_failed``.

        Args:
            update_id: The update that failed.
            error: The text described in `FailedUpdate.error`.
        """

    @abstractmethod
    def failed(self, limit: int = 100) -> list[FailedUpdate]:
        """Lists the failed updates, oldest first.

        `Bot.failed_updates` calls it.

        Args:
            limit: The most to return.

        Returns:
            Up to ``limit`` failed updates, in ``updateId`` order.
        """
        ...

    @abstractmethod
    def requeue_failed(self, update_ids: Sequence[int] | None = None) -> int:
        """Moves failed updates back to the pending list, to run again.

        `Bot.retry_failed` calls it, and a running bot then picks them up at once; otherwise
        they run at the next start. Their run count starts again from zero.

        Args:
            update_ids: The updates to move. ``None`` moves all of them; an ID that is not in
                the failed list is skipped.

        Returns:
            How many updates were moved.
        """

    @abstractmethod
    def discard_failed(self, update_ids: Sequence[int] | None = None) -> int:
        """Deletes failed updates for good.

        `Bot.discard_failed` calls it.

        Args:
            update_ids: The updates to delete. ``None`` deletes all of them; an ID that is not
                in the failed list is skipped.

        Returns:
            How many updates were deleted.
        """
        ...

    @abstractmethod
    def get_pin(self, user_id: int) -> str | None:
        """Reads the key fingerprint pinned for a person.

        With ``pin_keys=True`` the bot calls it each time it checks a person's key: before it
        seals a message to them, and when it checks the signature of a message they sent.

        Args:
            user_id: The person's user ID.

        Returns:
            The pinned fingerprint, or ``None`` if none is pinned yet.
        """
        ...

    @abstractmethod
    def set_pin(self, user_id: int, fingerprint: str) -> None:
        """Pins a key fingerprint for a person, replacing any earlier pin.

        Called with ``pin_keys=True`` the first time the bot sees a person's key, and by
        `Bot.trust_key`. The built-in storages keep it in upper case; the bot compares
        fingerprints without regard to case.

        Args:
            user_id: The person's user ID.
            fingerprint: The key's fingerprint, in hexadecimal: 40 characters for a v4 key, 64
                for a v6 key.
        """
        ...

    def count_pending(self) -> int:
        """Counts the pending updates.

        The bot itself does not need it: it is there for your own checks, such as a report at
        start.

        Returns:
            How many updates are accepted and neither finished nor failed.
        """
        return len(self.pending())

    def pending_ids(self) -> list[int]:
        """Lists the IDs of the pending updates, oldest first.

        The bot calls it when it looks for work: at start if it got the right from `try_lock`,
        or later when it gets it; after `requeue_failed`; and, in a webhook process with a
        durable storage, every 2 seconds while it holds the right, to pick up what other worker
        processes stored.

        Returns:
            The ``updateId`` of each pending update, in ascending order.
        """
        return [_update_id(update) for update in self.pending()]

    def load(self, update_ids: Sequence[int]) -> list[Mapping[str, Any]]:
        """Reads the pending updates with the given IDs.

        Called right after `pending_ids`, with the IDs that are not running yet, as many as the
        bot has room for.

        Args:
            update_ids: The IDs to read. An ID that is not pending any more is left out.

        Returns:
            The pending updates among ``update_ids``, oldest first.
        """
        wanted = set(update_ids)
        return [update for update in self.pending() if _update_id(update) in wanted]


class MemoryStorage(Storage):
    """Keeps the updates in memory: the default storage.

    Everything is lost when the process ends, so `Storage.durable` is ``False``: the bot
    confirms an update to the server only once its handlers have finished. A crash means the
    server sends some updates again, never that one is lost. The price is speed: long polling
    waits for a whole batch to be handled before it asks for more, and a webhook handles one
    update at a time, because the server waits for each answer.

    It sees only its own process. Two processes that poll the same bot are not detected (the
    bot only logs a warning when polls keep coming back empty), and the pins last only as long
    as the process. A bot stopped and started again in the same process finds its pending and
    failed updates and its pins where it left them.

    Attributes:
        max_failed (int): How many failed updates the list keeps. When one more fails, the one
            with the lowest ``updateId`` is dropped.
    """

    durable = False

    def __init__(self, *, max_failed: int = 1000) -> None:
        """Sets up an empty storage.

        Args:
            max_failed: How many failed updates to keep, 1000 by default.
        """
        self.max_failed = max_failed
        self._lock = threading.Lock()
        self._bot_id: int | None = None
        self._max: int | None = None
        self._inbox: dict[int, Mapping[str, Any]] = {}
        self._runs: dict[int, int] = {}
        self._failed: dict[int, FailedUpdate] = {}
        self._pins: dict[int, str] = {}
        self._held = False

    def open(self, bot_id: int) -> None:
        """Binds the storage to a bot.

        For the bot it was opened for before, it keeps everything it holds. For another bot it
        starts again from nothing, rather than mix two queues.

        Args:
            bot_id: The bot's user ID, read from its token.
        """
        with self._lock:
            if self._bot_id is not None and self._bot_id != bot_id:
                # a different bot: start from nothing rather than mix two queues
                self._max, self._inbox, self._runs, self._failed, self._pins = None, {}, {}, {}, {}
            self._bot_id = bot_id

    def close(self) -> None:
        """Does nothing: the data stays in memory, for the next `open` in this process."""

    def try_lock(self) -> bool:
        """Takes the right to run the bot's handlers, if no bot that uses this object holds it.

        The right lives in this object, so another process, or another `MemoryStorage`, can't
        see it. While it is taken the answer is ``False``, even to the bot that took it.

        Returns:
            ``True`` if the right was free and is now taken, ``False`` if it was taken already.
        """
        with self._lock:
            if self._held:
                return False
            self._held = True
            return True

    def unlock(self) -> None:
        """Frees the right taken by `try_lock`, whoever took it.

        The right is not tied to a bot: a bot that never got it frees it too when it stops.
        """
        with self._lock:
            self._held = False

    def accepted_max(self) -> int | None:
        """Reads the high-water mark, as `Storage.accepted_max` describes.

        Returns:
            The highest ``updateId`` accepted, or ``None`` before the first one.
        """
        with self._lock:
            return self._max

    def accept(self, updates: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """Keeps the updates it has not seen before, as `Storage.accept` describes.

        Args:
            updates: The updates, as the server sent them.

        Returns:
            The new updates, sorted by ``updateId``.

        Raises:
            StorageError: An update has no integer ``updateId``. Nothing is kept.
        """
        with self._lock:
            fresh = []
            top = self._max
            for update in sorted(updates, key=_update_id):
                uid = _update_id(update)
                if top is not None and uid <= top:
                    continue
                self._inbox[uid] = update
                fresh.append(update)
                top = uid
            self._max = top
            return fresh

    def pending(self) -> list[Mapping[str, Any]]:
        """Lists the pending updates: accepted, and neither finished nor failed.

        Returns:
            The pending updates, oldest first.
        """
        with self._lock:
            return [self._inbox[k] for k in sorted(self._inbox)]

    def begin(self, update_id: int) -> int:
        """Counts one more handler run of a pending update, as `Storage.begin` describes.

        Args:
            update_id: The update about to run.

        Returns:
            The number of runs, this one included, or ``0`` when the update is not pending.
        """
        with self._lock:
            if update_id not in self._inbox:
                return 0
            self._runs[update_id] = self._runs.get(update_id, 0) + 1
            return self._runs[update_id]

    def finish(self, update_id: int) -> None:
        """Forgets a pending update: its handlers are done.

        Args:
            update_id: The update that is done.
        """
        with self._lock:
            self._inbox.pop(update_id, None)
            self._runs.pop(update_id, None)

    def fail(self, update_id: int, error: str) -> None:
        """Moves a pending update to the failed list, with the error and the time.

        Then, if the list holds more than `max_failed` updates, the oldest are dropped.

        Args:
            update_id: The update that failed.
            error: The text described in `FailedUpdate.error`.
        """
        with self._lock:
            payload = self._inbox.pop(update_id, None)
            self._runs.pop(update_id, None)
            if payload is not None:
                self._failed[update_id] = FailedUpdate(update_id, payload, error, datetime.now(UTC))
                # anyone can send what makes a handler fail: keep the newest, not all of it
                while len(self._failed) > self.max_failed:
                    del self._failed[min(self._failed)]

    def failed(self, limit: int = 100) -> list[FailedUpdate]:
        """Lists the failed updates, oldest first.

        Args:
            limit: The most to return.

        Returns:
            Up to ``limit`` failed updates, in ``updateId`` order.
        """
        with self._lock:
            return [self._failed[k] for k in sorted(self._failed)][:limit]

    def requeue_failed(self, update_ids: Sequence[int] | None = None) -> int:
        """Moves failed updates back to the pending list, to run again.

        Args:
            update_ids: The updates to move. ``None`` moves all of them; an ID that is not in
                the failed list is skipped.

        Returns:
            How many updates were moved.
        """
        with self._lock:
            ids = list(self._failed) if update_ids is None else [i for i in update_ids if i in self._failed]
            for uid in ids:
                self._inbox[uid] = self._failed.pop(uid).payload
            return len(ids)

    def discard_failed(self, update_ids: Sequence[int] | None = None) -> int:
        """Deletes failed updates.

        Args:
            update_ids: The updates to delete. ``None`` deletes all of them; an ID that is not
                in the failed list is skipped.

        Returns:
            How many updates were deleted.
        """
        with self._lock:
            ids = list(self._failed) if update_ids is None else [i for i in update_ids if i in self._failed]
            for uid in ids:
                del self._failed[uid]
            return len(ids)

    def get_pin(self, user_id: int) -> str | None:
        """Reads the key fingerprint pinned for a person.

        Args:
            user_id: The person's user ID.

        Returns:
            The pinned fingerprint, or ``None`` if none is pinned yet.
        """
        with self._lock:
            return self._pins.get(user_id)

    def set_pin(self, user_id: int, fingerprint: str) -> None:
        """Pins a key fingerprint for a person, in upper case, replacing any earlier pin.

        Args:
            user_id: The person's user ID.
            fingerprint: The key's fingerprint, in hexadecimal.
        """
        with self._lock:
            self._pins[user_id] = fingerprint.upper()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    bot_id INTEGER PRIMARY KEY,
    accepted_max INTEGER
);
CREATE TABLE IF NOT EXISTS inbox (
    bot_id INTEGER NOT NULL,
    update_id INTEGER NOT NULL,
    payload TEXT NOT NULL,
    received_at REAL NOT NULL,
    runs INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bot_id, update_id)
);
CREATE TABLE IF NOT EXISTS failed (
    bot_id INTEGER NOT NULL,
    update_id INTEGER NOT NULL,
    payload TEXT NOT NULL,
    error TEXT NOT NULL,
    failed_at REAL NOT NULL,
    PRIMARY KEY (bot_id, update_id)
);
CREATE TABLE IF NOT EXISTS pins (
    bot_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    pinned_at REAL NOT NULL,
    PRIMARY KEY (bot_id, user_id)
);
"""


def _warn_if_readable(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        log.warning(
            "%s can be read by other users (mode %o) and may hold messages of readable chats in clear: chmod 600 it",
            path,
            mode & 0o777,
        )


class _FileLock:
    """An OS level lock on a side file. The OS drops it when the process dies, crash or not."""

    # windows locks are mandatory: lock a byte well past the pid so others can still read it
    _WIN_OFFSET = 1 << 16

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: IO[bytes] | None = None

    def acquire(self) -> bool:
        if self._file is not None:
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        handle = os.fdopen(os.open(self._path, flags, 0o600), "r+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(self._WIN_OFFSET)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n".encode())
            handle.flush()
        except OSError:
            pass  # the lock is what counts, the pid is just for whoever looks
        self._file = handle
        return True

    def release(self) -> None:
        handle, self._file = self._file, None
        if handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(self._WIN_OFFSET)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()

    def holder(self) -> str | None:
        try:
            return self._path.read_text(encoding="ascii").strip() or None
        except OSError:
            return None


class SQLiteStorage(Storage):
    """Keeps the updates in a SQLite database file, so they survive restarts and crashes.

    `Storage.durable` is ``True``: the bot confirms an update to the server as soon as it is
    written here, without waiting for its handlers. A crash before they finish loses nothing:
    the update is still in the file and runs at the next start. ``Bot(storage="bot.db")`` is the same as
    ``Bot(storage=SQLiteStorage("bot.db"))``.

    How it works:

    - The database is in WAL mode, with ``synchronous=FULL`` by default: an update is confirmed
      to the server as soon as it is written, so a write that a power cut could undo would be an
      update lost.
    - Writes of several statements run in one ``BEGIN IMMEDIATE`` transaction. Worker processes
      that share the file wait for each other there, so two of them never accept the same update.
    - An operating system lock on a file next to the database, ``<path>.<bot id>.lock`` (for
      example ``bot.db.42.lock``), lets one process at a time run a bot's handlers. The system
      releases it when the process dies, even in a crash. A second process that polls the same
      bot gets `PollingConflictError`; webhook workers share the file, and the one that holds
      the lock runs the handlers.
    - One file can hold several bots: each bot's rows are kept apart by its ID, and each bot has
      its own lock file.
    - The failed list keeps the newest `max_failed` updates per bot, each error cut to 4000
      characters.
    - The methods that read or write updates and pins raise `StorageError` while the storage is
      not open: before `open`, or after `close`.

    On Linux and macOS the database and the lock file are created readable by their owner only
    (``0600``), and opening an existing database that other users can read logs a warning. Keep
    the file on a local disk: SQLite locks are not reliable on network shares (NFS, SMB).

    Attributes:
        path (pathlib.Path): The database file.
        max_failed (int): How many failed updates the list keeps per bot. When one more fails,
            the one with the lowest ``updateId`` is dropped.

    Warning:
        Updates are stored as the server sent them. From encrypted chats they are armored
        OpenPGP messages, and nothing decrypted is ever written; from readable chats they are
        plain text. Protect the file and its backups like the messages it holds. To copy it
        while the bot runs, use SQLite's own backup (``sqlite3 bot.db ".backup copy.db"``).
    """

    durable = True

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        synchronous: str = "FULL",
        timeout: float = 10.0,
        max_failed: int = 10_000,
    ) -> None:
        """Sets up the storage. Nothing touches the file until the bot starts and calls `open`.

        Args:
            path: The database file. It is created, with its folder, if it does not exist.
            synchronous: SQLite's ``synchronous`` setting: ``"FULL"`` (the default),
                ``"EXTRA"`` or ``"NORMAL"``, in upper or lower case. Keep ``"FULL"``: with
                ``"NORMAL"`` a power cut can lose the last update the server was told is safe.
            timeout: How many seconds to wait when another process holds the database's write
                lock.
            max_failed: How many failed updates to keep per bot, 10 000 by default.

        Raises:
            ValueError: ``synchronous`` is not one of the three values.
        """
        if synchronous.upper() not in ("FULL", "NORMAL", "EXTRA"):
            raise ValueError("synchronous must be FULL, EXTRA or NORMAL")
        self.path = Path(path)
        self.max_failed = max_failed
        self._synchronous = synchronous.upper()
        self._timeout = timeout
        self._db: sqlite3.Connection | None = None
        self._bot_id: int | None = None
        self._mutex = threading.RLock()
        self._file_lock: _FileLock | None = None

    def __repr__(self) -> str:
        return f"SQLiteStorage({str(self.path)!r})"

    # --- connection ---

    def open(self, bot_id: int) -> None:
        """Opens the database file for a bot, and creates it if it does not exist.

        A new file is created with its folder, readable by its owner only on Linux and macOS.
        Opening an existing file that other users can read logs a warning. The connection uses
        WAL mode and the ``synchronous`` setting given to the constructor. Opened again for
        another bot, the storage releases the first bot's lock and works on the new bot's rows.

        Args:
            bot_id: The bot's user ID, read from its token.

        Raises:
            StorageError: The file can't be opened as a SQLite database.
            OSError: The file or its folder can't be created.
        """
        with self._mutex:
            if self._bot_id != bot_id:
                if self._file_lock is not None:
                    self._file_lock.release()
                # one lock per bot: two bots can share a file, each with its own processor
                self._file_lock = _FileLock(self.path.with_name(f"{self.path.name}.{bot_id}.lock"))
            self._bot_id = bot_id
            if self._db is not None:
                return
            fresh = not self.path.exists()
            if fresh:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # create it private before sqlite does, sqlite would use the umask
                fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
                os.close(fd)
            else:
                _warn_if_readable(self.path)
            db: sqlite3.Connection | None = None
            try:
                db = sqlite3.connect(self.path, timeout=self._timeout, check_same_thread=False, isolation_level=None)
                db.execute("PRAGMA journal_mode=WAL")
                db.execute(f"PRAGMA synchronous={self._synchronous}")
                db.execute(f"PRAGMA busy_timeout={int(self._timeout * 1000)}")
                db.executescript(_SCHEMA)
            except sqlite3.Error as exc:
                # a file that isn't a database fails on the first pragma: don't leave the handle open
                if db is not None:
                    db.close()
                raise StorageError(f"cannot open {self.path}: {exc}") from exc
            self._db = db

    def close(self) -> None:
        """Releases the lock and closes the database. `open` can open it again later."""
        with self._mutex:
            self.unlock()
            if self._db is not None:
                try:
                    self._db.close()
                finally:
                    self._db = None

    def _conn(self) -> tuple[sqlite3.Connection, int]:
        if self._db is None or self._bot_id is None:
            raise StorageError("the storage is not open: Bot opens it when it starts")
        return self._db, self._bot_id

    def _write(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        db, _ = self._conn()
        try:
            return db.execute(sql, params)
        except sqlite3.Error as exc:
            raise StorageError(str(exc)) from exc

    # --- the lock ---

    def try_lock(self) -> bool:
        """Takes the operating system lock on ``<path>.<bot id>.lock``.

        The lock file is created next to the database if needed, and the process ID is written
        in it for `lock_holder`. Asking again while holding the lock returns ``True``. Two
        `SQLiteStorage` objects on the same file conflict even within one process.

        Returns:
            ``True`` if this storage holds the lock now, ``False`` if someone else holds it.

        Raises:
            StorageError: The storage was never opened.
        """
        with self._mutex:
            if self._file_lock is None:
                raise StorageError("the storage is not open: Bot opens it when it starts")
            return self._file_lock.acquire()

    def unlock(self) -> None:
        """Releases the file lock, if this storage holds it. The storage stays open."""
        with self._mutex:
            if self._file_lock is not None:
                self._file_lock.release()

    def lock_holder(self) -> str | None:
        """Reads the process ID written in the lock file, for error messages.

        A process writes its ID there when it takes the lock, and the ID stays after the lock
        is released: it names the current holder only while the lock is taken.

        Returns:
            The process ID as text, or ``None`` before `open` or when the file is empty or
            can't be read.
        """
        return self._file_lock.holder() if self._file_lock is not None else None

    def require_lock(self) -> None:
        """Takes the lock, or raises an error that says which process has it.

        The bot calls it when it starts long polling with this storage.

        Raises:
            PollingConflictError: Another process, or another `SQLiteStorage` on the same file,
                holds the lock. The message names the database file and, when it is known, the
                process ID.
            StorageError: The storage was never opened.
        """
        if not self.try_lock():
            holder = self.lock_holder()
            raise PollingConflictError(
                f"another process{f' (pid {holder})' if holder else ''} is already running this bot "
                f"on {self.path}; two pollers would starve each other"
            )

    # --- updates ---

    def accepted_max(self) -> int | None:
        """Reads the high-water mark kept in the file, as `Storage.accepted_max` describes.

        Returns:
            The highest ``updateId`` accepted, or ``None`` before the first one.
        """
        with self._mutex:
            db, bot = self._conn()
            row = db.execute("SELECT accepted_max FROM state WHERE bot_id = ?", (bot,)).fetchone()
            return row[0] if row else None

    def accept(self, updates: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """Writes the updates it has not seen before, in one ``BEGIN IMMEDIATE`` transaction.

        Worker processes that share the file wait for each other here, so two of them never
        accept the same update. The rest is as `Storage.accept` describes.

        Args:
            updates: The updates, as the server sent them.

        Returns:
            The new updates, sorted by ``updateId``.

        Raises:
            StorageError: SQLite could not write them, one has no integer ``updateId``, or the
                storage is not open. Nothing is written.
        """
        if not updates:
            return []
        with self._mutex:
            db, bot = self._conn()
            try:
                # IMMEDIATE: two processes accepting at once must not both read the old mark
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT accepted_max FROM state WHERE bot_id = ?", (bot,)).fetchone()
                top = row[0] if row else None
                fresh: list[Mapping[str, Any]] = []
                now = time.time()
                for update in sorted(updates, key=_update_id):
                    uid = _update_id(update)
                    if top is not None and uid <= top:
                        continue
                    db.execute(
                        "INSERT OR IGNORE INTO inbox (bot_id, update_id, payload, received_at) VALUES (?, ?, ?, ?)",
                        (bot, uid, json.dumps(update, ensure_ascii=False, separators=(",", ":")), now),
                    )
                    fresh.append(update)
                    top = uid
                if fresh:
                    db.execute(
                        "INSERT INTO state (bot_id, accepted_max) VALUES (?, ?) "
                        "ON CONFLICT(bot_id) DO UPDATE SET accepted_max = excluded.accepted_max",
                        (bot, top),
                    )
                db.execute("COMMIT")
                return fresh
            except BaseException as exc:
                try:
                    db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                if isinstance(exc, sqlite3.Error):
                    raise StorageError(str(exc)) from exc
                raise

    def pending(self) -> list[Mapping[str, Any]]:
        """Lists the pending updates: accepted, and neither finished nor failed.

        Returns:
            The pending updates, oldest first.
        """
        with self._mutex:
            db, bot = self._conn()
            rows = db.execute("SELECT payload FROM inbox WHERE bot_id = ? ORDER BY update_id", (bot,)).fetchall()
        out = []
        for (payload,) in rows:
            try:
                out.append(json.loads(payload))
            except ValueError:
                continue  # can't happen unless somebody edited the file by hand
        return out

    def count_pending(self) -> int:
        """Counts the pending updates with one query, without reading them.

        Returns:
            How many updates are accepted and neither finished nor failed.
        """
        with self._mutex:
            db, bot = self._conn()
            return int(db.execute("SELECT COUNT(*) FROM inbox WHERE bot_id = ?", (bot,)).fetchone()[0])

    def pending_ids(self) -> list[int]:
        """Lists the IDs of the pending updates, oldest first, without reading the updates.

        Returns:
            The ``updateId`` of each pending update, in ascending order.
        """
        with self._mutex:
            db, bot = self._conn()
            rows = db.execute("SELECT update_id FROM inbox WHERE bot_id = ? ORDER BY update_id", (bot,)).fetchall()
        return [int(uid) for (uid,) in rows]

    def load(self, update_ids: Sequence[int]) -> list[Mapping[str, Any]]:
        """Reads the pending updates with the given IDs.

        Args:
            update_ids: The IDs to read. An ID that is not pending any more is left out.

        Returns:
            The pending updates among ``update_ids``, oldest first.
        """
        if not update_ids:
            return []
        out: list[Mapping[str, Any]] = []
        with self._mutex:
            db, bot = self._conn()
            # in slices: sqlite caps the number of ? in one statement
            for start in range(0, len(update_ids), 500):
                chunk = list(update_ids[start : start + 500])
                marks = ",".join("?" * len(chunk))
                rows = db.execute(
                    f"SELECT payload FROM inbox WHERE bot_id = ? AND update_id IN ({marks}) ORDER BY update_id",  # noqa: S608 - only ? placeholders
                    [bot, *chunk],
                ).fetchall()
                for (payload,) in rows:
                    try:
                        out.append(json.loads(payload))
                    except ValueError:
                        continue
        out.sort(key=_update_id)
        return out

    def begin(self, update_id: int) -> int:
        """Counts one more handler run of a pending update, in the file.

        The count is kept with the update, so it survives a crash: see `Storage.begin`.

        Args:
            update_id: The update about to run.

        Returns:
            The number of runs, this one included, or ``0`` when the update is not pending.
        """
        with self._mutex:
            _, bot = self._conn()
            cursor = self._write(
                "UPDATE inbox SET runs = runs + 1 WHERE bot_id = ? AND update_id = ?", (bot, update_id)
            )
            if cursor.rowcount == 0:
                # finished (or failed) since it was read from the inbox
                return 0
            db, _ = self._conn()
            row = db.execute("SELECT runs FROM inbox WHERE bot_id = ? AND update_id = ?", (bot, update_id)).fetchone()
            return int(row[0]) if row else 0

    def finish(self, update_id: int) -> None:
        """Deletes a pending update: its handlers are done.

        Args:
            update_id: The update that is done.
        """
        with self._mutex:
            _, bot = self._conn()
            self._write("DELETE FROM inbox WHERE bot_id = ? AND update_id = ?", (bot, update_id))

    def fail(self, update_id: int, error: str) -> None:
        """Moves a pending update to the failed list, with the error cut to 4000 characters.

        In the same transaction, the oldest failed updates beyond `max_failed` are deleted.

        Args:
            update_id: The update that failed.
            error: The text described in `FailedUpdate.error`.

        Raises:
            StorageError: SQLite could not write it. Nothing changes.
        """
        with self._mutex:
            db, bot = self._conn()
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT OR REPLACE INTO failed (bot_id, update_id, payload, error, failed_at) "
                    "SELECT bot_id, update_id, payload, ?, ? FROM inbox WHERE bot_id = ? AND update_id = ?",
                    (error[:4000], time.time(), bot, update_id),
                )
                db.execute("DELETE FROM inbox WHERE bot_id = ? AND update_id = ?", (bot, update_id))
                # anyone can send what makes a handler fail: keep the newest, not all of it
                db.execute(
                    "DELETE FROM failed WHERE bot_id = ? AND update_id NOT IN "
                    "(SELECT update_id FROM failed WHERE bot_id = ? ORDER BY update_id DESC LIMIT ?)",
                    (bot, bot, self.max_failed),
                )
                db.execute("COMMIT")
            except sqlite3.Error as exc:
                try:
                    db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise StorageError(str(exc)) from exc

    def failed(self, limit: int = 100) -> list[FailedUpdate]:
        """Lists the failed updates, oldest first.

        Args:
            limit: The most to return.

        Returns:
            Up to ``limit`` failed updates, in ``updateId`` order.
        """
        with self._mutex:
            db, bot = self._conn()
            rows = db.execute(
                "SELECT update_id, payload, error, failed_at FROM failed WHERE bot_id = ? ORDER BY update_id LIMIT ?",
                (bot, limit),
            ).fetchall()
        return [
            FailedUpdate(uid, json.loads(payload), error, datetime.fromtimestamp(at, UTC))
            for uid, payload, error, at in rows
        ]

    def _move_failed(self, update_ids: Sequence[int] | None, back: bool) -> int:
        with self._mutex:
            db, bot = self._conn()
            where = "bot_id = ?"
            params: list[Any] = [bot]
            if update_ids is not None:
                if not update_ids:
                    return 0
                # only "?" placeholders go into the string, the ids travel as parameters
                where += f" AND update_id IN ({','.join('?' * len(update_ids))})"
                params.extend(update_ids)
            try:
                db.execute("BEGIN IMMEDIATE")
                if back:
                    db.execute(
                        "INSERT OR IGNORE INTO inbox (bot_id, update_id, payload, received_at) "  # noqa: S608 - only ? placeholders are formatted in
                        f"SELECT bot_id, update_id, payload, ? FROM failed WHERE {where}",
                        [time.time(), *params],
                    )
                count = db.execute(f"DELETE FROM failed WHERE {where}", params).rowcount  # noqa: S608
                db.execute("COMMIT")
                return int(count)
            except sqlite3.Error as exc:
                try:
                    db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise StorageError(str(exc)) from exc

    def requeue_failed(self, update_ids: Sequence[int] | None = None) -> int:
        """Moves failed updates back to the pending list, in one transaction.

        Args:
            update_ids: The updates to move. ``None`` moves all of them; an ID that is not in
                the failed list is skipped.

        Returns:
            How many updates were moved.
        """
        return self._move_failed(update_ids, back=True)

    def discard_failed(self, update_ids: Sequence[int] | None = None) -> int:
        """Deletes failed updates from the file.

        Args:
            update_ids: The updates to delete. ``None`` deletes all of them; an ID that is not
                in the failed list is skipped.

        Returns:
            How many updates were deleted.
        """
        return self._move_failed(update_ids, back=False)

    # --- pins ---

    def get_pin(self, user_id: int) -> str | None:
        """Reads the key fingerprint pinned for a person.

        Args:
            user_id: The person's user ID.

        Returns:
            The pinned fingerprint, or ``None`` if none is pinned yet.
        """
        with self._mutex:
            db, bot = self._conn()
            row = db.execute("SELECT fingerprint FROM pins WHERE bot_id = ? AND user_id = ?", (bot, user_id)).fetchone()
            return row[0] if row else None

    def set_pin(self, user_id: int, fingerprint: str) -> None:
        """Pins a key fingerprint for a person, in upper case, replacing any earlier pin.

        Args:
            user_id: The person's user ID.
            fingerprint: The key's fingerprint, in hexadecimal.
        """
        with self._mutex:
            _, bot = self._conn()
            self._write(
                "INSERT INTO pins (bot_id, user_id, fingerprint, pinned_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(bot_id, user_id) DO UPDATE SET fingerprint = excluded.fingerprint, pinned_at = excluded.pinned_at",
                (bot, user_id, fingerprint.upper(), time.time()),
            )
