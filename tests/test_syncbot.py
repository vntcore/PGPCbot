from __future__ import annotations

import contextlib
import re
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from pgpcbot import (
    AuthenticationError,
    BadRequestError,
    BotKey,
    Button,
    ChatMode,
    ChatNotFoundError,
    ConfigurationError,
    EncryptionError,
    EncryptionRequiredError,
    InlineKeyboard,
    InvalidKeyError,
    KeyChangedError,
    Media,
    NotARecipientError,
    PublicKey,
    SignatureStatus,
    SkipHandler,
    SyncBot,
    SyncCallbackQuery,
    SyncMessage,
    SyncSentMessage,
    Update,
    ValidationError,
    WrongBodyKindError,
    filters,
)

from .conftest import FIXTURES, fixture_text
from .fakeapi import MEDIA_ID, FakeAPI

PHONE_TEXT = "Ciao dal telefono: è già sera 🌙\nseconda riga"
ATTACHMENT = b"\xff\xd8\xffJPEGDATA"


def key_row(user_id: int, fixture: str, fingerprint: str) -> dict[str, Any]:
    return {"userId": user_id, "fingerprint": fingerprint, "publicKey": fixture_text(fixture)}


def last_update(fake: FakeAPI) -> dict[str, Any]:
    return fake.queue[-1]


def posts(fake: FakeAPI) -> list[dict[str, Any]]:
    return [call["json"] for call in fake.calls("POST", "/bot/v1/messages")]


def answers(fake: FakeAPI) -> list[dict[str, Any]]:
    return fake.calls("POST", r"/bot/v1/callbacks/[^/]+/answer")


