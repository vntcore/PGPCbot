from __future__ import annotations

import copy
import time
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pgpcbot import (
    KEEP,
    AdminRights,
    Bot,
    BotInfo,
    BotStatus,
    Button,
    CallbackQuery,
    CallbackQueryBase,
    Chat,
    ChatCard,
    ChatKeys,
    ChatKind,
    ChatMembers,
    ChatMode,
    ChatPeer,
    ChatRef,
    Command,
    CommandScope,
    HandleKind,
    InlineKeyboard,
    Limits,
    Media,
    Member,
    Message,
    MessageBase,
    PayloadError,
    PGPCbotError,
    PublishedKey,
    Resolved,
    Seat,
    SeatStatus,
    SentMessage,
    SentMessageBase,
    SignatureStatus,
    SyncBot,
    SyncCallbackQuery,
    SyncMessage,
    SyncSentMessage,
    Update,
    UpdateKind,
    User,
    UserKey,
    Verification,
    WebhookInfo,
    models,
)

from .fakeapi import MEDIA_ID, FakeAPI

DATE_MS = 1791000000000
DATE = datetime(2026, 10, 3, 4, 0, tzinfo=UTC)

ANNA = {"id": 55, "username": "anna", "firstName": "Anna", "lastName": "Rossi", "isBot": False}
PRIVATE = {"id": 7, "kind": "private"}
GROUP = {"id": 300, "kind": "group", "title": "Dev room", "username": "devroom"}


def message_json(**fields: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": 99,
        "chat": dict(PRIVATE),
        "from": dict(ANNA),
        "date": DATE_MS,
        "replyTo": None,
        "kind": "user",
        "text": "/start rome",
        "media": [],
    }
    data.update(fields)
    return data


def callback_json(**fields: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "cb-1",
        "from": dict(ANNA),
        "chat": dict(PRIVATE),
        "messageId": 1001,
        "data": "hourly:rome",
        "expiresAt": DATE_MS + 60_000,
    }
    data.update(fields)
    return data


def sent_json(**fields: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": 1001,
        "chat": dict(PRIVATE),
        "date": DATE_MS,
        "replyTo": 99,
        "markup": None,
        "text": "hi",
    }
    data.update(fields)
    return data


def without(data: dict[str, Any], key: str) -> dict[str, Any]:
    out = copy.deepcopy(data)
    del out[key]
    return out


@pytest.mark.parametrize(
    ("enum", "value"),
    [
        (ChatKind, "supergroup"),
        (ChatMode, "sealed"),
        (SeatStatus, "banned"),
        (UpdateKind, "poll"),
        (BotStatus, "frozen"),
    ],
)
def test_unknown_enum_value_is_a_pseudo_member_equal_to_its_string(enum: Any, value: str) -> None:
    member = enum(value)
    assert isinstance(member, enum)
    assert member == value
    assert member.value == value
    assert str(member) == value
    assert hash(member) == hash(value)
    assert member not in list(enum)


def test_known_enum_values_are_the_named_members() -> None:
    assert ChatKind("private") is ChatKind.PRIVATE
    assert UpdateKind("callback_query") is UpdateKind.CALLBACK_QUERY


@pytest.mark.parametrize("value", ["", None, 5])
def test_open_enum_still_refuses_empty_and_non_string_values(value: Any) -> None:
    with pytest.raises(ValueError):
        ChatKind(value)


def test_signature_status_is_closed() -> None:
    with pytest.raises(ValueError):
        SignatureStatus("maybe")


def test_unix_milliseconds_become_exact_aware_utc_datetimes() -> None:
    message = MessageBase.from_json(message_json(date=DATE_MS + 123))
    assert message.date == DATE + timedelta(milliseconds=123)
    assert message.date.tzinfo is UTC
    assert message.date.microsecond == 123_000


def test_dates_before_the_epoch_are_fine() -> None:
    message = MessageBase.from_json(message_json(date=-1500))
    assert message.date == datetime(1969, 12, 31, 23, 59, 58, 500_000, tzinfo=UTC)


@pytest.mark.parametrize("date", [None, "1791000000000", 1791000000000.0, True, 10**20])
def test_a_message_without_a_usable_date_is_a_payload_error(date: Any) -> None:
    with pytest.raises(PayloadError, match=r"message\.date"):
        MessageBase.from_json(message_json(date=date))


def test_an_optional_date_out_of_range_is_dropped() -> None:
    info = WebhookInfo.from_json({"url": None, "lastOkAt": 10**20, "nextAttemptAt": "soon"})
    assert info.last_ok_at is None
    assert info.next_attempt_at is None


def test_user_from_json() -> None:
    user = User.from_json(ANNA)
    assert (user.id, user.username, user.first_name, user.last_name, user.is_bot) == (
        55,
        "anna",
        "Anna",
        "Rossi",
        False,
    )
    assert user.full_name == "Anna Rossi"
    assert user.mention == "@anna"
    assert user.raw == ANNA


@pytest.mark.parametrize(
    ("data", "full_name", "mention"),
    [
        ({"id": 1, "firstName": "Bruno"}, "Bruno", "Bruno"),
        ({"id": 1, "lastName": "Verdi"}, "Verdi", "Verdi"),
        ({"id": 1, "username": "bruno"}, "@bruno", "@bruno"),
        ({"id": 1}, "user 1", "user 1"),
    ],
)
def test_user_names_fall_back_sensibly(data: dict[str, Any], full_name: str, mention: str) -> None:
    user = User.from_json(data)
    assert user.full_name == full_name
    assert user.mention == mention


@pytest.mark.parametrize("data", [{}, {"id": "55"}, {"id": True}, {"id": None}, "55", None])
def test_user_needs_an_integer_id(data: Any) -> None:
    with pytest.raises(PayloadError):
        User.from_json(data)


