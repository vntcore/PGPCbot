from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from pgpcbot import AsyncClient, Bot, Client, RetryPolicy, SyncBot
from pgpcbot.crypto import BotKey, PublicKey

from .fakeapi import BASE, TOKEN, FakeAPI

FIXTURES = Path(__file__).parent / "fixtures"

# retries that don't make the suite wait: same rules, no sleeping
FAST_RETRY = RetryPolicy(attempts=4, backoff_base=0.0, backoff_max=0.0, rate_limit_retries=3, max_rate_limit_wait=5.0)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # a developer's own token or key in the environment must never reach a test
    for name in list(os.environ):
        if name.startswith("PGPCHAT_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fake() -> FakeAPI:
    return FakeAPI()


@pytest.fixture
async def api(fake: FakeAPI) -> AsyncIterator[AsyncClient]:
    client = AsyncClient(TOKEN, api_url=BASE, transport=fake, retry=FAST_RETRY, rate_limiter=False)
    yield client
    await client.aclose()


@pytest.fixture
def sync_api(fake: FakeAPI) -> Iterator[Client]:
    client = Client(TOKEN, api_url=BASE, transport=fake, retry=FAST_RETRY, rate_limiter=False)
    yield client
    client.close()


@pytest.fixture
def make_bot(fake: FakeAPI) -> Callable[..., Bot]:
    def factory(**options: Any) -> Bot:
        options.setdefault("api_url", BASE)
        options.setdefault("transport", fake)
        options.setdefault("retry", FAST_RETRY)
        options.setdefault("rate_limit", False)
        options.setdefault("shutdown_timeout", 2.0)
        return Bot(TOKEN, **options)

    return factory


@pytest.fixture
def make_sync_bot(fake: FakeAPI) -> Callable[..., SyncBot]:
    def factory(**options: Any) -> SyncBot:
        options.setdefault("api_url", BASE)
        options.setdefault("transport", fake)
        options.setdefault("retry", FAST_RETRY)
        options.setdefault("rate_limit", False)
        options.setdefault("shutdown_timeout", 2.0)
        return SyncBot(TOKEN, **options)

    return factory


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="ascii")


@pytest.fixture(scope="session")
def bot_key() -> BotKey:
    """The bot's key the PGPainless fixtures were sealed to."""
    return BotKey.from_file(FIXTURES / "bot.sec.asc")


@pytest.fixture(scope="session")
def old_bot_key() -> BotKey:
    return BotKey.from_file(FIXTURES / "bot_old.sec.asc")


@pytest.fixture(scope="session")
def phone_key() -> PublicKey:
    """Anna's phone: a PGPainless 1.7.6 modernKeyRing (Ed25519 + X25519)."""
    return PublicKey.from_file(FIXTURES / "phone_ed.pub.asc")


@pytest.fixture(scope="session")
def rsa_phone_key() -> PublicKey:
    """Bruno's phone: a PGPainless 1.7.6 RSA-3072 key."""
    return PublicKey.from_file(FIXTURES / "phone_rsa.pub.asc")


@pytest.fixture(scope="session")
def stranger_key() -> BotKey:
    return BotKey.generate("Stranger")
