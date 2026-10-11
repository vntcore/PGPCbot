"""Moves updates from where they come from, through the storage, to the handlers.

Internal: the bot builds one ``Engine`` each time it starts. Two sources feed it, the long poll
(``poll``) and the webhook, and both end in ``Engine.submit``. From there:

- updates of the same chat run one after another, in ``updateId`` order;
- different chats run side by side, at most ``concurrency`` at a time;
- an update is finished only after its handlers return. One whose handlers raise, when no
  error handler takes the error, moves to the storage's failed list with the error: it is
  never dropped;
- an update that keeps killing the process (it was started ``max_runs`` times and never
  finished) moves to the failed list too, so a restart loop can't go on for ever.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
import traceback
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from .errors import (
    APIError,
    AuthenticationError,
    BotPausedError,
    NetworkError,
    PGPCbotError,
    PollingError,
    RateLimitError,
    ServerError,
    StorageError,
    WebhookActiveError,
)
from .storage import Storage

log = logging.getLogger("pgpcbot.engine")

Process = Callable[[Mapping[str, Any]], Awaitable[None]]


def _chat_key(update: Mapping[str, Any]) -> str:
    """The ordering key: the chat the update belongs to."""
    for path in (("message", "chat", "id"), ("callback", "chat", "id"), ("chat", "id")):
        node: Any = update
        for step in path:
            node = node.get(step) if isinstance(node, Mapping) else None
        if isinstance(node, int) and not isinstance(node, bool):
            return f"chat:{node}"
    # nothing to order against: give it a lane of its own
    return f"update:{update.get('updateId')}"


def sealed(update: Mapping[str, Any]) -> bool:
    """Tells whether an update carries an end-to-end encrypted message.

    Args:
        update: The update, as the server sent it.

    Returns:
        ``True`` when its message has an ``armored`` body.
    """
    message = update.get("message")
    return isinstance(message, Mapping) and isinstance(message.get("armored"), str)


def describe(exc: BaseException, *, private: bool = False) -> str:
    """Describes an error for the failed list: its type, its message and where it was raised.

    Args:
        exc: The exception the handlers raised.
        private: The update came from an encrypted chat. A handler's own exception may quote
            what it decrypted (``ValueError(f"bad order {text}")``), and this text goes to disk
            and to the logs, so the message is kept only for pgpcbot's own exceptions, which
            never carry content.

    Returns:
        The text, such as ``"KeyError: 'city' at bot.py:31"``.
    """
    frames = traceback.extract_tb(exc.__traceback__)
    where = f" at {frames[-1].filename.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]}:{frames[-1].lineno}" if frames else ""
    if private and not isinstance(exc, PGPCbotError):
        return f"{type(exc).__name__} (message withheld: the update came from an encrypted chat){where}"
    return f"{type(exc).__name__}: {exc}{where}"


def _frames_of(exc: BaseException) -> str:
    """The traceback without its last line, the one that prints the message."""
    return ("Traceback (most recent call last):\n" + "".join(traceback.format_tb(exc.__traceback__))).rstrip()


class Engine:
    """Schedules updates for the handlers. One per running bot.

    Attributes:
        storage (Storage): Where the updates wait.
        max_pending (int): The number of scheduled updates at which the engine is full. The
            poller then waits for room, and a webhook stores new deliveries or refuses them.
        processed (int): How many updates finished since the engine was made.
        failed (int): How many updates went to the failed list since the engine was made.
    """

    def __init__(
        self,
        storage: Storage,
        process: Process,
        *,
        concurrency: int = 8,
        max_runs: int = 5,
        max_pending: int = 1000,
    ) -> None:
        """Sets up the engine. Nothing runs until updates are submitted or recovered.

        Args:
            storage: The bot's storage.
            process: The coroutine function that runs one update through the handlers.
            concurrency: How many updates may run at once, each in a different chat.
            max_runs: How many runs that never finished an update gets before it goes to the
                failed list.
            max_pending: The number of scheduled updates at which the engine is full.

        Raises:
            ValueError: ``concurrency`` is below 1.
        """
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self.storage = storage
        self._process = process
        self._sem = asyncio.Semaphore(concurrency)
        self._max_runs = max_runs
        self.max_pending = max_pending
        self._tails: dict[str, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._scheduled: set[int] = set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._room = asyncio.Event()
        self._room.set()
        self._closing = False
        self.processed = 0
        self.failed = 0

    # --- storage calls: off the loop when they touch a disk ---

    async def _io(self, func: Callable[..., Any], *args: Any) -> Any:
        if self.storage.durable:
            return await asyncio.to_thread(func, *args)
        return func(*args)

    # --- intake ---

    async def recover(self, *, quiet: bool = False) -> int:
        """Schedules the pending updates that are not running yet.

        They were left by a crash or a stop, put back by a retry of failed updates, or stored
        by another worker process. At most as many as there is room for: the rest waits for the
        next call.

        Args:
            quiet: Don't log how many were picked up.

        Returns:
            How many updates were scheduled.
        """
        ids = await self._io(self.storage.pending_ids)
        room = max(0, self.max_pending - len(self._tasks))
        new = [uid for uid in ids if uid not in self._scheduled][:room]
        if not new:
            return 0
        updates = await self._io(self.storage.load, new)
        for update in updates:
            self._schedule(update)
        if updates and not quiet:
            log.info("resuming %d update(s) left pending from the last run", len(updates))
        return len(updates)

    async def submit(self, updates: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """Accepts updates into the storage and schedules the new ones.

        Args:
            updates: The updates, as the server sent them.

        Returns:
            The new updates, oldest first. Repeats are left out.
        """
        fresh = await self.store(updates)
        for update in fresh:
            self._schedule(update)
        return fresh

    async def store(self, updates: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """Accepts updates into the storage without scheduling them.

        Args:
            updates: The updates, as the server sent them.

        Returns:
            The new updates, oldest first. Repeats are left out.
        """
        if not updates:
            return []
        fresh: list[Mapping[str, Any]] = await self._io(self.storage.accept, updates)
        skipped = len(updates) - len(fresh)
        if skipped:
            log.debug("%d update(s) already seen, ignored", skipped)
        return fresh

    def _schedule(self, update: Mapping[str, Any]) -> None:
        uid = update.get("updateId")
        if not isinstance(uid, int) or uid in self._scheduled:
            return
        self._scheduled.add(uid)
        key = _chat_key(update)
        previous = self._tails.get(key)
        task = asyncio.create_task(self._run(uid, update, previous), name=f"pgpcbot-update-{uid}")
        self._tails[key] = task
        self._tasks.add(task)
        self._idle.clear()
        if len(self._tasks) >= self.max_pending:
            self._room.clear()

        def done(t: asyncio.Task[None], key: str = key) -> None:
            self._tasks.discard(t)
            self._scheduled.discard(uid)
            if self._tails.get(key) is t:
                del self._tails[key]
            if len(self._tasks) <= self.max_pending // 2:
                self._room.set()
            if not self._tasks:
                self._idle.set()

        task.add_done_callback(done)

    async def _run(self, uid: int, update: Mapping[str, Any], previous: asyncio.Task[None] | None) -> None:
        if previous is not None:
            # same chat: wait our turn, whatever came of it. wait() and not a plain
            # await: a cancel of this task (at stop) must still get through
            await asyncio.wait([previous])
        async with self._sem:
            runs = await self._begin(uid)
            if runs is None or runs == 0:
                # 0: finished since it was read from the inbox, nothing left to do
                return
            if runs > self._max_runs:
                note = f"gave up: started {runs - 1} times and never finished (the process kept stopping)"
                log.error("update %d: %s", uid, note)
                await self._settle(self.storage.fail, uid, note)
                self.failed += 1
                return
            try:
                await self._process(update)
            except asyncio.CancelledError:
                # shutting down mid-handler: left pending, it runs again next time
                raise
            except Exception as exc:
                private = sealed(update) and not isinstance(exc, PGPCbotError)
                if private:
                    # no exc_info: its last line is the message, which may quote what was decrypted
                    log.error(
                        "update %d failed and was set aside: %s\n%s",
                        uid,
                        describe(exc, private=True),
                        _frames_of(exc),
                    )
                else:
                    log.error("update %d failed and was set aside: %s", uid, describe(exc), exc_info=exc)
                await self._settle(self.storage.fail, uid, describe(exc, private=private))
                self.failed += 1
                return
            await self._settle(self.storage.finish, uid)
            self.processed += 1

    async def _begin(self, uid: int) -> int | None:
        # a storage that can't count the run right now (busy, full disk) gets a few
        # tries while this task keeps the chat's turn; after that the update stays
        # pending for the next sweep or start
        for attempt in range(3):
            try:
                runs: int = await self._io(self.storage.begin, uid)
                return runs
            except Exception as exc:  # noqa: BLE001 - any storage failure means the same here
                if attempt == 2:
                    log.error("update %d: the storage failed (%s); it stays pending", uid, exc)
                    return None
                await asyncio.sleep(0.5 * 2**attempt)
        return None  # pragma: no cover

    async def _settle(self, func: Callable[[int], Any] | Callable[[int, str], Any], *args: Any) -> None:
        try:
            await self._io(func, *args)
        except Exception as exc:  # noqa: BLE001
            # the handlers ran; the update stays pending and runs again: at least once
            log.error("update %s: could not record the outcome in the storage (%s)", args[0], exc)

    # --- waiting ---

    @property
    def busy(self) -> int:
        """How many updates are scheduled or running."""
        return len(self._tasks)

    async def drained(self) -> None:
        """Waits until nothing is scheduled or running."""
        await self._idle.wait()

    async def room(self) -> None:
        """Waits until there is room for more updates: the poller's backpressure.

        After ``max_pending`` was reached, the room comes back when half of it is free.
        """
        await self._room.wait()

    def is_running(self, update_id: int) -> bool:
        """Tells whether an update is scheduled or running in this process.

        Args:
            update_id: The update to look for.

        Returns:
            ``True`` while the update is scheduled or running.
        """
        return update_id in self._scheduled

    def inside(self) -> bool:
        """Tells whether the caller runs inside one of the engine's update tasks.

        Returns:
            ``True`` inside an update task; ``False`` elsewhere, or outside an event loop.
        """
        try:
            return asyncio.current_task() in self._tasks
        except RuntimeError:
            return False

    async def wait_for(self, update_id: int, seconds: float) -> bool:
        """Waits until an update is no longer scheduled or running.

        Args:
            update_id: The update to wait for.
            seconds: The longest wait.

        Returns:
            ``True`` once the update is no longer scheduled, ``False`` if ``seconds`` ran out
            first.
        """
        deadline = time.monotonic() + seconds
        while update_id in self._scheduled:
            if time.monotonic() >= deadline:
                return False
            tasks = [t for t in self._tasks if t.get_name() == f"pgpcbot-update-{update_id}"]
            if not tasks:
                return True
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(asyncio.shield(tasks[0]), max(0.0, deadline - time.monotonic()))
        return True

    async def stop(self, grace: float) -> None:
        """Waits up to ``grace`` seconds for the scheduled updates, then cancels the rest.

        A cancelled update is not finished, so it runs again at the next start.

        Args:
            grace: How many seconds to wait before cancelling.
        """
        self._closing = True
        if self._tasks:
            log.info("waiting up to %.0fs for %d update(s) still running", grace, len(self._tasks))
            _, late = await asyncio.wait(list(self._tasks), timeout=grace)
            for task in late:
                task.cancel()
            if late:
                log.warning("%d update(s) cut short; they stay pending and run again on the next start", len(late))
                await asyncio.gather(*late, return_exceptions=True)


# --- long polling ---


async def poll(
    fetch: Callable[[int | None, int, int], Awaitable[list[dict[str, Any]]]],
    engine: Engine,
    stop: asyncio.Event,
    *,
    poll_timeout: int = 50,
    limit: int = 100,
    paused_wait: float = 30.0,
) -> None:
    """Runs the long-poll loop until ``stop`` is set.

    The offset sent to the server confirms, and deletes for good, every update below it, so it
    only ever covers updates that are safe: written to a durable storage or, with a storage
    that is not durable, through their handlers. Network errors, most API errors (``5xx``
    among them) and a batch the storage could not keep are tried again after a growing wait; a
    ``429`` waits as long as the server says; a paused bot is checked again every
    ``paused_wait`` seconds.

    Args:
        fetch: ``fetch(offset, limit, timeout)`` does one ``GET /updates`` and returns the
            updates as plain JSON.
        engine: The engine the updates go to.
        stop: Set it to end the loop. A long poll in progress is dropped, and what it brought
            is not confirmed.
        poll_timeout: How long each request may wait on the server, in seconds.
        limit: How many updates one request may bring.
        paused_wait: How many seconds to wait between checks while the owner has paused the
            bot.

    Raises:
        AuthenticationError: The token is wrong or revoked.
        PollingError: A webhook is set for the bot, so the server refuses to be polled.
        APIError: A ``403``, ``404``, ``409`` or ``410`` that no retry gets past.
    """
    storage = engine.storage
    top = await engine._io(storage.accepted_max)
    offset: int | None = top + 1 if top is not None and storage.durable else None
    failures = 0
    early_empty = 0
    paused_said = False

    async def sleep(seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(stop.wait(), seconds)

    async def fetch_or_stop() -> list[dict[str, Any]] | None:
        # a stop (SIGTERM, request_stop) must not wait for a long poll that can hang
        # for 50 s. Dropping the poll loses nothing: what it brought is not confirmed
        polling = asyncio.ensure_future(fetch(offset, limit, poll_timeout))
        stopping = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({polling, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
        if not polling.done():
            polling.cancel()
            with contextlib.suppress(BaseException):
                await polling
            return None
        return polling.result()

    while not stop.is_set():
        started = time.monotonic()
        try:
            fetched = await fetch_or_stop()
            if fetched is None:
                break
            batch = fetched
        except AuthenticationError:
            raise
        except WebhookActiveError as exc:
            raise PollingError(
                "a webhook is set for this bot, so the server refuses to be polled. "
                "Delete it (bot.run(drop_webhook=True), or api.delete_webhook()) or use the webhook"
            ) from exc
        except BotPausedError:
            if not paused_said:
                log.warning("the bot is paused by its owner; checking again every %.0fs", paused_wait)
                paused_said = True
            await sleep(paused_wait)
            continue
        except RateLimitError as exc:
            wait = exc.retry_after if exc.retry_after is not None else 5.0
            log.warning("polling rate limited, waiting %.1fs", wait)
            await sleep(wait)
            continue
        except (NetworkError, ServerError, APIError) as exc:
            if stop.is_set():
                # stop() closed the connection under the poll: that is the way out, not a failure
                break
            if isinstance(exc, APIError) and not isinstance(exc, ServerError) and exc.status in (403, 404, 409, 410):
                # a refusal no retry gets past: something has to change first
                raise
            failures += 1
            wait = random.uniform(0, min(30.0, 1.0 * 2 ** min(failures, 6)))
            level = logging.WARNING if failures < 5 else logging.ERROR
            log.log(level, "polling failed (%s), trying again in %.1fs", exc, wait)
            await sleep(wait)
            continue

        if paused_said:
            log.info("the bot is active again")
            paused_said = False
        failures = 0

        # a second poller on the same bot ends ours at once with an empty answer
        if not batch and poll_timeout >= 10 and time.monotonic() - started < 1.0:
            early_empty += 1
            if early_empty == 3:
                log.warning(
                    "polls keep coming back empty right away: another process is probably polling "
                    "this bot with the same token, and the two of you are taking turns stealing updates"
                )
            await sleep(min(5.0, 0.5 * early_empty))
        else:
            early_empty = 0

        if not batch:
            continue

        top_id = max(u["updateId"] for u in batch if isinstance(u.get("updateId"), int))
        try:
            await engine.submit(batch)
        except StorageError as exc:
            # not stored: don't confirm, the next poll gets the same batch again
            failures += 1
            wait = random.uniform(0, min(30.0, 1.0 * 2 ** min(failures, 6)))
            log.error("could not store %d update(s) (%s), trying again in %.1fs", len(batch), exc, wait)
            await sleep(wait)
            continue
        if not storage.durable:
            # nothing on disk: confirm only what the handlers are through with
            await engine.drained()
        offset = top_id + 1
        await engine.room()


async def skip_pending(fetch: Callable[[int | None, int, int], Awaitable[list[dict[str, Any]]]], engine: Engine) -> int:
    """Confirms everything queued on the server without running a handler.

    The skipped IDs are also stored as seen and finished, so a late repeat of one is ignored.

    Args:
        fetch: Does one ``GET /updates``, as for ``poll``.
        engine: The engine whose storage records the skipped IDs.

    Returns:
        How many updates were skipped.
    """
    skipped = 0
    offset: int | None = None
    while True:
        batch = await fetch(offset, 100, 0)
        if not batch:
            break
        ids = [u["updateId"] for u in batch if isinstance(u.get("updateId"), int)]
        skipped += len(ids)
        offset = max(ids) + 1
        # remember them as seen, so a stray redelivery is ignored too
        await engine._io(engine.storage.accept, [{"updateId": i, "kind": "skipped"} for i in ids])
        for i in ids:
            await engine._io(engine.storage.finish, i)
    if offset is not None:
        # one more call with the offset is what makes the server forget the last batch
        await fetch(offset, 1, 0)
    return skipped
