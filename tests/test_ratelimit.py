from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pgpcbot import ChatKind, Limits, RateLimiter, ratelimit

PRIVATE, GROUP, CHANNEL = ChatKind.PRIVATE, ChatKind.GROUP, ChatKind.CHANNEL


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> list[float]:
    """Both sleeps move the fake clock instead of waiting, and say how long they were asked for."""
    asked: list[float] = []

    def sleep(seconds: float) -> None:
        asked.append(seconds)
        clock.advance(seconds)

    async def async_sleep(seconds: float) -> None:
        asked.append(seconds)
        clock.advance(seconds)

    monkeypatch.setattr(ratelimit, "time", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(ratelimit, "asyncio", SimpleNamespace(sleep=async_sleep))
    return asked


def limiter(clock: Clock, **limits: int) -> RateLimiter:
    return RateLimiter(Limits(**limits), clock=clock)


# _plan is one attempt of acquire() without the sleeping: 0 when it booked the call,
# otherwise the seconds it would have to wait. it lets a test look at every attempt.
def send(rl: RateLimiter, chat_id: int | None, kind: ChatKind | None = PRIVATE, *, edit: bool = False) -> float:
    return rl._plan(send=True, edit=edit, chat_id=chat_id, chat_kind=kind)


def call(rl: RateLimiter) -> float:
    return rl._plan(send=False, edit=False, chat_id=None, chat_kind=None)


def test_one_send_a_second_into_a_private_chat(clock: Clock) -> None:
    rl = limiter(clock)
    assert send(rl, 7) == 0
    assert send(rl, 7) == pytest.approx(1.0)
    clock.advance(0.4)
    assert send(rl, 7) == pytest.approx(0.6)
    clock.advance(0.6)
    assert send(rl, 7) == 0


def test_private_chats_do_not_share_a_window(clock: Clock) -> None:
    rl = limiter(clock)
    assert [send(rl, chat) for chat in (1, 2, 3)] == [0, 0, 0]


@pytest.mark.parametrize("kind", [GROUP, CHANNEL])
def test_twenty_sends_a_minute_into_a_group_or_channel(clock: Clock, kind: ChatKind) -> None:
    rl = limiter(clock)
    for _ in range(20):
        assert send(rl, 300, kind) == 0
        clock.advance(0.5)
    # the first of the twenty went out 10 s ago
    assert send(rl, 300, kind) == pytest.approx(50.0)
    clock.advance(50.0)
    assert send(rl, 300, kind) == 0
    assert send(rl, 300, kind) == pytest.approx(0.5)


def test_thirty_sends_a_second_overall(clock: Clock) -> None:
    rl = limiter(clock)
    for chat in range(1, 31):
        assert send(rl, chat) == 0
    assert send(rl, 31) == pytest.approx(1.0)
    clock.advance(1.0)
    assert send(rl, 31) == 0


def test_api_calls_a_minute(clock: Clock) -> None:
    rl = limiter(clock, api_calls_per_minute=3)
    assert [call(rl), call(rl), call(rl)] == [0, 0, 0]
    assert call(rl) == pytest.approx(60.0)
    # a send is a call too
    assert send(rl, 7) == pytest.approx(60.0)


@pytest.mark.parametrize("kind", [None, ChatKind("supergroup")])
def test_an_unknown_chat_kind_gets_the_private_window(clock: Clock, kind: ChatKind | None) -> None:
    rl = limiter(clock)
    assert send(rl, 5, kind) == 0
    assert send(rl, 5, kind) == pytest.approx(1.0)


def test_a_send_without_a_chat_only_counts_overall(clock: Clock) -> None:
    rl = limiter(clock, sends_per_second=2)
    assert send(rl, None) == 0
    assert send(rl, None) == 0
    assert send(rl, None) == pytest.approx(1.0)


def test_edits_have_their_own_window_per_chat(clock: Clock) -> None:
    rl = limiter(clock)
    assert send(rl, 7) == 0
    assert send(rl, 7, edit=True) == 0
    assert send(rl, 7, edit=True) == pytest.approx(1.0)
    assert send(rl, 7) == pytest.approx(1.0)


def test_edits_share_the_overall_window_with_sends(clock: Clock) -> None:
    rl = limiter(clock, sends_per_second=2)
    assert send(rl, 1) == 0
    assert send(rl, 2, edit=True) == 0
    assert send(rl, 3, edit=True) == pytest.approx(1.0)
    assert send(rl, 4) == pytest.approx(1.0)


def test_a_refused_attempt_books_nothing(clock: Clock) -> None:
    rl = limiter(clock, sends_per_second=1, sends_per_group_per_minute=1, api_calls_per_minute=2)
    assert send(rl, 301, GROUP) == 0
    # its chat and call books take it first, then the full overall window says no
    assert send(rl, 302, GROUP) == pytest.approx(1.0)
    clock.advance(1.0)
    # booked a second ago, chat 302 would be shut for 59 s more and the calls would be full
    assert send(rl, 302, GROUP) == 0
    # two calls in the minute: the first send and this one
    assert call(rl) == pytest.approx(59.0)


def test_penalize_one_chat(clock: Clock) -> None:
    rl = limiter(clock)
    rl.penalize(5.0, chat_id=7)
    assert send(rl, 7) == pytest.approx(5.0)
    assert send(rl, 8) == 0
    assert call(rl) == 0
    assert send(rl, 7, edit=True) == 0
    clock.advance(5.0)
    assert send(rl, 7) == 0


def test_penalize_the_edits_of_one_chat(clock: Clock) -> None:
    rl = limiter(clock)
    rl.penalize(3.0, chat_id=7, edit=True)
    assert send(rl, 7, edit=True) == pytest.approx(3.0)
    assert send(rl, 7) == 0


def test_penalize_every_send(clock: Clock) -> None:
    rl = limiter(clock)
    rl.penalize(2.0)
    assert send(rl, 1) == pytest.approx(2.0)
    assert send(rl, 2, GROUP) == pytest.approx(2.0)
    assert call(rl) == 0


def test_penalize_every_call(clock: Clock) -> None:
    rl = limiter(clock)
    rl.penalize(10.0, calls=True, chat_id=7)
    assert call(rl) == pytest.approx(10.0)
    assert send(rl, 8) == pytest.approx(10.0)


def test_a_penalty_never_shortens_a_longer_one(clock: Clock) -> None:
    rl = limiter(clock)
    rl.penalize(8.0, chat_id=7)
    rl.penalize(2.0, chat_id=7)
    assert send(rl, 7) == pytest.approx(8.0)
    rl.penalize(12.0, chat_id=7)
    assert send(rl, 7) == pytest.approx(12.0)


@pytest.mark.parametrize("seconds", [0.0, -3.0])
def test_a_penalty_of_nothing_is_ignored(clock: Clock, seconds: float) -> None:
    rl = limiter(clock)
    rl.penalize(seconds, chat_id=7)
    assert send(rl, 7) == 0


def test_a_blocked_attempt_books_nothing(clock: Clock) -> None:
    rl = limiter(clock, sends_per_group_per_minute=1)
    rl.penalize(1.0, chat_id=300)
    assert send(rl, 300, GROUP) == pytest.approx(1.0)
    clock.advance(1.0)
    # had the blocked try been booked, the one-a-minute window would be shut for 59 s
    assert send(rl, 300, GROUP) == 0


def test_defaults_are_the_documented_numbers(clock: Clock) -> None:
    rl = RateLimiter(clock=clock)
    for chat in range(1, 31):
        assert send(rl, chat) == 0
    assert send(rl, 99) > 0
    clock.advance(1.0)
    for _ in range(20):
        assert send(rl, 300, GROUP) == 0
        clock.advance(1.0)
    assert send(rl, 300, GROUP) > 0


def test_update_limits_takes_effect_at_once(clock: Clock) -> None:
    rl = limiter(clock)
    assert send(rl, 7) == 0
    assert send(rl, 7) > 0
    rl.update_limits(Limits(sends_per_private_per_second=3))
    assert send(rl, 7) == 0
    assert send(rl, 7) == 0
    assert send(rl, 7) == pytest.approx(1.0)


def test_limits_from_the_server_are_used(clock: Clock) -> None:
    rl = RateLimiter(Limits.from_json({"sendsPerGroupPerMinute": 2}), clock=clock)
    assert send(rl, 300, GROUP) == 0
    assert send(rl, 300, GROUP) == 0
    assert send(rl, 300, GROUP) == pytest.approx(60.0)


def test_idle_books_are_pruned(clock: Clock) -> None:
    rl = limiter(clock)
    for chat in range(1, 6):
        send(rl, chat)
    rl.penalize(5.0, chat_id=1)
    rl.penalize(500.0, chat_id=2)
    clock.advance(100.0)
    send(rl, 9)
    clock.advance(21.0)
    call(rl)
    # what the dicts hold is the memory a long-running bot would otherwise keep forever
    assert set(rl._hits) == {"calls", "chat:9", "sends"}
    assert set(rl._blocked_until) == {"chat:2"}


def test_pruning_runs_every_two_minutes_and_drops_books_idle_for_longer(clock: Clock) -> None:
    rl = limiter(clock)
    clock.advance(10.0)
    send(rl, 1)
    clock.advance(110.0)
    call(rl)  # a round runs: chat 1 has been idle 110 s, it stays
    assert "chat:1" in rl._hits
    clock.advance(15.0)
    call(rl)  # idle 125 s now, but the next round is not due yet
    assert "chat:1" in rl._hits
    clock.advance(105.0)
    call(rl)
    assert "chat:1" not in rl._hits


def test_acquire_does_not_wait_when_there_is_room(clock: Clock, sleeps: list[float]) -> None:
    rl = limiter(clock)
    assert rl.acquire() == 0.0
    assert rl.acquire(send=True, chat_id=7, chat_kind=PRIVATE) == 0.0
    assert sleeps == []


def test_acquire_sleeps_until_the_window_opens_and_says_how_long(clock: Clock, sleeps: list[float]) -> None:
    rl = limiter(clock)
    rl.acquire(send=True, chat_id=7, chat_kind=PRIVATE)
    clock.advance(0.25)
    waited = rl.acquire(send=True, chat_id=7, chat_kind=PRIVATE)
    assert waited == pytest.approx(0.75)
    assert sleeps == [pytest.approx(0.75)]
    assert clock.now == pytest.approx(1001.0)


def test_acquire_keeps_waiting_while_a_penalty_holds(clock: Clock, sleeps: list[float]) -> None:
    rl = limiter(clock)
    rl.acquire(send=True, chat_id=7, chat_kind=PRIVATE)
    rl.penalize(3.0, chat_id=7)
    assert rl.acquire(send=True, chat_id=7, chat_kind=PRIVATE) == pytest.approx(3.0)
    assert sleeps == [pytest.approx(3.0)]


async def test_acquire_async_waits_the_same_way(clock: Clock, sleeps: list[float]) -> None:
    rl = limiter(clock)
    for _ in range(20):
        assert await rl.acquire_async(send=True, chat_id=300, chat_kind=GROUP) == 0.0
    assert await rl.acquire_async(send=True, chat_id=300, chat_kind=GROUP) == pytest.approx(60.0)
    assert await rl.acquire_async(send=True, chat_id=300, chat_kind=GROUP, edit=True) == 0.0
    assert sleeps == [pytest.approx(60.0)]


async def test_acquire_async_adds_up_several_waits(clock: Clock, sleeps: list[float]) -> None:
    rl = limiter(clock, sends_per_second=1)
    await rl.acquire_async(send=True, chat_id=1, chat_kind=PRIVATE)
    rl.penalize(0.5, chat_id=2)
    # first the penalty on chat 2, then the overall window still busy with chat 1
    waited = await rl.acquire_async(send=True, chat_id=2, chat_kind=PRIVATE)
    assert sleeps == [pytest.approx(0.5), pytest.approx(0.5)]
    assert waited == pytest.approx(1.0)


def test_one_limiter_serves_sync_and_async_callers(clock: Clock, sleeps: list[float]) -> None:
    rl = limiter(clock)
    rl.acquire(send=True, chat_id=7, chat_kind=PRIVATE)
    assert asyncio.run(rl.acquire_async(send=True, chat_id=7, chat_kind=PRIVATE)) == pytest.approx(1.0)