def test_payload_error_is_a_pgpcbot_error_and_a_value_error() -> None:
    with pytest.raises(PayloadError) as info:
        User.from_json({})
    assert isinstance(info.value, PGPCbotError)
    assert isinstance(info.value, ValueError)
    assert "user.id" in str(info.value)


def test_chat_ref_kinds() -> None:
    private = ChatRef.from_json(PRIVATE)
    group = ChatRef.from_json(GROUP)
    channel = ChatRef.from_json({"id": 9, "kind": "channel", "title": "News"})
    assert (private.is_private, private.is_group, private.is_channel) == (True, False, False)
    assert (group.is_private, group.is_group, group.is_channel) == (False, True, False)
    assert (channel.is_private, channel.is_group, channel.is_channel) == (False, False, True)
    assert (group.title, group.username) == ("Dev room", "devroom")
    assert private.title is None


def test_chat_ref_of_an_unknown_kind_is_none_of_the_three() -> None:
    chat = ChatRef.from_json({"id": 9, "kind": "forum"})
    assert chat.kind == "forum"
    assert not chat.is_private
    assert not chat.is_group
    assert not chat.is_channel


def test_chat_ref_without_a_kind_is_taken_as_private() -> None:
    assert ChatRef.from_json({"id": 9}).kind is ChatKind.PRIVATE


def test_private_chat_from_json() -> None:
    data = {
        "id": 7,
        "kind": "private",
        "mode": "open",
        "user": {"id": 55, "username": "anna", "firstName": "Anna", "lastName": None, "deleted": False, "hasKey": True},
        "blocked": False,
    }
    chat = Chat.from_json(data)
    assert chat.is_private
    assert chat.mode is ChatMode.OPEN
    assert not chat.protected
    assert isinstance(chat.user, ChatPeer)
    assert (chat.user.id, chat.user.has_key, chat.user.deleted) == (55, True, False)
    assert chat.blocked is False
    assert (chat.member_count, chat.history_visible, chat.slow_mode_seconds, chat.me) == (None, None, None, None)


def test_group_chat_from_json() -> None:
    data = {
        **GROUP,
        "mode": "protected",
        "description": "where it happens",
        "memberCount": 12,
        "historyVisible": False,
        "slowModeSeconds": 30,
        "me": {"status": "admin", "adminRights": 5, "can": {"deleteMessages": True}, "customTitle": "Robot"},
    }
    chat = Chat.from_json(data)
    assert chat.is_group
    assert chat.protected
    assert (chat.description, chat.member_count, chat.history_visible, chat.slow_mode_seconds) == (
        "where it happens",
        12,
        False,
        30,
    )
    assert chat.me is not None
    assert chat.me.is_admin
    assert chat.me.can == AdminRights(delete_messages=True)
    assert (chat.user, chat.blocked) == (None, None)


def test_chat_without_a_mode_is_taken_as_encrypted() -> None:
    # the safe guess: sealing for a readable chat is refused without disclosing anything
    assert Chat.from_json({"id": 7, "kind": "private"}).mode is ChatMode.PROTECTED


def test_bot_without_a_mode_is_protected_exactly_when_it_has_a_fingerprint() -> None:
    assert BotInfo.from_json({"id": 412, "username": "b", "name": "B"}).mode is ChatMode.OPEN
    published = BotInfo.from_json({"id": 412, "username": "b", "name": "B", "fingerprint": "AB" * 20})
    assert published.mode is ChatMode.PROTECTED


def test_chat_needs_an_id() -> None:
    with pytest.raises(PayloadError, match=r"chat\.id"):
        Chat.from_json({"kind": "group", "mode": "open"})


def test_a_resolved_person_carries_the_id_at_the_top() -> None:
    found = Resolved.from_json(
        {
            "kind": "user",
            "id": 55,
            "username": "anna",
            "user": {"id": 55, "username": "anna", "firstName": "Anna", "isBot": False, "hasKey": True},
            "chat": None,
        }
    )
    assert (found.kind, found.id, found.username, found.mention) == (HandleKind.USER, 55, "anna", "@anna")
    assert (found.is_user, found.is_chat) == (True, False)
    assert isinstance(found.user, ChatPeer)
    assert (found.user.full_name, found.user.has_key, found.user.is_bot) == ("Anna", True, False)
    assert found.chat is None


def test_a_resolved_channel_is_a_card_that_can_be_passed_as_a_chat() -> None:
    found = Resolved.from_json(
        {
            "kind": "channel",
            "id": 40,
            "username": "notizie",
            "user": None,
            "chat": {
                "id": 40,
                "kind": "channel",
                "title": "Notizie",
                "username": "notizie",
                "description": "ogni mattina",
                "memberCount": 1280,
                "historyVisible": True,
            },
        }
    )
    assert (found.is_chat, found.is_user) == (True, False)
    assert found.user is None
    assert isinstance(found.chat, ChatCard)
    assert found.chat.is_channel
    assert (found.chat.id, found.chat.member_count, found.chat.history_visible) == (40, 1280, True)
    assert (found.chat.description, found.chat.title) == ("ogni mattina", "Notizie")
    # a ChatRef is what every chat method takes, and a card is one
    assert isinstance(found.chat, ChatRef)


def test_a_resolved_handle_of_an_unknown_kind_is_neither() -> None:
    found = Resolved.from_json({"kind": "forum", "id": 3})
    assert found.kind == "forum"
    assert not found.is_user
    assert not found.is_chat
    assert found.mention == "id 3"


def test_a_bare_user_object_is_read_as_a_person() -> None:
    # no kind: the only shape that has ever meant anything else is a chat
    found = Resolved.from_json({"id": 55, "user": {"id": 55}})
    assert found.kind is HandleKind.USER
    assert found.user is not None
    assert found.user.id == 55


