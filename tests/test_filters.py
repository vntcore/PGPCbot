from __future__ import annotations

import dataclasses
import inspect
import re
from typing import Any

import pytest

from pgpcbot import CallbackQueryBase, ChatKind, MessageBase, ParsedCommand, SignatureStatus, Update, Verification
from pgpcbot import filters as f
from pgpcbot.filters import Filter, FilterContext, merge, parse_command

ANNA = {"id": 55, "username": "Anna_R", "firstName": "Anna", "lastName": None, "isBot": False}
CHATS = {
    "private": {"id": 7, "kind": "private"},
    "group": {"id": 300, "kind": "group", "title": "Dev", "username": "DevRoom"},
    "channel": {"id": 900, "kind": "channel", "title": "News", "username": None},
}


def message(text: str | None = "hello", *, chat: str = "private", **fields: Any) -> MessageBase:
    data: dict[str, Any] = {
        "id": 1,
        "chat": CHATS[chat],
        "from": ANNA,
        "date": 1791000000000,
        "kind": "user",
        "text": text,
        "media": [],
    }
    data.update(fields)
    return MessageBase.from_json(data)


def press(data: str = "page:2", *, chat: str = "private") -> CallbackQueryBase:
    return CallbackQueryBase.from_json(
        {"id": "q1", "from": ANNA, "chat": CHATS[chat], "messageId": 10, "data": data, "expiresAt": 1791000060000}
    )


def context(bot_username: str | None = "testbot") -> FilterContext:
    return FilterContext(bot_username=bot_username, update=None, bot=None)


async def run(flt: Any, event: Any, *, bot_username: str | None = "testbot") -> Any:
    result = f.as_filter(flt)(event, context(bot_username))
    if inspect.isawaitable(result):
        result = await result
    return result


async def passes(flt: Any, event: Any, *, bot_username: str | None = "testbot") -> dict[str, Any] | None:
    """The handler kwargs when the filter lets the event through, None when it does not."""
    data: dict[str, Any] = {}
    return data if merge(await run(flt, event, bot_username=bot_username), data) else None


def test_parse_command_splits_name_mention_and_args() -> None:
    parsed = parse_command("/start@weatherbot rome  milan")
    assert parsed == ParsedCommand(name="start", args="rome  milan", mention="weatherbot", token="/start@weatherbot")
    assert parsed.argv == ["rome", "milan"]


def test_parse_command_lowercases_the_name_but_not_the_mention() -> None:
    parsed = parse_command("/START@WeatherBot")
    assert parsed is not None
    assert (parsed.name, parsed.mention, parsed.token, parsed.args) == ("start", "WeatherBot", "/START@WeatherBot", "")


def test_parse_command_args_run_over_lines_and_are_trimmed() -> None:
    parsed = parse_command("/note\n  first line\nsecond line  ")
    assert parsed is not None
    assert parsed.args == "first line\nsecond line"
    assert parsed.argv == ["first", "line", "second", "line"]


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "/",
        "hello",
        " /start",
        "start",
        "/a-b",
        "/start@bot",  # a handle is at least 5 characters, so this is not a command at all
        "/start@weather-bot",
        "/" + "x" * 33,
        "/start@" + "b" * 33,
        "/città",
    ],
)
def test_parse_command_refuses_what_the_server_would_not_call_a_command(text: str | None) -> None:
    assert parse_command(text) is None


@pytest.mark.parametrize(
    ("text", "name", "mention"),
    [("/" + "x" * 32, "x" * 32, None), ("/go@abcde", "go", "abcde"), ("/go@" + "b" * 32, "go", "b" * 32)],
)
def test_parse_command_at_the_length_limits(text: str, name: str, mention: str | None) -> None:
    parsed = parse_command(text)
    assert parsed is not None
    assert (parsed.name, parsed.mention) == (name, mention)


async def test_command_hands_over_the_parsed_command() -> None:
    kwargs = await passes(f.command("start"), message("/start rome"))
    assert kwargs == {"command": parse_command("/start rome")}


@pytest.mark.parametrize("text", ["/stop", "/startx", "start", "hello /start", None])
async def test_command_refuses_other_text(text: str | None) -> None:
    assert await passes(f.command("start"), message(text)) is None


async def test_command_names_are_normalised() -> None:
    assert await passes(f.command("/Start", "HELP"), message("/help")) is not None
    assert await passes(f.command("/Start", "HELP"), message("/START now")) is not None


async def test_command_addressed_to_this_bot_matches_in_any_case() -> None:
    assert await passes(f.command("start"), message("/start@testbot")) is not None
    assert await passes(f.command("start"), message("/start@TestBot x")) is not None