def wait_until(probe: Callable[[], Any], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not probe():
        if time.monotonic() > deadline:
            raise AssertionError("condition still false after waiting")
        time.sleep(0.01)


@contextlib.contextmanager
def running(bot: SyncBot, fake: FakeAPI) -> Iterator[dict[str, BaseException]]:
    """``bot.run()`` in a thread. Leaving the block revokes the token, which is what ends a polling bot."""
    outcome: dict[str, BaseException] = {}

    def run() -> None:
        try:
            bot.run(poll_timeout=1, configure_logging=False)
        except BaseException as exc:
            outcome["error"] = exc

    fake.revoked = False
    thread = threading.Thread(target=run, name="syncbot-runner", daemon=True)
    thread.start()
    try:
        yield outcome
    finally:
        fake.revoked = True
        thread.join(10)
    assert not thread.is_alive()


@pytest.fixture
def new_sync_bot(make_sync_bot: Callable[..., SyncBot]) -> Iterator[Callable[..., SyncBot]]:
    made: list[SyncBot] = []

    def factory(**options: Any) -> SyncBot:
        bot = make_sync_bot(**options)
        made.append(bot)
        return bot

    yield factory
    for bot in made:
        bot.stop()
        bot.api.close()


@pytest.fixture(scope="module")
def phone_secret() -> BotKey:
    return BotKey.from_file(FIXTURES / "phone_ed.sec.asc")


@pytest.fixture(scope="module")
def rsa_secret() -> BotKey:
    return BotKey.from_file(FIXTURES / "phone_rsa.sec.asc")


@pytest.fixture(scope="module")
def v6_key() -> BotKey:
    from openpgp.composed import EncryptionCaps, KeyType, SecretKeyParamsBuilder, SubkeyParamsBuilder

    primary = (
        SecretKeyParamsBuilder()
        .version(6)
        .key_type(KeyType.ed25519())
        .can_certify(True)
        .can_sign(True)
        .primary_user_id("Six")
    )
    subkey = SubkeyParamsBuilder().version(6).key_type(KeyType.x25519()).can_encrypt(EncryptionCaps.all())
    return BotKey.from_armored(primary.subkey(subkey.build()).generate().to_armored())


@pytest.fixture
def anna_row(phone_key: PublicKey) -> dict[str, Any]:
    return key_row(55, "phone_ed.pub.asc", phone_key.fingerprint)


def test_plain_handler_replies_from_a_pgpcbot_handler_thread(fake, new_sync_bot):
    fake.add_chat(7)
    bot = new_sync_bot()
    seen: dict[str, Any] = {}

    @bot.on_message()
    def echo(message):
        seen["thread"] = threading.current_thread().name
        seen["message"] = message
        seen["sent"] = message.reply(f"echo {message.text}")

    fake.push_message(7, "hi", message_id=4007)
    bot.process_update(Update.from_json(last_update(fake)))

    assert seen["thread"].startswith("pgpcbot-handler")
    assert isinstance(seen["message"], SyncMessage)
    assert isinstance(seen["sent"], SyncSentMessage)
    assert seen["sent"].text == "echo hi"
    [post] = posts(fake)
    assert (post["chatId"], post["text"], post["replyTo"]) == (7, "echo hi", 4007)


def test_plain_handler_message_can_answer_show_typing_and_delete_itself(fake, new_sync_bot):
    fake.add_chat(7)
    bot = new_sync_bot()
    results: dict[str, Any] = {}

    @bot.on_message()
    def act(message):
        results["answer"] = message.answer("without a quote")
        results["typing"] = message.typing()
        results["deleted"] = message.delete()

    fake.push_message(7, "hi", message_id=4007)
    fake.messages[4007] = {"id": 4007, "chatId": 7, "body": "hi"}
    bot.process_update(last_update(fake))

    [post] = posts(fake)
    assert post["text"] == "without a quote"
    assert "replyTo" not in post
    assert fake.calls("POST", "/bot/v1/chats/7/typing")[0]["json"] == {"typing": True}
    assert 4007 not in fake.messages
    assert results["typing"] is True
    assert results["deleted"] is True
    assert isinstance(results["answer"], SyncSentMessage)


def test_plain_handler_raising_skip_handler_passes_the_update_on(fake, new_sync_bot):
    fake.add_chat(7)
    bot = new_sync_bot()
    ran: list[str] = []

    @bot.on_message()
    def picky(message):
        ran.append("picky")
        raise SkipHandler

    @bot.on_message()
    def fallback(message):
        ran.append("fallback")

    fake.push_message(7, "hello")
    bot.process_update(last_update(fake))

    assert ran == ["picky", "fallback"]


def test_update_no_plain_handler_wants_is_dropped_quietly(fake, new_sync_bot, caplog):
    fake.add_chat(7)
    bot = new_sync_bot()
    ran: list[str] = []

    @bot.on_message(group=True)
    def groups_only(message):
        ran.append(message.text)

    caplog.set_level("DEBUG", logger="pgpcbot.bot")
    fake.push_message(7, "anyone?")
    bot.process_update(last_update(fake))

    assert ran == []
    assert "no handler took it" in caplog.text


def test_command_arguments_and_regex_match_reach_plain_handlers(fake, new_sync_bot):
    fake.add_chat(7)
    bot = new_sync_bot()
    got: list[tuple[str, str]] = []

    @bot.command("start")
    def start(message, command):
        got.append(("start", command.args))

    @bot.on_message(filters.regex(r"ticket #(\d+)"))
    def ticket(message, match):
        got.append(("ticket", match.group(1)))

    for text in ("/start@testbot deep-link", "/start@otherbot", "about ticket #12"):
        fake.push_message(7, text)
        bot.process_update(last_update(fake))

    assert got == [("start", "deep-link"), ("ticket", "12")]


def test_callback_answered_by_a_plain_handler_is_answered_once(fake, new_sync_bot):
    fake.add_chat(7)
    bot = new_sync_bot()
    queries: list[SyncCallbackQuery] = []

    @bot.on_callback_query(data="save")
    def save(query):
        queries.append(query)
        assert query.answer("Saved", alert=True) is True

    fake.push_callback(7, 1500, "save")
    bot.process_update(last_update(fake))

    [query] = queries
    assert isinstance(query, SyncCallbackQuery)
    [answer] = answers(fake)
    assert answer["json"] == {"text": "Saved", "alert": True}


def test_unanswered_callback_gets_an_empty_answer_but_an_expired_one_gets_none(fake, new_sync_bot):
    fake.add_chat(7)
    bot = new_sync_bot()
    pressed: list[str] = []

    @bot.on_callback_query()
    def press(query):
        pressed.append(query.data)

    fresh_id = fake.push_callback(7, 1500, "fresh")
    bot.process_update(last_update(fake))
    fake.push_callback(7, 1500, "stale", expires_in=-5)
    bot.process_update(last_update(fake))

    assert pressed == ["fresh", "stale"]
    [answer] = answers(fake)
    assert answer["path"] == f"/bot/v1/callbacks/{fresh_id}/answer"
    assert answer["json"] == {}


def test_callback_helpers_edit_keep_or_remove_buttons_and_send(fake, new_sync_bot):
    fake.add_chat(7)
    bot = new_sync_bot()
    keyboard = InlineKeyboard.build([Button.callback("Yes", "yes"), Button.callback("No", "no")])
    question = bot.send_message(7, "Pick one", markup=keyboard)
    results: list[SyncSentMessage] = []

    @bot.on_callback_query(data="yes")
    def picked(query):
        results.append(query.edit_message("Thinking it over"))
        results.append(query.edit_message("You picked yes", markup=None))
        results.append(query.send("Thanks"))

    fake.push_callback(7, question.id, "yes")
    bot.process_update(last_update(fake))

    kept, removed = fake.calls("PATCH", f"/bot/v1/messages/{question.id}")
    assert kept["json"] == {"chatId": 7, "text": "Thinking it over"}
    assert removed["json"] == {"chatId": 7, "text": "You picked yes", "markup": None}
    assert results[0].markup is not None
    assert results[1].markup is None
    assert posts(fake)[-1]["text"] == "Thanks"
    assert all(isinstance(r, SyncSentMessage) for r in results)


def test_plain_callback_handler_can_delete_the_message_under_the_button(fake, new_sync_bot):
    fake.add_chat(7)
    bot = new_sync_bot()
    question = bot.send_message(7, "Pick one", markup=InlineKeyboard.build([Button.callback("Yes", "yes")]))

    @bot.on_callback_query()
    def remove(query):
        assert query.delete_message() is True

    fake.push_callback(7, question.id, "yes")
    bot.process_update(last_update(fake))

    assert question.id not in fake.messages


def test_sent_message_can_be_edited_and_deleted(fake, new_sync_bot):
    fake.add_chat(7)
    bot = new_sync_bot()
    draft = bot.send_message(7, "draft")

    final = draft.edit("final")
    plain = final.edit(markup=None)

    first, second = fake.calls("PATCH", f"/bot/v1/messages/{draft.id}")
    assert first["json"] == {"chatId": 7, "text": "final"}
    assert second["json"] == {"chatId": 7, "markup": None}
    assert plain.markup is None
    assert isinstance(final, SyncSentMessage)
    assert final.text == "final"
    assert final.delete() is True
    assert draft.id not in fake.messages


def test_plain_middlewares_wrap_in_order_and_can_inject_data_or_stop(fake, new_sync_bot):
    fake.add_chat(7)
    bot = new_sync_bot()
    trail: list[str] = []

    @bot.middleware
    def outer(handler, event, data):
        trail.append("outer")
        if event.text == "stop":
            return None
        data["lang"] = "it"
        return handler(event, data)

    @bot.middleware
    def inner(handler, event, data):
        trail.append("inner")
        return handler(event, data)

    @bot.on_message()
    def handle(message, lang):
        trail.append(f"handler {lang}")

    for text in ("go", "stop"):
        fake.push_message(7, text)
        bot.process_update(last_update(fake))

    assert trail == ["outer", "inner", "handler it", "outer"]


def test_plain_error_handlers_get_the_error_update_and_bot_and_can_skip(fake, new_sync_bot):
    fake.add_chat(7)
    the_bot = new_sync_bot()
    calls: list[Any] = []

    @the_bot.on_message()
    def broken(message):
        raise ValueError("bad input")

    @the_bot.on_error(KeyError)
    def wrong_type(error):
        calls.append("key")

    @the_bot.on_error(ValueError)
    def not_mine(error):
        calls.append("not mine")
        raise SkipHandler

    @the_bot.on_error()
    def report(error, update, bot):
        calls.append((str(error), update.message.text, bot is the_bot, threading.current_thread().name))

    fake.push_message(7, "boom")
    the_bot.process_update(last_update(fake))

    assert calls[0] == "not mine"
    error, text, same_bot, thread = calls[1]
    assert (error, text, same_bot) == ("bad input", "boom", True)
    assert thread.startswith("pgpcbot-handler")


def test_error_nobody_handles_propagates_from_process_update(fake, new_sync_bot):
    fake.add_chat(7)
    bot = new_sync_bot()

    @bot.on_message()
    def broken(message):
        raise RuntimeError("database down")

    fake.push_message(7, "boom")
    with pytest.raises(RuntimeError, match="database down"):
        bot.process_update(last_update(fake))


def test_plain_bot_refuses_async_handlers_middlewares_and_filters_at_start(fake, new_sync_bot):
    bot = new_sync_bot()

    @bot.on_message()
    async def async_handler(message):
        pass

    async def only_admins(message):
        return True

    @bot.on_message(only_admins)
    def plain_handler(message):
        pass

    @bot.middleware
    async def async_middleware(handler, event, data):
        pass

    @bot.on_error()
    async def async_error_handler(error):
        pass

    with pytest.raises(ConfigurationError) as info:
        bot.run(configure_logging=False)

    text = str(info.value)
    for name in ("async_handler", "only_admins", "async_middleware", "async_error_handler"):
        assert name in text
    # the filter is reported with the handler it guards
    assert re.search(r"filter <Filter only_admins> of \S*plain_handler\b", text)
    assert fake.requests == []


async def test_run_inside_a_running_event_loop_is_refused(fake, make_sync_bot):
    bot = make_sync_bot()

    with pytest.raises(RuntimeError, match="running event loop"):
        bot.run(configure_logging=False)
    assert fake.requests == []


def test_run_returns_quietly_on_ctrl_c(fake, new_sync_bot, monkeypatch):
    bot = new_sync_bot()

    async def interrupted(request: Any) -> Any:
        raise KeyboardInterrupt

    # ctrl+c while the bot waits on the server
    monkeypatch.setattr(fake, "handle_async_request", interrupted)
    bot.run(configure_logging=False)


def test_run_polls_and_handles_in_worker_threads_until_the_token_is_revoked(fake, new_sync_bot):
    fake.add_chat(7)
    bot = new_sync_bot()
    replied = threading.Event()
    threads: list[str] = []

    @bot.on_message()
    def echo(message):
        threads.append(threading.current_thread().name)
        message.reply(f"echo: {message.text}")
        replied.set()

    with running(bot, fake) as outcome:
        fake.push_message(7, "ping")
        assert replied.wait(5)

    assert isinstance(outcome.get("error"), AuthenticationError)
    assert threads[0].startswith("pgpcbot-handler")
    assert [p["text"] for p in posts(fake)] == ["echo: ping"]


def test_failed_updates_are_set_aside_then_retried_or_discarded(fake, new_sync_bot):
    fake.add_chat(7)
    bot = new_sync_bot()
    runs: list[str] = []
    retried = threading.Event()

    @bot.on_message()
    def flaky(message):
        runs.append(message.text)
        if runs.count(message.text) == 1:
            raise RuntimeError(f"{message.text} failed")
        retried.set()

    first_id = fake.push_message(7, "first")
    second_id = fake.push_message(7, "second")
    with running(bot, fake):
        wait_until(lambda: len(bot.failed_updates()) == 2)

    first, second = bot.failed_updates()
    assert (first.update_id, second.update_id) == (first_id, second_id)
    assert first.error.startswith("RuntimeError: first failed")
    assert first.payload["message"]["text"] == "first"
    assert bot.retry_failed([first_id]) == 1
    assert bot.discard_failed([second_id]) == 1
    assert bot.failed_updates() == []

    # back in line: the next run picks it up, the discarded one is gone for good
    with running(bot, fake):
        assert retried.wait(5)
    assert runs == ["first", "second", "first"]


def test_retry_failed_reruns_the_update_while_the_bot_runs(fake, new_sync_bot):
    fake.add_chat(7)
    bot = new_sync_bot()
    runs: list[str] = []
    rerun = threading.Event()

    @bot.on_message()
    def flaky(message):
        runs.append(message.text)
        if len(runs) == 1:
            raise RuntimeError("not now")
        rerun.set()

    with running(bot, fake):
        fake.push_message(7, "save me")
        wait_until(bot.failed_updates)
        assert bot.retry_failed() == 1
        ran_again = rerun.wait(1.0)

    assert ran_again
    assert bot.failed_updates() == []


def test_send_into_a_protected_private_chat_is_sealed_and_signed(fake, new_sync_bot, bot_key, anna_row, phone_secret):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[anna_row])
    bot = new_sync_bot(pgp_private_key=bot_key)

    sent = bot.send_message(7, "the forecast says rain")

    [post] = posts(fake)
    assert "text" not in post
    opened = phone_secret.decrypt(post["armored"], verify_with=[bot_key.public_key])
    assert opened.text == "the forecast says rain"
    assert opened.verification.valid
    assert opened.verification.signer_fingerprint == bot_key.fingerprint
    assert isinstance(sent, SyncSentMessage)
    assert sent.encrypted