def test_a_resolved_handle_needs_an_id() -> None:
    with pytest.raises(PayloadError, match=r"resolved\.id"):
        Resolved.from_json({"kind": "user", "username": "anna"})
    with pytest.raises(PayloadError, match=r"resolved\.chat"):
        Resolved.from_json({"kind": "group", "id": 9, "chat": {"kind": "group"}})


def test_admin_seat_carries_rights() -> None:
    seat = Seat.from_json(
        {
            "status": "admin",
            "adminRights": 1023,
            "customTitle": "Boss",
            "can": {
                "deleteMessages": True,
                "banUsers": True,
                "addMembers": False,
                "pinMessages": True,
                "changeInfo": False,
                "manageAdmins": False,
                "inviteLinks": True,
                "postMessages": True,
                "editMessages": False,
                "manageComments": True,
                "launchRockets": True,
            },
        }
    )
    assert seat.status is SeatStatus.ADMIN
    assert seat.is_admin
    assert seat.is_inside
    assert (seat.admin_rights, seat.custom_title, seat.rights) == (1023, "Boss", None)
    assert seat.can == AdminRights(
        delete_messages=True,
        ban_users=True,
        pin_messages=True,
        invite_links=True,
        post_messages=True,
        manage_comments=True,
    )
    # a right this version does not type is still there
    assert seat.can.raw["launchRockets"] is True


def test_admin_rights_take_only_real_booleans() -> None:
    rights = AdminRights.from_json({"deleteMessages": 1, "banUsers": "true", "pinMessages": True})
    assert rights == AdminRights(pin_messages=True)


def test_restricted_seat_carries_the_bitmask_and_the_end_of_the_restriction() -> None:
    seat = Seat.from_json({"status": "restricted", "rights": 6, "restrictedUntil": DATE_MS})
    assert (seat.rights, seat.restricted_until, seat.can) == (6, DATE, None)
    assert seat.is_inside
    assert not seat.is_admin


@pytest.mark.parametrize(
    ("status", "inside"),
    [("admin", True), ("member", True), ("restricted", True), ("left", False), ("kicked", False), ("banned", False)],
)
def test_seat_is_inside(status: str, inside: bool) -> None:
    assert Seat.from_json({"status": status}).is_inside is inside


def test_seat_without_a_status_is_left() -> None:
    assert Seat.from_json({}).status is SeatStatus.LEFT


def test_member_is_a_seat_with_a_user() -> None:
    member = Member.from_json({"user": ANNA, "status": "member", "rights": 255, "restrictedUntil": None})
    assert member.user == User.from_json(ANNA)
    assert (member.status, member.rights, member.restricted_until) == (SeatStatus.MEMBER, 255, None)
    assert isinstance(member, Seat)


def test_member_needs_a_user() -> None:
    with pytest.raises(PayloadError, match="user"):
        Member.from_json({"status": "member"})


def test_chat_members_counts_itself_when_the_server_does_not() -> None:
    members = [{"user": ANNA, "status": "admin"}, {"user": {"id": 56}, "status": "member"}]
    assert ChatMembers.from_json({"members": members, "memberCount": 40}).member_count == 40
    listed = ChatMembers.from_json({"members": members})
    assert listed.member_count == 2
    assert [m.user.id for m in listed.members] == [55, 56]


def test_chat_keys_of_a_group() -> None:
    keys = ChatKeys.from_json(
        {
            "keys": [{"userId": 55, "fingerprint": "AB" * 20, "publicKey": "-----BEGIN PGP PUBLIC KEY BLOCK-----..."}],
            "memberCount": 3,
        }
    )
    assert keys.member_count == 3
    assert keys.keys == (
        UserKey(user_id=55, fingerprint="AB" * 20, public_key="-----BEGIN PGP PUBLIC KEY BLOCK-----..."),
    )


def test_chat_keys_of_a_private_chat_has_no_member_count() -> None:
    keys = ChatKeys.from_json({"keys": [{"userId": 55, "fingerprint": None, "publicKey": "k"}]})
    assert keys.member_count is None
    assert keys.keys[0].fingerprint is None


@pytest.mark.parametrize("missing", ["userId", "publicKey"])
def test_user_key_needs_the_user_and_the_key(missing: str) -> None:
    data = without({"userId": 55, "fingerprint": None, "publicKey": "k"}, missing)
    with pytest.raises(PayloadError):
        UserKey.from_json(data)
    with pytest.raises(PayloadError):
        ChatKeys.from_json({"keys": [data]})


def test_published_key() -> None:
    assert PublishedKey.from_json({"fingerprint": "AB" * 20, "mode": "protected"}) == PublishedKey(
        fingerprint="AB" * 20, mode=ChatMode.PROTECTED
    )
    assert PublishedKey.from_json({}).mode is ChatMode.PROTECTED


def me_json(**fields: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": 412,
        "username": "testbot",
        "name": "Test",
        "about": "weather",
        "description": None,
        "mode": "protected",
        "fingerprint": "AB" * 20,
        "privacyMode": False,
        "allowGroups": False,
        "status": "paused",
        "webhook": {"url": "https://bot.example.org/hook", "hasSecret": True, "failures": 2, "lastError": "timeout"},
        "queued": 4,
        "limits": {"textMax": 4096, "pollTimeoutMaxS": 50, "sendsPerSecond": 30, "apiCallsPerMinute": 600},
    }
    data.update(fields)
    return data