async def test_command_addressed_to_another_bot_is_not_ours() -> None:
    assert await passes(f.command("start"), message("/start@otherbot")) is None
    kwargs = await passes(f.command("start", ignore_mention=True), message("/start@otherbot"))
    assert kwargs is not None
    assert kwargs["command"].mention == "otherbot"


async def test_command_with_a_mention_is_refused_while_the_bot_handle_is_unknown() -> None:
    assert await passes(f.command("start"), message("/start@testbot"), bot_username=None) is None
    assert await passes(f.command("start"), message("/start"), bot_username=None) is not None


async def test_command_with_no_names_takes_any_command() -> None:
    kwargs = await passes(f.command(), message("/whatever 1 2"))
    assert kwargs is not None
    assert kwargs["command"].argv == ["1", "2"]
    assert await passes(f.command(), message("plain words")) is None


async def test_command_ignores_events_without_text() -> None:
    assert await passes(f.command(), press("/start")) is None


async def test_regex_hands_over_the_match_of_a_message() -> None:
    kwargs = await passes(f.regex(r"weather in (\w+)"), message("what's the weather in Rome?"))
    assert kwargs is not None
    assert isinstance(kwargs["match"], re.Match)
    assert kwargs["match"].group(1) == "Rome"


async def test_regex_searches_callback_data() -> None:
    kwargs = await passes(f.regex(r"^page:(\d+)$"), press("page:12"))
    assert kwargs is not None
    assert kwargs["match"].group(1) == "12"


async def test_regex_flags_and_precompiled_patterns() -> None:
    assert await passes(f.regex(r"^ciao\b", re.IGNORECASE), message("CIAO a tutti")) is not None
    assert await passes(f.regex(r"^ciao\b"), message("CIAO a tutti")) is None
    assert await passes(f.regex(re.compile(r"tutti$", re.IGNORECASE)), message("ciao a TUTTI")) is not None


async def test_regex_without_text_or_without_a_match_fails() -> None:
    assert await passes(f.regex("x"), message(None)) is None
    assert await passes(f.regex("x"), message("abc")) is None
    assert await passes(f.regex("x"), object()) is None


async def test_text_matches_whole_trimmed_text() -> None:
    flt = f.text("yes", "no")
    assert await passes(flt, message("  yes \n")) is not None
    assert await passes(flt, message("no")) is not None
    assert await passes(flt, message("yes please")) is None
    assert await passes(flt, message("YES")) is None
    assert await passes(flt, message(None)) is None


async def test_text_can_ignore_case() -> None:
    flt = f.text("Straße", ignore_case=True)
    assert await passes(flt, message("STRASSE")) is not None
    assert await passes(flt, message("strasse")) is not None


async def test_and_merges_what_both_sides_hand_over() -> None:
    kwargs = await passes(f.command("w") & f.regex(r"(\w+)$"), message("/w rome"))
    assert kwargs is not None
    assert kwargs["command"].args == "rome"
    assert kwargs["match"].group(1) == "rome"


async def test_and_stops_at_the_first_failure() -> None:
    seen: list[str] = []

    def right(event: Any) -> bool:
        seen.append("right")
        return True

    assert await passes(f.group & right, message("x")) is None
    assert seen == []


async def test_and_of_plain_true_results_is_true() -> None:
    assert await run(f.private & f.has_text, message("x")) is True


async def test_and_fails_when_only_the_right_side_fails() -> None:
    assert await passes(f.private & f.has_text, message(None)) is None
    assert await passes(f.regex("x") & f.group, message("x")) is None


async def test_or_returns_the_first_side_that_passes() -> None:
    seen: list[str] = []

    def right(event: Any) -> bool:
        seen.append("right")
        return True

    kwargs = await passes(f.regex("hello") | right, message("hello"))
    assert kwargs is not None
    assert kwargs["match"].group() == "hello"
    assert seen == []
    assert await passes(f.regex("nope") | right, message("hello")) == {}
    assert seen == ["right"]


async def test_or_fails_when_both_fail() -> None:
    assert await passes(f.group | f.channel, message("x")) is None


async def test_not_inverts() -> None:
    assert await passes(~f.private, message("x", chat="group")) is not None
    assert await passes(~f.private, message("x")) is None
    assert await passes(~f.regex("x"), message("abc")) is not None
    assert await passes(~f.regex("a"), message("abc")) is None


async def test_plain_callables_combine_on_either_side() -> None:
    assert await passes((lambda e: e.id == 1) & f.private, message("x")) is not None
    assert await passes(f.group | (lambda e: True), message("x")) is not None
    assert await passes((lambda e: False) | f.private, message("x")) is not None


def test_combining_with_something_that_is_not_callable_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="a filter must be callable, got str"):
        f.private & "private"  # type: ignore[operator]
    with pytest.raises(TypeError, match="got int"):
        f.as_filter(3)