def test_reply_in_a_protected_group_is_sealed_to_its_members(fake, new_sync_bot, bot_key, anna_row, phone_secret):
    fake.add_chat(900, "group", title="Team", history_visible=False, keys=[anna_row])
    bot = new_sync_bot(pgp_private_key=bot_key)

    @bot.on_message()
    def answer(message):
        message.answer("roger")

    fake.push_message(900, armored=fixture_text("msg_text_signed.asc"))
    bot.process_update(last_update(fake))

    [post] = posts(fake)
    assert "replyTo" not in post
    assert phone_secret.decrypt(post["armored"]).text == "roger"
    # the armored body is only a hint (anyone in the group shapes it): the group is asked once
    assert len(fake.calls("GET", "/bot/v1/chats/900")) == 1


@pytest.mark.parametrize(
    ("options", "send_options"),
    [({"require_encryption": True}, {}), ({}, {"encrypt": True})],
    ids=["require_encryption", "encrypt=True"],
)
def test_open_chat_refuses_a_message_that_must_be_encrypted(fake, new_sync_bot, bot_key, options, send_options):
    fake.add_chat(7)
    bot = new_sync_bot(pgp_private_key=bot_key, **options)

    with pytest.raises(EncryptionRequiredError):
        bot.send_message(7, "the vault code", **send_options)
    assert posts(fake) == []