def test_bot_info_from_json() -> None:
    me = BotInfo.from_json(me_json())
    assert (me.id, me.username, me.name, me.about, me.description) == (412, "testbot", "Test", "weather", None)
    assert me.mode is ChatMode.PROTECTED
    assert me.protected
    assert me.status is BotStatus.PAUSED
    assert me.paused
    assert (me.fingerprint, me.privacy_mode, me.allow_groups, me.queued) == ("AB" * 20, False, False, 4)
    assert (me.webhook.url, me.webhook.has_secret, me.webhook.failures, me.webhook.last_error) == (
        "https://bot.example.org/hook",
        True,
        2,
        "timeout",
    )
    assert me.limits.text_max == 4096


def test_bot_info_defaults_when_the_server_leaves_things_out() -> None:
    me = BotInfo.from_json({"id": 412})
    assert (me.username, me.name, me.mode, me.status) == ("", "", ChatMode.OPEN, BotStatus.ACTIVE)
    assert (me.privacy_mode, me.allow_groups, me.queued, me.fingerprint) == (True, True, 0, None)
    assert me.webhook.url is None
    assert not me.webhook.has_secret
    assert me.limits == Limits()
    assert not me.protected
    assert not me.paused


def test_bot_info_with_an_unknown_status_is_not_paused() -> None:
    me = BotInfo.from_json(me_json(status="frozen"))
    assert me.status == "frozen"
    assert not me.paused


def test_bot_info_needs_an_id() -> None:
    with pytest.raises(PayloadError, match=r"me\.id"):
        BotInfo.from_json(without(me_json(), "id"))


LIMIT_NAMES = {
    "text_max": "textMax",
    "armored_max": "armoredMax",
    "markup_bytes": "markupBytes",
    "keyboard_rows": "keyboardRows",
    "buttons_per_row": "buttonsPerRow",
    "buttons_total": "buttonsTotal",
    "button_text_max": "buttonTextMax",
    "callback_data_bytes": "callbackDataBytes",
    "url_max": "urlMax",
    "callback_answer_max": "callbackAnswerMax",
    "callback_window_ms": "callbackWindowMs",
    "poll_timeout_max_s": "pollTimeoutMaxS",
    "poll_limit_max": "pollLimitMax",
    "webhook_timeout_ms": "webhookTimeoutMs",
    "sends_per_second": "sendsPerSecond",
    "sends_per_group_per_minute": "sendsPerGroupPerMinute",
    "sends_per_private_per_second": "sendsPerPrivatePerSecond",
    "api_calls_per_minute": "apiCallsPerMinute",
    "commands_per_scope": "commandsPerScope",
    "command_description_max": "commandDescriptionMax",
}


def test_every_limit_is_read_from_its_camel_case_name() -> None:
    data = {json_name: 1000 + i for i, json_name in enumerate(LIMIT_NAMES.values())}
    limits = Limits.from_json(data)
    for i, attr in enumerate(LIMIT_NAMES):
        assert getattr(limits, attr) == 1000 + i, attr
    assert {f for f in Limits.__dataclass_fields__ if f != "raw"} == set(LIMIT_NAMES)


def test_limits_default_to_the_documented_numbers() -> None:
    limits = Limits.from_json(None)
    assert (limits.text_max, limits.armored_max, limits.markup_bytes) == (4096, 131072, 8192)
    assert (limits.sends_per_second, limits.sends_per_group_per_minute, limits.sends_per_private_per_second) == (
        30,
        20,
        1,
    )
    assert (limits.api_calls_per_minute, limits.poll_timeout_max_s, limits.callback_window_ms) == (600, 50, 60000)
    assert limits.raw == {}


@pytest.mark.parametrize("value", [0, -5, True, "4000", 4000.5, None])
def test_limits_ignore_values_that_are_not_positive_integers(value: Any) -> None:
    assert Limits.from_json({"textMax": value}).text_max == 4096


def test_limits_get_reads_any_name_including_untyped_ones() -> None:
    limits = Limits.from_json({"textMax": 3000, "stickersPerDay": 7, "flag": True, "ratio": 0.5})
    assert limits.get("textMax") == 3000
    assert limits.get("stickersPerDay") == 7
    assert limits.get("missing") is None
    assert limits.get("missing", 9) == 9
    assert limits.get("flag", -1) == -1
    assert limits.get("ratio", -1) == -1


def test_webhook_info_from_a_full_get() -> None:
    info = WebhookInfo.from_json(
        {
            "url": "https://bot.example.org/hook",
            "hasSecret": True,
            "pending": 3,
            "failures": 1,
            "lastError": "HTTP 502",
            "lastOkAt": DATE_MS,
            "nextAttemptAt": DATE_MS + 30_000,
        }
    )
    assert (info.url, info.has_secret, info.pending, info.failures, info.last_error) == (
        "https://bot.example.org/hook",
        True,
        3,
        1,
        "HTTP 502",
    )
    assert info.last_ok_at == DATE
    assert info.next_attempt_at == DATE + timedelta(seconds=30)


def test_webhook_info_from_the_partial_answers_of_set_and_delete() -> None:
    assert WebhookInfo.from_json({"url": "https://b.example.org/h", "hasSecret": False, "pending": 2}) == WebhookInfo(
        url="https://b.example.org/h", pending=2
    )
    assert WebhookInfo.from_json({"url": None, "pending": 0}) == WebhookInfo()


def test_webhook_info_needs_an_object() -> None:
    with pytest.raises(PayloadError, match="webhook"):
        WebhookInfo.from_json(None)


def test_command_round_trip() -> None:
    command = Command.from_json({"command": "weather", "description": "Today's weather"})
    assert (command.command, command.description) == ("weather", "Today's weather")
    assert command.to_json() == {"command": "weather", "description": "Today's weather"}


