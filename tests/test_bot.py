from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import pickle
import signal
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

import pgpcbot
from pgpcbot import (
    AsyncClient,
    AuthenticationError,
    BadRequestError,
    Bot,
    BotKey,
    Button,
    CallbackQuery,
    ChatKind,
    ChatMode,
    ChatNotFoundError,
    ConfigurationError,
    DecryptionError,
    DeletedMessages,
    EncryptionError,
    EncryptionRequiredError,
    InlineKeyboard,
    InvalidKeyError,
    KeyChangedError,
    Media,
    MemoryStorage,
    Message,
    NotARecipientError,
    NoUserKeyError,
    ParsedCommand,
    PassphraseError,
    PublicKey,
    Router,
    SentMessage,
    SignatureError,
    SignatureStatus,
    SkipHandler,
    SQLiteStorage,
    Update,
    UpdateKind,
    ValidationError,
    WrongBodyKindError,
    filters,
)
from pgpcbot.errors import PollingConflictError, PollingError

from .conftest import FAST_RETRY, FIXTURES, fixture_text
from .fakeapi import BASE, BOT_ID, MEDIA_ID, TOKEN, FakeAPI

PHONE_TEXT = "Ciao dal telefono: è già sera 🌙\nseconda riga"
ATTACHMENT = b"\xff\xd8\xffJPEGDATA"
GARBAGE_KEY = "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nbm90IGEga2V5IGF0IGFsbA==\n-----END PGP PUBLIC KEY BLOCK-----\n"


def key_row(user_id: int, fixture: str, fingerprint: str | None) -> dict[str, Any]:
    return {"userId": user_id, "fingerprint": fingerprint, "publicKey": fixture_text(fixture)}


def last_update(fake: FakeAPI) -> dict[str, Any]:
    return fake.queue[-1]


def posts(fake: FakeAPI) -> list[dict[str, Any]]:
    return [call["json"] for call in fake.calls("POST", "/bot/v1/messages")]


def answers(fake: FakeAPI) -> list[dict[str, Any]]:
    return fake.calls("POST", r"/bot/v1/callbacks/[^/]+/answer")


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


@contextlib.asynccontextmanager
async def polling(bot: Bot, **options: Any) -> AsyncIterator[asyncio.Task[None]]:
    task = asyncio.create_task(bot.start_polling(poll_timeout=1, **options))
    try:
        yield task
    finally:
        await bot.stop()
        await asyncio.wait_for(task, 5)


@pytest.fixture
async def new_bot(make_bot: Callable[..., Bot]) -> AsyncIterator[Callable[..., Bot]]:
    made: list[Bot] = []

    def factory(**options: Any) -> Bot:
        bot = make_bot(**options)
        made.append(bot)
        return bot

    yield factory
    for bot in made:
        await bot.stop()


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


@pytest.fixture
def bruno_row(rsa_phone_key: PublicKey) -> dict[str, Any]:
    return key_row(56, "phone_rsa.pub.asc", rsa_phone_key.fingerprint)


@pytest.fixture(scope="module")
def sign_only_key() -> PublicKey:
    from openpgp.composed import KeyType, SecretKeyParamsBuilder

    secret = (
        SecretKeyParamsBuilder()
        .version(4)
        .key_type(KeyType.ed25519_legacy())
        .can_certify(True)
        .can_sign(True)
        .primary_user_id("Signer")
        .generate()
    )
    return PublicKey.from_armored(secret.to_public_key().to_armored())


@pytest.fixture
def unusable_row(
    v6_key: BotKey, sign_only_key: PublicKey, rsa_phone_key: PublicKey
) -> Callable[[str, int], dict[str, Any]]:
    def make(kind: str, user_id: int) -> dict[str, Any]:
        if kind == "garbage":
            return {"userId": user_id, "fingerprint": None, "publicKey": GARBAGE_KEY}
        if kind == "v6":
            return {"userId": user_id, "fingerprint": v6_key.fingerprint, "publicKey": v6_key.export_public()}
        if kind == "sign-only":
            return {"userId": user_id, "fingerprint": sign_only_key.fingerprint, "publicKey": sign_only_key.armored}
        # the server vouches for one fingerprint and hands out another key
        return key_row(user_id, "phone_ed.pub.asc", rsa_phone_key.fingerprint)

    return make


async def test_private_and_group_handlers_only_see_their_own_chats(fake, new_bot):
    fake.add_chat(7)
    fake.add_chat(900, "group", title="Team", history_visible=True)
    bot = new_bot()
    seen: list[tuple[str, int]] = []

    @bot.on_message(private=True)
    async def in_private(message):
        seen.append(("private", message.chat.id))

    @bot.on_message(group=True)
    async def in_group(message):
        seen.append(("group", message.chat.id))

    fake.push_message(7, "hi bot")
    await bot.process_update(last_update(fake))
    fake.push_message(900, "hi all")
    await bot.process_update(last_update(fake))

    assert seen == [("private", 7), ("group", 900)]


async def test_handler_can_exclude_a_chat_kind(fake, new_bot):
    fake.add_chat(7)
    fake.add_chat(900, "group", title="Team", history_visible=True)
    bot = new_bot()
    seen: list[int] = []

    @bot.on_message(private=False)
    async def not_private(message):
        seen.append(message.chat.id)

    for chat_id in (7, 900):
        fake.push_message(chat_id, "hello")
        await bot.process_update(last_update(fake))

    assert seen == [900]


@pytest.mark.parametrize("text", ["/start", "/start@testbot", "/START", "/Start@TestBot please"])
async def test_start_command_matches_its_plain_addressed_and_uppercase_forms(fake, new_bot, text):
    fake.add_chat(7)
    bot = new_bot()
    started: list[ParsedCommand] = []

    @bot.command("start")
    async def start(message, command):
        started.append(command)

    fake.push_message(7, text)
    await bot.process_update(last_update(fake))

    [command] = started
    assert command.name == "start"


@pytest.mark.parametrize("text", ["/start@otherbot", "/started", "please /start", "start"])
async def test_start_command_ignores_other_bots_and_other_words(fake, new_bot, text):
    fake.add_chat(7)
    bot = new_bot()
    started: list[str] = []

    @bot.command("start")
    async def start(message):
        started.append(message.text)

    fake.push_message(7, text)
    await bot.process_update(last_update(fake))

    assert started == []


async def test_command_mention_is_checked_against_the_handle_the_server_reports(fake, new_bot):
    fake.me["username"] = "weatherbot"
    fake.add_chat(7)
    bot = new_bot()
    started: list[str] = []

    @bot.command("start")
    async def start(message):
        started.append(message.text)

    for text in ("/start@testbot", "/start@weatherbot"):
        fake.push_message(7, text)
        await bot.process_update(last_update(fake))

    assert started == ["/start@weatherbot"]