@pytest.mark.parametrize(
    ("with_key", "send_options", "error"),
    [(True, {"encrypt": False}, ValidationError), (False, {}, EncryptionError)],
    ids=["encrypt=False", "no bot key"],
)
def test_protected_chat_refuses_what_it_cannot_take(
    fake, new_sync_bot, bot_key, anna_row, with_key, send_options, error
):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[anna_row])
    bot = new_sync_bot(pgp_private_key=bot_key if with_key else None)

    with pytest.raises(error):
        bot.send_message(7, "hello", **send_options)
    assert posts(fake) == []


def test_chat_that_turned_protected_gets_a_sealed_resend_with_the_same_send_id(
    fake, new_sync_bot, bot_key, anna_row, phone_secret
):
    fake.add_chat(7, keys=[anna_row])
    bot = new_sync_bot(pgp_private_key=bot_key)
    bot.get_chat(7)
    bot.get_me()
    # published from somewhere else (the console, another worker) while this bot's view is fresh
    fake.me["mode"] = "protected"

    sent = bot.send_message(7, "still there?")

    clear, sealed = posts(fake)
    assert clear["text"] == "still there?"
    assert "text" not in sealed
    assert sealed["sendId"] == clear["sendId"]
    assert phone_secret.decrypt(sealed["armored"]).text == "still there?"
    assert sent.encrypted