def test_command_scope_from_json() -> None:
    scope = CommandScope.from_json({"scope": "chat:42", "commands": [{"command": "a", "description": "A"}]})
    assert scope.scope == "chat:42"
    assert scope.commands == (Command(command="a", description="A"),)
    assert CommandScope.from_json({}) == CommandScope(scope="default", commands=())


def test_readable_message_from_json() -> None:
    data = message_json(
        replyTo=42,
        text="look",
        media=[{"id": MEDIA_ID, "kind": "image", "mime": "image/jpeg", "url": f"/media/{MEDIA_ID}", "width": 640}],
    )
    message = MessageBase.from_json(data)
    assert (message.id, message.chat_id, message.reply_to, message.kind, message.text) == (99, 7, 42, "user", "look")
    assert message.chat == ChatRef(id=7, kind=ChatKind.PRIVATE)
    assert message.from_user == User.from_json(ANNA)
    assert message.date == DATE
    assert message.media == (Media(id=MEDIA_ID, kind="image", mime="image/jpeg", url=f"/media/{MEDIA_ID}", width=640),)
    assert not message.encrypted
    assert (message.armored, message.plaintext, message.verification, message.signature) == (None, None, None, None)


def test_armored_message_from_json() -> None:
    data = without(without(message_json(), "text"), "media")
    data["armored"] = "-----BEGIN PGP MESSAGE-----\n..."
    message = MessageBase.from_json(data)
    assert message.encrypted
    assert message.armored == data["armored"]
    assert message.text is None
    assert message.media == ()


def test_channel_post_without_a_sender() -> None:
    data = message_json(chat={"id": 9, "kind": "channel", "title": "News"}, signature="The editor")
    data["from"] = None
    message = MessageBase.from_json(data)
    assert message.from_user is None
    assert message.signature == "The editor"
    assert message.chat.is_channel


def test_message_skips_media_entries_that_are_not_objects() -> None:
    message = MessageBase.from_json(message_json(media=["junk", {"id": MEDIA_ID, "kind": "video"}, None]))
    assert [m.id for m in message.media] == [MEDIA_ID]


@pytest.mark.parametrize(
    ("text", "is_command"), [("/start", True), ("/start rome", True), ("hi /start", False), ("", False), (None, False)]
)
def test_message_is_command(text: str | None, is_command: bool) -> None:
    assert MessageBase.from_json(message_json(text=text)).is_command is is_command


def test_message_keeps_fields_this_version_does_not_know_in_raw() -> None:
    data = message_json(reactions=[{"emoji": "x"}])
    data["chat"]["topic"] = 3
    message = MessageBase.from_json(data)
    assert message.raw == data
    assert message.raw["reactions"] == [{"emoji": "x"}]
    assert message.chat.raw["topic"] == 3


def test_raw_is_left_out_of_equality() -> None:
    a = MessageBase.from_json(message_json())
    b = MessageBase.from_json(message_json(extra="field"))
    assert a == b


def test_media_from_json() -> None:
    media = Media.from_json(
        {
            "id": MEDIA_ID,
            "kind": "video",
            "mime": "video/mp4",
            "url": "/media/x",
            "width": 1920,
            "height": 1080,
            "duration": 1234.6,
        }
    )
    assert (media.id, media.kind, media.mime, media.url, media.width, media.height) == (
        MEDIA_ID,
        "video",
        "video/mp4",
        "/media/x",
        1920,
        1080,
    )
    assert media.duration_ms == 1235
    # name and size only come from inside the armor, and a server-listed file was sent in the clear
    assert (media.name, media.size, media.encrypted) == (None, None, False)


def test_media_defaults_to_a_file_with_nothing_else_known() -> None:
    media = Media.from_json({"id": MEDIA_ID, "width": "640", "duration": True})
    assert (media.kind, media.width, media.duration_ms, media.url) == ("file", None, None, None)


@pytest.mark.parametrize("data", [{}, {"id": ""}, {"id": 5}, "x"])
def test_media_needs_an_id(data: Any) -> None:
    with pytest.raises(PayloadError):
        Media.from_json(data)


@pytest.mark.parametrize("missing", ["id", "chat", "date"])
def test_message_needs_id_chat_and_date(missing: str) -> None:
    with pytest.raises(PayloadError):
        MessageBase.from_json(without(message_json(), missing))


def test_sent_message_with_buttons() -> None:
    markup = {"inlineKeyboard": [[{"text": "Again", "callbackData": "again"}]]}
    sent = SentMessageBase.from_json(sent_json(markup=markup))
    assert sent.markup == InlineKeyboard.build([Button.callback("Again", "again")])
    assert (sent.id, sent.reply_to, sent.text, sent.armored, sent.date) == (1001, 99, "hi", None, DATE)
    assert not sent.encrypted


def test_sent_message_without_buttons_and_sealed() -> None:
    sent = SentMessageBase.from_json(without(sent_json(armored="-----BEGIN PGP MESSAGE-----"), "text"))
    assert sent.markup is None
    assert sent.encrypted
    assert sent.text is None


@pytest.mark.parametrize("missing", ["id", "chat", "date"])
def test_sent_message_needs_id_chat_and_date(missing: str) -> None:
    with pytest.raises(PayloadError):
        SentMessageBase.from_json(without(sent_json(), missing))


def test_callback_query_from_json() -> None:
    query = CallbackQueryBase.from_json(callback_json())
    assert (query.id, query.message_id, query.data) == ("cb-1", 1001, "hourly:rome")
    assert query.from_user.id == 55
    assert query.chat.id == 7
    assert query.expires_at == DATE + timedelta(seconds=60)


def test_callback_query_expired_compares_with_now() -> None:
    now_ms = int(time.time() * 1000)
    assert CallbackQueryBase.from_json(callback_json(expiresAt=now_ms - 1000)).expired
    assert not CallbackQueryBase.from_json(callback_json(expiresAt=now_ms + 60_000)).expired
    assert not CallbackQueryBase.from_json(without(callback_json(), "expiresAt")).expired