async def test_command_arguments_and_regex_match_are_handed_to_the_handler(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    got: dict[str, Any] = {}

    @bot.on_message(filters.regex(r"order #(\d+)"))
    async def order(message, match):
        got["order"] = match.group(1)

    @bot.command("weather")
    async def weather(message, command):
        got["command"] = command

    fake.push_message(7, "/weather@testbot Rome  tomorrow")
    await bot.process_update(last_update(fake))
    fake.push_message(7, "where is order #42?")
    await bot.process_update(last_update(fake))

    command = got["command"]
    assert (command.name, command.mention, command.args) == ("weather", "testbot", "Rome  tomorrow")
    assert command.argv == ["Rome", "tomorrow"]
    assert got["order"] == "42"


async def test_edited_message_goes_to_edited_handlers_only(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    seen: list[tuple[str, str]] = []

    @bot.on_message()
    async def new(message):
        seen.append(("new", message.text))

    @bot.on_edited_message()
    async def edited(message):
        seen.append(("edited", message.text))

    fake.push_message(7, "helo", message_id=3001)
    await bot.process_update(last_update(fake))
    fake.push_message(7, "hello", message_id=3001, edited=True)
    await bot.process_update(last_update(fake))

    assert seen == [("new", "helo"), ("edited", "hello")]


async def test_deleted_messages_reach_their_handler(fake, new_bot):
    chat = fake.add_chat(7)
    bot = new_bot()
    seen: list[DeletedMessages] = []

    @bot.on_deleted_messages()
    async def gone(deleted):
        seen.append(deleted)

    fake.push("deleted_messages", chat=chat.ref(), messageIds=[3001, 3002])
    await bot.process_update(last_update(fake))

    [deleted] = seen
    assert deleted.chat.id == 7
    assert deleted.message_ids == (3001, 3002)


async def test_chat_member_update_reaches_its_handler_and_drops_what_was_cached_for_that_chat(fake, new_bot):
    team = fake.add_chat(900, "group", title="Team", history_visible=True)
    fake.add_chat(901, "group", title="Other", history_visible=True)
    bot = new_bot()
    await bot.get_chat(900)
    await bot.get_chat(901)
    changes = []

    @bot.on_chat_member()
    async def seat(change):
        changes.append(change)

    fake.push(
        "my_chat_member",
        chat=team.ref(),
        date=1791000000000,
        old={"status": "member", "rights": 255},
        new={"status": "admin", "adminRights": 3, "can": {"deleteMessages": True}},
        **{"from": {"id": 55, "username": "anna", "firstName": "Anna", "isBot": False}},
    )
    await bot.process_update(last_update(fake))
    await bot.get_chat(900)
    await bot.get_chat(901)

    [change] = changes
    assert change.new.is_admin
    assert change.new.can.delete_messages
    assert change.from_user.id == 55
    assert not change.joined
    assert not change.left
    # our seat changed in 900: what the bot knew about it is stale, 901 is untouched
    assert len(fake.calls("GET", "/bot/v1/chats/900")) == 2
    assert len(fake.calls("GET", "/bot/v1/chats/901")) == 1


async def test_on_update_handler_receives_the_update_itself(fake, new_bot):
    chat = fake.add_chat(7)
    bot = new_bot()
    seen: list[Any] = []

    @bot.on_update()
    async def everything(update):
        seen.append(update)

    fake.push_message(7, "hi")
    await bot.process_update(Update.from_json(last_update(fake)))
    fake.push("deleted_messages", chat=chat.ref(), messageIds=[3001])
    await bot.process_update(last_update(fake))

    assert all(isinstance(update, Update) for update in seen)
    assert [update.kind for update in seen] == [UpdateKind.MESSAGE, UpdateKind.DELETED_MESSAGES]
    assert seen[0].message.text == "hi"


async def test_higher_priority_runs_first_then_registration_order(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    ran: list[str] = []

    @bot.on_message()
    async def first(message):
        ran.append("first")

    @bot.on_message()
    async def second(message):
        ran.append("second")

    fake.push_message(7, "one")
    await bot.process_update(last_update(fake))

    @bot.on_message(priority=5)
    async def urgent(message):
        ran.append("urgent")

    fake.push_message(7, "two")
    await bot.process_update(last_update(fake))

    assert ran == ["first", "urgent"]


async def test_skip_handler_passes_the_update_to_the_next_handler(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    ran: list[str] = []

    @bot.on_message()
    async def picky(message):
        ran.append("picky")
        raise SkipHandler

    @bot.on_message()
    async def fallback(message):
        ran.append("fallback")

    fake.push_message(7, "hello")
    await bot.process_update(last_update(fake))

    assert ran == ["picky", "fallback"]


async def test_update_no_handler_wants_is_dropped_quietly(fake, new_bot, caplog):
    fake.add_chat(7)
    bot = new_bot()
    ran: list[str] = []

    @bot.on_message(group=True)
    async def groups_only(message):
        ran.append(message.text)

    caplog.set_level(logging.DEBUG, logger="pgpcbot.bot")
    fake.push_message(7, "anyone?")
    await bot.process_update(last_update(fake))
    await new_bot().process_update(last_update(fake))

    assert ran == []
    assert "no handler took it" in caplog.text


async def test_sub_router_handlers_are_tried_after_the_bots_own(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    child = Router("child")
    bot.include_router(child)
    ran: list[str] = []

    @child.on_message(priority=100)
    async def in_child(message):
        ran.append("child")

    @bot.on_message(filters.text("ping"))
    async def in_bot(message):
        ran.append("bot")

    for text in ("ping", "pong"):
        fake.push_message(7, text)
        await bot.process_update(last_update(fake))

    assert ran == ["bot", "child"]


async def test_reply_asks_a_group_for_its_mode_but_not_a_private_chat(fake, new_bot):
    fake.add_chat(7)
    fake.add_chat(900, "group", title="Team", history_visible=True)
    bot = new_bot()

    @bot.on_message()
    async def echo(message):
        sent = await message.reply(f"echo {message.text}")
        assert isinstance(message, Message)
        assert isinstance(sent, SentMessage)

    for chat_id in (7, 900):
        fake.push_message(chat_id, "hi", message_id=4000 + chat_id)
        await bot.process_update(last_update(fake))

    assert [(p["chatId"], p["text"], p["replyTo"]) for p in posts(fake)] == [
        (7, "echo hi", 4007),
        (900, "echo hi", 4900),
    ]
    # a private chat follows /me. A group's body is a hint anyone in it can shape, so
    # the group is asked once rather than trusted on its word
    assert [c["path"] for c in fake.calls("GET", r"/bot/v1/chats/\d+")] == ["/bot/v1/chats/900"]


async def test_update_of_a_kind_this_version_does_not_know_still_reaches_on_update(fake, new_bot):
    bot = new_bot()
    seen: list[Any] = []

    @bot.on_message()
    async def messages(message):
        seen.append("message handler")

    @bot.on_update()
    async def everything(update):
        seen.append(update)

    await bot.process_update({"updateId": 77, "kind": "poll_answer", "poll": {"id": "p1"}})

    [update] = seen
    assert update.kind == "poll_answer"
    assert update.chat is None
    assert update.event is None
    assert update.raw["poll"] == {"id": "p1"}


async def test_message_can_answer_show_typing_and_delete_itself(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    results: dict[str, Any] = {}

    @bot.on_message()
    async def act(message):
        results["answer"] = await message.answer("without a quote")
        results["typing"] = await message.typing()
        results["deleted"] = await message.delete()

    fake.push_message(7, "hi", message_id=4007)
    fake.messages[4007] = {"id": 4007, "chatId": 7, "body": "hi"}
    await bot.process_update(last_update(fake))

    [post] = posts(fake)
    assert post["text"] == "without a quote"
    assert "replyTo" not in post
    [typing] = fake.calls("POST", "/bot/v1/chats/7/typing")
    assert typing["json"] == {"typing": True}
    [delete] = fake.calls("DELETE", "/bot/v1/messages/4007")
    assert delete["query"] == {"chatId": "7"}
    assert 4007 not in fake.messages
    assert results["typing"] is True
    assert results["deleted"] is True
    assert isinstance(results["answer"], SentMessage)


async def test_chats_and_messages_can_be_passed_as_objects(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    received: list[Message] = []

    @bot.on_message()
    async def keep(message):
        received.append(message)

    fake.push_message(7, "hi", message_id=4007)
    await bot.process_update(last_update(fake))
    [message] = received

    sent = await bot.send_message(message.chat, "by reference", reply_to=message)
    assert await bot.send_typing(message) is True
    assert await bot.delete_message(sent, sent) is True

    [post] = posts(fake)
    assert (post["chatId"], post["replyTo"]) == (7, 4007)
    assert len(fake.calls("POST", "/bot/v1/chats/7/typing")) == 1
    assert sent.id not in fake.messages


async def test_async_filters_are_awaited(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    handled: list[int] = []

    async def from_anna(message):
        return message.from_user.id == 55

    @bot.on_message(from_anna)
    async def greet(message):
        handled.append(message.from_user.id)

    for sender in (66, 55):
        fake.push_message(7, "hello", from_id=sender)
        await bot.process_update(last_update(fake))

    assert handled == [55]


async def test_command_and_regex_filters_skip_messages_without_text(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    handled: list[str] = []

    @bot.command("start")
    async def start(message):
        handled.append("command")

    @bot.on_message(filters.regex(r".*"))
    async def anything(message):
        handled.append("regex")

    @bot.on_message()
    async def fallback(message):
        handled.append("fallback")

    photo = {"id": "5d1f0000-aaaa-bbbb-cccc-000000000001", "kind": "image"}
    fake.push_message(7, None, media=[photo])
    await bot.process_update(last_update(fake))

    assert handled == ["fallback"]


@pytest.mark.parametrize("chat", [0, -5, True, "7"], ids=["zero", "negative", "bool", "str"])
async def test_send_to_something_that_is_not_a_chat_id_is_refused(fake, new_bot, chat):
    bot = new_bot()

    with pytest.raises(ValidationError, match="chat_id"):
        await bot.send_message(chat, "hello")
    assert fake.requests == []


async def test_models_not_handed_out_by_a_bot_cannot_act(fake):
    fake.add_chat(7)
    fake.push_message(7, "hi")
    message = Message.from_json(last_update(fake)["message"])

    with pytest.raises(RuntimeError, match="not bound to a bot"):
        await message.reply("hello")
    assert fake.requests == []


async def test_callback_answered_by_the_handler_is_answered_once(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    queries: list[CallbackQuery] = []

    @bot.on_callback_query(data="save")
    async def save(query):
        queries.append(query)
        assert await query.answer("Saved") is True

    callback_id = fake.push_callback(7, 1500, "save")
    await bot.process_update(last_update(fake))

    [query] = queries
    assert isinstance(query, CallbackQuery)
    assert query.data == "save"
    assert query.message_id == 1500
    [answer] = answers(fake)
    assert answer["path"] == f"/bot/v1/callbacks/{callback_id}/answer"
    assert answer["json"] == {"text": "Saved"}


async def test_unanswered_callback_gets_an_empty_answer(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    pressed: list[str] = []

    @bot.on_callback_query(prefix="page:")
    async def page(query):
        pressed.append(query.data)

    callback_id = fake.push_callback(7, 1500, "page:2")
    await bot.process_update(last_update(fake))

    assert pressed == ["page:2"]
    [answer] = answers(fake)
    assert answer["json"] == {}
    assert fake.callbacks[callback_id]["answered"]


async def test_expired_callback_gets_no_automatic_answer(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    pressed: list[bool] = []

    @bot.on_callback_query()
    async def late(query):
        pressed.append(query.expired)

    fake.push_callback(7, 1500, "save", expires_in=-5)
    await bot.process_update(last_update(fake))

    assert pressed == [True]
    assert answers(fake) == []


async def test_callback_spinner_stops_even_when_the_handler_fails(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()

    @bot.on_callback_query()
    async def broken(query):
        raise RuntimeError("handler bug")

    fake.push_callback(7, 1500, "save")
    with pytest.raises(RuntimeError, match="handler bug"):
        await bot.process_update(last_update(fake))

    [answer] = answers(fake)
    assert answer["json"] == {}


async def test_automatic_callback_answers_can_be_turned_off(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot(auto_answer_callbacks=False)

    @bot.on_callback_query()
    async def silent(query):
        pass

    fake.push_callback(7, 1500, "save")
    await bot.process_update(last_update(fake))

    assert answers(fake) == []


async def test_callback_edit_keeps_buttons_by_default_and_removes_them_with_none(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    keyboard = InlineKeyboard.build([Button.callback("Yes", "yes"), Button.callback("No", "no")])
    question = await bot.send_message(7, "Pick one", markup=keyboard)
    edits: list[SentMessage] = []

    @bot.on_callback_query(data="yes")
    async def picked(query):
        edits.append(await query.edit_message("Thinking it over"))
        edits.append(await query.edit_message("You picked yes", markup=None))

    fake.push_callback(7, question.id, "yes")
    await bot.process_update(last_update(fake))

    kept, removed = fake.calls("PATCH", f"/bot/v1/messages/{question.id}")
    assert kept["json"] == {"chatId": 7, "text": "Thinking it over"}
    assert removed["json"] == {"chatId": 7, "text": "You picked yes", "markup": None}
    assert [button.callback_data for button in edits[0].markup.buttons()] == ["yes", "no"]
    assert edits[1].markup is None
    assert fake.messages[question.id]["markup"] is None


async def test_callback_send_and_pattern_match(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()

    @bot.on_callback_query(pattern=r"^page:(\d+)$")
    async def page(query, match):
        await query.send(f"page {match.group(1)}")

    fake.push_callback(7, 1500, "page:3")
    await bot.process_update(last_update(fake))

    [post] = posts(fake)
    assert post["chatId"] == 7
    assert post["text"] == "page 3"
    assert "replyTo" not in post


async def test_callback_can_delete_the_message_under_the_button(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    question = await bot.send_message(7, "Pick one", markup=InlineKeyboard.build([Button.callback("Yes", "yes")]))

    @bot.on_callback_query()
    async def remove(query):
        assert await query.delete_message() is True

    fake.push_callback(7, question.id, "yes")
    await bot.process_update(last_update(fake))

    assert question.id not in fake.messages
    assert fake.calls("DELETE", f"/bot/v1/messages/{question.id}")[0]["query"] == {"chatId": "7"}


@pytest.mark.parametrize(
    "fault",
    [{"status": 410, "code": "callback_expired"}, {"status": 500, "code": "internal"}, {"network": True}],
    ids=["expired", "5xx", "network"],
)
async def test_failing_automatic_answer_does_not_fail_the_update(fake, new_bot, fault):
    fake.add_chat(7)
    bot = new_bot()
    pressed: list[str] = []

    @bot.on_callback_query()
    async def press(query):
        pressed.append(query.data)

    fake.fail("POST", r"/callbacks/[^/]+/answer$", times=10, **fault)
    fake.push_callback(7, 1500, "save")
    await bot.process_update(last_update(fake))

    assert pressed == ["save"]


async def test_middlewares_wrap_handlers_outermost_first(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    child = Router("child")
    bot.include_router(child)
    trail: list[str] = []

    def tracing(name: str) -> Callable[..., Any]:
        async def middleware(handler, event, data):
            trail.append(f"{name} in")
            result = await handler(event, data)
            trail.append(f"{name} out")
            return result

        return middleware

    bot.middleware(tracing("outer"))
    bot.middleware(tracing("second"))
    child.middleware(tracing("inner"))

    @child.on_message()
    async def handle(message):
        trail.append("handler")

    fake.push_message(7, "hi")
    await bot.process_update(last_update(fake))

    assert trail == ["outer in", "second in", "inner in", "handler", "inner out", "second out", "outer out"]


async def test_middleware_that_does_not_call_on_stops_the_update(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    handled: list[int] = []

    @bot.middleware
    async def only_anna(handler, event, data):
        if event.from_user.id != 55:
            return None
        return await handler(event, data)

    @bot.on_message()
    async def handle(message):
        handled.append(message.from_user.id)

    @bot.on_message()
    async def fallback(message):
        handled.append(-1)

    fake.push_message(7, "from a stranger", from_id=66)
    await bot.process_update(last_update(fake))
    fake.push_message(7, "from anna", from_id=55)
    await bot.process_update(last_update(fake))

    assert handled == [55]


async def test_middleware_can_hand_data_to_the_handler(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    got: dict[str, Any] = {}

    @bot.middleware
    async def locale(handler, event, data):
        got["seen by middleware"] = (data["bot"], data["update"].update_id)
        data["lang"] = "it"
        return await handler(event, data)

    @bot.on_message()
    async def greet(message, lang, update):
        got["lang"] = lang
        got["update"] = update.update_id

    uid = fake.push_message(7, "ciao")
    await bot.process_update(last_update(fake))

    assert got == {"seen by middleware": (bot, uid), "lang": "it", "update": uid}


async def test_error_goes_to_the_first_error_handler_whose_type_matches(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    calls: list[tuple[str, BaseException]] = []

    @bot.on_message()
    async def broken(message):
        raise KeyError("missing")

    @bot.on_error(ZeroDivisionError)
    async def arithmetic(error):
        calls.append(("arithmetic", error))

    @bot.on_error(LookupError)
    async def lookup(error):
        calls.append(("lookup", error))

    @bot.on_error(KeyError)
    async def key(error):
        calls.append(("key", error))

    fake.push_message(7, "boom")
    await bot.process_update(last_update(fake))

    [(name, error)] = calls
    assert name == "lookup"
    assert isinstance(error, KeyError)


async def test_error_handler_can_ask_for_the_update_and_the_bot(fake, new_bot):
    fake.add_chat(7)
    the_bot = new_bot()
    seen: dict[str, Any] = {}

    @the_bot.on_message()
    async def broken(message):
        raise ValueError("bad input")

    @the_bot.on_error(ValueError)
    async def report(error, update, bot):
        seen.update(error=error, update=update, bot=bot)

    fake.push_message(7, "boom")
    await the_bot.process_update(last_update(fake))

    assert str(seen["error"]) == "bad input"
    assert seen["update"].message.text == "boom"
    assert seen["bot"] is the_bot


async def test_error_handler_raising_skip_handler_hands_the_error_to_the_next(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    calls: list[str] = []

    @bot.on_message()
    async def broken(message):
        raise RuntimeError("boom")

    @bot.on_error()
    async def not_mine(error):
        calls.append("not mine")
        raise SkipHandler

    @bot.on_error()
    async def mine(error):
        calls.append("mine")

    fake.push_message(7, "boom")
    await bot.process_update(last_update(fake))

    assert calls == ["not mine", "mine"]


async def test_error_nobody_handles_propagates_from_process_update(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    handled: list[BaseException] = []

    @bot.on_message()
    async def broken(message):
        raise RuntimeError("database down")

    @bot.on_error(ValueError)
    async def values_only(error):
        handled.append(error)

    fake.push_message(7, "boom")
    with pytest.raises(RuntimeError, match="database down"):
        await bot.process_update(last_update(fake))
    assert handled == []


async def test_bot_refuses_plain_functions_at_start_and_names_them(fake, new_bot):
    bot = new_bot()

    @bot.on_message()
    def plain_handler(message):
        pass

    @bot.on_error()
    def plain_error_handler(error):
        pass

    @bot.middleware
    def plain_middleware(handler, event, data):
        pass

    with pytest.raises(ConfigurationError) as info:
        await bot.start()

    text = str(info.value)
    assert "plain_handler" in text
    assert "plain_error_handler" in text
    assert "plain_middleware" in text
    assert "SyncBot" in text
    # refused before it even talked to the server
    assert fake.requests == []


async def test_polling_runs_handlers_and_confirms_what_they_finished(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    replied = asyncio.Event()

    @bot.on_message()
    async def echo(message):
        await message.reply(f"echo: {message.text}")
        replied.set()

    async with polling(bot):
        fake.push_message(7, "ping", message_id=4321)
        await asyncio.wait_for(replied.wait(), 5)
        # the next poll carries an offset past it: the server forgets it
        await eventually(lambda: not fake.queue)

    [reply] = posts(fake)
    assert (reply["text"], reply["replyTo"]) == ("echo: ping", 4321)


async def test_polling_sets_aside_only_updates_whose_error_nobody_handled(fake, new_bot, caplog):
    fake.add_chat(7)
    bot = new_bot()
    done = asyncio.Event()

    @bot.on_message()
    async def handle(message):
        if message.text == "crash":
            raise RuntimeError("database down")
        if message.text == "invalid":
            raise ValueError("bad input")
        done.set()

    @bot.on_error(ValueError)
    async def invalid_input(error):
        pass

    crash_id = fake.push_message(7, "crash")
    fake.push_message(7, "invalid")
    fake.push_message(7, "done")
    async with polling(bot):
        # one chat runs one update at a time: "done" comes after the other two settled
        await asyncio.wait_for(done.wait(), 5)
        failed = await bot.failed_updates()

    [entry] = failed
    assert entry.update_id == crash_id
    assert "database down" in entry.error
    assert entry.payload["message"]["text"] == "crash"
    assert any(r.levelno == logging.ERROR and "set aside" in r.getMessage() for r in caplog.records)


async def test_failed_update_can_be_retried_and_discarded(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    runs: list[str] = []
    recovered = asyncio.Event()

    @bot.on_message()
    async def flaky(message):
        runs.append(message.text)
        if message.text == "doomed" or runs.count("flaky") == 1:
            raise RuntimeError("not now")
        recovered.set()

    flaky_id = fake.push_message(7, "flaky")
    doomed_id = fake.push_message(7, "doomed")
    async with polling(bot):
        await eventually(lambda: len(bot.storage.failed()) == 2)

        assert await bot.retry_failed([flaky_id]) == 1
        await asyncio.wait_for(recovered.wait(), 5)
        assert [entry.update_id for entry in await bot.failed_updates()] == [doomed_id]

        assert await bot.discard_failed() == 1
        assert await bot.failed_updates() == []
        assert await bot.retry_failed() == 0

    assert runs == ["flaky", "doomed", "flaky"]


async def test_polling_refuses_to_start_while_a_webhook_is_set(fake, new_bot):
    fake.webhook["url"] = "https://bot.example/hook"
    bot = new_bot()

    with pytest.raises(PollingError, match="webhook"):
        await bot.start_polling(poll_timeout=1)

    assert fake.webhook["url"] == "https://bot.example/hook"
    assert fake.calls("DELETE", "/bot/v1/webhook") == []
    assert fake.calls("GET", "/bot/v1/updates") == []


def test_polling_error_can_be_imported_from_the_package():
    assert "PollingError" in pgpcbot.__all__
    assert pgpcbot.PollingError is PollingError


async def test_polling_removes_the_webhook_when_told_to(fake, new_bot):
    fake.webhook["url"] = "https://bot.example/hook"
    fake.add_chat(7)
    bot = new_bot()
    got = asyncio.Event()

    @bot.on_message()
    async def handle(message):
        got.set()

    fake.push_message(7, "hello")
    async with polling(bot, drop_webhook=True):
        await asyncio.wait_for(got.wait(), 5)

    assert fake.webhook["url"] is None
    assert len(fake.calls("DELETE", "/bot/v1/webhook")) == 1


async def test_skip_pending_drops_what_queued_up_while_the_bot_was_down(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    seen: list[str] = []
    fresh = asyncio.Event()

    @bot.on_message()
    async def handle(message):
        seen.append(message.text)
        fresh.set()

    fake.push_message(7, "stale one")
    fake.push_message(7, "stale two")
    async with polling(bot, skip_pending=True):
        # the first poll that waits on the server comes after the skipping
        await eventually(
            lambda: any(call["query"].get("timeout") == "1" for call in fake.calls("GET", "/bot/v1/updates"))
        )
        fake.push_message(7, "fresh")
        await asyncio.wait_for(fresh.wait(), 5)

    assert seen == ["fresh"]


def test_run_returns_quietly_on_ctrl_c(make_bot, monkeypatch):
    bot = make_bot()
    started: list[dict[str, Any]] = []

    async def interrupted(**options: Any) -> None:
        started.append(options)
        raise KeyboardInterrupt

    monkeypatch.setattr(bot, "start_polling", interrupted)
    bot.run(poll_timeout=7, configure_logging=False)

    assert started == [{"poll_timeout": 7, "limit": 100, "drop_webhook": False, "skip_pending": False}]


async def test_start_waits_for_a_server_that_is_down(fake, new_bot, monkeypatch, caplog):
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def no_wait(delay: float, *args: Any, **kwargs: Any) -> Any:
        slept.append(delay)
        return await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    # four attempts per call with FAST_RETRY: the first boot try fails, the second gets through
    fake.fail("GET", r"/me$", status=503, code="unavailable", times=5)
    bot = new_bot()

    await bot.start()

    assert bot.me is not None
    assert bot.me.username == "testbot"
    assert [delay for delay in slept if delay] == [2.0]
    assert "not answering" in caplog.text


async def test_start_gives_up_at_once_on_a_revoked_token(fake, new_bot):
    fake.revoked = True
    bot = new_bot()

    with pytest.raises(AuthenticationError):
        await bot.start()
    assert len(fake.calls("GET", "/bot/v1/me")) == 1


async def test_paused_bot_is_warned_about_at_start(fake, new_bot, caplog):
    fake.me["status"] = "paused"
    bot = new_bot()

    await bot.start()

    assert bot.me.paused
    assert "paused" in caplog.text


async def test_starting_twice_is_harmless(fake, new_bot):
    bot = new_bot()

    await bot.start()
    await bot.start()

    assert len(fake.calls("GET", "/bot/v1/me")) == 1


async def test_two_bots_cannot_poll_with_the_same_storage(fake, new_bot):
    fake.add_chat(7)
    shared = MemoryStorage()
    first = new_bot(storage=shared)
    second = new_bot(storage=shared)
    handled = asyncio.Event()

    @first.on_message()
    async def handle(message):
        handled.set()

    async with polling(first):
        await eventually(lambda: fake.calls("GET", "/bot/v1/updates"))
        with pytest.raises(PollingConflictError):
            await second.start_polling(poll_timeout=1)
        # the bot that got there first is not disturbed
        fake.push_message(7, "still here?")
        await asyncio.wait_for(handled.wait(), 5)


async def test_polling_works_on_a_file_storage(fake, new_bot, tmp_path):
    fake.add_chat(7)
    bot = new_bot(storage=tmp_path / "updates.db")
    handled = asyncio.Event()

    @bot.on_message()
    async def handle(message):
        await message.reply("stored and handled")
        handled.set()

    async with polling(bot):
        fake.push_message(7, "hello")
        await asyncio.wait_for(handled.wait(), 5)
        await eventually(lambda: not fake.queue)

    assert [p["text"] for p in posts(fake)] == ["stored and handled"]


async def test_sigterm_stops_polling_and_puts_the_old_handler_back(fake, new_bot):
    before = signal.getsignal(signal.SIGTERM)
    bot = new_bot()
    task = asyncio.create_task(bot.start_polling(poll_timeout=1))
    await eventually(lambda: fake.calls("GET", "/bot/v1/updates"))

    installed = signal.getsignal(signal.SIGTERM)
    # never raise it unless the bot's own handler is there to catch it
    assert "_install_signals" in getattr(installed, "__qualname__", "")
    signal.raise_signal(signal.SIGTERM)
    await asyncio.wait_for(task, 5)

    assert signal.getsignal(signal.SIGTERM) == before


def test_run_sets_up_logging_only_for_an_app_that_has_none(make_bot, monkeypatch):
    bot = make_bot()

    async def quick(**options: Any) -> None:
        return None

    monkeypatch.setattr(bot, "start_polling", quick)
    configured: list[dict[str, Any]] = []
    monkeypatch.setattr(logging, "basicConfig", lambda **options: configured.append(options))
    root = logging.getLogger()

    # pytest's own handlers sit on the root logger: the app has logging
    bot.run()
    assert configured == []

    saved = root.handlers[:]
    root.handlers.clear()
    try:
        bot.run(configure_logging=False)
        assert configured == []
        bot.run()
    finally:
        root.handlers[:] = saved
    [options] = configured
    assert options["level"] == logging.INFO


async def test_bot_as_an_async_context_manager_starts_and_stops(fake, make_bot):
    async with make_bot() as bot:
        assert bot.me is not None
        assert bot.me.id == BOT_ID
        # while running the bot holds its storage
        assert not bot.storage.try_lock()

    assert bot.storage.try_lock()
    bot.storage.unlock()


async def test_send_into_an_open_chat_is_plain_text(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()

    sent = await bot.send_message(7, "hello", reply_to=1234, silent=True)

    [post] = posts(fake)
    assert post["chatId"] == 7
    assert post["text"] == "hello"
    assert "armored" not in post
    assert post["replyTo"] == 1234
    assert post["silent"] is True
    assert post["sendId"]
    assert isinstance(sent, SentMessage)
    assert sent.text == "hello"
    assert not sent.encrypted


async def test_send_into_a_protected_private_chat_is_sealed_and_signed_by_the_bot(
    fake, new_bot, bot_key, anna_row, phone_secret
):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[anna_row])
    bot = new_bot(pgp_private_key=bot_key)

    sent = await bot.send_message(7, "the forecast says rain")

    [post] = posts(fake)
    assert "text" not in post
    armored = post["armored"]
    assert armored.startswith("-----BEGIN PGP MESSAGE-----")
    opened = phone_secret.decrypt(armored, verify_with=[bot_key.public_key])
    assert opened.text == "the forecast says rain"
    assert opened.verification.valid
    assert opened.verification.signer_fingerprint == bot_key.fingerprint
    assert sent.encrypted
    assert sent.armored == armored


async def test_send_into_a_protected_group_is_sealed_to_every_listed_key(
    fake, new_bot, bot_key, anna_row, bruno_row, phone_secret, rsa_secret, stranger_key
):
    fake.add_chat(900, "group", title="Team", history_visible=False, keys=[anna_row, bruno_row])
    bot = new_bot(pgp_private_key=bot_key)

    await bot.send_message(900, "standup at nine")

    armored = posts(fake)[0]["armored"]
    for reader in (phone_secret, rsa_secret, bot_key):
        assert reader.decrypt(armored).text == "standup at nine"
    with pytest.raises(NotARecipientError):
        stranger_key.decrypt(armored)
    assert len(fake.calls("GET", "/bot/v1/chats/900/keys")) == 1


async def test_send_into_an_open_group_is_plain_text(fake, new_bot, bot_key):
    fake.add_chat(900, "group", title="Lobby", history_visible=True)
    bot = new_bot(pgp_private_key=bot_key)

    sent = await bot.send_message(900, "welcome")

    assert posts(fake) == [{"chatId": 900, "text": "welcome", "sendId": posts(fake)[0]["sendId"]}]
    assert sent.text == "welcome"
    assert fake.calls("GET", "/bot/v1/chats/900/keys") == []


async def test_unusable_member_keys_are_left_out_of_a_group_with_a_warning(
    fake, new_bot, bot_key, anna_row, unusable_row, phone_secret, caplog
):
    kinds = ["garbage", "v6", "mislabelled", "sign-only"]
    keys = [anna_row, *(unusable_row(kind, 77 + i) for i, kind in enumerate(kinds))]
    fake.add_chat(900, "group", title="Team", history_visible=False, keys=keys)
    bot = new_bot(pgp_private_key=bot_key)

    await bot.send_message(900, "still delivered")

    assert phone_secret.decrypt(posts(fake)[0]["armored"]).text == "still delivered"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and r.name == "pgpcbot.bot"]
    for user_id in (77, 78, 79, 80):
        assert any(f"leaving out user {user_id}" in w for w in warnings), warnings


@pytest.mark.parametrize("kind", ["garbage", "v6", "mislabelled", "sign-only"])
async def test_unusable_key_in_a_private_chat_refuses_to_send(fake, new_bot, bot_key, unusable_row, kind):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[unusable_row(kind, 55)])
    bot = new_bot(pgp_private_key=bot_key)

    with pytest.raises(EncryptionError, match="user 55"):
        await bot.send_message(7, "for your eyes only")
    assert posts(fake) == []


@pytest.mark.parametrize("own_key_listed", [False, True], ids=["no keys", "only the bot's own"])
async def test_nobody_to_seal_to_refuses_to_send(fake, new_bot, bot_key, own_key_listed):
    keys = [key_row(BOT_ID, "bot.pub.asc", bot_key.fingerprint)] if own_key_listed else []
    fake.add_chat(900, "group", title="Team", history_visible=False, keys=keys)
    bot = new_bot(pgp_private_key=bot_key)

    with pytest.raises(EncryptionError, match="nobody"):
        await bot.send_message(900, "hello?")
    assert posts(fake) == []


async def test_private_chat_whose_user_has_no_key_refuses_to_send(fake, new_bot, bot_key):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[])
    bot = new_bot(pgp_private_key=bot_key)

    with pytest.raises(NoUserKeyError):
        await bot.send_message(7, "hello")
    assert posts(fake) == []


async def test_protected_chat_without_a_bot_key_refuses_to_send(fake, new_bot, anna_row):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[anna_row])
    bot = new_bot()

    with pytest.raises(EncryptionError, match="no private key"):
        await bot.send_message(7, "hello")
    assert posts(fake) == []


@pytest.mark.parametrize(
    ("options", "send_options"),
    [({"require_encryption": True}, {}), ({}, {"encrypt": True})],
    ids=["require_encryption", "encrypt=True"],
)
async def test_open_chat_refuses_a_message_that_must_be_encrypted(fake, new_bot, bot_key, options, send_options):
    fake.add_chat(7)
    bot = new_bot(pgp_private_key=bot_key, **options)

    with pytest.raises(EncryptionRequiredError):
        await bot.send_message(7, "the vault code", **send_options)
    assert posts(fake) == []


async def test_encrypt_false_in_a_protected_chat_is_refused(fake, new_bot, bot_key, anna_row):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[anna_row])
    bot = new_bot(pgp_private_key=bot_key)

    with pytest.raises(ValidationError, match="encrypt=False"):
        await bot.send_message(7, "in clear please", encrypt=False)
    assert posts(fake) == []


@pytest.mark.parametrize("kind", ["private", "group"])
async def test_chat_that_turned_protected_gets_a_sealed_resend_with_the_same_send_id(
    fake, new_bot, bot_key, anna_row, phone_secret, caplog, kind
):
    if kind == "private":
        chat = fake.add_chat(7, keys=[anna_row])
    else:
        chat = fake.add_chat(7, "group", title="Team", history_visible=True, keys=[anna_row])
    bot = new_bot(pgp_private_key=bot_key)
    await bot.get_me()
    await bot.get_chat(7)
    # meanwhile the chat became end-to-end encrypted
    if kind == "private":
        fake.me["mode"] = "protected"
    else:
        chat.history_visible = False

    sent = await bot.send_message(7, "still there?")

    clear, sealed = posts(fake)
    assert clear["text"] == "still there?"
    assert "text" not in sealed
    assert sealed["sendId"] == clear["sendId"]
    assert phone_secret.decrypt(sealed["armored"]).text == "still there?"
    assert sent.encrypted
    assert "sealing and sending again" in caplog.text


async def test_chat_that_turned_readable_is_never_sent_in_clear_on_its_own(fake, new_bot, bot_key, anna_row):
    chat = fake.add_chat(900, "group", title="Team", history_visible=False, keys=[anna_row])
    bot = new_bot(pgp_private_key=bot_key)
    await bot.send_message(900, "sealed hello")
    chat.history_visible = True

    with pytest.raises(WrongBodyKindError) as info:
        await bot.send_message(900, "secret plan")

    assert info.value.expected == "text"
    assert [sorted(p) for p in posts(fake)] == [["armored", "chatId", "sendId"]] * 2
    # the refusal is remembered: the next send follows the new mode
    sent = await bot.send_message(900, "hello in clear")
    assert sent.text == "hello in clear"
    assert posts(fake)[-1]["text"] == "hello in clear"
    assert all(p.get("text") != "secret plan" for p in posts(fake))


async def test_chat_that_turned_readable_before_its_keys_were_fetched_takes_text_on_the_next_send(
    fake, new_bot, bot_key, anna_row
):
    chat = fake.add_chat(900, "group", title="Team", history_visible=False, keys=[anna_row])
    bot = new_bot(pgp_private_key=bot_key)
    await bot.get_chat(900)
    chat.history_visible = True

    with pytest.raises(BadRequestError):
        await bot.send_message(900, "first try")
    assert posts(fake) == []

    sent = await bot.send_message(900, "second try")
    assert sent.text == "second try"


async def test_edit_in_a_chat_that_turned_protected_is_sealed_and_sent_again(
    fake, new_bot, bot_key, anna_row, phone_secret
):
    fake.add_chat(7, keys=[anna_row])
    bot = new_bot(pgp_private_key=bot_key)
    await bot.get_me()
    draft = await bot.send_message(7, "draft")
    fake.me["mode"] = "protected"

    final = await draft.edit("final words")

    clear, sealed = fake.calls("PATCH", f"/bot/v1/messages/{draft.id}")
    assert clear["json"] == {"chatId": 7, "text": "final words"}
    assert "text" not in sealed["json"]
    assert phone_secret.decrypt(sealed["json"]["armored"]).text == "final words"
    assert final.encrypted
    assert isinstance(final, SentMessage)


async def test_edit_in_a_chat_that_turned_readable_is_not_resent_in_clear(fake, new_bot, bot_key, anna_row):
    chat = fake.add_chat(900, "group", title="Team", history_visible=False, keys=[anna_row])
    bot = new_bot(pgp_private_key=bot_key)
    draft = await bot.send_message(900, "sealed draft")
    chat.history_visible = True

    with pytest.raises(WrongBodyKindError):
        await draft.edit("final words")

    [patch] = fake.calls("PATCH", f"/bot/v1/messages/{draft.id}")
    assert "armored" in patch["json"]
    assert "text" not in patch["json"]


async def test_sent_message_can_be_edited_and_deleted(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()
    keyboard = InlineKeyboard.build([Button.callback("Undo", "undo")])
    draft = await bot.send_message(7, "draft", markup=keyboard)

    final = await draft.edit("final")

    plain = await final.edit(markup=None)

    first, second = fake.calls("PATCH", f"/bot/v1/messages/{draft.id}")
    assert first["json"] == {"chatId": 7, "text": "final"}
    assert second["json"] == {"chatId": 7, "markup": None}
    assert final.text == "final"
    assert final.markup is not None
    assert plain.markup is None
    assert await plain.delete() is True
    assert draft.id not in fake.messages


async def test_seal_encrypts_for_the_chat_without_sending(fake, new_bot, bot_key, anna_row, phone_secret, phone_key):
    fake.add_chat(900, "group", title="Team", history_visible=False, keys=[anna_row])
    bot = new_bot(pgp_private_key=bot_key)

    recipients = await bot.recipients(900)
    armored = await bot.seal(900, "for later")

    assert [key.fingerprint for key in recipients] == [phone_key.fingerprint]
    assert phone_secret.decrypt(armored).text == "for later"
    assert posts(fake) == []


@pytest.mark.parametrize(("cache_ttl", "lookups"), [(60.0, 1), (0.0, 2)])
async def test_chat_mode_is_looked_up_once_within_cache_ttl(fake, new_bot, cache_ttl, lookups):
    fake.add_chat(900, "group", title="Lobby", history_visible=True)
    bot = new_bot(cache_ttl=cache_ttl)

    await bot.send_message(900, "one")
    await bot.send_message(900, "two")

    assert len(fake.calls("GET", "/bot/v1/chats/900")) == lookups
    assert len(posts(fake)) == 2


async def test_get_chat_is_cached_until_refreshed(fake, new_bot):
    fake.add_chat(900, "group", title="Lobby", history_visible=True)
    bot = new_bot()

    first = await bot.get_chat(900)
    again = await bot.get_chat(900)
    fake.chats[900].title = "Renamed"
    fresh = await bot.get_chat(900, refresh=True)

    assert again is first
    assert (first.kind, first.mode, first.title) == (ChatKind.GROUP, ChatMode.OPEN, "Lobby")
    assert fresh.title == "Renamed"
    assert len(fake.calls("GET", "/bot/v1/chats/900")) == 2


async def test_send_normalises_line_breaks(fake, new_bot):
    fake.add_chat(7)
    bot = new_bot()

    await bot.send_message(7, "line one\r\nline two\rline three")

    assert posts(fake)[0]["text"] == "line one\nline two\nline three"


async def test_text_limit_comes_from_the_server(fake, new_bot):
    fake.me["limits"]["textMax"] = 10
    fake.add_chat(7)
    bot = new_bot()
    await bot.get_me()

    with pytest.raises(ValidationError, match="limit is 10"):
        await bot.send_message(7, "x" * 11)
    assert posts(fake) == []
    await bot.send_message(7, "x" * 10)
    assert posts(fake)[0]["text"] == "x" * 10


async def test_signed_message_from_a_phone_is_opened_and_verified(fake, new_bot, bot_key, anna_row, phone_key):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[anna_row])
    bot = new_bot(pgp_private_key=bot_key)
    received: list[Message] = []

    @bot.on_message(filters.encrypted & filters.signed)
    async def got(message):
        received.append(message)

    fake.push_message(7, armored=fixture_text("msg_text_signed.asc"))
    await bot.process_update(last_update(fake))

    [message] = received
    assert message.text == PHONE_TEXT
    assert message.plaintext == PHONE_TEXT
    assert message.encrypted
    assert message.armored == fixture_text("msg_text_signed.asc")
    assert message.verification.status == SignatureStatus.VALID
    assert message.verification.signer_fingerprint == phone_key.fingerprint


async def test_required_signatures_refuse_an_unsigned_message(fake, new_bot, bot_key, anna_row):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[anna_row])
    bot = new_bot(pgp_private_key=bot_key, verify_signatures="require")
    handled: list[str] = []
    refused: list[tuple[BaseException, Update]] = []

    @bot.on_message()
    async def got(message):
        handled.append(message.text)

    @bot.on_error(SignatureError)
    async def unsigned(error, update):
        refused.append((error, update))

    fake.push_message(7, armored=fixture_text("msg_text_unsigned.asc"))
    await bot.process_update(last_update(fake))
    fake.push_message(7, armored=fixture_text("msg_text_signed.asc"))
    await bot.process_update(last_update(fake))

    [(error, update)] = refused
    assert "unsigned" in str(error)
    assert update.message.text is None
    assert handled == [PHONE_TEXT]


async def test_verification_off_does_not_fetch_keys(fake, new_bot, bot_key, anna_row):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[anna_row])
    bot = new_bot(pgp_private_key=bot_key, verify_signatures="off")
    received: list[Message] = []

    @bot.on_message()
    async def got(message):
        received.append(message)

    fake.push_message(7, armored=fixture_text("msg_text_signed.asc"))
    await bot.process_update(last_update(fake))

    [message] = received
    assert message.text == PHONE_TEXT
    assert message.verification.status == SignatureStatus.NOT_CHECKED
    assert fake.calls("GET", "/bot/v1/chats/7/keys") == []


@pytest.mark.parametrize("fault", [{"status": 503, "code": "unavailable"}, {"network": True}], ids=["5xx", "network"])
async def test_keys_that_cannot_be_fetched_leave_the_signature_unchecked(fake, new_bot, bot_key, anna_row, fault):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[anna_row])
    fake.fail("GET", r"/chats/7/keys$", times=10, **fault)
    bot = new_bot(pgp_private_key=bot_key)
    received: list[Message] = []

    @bot.on_message()
    async def got(message):
        received.append(message)

    fake.push_message(7, armored=fixture_text("msg_text_signed.asc"))
    await bot.process_update(last_update(fake))

    [message] = received
    assert message.text == PHONE_TEXT
    assert message.verification.status == SignatureStatus.NOT_CHECKED
    assert message.verification.detail == "the chat's keys could not be fetched"


@pytest.mark.parametrize(
    ("from_id", "listed", "detail"),
    [
        (None, "anna", "a post that speaks as the channel carries no sender to check against"),
        (66, "anna", "the server lists no key for the sender in this chat"),
        (55, "garbage", "the sender's key is unreadable"),
    ],
    ids=["anonymous channel post", "sender without a listed key", "unreadable sender key"],
)
async def test_signature_is_left_unchecked_when_the_sender_has_no_usable_key(
    fake, new_bot, bot_key, anna_row, unusable_row, from_id, listed, detail
):
    keys = [anna_row] if listed == "anna" else [unusable_row("garbage", 55)]
    fake.add_chat(300, "channel", title="News", history_visible=False, keys=keys)
    bot = new_bot(pgp_private_key=bot_key)
    received: list[Message] = []

    @bot.on_message()
    async def got(message):
        received.append(message)

    fake.push_message(300, armored=fixture_text("msg_text_signed.asc"), from_id=from_id)
    await bot.process_update(last_update(fake))

    [message] = received
    assert message.text == PHONE_TEXT
    assert message.verification.status == SignatureStatus.NOT_CHECKED
    assert message.verification.detail.startswith(detail)


async def test_crlf_text_from_a_phone_reads_with_plain_newlines(fake, new_bot, bot_key, anna_row, phone_secret):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[anna_row])
    bot = new_bot(pgp_private_key=bot_key)
    received: list[Message] = []

    @bot.on_message()
    async def got(message):
        received.append(message)

    # OpenPGP.js writes text literals with CRLF
    fake.push_message(7, armored=phone_secret.seal("line one\r\nline two", [bot_key.public_key]))
    await bot.process_update(last_update(fake))

    [message] = received
    assert message.text == "line one\nline two"
    assert message.plaintext == "line one\r\nline two"
    assert message.verification.valid


async def test_encrypted_message_for_a_bot_without_a_key_goes_to_the_error_handlers(fake, new_bot, anna_row):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[anna_row])
    bot = new_bot()
    errors: list[BaseException] = []

    @bot.on_error(DecryptionError)
    async def cannot_open(error):
        errors.append(error)

    fake.push_message(7, armored=fixture_text("msg_text_signed.asc"))
    await bot.process_update(last_update(fake))

    [error] = errors
    assert "no private key" in str(error)


async def test_message_not_sealed_to_the_bot_goes_to_the_error_handlers(fake, new_bot, bot_key, anna_row):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[anna_row])
    bot = new_bot(pgp_private_key=bot_key)
    handled: list[str] = []
    errors: list[tuple[BaseException, Update]] = []

    @bot.on_message()
    async def got(message):
        handled.append(message.text)

    @bot.on_error(NotARecipientError)
    async def not_for_us(error, update):
        errors.append((error, update))

    fake.push_message(7, armored=fixture_text("msg_not_for_bot.asc"))
    await bot.process_update(last_update(fake))

    [(error, update)] = errors
    assert bot_key.fingerprint in str(error)
    assert update.message.encrypted
    assert update.message.text is None
    assert handled == []


async def test_previous_keys_still_open_messages_sealed_to_them(fake, new_bot, bot_key, old_bot_key, anna_row):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[anna_row])
    rotated = new_bot(pgp_private_key=bot_key, pgp_previous_keys=[old_bot_key])
    forgetful = new_bot(pgp_private_key=bot_key)
    received: list[str] = []

    @rotated.on_message()
    async def got(message):
        received.append(message.text)

    fake.push_message(7, armored=fixture_text("msg_for_old_key.asc"))
    await rotated.process_update(last_update(fake))
    with pytest.raises(NotARecipientError, match="pgp_previous_keys"):
        await forgetful.process_update(last_update(fake))

    assert received == [PHONE_TEXT]


async def test_encrypted_photo_comes_with_its_caption_and_downloads_decrypted(fake, new_bot, bot_key, anna_row):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[anna_row])
    fake.media[MEDIA_ID] = (FIXTURES / "attachment.pgp").read_bytes()
    bot = new_bot(pgp_private_key=bot_key)
    photos: list[Message] = []

    @bot.on_message(filters.has_media)
    async def photo(message):
        photos.append(message)

    fake.push_message(7, armored=fixture_text("msg_media.asc"))
    await bot.process_update(last_update(fake))

    [message] = photos
    assert message.text == "guarda che tramonto"
    [media] = message.media
    assert (media.id, media.kind, media.mime, media.encrypted) == (MEDIA_ID, "image", "image/jpeg", True)
    assert await bot.download_media(media) == ATTACHMENT
    [download] = fake.calls("GET", f"/media/{MEDIA_ID}")
    # media is fetched without the bot token
    assert "authorization" not in download["headers"]


async def test_media_of_an_open_chat_is_downloaded_as_it_is(fake, new_bot):
    media_id = "5d1f0000-aaaa-bbbb-cccc-000000000001"
    fake.media[media_id] = b"plain jpeg bytes"
    bot = new_bot()

    assert await bot.download_media(Media(id=media_id, kind="image")) == b"plain jpeg bytes"
    assert await bot.download_media(media_id) == b"plain jpeg bytes"


async def test_first_sight_pins_the_senders_key(fake, new_bot, bot_key, anna_row, phone_key):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[anna_row])
    bot = new_bot(pgp_private_key=bot_key, pin_keys=True)
    received: list[Message] = []

    @bot.on_message()
    async def got(message):
        received.append(message)

    fake.push_message(7, armored=fixture_text("msg_text_signed.asc"))
    await bot.process_update(last_update(fake))

    assert bot.storage.get_pin(55) == phone_key.fingerprint
    assert received[0].verification.valid
    await bot.send_message(7, "same key, no fuss")
    assert len(posts(fake)) == 1


async def test_changed_key_stops_sends_until_it_is_trusted(
    fake, new_bot, bot_key, anna_row, phone_key, rsa_phone_key, rsa_secret
):
    fake.me["mode"] = "protected"
    chat = fake.add_chat(7, keys=[anna_row])
    # no caching, so the bot sees the new key on the very next send
    bot = new_bot(pgp_private_key=bot_key, pin_keys=True, cache_ttl=0)
    await bot.send_message(7, "first")
    chat.keys = [key_row(55, "phone_rsa.pub.asc", rsa_phone_key.fingerprint)]

    with pytest.raises(KeyChangedError) as info:
        await bot.send_message(7, "second")

    assert (info.value.user_id, info.value.pinned, info.value.current) == (
        55,
        phone_key.fingerprint,
        rsa_phone_key.fingerprint,
    )
    assert len(posts(fake)) == 1

    await bot.trust_key(55, rsa_phone_key.fingerprint)
    await bot.send_message(7, "third")
    assert rsa_secret.decrypt(posts(fake)[-1]["armored"]).text == "third"
    assert bot.storage.get_pin(55) == rsa_phone_key.fingerprint


async def test_changed_key_refuses_incoming_messages_until_trusted(
    fake, new_bot, bot_key, anna_row, phone_key, rsa_phone_key
):
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[anna_row])
    bot = new_bot(pgp_private_key=bot_key, pin_keys=True)
    # pinned back when anna had another phone
    bot.storage.set_pin(55, rsa_phone_key.fingerprint)
    received: list[Message] = []
    refused: list[KeyChangedError] = []

    @bot.on_message()
    async def got(message):
        received.append(message)

    @bot.on_error(KeyChangedError)
    async def changed(error):
        refused.append(error)

    fake.push_message(7, armored=fixture_text("msg_text_signed.asc"))
    await bot.process_update(last_update(fake))
    # pinning refuses a changed key both ways: the handler never saw the message
    assert received == []
    [error] = refused
    assert (error.user_id, error.pinned, error.current) == (55, rsa_phone_key.fingerprint, phone_key.fingerprint)

    await bot.trust_key(55, phone_key.fingerprint)
    fake.push_message(7, armored=fixture_text("msg_text_signed.asc"))
    await bot.process_update(last_update(fake))

    [message] = received
    assert message.text == PHONE_TEXT
    assert message.verification.valid


async def test_publish_key_puts_the_public_key_and_seals_private_chats_from_then_on(
    fake, new_bot, bot_key, anna_row, phone_secret
):
    fake.add_chat(7, keys=[anna_row])
    bot = new_bot(pgp_private_key=bot_key)
    await bot.get_chat(7)

    result = await bot.publish_key()

    [put] = fake.calls("PUT", "/bot/v1/key")
    assert put["json"] == {"publicKey": bot_key.export_public(), "fingerprint": bot_key.fingerprint}
    assert (result.fingerprint, result.mode) == (bot_key.fingerprint, ChatMode.PROTECTED)
    assert bot.me.protected
    assert bot.me.fingerprint == bot_key.fingerprint
    # the mode cached as open is gone: the very first post is sealed, no refused clear one
    await bot.send_message(7, "sealed from now on")
    [post] = posts(fake)
    assert phone_secret.decrypt(post["armored"]).text == "sealed from now on"


async def test_get_chat_reports_private_chats_protected_after_publish_key(fake, new_bot, bot_key):
    fake.add_chat(7)
    bot = new_bot(pgp_private_key=bot_key)
    assert (await bot.get_chat(7)).mode == ChatMode.OPEN

    await bot.publish_key()

    assert (await bot.get_chat(7)).mode == ChatMode.PROTECTED


@pytest.mark.parametrize(("published", "puts"), [(None, 1), ("same", 0), ("old", 1)])
async def test_publish_key_on_start_only_when_the_server_has_another_key(
    fake, new_bot, bot_key, old_bot_key, published, puts
):
    if published is not None:
        key = bot_key if published == "same" else old_bot_key
        fake.me.update(mode="protected", fingerprint=key.fingerprint)
    bot = new_bot(pgp_private_key=bot_key, publish_key=True)

    await bot.start()

    assert len(fake.calls("PUT", "/bot/v1/key")) == puts
    assert bot.me.protected
    assert bot.me.fingerprint == bot_key.fingerprint


async def test_unpublished_key_is_warned_about_at_start(fake, new_bot, bot_key, caplog):
    bot = new_bot(pgp_private_key=bot_key)

    await bot.start()

    assert "no published key" in caplog.text
    assert fake.calls("PUT", "/bot/v1/key") == []


async def test_key_other_than_the_published_one_is_warned_about_at_start(fake, new_bot, bot_key, old_bot_key, caplog):
    fake.me.update(mode="protected", fingerprint=old_bot_key.fingerprint)
    bot = new_bot(pgp_private_key=bot_key)

    await bot.start()

    assert old_bot_key.fingerprint in caplog.text
    assert bot_key.fingerprint in caplog.text
    assert fake.calls("PUT", "/bot/v1/key") == []


async def test_protected_bot_without_a_key_is_warned_about_at_start(fake, new_bot, bot_key, caplog):
    fake.me.update(mode="protected", fingerprint=bot_key.fingerprint)
    bot = new_bot()

    await bot.start()

    assert "no private key was given" in caplog.text


async def test_rotate_key_publishes_the_new_key_and_keeps_opening_old_messages(
    fake, new_bot, bot_key, old_bot_key, anna_row
):
    fake.me.update(mode="protected", fingerprint=old_bot_key.fingerprint)
    fake.add_chat(7, keys=[anna_row])
    bot = new_bot(pgp_private_key=old_bot_key)
    received: list[str] = []

    @bot.on_message()
    async def got(message):
        received.append(message.text)

    result = await bot.rotate_key(bot_key)
    for fixture in ("msg_for_old_key.asc", "msg_text_signed.asc"):
        fake.push_message(7, armored=fixture_text(fixture))
        await bot.process_update(last_update(fake))

    assert result.fingerprint == bot_key.fingerprint
    assert [put["json"]["fingerprint"] for put in fake.calls("PUT", "/bot/v1/key")] == [bot_key.fingerprint]
    assert bot.key is bot_key
    assert received == [PHONE_TEXT, PHONE_TEXT]


async def test_v6_key_is_never_published(fake, new_bot, bot_key, v6_key):
    bot = new_bot(pgp_private_key=bot_key)

    with pytest.raises(InvalidKeyError, match="v4"):
        await bot.publish_key(v6_key)
    with pytest.raises(InvalidKeyError):
        await bot.rotate_key(v6_key)

    assert bot.key is bot_key
    assert fake.calls("PUT", "/bot/v1/key") == []


async def test_publish_key_without_any_key_is_a_configuration_error(fake, new_bot):
    bot = new_bot()

    with pytest.raises(ConfigurationError, match="no key to publish"):
        await bot.publish_key()
    assert fake.calls("PUT", "/bot/v1/key") == []


async def test_sync_commands_publishes_every_described_command(fake, new_bot):
    bot = new_bot()

    @bot.command("start", description="Say hello")
    async def start(message):
        pass

    @bot.command("weather", "w", description="Forecast for a city")
    async def weather(message):
        pass

    @bot.command("debug")
    async def debug(message):
        pass

    scope = await bot.sync_commands()

    expected = [("start", "Say hello"), ("weather", "Forecast for a city"), ("w", "Forecast for a city")]
    assert [(c.command, c.description) for c in scope.commands] == expected
    assert fake.commands["default"] == [{"command": c, "description": d} for c, d in expected]


async def test_sync_commands_without_descriptions_is_a_configuration_error(fake, new_bot):
    bot = new_bot()

    @bot.command("start")
    async def start(message):
        pass

    with pytest.raises(ConfigurationError, match="description"):
        await bot.sync_commands()
    assert fake.calls("PUT", "/bot/v1/commands") == []


async def test_get_me_is_kept_until_refreshed(fake, new_bot):
    bot = new_bot()

    first = await bot.get_me()
    again = await bot.get_me()
    fake.me["name"] = "Renamed"
    fresh = await bot.get_me(refresh=True)

    assert again is first
    assert (first.id, first.username, first.name) == (BOT_ID, "testbot", "Test")
    assert fresh.name == "Renamed"
    assert len(fake.calls("GET", "/bot/v1/me")) == 2
    assert bot.username == "testbot"


async def test_webhook_can_be_managed_through_the_bot(fake, new_bot):
    bot = new_bot()

    created = await bot.set_webhook("https://bot.example/hook", "Shared_secret-0123456789abcdef")
    current = await bot.get_webhook()
    removed = await bot.delete_webhook()

    assert (created.url, created.has_secret) == ("https://bot.example/hook", True)
    assert current.url == "https://bot.example/hook"
    assert removed.url is None
    assert fake.webhook["url"] is None


async def test_command_menus_can_be_managed_through_the_bot(fake, new_bot):
    bot = new_bot()

    await bot.set_commands([("start", "Say hello")])
    listed = await bot.get_commands("default")
    cleared = await bot.delete_commands()

    assert [(c.command, c.description) for c in listed] == [("start", "Say hello")]
    assert cleared.commands == ()
    assert "default" not in fake.commands


async def test_chat_and_key_lookups_go_through_the_bot(fake, new_bot, anna_row, phone_key):
    anna = {"id": 55, "username": "anna", "firstName": "Anna", "isBot": False}
    members = [{"user": anna, "status": "admin", "adminRights": 1, "can": {"deleteMessages": True}}]
    fake.add_chat(900, "group", title="Team", history_visible=False, keys=[anna_row], members=members)
    fake.user_keys[55] = anna_row
    bot = new_bot()

    assert isinstance(bot.api, AsyncClient)
    keys = await bot.get_chat_keys(900)
    roster = await bot.get_chat_members(900)
    member = await bot.get_chat_member(900, 55)
    user_key = await bot.get_user_key(55)

    assert [k.user_id for k in keys.keys] == [55]
    assert keys.member_count == 3
    assert [m.user.id for m in roster.members] == [55]
    assert member.is_admin
    assert member.can.delete_messages
    assert user_key.fingerprint == phone_key.fingerprint


async def test_revoke_token_burns_it(fake, new_bot):
    bot = new_bot()

    assert await bot.revoke_token() is True

    assert fake.revoked
    with pytest.raises(AuthenticationError):
        await bot.get_me(refresh=True)


async def test_leave_chat_forgets_what_the_bot_knew_about_it(fake, new_bot):
    fake.add_chat(900, "group", title="Team", history_visible=True)
    bot = new_bot()
    await bot.get_chat(900)

    assert await bot.leave_chat(900) is True

    # nothing cached any more: the server is asked, and the bot is not in it
    with pytest.raises(ChatNotFoundError):
        await bot.get_chat(900)
    assert len(fake.calls("GET", "/bot/v1/chats/900")) == 2


@pytest.mark.parametrize("source", ["env", "env file", "callable"])
async def test_token_can_come_from_the_environment_or_a_provider(fake, monkeypatch, tmp_path, source):
    token: Any = None
    if source == "env":
        monkeypatch.setenv("PGPCHAT_BOT_TOKEN", TOKEN)
    elif source == "env file":
        path = tmp_path / "token"
        path.write_text(TOKEN + "\n", encoding="ascii")
        monkeypatch.setenv("PGPCHAT_BOT_TOKEN_FILE", str(path))
    else:

        def provider() -> str:
            return TOKEN

        token = provider
    bot = Bot(token, api_url=BASE, transport=fake, retry=FAST_RETRY, rate_limit=False)
    try:
        await bot.get_me()
    finally:
        await bot.stop()

    assert bot.bot_id == BOT_ID
    [call] = fake.calls("GET", "/bot/v1/me")
    assert call["headers"]["authorization"] == f"Bearer {TOKEN}"


def test_missing_token_is_a_configuration_error(fake):
    with pytest.raises(ConfigurationError, match="PGPCHAT_BOT_TOKEN"):
        Bot(api_url=BASE, transport=fake)


@pytest.mark.parametrize("form", ["path", "armored"])
def test_private_key_can_come_from_the_environment(fake, monkeypatch, bot_key, form):
    value = str(FIXTURES / "bot.sec.asc") if form == "path" else fixture_text("bot.sec.asc")
    monkeypatch.setenv("PGPCHAT_BOT_KEY", value)

    bot = Bot(TOKEN, api_url=BASE, transport=fake)

    assert bot.key is not None
    assert bot.key.fingerprint == bot_key.fingerprint


def test_locked_private_key_needs_its_passphrase(fake, monkeypatch):
    locked = FIXTURES / "bot_locked.sec.asc"

    with pytest.raises(PassphraseError):
        Bot(TOKEN, api_url=BASE, transport=fake, pgp_private_key=locked)
    assert Bot(TOKEN, api_url=BASE, transport=fake, pgp_private_key=str(locked), pgp_passphrase="correct horse").key

    monkeypatch.setenv("PGPCHAT_BOT_KEY", str(locked))
    monkeypatch.setenv("PGPCHAT_BOT_KEY_PASSPHRASE", "correct horse")
    from_env = Bot(TOKEN, api_url=BASE, transport=fake)
    assert from_env.key is not None
    assert from_env.key.protected


@pytest.mark.parametrize("form", ["key object", "path", "path string", "armored text", "armored bytes"])
def test_private_key_argument_takes_a_key_a_path_or_armored_text(make_bot, bot_key, form):
    path = FIXTURES / "bot.sec.asc"
    value = {
        "key object": bot_key,
        "path": path,
        "path string": str(path),
        "armored text": path.read_text(encoding="ascii"),
        "armored bytes": path.read_bytes(),
    }[form]

    bot = make_bot(pgp_private_key=value)

    assert bot.key is not None
    assert bot.key.fingerprint == bot_key.fingerprint


@pytest.mark.parametrize("value", ["no/such/key.asc", 42], ids=["missing file", "wrong type"])
def test_private_key_that_is_neither_a_key_nor_a_file_is_refused(fake, value):
    with pytest.raises(ConfigurationError, match="pgp_private_key"):
        Bot(TOKEN, api_url=BASE, transport=fake, pgp_private_key=value)


@pytest.mark.parametrize("options", [{"verify_signatures": "sometimes"}, {"concurrency": 0}])
def test_invalid_options_are_refused_up_front(make_bot, options):
    with pytest.raises(ConfigurationError):
        make_bot(**options)


def test_storage_given_as_a_path_is_an_sqlite_file(make_bot, tmp_path):
    bot = make_bot(storage=tmp_path / "updates.db")

    assert isinstance(bot.storage, SQLiteStorage)


def test_repr_names_the_bot_but_hides_the_token(make_bot):
    text = repr(make_bot())

    assert "412" in text
    assert TOKEN not in text
    assert "A" * 8 not in text


def test_bot_cannot_be_pickled(make_bot):
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(make_bot())