def test_chat_that_turned_readable_is_never_sent_in_clear_on_its_own(fake, new_sync_bot, bot_key, anna_row):
    chat = fake.add_chat(900, "group", title="Team", history_visible=False, keys=[anna_row])
    bot = new_sync_bot(pgp_private_key=bot_key)
    bot.send_message(900, "sealed hello")
    chat.history_visible = True

    with pytest.raises(WrongBodyKindError) as info:
        bot.send_message(900, "secret plan")

    assert info.value.expected == "text"
    assert all("text" not in p for p in posts(fake))
    assert bot.send_message(900, "hello in clear").text == "hello in clear"


def test_edit_in_a_chat_that_turned_protected_is_sealed_and_sent_again(
    fake, new_sync_bot, bot_key, anna_row, phone_secret
):
    fake.add_chat(7, keys=[anna_row])
    bot = new_sync_bot(pgp_private_key=bot_key)
    draft = bot.send_message(7, "draft")
    fake.me["mode"] = "protected"

    final = draft.edit("final words")

    clear, sealed = fake.calls("PATCH", f"/bot/v1/messages/{draft.id}")
    assert clear["json"] == {"chatId": 7, "text": "final words"}
    assert phone_secret.decrypt(sealed["json"]["armored"]).text == "final words"
    assert final.encrypted