def test_callback_query_without_data_has_empty_data() -> None:
    assert CallbackQueryBase.from_json(without(callback_json(), "data")).data == ""


@pytest.mark.parametrize(
    "data",
    [
        without(callback_json(), "id"),
        callback_json(id=""),
        without(callback_json(), "from"),
        without(callback_json(), "messageId"),
        without(callback_json(), "chat"),
    ],
)
def test_callback_query_needs_id_sender_chat_and_message(data: dict[str, Any]) -> None:
    with pytest.raises(PayloadError):
        CallbackQueryBase.from_json(data)


def test_message_update() -> None:
    update = Update.from_json({"updateId": 5, "kind": "message", "message": message_json()})
    assert (update.update_id, update.kind) == (5, UpdateKind.MESSAGE)
    assert isinstance(update.message, MessageBase)
    assert update.event is update.message
    assert update.chat == update.message.chat
    assert update.from_user == update.message.from_user
    assert (update.callback_query, update.deleted, update.chat_member) == (None, None, None)


def test_edited_message_update_carries_the_message_too() -> None:
    update = Update.from_json({"updateId": 6, "kind": "edited_message", "message": message_json(text="fixed")})
    assert update.kind is UpdateKind.EDITED_MESSAGE
    assert update.message is not None
    assert update.message.text == "fixed"


def test_callback_query_update() -> None:
    update = Update.from_json({"updateId": 7, "kind": "callback_query", "callback": callback_json()})
    assert isinstance(update.callback_query, CallbackQueryBase)
    assert update.event is update.callback_query
    assert update.chat == update.callback_query.chat
    assert update.from_user is not None
    assert update.from_user.id == 55
    assert update.message is None


def test_deleted_messages_update() -> None:
    update = Update.from_json(
        {"updateId": 8, "kind": "deleted_messages", "chat": GROUP, "messageIds": [3, 4, "5", True, 6.0, 7]}
    )
    assert update.deleted is not None
    assert update.deleted.message_ids == (3, 4, 7)
    assert update.chat == ChatRef.from_json(GROUP)
    assert update.event is update.deleted
    # nobody is named on a deletion
    assert update.from_user is None


def test_my_chat_member_update() -> None:
    update = Update.from_json(
        {
            "updateId": 9,
            "kind": "my_chat_member",
            "chat": GROUP,
            "from": ANNA,
            "date": DATE_MS,
            "old": {"status": "left"},
            "new": {"status": "member", "rights": 255},
        }
    )
    change = update.chat_member
    assert change is not None
    assert (change.old.status, change.new.status, change.new.rights) == (SeatStatus.LEFT, SeatStatus.MEMBER, 255)
    assert change.date == DATE
    assert update.from_user is not None
    assert update.from_user.id == 55
    assert update.chat is change.chat
    assert update.event is change


def test_my_chat_member_update_from_nobody() -> None:
    update = Update.from_json(
        {
            "updateId": 9,
            "kind": "my_chat_member",
            "chat": PRIVATE,
            "old": {"status": "member"},
            "new": {"status": "kicked"},
        }
    )
    assert update.chat_member is not None
    assert (update.chat_member.from_user, update.chat_member.date) == (None, None)
    assert update.from_user is None


def test_unknown_update_kind_still_parses_and_keeps_everything_in_raw() -> None:
    data = {"updateId": 10, "kind": "poll_answer", "poll": {"id": 1, "option": 2}}
    update = Update.from_json(data)
    assert update.kind == "poll_answer"
    assert update.kind not in list(UpdateKind)
    assert (update.message, update.callback_query, update.deleted, update.chat_member) == (None, None, None, None)
    assert (update.chat, update.from_user, update.event) == (None, None, None)
    assert update.raw == data


def test_update_without_a_kind_is_unknown() -> None:
    assert Update.from_json({"updateId": 1}).kind == "unknown"


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "message", "message": message_json()},
        {"updateId": "1", "kind": "message", "message": message_json()},
        {"updateId": 1, "kind": "message"},
        {"updateId": 1, "kind": "callback_query"},
        {"updateId": 1, "kind": "deleted_messages", "messageIds": [1]},
        {"updateId": 1, "kind": "my_chat_member", "chat": PRIVATE, "new": {"status": "member"}},
        [],
    ],
)
def test_update_missing_what_its_kind_requires_is_a_payload_error(data: Any) -> None:
    with pytest.raises(PayloadError):
        Update.from_json(data)


def test_updates_from_json_keeps_the_order() -> None:
    updates = models.updates_from_json(
        [{"updateId": 2, "kind": "message", "message": message_json()}, {"updateId": 3, "kind": "x"}]
    )
    assert [u.update_id for u in updates] == [2, 3]


@pytest.mark.parametrize(
    ("kind", "old", "new", "expected"),
    [
        ("group", "left", "member", {"joined"}),
        ("group", "kicked", "member", {"joined"}),
        ("group", "member", "admin", set()),
        ("group", "restricted", "member", set()),
        ("group", "admin", "kicked", {"left"}),
        ("group", "member", "left", {"left"}),
        ("channel", "left", "admin", {"joined"}),
        ("private", "member", "kicked", {"left", "blocked"}),
        ("private", "kicked", "member", {"joined", "unblocked"}),
        ("private", "left", "member", {"joined"}),
        ("private", "kicked", "kicked", {"blocked"}),
    ],
)
def test_chat_member_update_helpers(kind: str, old: str, new: str, expected: set[str]) -> None:
    update = Update.from_json(
        {
            "updateId": 1,
            "kind": "my_chat_member",
            "chat": {"id": 1, "kind": kind},
            "old": {"status": old},
            "new": {"status": new},
        }
    )
    change = update.chat_member
    assert change is not None
    flags = {name for name in ("joined", "left", "blocked", "unblocked") if getattr(change, name)}
    assert flags == expected