def test_as_filter_keeps_a_filter_as_it_is() -> None:
    assert f.as_filter(f.private) is f.private


def test_filters_describe_themselves() -> None:
    assert repr(f.private) == "<Filter private>"
    assert repr(f.private & ~f.has_text) == "<Filter (private & ~has_text)>"
    assert repr(f.group | f.channel) == "<Filter (group | channel)>"

    def is_vip(event: Any) -> bool:
        return True

    assert repr(Filter(is_vip)) == "<Filter is_vip>"
    assert repr(Filter(is_vip, "vip")) == "<Filter vip>"


async def test_not_of_a_filter_that_passed_with_an_empty_dict_fails() -> None:
    passed_empty = Filter(lambda event: {})
    assert await passes(passed_empty, message("x")) == {}
    assert await passes(~passed_empty, message("x")) is None


async def test_or_accepts_a_left_side_that_passed_with_an_empty_dict() -> None:
    passed_empty = Filter(lambda event: {})
    assert await passes(passed_empty | (lambda event: False), message("x")) == {}


async def is_admin(event: Any) -> bool:
    return event.from_user is not None and event.from_user.id == 55


async def no(event: Any) -> bool:
    return False


async def with_role(event: Any) -> dict[str, Any]:
    return {"role": "admin"}


@pytest.mark.parametrize(
    ("flt", "expected"),
    [
        (Filter(is_admin), {}),
        (Filter(is_admin) & f.private, {}),
        (f.private & Filter(is_admin), {}),
        (Filter(with_role) & f.regex("h(i)"), {"role": "admin", "match": "hi"}),
        (f.regex("h(i)") & Filter(with_role), {"match": "hi", "role": "admin"}),
        (Filter(no) | f.private, {}),
        (f.group | Filter(with_role), {"role": "admin"}),
        (Filter(with_role) | f.group, {"role": "admin"}),
        (~Filter(no), {}),
        (Filter(no) & f.private, None),
        (f.private & Filter(no), None),
        (Filter(with_role) & f.group, None),
        (Filter(no) | Filter(no), None),
        (~Filter(is_admin), None),
    ],
)
async def test_async_predicates_combine_like_sync_ones(flt: Filter, expected: dict[str, Any] | None) -> None:
    kwargs = await passes(flt, message("hi"))
    if kwargs is not None and "match" in kwargs:
        kwargs["match"] = kwargs["match"].group()
    assert kwargs == expected


def test_is_async_tells_whether_a_filter_needs_awaiting() -> None:
    class AsyncCheck:
        async def __call__(self, event: Any) -> bool:
            return True

    assert Filter(is_admin).is_async
    assert Filter(AsyncCheck()).is_async
    assert not f.private.is_async
    assert (f.private & Filter(is_admin)).is_async
    assert (Filter(is_admin) | f.group).is_async
    assert (~Filter(is_admin)).is_async
    assert not (f.private & ~f.group | f.channel).is_async


async def test_a_two_argument_predicate_gets_the_context() -> None:
    seen: list[FilterContext] = []

    def needs_context(event: Any, ctx: FilterContext) -> bool:
        seen.append(ctx)
        return ctx.bot_username == "testbot"

    def star_args(*args: Any) -> bool:
        seen.append(args[1])
        return True

    assert await passes(needs_context, message("x")) is not None
    assert await passes(star_args, message("x")) is not None
    assert [c.bot_username for c in seen] == ["testbot", "testbot"]


async def test_a_predicate_whose_signature_cannot_be_read_gets_only_the_event() -> None:
    class Opaque:
        __signature__ = "not a signature"

        def __call__(self, *args: Any) -> bool:
            return len(args) == 1

    assert await passes(Opaque(), message("x")) is not None


@pytest.mark.parametrize("kind", ["private", "group", "channel"])
async def test_chat_kind_filters(kind: str) -> None:
    for name, flt in (("private", f.private), ("group", f.group), ("channel", f.channel)):
        assert (await passes(flt, message("x", chat=kind)) is not None) is (name == kind)
        assert (await passes(flt, press(chat=kind)) is not None) is (name == kind)


async def test_chat_kind_filters_work_on_updates_and_deletions() -> None:
    deleted = Update.from_json({"updateId": 1, "kind": "deleted_messages", "chat": CHATS["group"], "messageIds": [1]})
    assert await passes(f.group, deleted) is not None
    assert await passes(f.group, deleted.deleted) is not None
    assert await passes(f.private, deleted) is None