def test_edit_in_a_chat_that_turned_readable_is_not_resent_in_clear(fake, new_sync_bot, bot_key, anna_row):
    chat = fake.add_chat(900, "group", title="Team", history_visible=False, keys=[anna_row])
    bot = new_sync_bot(pgp_private_key=bot_key)
    draft = bot.send_message(900, "sealed draft")
    chat.history_visible = True

    with pytest.raises(WrongBodyKindError):
        draft.edit("final words")

    [patch] = fake.calls("PATCH", f"/bot/v1/messages/{draft.id}")
    assert "text" not in patch["json"]


def test_seal_encrypts_for_the_chat_without_sending(fake, new_sync_bot, bot_key, anna_row, phone_secret, phone_key):
    fake.add_chat(900, "group", title="Team", history_visible=False, keys=[anna_row])
    bot = new_sync_bot(pgp_private_key=bot_key)

    assert [key.fingerprint for key in bot.recipients(900)] == [phone_key.fingerprint]
    assert phone_secret.decrypt(bot.seal(900, "for later")).text == "for later"
    assert posts(fake) == []


def test_chat_that_turned_readable_before_its_keys_were_fetched_takes_text_on_the_next_send(
    fake, new_sync_bot, bot_key, anna_row
):
    chat = fake.add_chat(900, "group", title="Team", history_visible=False, keys=[anna_row])
    bot = new_sync_bot(pgp_private_key=bot_key)
    bot.get_chat(900)
    chat.history_visible = True

    with pytest.raises(BadRequestError):
        bot.send_message(900, "first try")
    assert posts(fake) == []

    assert bot.send_message(900, "second try").text == "second try"