class AsyncRecorder:
    """Stands in for Bot: records what a bound object asks of it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Callable[..., Any]:
        async def method(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            return f"{name} result"

        return method


class SyncRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Callable[..., Any]:
        def method(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            return f"{name} result"

        return method


@pytest.mark.parametrize(
    ("bound", "base", "data"),
    [
        (Message, MessageBase, message_json),
        (SyncMessage, MessageBase, message_json),
        (SentMessage, SentMessageBase, sent_json),
        (SyncSentMessage, SentMessageBase, sent_json),
        (CallbackQuery, CallbackQueryBase, callback_json),
        (SyncCallbackQuery, CallbackQueryBase, callback_json),
    ],
)
def test_bind_keeps_every_field_and_adds_the_bot(bound: Any, base: Any, data: Callable[[], dict[str, Any]]) -> None:
    plain = base.from_json(data())
    bot = object()
    obj = bound.bind(plain, bot)
    assert type(obj) is bound
    assert obj._bot is bot
    assert plain._bot is None
    for name in base.__dataclass_fields__:
        if name != "_bot":
            assert getattr(obj, name) == getattr(plain, name), name
    assert obj.raw is plain.raw


def test_bind_carries_the_decrypted_parts_over() -> None:
    plain = MessageBase(
        id=1,
        chat=ChatRef(id=7, kind=ChatKind.PRIVATE),
        from_user=None,
        date=DATE,
        text="ciao",
        armored="-----BEGIN PGP MESSAGE-----",
        plaintext="ciao",
        verification=Verification(SignatureStatus.VALID, "AB" * 20),
    )
    bound = Message.bind(plain, object())  # type: ignore[arg-type]
    assert (bound.plaintext, bound.verification) == ("ciao", plain.verification)
    assert bound.verification is not None
    assert bound.verification.valid


async def test_message_reply_quotes_and_answer_does_not() -> None:
    bot = AsyncRecorder()
    message = Message.bind(MessageBase.from_json(message_json()), bot)  # type: ignore[arg-type]
    kb = InlineKeyboard.build([Button.callback("A", "a")])
    assert await message.reply("pong", markup=kb, silent=True, send_id="sid") == "send_message result"
    await message.reply("no quote", quote=False)
    await message.answer("plain", encrypt=True)
    assert bot.calls == [
        (
            "send_message",
            (7, "pong"),
            {"reply_to": 99, "markup": kb, "silent": True, "encrypt": None, "send_id": "sid"},
        ),
        (
            "send_message",
            (7, "no quote"),
            {"reply_to": None, "markup": None, "silent": False, "encrypt": None, "send_id": None},
        ),
        (
            "send_message",
            (7, "plain"),
            {"reply_to": None, "markup": None, "silent": False, "encrypt": True, "send_id": None},
        ),
    ]


async def test_message_delete_and_typing() -> None:
    bot = AsyncRecorder()
    message = Message.bind(MessageBase.from_json(message_json()), bot)  # type: ignore[arg-type]
    assert await message.delete() is True
    assert await message.typing() is True
    await message.typing(False)
    assert bot.calls == [
        ("delete_message", (7, 99), {}),
        ("send_typing", (7, True), {}),
        ("send_typing", (7, False), {}),
    ]


async def test_sent_message_edit_keeps_the_buttons_unless_told_otherwise() -> None:
    bot = AsyncRecorder()
    sent = SentMessage.bind(SentMessageBase.from_json(sent_json()), bot)  # type: ignore[arg-type]
    await sent.edit("new words")
    await sent.edit(markup=None)
    await sent.delete()
    assert bot.calls[0] == ("edit_message", (7, 1001, "new words"), {"markup": KEEP, "encrypt": None})
    assert bot.calls[0][2]["markup"] is KEEP
    assert bot.calls[1] == ("edit_message", (7, 1001, None), {"markup": None, "encrypt": None})
    assert bot.calls[2] == ("delete_message", (7, 1001), {})


async def test_callback_query_actions_target_the_pressed_message() -> None:
    bot = AsyncRecorder()
    query = CallbackQuery.bind(CallbackQueryBase.from_json(callback_json()), bot)  # type: ignore[arg-type]
    assert await query.answer("Saved", alert=True) is True
    await query.edit_message("updated")
    await query.delete_message()
    await query.send("new one")
    assert bot.calls == [
        ("answer_callback", ("cb-1", "Saved"), {"alert": True, "url": None}),
        ("edit_message", (7, 1001, "updated"), {"markup": KEEP, "encrypt": None}),
        ("delete_message", (7, 1001), {}),
        ("send_message", (7, "new one"), {"markup": None, "encrypt": None}),
    ]


def test_sync_twins_call_the_same_methods() -> None:
    bot = SyncRecorder()
    message = SyncMessage.bind(MessageBase.from_json(message_json()), bot)  # type: ignore[arg-type]
    sent = SyncSentMessage.bind(SentMessageBase.from_json(sent_json()), bot)  # type: ignore[arg-type]
    query = SyncCallbackQuery.bind(CallbackQueryBase.from_json(callback_json()), bot)  # type: ignore[arg-type]
    assert message.reply("pong") == "send_message result"
    message.answer("plain")
    assert message.delete() is True
    message.typing()
    sent.edit("again")
    sent.delete()
    query.answer(url="https://example.org/game")
    query.edit_message(markup=None)
    query.delete_message()
    query.send("more")
    assert bot.calls == [
        (
            "send_message",
            (7, "pong"),
            {"reply_to": 99, "markup": None, "silent": False, "encrypt": None, "send_id": None},
        ),
        (
            "send_message",
            (7, "plain"),
            {"reply_to": None, "markup": None, "silent": False, "encrypt": None, "send_id": None},
        ),
        ("delete_message", (7, 99), {}),
        ("send_typing", (7, True), {}),
        ("edit_message", (7, 1001, "again"), {"markup": KEEP, "encrypt": None}),
        ("delete_message", (7, 1001), {}),
        ("answer_callback", ("cb-1", None), {"alert": False, "url": "https://example.org/game"}),
        ("edit_message", (7, 1001, None), {"markup": None, "encrypt": None}),
        ("delete_message", (7, 1001), {}),
        ("send_message", (7, "more"), {"markup": None, "encrypt": None}),
    ]


ASYNC_ACTIONS = [
    (Message, message_json, "reply", ("x",)),
    (Message, message_json, "answer", ("x",)),
    (Message, message_json, "delete", ()),
    (Message, message_json, "typing", ()),
    (SentMessage, sent_json, "edit", ("x",)),
    (SentMessage, sent_json, "delete", ()),
    (CallbackQuery, callback_json, "answer", ()),
    (CallbackQuery, callback_json, "edit_message", ("x",)),
    (CallbackQuery, callback_json, "delete_message", ()),
    (CallbackQuery, callback_json, "send", ("x",)),
]

SYNC_ACTIONS = [
    (SyncMessage, message_json, "reply", ("x",)),
    (SyncMessage, message_json, "answer", ("x",)),
    (SyncMessage, message_json, "delete", ()),
    (SyncMessage, message_json, "typing", ()),
    (SyncSentMessage, sent_json, "edit", ("x",)),
    (SyncSentMessage, sent_json, "delete", ()),
    (SyncCallbackQuery, callback_json, "answer", ()),
    (SyncCallbackQuery, callback_json, "edit_message", ("x",)),
    (SyncCallbackQuery, callback_json, "delete_message", ()),
    (SyncCallbackQuery, callback_json, "send", ("x",)),
]


@pytest.mark.parametrize(("cls", "data", "action", "args"), ASYNC_ACTIONS)
async def test_unbound_async_objects_refuse_to_act(cls: Any, data: Any, action: str, args: tuple[Any, ...]) -> None:
    # from_json on the acting class builds it, but nothing bound it to a bot
    obj = cls.from_json(data())
    with pytest.raises(RuntimeError, match="not bound to a bot"):
        await getattr(obj, action)(*args)


@pytest.mark.parametrize(("cls", "data", "action", "args"), SYNC_ACTIONS)
def test_unbound_sync_objects_refuse_to_act(cls: Any, data: Any, action: str, args: tuple[Any, ...]) -> None:
    obj = cls.from_json(data())
    with pytest.raises(RuntimeError, match="not bound to a bot"):
        getattr(obj, action)(*args)


def test_keep_is_a_falsy_singleton_that_names_itself() -> None:
    assert repr(KEEP) == "KEEP"
    assert not KEEP
    assert KEEP is models.KEEP
    assert KEEP is not None


@pytest.fixture
async def bot(make_bot: Callable[..., Bot], fake: FakeAPI) -> AsyncIterator[Bot]:
    fake.add_chat(7)
    bot = make_bot()
    yield bot
    await bot.api.aclose()


@pytest.fixture
def sync_bot(make_sync_bot: Callable[..., SyncBot], fake: FakeAPI) -> Iterator[SyncBot]:
    fake.add_chat(7)
    bot = make_sync_bot()
    yield bot
    bot.api.close()


async def test_reply_goes_out_quoting_the_message(bot: Bot, fake: FakeAPI) -> None:
    message = Message.bind(MessageBase.from_json(message_json()), bot)
    sent = await message.reply("pong")
    await message.answer("unquoted")
    first, second = fake.calls("POST", "/bot/v1/messages")
    assert first["json"]["replyTo"] == 99
    assert first["json"]["chatId"] == 7
    assert first["json"]["text"] == "pong"
    assert "replyTo" not in second["json"]
    assert isinstance(sent, SentMessage)
    assert sent._bot is bot


async def test_edit_on_the_wire_keeps_or_drops_the_buttons(bot: Bot, fake: FakeAPI) -> None:
    message = Message.bind(MessageBase.from_json(message_json()), bot)
    sent = await message.reply("pick", markup=InlineKeyboard.build([Button.callback("A", "a")]))
    await sent.edit("still picking")
    await sent.edit(markup=None)
    keep, drop = fake.calls("PATCH", r"/bot/v1/messages/\d+")
    assert keep["json"] == {"chatId": 7, "text": "still picking"}
    assert drop["json"] == {"chatId": 7, "markup": None}


async def test_callback_answer_on_the_wire(bot: Bot, fake: FakeAPI) -> None:
    callback_id = fake.push_callback(7, 1001, "a")
    payload = fake.queue[-1]["callback"]
    query = CallbackQuery.bind(CallbackQueryBase.from_json(payload), bot)
    assert await query.answer("done") is True
    (call,) = fake.calls("POST", f"/bot/v1/callbacks/{callback_id}/answer")
    assert call["json"] == {"text": "done"}
    assert fake.callbacks[callback_id]["answered"]


def test_sync_reply_goes_out_quoting_the_message(sync_bot: SyncBot, fake: FakeAPI) -> None:
    message = SyncMessage.bind(MessageBase.from_json(message_json()), sync_bot)
    sent = message.reply("pong")
    (call,) = fake.calls("POST", "/bot/v1/messages")
    assert call["json"]["replyTo"] == 99
    assert isinstance(sent, SyncSentMessage)
