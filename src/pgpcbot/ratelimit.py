"""Pacing on the client, so the server rarely has to answer ``429``.

The server counts in sliding windows: by default one message a second into a private chat,
twenty a minute into a group or channel, thirty a second in all chats together, and 600 API
calls a minute of any kind. Edits have windows of their own per chat, with the same numbers,
and share the thirty a second with sends. A server can use other numbers; ``GET /me`` says
which.

`~pgpcbot.RateLimiter` keeps the same books on the client, and makes a call wait before it
would be refused instead of after. It never refuses anything itself: the worst it does is make
a call wait its turn. Bots and clients make one for you.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from .models import ChatKind, Limits

__all__ = ["RateLimiter"]

_IDLE_PRUNE_S = 120.0


@dataclass(frozen=True, slots=True)
class _Window:
    limit: int
    seconds: float


class RateLimiter:
    """Paces API calls with the same sliding windows the server counts with.

    Before each call the client asks the limiter, which waits until every window the call
    counts in has room, then books the call. It never refuses a call, so the server rarely has
    to answer ``429``. The windows, with the fields of `Limits` that size them:

    - every API call: ``api_calls_per_minute`` (600) in any 60 seconds;
    - every send and edit, all chats together: ``sends_per_second`` (30) in any second;
    - sends into one private chat: ``sends_per_private_per_second`` (1) in any second;
    - sends into one group or channel: ``sends_per_group_per_minute`` (20) in any 60 seconds.

    Edits are counted per chat in windows of their own, with the same numbers as sends. A chat
    whose kind is not known gets the private window. A call that has to wait books nothing,
    as on the server. A group's slow mode and the limit on typing indicators are left to the
    server.

    Every `Bot`, `SyncBot`, `AsyncClient` and `Client` makes a limiter unless told otherwise
    (``rate_limit=`` on a bot, ``rate_limiter=`` on a client; ``False`` turns pacing off). The
    one a bot makes takes the server's numbers from ``GET /me`` when the bot starts. One you
    pass keeps the numbers you gave it. A limiter only counts the calls of its own process,
    and can be shared by several clients, async and sync, and by several threads.

    Example:
        Two processes that share a token can split the budget::

            from pgpcbot import Bot, Limits, RateLimiter

            half = Limits(sends_per_second=15, api_calls_per_minute=300)
            bot = Bot(rate_limit=RateLimiter(half))
    """

    def __init__(self, limits: Limits | None = None, *, clock: Callable[[], float] = time.monotonic) -> None:
        """Sets up empty windows, sized by ``limits``.

        Args:
            limits: The numbers to pace with. ``None`` means ``Limits()``, the server's
                documented defaults. Only ``api_calls_per_minute``, ``sends_per_second``,
                ``sends_per_private_per_second`` and ``sends_per_group_per_minute`` are used.
            clock: Where the limiter reads the time, in seconds; only differences matter. The
                waits themselves always sleep for real.
        """
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}
        self._blocked_until: dict[str, float] = {}
        self._clock = clock
        self._last_prune = clock()
        self.update_limits(limits or Limits())

    def update_limits(self, limits: Limits) -> None:
        """Switches to new numbers, at once.

        Calls already booked stay booked. A bot calls this with the numbers of ``GET /me`` when
        it starts, but only on a limiter it made itself.

        Args:
            limits: The new numbers. Only ``api_calls_per_minute``, ``sends_per_second``,
                ``sends_per_private_per_second`` and ``sends_per_group_per_minute`` are used.
        """
        with self._lock:
            self._calls = _Window(limits.api_calls_per_minute, 60.0)
            self._overall = _Window(limits.sends_per_second, 1.0)
            self._private = _Window(limits.sends_per_private_per_second, 1.0)
            self._group = _Window(limits.sends_per_group_per_minute, 60.0)

    # --- the books ---

    def _take(self, key: str, window: _Window, now: float) -> float:
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= window.seconds:
            hits.popleft()
        if len(hits) >= window.limit:
            return window.seconds - (now - hits[0])
        hits.append(now)
        return 0.0

    def _give_back(self, key: str) -> None:
        hits = self._hits.get(key)
        if hits:
            hits.pop()

    def _prune(self, now: float) -> None:
        if now - self._last_prune < _IDLE_PRUNE_S:
            return
        self._last_prune = now
        for key in [k for k, h in self._hits.items() if not h or now - h[-1] > _IDLE_PRUNE_S]:
            del self._hits[key]
        for key in [k for k, until in self._blocked_until.items() if until <= now]:
            del self._blocked_until[key]

    def _plan(self, *, send: bool, edit: bool, chat_id: int | None, chat_kind: ChatKind | None) -> float:
        """Books the call and returns 0, or books nothing and returns the seconds to wait."""
        with self._lock:
            now = self._clock()
            self._prune(now)

            keys = ["calls"]
            if send and chat_id is not None:
                keys.append(f"chat:{chat_id}{':edit' if edit else ''}")
            if send:
                keys.append("sends")
            blocked = max((self._blocked_until.get(k, 0.0) - now for k in keys), default=0.0)
            if blocked > 0:
                return blocked

            taken: list[str] = []
            for key in keys:
                if key == "calls":
                    window = self._calls
                elif key == "sends":
                    window = self._overall
                else:
                    # unknown kind: assume private, the strictest per-second window
                    window = self._group if chat_kind in (ChatKind.GROUP, ChatKind.CHANNEL) else self._private
                wait = self._take(key, window, now)
                if wait > 0:
                    # same as the server: a refused call books nothing
                    for done in taken:
                        self._give_back(done)
                    return wait
                taken.append(key)
            return 0.0

    def penalize(self, seconds: float, *, chat_id: int | None = None, edit: bool = False, calls: bool = False) -> None:
        """Keeps a window shut for ``seconds``, after the server answered ``429`` anyway.

        A ``429`` can still come, for example when another process uses the same token. The
        clients call this with the ``retry_after`` of a `FloodWaitError` or of a
        ``rate_limited`` `RateLimitError`, so the other calls that would hit the same limit wait
        too, instead of being refused one by one. A penalty never shortens a longer one already
        in place, and zero or less does nothing.

        Args:
            seconds: How long the window stays shut.
            chat_id: Shut the sends into this chat only. ``None`` shuts every send and edit.
            edit: With ``chat_id``, shut that chat's edits instead of its sends.
            calls: Shut every API call, whatever ``chat_id`` says.
        """
        if seconds <= 0:
            return
        with self._lock:
            until = self._clock() + seconds
            if calls:
                key = "calls"
            elif chat_id is not None:
                key = f"chat:{chat_id}{':edit' if edit else ''}"
            else:
                key = "sends"
            self._blocked_until[key] = max(self._blocked_until.get(key, 0.0), until)

    # --- waiting ---

    def acquire(
        self,
        *,
        send: bool = False,
        edit: bool = False,
        chat_id: int | None = None,
        chat_kind: ChatKind | None = None,
    ) -> float:
        """Blocks the calling thread until the call may go, then books it.

        Args:
            send: The call sends or edits a message, so it counts in the send windows too, not
                only in the calls per minute.
            edit: The send is an edit: it counts in the chat's edit window, not its send window.
            chat_id: The chat the message goes into. ``None`` counts the send in the overall
                window only.
            chat_kind: The kind of that chat, which picks its window: per minute for a group or
                a channel, per second for anything else, ``None`` included.

        Returns:
            How long it waited, in seconds; ``0.0`` when there was room at once.
        """
        waited = 0.0
        while True:
            wait = self._plan(send=send, edit=edit, chat_id=chat_id, chat_kind=chat_kind)
            if wait <= 0:
                return waited
            time.sleep(wait)
            waited += wait

    async def acquire_async(
        self,
        *,
        send: bool = False,
        edit: bool = False,
        chat_id: int | None = None,
        chat_kind: ChatKind | None = None,
    ) -> float:
        """Waits, without blocking the event loop, until the call may go, then books it.

        Args:
            send: The call sends or edits a message, so it counts in the send windows too, not
                only in the calls per minute.
            edit: The send is an edit: it counts in the chat's edit window, not its send window.
            chat_id: The chat the message goes into. ``None`` counts the send in the overall
                window only.
            chat_kind: The kind of that chat, which picks its window: per minute for a group or
                a channel, per second for anything else, ``None`` included.

        Returns:
            How long it waited, in seconds; ``0.0`` when there was room at once.
        """
        waited = 0.0
        while True:
            wait = self._plan(send=send, edit=edit, chat_id=chat_id, chat_kind=chat_kind)
            if wait <= 0:
                return waited
            await asyncio.sleep(wait)
            waited += wait