def test_changed_key_stops_sends_until_it_is_trusted(
    fake, new_sync_bot, bot_key, anna_row, phone_key, rsa_phone_key, rsa_secret
):
    fake.me["mode"] = "protected"
    chat = fake.add_chat(7, keys=[anna_row])
    bot = new_sync_bot(pgp_private_key=bot_key, pin_keys=True, cache_ttl=0)
    bot.send_message(7, "first")
    assert bot.storage.get_pin(55) == phone_key.fingerprint
    chat.keys = [key_row(55, "phone_rsa.pub.asc", rsa_phone_key.fingerprint)]

    with pytest.raises(KeyChangedError) as info:
        bot.send_message(7, "second")

    assert (info.value.pinned, info.value.current) == (phone_key.fingerprint, rsa_phone_key.fingerprint)
    bot.trust_key(55, rsa_phone_key.fingerprint)
    bot.send_message(7, "third")
    assert rsa_secret.decrypt(posts(fake)[-1]["armored"]).text == "third"
    assert len(posts(fake)) == 2


def test_signed_message_is_opened_and_verified_for_a_plain_handler(fake, new_sync_bot, bot_key, anna_row, phone_key):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[anna_row])
    bot = new_sync_bot(pgp_private_key=bot_key)
    received: list[SyncMessage] = []

    @bot.on_message(filters.signed)
    def got(message):
        received.append(message)

    fake.push_message(7, armored=fixture_text("msg_text_signed.asc"))
    bot.process_update(last_update(fake))

    [message] = received
    assert message.text == PHONE_TEXT
    assert message.plaintext == PHONE_TEXT
    assert message.encrypted
    assert message.verification.status == SignatureStatus.VALID
    assert message.verification.signer_fingerprint == phone_key.fingerprint


def test_message_not_sealed_to_the_bot_goes_to_a_plain_error_handler(fake, new_sync_bot, bot_key, anna_row):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[anna_row])
    bot = new_sync_bot(pgp_private_key=bot_key)
    errors: list[BaseException] = []

    @bot.on_message()
    def got(message):
        errors.append(AssertionError("the handler should not run"))

    @bot.on_error(NotARecipientError)
    def not_for_us(error):
        errors.append(error)

    fake.push_message(7, armored=fixture_text("msg_not_for_bot.asc"))
    bot.process_update(last_update(fake))

    [error] = errors
    assert isinstance(error, NotARecipientError)


def test_encrypted_attachment_is_downloaded_decrypted_and_open_media_as_is(fake, new_sync_bot, bot_key, anna_row):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[anna_row])
    fake.media[MEDIA_ID] = (FIXTURES / "attachment.pgp").read_bytes()
    fake.media["5d1f0000-aaaa-bbbb-cccc-000000000001"] = b"plain jpeg bytes"
    bot = new_sync_bot(pgp_private_key=bot_key)
    photos: list[SyncMessage] = []

    @bot.on_message(filters.has_media)
    def photo(message):
        photos.append(message)

    fake.push_message(7, armored=fixture_text("msg_media.asc"))
    bot.process_update(last_update(fake))

    [message] = photos
    assert message.text == "guarda che tramonto"
    assert bot.download_media(message.media[0]) == ATTACHMENT
    plain = Media(id="5d1f0000-aaaa-bbbb-cccc-000000000001", kind="image")
    assert bot.download_media(plain) == b"plain jpeg bytes"


def test_publish_key_puts_the_key_and_seals_private_chats_from_then_on(
    fake, new_sync_bot, bot_key, anna_row, phone_secret
):
    fake.add_chat(7, keys=[anna_row])
    bot = new_sync_bot(pgp_private_key=bot_key)
    bot.get_chat(7)

    result = bot.publish_key()

    [put] = fake.calls("PUT", "/bot/v1/key")
    assert put["json"] == {"publicKey": bot_key.export_public(), "fingerprint": bot_key.fingerprint}
    assert (result.fingerprint, result.mode) == (bot_key.fingerprint, ChatMode.PROTECTED)
    assert bot.me.protected
    bot.send_message(7, "sealed from now on")
    [post] = posts(fake)
    assert phone_secret.decrypt(post["armored"]).text == "sealed from now on"


def test_get_chat_reports_private_chats_protected_after_publish_key(fake, new_sync_bot, bot_key):
    fake.add_chat(7)
    bot = new_sync_bot(pgp_private_key=bot_key)
    assert bot.get_chat(7).mode == ChatMode.OPEN

    bot.publish_key()

    assert bot.get_chat(7).mode == ChatMode.PROTECTED