async def test_chat_kind_filters_refuse_what_has_no_chat_or_an_unknown_kind() -> None:
    unknown = message("x", chat="private")
    unknown = dataclasses.replace(unknown, chat=dataclasses.replace(unknown.chat, kind=ChatKind("forum")))
    for flt in (f.private, f.group, f.channel):
        assert await passes(flt, object()) is None
        assert await passes(flt, unknown) is None
        assert await passes(flt, Update.from_json({"updateId": 1, "kind": "later"})) is None


async def test_has_text_and_has_media() -> None:
    media = [{"id": "0f3c5a3e-1111-2222-3333-444455556666", "kind": "image"}]
    assert await passes(f.has_text, message("x")) is not None
    assert await passes(f.has_text, message("")) is None
    assert await passes(f.has_text, message(None)) is None
    assert await passes(f.has_media, message(None, media=media)) is not None
    assert await passes(f.has_media, message("x")) is None
    assert await passes(f.has_text, press()) is None


async def test_encrypted_reply_and_signed_only_apply_to_messages() -> None:
    sealed = message(None, armored="-----BEGIN PGP MESSAGE-----")
    assert await passes(f.encrypted, sealed) is not None
    assert await passes(f.encrypted, message("x")) is None
    assert await passes(f.reply, message("x", replyTo=5)) is not None
    assert await passes(f.reply, message("x")) is None
    for flt in (f.encrypted, f.reply, f.signed):
        assert await passes(flt, press()) is None


@pytest.mark.parametrize(
    ("status", "signed"),
    [
        (SignatureStatus.VALID, True),
        (SignatureStatus.INVALID, False),
        (SignatureStatus.UNKNOWN_KEY, False),
        (SignatureStatus.UNSIGNED, False),
        (SignatureStatus.NOT_CHECKED, False),
        (None, False),
    ],
)
async def test_signed_wants_a_valid_signature(status: SignatureStatus | None, signed: bool) -> None:
    verification = Verification(status) if status is not None else None
    event = dataclasses.replace(message(None, armored="-----BEGIN PGP MESSAGE-----"), verification=verification)
    assert (await passes(f.signed, event) is not None) is signed


async def test_user_by_id_or_handle() -> None:
    assert await passes(f.user(55), message("x")) is not None
    assert await passes(f.user("anna_r"), message("x")) is not None
    assert await passes(f.user("@ANNA_R"), press()) is not None
    assert await passes(f.user(56, "bruno"), message("x")) is None


async def test_user_needs_a_sender() -> None:
    post = message("x", chat="channel")
    post = dataclasses.replace(post, from_user=None)
    assert await passes(f.user(55), post) is None


async def test_chat_by_id_or_public_handle() -> None:
    assert await passes(f.chat(300), message("x", chat="group")) is not None
    assert await passes(f.chat("@devroom"), message("x", chat="group")) is not None
    assert await passes(f.chat("devroom"), message("x", chat="private")) is None
    assert await passes(f.chat(7), press()) is not None
    assert await passes(f.chat(7), object()) is None


@pytest.mark.parametrize("factory", [f.user, f.chat])
def test_user_and_chat_refuse_a_bool(factory: Any) -> None:
    with pytest.raises(TypeError, match="not a bool"):
        factory(True)


async def test_callback_data_by_value_or_prefix() -> None:
    assert await passes(f.callback_data("yes", "no"), press("no")) is not None
    assert await passes(f.callback_data("yes"), press("yes please")) is None
    assert await passes(f.callback_data(prefix="page:"), press("page:3")) is not None
    assert await passes(f.callback_data("yes", prefix="page:"), press("yes")) is not None
    assert await passes(f.callback_data(prefix="page:"), press("pages")) is None
    assert await passes(f.callback_data(), press("anything")) is None


async def test_callback_data_ignores_messages() -> None:
    assert await passes(f.callback_data("yes"), message("yes")) is None


@pytest.mark.parametrize(
    ("text", "mentioned"),
    [
        ("@testbot what's up", True),
        ("hey @TestBot!", True),
        ("(@testbot)", True),
        ("first line\n@testbot", True),
        ("hey @testbot_fan", False),
        ("hey @testbots", False),
        ("mail@testbot.org", False),
        ("testbot", False),
        (None, False),
    ],
)
async def test_mentioned_follows_the_server_rule(text: str | None, mentioned: bool) -> None:
    assert (await passes(f.mentioned, message(text)) is not None) is mentioned


async def test_mentioned_needs_to_know_the_bot_handle() -> None:
    assert await passes(f.mentioned, message("@testbot"), bot_username=None) is None


async def test_mentioned_escapes_the_handle() -> None:
    assert await passes(f.mentioned, message("@a.c"), bot_username="a.c") is not None
    assert await passes(f.mentioned, message("@abc"), bot_username="a.c") is None