def test_publish_key_without_any_key_is_a_configuration_error(fake, new_sync_bot):
    bot = new_sync_bot()

    with pytest.raises(ConfigurationError, match="no key to publish"):
        bot.publish_key()
    assert fake.calls("PUT", "/bot/v1/key") == []


def test_rotate_key_keeps_the_old_key_for_decryption_and_v6_is_refused(
    fake, new_sync_bot, bot_key, old_bot_key, anna_row, v6_key
):
    fake.me.update(mode="protected", fingerprint=old_bot_key.fingerprint)
    fake.add_chat(7, keys=[anna_row])
    bot = new_sync_bot(pgp_private_key=old_bot_key)
    received: list[str] = []

    @bot.on_message()
    def got(message):
        received.append(message.text)

    with pytest.raises(InvalidKeyError):
        bot.rotate_key(v6_key)
    bot.rotate_key(bot_key)
    for fixture in ("msg_for_old_key.asc", "msg_text_signed.asc"):
        fake.push_message(7, armored=fixture_text(fixture))
        bot.process_update(last_update(fake))

    assert [put["json"]["fingerprint"] for put in fake.calls("PUT", "/bot/v1/key")] == [bot_key.fingerprint]
    assert bot.key is bot_key
    assert received == [PHONE_TEXT, PHONE_TEXT]


def test_get_chat_is_cached_and_leave_chat_forgets_it(fake, new_sync_bot):
    fake.add_chat(900, "group", title="Team", history_visible=True)
    bot = new_sync_bot()

    assert bot.get_chat(900) is bot.get_chat(900)
    assert bot.leave_chat(900) is True
    with pytest.raises(ChatNotFoundError):
        bot.get_chat(900)
    assert len(fake.calls("GET", "/bot/v1/chats/900")) == 2


def test_get_me_is_kept_until_refreshed_and_commands_sync(fake, new_sync_bot):
    bot = new_sync_bot()

    @bot.command("help", description="What I can do")
    def help_(message):
        pass

    first = bot.get_me()
    fake.me["name"] = "Renamed"
    assert bot.get_me() is first
    assert bot.get_me(refresh=True).name == "Renamed"
    scope = bot.sync_commands()
    assert [(c.command, c.description) for c in scope.commands] == [("help", "What I can do")]
    assert fake.commands["default"] == [{"command": "help", "description": "What I can do"}]


def test_api_helpers_work_without_await(fake, new_sync_bot, anna_row, phone_key):
    anna = {"id": 55, "username": "anna", "firstName": "Anna", "isBot": False}
    members = [{"user": anna, "status": "member", "rights": 255}]
    fake.add_chat(900, "group", title="Team", history_visible=False, keys=[anna_row], members=members)
    fake.user_keys[55] = anna_row
    bot = new_sync_bot()

    assert [k.user_id for k in bot.get_chat_keys(900).keys] == [55]
    assert [m.user.id for m in bot.get_chat_members(900).members] == [55]
    assert bot.get_chat_member(900, 55).is_inside
    assert bot.get_user_key(55).fingerprint == phone_key.fingerprint
    assert bot.send_typing(900) is True

    bot.set_commands([("start", "Say hello")])
    assert [c.command for c in bot.get_commands("default")] == ["start"]
    assert bot.delete_commands().commands == ()

    assert bot.set_webhook("https://bot.example/hook", "Shared_secret-0123456789abcdef").has_secret
    assert bot.get_webhook().url == "https://bot.example/hook"
    assert bot.delete_webhook().url is None

    assert bot.revoke_token() is True
    assert fake.revoked


def test_sync_commands_without_descriptions_is_a_configuration_error(fake, new_sync_bot):
    bot = new_sync_bot()

    @bot.command("start")
    def start(message):
        pass

    with pytest.raises(ConfigurationError):
        bot.sync_commands()
    assert fake.calls("PUT", "/bot/v1/commands") == []
